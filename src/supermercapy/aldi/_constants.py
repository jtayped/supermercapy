"""hosts, the public search key, and the index names the aldi site reads."""

from __future__ import annotations

SITE_URL = "https://www.aldi.es"
ALGOLIA_URL = "https://l9knu74io7-dsn.algolia.net"
ALGOLIA_APP_ID = "L9KNU74IO7"
# the search-only key the site's own category and search pages send, in plain
# text in their next.js bundle (/_next/static/chunks/pages/product-overview/
# [...categories]-<hash>.js on 2026-10-04, unchanged since the reconnaissance).
# it identifies the public storefront, not a person, and can only search and
# read records. pinned rather than read from the bundle at run time: that would
# cost two more requests per client and depend on a hashed file name and
# minified code, which change with every deploy, while the key has not. a
# rotated key fails loudly with AuthenticationError, and the live tests check
# the bundle still carries this one.
ALGOLIA_API_KEY = "83df5acd172c42ab174afa4583232b5d"
INDEX_TEMPLATE = "an_prd_es_es_{region}_products2"
MAX_PAGE_SIZE = 1000
