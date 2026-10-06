"""hosts, paths, and the static values the condis storefront is keyed on."""

from __future__ import annotations

# the storefront, a next.js app that renders product pages on the server
SITE_URL = "https://compraonline.condis.es"
# the empathy search index the storefront's own search box and listings call
EMPATHY_URL = "https://api.empathy.co/search/v1/query/condis"
# the image cdn, which serves a product image at a size: /fit-in/<size>/es/products/
CDN_URL = "https://cdn.condis.es"
# the largest size the storefront asks for, the product page's zoom
PHOTO_SIZE = "1920x1080"

LOCALE = "es_ES"
INDEX_LANGUAGE = "es"
# the storefront ignores a product url's slug, so the client sends this one
PLACEHOLDER_SLUG = "p"

# the picking centre an anonymous session prices and stocks from; the index
# answers an empty catalogue for a centre it does not know, and refuses a
# request without one
DEFAULT_PICKING_CENTRE = "718"

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 500
# the index refuses an offset of 2,495 or more
MAX_START = 2494

# the most requests one page may take: a request without a session goes to
# the anonymous sign-in, three requests, and back to the page, five in all
MAX_REDIRECTS = 6

# the server action behind the storefront's postcode picker, called by name;
# next.js publishes only a build-specific id for it, inside the page's scripts
POSTCODE_ACTION = "fetchPostalCodeById"
ACTION_HEADER = "Next-Action"
COMPONENT_ACCEPT = "text/x-component"
HTML_ACCEPT = "text/html,application/xhtml+xml"

# the facets that mark the storefront's novelties and its two kinds of offer
NOVELTY_FACET = "is_novelty"
SALE_FACET = "on_sale"
PROMOTION_FACET = "on_promotion"
CATEGORY_FACET = "filterCategory"
