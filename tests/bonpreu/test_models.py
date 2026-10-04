from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from supermercapy import Nutrition, Product, Unit, UnitPrice
from supermercapy.bonpreu import (
    BonpreuPhoto,
    BonpreuProduct,
    ProductType,
    encode_filters,
)
from supermercapy.bonpreu.models import (
    next_page_token,
    parse_categories,
    parse_category,
    parse_decorated,
    parse_detail,
    parse_nutrition,
    parse_product,
    parse_product_ids,
    parse_search_result,
    parse_suggestions,
)
from tests.conftest import read_fixture

STORE = "bonpreu"
IMAGES = "https://www.compraonline.bonpreuesclat.cat/images-v3"
TENANT = "dcbcfd72-cf23-44a2-8e14-8a38edd645a3"


@pytest.fixture
def sheet() -> Any:
    return read_fixture(STORE, "product.json")


@pytest.fixture
def listing() -> Any:
    return read_fixture(STORE, "category.json")


def row(listing: Any, index: int = 0) -> Any:
    return listing["productGroups"][0]["decoratedProducts"][index]


# -------------------------------------------------------------- product sheet


def test_the_sheet_carries_both_identities(sheet: Any) -> None:
    product = parse_detail(sheet)

    assert isinstance(product, BonpreuProduct)
    assert isinstance(product, Product)
    assert product.id == "29189"
    assert product.product_uuid == "f3ea1055-906f-430a-b024-8bd03feadb9b"
    assert product.name == "GALLO Farina de rebosteria"
    assert product.brand == "GALLO"
    assert product.pack_size_text == "1kg"
    assert product.product_type is ProductType.REGULAR
    assert product.is_in_current_catalog is True


def test_no_product_can_ever_carry_an_ean(sheet: Any) -> None:
    # confirmed three ways during reconnaissance: live responses, the
    # frontend's own response schema, and the page's json-ld
    assert parse_detail(sheet).ean is None


def test_prices_arrive_as_decimal_strings_with_a_machine_readable_unit(
    sheet: Any,
) -> None:
    price = parse_detail(sheet).price

    assert price.amount == Decimal("1.69")
    assert price.currency == "EUR"
    assert price.unit_price == Decimal("1.69")
    # `unit` is an i18n message key; `unitName` is the value to branch on
    assert price.unit_name == "PER_1KG"
    assert price.unit_message_key == "fop.price.per.kg"
    assert price.reference == UnitPrice(amount=Decimal("1.69"), unit=Unit.KILOGRAM)


def test_the_breadcrumbs_become_the_category_path(sheet: Any) -> None:
    product = parse_detail(sheet)

    assert [category.name for category in product.category_path] == [
        "Alimentació",
        "Pastes, arrossos, farines i llegums",
        "Farines i sèmoles",
        "Farines de rebosteria i especials",
    ]
    assert product.category_path[1].parent_id == product.category_path[0].id
    assert product.category_ids == tuple(
        category.id for category in product.category_path
    )


def test_a_sheet_without_breadcrumbs_falls_back_to_the_name_path(sheet: Any) -> None:
    sheet["bopData"].pop("breadcrumbs")

    product = parse_detail(sheet)

    assert [category.name for category in product.category_path] == [
        "Alimentació",
        "Pastes, arrossos, farines i llegums",
        "Farines i sèmoles",
        "Farines de rebosteria i especials",
    ]
    assert product.category_ids == ()


def test_every_descriptive_field_is_kept_raw_and_stripped(sheet: Any) -> None:
    product = parse_detail(sheet)

    assert [field.title for field in product.fields] == [
        "brand",
        "furtherDescription",
        "ingredients",
        "nutritionalData",
        "cookingGuidelines",
    ]
    assert (
        product.field("cookingGuidelines")
        == "Conservar en lloc sec i aïllat del terra."
    )
    raw = product.raw_field("cookingGuidelines")
    assert raw is not None and raw.startswith("<br />")
    assert product.field("winemaker") is None
    assert product.raw_field("winemaker") is None
    assert product.storage == "Conservar en lloc sec i aïllat del terra."
    assert product.description == "Farina de Rebosteria Gallo en paquet de 1 kg"


def test_a_title_outside_the_known_set_is_still_kept(sheet: Any) -> None:
    sheet["bopData"]["fields"].append({"title": "somethingNew", "content": "<b>x</b>"})

    product = parse_detail(sheet)

    assert product.field("somethingNew") == "x"


def test_the_nutrition_table_is_parsed_out_of_its_html(sheet: Any) -> None:
    nutrition = parse_detail(sheet).nutrition

    assert isinstance(nutrition, Nutrition)
    assert nutrition.per == "per 100 g"
    assert [value.name for value in nutrition.values] == [
        "Valor energètic",
        "Greixos",
        "dels quals saturats",
        "Hidrats de carboni",
        "dels quals sucres",
        "Proteïnes",
        "Sal",
    ]
    assert nutrition.values[0].per_100 == "363 kcal / 1537 kJ"
    assert nutrition.values[-1].per_100 == "0,03 g"
    assert nutrition.raw_html is not None
    assert nutrition.raw_html.startswith("<table>")


def test_allergens_are_read_from_the_bolded_words_of_the_ingredients(
    sheet: Any,
) -> None:
    # there is no allergens field on this product; the storefront bolds them
    # inside the ingredients html instead
    nutrition = parse_detail(sheet).nutrition

    assert nutrition is not None
    assert nutrition.ingredients == "INGREDIENTS: farina de blat."
    assert nutrition.allergens == "blat"


def test_a_standalone_allergens_field_wins_over_the_bolded_words(sheet: Any) -> None:
    sheet["bopData"]["fields"].append(
        {"title": "allergens", "content": "Conté <b>gluten</b>."}
    )

    nutrition = parse_detail(sheet).nutrition

    assert nutrition is not None
    assert nutrition.allergens == "Conté gluten."


def test_a_product_with_no_sheet_fields_has_no_nutrition(sheet: Any) -> None:
    sheet["bopData"]["fields"] = []

    assert parse_detail(sheet).nutrition is None
    assert parse_nutrition(()) is None


def test_a_three_column_nutrition_table_keeps_the_serving_column() -> None:
    fields = (
        {
            "title": "nutritionalData",
            "content": (
                "<table><tr><td></td><th>per 100 g</th><th>per ració</th></tr>"
                "<tr><td>Sal</td><td>0,5 g</td><td>0,1 g</td></tr></table>"
            ),
        },
    )
    product = parse_detail(
        {
            "product": {"retailerProductId": "1", "name": "x"},
            "bopData": {"fields": list(fields)},
        }
    )

    assert product.nutrition is not None
    assert product.nutrition.per == "per 100 g"
    assert product.nutrition.values[0].per_serving == "0,1 g"


def test_photos_are_addressable_at_every_square_rendition(sheet: Any) -> None:
    photo = parse_detail(sheet).photos[0]
    base = f"{IMAGES}/{TENANT}/1369342e-b25a-4888-9783-af82983f5345"

    assert isinstance(photo, BonpreuPhoto)
    assert photo.url == f"{base}/300x300.jpg"
    assert photo.image_id == "1369342e-b25a-4888-9783-af82983f5345"
    assert photo.sized() == f"{base}/500x500.jpg"
    assert photo.sized(800, image_format="webp") == f"{base}/800x800.webp"
    assert photo.sized("1280x1280") == f"{base}/1280x1280.jpg"


@pytest.mark.parametrize("size", [512, "600x400", "", 0])
def test_a_rendition_outside_the_ladder_is_refused(size: Any) -> None:
    photo = BonpreuPhoto(url="https://example.invalid/x/500x500.jpg", base_url="x")

    with pytest.raises(ValueError, match="size"):
        photo.sized(size)


def test_an_unknown_image_format_is_refused() -> None:
    photo = BonpreuPhoto(url="https://example.invalid/x/500x500.jpg", base_url="x")

    with pytest.raises(ValueError, match="image_format"):
        photo.sized(500, image_format="avif")


def test_a_photo_with_no_rendition_suffix_keeps_its_own_url() -> None:
    photo = BonpreuPhoto(url="https://example.invalid/banner.png")

    assert photo.base_url is None
    assert photo.sized(800) == "https://example.invalid/banner.png"


def test_image_paths_stand_in_when_no_image_object_is_published() -> None:
    base = f"{IMAGES}/{TENANT}/1369342e-b25a-4888-9783-af82983f5345"
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "imageIds": ["1369342e-b25a-4888-9783-af82983f5345"],
            "imagePaths": [base],
        }
    )

    assert product.photos[0].url == f"{base}/500x500.jpg"
    assert product.photos[0].base_url == base
    assert product.photos[0].image_id == "1369342e-b25a-4888-9783-af82983f5345"


def test_detailed_images_are_appended_to_the_gallery(sheet: Any) -> None:
    sheet["detailedImages"] = [
        {"imageUrl": "https://example.invalid/a.jpg", "altText": "back of pack"}
    ]

    product = parse_detail(sheet)

    assert product.photos[-1].url == "https://example.invalid/a.jpg"
    assert product.photos[-1].kind == "detail"
    assert product.photos[-1].alt == "back of pack"


# ------------------------------------------------------------------- listings


def test_a_listing_row_parses_into_the_same_product_shape(listing: Any) -> None:
    product = parse_product(row(listing))

    assert product.id == "03643"
    assert product.name == "Nectarina Km0 1 u."
    assert product.brand is None
    assert product.nutrition is None
    assert product.fields == ()


def test_the_before_price_is_read_out_of_the_promotion_prose(listing: Any) -> None:
    # no numeric field carries it; "Abans 0,58€" is all there is
    product = parse_product(row(listing))

    assert product.price.amount == Decimal("0.49")
    assert product.price.previous == Decimal("0.58")
    assert product.price.is_discounted is True
    assert product.promotions[0].kind == "OFFER"
    assert product.promotions[0].retailer_id == "OF03643_2608250001"
    assert product.promotions[0].description == "Abans 0,58€"


def test_the_spanish_spelling_of_the_before_price_is_read_too(listing: Any) -> None:
    entry = row(listing)
    entry["promotions"][0]["description"] = "Antes 1.234,50 €"

    assert parse_product(entry).price.previous == Decimal("1234.50")


def test_a_promotion_that_is_not_a_before_price_leaves_it_unset(listing: Any) -> None:
    entry = row(listing)
    entry["promotions"][0]["description"] = "2n a meitat de preu"

    product = parse_product(entry)

    assert product.price.previous is None
    # a promotion still marks the row as discounted
    assert product.price.is_discounted is True


def test_a_loyalty_promotion_is_flagged_member_only() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "promotions": [
                {
                    "promoId": "p",
                    "type": "LOYALTY",
                    "description": "targeta",
                    "requiredProductQuantity": 2,
                    "isMultiBuy": True,
                    "equivalentPrice": {"totalPrice": {"amount": "1.20"}},
                }
            ],
        }
    )

    assert product.promotions[0].member_only is True
    assert product.promotions[0].requires_quantity == 2
    assert product.promotions[0].is_multi_buy is True
    assert product.promotions[0].price == Decimal("1.20")


def test_sponsored_rows_are_flagged_by_advert_and_by_group() -> None:
    page = parse_search_result(
        read_fixture(STORE, "search.json"), query="llet", page_size=30
    )

    sponsored = [product for product in page.products if product.is_sponsored]
    assert [product.id for product in sponsored] == ["49637", "49644"]
    assert sponsored[0].group_type == "featured"
    assert sponsored[0].campaign_id == "61rWVS03lHw85dO286vITU"
    assert sponsored[0].external_advert_id is not None
    assert page.products[2].is_sponsored is False
    assert page.products[2].group_type == "personalized"


def test_a_product_placed_in_two_groups_is_returned_once() -> None:
    data = read_fixture(STORE, "search.json")
    data["productGroups"].append(data["productGroups"][0])

    page = parse_search_result(data, query="llet", page_size=30)

    assert len(page.products) == len({product.id for product in page.products})


def test_the_page_carries_its_filters_and_sort_options(listing: Any) -> None:
    page = parse_search_result(listing, query="", page_size=30)

    assert [group.id for group in page.filters] == [
        "boolean",
        "brands",
        "dietaryAndLifestyle",
    ]
    assert page.filters[0].kind == "BOOLEAN"
    assert page.filters[0].attributes[0].id == "onOffer"
    assert page.filters[0].attributes[0].selected is False
    assert page.sort_options[0] == "favorite"


def test_an_empty_page_ends_the_walk_even_with_a_token_present() -> None:
    data = read_fixture(STORE, "listing_page2.json")

    assert next_page_token(data) is not None
    assert parse_search_result(data, query="", page_size=30).next_cursor is None


def test_a_page_with_no_metadata_has_no_cursor() -> None:
    assert next_page_token({"metadata": {"nextPageToken": ""}}) is None
    assert next_page_token({}) is None
    assert next_page_token("not an object") is None


# ------------------------------------------------------------------ catchweight


def test_a_catchweight_product_reports_an_approximate_price() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "Pollastre sencer",
            "type": "CATCHWEIGHT",
            "price": {"amount": "7.50", "currency": "EUR"},
            "catchweight": {
                "minQuantity": {"uom": "kg", "value": "1.2"},
                "maxQuantity": {"uom": "kg", "value": "1.8"},
                "typicalQuantity": {"uom": "kg", "value": "1.5"},
            },
        }
    )

    assert product.product_type is ProductType.CATCHWEIGHT
    assert product.is_variable_weight is True
    assert product.price.is_approximate is True
    assert product.catchweight is not None
    assert product.catchweight.minimum is not None
    assert product.catchweight.minimum.value == Decimal("1.2")
    assert product.catchweight.maximum is not None
    assert product.catchweight.maximum.value == Decimal("1.8")
    assert product.catchweight.typical is not None
    assert product.catchweight.typical.value == Decimal("1.5")
    assert product.catchweight.typical.unit == "kg"


def test_a_product_without_a_catchweight_block_has_none() -> None:
    product = parse_product({"retailerProductId": "1", "name": "x"})

    assert product.catchweight is None
    assert product.is_variable_weight is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("REGULAR", ProductType.REGULAR),
        ("VARIABLE_PRICE", ProductType.VARIABLE_PRICE),
        ("SOMETHING_NEW", ProductType.UNKNOWN),
        (None, ProductType.UNKNOWN),
    ],
)
def test_an_unknown_product_type_degrades_rather_than_raising(
    value: Any, expected: ProductType
) -> None:
    data: dict[str, Any] = {"retailerProductId": "1", "name": "x"}
    if value is not None:
        data["type"] = value

    assert parse_product(data).product_type is expected


def test_an_age_restricted_product_is_flagged_from_any_of_its_markers() -> None:
    for marker in ("alcohol", "ageRestriction", "medicalQuestionnaireRequired"):
        product = parse_product({"retailerProductId": "1", "name": "x", marker: True})
        assert product.requires_age_check is True
    assert parse_product(
        {"retailerProductId": "1", "name": "x", "verifyMode": "UNDERAGE"}
    ).requires_age_check


def test_availability_reads_the_published_flags() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "available": False,
            "maxAvailableQuantity": 4,
            "timeRestricted": True,
        }
    )

    assert product.availability.available is False
    assert product.availability.status == "sold_out"
    assert product.availability.max_quantity == Decimal("4")
    assert product.is_time_restricted is True


# ------------------------------------------------------------------ categories


def test_the_tree_nests_and_keeps_the_retailer_codes() -> None:
    categories = parse_categories(read_fixture(STORE, "categories.json"))

    assert [category.name for category in categories] == [
        "Frescos",
        "Alimentació",
        "Novetats",
    ]
    assert categories[0].retailer_category_id == "03"
    assert categories[0].level == 0
    child = categories[0].children[0]
    assert child.level == 1
    assert child.parent_id == categories[0].id
    assert categories[2].children == ()


def test_a_category_node_without_an_id_or_a_name_is_dropped() -> None:
    assert parse_categories([{"name": "no id"}, {"categoryId": "x"}, "junk"]) == ()


# --------------------------------------------------------------------- filters


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ({"boolean": "onOffer"}, "boolean=onOffer"),
        (
            {"dietaryAndLifestyle": ["eco", "vegan"]},
            "dietaryAndLifestyle=eco,vegan",
        ),
        ({"brands": "LA COLLITA"}, "brands=LA%20COLLITA"),
        ({"brands": ("COCA-COLA",)}, "brands=COCA-COLA"),
        (
            {"boolean": "onOffer", "dietaryAndLifestyle": ["eco"]},
            "boolean=onOffer&dietaryAndLifestyle=eco",
        ),
        ({}, ""),
    ],
)
def test_filters_serialise_the_way_the_storefront_serialises_them(
    selection: Any, expected: str
) -> None:
    assert encode_filters(selection) == expected


@pytest.mark.parametrize("selection", ["boolean=onOffer", ["eco"], None, 3])
def test_a_filter_selection_that_is_not_a_mapping_is_refused(selection: Any) -> None:
    with pytest.raises(TypeError, match="mapping"):
        encode_filters(selection)


# -------------------------------------------------------- uuid lists and batch


def test_uuid_lists_are_read_from_both_shapes_and_deduplicated() -> None:
    assert parse_product_ids(["a", "b", "a", 7, None]) == ("a", "b")
    assert parse_product_ids([{"productId": "a"}, {"productId": "b"}]) == ("a", "b")
    assert parse_product_ids({}) == ()


def test_the_batch_response_parses_into_products() -> None:
    products = parse_decorated(read_fixture(STORE, "batch.json"))

    assert [product.id for product in products] == ["03643", "07464"]
    assert parse_decorated({}) == ()


def test_suggestions_are_read_as_a_flat_array_of_strings() -> None:
    assert parse_suggestions(read_fixture(STORE, "suggestions.json"))[0] == "llet"
    assert parse_suggestions([1, "a", None]) == ("a",)
    assert parse_suggestions("not an array") == ()


# ---------------------------------------------------- defensive parsing


def test_a_filter_value_that_is_not_text_is_stringified() -> None:
    assert encode_filters({"minRating": 4}) == "minRating=4"


def test_an_unusable_image_path_is_skipped() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "imagePaths": [None, "https://example.invalid/x"],
        }
    )

    assert [photo.url for photo in product.photos] == [
        "https://example.invalid/x/500x500.jpg"
    ]


def test_a_before_price_with_no_number_falls_through_to_the_next_promotion() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "promotions": [
                {"promoId": "a", "description": "Abans"},
                {"promoId": "b", "description": "Abans 2,10€"},
            ],
        }
    )

    assert product.price.previous == Decimal("2.10")


def test_a_catchweight_range_with_only_one_quantity_leaves_the_rest_unset() -> None:
    product = parse_product(
        {
            "retailerProductId": "1",
            "name": "x",
            "type": "CATCHWEIGHT",
            "catchweight": {"typicalQuantity": {"uom": "kg", "value": "1.5"}},
        }
    )

    assert product.catchweight is not None
    assert product.catchweight.minimum is None
    assert product.catchweight.maximum is None


def test_a_sheet_field_missing_a_title_or_a_body_is_dropped(sheet: Any) -> None:
    sheet["bopData"]["fields"] = [
        {"title": "storage"},
        {"content": "orphan"},
        {"title": "storage", "content": "sec"},
    ]

    product = parse_detail(sheet)

    assert [field.title for field in product.fields] == ["storage"]
    assert product.storage == "sec"


def test_nutrition_rows_that_name_nothing_or_measure_nothing_are_dropped(
    sheet: Any,
) -> None:
    sheet["bopData"]["fields"] = [
        {
            "title": "nutritionalData",
            "content": (
                "<table><tr><td></td><td>per 100 g</td></tr>"
                "<tr><td></td><td>orphan</td></tr>"
                "<tr><td>Sal</td></tr>"
                "<tr><td>Greixos</td><td>2 g</td></tr></table>"
            ),
        }
    ]

    nutrition = parse_detail(sheet).nutrition

    assert nutrition is not None
    assert [value.name for value in nutrition.values] == ["Greixos"]


def test_a_breadcrumb_without_an_id_or_a_name_is_dropped(sheet: Any) -> None:
    sheet["bopData"]["breadcrumbs"] = [
        {"categoryName": "no id"},
        {"categoryId": "x", "categoryName": "Alimentació"},
    ]

    product = parse_detail(sheet)

    assert product.category_ids == ("x",)


def test_a_detailed_image_without_a_url_is_dropped(sheet: Any) -> None:
    sheet["detailedImages"] = [{"altText": "no url"}]

    assert len(parse_detail(sheet).photos) == 1


def test_a_filter_group_without_an_id_is_dropped(listing: Any) -> None:
    listing["additionalPageInfo"]["filters"].insert(0, {"label": "no id"})

    page = parse_search_result(listing, query="", page_size=30)

    assert [group.id for group in page.filters] == [
        "boolean",
        "brands",
        "dietaryAndLifestyle",
    ]


def test_a_listing_with_no_page_info_names_no_category(listing: Any) -> None:
    listing.pop("additionalPageInfo")
    page = parse_search_result(listing, query="", page_size=30)

    assert parse_category(listing, page=page) is None


def test_a_category_that_names_its_own_children_keeps_them(listing: Any) -> None:
    listing["additionalPageInfo"]["currentCategory"]["childCategories"] = [
        {"name": "Fruita", "categoryId": "9d33281d", "retailerCategoryId": "030101"}
    ]
    page = parse_search_result(listing, query="", page_size=30)

    category = parse_category(listing, page=page)

    assert category is not None
    assert [child.name for child in category.children] == ["Fruita"]


def test_a_repeated_or_empty_bold_term_is_not_listed_twice(sheet: Any) -> None:
    sheet["bopData"]["fields"] = [
        {
            "title": "ingredients",
            "content": "farina de <b>blat</b>, <b></b> midó de <b>blat</b>, <b>ou</b>.",
        }
    ]

    nutrition = parse_detail(sheet).nutrition

    assert nutrition is not None
    assert nutrition.allergens == "blat, ou"


def test_a_promotions_row_without_a_unit_name_reads_it_off_the_message_key() -> None:
    # since october 2026 the promotions listing sends numbers rather than
    # strings, and the unit-price message key without its `unitName`
    rows = read_fixture(STORE, "promotions.json")["productGroups"][0]
    by_id = {
        product.id: product
        for product in (parse_product(item) for item in rows["decoratedProducts"])
    }

    ham, beer, burger = by_id["23201"], by_id["36338"], by_id["42519"]
    assert ham.price.amount == Decimal("3.09")
    assert ham.price.unit_price == Decimal("30.9")
    assert ham.price.unit_name == "PER_1KG"
    assert ham.price.unit_price_unit == "PER_1KG"
    assert ham.price.unit_message_key == "fop.price.per.kg"
    assert beer.price.unit_name == "PER_LITRE"
    # a promotional price replaces the shelf price
    assert burger.price.amount == Decimal("4.42")
    assert burger.price.is_discounted is True
    assert ham.price.reference == UnitPrice(amount=Decimal("30.90"), unit=Unit.KILOGRAM)
    assert beer.price.reference == UnitPrice(amount=Decimal("2.64"), unit=Unit.LITRE)


def test_the_reference_price_follows_a_promotional_price() -> None:
    rows = read_fixture(STORE, "promotions.json")["productGroups"][0]
    burger = next(
        parse_product(item)
        for item in rows["decoratedProducts"]
        if item["retailerProductId"] == "42519"
    )

    # the unit price and the reference both follow the promotional amount
    assert burger.price.unit_price == Decimal("11.05")
    assert burger.price.reference == UnitPrice(
        amount=Decimal("11.05"), unit=Unit.KILOGRAM
    )
    # a promoted row whose promotional unit price is missing has none
    row = {
        "retailerProductId": "1",
        "name": "x",
        "price": {"amount": 1, "currency": "EUR"},
        "promoPrice": {"amount": 0.8, "currency": "EUR"},
        "unitPrice": {"price": {"amount": 2}, "unit": "fop.price.per.kg"},
    }
    assert parse_product(row).price.reference is None
    assert parse_product(row).price.unit_price is None


@pytest.mark.parametrize(
    ("unit", "unit_name", "amount", "expected"),
    [
        # every unit seen on october 2026 searches, both spellings
        ("fop.price.per.each", "EACH", "0.38", ("0.38", Unit.PIECE)),
        ("fop.price.per.kg", "PER_1KG", "2.99", ("2.99", Unit.KILOGRAM)),
        ("fop.price.per.litre", "PER_LITRE", "1.16", ("1.16", Unit.LITRE)),
        ("fop.price.per.100ml", "PER_100ML", "1.50", ("15.00", Unit.LITRE)),
        # ten eggs at 4,85 €
        ("fop.price.per.dozen", "PER_DOZEN", "5.82", ("0.485", Unit.PIECE)),
        ("fop.price.per.dozen", None, "5.82", ("0.485", Unit.PIECE)),
        (None, "PER_DOZEN", "5.82", ("0.485", Unit.PIECE)),
        ("fop.price.per.furlong", None, "2", None),
        (None, None, "2", None),
    ],
)
def test_every_unit_price_unit_is_normalised(
    unit: str | None,
    unit_name: str | None,
    amount: str,
    expected: tuple[str, Unit] | None,
) -> None:
    row = {
        "retailerProductId": "1",
        "name": "x",
        "price": {"amount": "1.00", "currency": "EUR"},
        "unitPrice": {"price": {"amount": amount}, "unit": unit, "unitName": unit_name},
    }

    reference = parse_product(row).price.reference
    assert reference == (
        None
        if expected is None
        else UnitPrice(amount=Decimal(expected[0]), unit=expected[1])
    )


def test_an_unknown_message_key_leaves_the_unit_name_empty() -> None:
    row = {
        "retailerProductId": "1",
        "name": "x",
        "price": {"amount": 1, "currency": "EUR"},
        "unitPrice": {"price": {"amount": 2}, "unit": "fop.price.per.furlong"},
    }

    price = parse_product(row).price

    assert price.unit_name is None
    assert price.unit_message_key == "fop.price.per.furlong"
