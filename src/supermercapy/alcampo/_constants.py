"""hosts, paths, and the static ids the alcampo storefront is keyed on."""

from __future__ import annotations

from types import MappingProxyType

API_URL = "https://www.compraonline.alcampo.es"
HOST = "www.compraonline.alcampo.es"
IMAGE_URL = f"{API_URL}/images-v3"

# the region a new anonymous session lands in: retailer region 5, "vaguada",
# the hypermarket in north madrid. every region is one fulfilment centre and
# carries its own prices and assortment.
DEFAULT_REGION_ID = "ac90d761-9d58-4918-a37d-dd14e1ce384a"

# the anonymous session, which one put moves to another region the way the
# storefront's own region picker does, and the click-and-collect points
SESSION_PATH = "/api/customersessions/v2/sessions/active"
COLLECTION_POINTS_PATH = "/api/ecomdeliverydestinations/v4/delivery-addresses"
COLLECTION_METHOD = "CUSTOMER_COLLECTION"
VISITOR_HEADER = "visitor-id"
CUSTOMER_HEADER = "customer-id"

# the storefront's own "nuevo producto" filter, in the retailer filter group
# that every listing offers; on the whole-shop listing it selects the novelties
NEW_FILTER = MappingProxyType({"dummyValue": "new"})

UNIT_NAMES = MappingProxyType(
    {
        "fop.price.per.each": "EACH",
        "fop.price.per.kg": "PER_1KG",
        "fop.price.per.litre": "PER_LITRE",
        "fop.price.per.dozen": "PER_DOZEN",
    }
)
"""the unit name each unit-price message key stands for.

the promotions listing sends the message key alone; these are the pairs the
other listings send together.
"""

# the waf budget is per ip and refills over tens of minutes; a challenge
# suggests waiting this long before the next request
CHALLENGE_BACKOFF = 1800.0

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 300
MAX_CATEGORY_DEPTH = 4
DEFAULT_SUGGESTION_LIMIT = 10
