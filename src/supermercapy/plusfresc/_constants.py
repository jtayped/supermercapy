API_URL = "https://wscompra.plusfresc.cat/api"
SITE_URL = "https://compra.plusfresc.cat"
IMAGE_URL = "https://compra.plusfresc.cat/ImatgesProductes"

DEFAULT_CENTER = 12
ROOT_CATEGORY = "Root"
NEW_ARRIVALS_CATEGORY = "40"
OFFERS_CATEGORY = "Oferta2"
HIGHLIGHTS_CATEGORY = "PromoHighlight"

# search answers with at most this many rows and no cursor of any kind, so a
# page of exactly this length is indistinguishable from a truncated one.
SEARCH_RESULT_LIMIT = 100
MINIMUM_QUERY_LENGTH = 2

# a locker id embeds its parent store: "127899871" carries this literal plus a
# trailing check character around the real center id.
LOCKER_INFIX = "789987"

# guest tokens live thirty minutes; mint a fresh one this long before the end.
TOKEN_TTL = 1800.0
TOKEN_REFRESH_MARGIN = 60.0
