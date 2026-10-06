"""bonàrea's models and parsers, built on the shared core models.

bonàrea returns the same article object for a listing row, a search hit and a
product detail, so there is one :class:`BonareaProduct` rather than a summary
and a detail type; the detail adds a breadcrumb and two html blobs that the
listing leaves empty.

two things shape the parsers. ids exist in two spellings — ``13*5361`` in the
json api and ``13_5361`` in urls and image names — and :func:`to_api_id` and
:func:`to_url_id` translate between them. descriptions, ingredients, allergens
and the nutrition table are delivered as one html string per product, so they
are extracted with :mod:`supermercapy._core.html`; the markup is kept on the model
as ``raw_general_info`` and ``raw_extended_info`` for callers who would rather
read it themselves.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from html import unescape

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_euro_text,
    as_identifier,
    as_integer,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.exceptions import ConfigurationError, InvalidResponseError
from .._core.html import normalise_whitespace, strip_tags, strong_sections
from .._core.models import (
    Availability,
    Category,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    SearchResult,
)
from .._core.units import UnitReader
from ._constants import API_URL, IMAGE_URL

__all__ = [
    "BonareaAvailability",
    "BonareaCategory",
    "BonareaPhoto",
    "BonareaPrice",
    "BonareaProduct",
    "BonareaSearchResult",
    "BonareaVariant",
    "BonareaVariantGroup",
    "Characteristic",
    "listing_categories",
    "to_api_id",
    "to_url_id",
]

_UNIT_PRICE_UNIT = re.compile(r"€\s*/\s*(\S+)")
# "€/d." is per dose and "€/k." per kilogram. "€/ml." is on prices that are
# per litre: 12,17 € for 200 ml is labelled 60,85 €/ml.
_UNITS = UnitReader({"d": "dosis", "k": "kg", "ml": "l"})
_COLUMN_GAP = re.compile(r"\s{2,}")
_MEASUREMENT_VALUE = re.compile(r"^[<>]?\s*-?\d+(?:[.,]\d+)?\s+(\S+)")
_HEADING_TRIM = " \t:*.-"


class Characteristic(StrEnum):
    """the badges bonàrea tags an article with.

    the wire values are catalan constants in every locale, so they are an
    enumeration rather than display text; the storefront keeps the translated
    labels in its own javascript bundle. unrecognised constants stay in
    :attr:`BonareaProduct.characteristics` as plain strings.
    """

    OWN_BRAND = "BONAREA"
    GUARANTEE = "GARANTIA_BONAREA"
    ANIMAL_WELFARE = "BENESTAR_ANIMAL_AENOR"
    TRACEABILITY = "TRACABILITAT"
    CHILLED = "REFRIGERAT"
    FROZEN = "CONGELAT"
    GLUTEN_FREE = "SENSE_GLUTEN"
    HALAL = "HALAL"
    LOCALLY_SOLD = "VENDA_PROXIMITAT"
    PRICE_DROP = "BAIXADA_PREU"
    NEW = "NOU_PRODUCTE"


# the section headings observed in ``generalInfo`` and ``extendedInfo``, in
# both storefront languages, mapped to the field each one fills.
_SECTIONS: Mapping[str, str] = {
    "descripción": "description",
    "descripció": "description",
    "conservación": "storage",
    "conservació": "storage",
    "modo de uso": "usage",
    "mode ús": "usage",
    "origen": "origin",
    "ingredientes": "ingredients",
    "ingredients": "ingredients",
    "alérgenos": "allergens",
    "al·lèrgens": "allergens",
    "información nutricional": "nutrition",
    "informació nutricional": "nutrition",
    "información adicional": "additional_info",
    "informació addicional": "additional_info",
    "denominación": "legal_name",
    "denominació": "legal_name",
    "nombre i dirección del operador": "operator",
    "nom i adreça de l'operador": "operator",
}


def to_api_id(value: str | int) -> str:
    """return the ``13*5361`` spelling the json api expects.

    urls, image names and breadcrumb links separate the segments of an id with
    ``_``; the api answers ``{"article": null}`` for that spelling, so every
    id entering a request passes through here.
    """

    return str(value).strip().replace("_", "*")


def to_url_id(value: str | int) -> str:
    """return the ``13_5361`` spelling urls and image names use."""

    return str(value).strip().replace("*", "_")


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaPhoto(Photo):
    """one article image on the image cdn, resizable through :meth:`sized`.

    ``url`` addresses the original upload; the storefront itself asks for
    500 px in a grid and 1000 px on a detail page.
    """

    file_name: str

    def sized(self, width: int, height: int) -> str:
        """return the same image resized by the cdn, without performing i/o."""

        for label, value in (("width", width), ("height", height)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ConfigurationError(f"{label} must be a positive integer")
        return f"{self.url}?width={width}&height={height}"


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaPrice(Price):
    """what one selling unit costs.

    bonàrea publishes no previous price, no tax breakdown and no offer
    records, so ``previous``, ``tax_percentage`` and ``discount_percentage``
    are always ``None``; :attr:`Characteristic.PRICE_DROP` is the only signal
    that a price has fallen, and it sets ``is_discounted``.
    """

    selling_unit: str | None = None
    raw_discount: Decimal | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaAvailability(Availability):
    """whether an article can be bought, and in what quantities.

    ``days_without_service`` holds the storefront's own weekday codes, whose
    encoding is undocumented, and is only populated once a fulfilment mode has
    been chosen, which a read-only client never does.
    """

    max_stock: int | None = None
    is_published: bool = True
    grid_buy_allowed: bool = True
    days_without_service: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaCategory(Category):
    """one node of the storefront category tree.

    ``product_count`` counts the rows a listing actually returned: the
    ``totalArticles`` the storefront sends is zero on every node.
    """

    url: str | None = None
    icon: str | None = None
    children: tuple[BonareaCategory, ...] = ()
    products: tuple[BonareaProduct, ...] = ()

    @property
    def url_id(self) -> str:
        """return the id in the ``13_300_010`` spelling category urls use."""

        return to_url_id(self.id)


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaVariant:
    """one article inside a variant group, with the values that select it."""

    product_id: str
    first_value: str | None = None
    second_value: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaVariantGroup:
    """one ``agrupacions`` entry: an axis along which an article varies.

    ``first_label`` and the values inside ``variants`` follow the language of
    the request, but ``name`` arrives in the other one — a spanish listing
    names the group in catalan and a catalan listing names it in spanish. the
    value is passed through as it comes. ``first_kind`` says how the storefront
    renders the axis, such as ``FOTO`` for a strip of thumbnails.
    """

    id: str
    product_id: str | None = None
    name: str | None = None
    template: str | None = None
    first_label: str | None = None
    first_kind: str | None = None
    second_label: str | None = None
    second_kind: str | None = None
    variants: tuple[BonareaVariant, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaProduct(Product):
    """one article, in the single shape bonàrea uses everywhere.

    ``ean`` is always ``None``: no barcode is published in the json, the page
    markup or any metadata, so a bonàrea article cannot be cross-referenced
    against another retailer. ``brand`` is ``None`` for the same reason —
    there is no brand field, only the name and the
    :attr:`Characteristic.OWN_BRAND` badge.
    """

    thumbnail: BonareaPhoto | None = None
    photos: tuple[BonareaPhoto, ...] = ()
    price: BonareaPrice = BonareaPrice()
    availability: BonareaAvailability = BonareaAvailability()
    category_path: tuple[BonareaCategory, ...] = ()
    characteristics: tuple[str, ...] = ()
    variants: tuple[BonareaVariantGroup, ...] = ()
    family_name: str | None = None
    weight_grams: int | None = None
    additional_info: str | None = None
    operator: str | None = None
    raw_general_info: str | None = None
    raw_extended_info: str | None = None

    @property
    def url_id(self) -> str:
        """return the id in the ``13_5361`` spelling urls and images use."""

        return to_url_id(self.id)

    def has(self, characteristic: Characteristic | str) -> bool:
        """return whether the article carries one badge."""

        return str(characteristic) in self.characteristics


@dataclass(frozen=True, slots=True, kw_only=True)
class BonareaSearchResult(SearchResult):
    """one page of hits, sliced from the single response search returns.

    ``total_hits`` is the whole result set, not the page, because the
    storefront sends every match at once and paging happens here.
    """

    offset: int = 0
    results_url: str | None = None
    products: tuple[BonareaProduct, ...] = ()


# --------------------------------------------------------------------- parsers


def _display(value: object, label: str) -> str:
    """return a display name with its html entities decoded.

    names travel escaped because the storefront injects them straight into the
    page with ``.html()``, so ``M&amp;m's`` is one product, not two.
    """

    return unescape(required_text(value, label))


def _optional_display(value: object) -> str | None:
    """return a display name that may be absent, with its entities decoded."""

    text = as_text(value)
    return unescape(text) if text else None


def _slug(url: str | None) -> str | None:
    """return the second-to-last path segment, which is always the slug."""

    if url is None:
        return None
    segments = [segment for segment in url.split("/") if segment]
    return segments[-2] if len(segments) >= 2 else None


def _absolute(url: str | None) -> str | None:
    """anchor a category link on the storefront host.

    the tree sends ``categorias/...`` and a breadcrumb ``~/categorias/...``,
    asp.net's application-relative spelling; both resolve to the same page
    at the site root.
    """

    if url is None or url.startswith(("http://", "https://")):
        return url
    return f"{API_URL}/{url.lstrip('~/')}"


def _photos(data: object) -> tuple[BonareaPhoto, ...]:
    photos: list[BonareaPhoto] = []
    for item in as_items(data):
        name = as_text(item)
        if name is None or not name.strip():
            continue
        file_name = name.strip()
        photos.append(BonareaPhoto(url=f"{IMAGE_URL}/{file_name}", file_name=file_name))
    return tuple(photos)


def _characteristics(data: object) -> tuple[str, ...]:
    return tuple(
        text
        for item in as_items(data)
        if (text := as_text(as_object(item).get("descripcio"))) is not None and text
    )


def _variant(data: object) -> BonareaVariant | None:
    value = as_object(data)
    product_id = as_text(value.get("codiArticle"))
    if product_id is None or not product_id.strip():
        return None
    return BonareaVariant(
        product_id=to_api_id(product_id),
        first_value=as_text(value.get("filtre1")) or None,
        second_value=as_text(value.get("filtre2")) or None,
    )


def parse_variant_group(data: object) -> BonareaVariantGroup:
    """parse one ``agrupacions`` entry and the articles it groups."""

    value = as_object(data)
    product_id = as_text(value.get("codiArticle")) or None
    return BonareaVariantGroup(
        id=as_identifier(value.get("agrupacio"), "variant group id"),
        product_id=None if product_id is None else to_api_id(product_id),
        name=_optional_display(value.get("descripcio")),
        template=as_text(value.get("idPlantilla")) or None,
        first_label=as_text(value.get("titolFiltre1")) or None,
        first_kind=as_text(value.get("tipusFiltre1")) or None,
        second_label=as_text(value.get("titolFiltre2")) or None,
        second_kind=as_text(value.get("tipusFiltre2")) or None,
        variants=tuple(
            variant
            for item in as_items(value.get("filtres"))
            if (variant := _variant(item)) is not None
        ),
    )


def _price(data: JsonObject, *, characteristics: tuple[str, ...]) -> BonareaPrice:
    unit_price_text = as_text(data.get("unitPrice")) or None
    unit_match = (
        None if unit_price_text is None else _UNIT_PRICE_UNIT.search(unit_price_text)
    )
    return BonareaPrice(
        amount=as_decimal(data.get("priceToPay")),
        unit_price=as_euro_text(unit_price_text),
        unit_price_unit=None if unit_match is None else unit_match.group(1),
        unit_price_text=unit_price_text,
        reference=_UNITS.unit_price_from_text(unit_price_text),
        is_discounted=Characteristic.PRICE_DROP in characteristics,
        is_approximate=as_boolean(data.get("isAnApproximatePrice")) or False,
        selling_unit=as_text(data.get("euroUnit")) or None,
        raw_discount=as_decimal(data.get("discount")),
    )


def _availability(data: JsonObject) -> BonareaAvailability:
    in_stock = as_boolean(data.get("itsOnStock"))
    max_stock = as_integer(data.get("maximumStock"))
    return BonareaAvailability(
        available=in_stock,
        status=None
        if in_stock is None
        else ("in_stock" if in_stock else "out_of_stock"),
        max_quantity=None if max_stock is None else Decimal(max_stock),
        min_quantity=as_decimal(data.get("unitatMinimaComanda")),
        max_stock=max_stock,
        is_published=as_boolean(data.get("isValid")) is not False,
        grid_buy_allowed=as_boolean(data.get("isGridBuyAllowed")) is not False,
        days_without_service=tuple(
            code
            for item in as_items(data.get("daysWithoutService"))
            if (code := as_text(item)) is not None
        ),
    )


def _field(heading: str) -> str | None:
    """map one section heading, in either language, to the field it fills."""

    return _SECTIONS.get(heading.strip(_HEADING_TRIM).casefold())


def _sections(html: str | None) -> dict[str, str]:
    """map the known headings of one html blob to their text."""

    found: dict[str, str] = {}
    for section in strong_sections(html or ""):
        field = _field(section.heading)
        if field is None or field in found:
            continue
        text = _section_text(section.body)
        if text:
            found[field] = text
    return found


def _section_lines(body: str) -> list[str]:
    """return the non-empty lines of a section body, footnotes excluded."""

    return [
        line
        for raw in strip_tags(body).splitlines()
        # a line opening with '*' is the storefront's "labels may be more
        # current than this page" footnote, which trails the last section
        if (line := raw.rstrip()) and not line.lstrip().startswith("*")
    ]


def _section_text(body: str) -> str:
    return normalise_whitespace(" ".join(_section_lines(body)))


def _nutrition_value(line: str) -> NutritionValue | None:
    """parse one ``-name<gap>measurement`` row of the table."""

    columns = _COLUMN_GAP.split(line.strip().lstrip("-").strip(), maxsplit=1)
    name = normalise_whitespace(columns[0])
    if not name:
        return None
    measurement = normalise_whitespace(columns[1]) if len(columns) > 1 else ""
    unit = _MEASUREMENT_VALUE.match(measurement)
    return NutritionValue(
        name=name,
        per_100=measurement or None,
        unit=None if unit is None else unit.group(1).rstrip(",;.") or None,
    )


def _nutrition_table(body: str) -> tuple[str | None, tuple[NutritionValue, ...]]:
    """split the table body into its lead-in line and its rows."""

    per: str | None = None
    values: list[NutritionValue] = []
    for line in _section_lines(body):
        if line.lstrip().startswith("-"):
            value = _nutrition_value(line)
            if value is not None:
                values.append(value)
        elif per is None:
            per = normalise_whitespace(line).rstrip(":") or None
    return per, tuple(values)


def parse_nutrition(extended_info: str | None) -> Nutrition | None:
    """parse the ingredient, allergen and nutrition blob of a product detail.

    returns ``None`` when the blob carries none of the three, which is the
    case for every listing row: ``extendedInfo`` is empty outside a product
    detail. the table is column-aligned plain text inside one ``<p>``, so each
    row keeps its measurement exactly as the storefront wrote it, european
    decimal comma and ``< 0,5`` bounds included.
    """

    ingredients: str | None = None
    allergens: str | None = None
    per: str | None = None
    values: tuple[NutritionValue, ...] = ()
    for section in strong_sections(extended_info or ""):
        field = _field(section.heading)
        if field == "ingredients" and ingredients is None:
            ingredients = _section_text(section.body) or None
        elif field == "allergens" and allergens is None:
            allergens = _section_text(section.body) or None
        elif field == "nutrition" and not values:
            per, values = _nutrition_table(section.body)
    if ingredients is None and allergens is None and not values:
        return None
    return Nutrition(
        ingredients=ingredients,
        allergens=allergens,
        values=values,
        per=per,
        raw_html=extended_info,
    )


def parse_product(data: object) -> BonareaProduct:
    """parse one article from a listing, a search hit or a product detail."""

    value = as_object(data)
    identifier = to_api_id(as_identifier(value.get("identifier"), "article identifier"))
    name = _display(value.get("description"), "article description")
    characteristics = _characteristics(value.get("caracteristiques"))
    photos = _photos(value.get("image"))
    url = as_text(value.get("urlFriendly")) or None
    breadcrumb = parse_breadcrumb(value.get("breadcrumb"))
    general = _sections(as_text(value.get("generalInfo")))
    extended = _sections(as_text(value.get("extendedInfo")))
    return BonareaProduct(
        id=identifier,
        name=name,
        slug=_slug(url),
        url=url,
        pack_size_text=as_text(value.get("measurementUnit")) or None,
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=_price(value, characteristics=characteristics),
        availability=_availability(value),
        category_ids=tuple(category.id for category in breadcrumb),
        category_path=breadcrumb,
        description=general.get("description"),
        legal_name=extended.get("legal_name"),
        origin=general.get("origin"),
        storage=general.get("storage"),
        usage=general.get("usage"),
        nutrition=parse_nutrition(as_text(value.get("extendedInfo"))),
        is_new=Characteristic.NEW in characteristics,
        is_variable_weight=as_boolean(value.get("isAnApproximatePrice")) or False,
        characteristics=characteristics,
        variants=tuple(
            parse_variant_group(item) for item in as_items(value.get("agrupacions"))
        ),
        family_name=as_text(value.get("nameFamilySubfamily")) or None,
        weight_grams=as_integer(value.get("weightGrams")),
        additional_info=extended.get("additional_info"),
        operator=extended.get("operator"),
        raw_general_info=as_text(value.get("generalInfo")) or None,
        raw_extended_info=as_text(value.get("extendedInfo")) or None,
    )


def _level(identifier: str) -> int:
    return identifier.count("*")


def parse_breadcrumb(data: object) -> tuple[BonareaCategory, ...]:
    """parse a product detail's breadcrumb into its ancestor categories."""

    categories: list[BonareaCategory] = []
    parent_id: str | None = None
    for item in as_items(data):
        value = as_object(item)
        identifier = to_api_id(as_identifier(value.get("idNivell"), "category id"))
        url = as_text(value.get("url")) or None
        categories.append(
            BonareaCategory(
                id=identifier,
                name=_display(value.get("descripcio"), "category name"),
                parent_id=parent_id,
                level=_level(identifier),
                slug=_slug(url),
                url=_absolute(url),
            )
        )
        parent_id = identifier
    return tuple(categories)


def parse_category(data: object, *, parent_id: str | None = None) -> BonareaCategory:
    """parse one node of the category tree and every node beneath it."""

    value = as_object(data)
    identifier = to_api_id(as_identifier(value.get("identifier"), "category id"))
    url = as_text(value.get("url")) or None
    return BonareaCategory(
        id=identifier,
        name=_display(value.get("descripcio"), "category name"),
        parent_id=parent_id,
        level=_level(identifier),
        slug=_slug(url),
        url=_absolute(url),
        icon=as_text(value.get("icon")) or None,
        children=tuple(
            parse_category(item, parent_id=identifier)
            for item in as_items(value.get("children"))
        ),
    )


def parse_category_tree(data: object) -> tuple[BonareaCategory, ...]:
    """parse the ``nivells`` tree every listing response carries."""

    if not isinstance(data, list):
        raise InvalidResponseError("listing response has no category tree")
    return tuple(parse_category(item) for item in data)


def listing_categories(
    categories: tuple[BonareaCategory, ...],
) -> tuple[BonareaCategory, ...]:
    """return the nodes worth listing, one per branch of the tree.

    level 3 is the listing level and aggregates its level-4 children, so a
    walk stops there. a level-2 node is a menu that returns nothing — unless
    it has no children of its own, in which case it is the listing itself.
    level 1 only returns a curated handful, so it is skipped when it has
    children.
    """

    nodes: list[BonareaCategory] = []
    for root in categories:
        if not root.children:
            nodes.append(root)
            continue
        for child in root.children:
            nodes.extend(child.children or (child,))
    return tuple(nodes)


def parse_articles(data: object, *, label: str) -> tuple[BonareaProduct, ...]:
    """parse the ``articles`` array a listing or a search envelope carries."""

    rows = as_object(data).get("articles")
    if not isinstance(rows, list):
        raise InvalidResponseError(f"{label} response has no articles array")
    return tuple(parse_product(item) for item in rows)


def parse_search_result(
    data: object,
    *,
    query: str,
    offset: int,
    page_size: int,
) -> BonareaSearchResult:
    """slice one page out of the single response search returns."""

    value = as_object(data)
    products = parse_articles(value, label="search")
    page = products[offset : offset + page_size]
    following = offset + page_size
    return BonareaSearchResult(
        query=query,
        products=page,
        page_size=page_size,
        offset=offset,
        total_hits=len(products),
        next_cursor=str(following) if following < len(products) else None,
        results_url=as_text(value.get("url")) or None,
    )


def parse_postal_codes(data: object) -> tuple[str, ...]:
    """parse the postal codes of one province, keeping the order they arrive.

    the storefront repeats a code once per town it serves, so duplicates are
    dropped here.
    """

    if not isinstance(data, list):
        raise InvalidResponseError("postal code response is not a json array")
    codes = (text for item in data if (text := (as_text(item) or "").strip()))
    return tuple(dict.fromkeys(codes))
