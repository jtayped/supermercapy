"""the host, controller paths and fixed category ids of the ahorramás storefront."""

SITE_URL = "https://www.ahorramas.com"

# salesforce commerce cloud addresses its controllers by site and locale; the
# shop runs one site in one locale
CONTROLLER_PATH = "/on/demandware.store/Sites-Ahorramas-Site/es"
GRID_PATH = f"{CONTROLLER_PATH}/Search-UpdateGrid"
PRODUCT_PATH = f"{CONTROLLER_PATH}/Product-Variation"

# the catalogue's root, which every product sits under, and the offers branch
# of the menu
ROOT_CATEGORY = "root"
OFFERS_CATEGORY = "ofertas"
