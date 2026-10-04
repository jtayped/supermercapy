# command line

installing supermercapy also installs a `supermercapy` command, and `python -m supermercapy` runs the same program.

```bash
supermercapy --version
supermercapy --help
supermercapy search --help
```

tables go to standard output and fit the terminal width. notes about a store, such as why it could not answer, go to standard error one line each, so a pipe carries the table alone. a name too long for its column ends in `…`. prices appear exactly as the storefront published them, with no rounding and no currency sign, and every store prices in euros. the unit price column shows the normalised `price.reference`, per `kg`, `l`, `piece`, `dose`, `m` or `m2`, so rows compare across stores. when a store states a unit price the client cannot normalise, the column shows the store's own figure.

every command takes `--json`, which swaps the table for one json document. see [json output](#json-output).

the examples below are real runs from 4 october 2026 in an 80-column terminal. the products and prices will have changed since.

## stores

`supermercapy stores` lists every store with its client class, the constructor argument it binds through, and the capabilities it declares. it makes no request.

```text
$ supermercapy stores
store      class      binds to                      capabilities
mercadona  Mercadona  warehouse (required)          postal_code, stores,
                                                    catalog, new_arrivals, home,
                                                    ean, nutrition
consum     Consum     zone (optional)               postal_code, stores,
                                                    catalog, ean_lookup,
                                                    new_arrivals, offers, ean,
                                                    promotions
plusfresc  Plusfresc  center (default 12)           postal_code, stores,
                                                    catalog, new_arrivals,
                                                    offers, nutrition,
                                                    promotions
bonarea    Bonarea    nothing                       catalog, nutrition
carrefour  Carrefour  sale point (optional)         postal_code, stores,
                                                    ean_lookup, home, ean,
                                                    nutrition, promotions
lidl       Lidl       region (optional)             postal_code, stores,
                                                    catalog, offers, ean,
                                                    promotions, future_prices
bonpreu    Bonpreu    nothing                       new_arrivals, offers,
                                                    nutrition, promotions
dia        Dia        postal code (optional)        postal_code, catalog,
                                                    new_arrivals, offers,
                                                    nutrition, promotions
eroski     Eroski     nothing                       catalog, nutrition,
                                                    promotions
caprabo    Caprabo    nothing                       catalog, nutrition,
                                                    promotions
aldi       Aldi       region (default pen)          postal_code, catalog,
                                                    offers, promotions,
                                                    future_prices
ahorramas  Ahorramas  nothing                       catalog, offers, promotions
alcampo    Alcampo    store (optional)              stores, new_arrivals,
                                                    offers, nutrition,
                                                    promotions
condis     Condis     picking centre (default 718)  postal_code, catalog,
                                                    new_arrivals, offers,
                                                    nutrition, promotions
```

a store whose binding is required cannot answer anything until `--postal-code` gives it one. today that is mercadona alone.

## search

`supermercapy search QUERY` searches every store in parallel through [`search_all()`](usage.md#search-several-stores-at-once) and prints up to five products from each. `--store NAME` searches only the named store, and repeating it adds more, in the order given. `--limit N` asks each store for `N` products, capped at the most that store accepts. `--postal-code` binds every store that declares `POSTAL_CODE` before it searches, and the rest ignore it.

```text
$ supermercapy search leche --postal-code 08013 --limit 3
store      id         name                                     price  unit price
mercadona  10381      Leche semidesnatada Hacendado             4.98  0.83/l
mercadona  10379      Leche entera Hacendado                    5.76  0.96/l
mercadona  10721      Leche semidesnatada sin lactosa Hacend…   5.64  0.94/l
consum     7080604    Leche Semidesnatada Brik                  0.84  0.84/l
consum     7080596    Leche Entera Brik                         0.96  0.96/l
consum     5583190    Leche Semidesnatada Brik                  1.25  1.25/l
plusfresc  000426     Llet semidesnatada ASTURIANA, 1 l         1.17  1.17/l
plusfresc  000428     Llet desnatada ASTURIANA, 1 l             1.13  1.13/l
plusfresc  000857     Formatge de vaca PYRÉNÉE llet crua MOU…  27.99  27.99/kg
bonarea    13*0005    Leche desnatada paq. de 6 brics           5.22  0.87/l
bonarea    13*0020    Leche semidesnatada Calcio paq. de 6 b…    7.2  1.20/l
bonarea    13*0004    Leche semidesnatada paq. de 6 brics       5.34  0.89/l
carrefour  521007071  Leche semidesnatada Carrefour brik 1 l.   0.84  0.84/l
carrefour  521006992  Leche entera Carrefour brik 1 l.          0.96  0.96/l
carrefour  714713105  Leche semidesnatada Carrefour sin lact…   0.94  0.94/l
lidl       100408539  Espumador de leche »SMSP 500 A1«         17.99
lidl       100411764  Espumador de leche                       36.99
lidl       100411470  Cafetera »CAFFETTIERA«                   14.99
bonpreu    49637      DOLCE GUSTO Càpsules de cafè Espresso …   8.55  0.28/piece
bonpreu    49644      DOLCE GUSTO Càpsules de cafè amb llet     8.55  0.28/piece
bonpreu    25833      STARBUCKS Càpsules de cafè Espresso Ro…   3.99  0.40/piece
dia        504P6      Leche semidesnatada Dia Láctea pack 6 …   5.04  0.84/l
dia        608P6      Leche entera Dia Láctea pack 6 x 1 L      5.76  0.96/l
dia        130063P6   Leche semidesnatada sin lactosa Dia Lá…   5.34  0.89/l
eroski     18672295   Leche entera del País Vasco EROSKI, br…   1.15
eroski     18672311   Leche semidesnatada del PaÍs Vasco ERO…   1.06
eroski     26084350   Leche entera BOMILK, brik 1 litro         0.96
caprabo    18581678   Leche semidesnatada de Cataluña EROSKI…   0.99
caprabo    735399     Leche semidesnatada ASTURIANA, brik 1 …   1.09
caprabo    13150131   Leche semidesnatada sin lactosa PASCUA…   1.29
aldi       872200     Leche entera sin lactosa                  1.03
aldi       828100     Leche evaporada                           0.92  2.71/kg
aldi       82000      Leche con calcio semidesnatada            1.05
ahorramas  70865      Leche Alipende 1l semidesnatada           0.83  0.83/l
ahorramas  70864      Leche Alipende 1l entera                  0.96  0.96/l
ahorramas  70866      Leche Alipende 1l desnatada               0.81  0.81/l
alcampo    54180      AUCHAN Leche semidesnatada de vaca 6 x…   5.28  0.88/l
alcampo    54178      AUCHAN Leche entera de vaca 6 x 1 l Pr…   5.76  0.96/l
alcampo    99193      L.R. Leche entera 6 x 1 l.                5.70  0.95/l
condis     704049     LECHE CONDIS ENTERA 1 L                   0.99  0.99/l
condis     704048     LECHE CONDIS SEMIDESNATADA 1 L            0.87  0.87/l
condis     704005     LECHE ATO ENTERA BRIK 1 L                 1.28  1.28/l
```

that run made 45 requests to the fourteen stores. each store spends one on the search itself, and the rest go to postcode bindings and session warm-ups, 19 of them at condis. [request counts](usage.md#request-counts) gives each store's share. without `--postal-code` it makes 15 and skips mercadona, which notes why on standard error:

```text
mercadona: skipped: needs a postal code to bind a warehouse
```

a store that does not serve the postcode notes `out of coverage`, and one with nothing to show notes `no results`. neither counts as a failure. naming mercadona with `--store` and no `--postal-code` is a usage error, because it could never answer.

## compare

`supermercapy compare QUERY` runs the same search as `search`, then uses [`group_same()`](matching.md#the-same-product) to line up the listings that are the same brand, product and pack at different stores. each group is one block, cheapest store first, and the command prints only groups that reach two stores. it takes the same `--store` and `--postal-code` flags as `search`, asks each store for 10 products unless `--limit` says otherwise, and takes `--threshold` from 0 to 1, 0.8 by default. the `score` column is the group's weakest pair.

```text
$ supermercapy compare "cola cao"
group  score  store      id                name                price  unit price
1      0.81   bonarea    13*0092606        Cacao intantáneo …   4.35  11.36/kg
              alcampo    16806             COLACAO Cacao en …   4.74  12.38/kg
              carrefour  530913230         Cacao soluble Col…   4.95  12.9243/kg
              consum     7360985           Cacao Soluble        4.99  13.03/kg
              plusfresc  002510            Cacau en pols COL…   4.99  13.02/kg
              bonpreu    00633             COLACAO Cacau sol…   4.99  13.03/kg
              ahorramas  60035             Cacao Colacao 383g   4.99  13.03/kg
              condis     206070            CACAO COLACAO EN …   4.99  13.03/kg
              dia        64150             ColaCao 383 g           5  13.05/kg
              eroski     23666878          Cacao soluble ori…   5.00  13.05/kg
              caprabo    23666878          Cacao soluble ori…   5.00  13.05/kg
2      0.81   alcampo    16919             COLACAO Cacao en …   6.55  8.62/kg
              bonarea    13*0091234        Cacao instantáneo…   6.75  8.77/kg
              consum     7331569           Cacao Soluble        6.99  9.20/kg
              plusfresc  002511            Cacau en pols COL…   6.99  9.19/kg
              bonpreu    04215             COLACAO Cacau sol…   6.99  9.20/kg
              ahorramas  59329             Cacao Colacao 760g   6.99  9.20/kg
              condis     206071            CACAO COLACAO EN …   6.99  9.20/kg
              dia        161437            ColaCao 760 g           7  9.21/kg
              eroski     23666886          Cacao soluble COL…   7.00  9.21/kg
              caprabo    23666886          Cacao soluble COL…   7.00  9.21/kg
```

those are the first two of fifteen groups. that run made 15 requests, as many as the same `search` makes without a postcode, and grouping added none. besides the usual store notes, standard error says how many listings matched nothing:

```text
mercadona: skipped: needs a postal code to bind a warehouse
aldi: no results
49 of 119 products matched no other store
```

`compare` leaves out a product the stores name too differently to match, instead of guessing. [matching](matching.md) explains how pairs are scored and what precision and recall to expect.

## product

`supermercapy product STORE ID` prints one product, one field per line. the id is the one `search` prints. fields the store does not publish are left out, and `--json` prints the whole record, nutrition and photos included.

```text
$ supermercapy product mercadona 10381 --postal-code 08013
store       mercadona, warehouse bcn1
id          10381
name        Leche semidesnatada Hacendado
brand       Hacendado
ean         8402001002090
price       4.98 (was 5.04)
unit price  0.83/l
available   yes
category    Huevos, leche y mantequilla
url         https://tienda.mercadona.es/product/10381/leche-semidesnatada-hacendado-pack-6
```

that cost two requests, one to bind the postcode and one for the product. without `--postal-code`, a product costs one request at most stores. plusfresc fetches a guest token first, carrefour reads a warm-up page and follows one redirect, and condis follows the four redirects of its anonymous sign-in, so they spend two, three and five.

## categories

`supermercapy categories STORE` prints the tree the store's `get_categories()` returns, one category per line, each child indented under its parent. how deep it goes is up to the store. carrefour's menu returns its departments alone:

```text
$ supermercapy categories carrefour
id           name
cat20968591  Ofertas
cat20002     Frescos
cat20001     La Despensa
cat20003     Bebidas
cat20005     Droguería y limpieza
cat20004     Cuidado personal e Higiene
cat21449123  Congelados
cat20006     Bebé
cat20007     Mascotas
cat20008     Parafarmacia
```

that is one request. every other store's tree costs one too, except at condis, which reads it from a page behind its anonymous sign-in and spends five on a new client. `--postal-code` adds the binding.

## ean

`supermercapy ean EAN` looks up a barcode of 8 to 14 digits in every store that declares `EAN_LOOKUP`, consum and carrefour today, in parallel.

```text
$ supermercapy ean 8431876011937
store      id         name                                     price  unit price
carrefour  521007071  Leche semidesnatada Carrefour brik 1 l.   0.84  0.84/l
consum: not found
```

that lookup made five requests. consum filters its listing on the barcode in one. carrefour spent four, because it matches the barcode in its search index and then reads the product page after a warm-up page and a redirect. a store that does not carry the barcode notes `not found`, which is not a failure.

## json output

with `--json`, a command prints exactly one json document on standard output, and the notes on standard error stay as they are. decimals are strings, so `"0.84"` keeps the published figure. [saving results](usage.md#saving-results) covers the conversion.

| command | document |
| --- | --- |
| `stores` | a list with one object per store: `store`, `class`, `binding`, `binding_required`, and `capabilities` |
| `search` | the `SearchAllResult` that `search_all()` returned |
| `compare` | an object with `query`, `postal_code`, `threshold`, `groups`, `unmatched`, and `stores`: each group has its `listings`, `score`, and `reasons`, each unmatched entry is a `store` and its `product`, and each store entry is a search entry without its `result` |
| `product` | the `Product` |
| `categories` | the list of root categories, children nested inside each |
| `ean` | an object with `ean`, `postal_code`, and `stores`, one entry per store |

each `ean` entry has `store`, `status`, `store_id`, `product`, `reason`, and `error_type`, the same fields as a search entry with `product` in place of `result`. its status is `ok`, `not_found`, `skipped`, `out_of_coverage`, or `failed`.

```bash
supermercapy search leche --store consum --json | jq -r '.stores[0].result.products[].name'
```

## exit codes

| code | meaning |
| --- | --- |
| `0` | every store asked answered, or was skipped, out of coverage, or without a match |
| `1` | a store failed, and whatever the other stores found is still printed |
| `2` | a usage error, such as a missing argument, an invalid postcode, or a store that needs `--postal-code` |
| `130` | interrupted with ctrl-c |

a failure is one line on standard error, naming the store, the exception class, and its message, with no traceback:

```text
lidl: TransportError: GET https://www.lidl.es/q/api/search returned HTTP 503
```
