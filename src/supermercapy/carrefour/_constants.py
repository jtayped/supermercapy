from types import MappingProxyType

# the search index carrefour's own storefront calls, reachable directly and
# unauthenticated. this is the only path that honours a per-request store.
EMPATHY_URL = "https://api.empathy.co/search/v1/query/carrefour"

SITE_URL = "https://www.carrefour.es"
STATIC_URL = "https://static.carrefour.es"
SUPERMARKET_PATH = "/supermercado"

SALEPOINTS_URL = f"{SITE_URL}/cloud-api/salepoints/v1"
PROXY_SEARCH_URL = f"{SITE_URL}/search-api/query/v1/search"

# the category menu the storefront's own navigation calls. it answers one
# node's children per request and stops at the aisles: a leaf category is
# not in it at all, so a leaf is read from its listing page instead.
MENU_URL = f"{SITE_URL}/cloud-api/categories-api/v1/categories/menu"
FOOD_ROOT = "foodRootCategory"

# a category page answers any slug as long as there is one; with none it
# redirects home, exactly like an unknown id
CATEGORY_PATH = f"{SUPERMARKET_PATH}/c/{{category_id}}/c"

DEFAULT_CATALOG = "food"
CATALOGS = frozenset({"food", "cellar", "nonfood"})

# the cloudflare gate in front of www.carrefour.es is a user-agent check: any
# string that parses as a real browser passes, and curl, python-httpx and a
# bare "Mozilla/5.0" do not. the tls fingerprint is not inspected.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# __cf_bm lives thirty minutes from issue; warm a new session five minutes
# early so a long walk never trips the challenge halfway through a page.
PRIME_TTL = 1500.0

# the direct index refuses rows above this and start above the other, so one
# query reaches at most 2,998 documents.
MAX_ROWS = 500
MAX_START = 2498

# the first-party proxy answers a missing session parameter with a waf block
# rather than an api error, and its value is never read.
PROXY_SESSION = "empathy"
PROXY_MAX_ROWS = 48

STATE_MARKER = "window.__INITIAL_STATE__"

# the two bodies a 403 can carry, lowercased for comparison
BLOCKED_MARKER = "you have been blocked"
CHALLENGE_MARKER = "just a moment"

PRODUCT_SLUG_PATTERN = r"/([^/]+)/R-[^/]+/p"
CATEGORY_URL_PATTERN = r"/supermercado/((?:[^/]+/)*?)(cat\d+)/c"

ACCEPT_LANGUAGES = MappingProxyType(
    {
        "es": "es-ES,es;q=0.9",
        "ca": "ca-ES,ca;q=0.9",
        "en": "en-US,en;q=0.9",
    }
)

DOCUMENT_HEADERS = MappingProxyType(
    {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Site": "none",
    }
)
XHR_HEADERS = MappingProxyType(
    {
        "Accept": "application/json",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Site": "same-origin",
        "Referer": f"{SITE_URL}{SUPERMARKET_PATH}",
    }
)
