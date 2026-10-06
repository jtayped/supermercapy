"""carrefour's models and parsers, built on the shared core models.

carrefour publishes the same product in four shapes — a document from the
direct search index, a document from its own proxied search, a listing card
inside a rendered page, and the product block of a product page — and they
agree on almost nothing. there is one public :class:`CarrefourProduct` all
four normalise into, so a caller never has to know which request produced a
row; the fields a shape cannot fill stay ``None``.

two differences survive the normalisation and are worth knowing. the search
index sends prices as numbers and the rendered pages send them as localised
strings (``"7,29 €"``), so both are parsed here. and the app-exclusive price,
the purchase limits, the stock figure and the promotion badges exist only in
the rendered pages, so a summary that came from a search carries none of them.

the analytics beacon urls the search index attaches to every document as
``tagging`` are writes, never reads, and no parser here copies them onto a
model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_decimal,
    as_dmy_date,
    as_euro_text,
    as_identifier,
    as_integer,
    as_iso_datetime,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.exceptions import ConfigurationError, InvalidResponseError
from .._core.html import normalise_whitespace, strip_tags
from .._core.models import (
    Availability,
    Category,
    HomeSection,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
    Store,
)
from .._core.units import SHARED_UNITS
from ._constants import (
    CATEGORY_URL_PATTERN,
    FOOD_ROOT,
    MAX_START,
    PRODUCT_SLUG_PATTERN,
    SITE_URL,
    STATIC_URL,
)

__all__ = [
    "CarrefourCategory",
    "CarrefourHomeSection",
    "CarrefourListing",
    "CarrefourPhoto",
    "CarrefourProduct",
    "CarrefourSearchResult",
    "CarrefourStore",
    "InfoTag",
    "ProductDetail",
    "Restriction",
    "ReviewRating",
    "image_url",
    "normalise_category_id",
    "parse_category_page",
    "parse_drives",
    "parse_home",
    "parse_listing",
    "parse_listing_card",
    "parse_menu",
    "parse_product",
    "parse_search_document",
    "parse_search_result",
    "parse_stores_location",
    "parse_suggestions",
]

_PRODUCT_SLUG = re.compile(PRODUCT_SLUG_PATTERN)
_CATEGORY_URL = re.compile(CATEGORY_URL_PATTERN)
_IMAGE_WIDTH = re.compile(r"/hd_\d+x_/")
_MEASURE = re.compile(r"^\s*(-?[\d.,]+)\s*(.*?)\s*$")
_CATEGORY_ID = re.compile(r"^cat\d+$")
# the search index uses this timestamp as "unset" at both ends of a range
_DATE_SENTINEL = "0001-01-01"

# the spanish labels a product page uses for the free-text fields the core
# model names; everything else stays in ``details`` untouched.
_LEGAL_NAME_LABEL = "denominación legal"
_ORIGIN_LABEL = "país de origen"
_STORAGE_LABEL = "modo conservación"
_USAGE_LABEL = "condiciones y/o fecha de consumo una vez abierto el envase"


# ---------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True, kw_only=True)
class InfoTag:
    """one dietary or handling badge the storefront prints on a product."""

    message: str
    colour: str | None = None
    icon: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Restriction:
    """one purchase limit attached to a product by a campaign."""

    id: str | None = None
    name: str | None = None
    quantity: int | None = None
    kind: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewRating:
    """the aggregate customer rating a product page prints."""

    average: Decimal | None = None
    count: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductDetail:
    """one labelled free-text row from a product page's extra information."""

    name: str
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourPhoto(Photo):
    """one product image served by the on-the-fly width resizer."""

    width: int | None = None

    def sized(self, width: int) -> str:
        """return this image's url rendered at ``width`` pixels.

        any width works — the path segment is a resizer instruction, not a
        preset — but the output is capped at the source resolution, so asking
        for more than the original has returns the original.
        """

        if isinstance(width, bool) or not isinstance(width, int) or width < 1:
            raise ConfigurationError("width must be a positive integer")
        if not _IMAGE_WIDTH.search(self.url):
            raise ConfigurationError("photo url has no resizable width segment")
        return _IMAGE_WIDTH.sub(f"/hd_{width}x_/", self.url, count=1)


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourProduct(Product):
    """one product, however it was fetched.

    ``app_price`` is the price the mobile app charges and can be lower than
    ``price.amount``; it, ``stock``, ``restrictions`` and ``promotions`` come
    only from rendered pages, so a summary returned by :meth:`search_products`
    leaves them empty. ``nutrition``, ``nutri_score``, ``review_rating`` and
    ``details`` come only from a product page.
    """

    app_price: Decimal | None = None
    nutri_score: str | None = None
    restrictions: tuple[Restriction, ...] = ()
    review_rating: ReviewRating | None = None
    info_tags: tuple[InfoTag, ...] = ()
    details: tuple[ProductDetail, ...] = ()
    stock: int | None = None
    sell_pack_unit: int | None = None
    sku_id: str | None = None
    image_stem: str | None = None

    @property
    def has_app_discount(self) -> bool:
        """whether the app price undercuts the price on the website."""

        return (
            self.app_price is not None
            and self.price.amount is not None
            and self.app_price < self.price.amount
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourCategory(Category):
    """one node of the food category tree.

    departments and aisles come from the storefront's navigation menu, and a
    category's own children from its listing page, so ``name`` is the display
    name the storefront prints. ``slug_path`` is the run of slugs in the url,
    which is empty for the handful of nodes that link to a filtered listing
    rather than to a category page.
    """

    url: str | None = None
    slug_path: tuple[str, ...] = ()
    children: tuple[CarrefourCategory, ...] = ()
    products: tuple[CarrefourProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourSearchResult(SearchResult):
    """one page of search hits.

    ``truncated`` is set once the next page would start beyond the index's
    offset ceiling of 2,498 documents; narrow the query or walk the category
    tree to reach the rest.
    """

    offset: int = 0
    products: tuple[CarrefourProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourListing:
    """one page of a rendered category listing."""

    products: tuple[CarrefourProduct, ...] = ()
    total_results: int | None = None
    offset: int = 0
    page_size: int | None = None
    sort_options: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourStore(Store):
    """one sale point or one physical shop.

    the two are separate namespaces. ``kind`` is ``"drive"`` for the sale
    points a client can be bound to and ``"store"`` for the shops a postcode
    lookup returns; only a drive id is accepted as ``sale_point``.
    """

    sap_code: str | None = None
    store_code: str | None = None
    group: str | None = None
    distance_km: float | None = None
    stock_state: str | None = None
    click_and_collect: bool | None = None
    hours: str | None = None
    phone: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CarrefourHomeSection(HomeSection):
    """one product carousel of the rendered supermarket home page.

    ``layout`` is the cms document type, ``featuredProducts`` for every
    carousel. ``id`` is the cms document id and ``name`` its internal name,
    which says how the carousel is filled — the ``auto-ofers-*`` ones are
    the offers of the departments in ``category_ids``.
    """

    id: str | None = None
    name: str | None = None
    description: str | None = None
    category_ids: tuple[str, ...] = ()
    products: tuple[CarrefourProduct, ...] = ()


# --------------------------------------------------------------------- helpers


def normalise_category_id(value: str) -> str:
    """return a category id in the ``cat20093`` spelling urls and filters use."""

    text = value.strip()
    if text.isdigit():
        text = f"cat{text}"
    if not _CATEGORY_ID.fullmatch(text):
        raise ConfigurationError("category_id must look like 'cat20093'")
    return text


def _absolute(url: str | None) -> str | None:
    if url is None:
        return None
    if url.startswith("/"):
        return f"{SITE_URL}{url}"
    return url


def _slug_of(url: str | None) -> str | None:
    if url is None:
        return None
    match = _PRODUCT_SLUG.search(url)
    return match.group(1) if match is not None else None


def _photo(url: object, *, kind: str | None = None) -> CarrefourPhoto | None:
    text = as_text(url)
    if text is None or not text.strip():
        return None
    return CarrefourPhoto(url=text.strip(), kind=kind)


def _catalog_value(value: object, catalog: str = "food") -> object:
    """read a field that is either flat or keyed by catalog."""

    if isinstance(value, dict):
        return value.get(catalog) or next(iter(value.values()), None)
    return value


def _iso(value: object) -> datetime | None:
    text = as_text(value)
    if text is None or text.startswith(_DATE_SENTINEL):
        return None
    return as_iso_datetime(text)


def _dmy(value: object) -> datetime | None:
    parsed = as_dmy_date(value)
    if parsed is None:
        return None
    return datetime.combine(parsed, datetime.min.time(), tzinfo=UTC)


def _measure(value: object) -> tuple[str | None, str | None]:
    """split ``"1.6 g"`` into its number and its unit."""

    text = as_text(value)
    if text is None or not text.strip():
        return None, None
    match = _MEASURE.fullmatch(text)
    if match is None:
        return text.strip(), None
    return match.group(1), match.group(2) or None


def _info_tags(value: object) -> tuple[InfoTag, ...]:
    tags: list[InfoTag] = []
    for item in as_items(value):
        tag = as_object(item)
        message = as_text(tag.get("message"))
        if message is None or not message.strip():
            continue
        tags.append(
            InfoTag(
                message=message.strip(),
                colour=as_text(tag.get("color_code")),
                icon=as_text(tag.get("icon")),
            )
        )
    return tuple(tags)


def _restrictions(value: object) -> tuple[Restriction, ...]:
    limits: list[Restriction] = []
    for item in as_items(value):
        limit = as_object(item)
        if not limit:
            continue
        limits.append(
            Restriction(
                id=as_text(limit.get("id")),
                name=as_text(limit.get("display_name")),
                quantity=as_integer(limit.get("quantity")),
                kind=as_text(limit.get("restriction_type")),
            )
        )
    return tuple(limits)


def _promotions(value: object) -> tuple[Promotion, ...]:
    badges = as_object(value)
    promotions: list[Promotion] = []
    for kind, entries in badges.items():
        for item in as_items(entries):
            badge = as_object(item)
            name = as_text(badge.get("name"))
            if name is None or not name.strip():
                continue
            promotions.append(
                Promotion(
                    id=as_text(badge.get("link")),
                    description=name.strip(),
                    kind=as_text(badge.get("badge_type")) or kind,
                    starts_at=_dmy(badge.get("start_date")),
                    ends_at=_dmy(badge.get("end_date")),
                )
            )
    return tuple(promotions)


def _availability(stock: int | None, available: bool | None) -> Availability:
    if stock is not None:
        return Availability(available=stock > 0, status="in_stock" if stock else "out")
    if available is None:
        return Availability()
    return Availability(available=available)


# --------------------------------------------------------------------- parsers


def parse_search_document(document: JsonObject) -> CarrefourProduct:
    """turn one document of the direct search index into a product.

    the index sends numeric prices, an iso validity range and the leaf
    category of the product, and sends neither stock counts nor promotions.

    it sends no unit price either, but it does send how much of
    ``measure_unit`` one selling unit holds as ``unit_conversion_factor``, so
    ``price.reference`` is ``active_price`` divided by it. a variable-weight
    document is left without one: its factor is a placeholder ``0.001`` that
    does not describe the pack.
    """

    identifier = as_identifier(document.get("product_id"), "product id")
    url = as_text(_catalog_value(document.get("urls")))
    image = as_text(_catalog_value(document.get("image_path")))
    amount = as_decimal(document.get("active_price"))
    previous = as_decimal(document.get("list_price"))
    discounted = previous is not None and amount is not None and previous > amount
    category = as_text(_catalog_value(document.get("parent_category")))
    measure_unit = as_text(document.get("measure_unit"))
    variable_weight = as_boolean(document.get("variable_weight")) or False
    return CarrefourProduct(
        id=identifier,
        name=required_text(document.get("display_name"), "product name"),
        brand=as_text(document.get("brand")),
        ean=as_text(document.get("ean13")),
        slug=_slug_of(url),
        url=_absolute(url),
        pack_size_text=as_text(document.get("recipient")),
        thumbnail=_photo(image, kind="thumbnail"),
        price=Price(
            amount=amount,
            previous=previous if discounted else None,
            unit_price_unit=measure_unit,
            reference=None
            if variable_weight
            else SHARED_UNITS.unit_price(
                amount,
                measure_unit,
                per=as_decimal(document.get("unit_conversion_factor")),
            ),
            is_discounted=discounted,
            valid_from=_iso(document.get("start_date_food")),
            valid_until=_iso(document.get("end_date_food")),
        ),
        availability=_availability(
            None, as_boolean(document.get("sale_point_available"))
        ),
        category_ids=(category,) if category else (),
        info_tags=_info_tags(document.get("info_tags")),
        sku_id=as_text(document.get("catalog_ref_id")),
        image_stem=as_text(document.get("sms")),
    )


def parse_search_result(
    data: JsonObject, *, query: str, offset: int, page_size: int
) -> CarrefourSearchResult:
    """turn one direct search response into a page of hits."""

    catalog = as_object(data.get("catalog"))
    if not catalog:
        raise InvalidResponseError("search response has no catalog envelope")
    products = tuple(
        parse_search_document(as_object(item))
        for item in as_items(catalog.get("content"))
        if as_object(item)
    )
    total = as_integer(catalog.get("numFound"))
    next_offset = offset + len(products)
    has_more = bool(products) and (total is None or next_offset < total)
    beyond_ceiling = next_offset > MAX_START
    return CarrefourSearchResult(
        query=query,
        products=products,
        page_size=page_size,
        total_hits=total,
        offset=offset,
        next_cursor=str(next_offset) if has_more and not beyond_ceiling else None,
        truncated=has_more and beyond_ceiling,
    )


def parse_listing_card(item: JsonObject) -> CarrefourProduct:
    """turn one rendered listing card into a product.

    a card prices everything as localised text, and is the only shape that
    carries the app price, the stock count, the promotion badges and the
    purchase limits.
    """

    identifier = as_identifier(item.get("product_id"), "product id")
    url = as_text(item.get("url"))
    images = as_object(item.get("images"))
    amount = as_euro_text(item.get("price"))
    previous = as_euro_text(item.get("strikethrough_price"))
    stock = as_integer(item.get("units_in_stock"))
    unit_price_text = as_text(item.get("price_per_unit"))
    unit_price = as_euro_text(unit_price_text)
    measure_unit = as_text(item.get("measure_unit"))
    return CarrefourProduct(
        id=identifier,
        name=required_text(item.get("name"), "product name"),
        brand=as_text(item.get("brand")),
        slug=_slug_of(url),
        url=_absolute(url),
        thumbnail=_photo(images.get("desktop") or images.get("mobile"), kind="desktop"),
        price=Price(
            amount=amount,
            previous=previous,
            unit_price=unit_price,
            unit_price_unit=measure_unit,
            unit_price_text=unit_price_text,
            reference=SHARED_UNITS.unit_price(unit_price, measure_unit),
            is_discounted=previous is not None,
        ),
        availability=_availability(stock, None),
        promotions=_promotions(item.get("badge_map")),
        is_sponsored=bool(as_text(item.get("citrus_ad_id"))),
        app_price=as_euro_text(item.get("app_price")),
        restrictions=_restrictions(item.get("restrictions")),
        stock=stock,
        sell_pack_unit=as_integer(item.get("sell_pack_unit")),
        sku_id=as_text(item.get("sku_id")),
    )


def _cards(items: object) -> tuple[CarrefourProduct, ...]:
    products: list[CarrefourProduct] = []
    for item in as_items(items):
        card = as_object(item)
        if not card:
            continue
        try:
            products.append(parse_listing_card(card))
        except InvalidResponseError:
            continue
    return tuple(products)


def parse_listing(state: JsonObject) -> CarrefourListing:
    """read the product grid out of a rendered category page.

    sponsored rows live in their own ``citrus_sponsored_products`` key and are
    never read; a card ad-injected into the grid itself keeps its
    ``citrus_ad_id`` and is flagged ``is_sponsored``. the page prints two
    disagreeing totals and this takes the inner one, which is the one paging
    actually follows.
    """

    results = as_object(as_object(state.get("productCardList")).get("results"))
    pagination = as_object(results.get("pagination"))
    options = tuple(
        value
        for option in as_items(results.get("sort_options"))
        if (value := as_text(as_object(option).get("value")))
    )
    return CarrefourListing(
        products=_cards(results.get("items")),
        total_results=as_integer(pagination.get("total_results")),
        offset=as_integer(pagination.get("offset")) or 0,
        page_size=as_integer(pagination.get("page_size")),
        sort_options=options,
    )


def _nutrition_row(value: object, fallback: str) -> NutritionValue | None:
    row = as_object(value)
    if not row:
        return None
    amount, unit = _measure(row.get("valor"))
    if amount is None:
        return None
    return NutritionValue(
        name=as_text(row.get("nombre")) or fallback, per_100=amount, unit=unit
    )


def _nutrition_rows(info: JsonObject) -> tuple[NutritionValue, ...]:
    rows: list[NutritionValue] = []
    energy = as_object(info.get("valorEnergetico"))
    for key, fallback in (("kilocalorias", "kcal"), ("kilojulios", "kJ")):
        row = _nutrition_row(energy.get(key), fallback)
        if row is not None:
            rows.append(row)
    for key, fallback in (
        ("grasas", "grasas"),
        ("hidratos", "hidratos"),
        ("proteinas", "proteinas"),
        ("sal", "sal"),
    ):
        block = as_object(info.get(key))
        row = _nutrition_row(block, fallback)
        if row is not None:
            rows.append(row)
        for nested in as_items(block.get("listaInfo")):
            child = _nutrition_row(nested, fallback)
            if child is not None:
                rows.append(child)
    return tuple(rows)


def _details(info: JsonObject) -> tuple[ProductDetail, ...]:
    rows: list[ProductDetail] = []

    def collect(entry: object) -> None:
        row = as_object(entry)
        name = as_text(row.get("nombre"))
        value = as_text(row.get("valor"))
        if name and value:
            rows.append(
                ProductDetail(
                    name=name.strip(), value=normalise_whitespace(strip_tags(value))
                )
            )

    for block in as_items(info.get("masInfo")):
        for entry in as_items(as_object(block).get("listaInfo")):
            collect(entry)
    for entry in as_items(info.get("masInfoInforme")):
        collect(entry)
    return tuple(rows)


def _nutrition(info: JsonObject, nutri_score: str | None) -> Nutrition | None:
    if not info:
        return None
    ingredients_html = as_text(info.get("ingredientes"))
    allergens = as_text(as_object(info.get("alergenos")).get("contiene"))
    rows = _nutrition_rows(info)
    ingredients = (
        normalise_whitespace(strip_tags(ingredients_html)) if ingredients_html else None
    )
    if not (ingredients or allergens or rows or nutri_score):
        return None
    return Nutrition(
        ingredients=ingredients or None,
        allergens=allergens,
        values=rows,
        per=as_text(info.get("valorMedioPor")),
        nutri_score=nutri_score,
        raw_html=ingredients_html,
    )


def _breadcrumb(state: JsonObject) -> tuple[CarrefourCategory, ...]:
    pdp = as_object(state.get("pdp"))
    items = as_object(pdp.get("breadcrumb")).get("items")
    if items is None:
        items = as_object(state.get("breadcrumb")).get("items")
    path: list[CarrefourCategory] = []
    for entry in as_items(items):
        crumb = as_object(entry)
        identifier = as_text(crumb.get("category_id"))
        name = as_text(crumb.get("text"))
        if identifier is None or name is None or not _CATEGORY_ID.fullmatch(identifier):
            continue
        path.append(
            _category_node(
                identifier,
                name,
                as_text(crumb.get("url")),
                parent_id=path[-1].id if path else None,
                level=len(path) + 1,
            )
        )
    return tuple(path)


def _images(value: object) -> tuple[CarrefourPhoto, ...]:
    photos: list[CarrefourPhoto] = []
    for entry in as_items(value):
        image = as_object(entry)
        for kind in ("large", "medium", "thumbnail"):
            photo = _photo(image.get(kind), kind=kind)
            if photo is not None:
                photos.append(photo)
    return tuple(photos)


def parse_product(state: JsonObject) -> CarrefourProduct:
    """turn a product page's rendered state into a complete product.

    nutrition, ingredients, allergens, the nutri-score and the review rating
    exist on this shape only. the labels inside the nutrition block are
    spanish free text rather than a stable vocabulary, so the four the core
    model names are matched by label and everything else is kept in
    ``details``.
    """

    product = as_object(as_object(state.get("pdp")).get("product"))
    if not product:
        raise InvalidResponseError("product page has no product block")
    identifier = as_identifier(product.get("product_id"), "product id")
    offer = as_object(product.get("offer"))
    info = as_object(product.get("nutrition_info"))
    nutri_score = as_text(as_object(product.get("nutri_score")).get("name"))
    rating = as_object(product.get("review_rating"))
    url = as_text(product.get("url"))
    stock = as_integer(offer.get("units_in_stock"))
    unit_price_text = as_text(offer.get("price_per_unit"))
    unit_price = as_euro_text(unit_price_text)
    measure_unit = as_text(product.get("measure_unit"))
    details = _details(info)
    labelled = {detail.name.lower(): detail.value for detail in details}
    path = _breadcrumb(state)
    photos = _images(product.get("images"))
    return CarrefourProduct(
        id=identifier,
        name=required_text(product.get("name"), "product name"),
        brand=as_text(as_object(product.get("brand")).get("description"))
        or as_text(product.get("brand")),
        ean=as_text(product.get("ean")),
        slug=_slug_of(url),
        url=_absolute(url),
        thumbnail=photos[-1] if photos else None,
        price=Price(
            amount=as_euro_text(offer.get("price")),
            unit_price=unit_price,
            unit_price_unit=measure_unit,
            unit_price_text=unit_price_text,
            reference=SHARED_UNITS.unit_price(unit_price, measure_unit),
        ),
        availability=_availability(stock, None),
        category_ids=tuple(node.id for node in path),
        is_variable_weight=bool(as_boolean(product.get("variable_weight"))),
        photos=photos,
        legal_name=labelled.get(_LEGAL_NAME_LABEL),
        origin=labelled.get(_ORIGIN_LABEL),
        storage=labelled.get(_STORAGE_LABEL),
        usage=labelled.get(_USAGE_LABEL),
        category_path=path,
        nutrition=_nutrition(info, nutri_score),
        app_price=as_euro_text(offer.get("app_price")),
        nutri_score=nutri_score,
        review_rating=ReviewRating(
            average=as_decimal(rating.get("average_rating")),
            count=as_integer(rating.get("approved_reviews")),
        )
        if rating
        else None,
        details=details,
        stock=stock,
        sell_pack_unit=as_integer(product.get("sell_pack_unit")),
        sku_id=as_text(product.get("sku_id")),
        image_stem=as_text(product.get("sms")),
    )


def parse_home(state: JsonObject) -> tuple[CarrefourHomeSection, ...]:
    """read the product carousels out of the rendered home page.

    the page is assembled by a cms. its carousels arrive already filled, in
    page order, under ``cms.featured_products``, and each one's title lives
    in the cms document it was built from, keyed by the document id with its
    dashes removed. a carousel that holds no readable card is skipped rather
    than reported as an empty section.
    """

    cms = as_object(state.get("cms"))
    documents = as_object(as_object(cms.get("pageModel")).get("content"))
    sections: list[CarrefourHomeSection] = []
    for item in as_items(cms.get("featured_products")):
        block = as_object(item)
        products = _cards(block.get("products"))
        if not products:
            continue
        identifier = as_text(block.get("cms_content_id"))
        document = (
            as_object(documents.get(f"u{identifier.replace('-', '')}"))
            if identifier
            else {}
        )
        sections.append(
            CarrefourHomeSection(
                layout=as_text(document.get("documentType")) or "featuredProducts",
                title=_stripped(document.get("title")),
                id=identifier,
                name=_stripped(document.get("name")),
                description=_stripped(document.get("description")),
                category_ids=tuple(
                    text
                    for value in as_items(document.get("categoryId"))
                    if (text := as_text(value))
                ),
                products=products,
            )
        )
    return tuple(sections)


def _stripped(value: object) -> str | None:
    text = as_text(value)
    if text is None or not text.strip():
        return None
    return text.strip()


def _category_node(
    identifier: str,
    name: str,
    url: str | None,
    *,
    parent_id: str | None,
    level: int,
    image: str | None = None,
) -> CarrefourCategory:
    """build one category node, reading its slug path out of its url."""

    match = _CATEGORY_URL.search(url or "")
    slugs = tuple(part for part in match.group(1).split("/") if part) if match else ()
    return CarrefourCategory(
        id=identifier,
        name=name.strip(),
        parent_id=parent_id,
        level=level,
        slug=slugs[-1] if slugs else None,
        slug_path=slugs,
        url=_absolute(url),
        image_url=image,
    )


def _menu_node(
    value: object, *, parent_id: str | None, level: int
) -> CarrefourCategory | None:
    entry = as_object(value)
    identifier = as_text(entry.get("id"))
    name = as_text(entry.get("name"))
    if not identifier or not name or not name.strip():
        return None
    node = _category_node(
        identifier,
        name,
        as_text(entry.get("url_rel")) or as_text(entry.get("url")),
        parent_id=parent_id,
        level=level,
        image=as_text(entry.get("icon_path")),
    )
    children = tuple(
        child
        for item in as_items(entry.get("childs"))
        if (child := _menu_node(item, parent_id=identifier, level=level + 1))
    )
    return replace(node, children=children)


def _menu_find(nodes: object, wanted: str) -> JsonObject | None:
    for item in as_items(nodes):
        entry = as_object(item)
        if as_text(entry.get("id")) == wanted:
            return entry
        found = _menu_find(entry.get("childs"), wanted)
        if found is not None:
            return found
    return None


def parse_menu(
    data: JsonObject, current: str, *, level: int = 1
) -> tuple[CarrefourCategory, ...]:
    """return the children of ``current`` out of one navigation menu response.

    the menu nests the requested node inside its ancestors, and how many of
    them it repeats depends on the language, so the node is searched for by
    id. the food root is not a category of its own: its children, the
    departments, have no parent. an id the menu does not know, which
    includes every leaf, answers with an empty menu and so with no children.
    """

    entry = _menu_find(data.get("menu"), current)
    if entry is None:
        return ()
    parent_id = None if current == FOOD_ROOT else current
    return tuple(
        child
        for item in as_items(entry.get("childs"))
        if (child := _menu_node(item, parent_id=parent_id, level=level))
    )


def parse_category_page(state: JsonObject, category_id: str) -> CarrefourCategory:
    """read one category and its children out of its rendered listing page.

    the breadcrumb ends at the category itself and supplies its ancestors.
    the second row of the page's navigation strip lists the category's
    children — except on a leaf, where it lists the leaf's siblings, the
    leaf among them, which is how the two are told apart.
    """

    path = _breadcrumb(state)
    if path and path[-1].id == category_id:
        node = path[-1]
    else:
        name = as_text(as_object(state.get("category")).get("display_name"))
        if name is None or not name.strip():
            raise InvalidResponseError(f"category page {category_id} names no node")
        node = CarrefourCategory(
            id=category_id,
            name=name.strip(),
            parent_id=path[-1].id if path else None,
            level=len(path) + 1,
        )
    strip = as_object(
        as_object(state.get("horizontalNavigation")).get("secondLevelCategories")
    )
    children: list[CarrefourCategory] = []
    for item in as_items(strip.get("items")):
        entry = as_object(item)
        identifier = as_text(entry.get("id"))
        name = as_text(entry.get("display_name"))
        if not identifier or not name or not name.strip():
            continue
        if identifier == node.id:
            return node
        children.append(
            _category_node(
                identifier,
                name,
                as_text(entry.get("url")),
                parent_id=node.id,
                level=(node.level or 0) + 1,
                image=as_text(entry.get("image_url")),
            )
        )
    return replace(node, children=tuple(children))


def _coordinates(value: object) -> tuple[float | None, float | None]:
    text = as_text(value)
    if text is None:
        return None, None
    parts = text.split(",")
    if len(parts) != 2:
        return None, None
    try:
        return float(parts[0].strip()), float(parts[1].strip())
    except ValueError:
        return None, None


def _float(value: object) -> float | None:
    number = as_decimal(value)
    return None if number is None else float(number)


def parse_drives(data: JsonObject) -> tuple[CarrefourStore, ...]:
    """return every sale point of the drive directory, grouped order preserved.

    these ids are the ones a client binds to; a physical shop id from a
    postcode lookup is a different namespace and is not accepted.
    """

    stores: list[CarrefourStore] = []
    for group in as_items(data.get("groups")):
        block = as_object(group)
        for item in as_items(block.get("sale_points")):
            point = as_object(item)
            identifier = as_text(point.get("sale_point_id"))
            name = as_text(point.get("name"))
            if not identifier or not name:
                continue
            street = " ".join(
                part.strip()
                for key in ("street_type", "street_name", "street_number")
                if (part := as_text(point.get(key)))
            ).strip()
            stores.append(
                CarrefourStore(
                    id=identifier,
                    name=name.strip(),
                    kind="drive",
                    address=street or None,
                    postal_code=as_text(point.get("postal_code")),
                    city=as_text(point.get("city")),
                    province=as_text(point.get("province")),
                    latitude=_float(point.get("latitude")),
                    longitude=_float(point.get("longitude")),
                    store_code=as_text(point.get("store_id")),
                    group=as_text(point.get("group")) or as_text(block.get("name")),
                )
            )
    return tuple(stores)


def parse_stores_location(data: JsonObject) -> tuple[CarrefourStore, ...]:
    """return the physical shops near a postcode, nearest first."""

    stores: list[CarrefourStore] = []
    for item in as_items(data.get("stores")):
        shop = as_object(item)
        identifier = as_text(shop.get("id"))
        name = as_text(shop.get("name"))
        if not identifier or not name:
            continue
        latitude, longitude = _coordinates(shop.get("position"))
        stores.append(
            CarrefourStore(
                id=identifier,
                name=name.strip(),
                kind="store",
                address=as_text(shop.get("address")),
                postal_code=as_text(shop.get("postal_code")),
                latitude=latitude,
                longitude=longitude,
                sap_code=as_text(shop.get("sap_code")),
                distance_km=_float(shop.get("distance")),
                stock_state=as_text(shop.get("stock_state")),
                click_and_collect=as_boolean(shop.get("click_and_collect")),
                hours=as_text(shop.get("hours")),
                phone=as_text(shop.get("phone_number")),
            )
        )
    return tuple(stores)


def parse_suggestions(data: JsonObject) -> tuple[str, ...]:
    """return the query suggestions of an autocomplete response."""

    content = as_object(data.get("topTrends")).get("content")
    suggestions: list[str] = []
    for item in as_items(content):
        title = as_text(as_object(item).get("title_raw")) or as_text(
            as_object(item).get("title")
        )
        if title and title.strip() and title.strip() not in suggestions:
            suggestions.append(title.strip())
    return tuple(suggestions)


def image_url(stem: str, *, width: int, variant: str = "00", index: int = 1) -> str:
    """build an image url from a product's image stem.

    the stem is the ``sms`` field, which is the ``sku_id`` without its four
    trailing digits; reading it is more reliable than truncating the id.
    """

    return f"{STATIC_URL}/hd_{width}x_/img_pim_food/{stem}_{variant}_{index}.jpg"
