"""condis's models and parsers, built on the shared core models.

two sources describe a product. the empathy search index serves listing rows
as plain json, with euro amounts as numbers and the unit price as display text.
the storefront renders the product sheet on the server and ships its data in
the react server components payload of the page, as a ``productInformation``
object with prices in cents, next to the branch of the category tree it sits
in. both parse into :class:`CondisProduct`; a row leaves the sheet-only fields
empty.

no ean, gtin or barcode exists in either source, so ``ean`` is always
``None``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_cents,
    as_decimal,
    as_euro_text,
    as_identifier,
    as_integer,
    as_items,
    as_object,
    as_text,
    required_text,
)
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
from ._constants import CDN_URL, INDEX_LANGUAGE, PHOTO_SIZE, SITE_URL

__all__ = [
    "CondisCategory",
    "CondisProduct",
    "CondisSearchResult",
    "flight_payload",
    "flight_value",
    "page_url",
    "parse_categories",
    "parse_product",
    "parse_row",
    "parse_search_result",
    "parse_suggestions",
]

# one chunk of the server components payload, as next.js streams it into the
# page: a javascript string literal inside `self.__next_f.push([1, "..."])`
_FLIGHT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
# "0,99€/Litro": the amount, then the unit after the slash
_UNIT_PRICE = re.compile(r"^\s*[\d.,]+\s*€\s*/\s*(?P<unit>.+?)\s*$")
# allergens are bolded inside the ingredients html
_BOLD = re.compile(r"<b\b[^>]*>(.*?)</b>", re.IGNORECASE | re.DOTALL)
# the product page names its own address, with the real slug, in its head
_CANONICAL = re.compile(r'<link\b[^>]*\brel="canonical"[^>]*\bhref="([^"]+)"')
_PROMOTION_KIND = "promotion"
# a tree id joins its ancestors' codes: "c07__cat00210003__cat002100030003"
_TREE_SEPARATOR = "__"


@dataclass(frozen=True, slots=True, kw_only=True)
class CondisCategory(Category):
    """one node of the storefront's three-level category tree.

    :attr:`~supermercapy.Category.id` is the path-shaped id the search index
    browses by, such as ``"c07__cat00210003"``; :attr:`external_id` is the
    node's own code.
    """

    external_id: str | None = None
    children: tuple[CondisCategory, ...] = ()
    products: tuple[CondisProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CondisProduct(Product):
    """one product, from a search index row or from the product sheet.

    :attr:`badges` are the labels the storefront prints on the product card,
    such as the prime subscribers' discount or "marca propia".
    :attr:`section`, :attr:`family` and :attr:`variety` name the product's
    three levels of the category tree, top first. :attr:`category_names` is
    the row's variety, section and family names, in the index's order; the
    sheet leaves it empty.
    """

    section: str | None = None
    family: str | None = None
    variety: str | None = None
    category_names: tuple[str, ...] = ()
    badges: tuple[str, ...] = ()
    manufacturer: str | None = None
    kcal: Decimal | None = None
    net_weight: Decimal | None = None
    is_own_brand: bool = False
    is_eco: bool = False
    is_prime: bool = False
    is_gluten_free: bool = False
    is_lactose_free: bool = False
    is_seasonal: bool = False
    is_delivered_in_48h: bool = False
    has_lowered_price: bool = False
    units_limited: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CondisSearchResult(SearchResult):
    """one page of index rows, continued by the offset of the next row."""

    products: tuple[CondisProduct, ...] = ()
    offset: int = 0


# ------------------------------------------------------------------- payload


def flight_payload(html: str) -> str:
    """return the server components payload a page streamed, as one string.

    every chunk is a javascript string literal, which json reads; a chunk it
    cannot read is skipped rather than failing the page.
    """

    parts: list[str] = []
    for chunk in _FLIGHT_CHUNK.findall(html):
        try:
            parts.append(json.loads(f'"{chunk}"'))
        except ValueError:
            continue
    return "".join(parts)


def flight_value(payload: str, key: str) -> object:
    """return the json value the payload holds under ``key``, or ``None``.

    the payload is not json as a whole, so the value is read from the first
    place the quoted key is followed by a value json can decode. a string
    starting with ``$`` is a reference to a value streamed elsewhere, such as
    ``"$undefined"`` or ``"$8:1:props"``, and is passed over the same way.
    """

    decoder = json.JSONDecoder()
    marker = f'"{key}":'
    start = payload.find(marker)
    while start >= 0:
        try:
            value, _ = decoder.raw_decode(payload, start + len(marker))
        except ValueError:
            value = None
        if value is not None and not (isinstance(value, str) and value[:1] == "$"):
            return value
        start = payload.find(marker, start + 1)
    return None


def page_url(html: str) -> str | None:
    """return the address a page gives as its canonical one."""

    match = _CANONICAL.search(html)
    return match.group(1) if match else None


# ------------------------------------------------------------------- helpers


def _photos(names: object, alt: str) -> tuple[Photo, ...]:
    """return the photos a row or sheet names, as the storefront addresses them.

    both sources name each image by file, such as ``"704049.jpg"``, and the
    storefront asks the image cdn for it at a size; this is the largest it
    asks for, the zoom of the product page. the ``images`` paths the index
    also sends answer http 404.
    """

    return tuple(
        Photo(
            url=f"{CDN_URL}/fit-in/{PHOTO_SIZE}/{INDEX_LANGUAGE}/products/{stem}.jpg",
            alt=alt,
        )
        for item in as_items(names)
        if (stem := (as_text(item) or "").strip().removesuffix(".jpg"))
    )


def _absolute(base: str, path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("http") else f"{base}{path}"


def _slug(url: str | None) -> str | None:
    """return the slug of a product address, relative or absolute."""

    if not url:
        return None
    match = re.match(r"^/([^/]+)/p/", urlsplit(url).path)
    return match.group(1) if match else None


def _flag(value: object) -> bool:
    """read the index's flags, which are booleans or 0 and 1."""

    if isinstance(value, bool):
        return value
    return as_integer(value) == 1


def _price(
    amount: Decimal | None,
    previous: Decimal | None,
    *,
    discounted: bool,
    unit_text: str | None,
) -> Price:
    """build a price whose unit price is the display text ``"0,99€/Litro"``.

    the unit price follows the price the shopper pays, so the reference does
    too.
    """

    unit = None if unit_text is None else _UNIT_PRICE.match(unit_text)
    return Price(
        amount=amount,
        previous=previous,
        unit_price=as_euro_text(unit_text) if unit else None,
        unit_price_unit=unit.group("unit") if unit else None,
        unit_price_text=unit_text or None,
        reference=SHARED_UNITS.unit_price_from_text(unit_text),
        is_discounted=discounted or previous is not None,
    )


def _badges(data: JsonObject) -> tuple[str, ...]:
    flags = as_object(data.get("flags"))
    return tuple(
        text
        for item in as_items(flags.get("plp"))
        if (text := as_text(as_object(item).get("text")))
    )


def _promotion(text: str | None, *, flagged: bool | None) -> tuple[Promotion, ...]:
    """return the promotion a row or sheet describes, if it is flagged as one.

    the same text field carries labels that are no offer, such as "novedad"
    on a new product, so only a product flagged ``on_promotion`` has one.
    """

    text = normalise_whitespace(text or "")
    if not flagged or not text:
        return ()
    return (Promotion(description=text, kind=_PROMOTION_KIND),)


# ----------------------------------------------------------------- index rows


def parse_row(data: object) -> CondisProduct:
    """parse one search index row."""

    value = as_object(data)
    identifier = as_identifier(value.get("id"), "product id")
    name = required_text(value.get("description"), "product name").strip()
    price = as_object(value.get("price"))
    amount = as_decimal(price.get("current"))
    regular = as_decimal(price.get("regular"))
    previous = (
        regular
        if regular is not None and amount is not None and regular > amount
        else None
    )
    on_sale = as_boolean(value.get("on_sale")) or False
    promotions = _promotion(
        as_text(value.get("promotion_text")),
        flagged=as_boolean(value.get("on_promotion")),
    )
    pum = as_text(value.get("pum"))
    url = as_text(value.get("url"))
    photos = _photos(value.get("altImages"), name)
    parent = as_text(value.get("parentCategory"))
    return CondisProduct(
        id=identifier,
        name=name,
        brand=as_text(value.get("brand")) or None,
        slug=_slug(url),
        url=_absolute(SITE_URL, url),
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=_price(amount, previous, discounted=on_sale, unit_text=pum),
        availability=Availability(
            available=None
            if as_text(value.get("state")) is None
            else as_text(value.get("state")) == "ACTIVE",
            status=as_text(value.get("state")),
        ),
        category_ids=(parent,) if parent else (),
        promotions=promotions,
        is_new=as_boolean(value.get("is_novelty")) or False,
        is_variable_weight=_flag(value.get("variable_weight")),
        section=as_text(value.get("section")),
        family=as_text(value.get("family")),
        variety=as_text(value.get("variety")),
        category_names=tuple(
            text for item in as_items(value.get("category")) if (text := as_text(item))
        ),
        badges=_badges(value),
        kcal=as_decimal(value.get("kcal")),
        net_weight=as_decimal(value.get("netWeight")),
        is_own_brand=_flag(value.get("condis_brand")),
        is_eco=_flag(value.get("isEco")),
        is_prime=_flag(value.get("isPrime")),
        is_gluten_free=_flag(value.get("without_gluten")),
        is_lactose_free=_flag(value.get("without_lactose")),
        is_seasonal=_flag(value.get("isSeasonable")),
        is_delivered_in_48h=_flag(value.get("delivery48h")),
        has_lowered_price=_flag(value.get("has_lowered_price")),
        units_limited=_flag(value.get("unitLimited")),
    )


def parse_search_result(
    data: object, *, query: str, offset: int, page_size: int, max_start: int
) -> CondisSearchResult:
    """parse one index response into a page and the offset that follows it.

    the index refuses an offset beyond ``max_start``, so a page that would
    need one is flagged ``truncated`` instead of offering a cursor.
    """

    catalog = as_object(as_object(data).get("catalog"))
    products = tuple(
        parse_row(item) for item in as_items(catalog.get("content")) if as_object(item)
    )
    total = as_integer(catalog.get("numFound"))
    following = offset + len(products)
    has_more = bool(products) and (total is None or following < total)
    beyond = following > max_start
    return CondisSearchResult(
        query=query,
        products=products,
        page_size=page_size,
        total_hits=total,
        next_cursor=str(following) if has_more and not beyond else None,
        truncated=has_more and beyond,
        offset=offset,
    )


def parse_suggestions(data: object) -> tuple[str, ...]:
    """parse the index's autocomplete answer into distinct suggestions."""

    content = as_object(as_object(data).get("topTrends")).get("content")
    suggestions: list[str] = []
    for item in as_items(content):
        entry = as_object(item)
        text = as_text(entry.get("title_raw")) or as_text(entry.get("title"))
        if text and text.strip() and text.strip() not in suggestions:
            suggestions.append(text.strip())
    return tuple(suggestions)


# ------------------------------------------------------------- product sheet


def _allergens(html: str | None) -> str | None:
    terms: list[str] = []
    for match in _BOLD.findall(html or ""):
        term = normalise_whitespace(strip_tags(match))
        if term and term not in terms:
            terms.append(term)
    return ", ".join(terms) or None


def _nutrition(info: JsonObject) -> Nutrition | None:
    facts = as_items(as_object(info.get("nutritional_info")).get("nutritional_facts"))
    values: list[NutritionValue] = []
    for item in facts:
        fact = as_object(item)
        name = as_text(fact.get("description"))
        amount = as_text(fact.get("amount_per_100g"))
        unit = as_text(fact.get("unit_of_measure"))
        if not name:
            continue
        values.append(
            NutritionValue(
                name=name,
                per_100=" ".join(part for part in (amount, unit) if part) or None,
                unit=unit,
            )
        )
    html = as_text(info.get("ingredients"))
    ingredients = normalise_whitespace(strip_tags(html or "")) or None
    allergens = _allergens(html)
    if not (values or ingredients):
        return None
    return Nutrition(
        ingredients=ingredients,
        allergens=allergens,
        values=tuple(values),
        per="100 g" if values else None,
    )


def _text(value: object) -> str | None:
    text = as_text(value)
    return text.strip() or None if text is not None else None


def _category_path(
    branch: object, section: object, leaf_code: str | None
) -> tuple[CondisCategory, ...]:
    """return the product's place in the tree, top first, or nothing.

    the sheet names its leaf category by code only; the page around it holds
    the leaf's parent with its children, and the name of the top level. the
    path is built only when the leaf is found among those children.
    """

    node = as_object(branch)
    family_id = as_text(node.get("id"))
    family_name = _text(node.get("name"))
    if not leaf_code or not family_id or not family_name:
        return ()
    leaf = next(
        (
            child
            for item in as_items(node.get("children"))
            if as_text((child := as_object(item)).get("externalId")) == leaf_code
        ),
        None,
    )
    leaf_id = None if leaf is None else as_text(leaf.get("id"))
    leaf_name = None if leaf is None else _text(leaf.get("name"))
    if not leaf_id or not leaf_name:
        return ()
    # a node's level is the number of ancestors its id joins
    nested = _TREE_SEPARATOR in family_id
    root_id = family_id.split(_TREE_SEPARATOR, 1)[0]
    root_name = _text(section)
    path: list[CondisCategory] = []
    if nested and root_name:
        path.append(
            CondisCategory(id=root_id, name=root_name, level=0, external_id=root_id)
        )
    path.append(
        CondisCategory(
            id=family_id,
            name=family_name,
            parent_id=root_id if nested else None,
            level=family_id.count(_TREE_SEPARATOR),
            external_id=as_text(node.get("externalId")),
        )
    )
    path.append(
        CondisCategory(
            id=leaf_id,
            name=leaf_name,
            parent_id=family_id,
            level=leaf_id.count(_TREE_SEPARATOR),
            external_id=leaf_code,
        )
    )
    return tuple(path)


def parse_product(
    info: object,
    *,
    branch: object = None,
    section: object = None,
    url: str | None = None,
) -> CondisProduct:
    """parse the ``productInformation`` object of a product page.

    prices are in cents. ``sale_price`` is zero unless the product is on
    sale, in which case it is what the shopper pays and ``list_price`` the
    price before. ``branch`` and ``section`` are the page's ``matchedChild``
    and ``parentCategoryName``, which place the product in the tree, and
    ``url`` the page's canonical address.
    """

    value = as_object(info)
    identifier = as_identifier(value.get("ID"), "product id")
    name = required_text(value.get("description"), "product name").strip()
    listed = as_cents(value.get("list_price"))
    sale = as_cents(value.get("sale_price"))
    on_sale = sale is not None and sale > 0
    amount = sale if on_sale else listed
    previous = (
        listed
        if on_sale and listed is not None and sale is not None and listed > sale
        else None
    )
    um_price = _text(value.get("um_price"))
    photos = _photos(value.get("alt_images"), name)
    path = _category_path(branch, section, as_text(value.get("parent_category_id")))
    bulk = as_object(value.get("in_bulk_info"))
    manufacturer = as_object(value.get("manufacturer"))
    return CondisProduct(
        id=identifier,
        name=name,
        brand=_text(value.get("brand")),
        slug=_slug(url),
        url=url,
        pack_size_text=_text(value.get("net_amount")),
        thumbnail=photos[0] if photos else None,
        photos=photos,
        price=_price(
            amount,
            previous,
            discounted=on_sale or as_boolean(value.get("on_sale")) is True,
            unit_text=um_price,
        ),
        category_ids=(path[-1].id,) if path else (),
        category_path=path,
        promotions=_promotion(
            as_text(value.get("promotional_info")),
            flagged=as_boolean(value.get("on_promotion")),
        ),
        is_new=as_boolean(value.get("is_novelty")) or False,
        is_variable_weight=as_boolean(value.get("variable_weight")) or False,
        description=_text(value.get("long_description")),
        usage=_text(value.get("instructions")),
        origin=_text(bulk.get("origin")),
        nutrition=_nutrition(value),
        section=path[0].name if len(path) == 3 else None,
        family=path[-2].name if path else None,
        variety=_text(value.get("parent_category_description")),
        manufacturer=_text(manufacturer.get("name")),
        is_eco=as_boolean(value.get("is_eco")) or False,
        is_prime=as_boolean(value.get("is_prime")) or False,
        is_gluten_free=as_boolean(value.get("gluten_free")) or False,
        is_lactose_free=as_boolean(value.get("lactose_free")) or False,
        is_seasonal=as_boolean(value.get("is_seasonal")) or False,
        is_delivered_in_48h=as_boolean(value.get("delivery_48h")) or False,
        units_limited=as_boolean(value.get("units_limited")) or False,
    )


# --------------------------------------------------------------- categories


def _category(
    data: object, *, parent_id: str | None, level: int
) -> CondisCategory | None:
    value = as_object(data)
    identifier = as_text(value.get("id"))
    name = as_text(value.get("name"))
    if not identifier or not name:
        return None
    children = tuple(
        child
        for item in as_items(value.get("children"))
        if (child := _category(item, parent_id=identifier, level=level + 1)) is not None
    )
    return CondisCategory(
        id=identifier,
        name=name.strip(),
        parent_id=parent_id,
        level=level,
        children=children,
        external_id=as_text(value.get("externalId")),
    )


def parse_categories(data: object) -> tuple[CondisCategory, ...]:
    """parse the ``categoryList`` the storefront's navigation renders."""

    return tuple(
        category
        for item in as_items(data)
        if (category := _category(item, parent_id=None, level=0)) is not None
    )
