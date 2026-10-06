"""mercadona's models and parsers, built on the shared core models."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TypeAlias
from urllib.parse import urlencode, urlparse

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
from .._core.models import (
    Availability,
    Category,
    HomeSection,
    Nutrition,
    Photo,
    Price,
    Product,
    ProductSummary,
    SearchResult,
)
from .._core.units import UnitReader
from ._constants import IMAGE_URL

# reference_format codes beyond the shared vocabulary: two spellings of a
# dozen eggs and the per-wash price of laundry detergent
_UNITS = UnitReader({"dc": "docena", "dz": "docena", "lv": "lavado"})


class PhotoFit(StrEnum):
    """imgix resize modes supported by :meth:`MercadonaPhoto.sized`."""

    CROP = "crop"
    FIT = "fit"


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaPrice(Price):
    """mercadona's price block, including bulk and pack sizing."""

    bulk: Decimal | None = None
    unit_size: Decimal | None = None
    pack_size: Decimal | None = None
    total_units: Decimal | None = None
    drained_weight: Decimal | None = None
    minimum_amount: Decimal | None = None
    increment_amount: Decimal | None = None
    unit_name: str | None = None
    size_format: str | None = None
    is_new: bool = False
    is_pack: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaAvailability(Availability):
    """publication status and purchase limits for a product."""

    unavailable_from: str | None = None
    unavailable_weekdays: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaPhoto(Photo):
    """a product image identified independently of its requested size."""

    url: str = ""
    file_name: str
    perspective: int | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.file_name)
        file_name = PurePosixPath(parsed.path).name if parsed.scheme else self.file_name
        if not file_name or "/" in file_name or "\\" in file_name:
            raise ValueError("photo file_name must identify one image file")
        object.__setattr__(self, "file_name", file_name)
        object.__setattr__(self, "url", f"{IMAGE_URL}/{file_name}")

    def sized(
        self,
        *,
        width: int | None = None,
        height: int | None = None,
        fit: PhotoFit | str = PhotoFit.CROP,
    ) -> str:
        """build a resized image url without performing i/o."""

        try:
            fit_value = PhotoFit(fit).value
        except ValueError as error:
            raise ValueError("fit must be 'crop' or 'fit'") from error
        if width is not None and width <= 0:
            raise ValueError("width must be greater than zero")
        if height is not None and height <= 0:
            raise ValueError("height must be greater than zero")
        if width is None and height is None:
            return self.url
        parameters: list[tuple[str, str | int]] = [("fit", fit_value)]
        if height is not None:
            parameters.append(("h", height))
        if width is not None:
            parameters.append(("w", width))
        return f"{self.url}?{urlencode(parameters)}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductDetails:
    """descriptive, legal, origin, supplier, and handling details."""

    legal_name: str | None = None
    description: str | None = None
    origin: str | None = None
    suppliers: tuple[str, ...] = ()
    counter_info: str | None = None
    danger_mentions: str | None = None
    mandatory_mentions: str | None = None
    production_variant: str | None = None
    usage_instructions: str | None = None
    storage_instructions: str | None = None
    alcohol_by_volume: Decimal | None = None
    prepared_by_mercadona: bool | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaCategory(Category):
    """one category node with mercadona's layout metadata."""

    order: int | None = None
    layout: int | None = None
    published: bool | None = None
    is_extended: bool | None = None
    subtitle: str | None = None
    children: tuple[MercadonaCategory, ...] = ()
    products: tuple[MercadonaProductSummary, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaProductSummary(ProductSummary):
    """partial product data returned by mercadona's listing operations."""

    packaging: str | None = None
    main_feature: str | None = None
    thumbnail: MercadonaPhoto | None = None
    price: MercadonaPrice = MercadonaPrice()
    availability: MercadonaAvailability = MercadonaAvailability()
    categories: tuple[MercadonaCategory, ...] = ()
    requires_age_check: bool = False
    is_water: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaProduct(Product):
    """complete product data returned by mercadona's product endpoint."""

    packaging: str | None = None
    main_feature: str | None = None
    thumbnail: MercadonaPhoto | None = None
    photos: tuple[MercadonaPhoto, ...] = ()
    price: MercadonaPrice = MercadonaPrice()
    availability: MercadonaAvailability = MercadonaAvailability()
    categories: tuple[MercadonaCategory, ...] = ()
    details: ProductDetails = ProductDetails()
    nutrition: Nutrition = field(default_factory=Nutrition)
    is_water: bool = False
    is_bulk: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class SeasonSummary:
    """a seasonal collection advertised by a home-page banner."""

    id: str
    title: str
    banner_id: str | None = None
    campaign_id: str | None = None
    image_url: str | None = None
    text_color: str | None = None
    background_colors: tuple[str, ...] = ()
    button_color: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class HomeNotification:
    """one notification displayed in a home section."""

    title: str
    kind: str | None = None
    action: str | None = None
    action_title: str | None = None
    event_key: str | None = None


HomeItem: TypeAlias = MercadonaProductSummary | SeasonSummary | HomeNotification


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaHomeSection(HomeSection):
    """one ordered section from the storefront home response.

    ``items`` keeps every item in upstream order; ``products`` holds only the
    product summaries among them.
    """

    subtitle: str | None = None
    id: str | None = None
    source: str | None = None
    source_code: str | None = None
    show_more: bool | None = None
    items: tuple[HomeItem, ...] = ()
    products: tuple[MercadonaProductSummary, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Season:
    """one seasonal collection and its product summaries."""

    id: str
    title: str
    layout: str | None = None
    source: str | None = None
    source_code: str | None = None
    products: tuple[MercadonaProductSummary, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class MercadonaSearchResult(SearchResult):
    """one page of search results with algolia's pagination metadata.

    the index serves at most a thousand hits per query, so ``truncated`` is
    set on the last page it serves when ``total_hits`` says more matched;
    :meth:`~supermercapy.mercadona.Mercadona.get_indexed_catalog` reaches the rest.
    """

    page: int = 0
    total_pages: int = 0
    processing_time_ms: int | None = None
    products: tuple[MercadonaProductSummary, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class CatalogResult:
    """a warehouse's product index collected through partitions.

    an index with category facets is partitioned by top-level category; one
    without them is partitioned by half-open score ranges instead.
    """

    products: tuple[MercadonaProductSummary, ...]
    reported_total_hits: int
    queried_category_ids: tuple[str, ...]
    reconciled: bool
    queried_score_ranges: tuple[tuple[float, float], ...] = ()

    @property
    def partition_count(self) -> int:
        """how many partitions were queried, whichever kind they were."""

        return len(self.queried_category_ids) + len(self.queried_score_ranges)


def _price(data: object) -> MercadonaPrice:
    value = as_object(data)
    unit_price = as_decimal(value.get("reference_price"))
    unit_price_unit = as_text(value.get("reference_format"))
    return MercadonaPrice(
        amount=as_decimal(value.get("unit_price")),
        previous=as_decimal(value.get("previous_unit_price")),
        unit_price=unit_price,
        unit_price_unit=unit_price_unit,
        reference=_UNITS.unit_price(unit_price, unit_price_unit),
        tax_percentage=as_decimal(value.get("tax_percentage")),
        is_discounted=as_boolean(value.get("price_decreased")) or False,
        is_approximate=as_boolean(value.get("approx_size")) or False,
        bulk=as_decimal(value.get("bulk_price")),
        unit_size=as_decimal(value.get("unit_size")),
        pack_size=as_decimal(value.get("pack_size")),
        total_units=as_decimal(value.get("total_units")),
        drained_weight=as_decimal(value.get("drained_weight")),
        minimum_amount=as_decimal(value.get("min_bunch_amount")),
        increment_amount=as_decimal(value.get("increment_bunch_amount")),
        unit_name=as_text(value.get("unit_name")),
        size_format=as_text(value.get("size_format")),
        is_new=as_boolean(value.get("is_new")) or False,
        is_pack=as_boolean(value.get("is_pack")) or False,
    )


def _availability(data: JsonObject) -> MercadonaAvailability:
    weekdays = tuple(
        item
        for item in as_items(data.get("unavailable_weekdays"))
        if isinstance(item, int) and not isinstance(item, bool)
    )
    return MercadonaAvailability(
        available=as_boolean(data.get("published")),
        status=as_text(data.get("status")),
        max_quantity=as_decimal(data.get("limit")),
        unavailable_from=as_text(data.get("unavailable_from")),
        unavailable_weekdays=weekdays,
    )


def _photo(data: object) -> MercadonaPhoto | None:
    source: str | None
    if isinstance(data, str):
        source = data
        perspective = None
    else:
        value = as_object(data)
        source = next(
            (
                url
                for key in ("regular", "zoom", "thumbnail")
                if (url := as_text(value.get(key)))
            ),
            None,
        )
        perspective = as_integer(value.get("perspective"))
    if source is None:
        return None
    try:
        return MercadonaPhoto(file_name=source, perspective=perspective)
    except ValueError:
        return None


def _badges(data: JsonObject) -> tuple[bool, bool]:
    badges = as_object(data.get("badges"))
    return (
        as_boolean(badges.get("requires_age_check")) or False,
        as_boolean(badges.get("is_water")) or False,
    )


def _category_ids(categories: tuple[MercadonaCategory, ...]) -> tuple[str, ...]:
    ids: list[str] = []

    def walk(items: tuple[MercadonaCategory, ...]) -> None:
        for category in items:
            ids.append(category.id)
            walk(category.children)

    walk(categories)
    return tuple(dict.fromkeys(ids))


def parse_category(data: object, *, level: int | None = None) -> MercadonaCategory:
    """parse one category node and its children.

    product and search responses spell each node's ``level``; the category
    endpoints stopped doing so in october 2026, so a caller that knows where
    the node sits passes ``level`` and the children count down from it.
    """

    value = as_object(data)
    category_id = as_identifier(value.get("id"), "category id")
    name = required_text(value.get("name"), "category name")
    depth = as_integer(value.get("level"))
    if depth is None:
        depth = level
    child_level = None if depth is None else depth + 1
    children = tuple(
        parse_category(item, level=child_level)
        for item in as_items(value.get("categories"))
    )
    products = tuple(
        parse_product_summary(item) for item in as_items(value.get("products"))
    )
    return MercadonaCategory(
        id=category_id,
        name=name,
        level=depth,
        image_url=as_text(value.get("image")) or as_text(value.get("icon_url")),
        product_count=len(products) or None,
        children=children,
        products=products,
        order=as_integer(value.get("order")),
        layout=as_integer(value.get("layout")),
        published=as_boolean(value.get("published")),
        is_extended=as_boolean(value.get("is_extended")),
        subtitle=as_text(value.get("subtitle")),
    )


def parse_product_summary(data: object) -> MercadonaProductSummary:
    value = as_object(data)
    product_id = as_identifier(value.get("id"), "product id")
    name = required_text(value.get("display_name"), "product name")
    categories = tuple(
        parse_category(item) for item in as_items(value.get("categories"))
    )
    requires_age_check, is_water = _badges(value)
    price = _price(value.get("price_instructions"))
    return MercadonaProductSummary(
        id=product_id,
        name=name,
        slug=as_text(value.get("slug")),
        brand=as_text(value.get("brand")),
        url=as_text(value.get("share_url")),
        pack_size_text=as_text(value.get("packaging")),
        packaging=as_text(value.get("packaging")),
        main_feature=as_text(value.get("main_feature")),
        thumbnail=_photo(value.get("thumbnail")),
        price=price,
        availability=_availability(value),
        category_ids=_category_ids(categories),
        categories=categories,
        is_new=as_boolean(value.get("is_new_arrival")) or False,
        requires_age_check=requires_age_check,
        is_water=is_water,
    )


def _details(data: object, product: JsonObject) -> ProductDetails:
    value = as_object(data)
    suppliers = tuple(
        name
        for item in as_items(value.get("suppliers"))
        if (name := as_text(as_object(item).get("name"))) is not None
    )
    alcohol = as_text(value.get("alcohol_by_volume"))
    if alcohol is not None:
        alcohol = alcohol.strip().removesuffix("º").removesuffix("%")
        alcohol = alcohol.replace(",", ".")
    return ProductDetails(
        legal_name=as_text(value.get("legal_name")),
        description=as_text(value.get("description")),
        origin=as_text(value.get("origin")) or as_text(product.get("origin")),
        suppliers=suppliers,
        counter_info=as_text(value.get("counter_info")),
        danger_mentions=as_text(value.get("danger_mentions")),
        mandatory_mentions=as_text(value.get("mandatory_mentions")),
        production_variant=as_text(value.get("production_variant")),
        usage_instructions=as_text(value.get("usage_instructions")),
        storage_instructions=as_text(value.get("storage_instructions")),
        alcohol_by_volume=as_decimal(alcohol),
        prepared_by_mercadona=as_boolean(value.get("is_prepared_by_mercadona")),
    )


def parse_product(data: object) -> MercadonaProduct:
    value = as_object(data)
    product_id = as_identifier(value.get("id"), "product id")
    name = required_text(value.get("display_name"), "product name")
    photos = tuple(
        photo
        for item in as_items(value.get("photos"))
        if (photo := _photo(item)) is not None
    )
    categories = tuple(
        parse_category(item) for item in as_items(value.get("categories"))
    )
    nutrition_data = as_object(value.get("nutrition_information"))
    requires_age_check, is_water = _badges(value)
    details = _details(value.get("details"), value)
    return MercadonaProduct(
        id=product_id,
        name=name,
        ean=as_text(value.get("ean")),
        slug=as_text(value.get("slug")),
        brand=as_text(value.get("brand")),
        url=as_text(value.get("share_url")),
        pack_size_text=as_text(value.get("packaging")),
        packaging=as_text(value.get("packaging")),
        main_feature=as_text(value.get("main_feature")),
        thumbnail=_photo(value.get("thumbnail")) or (photos[0] if photos else None),
        photos=photos,
        price=_price(value.get("price_instructions")),
        availability=_availability(value),
        category_ids=_category_ids(categories),
        categories=categories,
        category_path=categories,
        description=details.description,
        legal_name=details.legal_name,
        origin=details.origin,
        storage=details.storage_instructions,
        usage=details.usage_instructions,
        details=details,
        nutrition=Nutrition(
            allergens=as_text(nutrition_data.get("allergens")),
            ingredients=as_text(nutrition_data.get("ingredients")),
        ),
        requires_age_check=requires_age_check,
        is_water=is_water,
        is_bulk=as_boolean(value.get("is_bulk")) or False,
        is_variable_weight=as_boolean(value.get("is_variable_weight")) or False,
        is_new=as_boolean(value.get("is_new_arrival")) or False,
    )


def _season_summary(data: object) -> SeasonSummary:
    value = as_object(data)
    api_path = required_text(value.get("api_path"), "season API path")
    path_parts = PurePosixPath(urlparse(api_path).path).parts
    if not path_parts:
        raise InvalidResponseError("response has no usable season id")
    season_id = path_parts[-1]
    return SeasonSummary(
        id=as_identifier(season_id, "season id"),
        title=required_text(value.get("title"), "season title"),
        banner_id=(
            as_identifier(value.get("id"), "season banner id")
            if value.get("id") is not None
            else None
        ),
        campaign_id=as_text(value.get("campaign_id")),
        image_url=as_text(value.get("image_url")),
        text_color=as_text(value.get("text_color")),
        background_colors=tuple(
            color
            for item in as_items(value.get("bg_colors"))
            if (color := as_text(item)) is not None
        ),
        button_color=as_text(value.get("button_color")),
    )


def parse_home_section(data: object) -> MercadonaHomeSection:
    value = as_object(data)
    layout = required_text(value.get("layout"), "home section layout")
    content = as_object(value.get("content"))
    parsed_items: list[HomeItem] = []
    if layout == "notification" and content:
        # the action was a bare path, and is now an object with a button title
        # and the url it leads to
        action = content.get("action")
        action_object = as_object(action)
        parsed_items.append(
            HomeNotification(
                title=required_text(content.get("title"), "notification title"),
                kind=as_text(content.get("type")),
                action=as_text(action) or as_text(action_object.get("redirect_url")),
                action_title=as_text(action_object.get("title")),
                event_key=as_text(content.get("event_key")),
            )
        )
    else:
        for item in as_items(content.get("items")):
            item_value = as_object(item)
            if item_value.get("api_path") is not None:
                parsed_items.append(_season_summary(item_value))
            elif item_value.get("id") is not None:
                parsed_items.append(parse_product_summary(item_value))
    return MercadonaHomeSection(
        layout=layout,
        title=as_text(content.get("title")),
        subtitle=as_text(content.get("subtitle")),
        id=as_text(content.get("uuid")),
        source=as_text(content.get("source")),
        source_code=as_text(content.get("source_code")),
        show_more=as_boolean(content.get("show_more")),
        items=tuple(parsed_items),
        products=tuple(
            item for item in parsed_items if isinstance(item, MercadonaProductSummary)
        ),
    )


def parse_season(data: object, requested_id: str) -> Season:
    value = as_object(data)
    title = required_text(value.get("title"), "season title")
    products = tuple(
        parse_product_summary(item) for item in as_items(value.get("items"))
    )
    return Season(
        id=as_text(value.get("uuid")) or requested_id,
        title=title,
        layout=as_text(value.get("layout")),
        source=as_text(value.get("source")),
        source_code=as_text(value.get("source_code")),
        products=products,
    )


def parse_search_result(
    data: object, *, query: str, requested_page: int, requested_page_size: int
) -> MercadonaSearchResult:
    value = as_object(data)
    hits = value.get("hits")
    if not isinstance(hits, list):
        raise InvalidResponseError("search response has no hits array")
    page_value = as_integer(value.get("page"))
    page = requested_page if page_value is None else page_value
    page_size_value = as_integer(value.get("hitsPerPage"))
    total_hits = as_integer(value.get("nbHits"))
    total_pages_value = as_integer(value.get("nbPages"))
    total_pages = 0 if total_pages_value is None else total_pages_value
    page_size = requested_page_size if page_size_value is None else page_size_value
    total = len(hits) if total_hits is None else total_hits
    has_next = page + 1 < total_pages
    # the index stops paging after a thousand hits however many match, so the
    # last page it serves can leave matches unreached
    reached = page * page_size + len(hits)
    return MercadonaSearchResult(
        query=query,
        page=page,
        page_size=page_size,
        total_hits=total,
        total_pages=total_pages,
        next_cursor=str(page + 1) if has_next else None,
        truncated=not has_next and reached < total,
        processing_time_ms=as_integer(value.get("processingTimeMS")),
        products=tuple(parse_product_summary(item) for item in hits),
    )
