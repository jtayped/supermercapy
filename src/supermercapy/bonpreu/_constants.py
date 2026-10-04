"""hosts, paths, and the static ids the bonpreu storefront is keyed on."""

from __future__ import annotations

from types import MappingProxyType

from .._core.models import Language

API_URL = "https://www.compraonline.bonpreuesclat.cat"
HOST = "www.compraonline.bonpreuesclat.cat"

IMAGE_URL = f"{API_URL}/images-v3"
TENANT_ID = "dcbcfd72-cf23-44a2-8e14-8a38edd645a3"

# the tenant runs one region and the server rejects any other uuid; a delivery
# destination a hundred kilometres away resolves to this same id and the cart
# reports the assortment unchanged, so it is a constant rather than a binding.
REGION_ID = "d57f6327-ce67-44d5-bdd5-dc63e98460a6"

# the pseudo-category a listing falls back to when its category filter is not
# understood; seeing it back means the request asked for something else
ROOT_CATEGORY_ID = "3d91c51c-1228-4f06-9afe-f31a68c3e740"

# "novetats", retailer category 90. pinned rather than resolved through the
# tree, because every request spends the same scarce budget (see the challenge
# note below).
NEW_ARRIVALS_CATEGORY_ID = "2068f9ba-b1af-4cf1-ad2b-3301c8d1a977"

LANGUAGE_COOKIE = "language"
LANGUAGE_VALUES = MappingProxyType(
    {Language.CATALAN: "ca-ES", Language.SPANISH: "es-ES"}
)
"""the cookie value each language travels as; ``Accept-Language`` is ignored."""

UNIT_NAMES = MappingProxyType(
    {
        "fop.price.per.each": "EACH",
        "fop.price.per.kg": "PER_1KG",
        "fop.price.per.litre": "PER_LITRE",
        "fop.price.per.100ml": "PER_100ML",
        "fop.price.per.dozen": "PER_DOZEN",
    }
)
"""the unit name each unit-price message key stands for.

the promotions listing stopped sending ``unitName`` in october 2026 and kept
only the message key; these are the pairs seen across 335 rows of the other
listings, which still send both.
"""

# the aws waf's budget is per ip, worth twenty to thirty requests cold in
# september 2026 and eight to thirteen in october, and refills over tens of
# minutes; a challenge suggests waiting this long. since october 2026 the same
# waf also refuses every `/api/` path to a user agent that does not look like a
# browser, so the client sends one.
CHALLENGE_BACKOFF = 1800.0

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 300
MAX_CATEGORY_DEPTH = 4
DEFAULT_SUGGESTION_LIMIT = 10

FIELD_TITLES = frozenset(
    {
        "agent",
        "alcoholByVolume",
        "alcoholUnits",
        "allergens",
        "beerDegree",
        "brand",
        "brandMarketing",
        "breed",
        "categorySpecificAttributes",
        "certifications",
        "color",
        "contactInformation",
        "cookingGuidelines",
        "countryOfBirth",
        "countryOfBottling",
        "countryOfFarming",
        "countryOfLastProcessing",
        "countryOfManufacture",
        "countryOfOrigin",
        "countryOfPackaging",
        "countryOfRearing",
        "countryOfSlaughter",
        "currentVintage",
        "dietaryInformation",
        "electricalAndBatteryWasteManagement",
        "features",
        "fishingArea",
        "furtherDescription",
        "grapeVariety",
        "history",
        "ingredients",
        "lifestyle",
        "lifestyleOther",
        "manufacturer",
        "manufacturerMarketing",
        "methodOfCapture",
        "nutritionalData",
        "otherInformation",
        "packageType",
        "pharmaInformation",
        "precautionaryStatements",
        "preparationAndUsage",
        "producer",
        "productLine",
        "recipes",
        "recognitions",
        "recyclingInformation",
        "regionalInformation",
        "returnToAddress",
        "safetyInstructions",
        "scientificName",
        "servingSuggestions",
        "size",
        "specification",
        "storage",
        "storageAndUsage",
        "subBrand",
        "tasteCategory",
        "tastingNotes",
        "unitType",
        "units",
        "vinificationDetails",
        "welfareDetails",
        "winemaker",
    }
)
"""every title a product sheet field can carry, enumerated from the storefront.

the list is closed, so a title outside it is a storefront change rather than a
product quirk; unknown titles are still kept on
:attr:`~supermercapy.bonpreu.BonpreuProduct.fields`.
"""
