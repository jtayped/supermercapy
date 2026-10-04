# matching products across stores

only mercadona, consum, carrefour and lidl publish barcodes, and mercadona only on a product page. so "is this the same product at another store?" mostly has to be answered from the name, the brand and the pack size. `supermercapy.match` answers it from the summaries a search already returned. it makes no request of its own.

it answers two questions, and keeps them apart because they want opposite things from a store brand.

| question | means | functions |
| --- | --- | --- |
| the same product | the same brand, the same product and the same pack, at another store | `score_same`, `find_same`, `pair_same`, `group_same` |
| a comparable alternative | the same kind of product under any brand, compared by `price.reference` | `score_alternative`, `find_alternatives` |

a hacendado semi-skimmed milk and a consum one are never the same product. as alternatives they are exactly what a shopper comparing prices per litre wants to see.

every answer is a `Match` with a `score` from 0 to 1 and the `reasons` that produced it, such as `("brand", "size", "name 0.87")`. pick your own threshold and show the reasons to whoever has to trust the answer. the defaults, `SAME_THRESHOLD = 0.8` and `ALTERNATIVE_THRESHOLD = 0.6`, live in `supermercapy.match`.

## the same product

`group_same()` takes `search_all(...).products` as it comes and splits it into groups of one product, at most one listing per store.

```python
from supermercapy import group_same, search_all

result = search_all("cola cao", page_size=10)

for group in group_same(result.products):
    if len(group.listings) < 2:
        continue
    print(group.score, group.reasons)
    for listing in group.listings:
        product = listing.product
        print(" ", listing.store, product.name, product.price.amount)
```

the search costs what `search_all()` costs, one request per store plus the bindings and warm-ups listed under [request counts](usage.md#request-counts). without a postcode that is 15 requests to thirteen stores, with mercadona skipped because it needs a postcode to bind a warehouse. grouping makes none.

every listing lands in exactly one group, and a listing that matched nothing is a group of its own with `score` set to `None`. two groups only join when every pair across them reaches the threshold, so one strong pair cannot drag in a listing that matches only half the group. listings that share a barcode stand in for each other there, so a product carrefour renamed for a promotion, `Cerveza Mahou 5 estrellas la velada del año`, still joins its group through consum's listing of the same barcode. a group's `score` and `reasons` are those of its weakest pair.

the other three functions cover the other shapes the question comes in.

```python
from supermercapy import Consum, Mercadona, find_same, pair_same, score_same

with Mercadona.from_postal_code("46001") as mercadona:
    basket = mercadona.search_products("leche semidesnatada", page_size=10).products
with Consum.from_postal_code("46001") as consum:
    page = consum.search_products("leche semidesnatada", page_size=20).products

match = score_same(("mercadona", basket[0]), ("consum", page[0]))
print(match.score, match.reasons)

found = find_same(("mercadona", basket[0]), [("consum", product) for product in page])
pairs = pair_same(basket, page)
```

that example makes four requests, two per store, one to bind the postcode and one to search. `score_same()` scores one pair. `find_same()` returns, best first, the best listing at each other store. `pair_same()` pairs two arbitrary lists one to one, in the left list's order, so no listing is used twice. every function takes a `Listing`, a `(store, summary)` pair as `search_all()` gives them, or a bare summary. a bare summary has no store, so the one-listing-per-store rule cannot apply to it.

## a comparable alternative

`find_alternatives()` takes a product and candidates from any store, its own included, and returns the ones of the same kind, cheapest first by `price.reference`. candidates without a unit price come last.

```python
from supermercapy import find_alternatives, search_all

result = search_all("leche semidesnatada", page_size=10)
store, milk = result.products[0]

for match in find_alternatives((store, milk), result.products):
    reference = match.right.product.price.reference
    print(match.right.store, match.right.product.name, reference, match.reasons)
```

that search costs the same 15 requests. brand and pack size do not count here. what counts is what the two names say the product is, once both brands are taken out, and whether the two unit prices are in the same unit. a pair whose prices are per litre and per piece cannot be compared, so its score is halved. a store brand gets the reason `store brand`, so an interface can label it.

## how a pair is scored

`score_same()` returns `1.0` with the single reason `ean` when both listings carry the same barcode. otherwise the score is half the name likeness, plus a quarter when the brands agree and a quarter when the pack sizes agree. a pair whose brand or size cannot be confirmed therefore stays below 0.8. a brand, a pack size, a barcode or a container that contradicts halves the score.

| reason | when |
| --- | --- |
| `ean` | both barcodes are equal; nothing else is checked |
| `ean differs` | both carry a barcode and they differ |
| `brand`, `brand differs`, `brand unknown` | the brands agree, disagree, or one side has none and its name does not say |
| `size`, `size differs`, `size unknown` | the pack sizes agree, disagree, or cannot be compared |
| `name 0.87` | how alike the names are |
| `container differs` | the two name different containers: can, bottle, brick, jar or squeeze bottle |
| `kind 0.87` | for an alternative, how alike the names are with no brand at all |
| `unit l`, `unit differs` | both unit prices are per litre, or in different units |
| `kind unknown` | one name says nothing once its brand is gone |
| `store brand` | the alternative is a store's own brand |

### barcode

a barcode counts when it is 8 to 14 digits with a valid check digit. codes a store prints for its own weighed items, 13 digits starting with 2 or 8 digits starting with 0 or 2, name nothing across stores and are ignored.

### brand

brands compare without case, accents, punctuation or a trailing `s.a.` or `s.l.`, and spelt apart or together, so `COLA CAO` and `COLACAO` agree. a brand that extends another agrees with it. `COCA COLA ZERO` agrees with `COCA COLA`, and the extra word stays in the name, where it has to match. `CENTRAL LECHERA ASTURIANA` agrees with `ASTURIANA`, and the maker's words go.

store brands are known by name: hacendado, deliplus, bosque verde and compy at mercadona, anything carrying consum, carrefour, bonàrea, dia, eroski, alcampo, auchan, alipende or condis, lidl's and aldi's house names, and eroski's brands at caprabo, which belongs to the same group. a store brand only ever agrees with itself, so `CARREFOUR BIO` is not `BIO`. when one listing has no brand, as at bonàrea, the other's brand is looked for in its name. when it is missing there and the other brand is a store brand of another chain, the brands differ, since a hacendado product is never on bonàrea's shelf.

### pack size

the size comes from the name and from `pack_size_text`, read with the same unit vocabulary as `price.reference`. multipacks such as `6 x 1 l`, `pack 2x2 l.`, `2 botellas de 2 l` and `paq. 3 u. de 200 ml` give a count and a total. counts such as `6 latas`, `4+2 rollos`, `pack-6` and `6 per paquet` give a count alone. `pack 2 unitats 4 l` is four litres in all, while carrefour's `pack 2 botellas 2 l` is two litres each, and the price decides which one a text means.

when the text says nothing, as at mercadona and consum, the size is read back from the price: 4.98 at 0.83 per litre is six litres. a unit price is rounded, so the size read back from it only counts while that rounding moves it by less than 5%. two totals agree within 2%, plus that rounding. two different counts always disagree.

### name

names are compared once the brand, the pack size, packaging words and stop words are gone, in spanish and catalan: `llet` reads `leche`, `oli` reads `aceite`, `sense` reads `sin`. words are cut to a rough stem so plurals and genders agree. a negation joins its word, so `sin lactosa` is one token. `zero`, `0% azúcar` and `sin azúcar añadido` all read `0`.

only the differences count. each word one name has and the other lacks adds a cost, and the likeness is `1 / (1 + cost)`.

| word | cost | example |
| --- | --- | --- |
| a descriptor | 0.15 | refresco, bebida, original, soluble, instantáneo, polvo, cacao, untar, papel, estilo, sabor |
| any other word | 0.7 | picual, hojiblanca, rubia, tostada |
| a number or a negation | 1.0 | 0, 5, sin lactosa, sin gluten |

with matching brand and size, one other word is enough to fall below 0.8, so `Aceite de oliva virgen extra picual Coosur` never matches the hojiblanca bottle. two descriptors are not, so `Cacao soluble Cola Cao` still matches `ColaCao` at the same size. a misspelt word of five letters or more, such as consum's `semidesntada`, costs only the share by which it differs.

## measured quality

the evaluation set is in `tests/fixtures/match/evaluation.json`, and `tests/test_match.py` asserts the figures below. it holds 1,343 listings from 14 live searches against postcode 08013 on 4 october 2026, hand-labelled into groups of the same product. pairs whose listings did not say enough to decide, such as plusfresc's `Crema d'avellanes NOCILLA, 180 g`, are labelled unsure and left out of the count.

the development set is the 10 searches the rules were written against: coca-cola 2 l, nocilla, colacao, aceite de oliva virgen extra 1 l, leche semidesnatada, atún claro en aceite, galletas maría, cerveza mahou lata, papel higiénico and detergente, across eleven stores. bonpreu answered the first six. the held-out set is 4 searches labelled before the matcher ever ran on them: yogur natural, mayonesa, galletas digestive and agua mineral 1,5 l, across thirteen stores.

at the default threshold of 0.8:

| set | labelled pairs | `score_same` precision | recall | `group_same` precision | recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| development | 392 | 1.00 | 0.73 | 1.00 | 0.72 |
| held out | 395 | 0.996 | 0.64 | 1.00 | 0.64 |

precision counts every cross-store pair scored at or above the threshold, and recall every labelled pair. for `group_same` they count the pairs that share a group.

the held-out figures are not entirely clean. the first run on that set scored 0.96 precision and 0.55 recall. all eight false positives were one alcampo listing, `HELLMANN"S Mayonesa frasco 440 +10 ml.`, which is the 450 ml jar with a promotional label and which the hand labels had wrongly kept apart. that listing is now labelled unsure. the rules then gained catalan words the held-out set showed missing, among them maionesa, ensucrat, civada and grec, the descriptors estilo and sabor, and jar and squeeze bottle as containers, and two bugs in brand removal and grouping were fixed. so read 0.96 and 0.55 as the honest estimate for a category the rules have never seen. the one false positive left is bonpreu's `CALVÉ Salsa fina`, which names no container and was matched with caprabo's squeeze bottle.

the threshold trades recall for precision, and 0.8 sits on a cliff. at 0.75 a single variant word such as `sin lactosa` is allowed through:

| threshold | development precision | recall | held-out precision | recall |
| ---: | ---: | ---: | ---: | ---: |
| 0.75 | 0.75 | 0.81 | 0.76 | 0.81 |
| 0.80 | 1.00 | 0.73 | 0.996 | 0.64 |
| 0.85 | 1.00 | 0.68 | 0.996 | 0.64 |
| 0.90 | 1.00 | 0.59 | 0.996 | 0.64 |

for alternatives, five development searches carry kind labels such as plain semi-skimmed milk, lactose-free milk, tuna in olive oil and tuna in sunflower oil. over every ordered pair of labelled listings, the default of 0.6 gives precision 0.998 and recall 0.60. that threshold lets names differ only by descriptors. at 0.55 one other word may differ, which gives precision 0.88 and recall 0.75. that suits a shopper who would take a picual oil for a plain extra virgen one.

## limits

- **store brands.** two store brands are never the same product, even when one factory fills both. the list of store brands is in the code, and a house name it does not know reads as an ordinary brand.
- **renamed products.** a promotional name such as carrefour's `la velada del año` matches only through a barcode. names one store spells its own way, such as nocilla's `1 sabor` against `original` or `2 sabores` against `dúo` and `chocoleche`, do not match, and that accounts for most of the missing recall.
- **multipacks and promotions.** a pack of two bottles is not one bottle, and `600 g + 33% gratis` is not 600 g. a promotional bundle whose size a store states differently is missed.
- **claims some stores add.** carrefour writes `sin gluten` on products other stores describe without it, and a negation costs too much to ignore, so those pairs are missed.
- **unit prices a store gets wrong.** plusfresc prices one twelve-roll pack at 3.99 per piece. a count in the text beats a piece count read back from the price, but a wrong total can still keep two listings apart.
- **bare summaries.** without a store, the one-listing-per-store rule does not apply and a store brand cannot be checked against its store.
- **vocabulary.** the spanish and catalan word lists are small and cover groceries. a category they miss, or another language, matches less often but no less precisely.
