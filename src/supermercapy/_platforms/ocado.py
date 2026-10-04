"""ocado smart platform: the parsers and request flow its storefronts share.

bonpreu and alcampo both run ocado smart platform behind an aws waf. they
serve the same anonymous json under the same ``/api/`` paths: a category tree,
product pages paged by a session-scoped token, a product sheet, a batch
decorate call that needs a csrf token from the home page, and a promotions
listing keyed on a region. this module holds everything the two have in
common, so each store keeps only its own client class, its own public models,
its constants and what is genuinely its own.

nothing here is public. a store wires its own model classes into
:class:`Models`, builds one :class:`Parser` from them, and drives the shared
request flow through one :class:`Storefront` held by its client.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Generic, Protocol, TypeVar
from urllib.parse import quote

import httpx

from .._core.client import BaseClient, ResponseVerdict, validate_identifier
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
from .._core.exceptions import (
    AuthenticationError,
    BlockedError,
    ChallengedError,
    ConfigurationError,
    InvalidResponseError,
    NotFoundError,
    TransportError,
)
from .._core.html import (
    extract_json_assignment,
    normalise_whitespace,
    strip_tags,
    table_rows,
)
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
from .._core.units import UnitPrice, UnitReader

# ocado services, reached through each storefront's own /api prefix rather
# than their internal names
PRODUCT_PAGES_WS = "/api/webproductpagews"
SEARCH_WS = "/api/search"
LISTING_WS = "/api/product-listing-pages"

CSRF_HEADER = "X-CSRF-TOKEN"
STATE_MARKER = "window.__INITIAL_STATE__"
HTML_ACCEPT = "text/html,application/xhtml+xml"

# an aws waf challenge: http 202, an empty body, and no `requestid` header,
# which every genuine response carries. the budget is per ip, refills over tens
# of minutes, and every challenged request seems to spend from it again, so a
# client raises rather than retries.
CHALLENGE_STATUS = 202
WAF_ACTION_HEADER = "x-amzn-waf-action"
REQUEST_ID_HEADER = "requestid"

# http 403 means two different things. the origin answers a non-get without a
# csrf token with an empty 403 that carries `requestid` and
# `ecom-csrf-failure: true`. the waf answers a caller it refuses outright with
# cloudfront's own "request blocked" page and no `requestid`.
REFUSED_STATUS = 403

# a browser user agent, for a waf that refuses anything else on `/api/`; the
# client does not imitate a browser in any other way
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MAX_QUERY_LENGTH = 50
LISTING_TAG = "web"

IMAGE_SIZES = (
    "100x100",
    "150x150",
    "200x200",
    "300x300",
    "350x350",
    "410x410",
    "500x500",
    "640x640",
    "800x800",
    "960x960",
    "1120x1120",
    "1280x1280",
)
"""the square renditions the image host serves; nothing else resolves."""

IMAGE_FORMATS = ("jpg", "webp")

INGREDIENTS_FIELD = "ingredients"
ALLERGENS_FIELD = "allergens"
NUTRITION_FIELD = "nutritionalData"
ORIGIN_FIELDS = ("countryOfOrigin", "fishingArea")
STORAGE_FIELDS = ("storage", "cookingGuidelines", "storageAndUsage")
USAGE_FIELDS = ("preparationAndUsage", "servingSuggestions")
LEGAL_NAME_FIELDS = ("unitType", "specification")

CATCHWEIGHT = "CATCHWEIGHT"
UNKNOWN = "UNKNOWN"
LOYALTY = "LOYALTY"

# allergens are bolded inside the ingredients html rather than listed apart
_BOLD = re.compile(r"<b\b[^>]*>(.*?)</b>", re.IGNORECASE | re.DOTALL)
# an image url ends in the rendition it was asked for
_RENDITION = re.compile(r"/\d+x\d+\.[a-z]+$", re.IGNORECASE)
_SPONSORED_GROUP = "featured"

ProductT = TypeVar("ProductT", bound=Product)
CategoryT = TypeVar("CategoryT", bound=Category)
ResultT = TypeVar("ResultT", bound=SearchResult)


class FieldLike(Protocol):
    """one product sheet entry, raw and stripped."""

    @property
    def title(self) -> str: ...

    @property
    def content(self) -> str: ...

    @property
    def text(self) -> str: ...


# ---------------------------------------------------------------- validation


def validate_query(query: str) -> str:
    """return a query truncated the way the storefront truncates its own."""

    if not isinstance(query, str) or not query.strip():
        raise ConfigurationError("query must be a non-empty string")
    return query[:MAX_QUERY_LENGTH]


def validate_cursor(cursor: str | None) -> str | None:
    """return a page token, or ``None`` for the first page."""

    if cursor is None:
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise ConfigurationError("cursor must be a page token returned by a listing")
    return cursor


def validate_limit(limit: int, label: str) -> int:
    """return a positive integer limit."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ConfigurationError(f"{label} must be a positive integer")
    return limit


def encode_filters(filters: object) -> str:
    """serialise a filter selection the way the storefront's own helper does.

    the mapping is flattened to ``group=value,value&group=value`` with every
    key and value percent-encoded, and that whole string then travels as the
    value of one ``filters`` parameter, so it is encoded a second time on the
    wire.
    """

    if not isinstance(filters, dict):
        raise TypeError("filters must be a mapping of group id to values")
    parts: list[str] = []
    for group, value in filters.items():
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, (list, tuple)):
            values = [str(item) for item in value]
        else:
            values = [str(value)]
        joined = ",".join(quote(item, safe="") for item in values)
        parts.append(f"{quote(str(group), safe='')}={joined}")
    return "&".join(parts)


def sized_url(
    url: str, base_url: str | None, size: int | str, image_format: str
) -> str:
    """return an image url at one of the square renditions."""

    wanted = f"{size}x{size}" if isinstance(size, int) else str(size)
    if wanted not in IMAGE_SIZES:
        supported = ", ".join(IMAGE_SIZES)
        raise ValueError(f"size must be one of {supported}")
    if image_format not in IMAGE_FORMATS:
        supported = ", ".join(IMAGE_FORMATS)
        raise ValueError(f"image_format must be one of {supported}")
    if base_url is None:
        return url
    return f"{base_url}/{wanted}.{image_format}"


def find_field(fields: tuple[FieldLike, ...], title: str, *, raw: bool) -> str | None:
    """return one sheet field's text, or its html when ``raw`` is set."""

    for entry in fields:
        if entry.title == title:
            return entry.content if raw else entry.text
    return None


# ------------------------------------------------------------------- parsers


@dataclass(frozen=True, slots=True, kw_only=True)
class Models(Generic[ProductT, CategoryT, ResultT]):
    """the classes one storefront's responses parse into.

    every entry is the store's own public dataclass, called with keyword
    arguments only; the field names are the same across ocado stores.
    """

    photo: Callable[..., Photo]
    price: Callable[..., Price]
    quantity: Callable[..., object]
    catchweight: Callable[..., object]
    field: Callable[..., FieldLike]
    promotion: Callable[..., Promotion]
    category: Callable[..., CategoryT]
    filter_attribute: Callable[..., object]
    filter_group: Callable[..., object]
    product: Callable[..., ProductT]
    search_result: Callable[..., ResultT]
    product_type: type[StrEnum]


class Parser(Generic[ProductT, CategoryT, ResultT]):
    """parse one ocado storefront's json into that store's models.

    ``units`` reads the unit-price message keys and unit names the store
    sends. ``unit_names`` maps a message key to its unit name for a listing
    that sends the key alone. ``previous_price`` matches a promotion
    description that states the "before" price in prose, or is ``None`` where
    the store never does. ``extras`` returns the keyword arguments of any
    product field the store's model adds, read from one raw row, and
    ``sheet`` returns the product fields a store reads out of its own sheet
    entries; a ``None`` it returns leaves the shared reading in place.
    """

    def __init__(
        self,
        models: Models[ProductT, CategoryT, ResultT],
        *,
        units: UnitReader,
        unit_names: Mapping[str, str],
        previous_price: re.Pattern[str] | None = None,
        extras: Callable[[JsonObject], dict[str, Any]] | None = None,
        sheet: Callable[[tuple[FieldLike, ...]], dict[str, Any]] | None = None,
    ) -> None:
        self._models = models
        self._units = units
        self._unit_names = unit_names
        self._previous_price_pattern = previous_price
        self._extras = extras
        self._sheet = sheet

    # ---------------------------------------------------------------- photos

    def _photo(self, data: JsonObject, *, alt: str | None) -> Photo | None:
        url = as_text(data.get("src"))
        if url is None:
            return None
        return self._models.photo(
            url=url,
            alt=as_text(data.get("description")) or alt,
            image_id=as_text(data.get("imageId")),
            base_url=_RENDITION.sub("", url) if _RENDITION.search(url) else None,
        )

    def _photos(self, data: JsonObject, *, alt: str | None) -> tuple[Photo, ...]:
        photos: list[Photo] = []
        seen: set[str] = set()
        entries = as_items(data.get("images")) or [data.get("image")]
        for item in entries:
            photo = self._photo(as_object(item), alt=alt)
            if photo is not None and photo.url not in seen:
                seen.add(photo.url)
                photos.append(photo)
        if photos:
            return tuple(photos)
        identifiers = [as_text(item) for item in as_items(data.get("imageIds"))]
        for index, item in enumerate(as_items(data.get("imagePaths"))):
            base = as_text(item)
            if base is None:
                continue
            image_id = identifiers[index] if index < len(identifiers) else None
            photos.append(
                self._models.photo(
                    url=f"{base}/500x500.jpg",
                    alt=alt,
                    image_id=image_id,
                    base_url=base,
                )
            )
        return tuple(photos)

    def _detailed_image(self, data: JsonObject, *, alt: str | None) -> Photo | None:
        url = as_text(data.get("imageUrl"))
        if url is None:
            return None
        return self._models.photo(
            url=url,
            kind="detail",
            alt=as_text(data.get("altText")) or alt,
            base_url=_RENDITION.sub("", url) if _RENDITION.search(url) else None,
        )

    # ------------------------------------------------------------ promotions

    def _promotion(self, data: JsonObject) -> Promotion:
        kind = as_text(data.get("type")) or UNKNOWN
        equivalent = as_object(data.get("equivalentPrice"))
        amount = as_decimal(as_object(equivalent.get("totalPrice")).get("amount"))
        return self._models.promotion(
            id=as_text(data.get("promoId")),
            description=as_text(data.get("description")),
            kind=kind,
            price=amount,
            requires_quantity=as_integer(data.get("requiredProductQuantity")),
            member_only=kind == LOYALTY,
            retailer_id=as_text(data.get("retailerPromotionId")),
            presentation_mode=as_text(data.get("presentationMode")),
            limit_reached=as_boolean(data.get("limitReached")) or False,
            is_multi_buy=as_boolean(data.get("isMultiBuy")) or False,
            long_description=as_text(data.get("longDescription")),
        )

    def _promotions(self, data: JsonObject) -> tuple[Promotion, ...]:
        return tuple(
            self._promotion(as_object(item))
            for key in ("promotions", "unavailablePromotions")
            for item in as_items(data.get(key))
        )

    def _previous_price(self, promotions: tuple[Promotion, ...]) -> Decimal | None:
        pattern = self._previous_price_pattern
        if pattern is None:
            return None
        for promotion in promotions:
            description = promotion.description
            if description is not None and pattern.match(description):
                amount = as_euro_text(description)
                if amount is not None:
                    return amount
        return None

    # ----------------------------------------------------------------- price

    def _reference(self, unit: JsonObject) -> UnitPrice | None:
        return self._units.unit_price(
            as_decimal(as_object(unit.get("price")).get("amount")),
            as_text(unit.get("unit")) or as_text(unit.get("unitName")),
        )

    def _price(self, data: JsonObject, promotions: tuple[Promotion, ...]) -> Price:
        promoted = as_object(data.get("promoPrice"))
        current = promoted or as_object(data.get("price"))
        unit = as_object(data.get("unitPrice"))
        # the unit price describes the same price as the amount: a promoted
        # row's comes from its promotional unit price, and is unknown when
        # that is absent
        current_unit = as_object(data.get("promoUnitPrice")) if promoted else unit
        previous = self._previous_price(promotions)
        message_key = as_text(unit.get("unit"))
        # the promotions listing may send the message key alone
        unit_name = as_text(unit.get("unitName")) or self._unit_names.get(
            message_key or ""
        )
        return self._models.price(
            amount=as_decimal(current.get("amount")),
            previous=previous,
            unit_price=as_decimal(as_object(current_unit.get("price")).get("amount")),
            unit_price_unit=unit_name,
            unit_price_text=as_text(unit.get("pricePerSuffix")),
            reference=self._reference(current_unit),
            currency=as_text(current.get("currency")) or "EUR",
            is_discounted=bool(promotions) or previous is not None,
            is_approximate=as_text(data.get("type")) == CATCHWEIGHT,
            unit_name=unit_name,
            unit_message_key=message_key,
        )

    def _quantity(self, data: object) -> object | None:
        value = as_object(data)
        if not value:
            return None
        return self._models.quantity(
            value=as_decimal(value.get("value")), unit=as_text(value.get("uom"))
        )

    def _catchweight(self, data: object) -> object | None:
        value = as_object(data)
        if not value:
            return None
        return self._models.catchweight(
            minimum=self._quantity(value.get("minQuantity")),
            maximum=self._quantity(value.get("maxQuantity")),
            typical=self._quantity(value.get("typicalQuantity")),
        )

    def _product_type(self, value: object) -> StrEnum:
        enum = self._models.product_type
        try:
            return enum(as_text(value) or UNKNOWN)
        except ValueError:
            return enum(UNKNOWN)

    # --------------------------------------------------------------- product

    def _row(self, data: object, *, group_type: str | None) -> dict[str, Any]:
        """return the keyword arguments of one decorated product row."""

        value = as_object(data)
        identifier = as_identifier(value.get("retailerProductId"), "product id")
        name = required_text(value.get("name"), "product name").strip()
        promotions = self._promotions(value)
        campaign = as_object(value.get("featuredProductCampaign"))
        advert = as_text(value.get("externalAdvertId"))
        kind = self._product_type(value.get("type"))
        photos = self._photos(value, alt=name)
        row: dict[str, Any] = {} if self._extras is None else self._extras(value)
        return {
            **row,
            "id": identifier,
            "name": name,
            "brand": as_text(value.get("brand")) or None,
            "pack_size_text": as_text(value.get("packSizeDescription")),
            "thumbnail": photos[0] if photos else None,
            "photos": photos,
            "price": self._price(value, promotions),
            "availability": _availability(value),
            "category_ids": (),
            "promotions": promotions,
            "is_new": as_boolean(value.get("isNew")) or False,
            "is_sponsored": advert is not None or group_type == _SPONSORED_GROUP,
            "is_variable_weight": kind == CATCHWEIGHT,
            "origin": as_text(value.get("countryOfOrigin")),
            "requires_age_check": _requires_age_check(value),
            "product_uuid": as_text(value.get("productId")),
            "product_type": kind,
            "catchweight": self._catchweight(value.get("catchweight")),
            "is_in_current_catalog": as_boolean(value.get("isInCurrentCatalog")),
            "is_time_restricted": as_boolean(value.get("timeRestricted")) or False,
            "group_type": group_type,
            "campaign_id": as_text(campaign.get("campaignId")),
            "campaign_name": as_text(campaign.get("campaignName")),
            "external_advert_id": advert,
        }

    def product(self, data: object, *, group_type: str | None = None) -> ProductT:
        """parse one decorated product, as listings and the batch call serve it."""

        return self._models.product(**self._row(data, group_type=group_type))

    def _fields(self, data: object) -> tuple[FieldLike, ...]:
        fields: list[FieldLike] = []
        for item in as_items(data):
            entry = as_object(item)
            title = as_text(entry.get("title"))
            content = as_text(entry.get("content"))
            if title is None or content is None:
                continue
            fields.append(
                self._models.field(
                    title=title,
                    content=content,
                    text=normalise_whitespace(strip_tags(content)),
                )
            )
        return tuple(fields)

    def _breadcrumbs(self, data: object) -> tuple[CategoryT, ...]:
        path: list[CategoryT] = []
        parent: str | None = None
        for level, item in enumerate(as_items(data)):
            entry = as_object(item)
            identifier = as_text(entry.get("categoryId"))
            name = as_text(entry.get("categoryName")) or as_text(entry.get("name"))
            if identifier is None or name is None:
                continue
            path.append(
                self._models.category(
                    id=identifier,
                    name=name,
                    parent_id=parent,
                    level=level,
                    retailer_category_id=as_text(entry.get("retailerCategoryId")),
                )
            )
            parent = identifier
        return tuple(path)

    def detail(self, data: object) -> ProductT:
        """parse the product-sheet envelope into one fully populated product."""

        envelope = as_object(data)
        # a sheet always wraps its product; anything else is read as a bare one
        product = as_object(envelope.get("product")) or envelope
        row = self._row(product, group_type=None)
        sheet = as_object(envelope.get("bopData"))
        fields = self._fields(sheet.get("fields"))
        breadcrumbs = self._breadcrumbs(sheet.get("breadcrumbs"))
        names = [
            text
            for item in as_items(product.get("categoryPath"))
            if (text := as_text(item)) is not None
        ]
        path = breadcrumbs or tuple(
            self._models.category(id=name, name=name, level=level)
            for level, name in enumerate(names)
        )
        promotions = row["promotions"] + tuple(
            self._promotion(as_object(item))
            for item in as_items(envelope.get("bopPromotions"))
        )
        description = normalise_whitespace(
            strip_tags(as_text(sheet.get("detailedDescription")) or "")
        )
        detailed = tuple(
            photo
            for item in as_items(envelope.get("detailedImages"))
            if (photo := self._detailed_image(as_object(item), alt=row["name"]))
            is not None
        )
        photos: tuple[Photo, ...] = row["photos"]
        known = {item.url for item in photos}
        row.update(
            brand=row["brand"] or _field(fields, "brand"),
            photos=photos
            + tuple(photo for photo in detailed if photo.url not in known),
            category_ids=tuple(item.id for item in breadcrumbs),
            promotions=promotions,
            category_path=path,
            description=description or None,
            legal_name=_field(fields, *LEGAL_NAME_FIELDS),
            origin=row["origin"] or _field(fields, *ORIGIN_FIELDS),
            storage=_field(fields, *STORAGE_FIELDS),
            usage=_field(fields, *USAGE_FIELDS),
            nutrition=parse_nutrition(fields),
            requires_age_check=row["requires_age_check"]
            or bool(as_text(envelope.get("verifyMode"))),
            fields=fields,
        )
        if self._sheet is not None:
            row.update(
                (key, value)
                for key, value in self._sheet(fields).items()
                if value is not None
            )
        return self._models.product(**row)

    # ------------------------------------------------------------ categories

    def _category(
        self,
        data: JsonObject,
        *,
        parent_id: str | None = None,
        level: int = 0,
        products: tuple[ProductT, ...] = (),
    ) -> CategoryT | None:
        identifier = as_text(data.get("categoryId"))
        name = as_text(data.get("name"))
        if identifier is None or name is None:
            return None
        children = tuple(
            child
            for item in as_items(data.get("childCategories"))
            if (
                child := self._category(
                    as_object(item), parent_id=identifier, level=level + 1
                )
            )
            is not None
        )
        return self._models.category(
            id=identifier,
            name=name,
            parent_id=parent_id,
            level=level,
            product_count=as_integer(data.get("productCount")),
            children=children,
            products=products,
            retailer_category_id=as_text(data.get("retailerCategoryId")),
        )

    def categories(self, data: object) -> tuple[CategoryT, ...]:
        """parse the whole category tree, which arrives as a bare json array."""

        return tuple(
            category
            for item in as_items(data)
            if (category := self._category(as_object(item))) is not None
        )

    def category(
        self,
        data: object,
        *,
        products: tuple[ProductT, ...],
        children: tuple[CategoryT, ...],
    ) -> CategoryT | None:
        """parse the category a listing reports it served, with its page attached.

        the node the listing echoes back carries no children of its own; the
        child categories, and the only real product counts the storefront
        publishes, travel beside it on the page.
        """

        info = as_object(as_object(data).get("additionalPageInfo"))
        current = as_object(info.get("currentCategory"))
        category = self._category(current, products=products)
        if category is None or category.children:
            return category
        return dataclasses.replace(category, children=children)

    # --------------------------------------------------------------- listing

    def _filters(self, data: object) -> tuple[object, ...]:
        groups: list[object] = []
        for item in as_items(data):
            value = as_object(item)
            identifier = as_text(value.get("filterId"))
            if identifier is None:
                continue
            groups.append(
                self._models.filter_group(
                    id=identifier,
                    label=as_text(value.get("label")),
                    kind=as_text(value.get("type")),
                    attributes=tuple(
                        self._models.filter_attribute(
                            id=attribute_id,
                            label=as_text(attribute.get("label")),
                            selected=as_boolean(attribute.get("selected")) or False,
                        )
                        for entry in as_items(value.get("attributes"))
                        if (attribute := as_object(entry))
                        and (
                            attribute_id := as_text(attribute.get("filterAttributeId"))
                        )
                        is not None
                    ),
                )
            )
        return tuple(groups)

    def listing_products(self, data: object) -> tuple[ProductT, ...]:
        """flatten every product group, keeping the first placement of each id."""

        products: list[ProductT] = []
        seen: set[str] = set()
        for item in as_items(as_object(data).get("productGroups")):
            group = as_object(item)
            group_type = as_text(group.get("type"))
            for entry in as_items(group.get("decoratedProducts")):
                product = self.product(entry, group_type=group_type)
                if product.id in seen:
                    continue
                seen.add(product.id)
                products.append(product)
        return tuple(products)

    def page_categories(self, data: object) -> tuple[CategoryT, ...]:
        """parse the child categories a listing page publishes beside its rows."""

        info = as_object(as_object(data).get("additionalPageInfo"))
        return tuple(
            category
            for item in as_items(info.get("categories"))
            if (category := self._category(as_object(item))) is not None
        )

    def search_result(self, data: object, *, query: str, page_size: int) -> ResultT:
        """parse a listing envelope into one page and its continuation token."""

        value = as_object(data)
        products = self.listing_products(value)
        info = as_object(value.get("additionalPageInfo"))
        token = next_page_token(value)
        return self._models.search_result(
            query=query,
            products=products,
            page_size=page_size,
            total_hits=None,
            # a token outlives the last page, so an empty page ends the walk
            next_cursor=token if products else None,
            categories=self.page_categories(value),
            filters=self._filters(info.get("filters")),
            sort_options=tuple(
                option
                for item in as_items(info.get("sortOptions"))
                if (option := as_text(as_object(item).get("sortOptionId"))) is not None
            ),
        )

    def decorated(self, data: object) -> tuple[ProductT, ...]:
        """parse the batch decorate response into products."""

        return tuple(
            self.product(item) for item in as_items(as_object(data).get("products"))
        )


def _availability(data: JsonObject) -> Availability:
    available = as_boolean(data.get("available"))
    return Availability(
        available=available,
        status=None
        if available is None
        else ("available" if available else "sold_out"),
        max_quantity=as_decimal(data.get("maxAvailableQuantity")),
    )


def _requires_age_check(data: JsonObject) -> bool:
    return bool(
        as_boolean(data.get("alcohol"))
        or as_boolean(data.get("ageRestriction"))
        or as_boolean(data.get("medicalQuestionnaireRequired"))
        or as_text(data.get("verifyMode"))
    )


def _field(fields: tuple[FieldLike, ...], *titles: str) -> str | None:
    for title in titles:
        for entry in fields:
            if entry.title == title and entry.text:
                return entry.text
    return None


def _allergens(ingredients_html: str | None) -> str | None:
    """return the allergens bolded inside the ingredients html, if any."""

    if ingredients_html is None:
        return None
    terms: list[str] = []
    for match in _BOLD.findall(ingredients_html):
        term = normalise_whitespace(strip_tags(match))
        if term and term not in terms:
            terms.append(term)
    return ", ".join(terms) or None


def parse_nutrition(fields: tuple[FieldLike, ...]) -> Nutrition | None:
    """parse the sheet's nutrition table, ingredients, and allergens.

    the table is an html fragment whose first row names the serving the
    columns are quoted per; every other row becomes one
    :class:`~supermercapy.NutritionValue`. the raw markup is kept so a caller
    can reread a table this parser could not make sense of.
    """

    table = find_field(fields, NUTRITION_FIELD, raw=True)
    ingredients_html = find_field(fields, INGREDIENTS_FIELD, raw=True)
    ingredients = _field(fields, INGREDIENTS_FIELD)
    allergens = _field(fields, ALLERGENS_FIELD) or _allergens(ingredients_html)
    rows = [row for row in table_rows(table or "") if any(row)]
    per: str | None = None
    values: list[NutritionValue] = []
    for index, row in enumerate(rows):
        # the header row names the serving: an empty first cell on bonpreu,
        # "valores medios por:" on alcampo
        if index == 0 and (not row[0] or row[0].endswith(":")):
            per = next((cell for cell in row[1:] if cell), None)
            continue
        if not row[0] or len(row) < 2:
            continue
        values.append(
            NutritionValue(
                name=row[0],
                per_100=row[1] or None,
                per_serving=row[2] or None if len(row) > 2 else None,
            )
        )
    if not (values or ingredients or allergens or table):
        return None
    return Nutrition(
        ingredients=ingredients,
        allergens=allergens,
        values=tuple(values),
        per=per,
        raw_html=table,
    )


def next_page_token(data: object) -> str | None:
    """return the cursor that continues a listing, or ``None`` at the end."""

    token = as_text(as_object(as_object(data).get("metadata")).get("nextPageToken"))
    return token or None


def parse_product_ids(data: object) -> tuple[str, ...]:
    """parse the bare uuid array the similar and related endpoints return."""

    identifiers: list[str] = []
    for item in as_items(data):
        text = as_text(item) or as_text(as_object(item).get("productId"))
        if text and text not in identifiers:
            identifiers.append(text)
    return tuple(identifiers)


def parse_suggestions(data: object) -> tuple[str, ...]:
    """parse the autocomplete response, a flat array of strings."""

    return tuple(text for item in as_items(data) if (text := as_text(item)))


# ------------------------------------------------------------- request flow


class Storefront(Generic[ProductT, CategoryT, ResultT]):
    """one ocado storefront, spoken to through a store client's transport.

    the client owns the connection pool, the retries and the pacing, and
    forwards its transport hooks here; this object builds the requests the
    platform expects, mints the csrf token, classifies the waf's answers and
    parses the responses with the store's :class:`Parser`.
    """

    def __init__(
        self,
        client: BaseClient,
        *,
        api_url: str,
        parser: Parser[ProductT, CategoryT, ResultT],
        sort_options: type[StrEnum],
        challenge_backoff: float,
    ) -> None:
        self._client = client
        self._api_url = api_url
        self.parser = parser
        self._sort_options = sort_options
        self._challenge_backoff = challenge_backoff
        self.csrf_token: str | None = None
        self.session: JsonObject = {}
        self._minting = False

    @property
    def _store(self) -> str:
        return self._client.store_name

    # -------------------------------------------------------------- arguments

    def _sort(self, sort: StrEnum | str | None) -> str | None:
        if sort is None:
            return None
        try:
            return str(self._sort_options(sort).value)
        except ValueError as error:
            supported = ", ".join(repr(item.value) for item in self._sort_options)
            raise ConfigurationError(f"sort must be one of {supported}") from error

    @staticmethod
    def _filters(filters: object) -> str | None:
        if filters is None:
            return None
        try:
            encoded = encode_filters(filters)
        except TypeError as error:
            raise ConfigurationError(
                "filters must be a mapping of filter group id to one or more values"
            ) from error
        return encoded or None

    # --------------------------------------------------------------- listings

    def page(
        self,
        path: str,
        *,
        page_size: int | None,
        cursor: str | None,
        category_id: str | int | None,
        sort: StrEnum | str | None,
        filters: object,
        parameters: dict[str, Any] | None = None,
    ) -> JsonObject:
        """request one listing page and return its raw envelope."""

        size = self._client._resolve_page_size(page_size)
        token = validate_cursor(cursor)
        params: dict[str, Any] = dict(parameters or {})
        if category_id is not None:
            # `category` is silently accepted and answers with the root
            # pseudo-category instead, so only ever send `categoryId`
            params["categoryId"] = validate_identifier(category_id, "category_id")
        params["tag"] = LISTING_TAG
        if token is None:
            params["includeAdditionalPageInfo"] = "true"
        params["maxPageSize"] = size
        params["maxProductsToDecorate"] = size
        chosen = self._sort(sort)
        if chosen is not None:
            params["sortOptionId"] = chosen
        encoded = self._filters(filters)
        if encoded is not None:
            params["filters"] = encoded
        if token is not None:
            params["pageToken"] = token
        return self._client._request_json(
            "GET", f"{self._api_url}{path}", params=params
        )

    def listing(
        self,
        path: str,
        *,
        query: str,
        page_size: int | None,
        cursor: str | None,
        category_id: str | int | None,
        sort: StrEnum | str | None,
        filters: object,
        parameters: dict[str, Any] | None = None,
    ) -> ResultT:
        """request one listing page and parse it."""

        data = self.page(
            path,
            page_size=page_size,
            cursor=cursor,
            category_id=category_id,
            sort=sort,
            filters=filters,
            parameters=parameters,
        )
        return self.parser.search_result(
            data, query=query, page_size=self._client._resolve_page_size(page_size)
        )

    def search(
        self,
        query: str,
        *,
        page_size: int | None,
        cursor: str | None,
        category_id: str | int | None,
        sort: StrEnum | str | None,
        filters: object,
    ) -> ResultT:
        """search one page of products."""

        text = validate_query(query)
        return self.listing(
            f"{PRODUCT_PAGES_WS}/v6/product-pages/search",
            query=query,
            page_size=page_size,
            cursor=cursor,
            category_id=category_id,
            sort=sort,
            filters=filters,
            parameters={"q": text},
        )

    def category(
        self,
        category_id: str | int,
        *,
        page_size: int | None,
        sort: StrEnum | str | None,
        filters: object,
    ) -> CategoryT:
        """return one category with its children and its first page of products.

        an unknown category answers http 404. a listing that echoes back a
        category other than the one asked for, which is how the platform
        falls back to its root, raises :class:`~supermercapy.NotFoundError`
        too, rather than handing back the wrong page.
        """

        wanted = validate_identifier(category_id, "category_id")
        data = self.page(
            f"{PRODUCT_PAGES_WS}/v6/product-pages",
            page_size=page_size,
            cursor=None,
            category_id=wanted,
            sort=sort,
            filters=filters,
        )
        category = self.parser.category(
            data,
            products=self.parser.listing_products(data),
            children=self.parser.page_categories(data),
        )
        if category is None or category.id != wanted:
            raise NotFoundError(f"{self._store} has no category {wanted}")
        return category

    def walk(
        self,
        *,
        page_size: int,
        category_id: str | None = None,
        filters: object = None,
    ) -> tuple[ProductT, ...]:
        """read every product of one listing, page by page.

        the listing is one category, or the whole shop narrowed by
        ``filters``. the first page reports how many products a category
        holds, and the walk stops once that many distinct rows are in hand,
        when no token follows, or when a page adds nothing: a token outlives
        the listing. a count of zero, which is what the whole-shop root
        reports, is no count at all.
        """

        found: dict[str, ProductT] = {}
        cursor: str | None = None
        expected: int | None = None
        while True:
            data = self.page(
                f"{PRODUCT_PAGES_WS}/v6/product-pages",
                page_size=page_size,
                cursor=cursor,
                category_id=category_id,
                sort=None,
                filters=filters,
            )
            if cursor is None:
                category = self.parser.category(data, products=(), children=())
                expected = None if category is None else category.product_count
            before = len(found)
            for product in self.parser.listing_products(data):
                found.setdefault(product.id, product)
            cursor = next_page_token(data)
            if (
                len(found) == before
                or cursor is None
                or (expected and len(found) >= expected)
            ):
                return tuple(found.values())

    def promotions(
        self,
        *,
        region_id: str,
        category_id: str | int | None,
        retailer_category_id: str | int | None,
        page_size: int | None,
        cursor: str | None,
        sort: StrEnum | str | None,
        filters: object,
    ) -> ResultT:
        """return one page of the promotions listing for one region."""

        parameters: dict[str, Any] = {"regionId": region_id}
        if retailer_category_id is not None:
            parameters["retailerCategoryId"] = validate_identifier(
                retailer_category_id, "retailer_category_id"
            )
        return self.listing(
            f"{LISTING_WS}/v1/pages/promotions",
            query="",
            page_size=page_size,
            cursor=cursor,
            category_id=category_id,
            sort=sort,
            filters=filters,
            parameters=parameters,
        )

    # ---------------------------------------------------------------- product

    def product(self, product_id: str | int) -> ProductT:
        """return one product sheet by its numeric ``retailerProductId``."""

        wanted = validate_identifier(product_id, "product_id")
        data = self._client._request_json(
            "GET",
            f"{self._api_url}{PRODUCT_PAGES_WS}/v5/products/bop",
            params={"retailerProductId": wanted},
        )
        return self.parser.detail(data)

    def categories(self, depth: int, *, max_depth: int) -> tuple[CategoryT, ...]:
        """return the category tree down to ``depth`` levels, in one request."""

        if (
            isinstance(depth, bool)
            or not isinstance(depth, int)
            or not 1 <= depth <= max_depth
        ):
            raise ConfigurationError(
                f"depth must be an integer between 1 and {max_depth}"
            )
        data = self._client._request_json_any(
            "GET",
            f"{self._api_url}{PRODUCT_PAGES_WS}/v1/categories",
            params={"decoration": "false", "categoryDepth": depth},
        )
        return self.parser.categories(data)

    def suggest(self, term: str, *, limit: int, region_id: str) -> tuple[str, ...]:
        """return the autocomplete suggestions for a partial search term."""

        text = validate_query(term)
        data = self._client._request_json_any(
            "GET",
            f"{self._api_url}{SEARCH_WS}/v1/suggestions/primary",
            params={
                "searchTerm": text,
                "limit": validate_limit(limit, "limit"),
                "regionId": region_id,
            },
        )
        return parse_suggestions(data)

    def related(
        self, path: str, product_id: str | int, parameters: dict[str, Any]
    ) -> tuple[ProductT, ...]:
        """resolve the uuids the similar or related endpoint answers with."""

        wanted = validate_identifier(product_id, "product_id")
        data = self._client._request_json_any(
            "GET",
            f"{self._api_url}{PRODUCT_PAGES_WS}/v5/products/{path}",
            params={"retailerProductId": wanted, **parameters},
        )
        return self.decorate(parse_product_ids(data))

    def decorate(self, product_ids: tuple[str, ...]) -> tuple[ProductT, ...]:
        """resolve internal uuids to products with the one batch endpoint."""

        if not product_ids:
            return ()
        self.ensure_csrf()
        data = self._client._request_json(
            "PUT",
            f"{self._api_url}{PRODUCT_PAGES_WS}/v6/products",
            json=list(product_ids),
        )
        return self.parser.decorated(data)

    # ------------------------------------------------------------------ csrf

    def ensure_csrf(self) -> None:
        """mint the token every non-get request needs, from the home page.

        the same page carries the anonymous session's metadata, such as the
        visitor id and the region it landed in, which is kept on
        :attr:`session`.
        """

        if self.csrf_token is not None:
            return
        self._minting = True
        try:
            page = self._client._request_text(
                "GET", f"{self._api_url}/", headers={"Accept": HTML_ACCEPT}
            )
        finally:
            self._minting = False
        state = extract_json_assignment(page, STATE_MARKER)
        session = as_object(as_object(state).get("session"))
        token = as_text(as_object(session.get("csrf")).get("token"))
        if not token:
            raise InvalidResponseError(f"{self._store} served no csrf token")
        self.csrf_token = token
        self.session = as_object(session.get("metadata"))

    # ------------------------------------------------------- transport hooks

    def prepare(self, request: httpx.Request) -> None:
        """attach the csrf token the way the storefront does.

        the storefront's own rule is mechanical: every non-get carries the
        token, every get carries none.
        """

        if request.method != "GET" and self.csrf_token is not None:
            request.headers[CSRF_HEADER] = self.csrf_token

    def classify(self, response: httpx.Response) -> ResponseVerdict | None:
        """classify a waf answer, or return ``None`` for the default rules."""

        # every response the origin produces carries `requestid`; the waf's
        # own answers at the edge do not
        from_origin = REQUEST_ID_HEADER in response.headers
        if response.status_code == CHALLENGE_STATUS and (
            WAF_ACTION_HEADER in response.headers or not from_origin
        ):
            return ResponseVerdict.CHALLENGED
        if response.status_code == REFUSED_STATUS:
            return (
                ResponseVerdict.AUTH_EXPIRED if from_origin else ResponseVerdict.BLOCKED
            )
        return None

    def on_auth_expired(self) -> bool:
        """mint a fresh token after the origin's 403 and allow one retry."""

        # the origin's 403 is a stale or missing csrf token, not a block
        if self._minting:
            return False
        self.csrf_token = None
        try:
            self.ensure_csrf()
        except BlockedError:
            # a waf block or challenge on the token page is the real answer;
            # reporting it as a credentials failure would hide it
            raise
        except (TransportError, InvalidResponseError, AuthenticationError):
            return False
        return True

    def on_challenge(self, response: httpx.Response) -> bool:
        """raise on a waf challenge rather than spend more of the budget."""

        response.close()
        raise ChallengedError(
            f"{self._store} answered with an aws waf challenge (http "
            f"{response.status_code}); the budget is per ip and refills over "
            "tens of minutes, so stop rather than retry",
            status_code=response.status_code,
            suggested_backoff=self._challenge_backoff,
        )
