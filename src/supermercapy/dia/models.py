"""dia's models and parsers, built on the shared core models.

dia sends one row shape for search, category and offer listings, and a richer
sheet for one product, so there is one :class:`DiaProduct` for both. a row
carries the brand and no nutrition; the sheet carries ingredients, the
nutrition table, storage and preparation text, and no brand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_identifier,
    as_integer,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.exceptions import InvalidResponseError
from .._core.html import normalise_whitespace, strip_tags
from .._core.models import (
    Availability,
    Category,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
)
from .._core.units import SHARED_UNITS
from ._constants import NEW_STAMP_CODE, SITE_URL

__all__ = [
    "DiaAvailability",
    "DiaCategory",
    "DiaNutrition",
    "DiaPrice",
    "DiaProduct",
    "DiaPromotion",
    "DiaSearchResult",
]

# allergens are bolded inside the ingredients html rather than listed apart
_STRONG = re.compile(r"<strong\b[^>]*>(.*?)</strong>", re.IGNORECASE | re.DOTALL)
# an image path names its view: _ISO_ (the pack), _FRO_ (front), _TRA_ (back)
_IMAGE_VIEW = re.compile(r"_([A-Z]{3})_\d+_[A-Z]{2}\.\w+$")
_CATEGORY_LINK = re.compile(r"/c/(L\d+)$")


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaPrice(Price):
    """what one selling unit costs now.

    ``is_club_price`` marks a price that only club dia members pay; an online
    order needs a club dia account, so it is what an online shopper pays.
    """

    is_club_price: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaAvailability(Availability):
    """stock as the bound store reports it; zero means not available there."""

    units_in_stock: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaPromotion(Promotion):
    """one entry of a row's ``promotions`` array.

    ``description`` is the shelf-label text, such as ``"20% DTO. PULEVA"`` or
    ``"2ª UD AL 50% DTO. SELECCIÓN ALPRO"``; no dates or prices are sent with
    it. ``member_only`` is set for club dia promotions.
    """

    short_description: str | None = None
    is_online_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaNutrition(Nutrition):
    """the sheet's nutrition table, with the energy line kept apart.

    the table is quoted per ``per``, such as ``"100 ml"``; the rows land in
    ``per_100`` when that is a hundred grams or millilitres and in
    ``per_serving`` otherwise, including when the sheet does not say.
    """

    energy_kcal: Decimal | None = None
    energy_kj: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaCategory(Category):
    """one node of the two-level category tree.

    ``url`` is the storefront path of the category page, in the client's
    language, such as ``"/frutas/c/L105"``.
    """

    url: str | None = None
    children: tuple[DiaCategory, ...] = ()
    products: tuple[DiaProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaProduct(Product):
    """one product, from a listing row or from the product sheet.

    rows fill ``brand``, ``url``, ``is_dia_brand`` and ``stamp``; the sheet
    fills ``photos``, ``category_path``, ``nutrition`` and the text fields.
    ``id`` is the sku, which is what every endpoint and page url speaks; a pack
    of six has its own sku with a ``P6`` suffix.
    """

    price: DiaPrice = DiaPrice()
    availability: DiaAvailability = DiaAvailability()
    promotions: tuple[DiaPromotion, ...] = ()
    category_path: tuple[DiaCategory, ...] = ()
    nutrition: DiaNutrition | None = None
    is_dia_brand: bool = False
    brand_type: str | None = None
    stamp: str | None = None
    stamp_code: str | None = None
    net_content: str | None = None
    info_labels: tuple[str, ...] = ()
    manufacturer: str | None = None
    manufacturer_address: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DiaSearchResult(SearchResult):
    """one page of search results or of a category listing.

    ``postal_code`` is the postcode the session priced the page for, when the
    response says; ``category_id`` is the category a listing page belongs to.
    """

    products: tuple[DiaProduct, ...] = ()
    postal_code: str | None = None
    category_id: str | None = None


# --------------------------------------------------------------------- parsers


def absolute_url(path: object) -> str | None:
    """return a storefront path as a full url, leaving full urls alone."""

    text = as_text(path)
    if text is None or not text.strip():
        return None
    if text.startswith(("http://", "https://")):
        return text
    return f"{SITE_URL}/{text.lstrip('/')}"


def category_id_of(link: object) -> str | None:
    """return the category id a storefront category path ends in."""

    text = as_text(link)
    if text is None:
        return None
    match = _CATEGORY_LINK.search(text)
    return None if match is None else match.group(1)


def parse_price(data: object, *, is_variable_weight: bool = False) -> DiaPrice:
    """parse a ``prices`` block; the unit price is the one the shopper pays."""

    value = as_object(data)
    amount = as_decimal(value.get("price"))
    struck = as_decimal(value.get("strikethrough_price"))
    is_promo = as_boolean(value.get("is_promo_price")) or False
    previous = (
        struck
        if is_promo and struck is not None and amount is not None and struck != amount
        else None
    )
    discount = as_decimal(value.get("discount_percentage"))
    unit_price = as_decimal(value.get("price_per_unit"))
    unit = as_text(value.get("measure_unit")) or None
    return DiaPrice(
        amount=amount,
        previous=previous,
        unit_price=unit_price,
        unit_price_unit=unit,
        reference=SHARED_UNITS.unit_price(unit_price, unit),
        currency=as_text(value.get("currency")) or "EUR",
        is_discounted=is_promo,
        discount_percentage=discount if is_promo and discount else None,
        is_approximate=is_variable_weight,
        is_club_price=as_boolean(value.get("is_club_price")) or False,
    )


def parse_promotion(data: object) -> DiaPromotion | None:
    """parse one promotion, or return ``None`` when it carries no text."""

    value = as_object(data)
    description = as_text(value.get("description")) or None
    short = as_text(value.get("short_description")) or None
    if description is None and short is None:
        return None
    return DiaPromotion(
        description=description or short,
        kind=as_text(value.get("document_type")) or None,
        member_only=as_boolean(value.get("only_club_dia")) or False,
        short_description=short,
        is_online_only=as_boolean(value.get("exclusive_online")) or False,
    )


def _promotions(data: object) -> tuple[DiaPromotion, ...]:
    return tuple(
        promotion
        for item in as_items(data)
        if (promotion := parse_promotion(item)) is not None
    )


def _availability(data: JsonObject) -> DiaAvailability:
    stock = as_integer(data.get("units_in_stock"))
    return DiaAvailability(
        available=None if stock is None else stock > 0, units_in_stock=stock
    )


def _is_variable_weight(data: JsonObject) -> bool:
    # loose produce sold "aprox." by weight carries an average piece weight
    return data.get("average_weight") is not None


def _photo(path: object) -> Photo | None:
    url = absolute_url(path)
    if url is None:
        return None
    match = _IMAGE_VIEW.search(url)
    return Photo(url=url, kind=None if match is None else match.group(1))


def parse_row(data: object, *, category_id: str | None = None) -> DiaProduct:
    """parse one listing row from search, a category, or the offers."""

    value = as_object(data)
    sku = as_identifier(value.get("sku_id") or value.get("object_id"), "sku")
    variable = _is_variable_weight(value)
    stamp_code = as_text(value.get("stamp_code")) or None
    return DiaProduct(
        id=sku,
        name=required_text(value.get("display_name"), "product name"),
        brand=as_text(value.get("brand")) or None,
        url=absolute_url(value.get("url")),
        thumbnail=_photo(value.get("image")),
        price=parse_price(value.get("prices"), is_variable_weight=variable),
        availability=_availability(value),
        category_ids=() if category_id is None else (category_id,),
        promotions=_promotions(value.get("promotions")),
        is_new=stamp_code == NEW_STAMP_CODE,
        is_variable_weight=variable,
        is_dia_brand=as_boolean(value.get("dia_brand")) or False,
        brand_type=as_text(value.get("brand_type")) or None,
        stamp=as_text(value.get("stamp_description")) or None,
        stamp_code=stamp_code,
    )


def _text(data: object) -> str | None:
    """return a ``{"title", "text"}`` block's text, stripped of markup."""

    html = as_text(as_object(data).get("text"))
    if html is None:
        return None
    return normalise_whitespace(strip_tags(html)) or None


def _allergens(ingredients_html: str | None) -> str | None:
    """return the allergens bolded inside the ingredients html, if any."""

    if ingredients_html is None:
        return None
    terms: list[str] = []
    for match in _STRONG.findall(ingredients_html):
        term = normalise_whitespace(strip_tags(match))
        if term and term not in terms:
            terms.append(term)
    return ", ".join(terms) or None


def _number_text(value: object) -> str | None:
    number = as_decimal(value)
    return None if number is None else str(number)


def _nutrition_rows(
    data: object, *, per_hundred: bool, rows: list[NutritionValue]
) -> None:
    for item in as_items(data):
        entry = as_object(item)
        name = normalise_whitespace(as_text(entry.get("title")) or "")
        if not name:
            continue
        amount = _number_text(entry.get("value"))
        rows.append(
            NutritionValue(
                name=name,
                per_100=amount if per_hundred else None,
                per_serving=None if per_hundred else amount,
                unit=as_text(entry.get("measure_unit")) or None,
            )
        )
        _nutrition_rows(entry.get("items"), per_hundred=per_hundred, rows=rows)


def parse_nutrition(data: JsonObject) -> DiaNutrition | None:
    """parse the sheet's ingredients and nutrition table, if it has either."""

    ingredients_html = as_text(as_object(data.get("ingredients")).get("text"))
    info = as_object(data.get("nutritional_info"))
    size = as_decimal(as_object(info.get("nutri_size")).get("value"))
    measure = as_text(as_object(info.get("nutri_measurement_unit")).get("value"))
    per = f"{size} {measure}" if size is not None and measure else None
    per_hundred = size == 100 and measure is not None
    table = as_object(info.get("nutritional_values"))
    rows: list[NutritionValue] = []
    _nutrition_rows(table.get("values"), per_hundred=per_hundred, rows=rows)
    _nutrition_rows(
        as_object(info.get("vitamins")).get("values"),
        per_hundred=per_hundred,
        rows=rows,
    )
    energy_kcal = as_decimal(table.get("energy_value"))
    energy_kj = as_decimal(table.get("energy_value_kj"))
    ingredients = _text(data.get("ingredients"))
    if not (rows or ingredients or energy_kcal is not None or energy_kj is not None):
        return None
    return DiaNutrition(
        ingredients=ingredients,
        allergens=_allergens(ingredients_html),
        values=tuple(rows),
        per=per,
        raw_html=ingredients_html,
        energy_kcal=energy_kcal,
        energy_kj=energy_kj,
    )


def _breadcrumb(data: object) -> tuple[DiaCategory, ...]:
    path: list[DiaCategory] = []
    for item in as_items(data):
        entry = as_object(item)
        link = as_text(entry.get("link"))
        identifier = category_id_of(link)
        name = as_text(entry.get("title"))
        if identifier is None or not name:
            continue
        path.append(
            DiaCategory(
                id=identifier,
                name=name,
                parent_id=path[-1].id if path else None,
                level=len(path) + 1,
                url=link,
            )
        )
    return tuple(path)


def parse_product(data: object) -> DiaProduct:
    """parse the product sheet answered by the ``pdp-back`` endpoint."""

    envelope = as_object(data)
    value = envelope.get("product")
    if not isinstance(value, dict):
        raise InvalidResponseError("product sheet has no product object")
    sku = as_identifier(value.get("sku_id"), "sku")
    name = required_text(as_object(value.get("primary_info")).get("title"), "name")
    variable = _is_variable_weight(value)
    photos = tuple(
        photo for item in as_items(value.get("images")) if (photo := _photo(item))
    )
    path = _breadcrumb(value.get("breadcrumb"))
    info = as_object(value.get("product_info"))
    instructions = as_object(value.get("instructions"))
    manufacturer = as_object(value.get("manufacturer_contact"))
    stamp_code = as_text(value.get("stamp_code")) or None
    return DiaProduct(
        id=sku,
        name=name,
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=parse_price(value.get("prices"), is_variable_weight=variable),
        availability=_availability(value),
        category_ids=tuple(category.id for category in path),
        category_path=path,
        promotions=_promotions(value.get("promotions")),
        is_new=stamp_code == NEW_STAMP_CODE,
        is_variable_weight=variable,
        description=as_text(info.get("description")) or None,
        legal_name=as_text(info.get("product")) or None,
        storage=_text(instructions.get("storage_instructions")),
        usage=_text(instructions.get("instructions_for_preparation")),
        nutrition=parse_nutrition(value),
        stamp=as_text(value.get("stamp_description")) or None,
        stamp_code=stamp_code,
        net_content=as_text(info.get("subtitle")) or None,
        info_labels=tuple(
            text
            for item in as_items(value.get("info_labels"))
            if (text := as_text(item))
        ),
        manufacturer=as_text(manufacturer.get("manufacturer_contact_name")) or None,
        manufacturer_address=(
            as_text(manufacturer.get("manufacturer_contact_address")) or None
        ),
    )


def _category(data: object, *, parent_id: str | None, level: int) -> DiaCategory | None:
    value = as_object(data)
    identifier = as_text(value.get("id"))
    name = as_text(value.get("name"))
    if not identifier or not name:
        return None
    children = tuple(
        child
        for item in as_items(value.get("children"))
        # each top-level node lists itself again as "todo …", its own page
        if as_text(as_object(item).get("id")) != identifier
        and (child := _category(item, parent_id=identifier, level=level + 1))
    )
    return DiaCategory(
        id=identifier,
        name=name,
        parent_id=parent_id,
        level=level,
        image_url=absolute_url(value.get("icon")),
        url=as_text(value.get("link")) or None,
        children=children,
    )


def parse_categories(data: object) -> tuple[DiaCategory, ...]:
    """parse the menu into the two-level tree, leaving out the offers entry."""

    value = as_object(data)
    roots = value.get("categories")
    if not isinstance(roots, list):
        raise InvalidResponseError("menu response has no categories array")
    return tuple(
        category
        for item in roots
        if (category := _category(item, parent_id=None, level=1)) is not None
    )
