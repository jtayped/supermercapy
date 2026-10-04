from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from supermercapy import Nutrition, Product, Store, Unit, UnitPrice
from supermercapy.alcampo import (
    AlcampoCategory,
    AlcampoPhoto,
    AlcampoProduct,
    AlcampoStore,
    ProductType,
)
from supermercapy.alcampo.models import PARSER, parse_stores
from tests.conftest import read_fixture

STORE = "alcampo"
IMAGES = "https://www.compraonline.alcampo.es/images-v3"
BANNER = "37ea0506-72ec-4543-93c8-a77bb916ec12"
FRUITS = "25fd70b8-959e-4633-b14a-0cee7cd3669a"


@pytest.fixture
def sheet() -> Any:
    return read_fixture(STORE, "product.json")


@pytest.fixture
def listing() -> Any:
    return read_fixture(STORE, "category.json")


def rows(name: str) -> list[Any]:
    data = read_fixture(STORE, name)
    return [
        row for group in data["productGroups"] for row in group["decoratedProducts"]
    ]


# -------------------------------------------------------------- product sheet


def test_the_sheet_carries_both_identities_and_no_ean(sheet: Any) -> None:
    product = PARSER.detail(sheet)

    assert isinstance(product, AlcampoProduct)
    assert isinstance(product, Product)
    assert product.id == "54180"
    assert product.product_uuid == "176cfd5e-afa9-4aa1-8b6f-fe3d757c535e"
    assert product.name.startswith("AUCHAN Leche semidesnatada")
    assert product.brand == "PRODUCTO ALCAMPO"
    assert product.pack_size_text == "6000ml"
    assert product.product_type is ProductType.REGULAR
    assert product.ean is None


def test_the_price_reads_the_unit_both_ways(sheet: Any) -> None:
    price = PARSER.detail(sheet).price

    assert price.amount == Decimal("5.28")
    assert price.unit_price == Decimal("0.88")
    assert price.unit_name == "PER_LITRE"
    assert price.unit_message_key == "fop.price.per.litre"
    assert price.reference == UnitPrice(amount=Decimal("0.88"), unit=Unit.LITRE)
    # alcampo never states a "before" price in its promotion prose
    assert price.previous is None
    assert price.is_discounted is True


def test_the_flyer_window_stays_in_the_promotion_description(sheet: Any) -> None:
    product = PARSER.detail(sheet)

    descriptions = [promotion.description for promotion in product.promotions]
    assert "Producto en Folleto (24/09/26_07/10/26)" in descriptions
    assert product.promotions[0].starts_at is None
    assert product.promotions[-1].long_description is not None


def test_the_nutrition_table_reads_its_th_header_row(sheet: Any) -> None:
    nutrition = PARSER.detail(sheet).nutrition

    assert isinstance(nutrition, Nutrition)
    # "valores medios por:" heads the table in a row of <th> cells
    assert nutrition.per == "100g"
    assert nutrition.values[0].name == "Valor energético (Kj)"
    assert nutrition.values[0].per_100 == "193.0 Kj"
    assert [value.name for value in nutrition.values][-1] == "Sal"
    assert nutrition.ingredients == (
        "Ingredientes: LECHE SEMIDESNATADA DE VACA. ORIGEN DE LA LECHE: ESPAÑA."
    )


def test_the_features_table_gives_the_legal_name_and_the_origin(sheet: Any) -> None:
    product = PARSER.detail(sheet)

    assert product.legal_name == "LECHE UHT SEMIDESNATADA"
    assert product.origin == "España"
    assert product.storage is not None
    assert product.storage.startswith("Conservar a temperatura ambiente")
    assert product.description is not None
    assert product.field("brand") == "PRODUCTO ALCAMPO"
    raw = product.raw_field("features")
    assert raw is not None and raw.startswith("<table>")
    assert product.field("winemaker") is None


def test_a_sheet_without_features_keeps_the_shared_reading(sheet: Any) -> None:
    sheet["bopData"]["fields"] = [
        field for field in sheet["bopData"]["fields"] if field["title"] != "features"
    ]
    sheet["bopData"]["fields"].append({"title": "unitType", "content": "Leche UHT"})

    product = PARSER.detail(sheet)

    assert product.legal_name == "Leche UHT"
    assert product.origin is None


def test_the_breadcrumbs_become_the_category_path(sheet: Any) -> None:
    product = PARSER.detail(sheet)

    assert [category.name for category in product.category_path] == [
        "Leche, Huevos, Lácteos, Yogures y Bebidas vegetales",
        "Leche",
        "Leche semidesnatada",
    ]
    assert isinstance(product.category_path[0], AlcampoCategory)
    assert product.category_ids == tuple(c.id for c in product.category_path)


def test_photos_are_addressable_at_every_square_rendition(sheet: Any) -> None:
    photo = PARSER.detail(sheet).photos[0]

    assert isinstance(photo, AlcampoPhoto)
    assert photo.url.startswith(f"{IMAGES}/{BANNER}/")
    assert photo.base_url is not None
    assert photo.sized() == f"{photo.base_url}/500x500.jpg"
    assert photo.sized(800, image_format="webp") == f"{photo.base_url}/800x800.webp"
    with pytest.raises(ValueError, match="size"):
        photo.sized(512)


def test_ratings_are_read_and_an_unrated_product_has_none() -> None:
    by_id = {
        product.id: product
        for product in (PARSER.product(row) for row in rows("category.json"))
    }

    assert by_id["55452"].rating == Decimal("1.0")
    assert by_id["55452"].rating_count == 4
    # "0.0" from no reviews at all is not a zero rating
    assert by_id["74694"].rating is None
    assert by_id["74694"].rating_count == 0
    assert PARSER.product({"retailerProductId": "1", "name": "x"}).rating is None


# ------------------------------------------------------------------- listings


def test_a_catchweight_row_reports_an_approximate_price(listing: Any) -> None:
    product = PARSER.product(listing["productGroups"][0]["decoratedProducts"][0])

    assert product.id == "74694"
    assert product.product_type is ProductType.CATCHWEIGHT
    assert product.is_variable_weight is True
    assert product.price.is_approximate is True
    assert product.catchweight is not None
    assert product.catchweight.typical is not None
    assert product.catchweight.typical.value == Decimal("1000")
    assert product.catchweight.typical.unit == "G"
    assert product.price.reference == UnitPrice(
        amount=Decimal("1.69"), unit=Unit.KILOGRAM
    )


def test_the_listing_page_carries_its_filters_and_sort_options(listing: Any) -> None:
    page = PARSER.search_result(listing, query="", page_size=30)

    assert [product.id for product in page.products] == ["74694", "55452", "59772"]
    assert page.products[0].group_type == "on_offer"
    assert page.products[0].is_sponsored is False
    assert [group.id for group in page.filters] == ["boolean", "brands", "dummyValue"]
    assert [item.id for item in page.filters[2].attributes] == [
        "new",
        "alcampoBrandGeneric",
    ]
    assert page.sort_options[0] == "favorite"
    # the category listing has no next page: three hundred rows hold it all
    assert page.next_cursor is None


def test_the_category_echoes_its_count_and_takes_the_page_children(
    listing: Any,
) -> None:
    category = PARSER.category(
        listing,
        products=PARSER.listing_products(listing),
        children=PARSER.page_categories(listing),
    )

    assert category is not None
    assert category.id == FRUITS
    assert category.retailer_category_id == "OC1701"
    assert category.product_count == 235
    assert category.children
    assert len(category.products) == 3


def test_search_pages_carry_a_token() -> None:
    page = PARSER.search_result(
        read_fixture(STORE, "search.json"), query="leche", page_size=30
    )

    assert page.next_cursor == "bc6d1b6c-cf6c-48db-a1a5-947ef0f4ed8e"
    assert page.total_hits is None
    assert [product.id for product in page.products][:2] == ["54180", "54178"]


@pytest.mark.parametrize(
    ("product_id", "unit_name", "expected"),
    [
        ("224414", "EACH", UnitPrice(amount=Decimal("1.00"), unit=Unit.PIECE)),
        ("50743", "PER_LITRE", UnitPrice(amount=Decimal("0.20"), unit=Unit.LITRE)),
        # six eggs for a euro, two euros the dozen
        ("633548", "PER_DOZEN", UnitPrice(amount=Decimal("0.1667"), unit=Unit.PIECE)),
        # 0,90 € for 45 g published as 0,02 €/g: too coarse to restate per kg
        ("88201", None, None),
    ],
)
def test_promotion_rows_read_the_unit_off_the_message_key(
    product_id: str, unit_name: str | None, expected: UnitPrice | None
) -> None:
    row = next(
        row for row in rows("promotions.json") if row["retailerProductId"] == product_id
    )

    price = PARSER.product(row).price

    assert price.unit_name == unit_name
    assert price.reference == expected


def test_the_per_gram_figure_is_kept_as_published() -> None:
    row = next(
        row for row in rows("promotions.json") if row["retailerProductId"] == "88201"
    )

    price = PARSER.product(row).price

    assert price.unit_price == Decimal("0.02")
    assert price.unit_message_key == "fop.price.per.gram"


# ---------------------------------------------------------------- categories


def test_the_tree_nests_and_keeps_the_retailer_codes() -> None:
    categories = PARSER.categories(read_fixture(STORE, "categories.json"))

    assert [category.retailer_category_id for category in categories] == [
        "OCFYP",
        "OC2112",
        "OC16",
    ]
    child = categories[1].children[0]
    assert child.parent_id == categories[1].id
    assert child.level == 1
    assert child.children[0].level == 2


# -------------------------------------------------------------------- stores


def test_collection_points_parse_into_stores_with_their_region() -> None:
    stores = parse_stores(read_fixture(STORE, "stores.json"))

    assert all(isinstance(store, AlcampoStore) for store in stores)
    assert all(isinstance(store, Store) for store in stores)
    laguna = next(store for store in stores if store.name == "La Laguna Hipermercado")
    assert laguna.id == "9c767992-4437-4cad-a914-0ff6ce7eebfa"
    assert laguna.region_id == "5c2a56e0-dc3b-4d29-8b67-24f8b3c78f38"
    assert laguna.postal_code == "38108"
    # the address is free text, so the town is not read out of it
    assert laguna.city is None
    assert laguna.kind == "SERVICED"
    assert laguna.latitude is not None and laguna.longitude is not None
    lockers = [store for store in stores if store.kind == "LOCKER"]
    assert lockers


def test_a_point_without_an_id_a_region_or_a_name_is_dropped() -> None:
    point = read_fixture(STORE, "stores.json")[0]
    broken = [
        {**point, "deliveryDestinationId": None},
        {**point, "resolvedRegionId": ""},
        {**point, "name": "  "},
        "junk",
    ]

    assert parse_stores(broken) == ()


def test_a_point_without_coordinates_or_a_postal_code_still_parses() -> None:
    point = read_fixture(STORE, "stores.json")[0]
    del point["coordinates"]
    # thirteen of 294 points sent this literal text in october 2026
    point["postalCode"] = "EMPTY"

    (store,) = parse_stores([point])

    assert store.latitude is None and store.longitude is None
    assert store.postal_code is None
    assert store.address is not None
