"""plusfresc's models and parsers, built on the shared core models.

every label the storefront publishes arrives in a repeated ``texts`` array of
``{lang, order, text, type}`` objects carrying catalan and spanish side by
side, so the models keep both spellings and resolve the plain ``name`` for the
client's language. listing rows and product details share one shape, wrapped in
a ``product`` key by the detail endpoint, so there is one
:class:`PlusfrescProduct` rather than a summary and a detail type.

the storefront publishes no barcode, so ``ean`` is always ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import IntEnum, StrEnum
from types import MappingProxyType

from .._core.coerce import (
    JsonObject,
    as_boolean,
    as_cents,
    as_dmy_date,
    as_identifier,
    as_integer,
    as_items,
    as_object,
    as_text,
    required_text,
)
from .._core.models import (
    Availability,
    Category,
    Language,
    Nutrition,
    NutritionValue,
    Photo,
    Price,
    Product,
    Promotion,
    SearchResult,
    Store,
)
from .._core.units import UnitReader
from ._constants import IMAGE_URL, LOCKER_INFIX, SEARCH_RESULT_LIMIT, SITE_URL

__all__ = [
    "NUTRITION_UNITS",
    "ImageSize",
    "PlusfrescAllergen",
    "PlusfrescCategory",
    "PlusfrescCharacteristic",
    "PlusfrescFormat",
    "PlusfrescFormatOption",
    "PlusfrescNutrition",
    "PlusfrescNutritionValue",
    "PlusfrescPhoto",
    "PlusfrescProduct",
    "PlusfrescPromotion",
    "PlusfrescSearchResult",
    "PlusfrescStore",
    "PlusfrescUnit",
    "TextType",
    "parse_category",
    "parse_center",
    "parse_pickup_point",
    "parse_product",
    "parse_search_result",
    "parse_unit",
    "resolve_center_id",
    "text_in",
    "texts_of",
]

_HUNDRED = Decimal(100)
_LARGE_SUFFIX = "_gran"

# the category id promo pseudo-categories carry in place of a real parent.
_UNUSED_CATEGORY = "Field not used"
# an "m for n" campaign, whose qty_required is the units the deal needs; the
# "CategoXXX_" bundles send values such as 0, 1000 and 2000 there that match
# no condition the storefront shows, so theirs is not read
_MULTIBUY_PREFIX = "CategoMxN_"


class TextType(IntEnum):
    """what one entry of a ``texts`` array says, as the bundle's accessors read it."""

    CATEGORY_NAME = 1
    FORMAT_TITLE = 2
    FORMAT_OPTION = 3
    PRODUCT_NAME = 4
    CHARACTERISTIC = 5
    PROMOTION = 6
    SLUG = 7
    ICON_KEY = 8
    PROMO_DESCRIPTION = 9
    PROMO_IMAGE = 10


class ImageSize(StrEnum):
    """the two fixed renditions the image host serves.

    there is no resizing proxy and no width parameter; the large rendition is
    the small one's filename with a ``_gran`` suffix.
    """

    SMALL = "small"
    LARGE = "large"


NUTRITION_UNITS: Mapping[str, str] = MappingProxyType(
    {"E14": "kcal", "KJO": "kJ", "GR": "g"}
)
"""the coded nutrition units observed so far; no endpoint enumerates them."""

# the unit_measure codes the shared vocabulary does not know, as the units
# endpoint names them: dotzena and dosis
_UNITS = UnitReader({"dot": "dotzena", "do": "dosis"})


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescPhoto(Photo):
    """one product image, switchable between the two renditions."""

    def sized(self, size: ImageSize | str) -> str:
        """return the same image at the other rendition without performing i/o.

        the url is returned unchanged when its filename has no extension to
        move the suffix around.
        """

        try:
            wanted = ImageSize(size)
        except ValueError as error:
            supported = ", ".join(repr(item.value) for item in ImageSize)
            raise ValueError(f"size must be one of {supported}") from error
        stem, separator, extension = self.url.rpartition(".")
        if not separator or "/" in extension:
            return self.url
        stem = stem.removesuffix(_LARGE_SUFFIX)
        if wanted is ImageSize.LARGE:
            stem = f"{stem}{_LARGE_SUFFIX}"
        return f"{stem}.{extension}"


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescCharacteristic:
    """one dietary or origin badge, such as gluten-free or catalan produce.

    ``id`` is upper-cased because the source data mixes casing for the same
    badge; ``raw_id`` keeps the spelling the storefront sent.
    """

    id: str
    raw_id: str
    icon_key: str | None = None
    label_ca: str | None = None
    label_es: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescFormatOption:
    """one selectable option of a service format, such as a six-pack.

    the four numeric bounds are thousandths of a selling unit, exactly as the
    storefront sends them.
    """

    option_id: str
    description_ca: str | None = None
    description_es: str | None = None
    min_value_mili: int | None = None
    max_value_mili: int | None = None
    interval_mili: int | None = None
    percentage_mili: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescFormat:
    """one group of alternative pack formats a product can be bought in."""

    format_id: str
    order: int | None = None
    title_ca: str | None = None
    title_es: str | None = None
    options: tuple[PlusfrescFormatOption, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescPromotion(Promotion):
    """one offer attached to a product.

    a plain markdown carries only a strapline and an end date; a multi-buy
    campaign adds ``id``, ``requires_quantity`` and the other item ids that
    count toward it. ``requires_quantity`` is the number of units an "m for
    n" deal needs, such as 2 for "la 2ª ut. al 50%", and ``None`` on the
    ``CategoXXX_`` bundles, whose quantity field matches nothing the
    storefront shows.
    """

    end_date: date | None = None
    band_url: str | None = None
    chip_color: str | None = None
    strapline_ca: str | None = None
    strapline_es: str | None = None
    combinable_with: tuple[str, ...] = ()

    @property
    def rgba(self) -> tuple[int, int, int, int] | None:
        """return ``chip_color`` as red, green, blue, and alpha bytes."""

        if self.chip_color is None:
            return None
        parts = self.chip_color.split(";")
        if len(parts) != 4 or not all(part.strip().isdigit() for part in parts):
            return None
        red, green, blue, alpha = (int(part) for part in parts)
        return red, green, blue, alpha


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescAllergen:
    """one allergen row of a product sheet."""

    name: str
    description: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescNutritionValue(NutritionValue):
    """one row of a nutrition table, with the storefront's coded unit kept."""

    per_product: str | None = None
    per_ingest: str | None = None
    unit_code: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescNutrition(Nutrition):
    """the ingredient, allergen, and nutrition blocks of a product sheet.

    the free text is always spanish, even when the sheet is requested in
    catalan; the nutrition table and the allergen names are localised.
    """

    values: tuple[PlusfrescNutritionValue, ...] = ()
    allergen_list: tuple[PlusfrescAllergen, ...] = ()
    net_weight: str | None = None
    ration_value: str | None = None
    has_ingredients: bool = False
    has_nutritionals: bool = False
    has_allergens: bool = False
    extended: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescCategory(Category):
    """one node of the storefront category tree.

    ids are mostly positional, a child extending its parent's id by two
    digits, but a few nodes break the pattern, so a parent is never inferred
    from an id: only the whole tree sets ``parent_id``. alongside them sit the
    pseudo-categories ``Root``, ``Oferta2``, ``PromoHighlight``, and the
    ``CategoXXX_…`` promo bundles, which carry an empty display name and
    describe themselves through ``description_ca`` and ``description_es``
    instead.
    """

    name_ca: str | None = None
    name_es: str | None = None
    description_ca: str | None = None
    description_es: str | None = None
    banner_url: str | None = None
    banner_id: str | None = None
    order: int | None = None
    row: int | None = None
    is_visible: bool = True
    leaf_count: int | None = None
    promo_type: int | None = None
    promo_subtype: str | None = None
    batch_price: Decimal | None = None
    children: tuple[PlusfrescCategory, ...] = ()
    products: tuple[PlusfrescProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescProduct(Product):
    """one product, in the single shape listings and details share.

    ``id`` is the six-digit ``item_id``, which is the product key and the
    argument the detail endpoint takes. ``placement_id`` is the composite
    ``filter_id + item_id`` the storefront calls ``id``; it identifies a
    placement, not a product, and repeats across the catalogue.

    ``ean`` is always ``None``: the storefront publishes no barcode anywhere.
    """

    item_id: str
    placement_id: str | None = None
    leaf_category_id: str | None = None
    name_ca: str | None = None
    name_es: str | None = None
    unit_measure: str | None = None
    order: int | None = None
    has_format: bool = False
    formats: tuple[PlusfrescFormat, ...] = ()
    characteristics: tuple[PlusfrescCharacteristic, ...] = ()
    thumbnail: PlusfrescPhoto | None = None
    photos: tuple[PlusfrescPhoto, ...] = ()
    promotions: tuple[PlusfrescPromotion, ...] = ()
    nutrition: PlusfrescNutrition | None = None
    label: str | None = None
    additional_data: str | None = None

    def characteristic(self, code: str) -> PlusfrescCharacteristic | None:
        """return one badge by its id, matched without regard to casing."""

        wanted = code.upper() if isinstance(code, str) else code
        for characteristic in self.characteristics:
            if characteristic.id == wanted:
                return characteristic
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescSearchResult(SearchResult):
    """one page carved client-side out of a single unpaged response.

    the backend returns every matching row at once, so ``next_cursor`` is an
    offset into rows already in hand rather than a request the storefront
    understands. ``truncated`` says the search hit its hard cap of a hundred
    rows, which is also the point at which the count stops being a total.
    """

    offset: int = 0
    row_count: int = 0
    products: tuple[PlusfrescProduct, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescStore(Store):
    """one preparation center or pickup point.

    ``id`` is always a center id a client can be built with, so a locker's
    embedded parent store is resolved for you; ``pickup_code`` keeps the raw
    id the storefront listed.
    """

    center_id: int
    name_ca: str | None = None
    name_es: str | None = None
    pickup_code: str | None = None
    is_locker: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class PlusfrescUnit:
    """one unit-of-measure code and its localised name."""

    code: str
    name: str
    language: str | None = None


# --------------------------------------------------------------------- parsers


def texts_of(data: object, text_type: TextType | int) -> dict[str, str]:
    """return the first non-blank text of ``text_type``, keyed by language."""

    found: dict[str, str] = {}
    for item in as_items(data):
        entry = as_object(item)
        if as_integer(entry.get("type")) != int(text_type):
            continue
        language = as_text(entry.get("lang"))
        text = (as_text(entry.get("text")) or "").strip()
        if language and text:
            found.setdefault(language, text)
    return found


def text_in(texts: Mapping[str, str], language: Language | str) -> str | None:
    """return one language's text, falling back to whichever one exists."""

    wanted = language.value if isinstance(language, Language) else str(language)
    if wanted in texts:
        return texts[wanted]
    for value in texts.values():
        return value
    return None


def _lines_of(data: object, text_type: TextType | int) -> dict[str, str]:
    lines: dict[str, list[str]] = {}
    for item in as_items(data):
        entry = as_object(item)
        if as_integer(entry.get("type")) != int(text_type):
            continue
        language = as_text(entry.get("lang"))
        text = (as_text(entry.get("text")) or "").strip()
        if language and text:
            lines.setdefault(language, []).append(text)
    return {language: "\n".join(values) for language, values in lines.items()}


def _image_url(filename: object) -> str | None:
    name = (as_text(filename) or "").strip()
    return f"{IMAGE_URL}/{name}" if name else None


def _image(filename: object, kind: str) -> PlusfrescPhoto | None:
    url = _image_url(filename)
    return None if url is None else PlusfrescPhoto(url=url, kind=kind)


def _site_url(path: object) -> str | None:
    text = (as_text(path) or "").strip()
    if not text:
        return None
    return text if text.startswith("http") else f"{SITE_URL}{text}"


def parse_characteristic(data: object) -> PlusfrescCharacteristic | None:
    """parse one badge, upper-casing the inconsistently spelled id."""

    value = as_object(data)
    raw_id = (as_text(value.get("id")) or "").strip()
    if not raw_id:
        return None
    labels = texts_of(value.get("texts"), TextType.CHARACTERISTIC)
    return PlusfrescCharacteristic(
        id=raw_id.upper(),
        raw_id=raw_id,
        icon_key=as_text(value.get("embedded_icon_url")) or None,
        label_ca=labels.get("ca"),
        label_es=labels.get("es"),
    )


def parse_format(data: object) -> PlusfrescFormat | None:
    """parse one alternative pack format and its options."""

    value = as_object(data)
    format_id = (as_text(value.get("format_id")) or "").strip()
    if not format_id:
        return None
    titles = texts_of(value.get("title"), TextType.FORMAT_TITLE)
    options: list[PlusfrescFormatOption] = []
    for item in as_items(value.get("options")):
        option = as_object(item)
        option_id = (as_text(option.get("option_id")) or "").strip()
        if not option_id:
            continue
        descriptions = texts_of(option.get("description"), TextType.FORMAT_OPTION)
        options.append(
            PlusfrescFormatOption(
                option_id=option_id,
                description_ca=descriptions.get("ca"),
                description_es=descriptions.get("es"),
                min_value_mili=as_integer(option.get("min_value_mili")),
                max_value_mili=as_integer(option.get("max_value_mili")),
                interval_mili=as_integer(option.get("interval_mili")),
                percentage_mili=as_integer(option.get("percentage_mili")),
            )
        )
    return PlusfrescFormat(
        format_id=format_id,
        order=as_integer(value.get("order")),
        title_ca=titles.get("ca"),
        title_es=titles.get("es"),
        options=tuple(options),
    )


def _price(value: JsonObject, *, ends_at: datetime | None) -> Price:
    listed = as_cents(value.get("value_cents"))
    reduced = as_cents(value.get("new_value_cents"))
    discounted = reduced is not None and reduced != listed
    amount = reduced if reduced is not None else listed
    discount: Decimal | None = None
    if discounted and listed is not None and amount is not None and listed > 0:
        discount = ((listed - amount) / listed * _HUNDRED).quantize(Decimal("0.01"))
    unit_price = as_cents(value.get("value_x_unit"))
    unit_price_unit = as_text(value.get("unit_measure")) or None
    return Price(
        amount=amount,
        previous=listed if discounted else None,
        unit_price=unit_price,
        unit_price_unit=unit_price_unit,
        reference=_UNITS.unit_price(unit_price, unit_price_unit),
        is_discounted=discounted,
        discount_percentage=discount,
        valid_until=ends_at,
    )


def _promotion(value: JsonObject, *, ends_at: datetime | None) -> PlusfrescPromotion:
    straplines = texts_of(value.get("texts"), TextType.PROMOTION)
    promotion_id = (as_text(value.get("promo_id")) or "").strip() or None
    reduced = as_cents(value.get("new_value_cents"))
    return PlusfrescPromotion(
        id=promotion_id,
        description=straplines.get("ca") or straplines.get("es"),
        kind="multibuy" if promotion_id is not None else "markdown",
        price=reduced,
        requires_quantity=(
            as_integer(value.get("qty_required"))
            if promotion_id is not None and promotion_id.startswith(_MULTIBUY_PREFIX)
            else None
        ),
        ends_at=ends_at,
        end_date=as_dmy_date(value.get("end_date")),
        band_url=_image_url(value.get("band_uri")),
        chip_color=as_text(value.get("chip_color")) or None,
        strapline_ca=straplines.get("ca"),
        strapline_es=straplines.get("es"),
        combinable_with=tuple(
            text
            for item in as_items(value.get("combinable_with"))
            if (text := (as_text(item) or "").strip())
        ),
    )


def _category_ids(value: JsonObject) -> tuple[str, ...]:
    ids = [
        text
        for key in ("category_id", "filter_id")
        if (text := (as_text(value.get(key)) or "").strip())
        and text != _UNUSED_CATEGORY
    ]
    return tuple(dict.fromkeys(ids))


def _nutrition_value(data: object) -> PlusfrescNutritionValue | None:
    value = as_object(data)
    name = (as_text(value.get("nutritionalname")) or "").strip()
    if not name:
        return None
    code = as_text(value.get("nutritionalunit")) or None
    return PlusfrescNutritionValue(
        name=name,
        per_100=as_text(value.get("per_100")) or None,
        per_serving=as_text(value.get("per_ration")) or None,
        unit=None if code is None else NUTRITION_UNITS.get(code),
        per_product=as_text(value.get("per_product")) or None,
        per_ingest=as_text(value.get("per_ingest")) or None,
        unit_code=code,
    )


def _allergens(data: object) -> tuple[PlusfrescAllergen, ...]:
    allergens: list[PlusfrescAllergen] = []
    for item in as_items(data):
        value = as_object(item)
        name = (as_text(value.get("alergensname")) or "").strip()
        if not name:
            continue
        allergens.append(
            PlusfrescAllergen(
                name=name,
                description=(as_text(value.get("alergendescription")) or "").strip()
                or None,
            )
        )
    return tuple(allergens)


def _allergen_text(allergens: tuple[PlusfrescAllergen, ...]) -> str | None:
    grouped: dict[str, list[str]] = {}
    for allergen in allergens:
        grouped.setdefault(allergen.description or "", []).append(allergen.name)
    parts = [
        ", ".join(names) if not description else f"{description}: {', '.join(names)}"
        for description, names in grouped.items()
    ]
    return "; ".join(parts) or None


def parse_nutrition(data: object) -> PlusfrescNutrition | None:
    """parse the sheet blocks of a product detail, or ``None`` when all empty."""

    value = as_object(data)
    details = as_object(value.get("filedetails"))
    values = tuple(
        parsed
        for item in as_items(value.get("nutritionals"))
        if (parsed := _nutrition_value(item)) is not None
    )
    allergens = _allergens(value.get("allergens"))
    ingredients = (as_text(details.get("ingredient_list")) or "").strip() or None
    net_weight = (as_text(details.get("netweight")) or "").strip() or None
    ration = (as_text(details.get("nutritionalrationvalue")) or "").strip() or None
    if not (values or allergens or ingredients or net_weight):
        return None
    return PlusfrescNutrition(
        ingredients=ingredients,
        allergens=_allergen_text(allergens),
        values=values,
        per=ration,
        allergen_list=allergens,
        net_weight=net_weight,
        ration_value=ration,
        has_ingredients=_is_populated(details.get("isingre")),
        has_nutritionals=_is_populated(details.get("isnutritional")),
        has_allergens=_is_populated(details.get("isallergens")),
        extended=(as_text(details.get("extended")) or "").strip() or None,
    )


def _is_populated(value: object) -> bool:
    text = (as_text(value) or "").strip()
    return bool(text) and text != "0"


def parse_product(
    data: object, *, language: Language | str = Language.CATALAN
) -> PlusfrescProduct:
    """parse one product from a listing row or the product-detail envelope.

    the detail endpoint wraps the same row in ``product`` and adds the sheet
    blocks alongside it, so both shapes are handled here.
    """

    envelope = as_object(data)
    value = as_object(envelope.get("product")) if "product" in envelope else envelope
    item_id = as_identifier(value.get("item_id"), "product item id")
    names = texts_of(value.get("texts"), TextType.PRODUCT_NAME)
    ends_at = _midnight(as_dmy_date(value.get("end_date")))
    thumbnail = _image(value.get("icon"), "icon")
    main = _image(value.get("image_url"), "image")
    photos = tuple(photo for photo in (main, thumbnail) if photo is not None)
    promotion = (
        _promotion(value, ends_at=ends_at)
        if value.get("promo_id") is not None or value.get("new_value_cents") is not None
        else None
    )
    details = as_object(envelope.get("filedetails"))
    return PlusfrescProduct(
        id=item_id,
        name=required_text(text_in(names, language), "product name"),
        brand=(as_text(value.get("brand_name")) or "").strip() or None,
        ean=None,
        pack_size_text=(as_text(details.get("netweight")) or "").strip() or None,
        thumbnail=thumbnail,
        photos=photos,
        price=_price(value, ends_at=ends_at),
        availability=Availability(available=as_boolean(value.get("available"))),
        category_ids=_category_ids(value),
        promotions=() if promotion is None else (promotion,),
        description=(as_text(details.get("desc")) or "").strip() or None,
        storage=(as_text(details.get("conservation")) or "").strip() or None,
        nutrition=parse_nutrition(envelope),
        item_id=item_id,
        placement_id=(as_text(value.get("id")) or "").strip() or None,
        leaf_category_id=(as_text(value.get("filter_id")) or "").strip() or None,
        name_ca=names.get("ca"),
        name_es=names.get("es"),
        unit_measure=(as_text(value.get("unit_measure")) or "").strip() or None,
        order=as_integer(value.get("order")),
        has_format=as_boolean(value.get("has_format")) or False,
        formats=tuple(
            parsed
            for item in as_items(value.get("formats"))
            if (parsed := parse_format(item)) is not None
        ),
        characteristics=tuple(
            badge
            for item in as_items(value.get("characteristics"))
            if (badge := parse_characteristic(item)) is not None
        ),
        label=(as_text(details.get("etiqueta")) or "").strip() or None,
        additional_data=(as_text(details.get("additionaldata")) or "").strip() or None,
    )


def _midnight(value: date | None) -> datetime | None:
    if value is None:
        return None
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def _category_name(
    names: Mapping[str, str],
    descriptions: Mapping[str, str],
    language: Language | str,
) -> str | None:
    name = text_in(names, language)
    if name:
        return name
    # a promo pseudo-category leaves the display name empty and opens its
    # description block with the headline instead.
    description = text_in(descriptions, language)
    return description.splitlines()[0] if description else None


def parse_category(
    data: object,
    *,
    language: Language | str = Language.CATALAN,
    parent_id: str | None = None,
) -> PlusfrescCategory:
    """parse one node of the category tree and every node beneath it.

    promo pseudo-categories publish an empty display name, so the node's own
    id stands in rather than the parse failing.
    """

    value = as_object(data)
    category_id = as_identifier(value.get("id"), "category id")
    names = texts_of(value.get("texts"), TextType.CATEGORY_NAME)
    descriptions = _lines_of(value.get("texts"), TextType.PROMO_DESCRIPTION)
    banners = texts_of(value.get("texts"), TextType.PROMO_IMAGE)
    leaf_count = as_integer(value.get("leaf_count"))
    return PlusfrescCategory(
        id=category_id,
        name=_category_name(names, descriptions, language) or category_id,
        parent_id=parent_id,
        level=as_integer(value.get("level")),
        slug=text_in(texts_of(value.get("texts"), TextType.SLUG), language),
        image_url=_site_url(value.get("icon")),
        product_count=leaf_count,
        name_ca=names.get("ca"),
        name_es=names.get("es"),
        description_ca=descriptions.get("ca"),
        description_es=descriptions.get("es"),
        banner_url=_site_url(text_in(banners, language)),
        banner_id=(as_text(value.get("banner_id")) or "").strip() or None,
        order=as_integer(value.get("order")),
        row=as_integer(value.get("row")),
        is_visible=as_boolean(value.get("visible")) is not False,
        leaf_count=leaf_count,
        promo_type=as_integer(value.get("promo_type")),
        promo_subtype=as_text(value.get("promo_subtype")) or None,
        batch_price=as_cents(value.get("batch_cents")),
        children=tuple(
            parse_category(item, language=language, parent_id=category_id)
            for item in as_items(value.get("childs"))
        ),
    )


def parse_search_result(
    rows: list[object],
    *,
    query: str,
    offset: int,
    page_size: int,
    language: Language | str = Language.CATALAN,
    limit: int | None = SEARCH_RESULT_LIMIT,
) -> PlusfrescSearchResult:
    """slice one page out of the whole unpaged response the backend returns.

    ``limit`` is the row cap the endpoint applies, which search has and a
    category listing does not; ``None`` never flags the page as truncated.
    """

    products = tuple(parse_product(item, language=language) for item in rows)
    page = products[offset : offset + page_size]
    consumed = offset + len(page)
    return PlusfrescSearchResult(
        query=query,
        products=page,
        page_size=page_size,
        total_hits=len(products),
        next_cursor=str(consumed) if consumed < len(products) else None,
        truncated=limit is not None and len(products) >= limit,
        offset=offset,
        row_count=len(products),
    )


def parse_center(data: object) -> PlusfrescStore | None:
    """parse one preparation center from the center enumeration."""

    value = as_object(data)
    center_id = as_integer(value.get("centerId"))
    if center_id is None:
        return None
    name_ca = (as_text(value.get("nameCa")) or "").strip() or None
    name_es = (as_text(value.get("nameEs")) or "").strip() or None
    return PlusfrescStore(
        id=str(center_id),
        name=name_ca or name_es or str(center_id),
        kind="center",
        address=(as_text(value.get("street")) or "").strip() or None,
        center_id=center_id,
        name_ca=name_ca,
        name_es=name_es,
    )


def resolve_center_id(value: object) -> int | None:
    """return the real center behind a center or locker id.

    a locker id embeds its parent store around a fixed literal plus a trailing
    check character, exactly as the storefront's own bundle strips them.
    """

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if LOCKER_INFIX in text:
        text = text.replace(LOCKER_INFIX, "", 1)[:-1]
    return int(text) if text.isdigit() else None


def parse_pickup_point(data: object) -> PlusfrescStore | None:
    """parse one pickup point, resolving a locker id to its parent center.

    the ``addressing`` field is a three-line postal block: street, then postal
    code and town, then province.
    """

    value = as_object(data)
    code = (as_text(value.get("centerid")) or "").strip() or None
    center_id = resolve_center_id(code)
    if center_id is None:
        return None
    is_locker = (as_text(value.get("TipoRecogida")) or "").strip() == "1"
    lines = [
        line.strip()
        for line in (as_text(value.get("addressing")) or "").splitlines()
        if line.strip()
    ]
    postal_code, _, city = (lines[1] if len(lines) > 1 else "").partition(" ")
    return PlusfrescStore(
        id=str(center_id),
        name=(as_text(value.get("centername")) or "").strip() or str(center_id),
        kind="locker" if is_locker else "store",
        address=lines[0] if lines else None,
        postal_code=postal_code.strip() or None if postal_code.isdigit() else None,
        city=city.strip() or None,
        province=lines[2] if len(lines) > 2 else None,
        center_id=center_id,
        pickup_code=code,
        is_locker=is_locker,
    )


def parse_unit(data: object) -> PlusfrescUnit | None:
    """parse one unit-of-measure label."""

    value = as_object(data)
    code = (as_text(value.get("codi")) or "").strip()
    name = (as_text(value.get("text")) or "").strip()
    if not code or not name:
        return None
    return PlusfrescUnit(
        code=code, name=name, language=as_text(value.get("idioma")) or None
    )
