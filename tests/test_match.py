"""cross-store matching: the scores, the list helpers, and their measured quality."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from supermercapy import (
    ConfigurationError,
    Listing,
    Match,
    MatchKind,
    Price,
    ProductGroup,
    ProductSummary,
    Unit,
    UnitPrice,
    find_alternatives,
    find_same,
    group_same,
    pair_same,
    score_alternative,
    score_same,
    to_dict,
    to_json,
)
from supermercapy import match as matching
from supermercapy._core.units import Quantity
from supermercapy._match_text import (
    Size,
    SizeText,
    brand_words,
    gtin,
    implied_total,
    likeness,
    owners,
    pack_size,
    read_size,
    same_size,
    tokens,
)

EVALUATION = Path(__file__).parent / "fixtures" / "match" / "evaluation.json"


def item(
    name: str,
    *,
    brand: str | None = None,
    ean: str | None = None,
    pack: str | None = None,
    price: str | None = None,
    reference: str | None = None,
    id: str = "1",
    variable: bool = False,
) -> ProductSummary:
    """build a summary the way a store's parser would."""

    unit_price = None
    if reference is not None:
        amount, unit = reference.split("/")
        unit_price = UnitPrice(amount=Decimal(amount), unit=Unit(unit))
    return ProductSummary(
        id=id,
        name=name,
        brand=brand,
        ean=ean,
        pack_size_text=pack,
        price=Price(
            amount=None if price is None else Decimal(price), reference=unit_price
        ),
        is_variable_weight=variable,
    )


# ---------------------------------------------------------------- measured


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp)

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn)


def _searches(split: str) -> list[dict[str, Any]]:
    document = json.loads(EVALUATION.read_text(encoding="utf-8"))
    columns = document["columns"]
    searches = []
    for search in document["searches"]:
        if search["split"] != split:
            continue
        listings = []
        for row in search["products"]:
            fields = dict(zip(columns, row, strict=True))
            listings.append(
                Listing(
                    store=fields["store"],
                    product=item(
                        fields["name"],
                        id=fields["id"],
                        brand=fields["brand"],
                        ean=fields["ean"],
                        pack=fields["pack_size_text"],
                        price=fields["price"],
                        reference=fields["reference"],
                    ),
                )
            )
        searches.append({**search, "listings": listings})
    return searches


def _key(listing: Listing) -> str:
    return f"{listing.store}:{listing.product.id}"


def _truth(search: dict[str, Any]) -> Any:
    cluster = {key: n for n, group in enumerate(search["same"]) for key in group}
    unsure = {
        frozenset(pair)
        for group in search["unsure"]
        for pair in itertools.combinations(group, 2)
    }

    def truth(a: str, b: str) -> bool | None:
        if a in cluster and cluster[a] == cluster.get(b):
            return True
        return None if frozenset((a, b)) in unsure else False

    return truth


def evaluate_same(split: str) -> tuple[Counts, Counts]:
    """count every cross-store pair, then every pair group_same put together."""

    pairs, groups = Counts(), Counts()
    for search in _searches(split):
        truth = _truth(search)
        listings = search["listings"]
        read = [(listing, matching._features(listing)) for listing in listings]
        for (a, a_features), (b, b_features) in itertools.combinations(read, 2):
            if a.store == b.store:
                continue
            expected = truth(_key(a), _key(b))
            if expected is None:
                continue
            found = matching._same(a, a_features, b, b_features).score
            if found >= matching.SAME_THRESHOLD:
                pairs.tp += expected
                pairs.fp += not expected
            else:
                pairs.fn += expected
        together = {
            frozenset((_key(a), _key(b)))
            for group in group_same(listings)
            for a, b in itertools.combinations(group.listings, 2)
        }
        for pair in together:
            expected = truth(*pair)
            if expected is not None:
                groups.tp += expected
                groups.fp += not expected
        groups.fn += sum(
            frozenset((a, b)) not in together
            for group in search["same"]
            for a, b in itertools.combinations(group, 2)
            if a.split(":")[0] != b.split(":")[0]
        )
    return pairs, groups


def evaluate_alternatives() -> Counts:
    """count, for every labelled listing, which others it takes as alternatives."""

    counts = Counts()
    for search in _searches("development"):
        kinds = {
            key: name for name, keys in search.get("kinds", {}).items() for key in keys
        }
        unsure = set(search.get("kinds_unsure", ()))
        read = [(x, matching._features(x)) for x in search["listings"]]
        for (a, a_features), (b, b_features) in itertools.permutations(read, 2):
            if _key(a) not in kinds or _key(b) in unsure:
                continue
            expected = kinds.get(_key(b)) == kinds[_key(a)]
            found = matching._alternative(a, a_features, b, b_features).score
            if found >= matching.ALTERNATIVE_THRESHOLD:
                counts.tp += expected
                counts.fp += not expected
            else:
                counts.fn += expected
    return counts


def test_the_evaluation_set_is_labelled_consistently() -> None:
    document = json.loads(EVALUATION.read_text(encoding="utf-8"))
    for search in document["searches"]:
        keys = {f"{row[0]}:{row[1]}" for row in search["products"]}
        labelled = [
            key
            for table in ("same", "unsure")
            for group in search[table]
            for key in group
        ]
        labelled += [key for keys_ in search.get("kinds", {}).values() for key in keys_]
        assert set(labelled) <= keys, search["query"]
        same = [key for group in search["same"] for key in group]
        assert len(same) == len(set(same)), search["query"]


def test_same_product_on_the_development_set() -> None:
    pairs, groups = evaluate_same("development")

    assert (pairs.tp, pairs.fp, pairs.fn) == (285, 0, 107)
    assert pairs.precision == 1.0
    assert round(pairs.recall, 2) == 0.73
    assert (groups.tp, groups.fp, groups.fn) == (283, 0, 109)
    assert round(groups.recall, 2) == 0.72


def test_same_product_on_the_held_out_set() -> None:
    pairs, groups = evaluate_same("held_out")

    assert (pairs.tp, pairs.fp, pairs.fn) == (254, 1, 141)
    assert round(pairs.precision, 3) == 0.996
    assert round(pairs.recall, 2) == 0.64
    assert (groups.tp, groups.fp, groups.fn) == (251, 0, 144)
    assert round(groups.recall, 2) == 0.64


def test_alternatives_on_the_development_set() -> None:
    counts = evaluate_alternatives()

    assert (counts.tp, counts.fp, counts.fn) == (7902, 12, 5148)
    assert round(counts.precision, 3) == 0.998
    assert round(counts.recall, 2) == 0.61


# ------------------------------------------------------------- same product


def test_equal_barcodes_settle_it() -> None:
    a = ("consum", item("Cerveza Lata 5 Estrellas", brand="MAHOU", ean="8411327122016"))
    b = (
        "carrefour",
        item(
            "Cerveza Mahou 5 estrellas la velada del año lata 33 cl.",
            ean="8411327122016",
        ),
    )

    match = score_same(a, b)

    assert (match.kind, match.score, match.reasons) == (MatchKind.SAME, 1.0, ("ean",))
    assert match.left == Listing(store="consum", product=a[1])


def test_different_barcodes_contradict() -> None:
    a = item("Coca Cola botella 2 l.", brand="COCA COLA", ean="5449000009067")
    b = item("Coca Cola botella 2 l.", brand="COCA COLA", ean="5449000131843")

    match = score_same(a, b)

    assert match.reasons == ("ean differs", "brand", "size", "name 1.00")
    assert match.score == 0.5


def test_brand_size_and_name_make_the_score() -> None:
    a = (
        "dia",
        item("Coca-Cola 2 L", brand="Coca-Cola", price="2.69", reference="1.35/l"),
    )
    b = (
        "consum",
        item(
            "Refresco Cola Botella", brand="COCA-COLA", price="2.52", reference="1.26/l"
        ),
    )

    match = score_same(a, b)

    # "refresco" is a descriptor and "cola" belongs to the shared brand
    assert match.reasons == ("brand", "size", "name 0.87")
    assert match.score == 0.93


def test_a_variant_word_keeps_two_products_apart() -> None:
    plain = item("Leche semidesnatada ASTURIANA, brik 1 litro", brand="ASTURIANA")
    lactose_free = item(
        "Leche semidesnatada sin lactosa ASTURIANA, brik 1 litro", brand="ASTURIANA"
    )
    picual = item(
        "Aceite oliva virgen extra Picual COOSUR, botella 1 litro", brand="COOSUR"
    )
    hojiblanca = item(
        "Aceite de oliva virgen extra hojiblanca Coosur 1 l.", brand="COOSUR"
    )

    assert score_same(plain, lactose_free).score == 0.75
    assert score_same(picual, hojiblanca).reasons == ("brand", "size", "name 0.42")


@pytest.mark.parametrize(
    ("a", "b", "reason"),
    [
        # the same brand, spelt apart, together, or with a legal suffix
        ("COLA CAO", "COLACAO", "brand"),
        ("Gallina Blanca S.A.", "GALLINA BLANCA", "brand"),
        # an extension names a variant, a prefix names the maker
        ("COCA COLA", "COCA COLA ZERO", "brand"),
        ("ASTURIANA", "CENTRAL LECHERA ASTURIANA", "brand"),
        # a store brand extends only itself
        ("BIO", "CARREFOUR BIO", "brand differs"),
        ("PASCUAL", "PULEVA", "brand differs"),
    ],
)
def test_brands_compare_by_their_words(a: str, b: str, reason: str) -> None:
    match = score_same(item("Leche 1 l", brand=a), item("Leche 1 l", brand=b))

    assert match.reasons[0] == reason


def test_a_brand_missing_from_one_side_is_read_from_its_name() -> None:
    bonarea = ("bonarea", item("Crema cacao 1 sabor Nocilla Original", pack="360 g"))
    eroski = (
        "eroski",
        item("Crema de cacao 1 sabor NOCILLA, vaso 360 g", brand="NOCILLA"),
    )
    own = ("bonarea", item("Leche semidesnatada paq. de 6 brics", pack="6 l"))
    hacendado = (
        "mercadona",
        item(
            "Leche semidesnatada Hacendado",
            brand="Hacendado",
            price="4.98",
            reference="0.83/l",
        ),
    )
    asturiana = (
        "dia",
        item("Leche semidesnatada Asturiana pack 6 x 1 L", brand="Asturiana"),
    )

    assert score_same(bonarea, eroski).reasons[0] == "brand"
    # hacendado is sold at mercadona alone
    assert score_same(own, hacendado).reasons[0] == "brand differs"
    assert score_same(own, asturiana).reasons[0] == "brand unknown"
    assert (
        score_same(own, ("lidl", item("Leche 6 x 1 l"))).reasons[0] == "brand unknown"
    )


def test_pack_sizes_come_from_text_or_from_the_unit_price() -> None:
    mercadona = item(
        "Refresco Coca-Cola",
        brand="Coca-Cola",
        pack="Pack-2",
        price="4.54",
        reference="1.135/l",
    )
    carrefour = item(
        "Coca Cola pack de 2 botellas de 2 l.", brand="COCA COLA", pack="pack 2x2 l."
    )
    single = item("Coca-Cola 2 L", brand="Coca-Cola")
    unknown = item("Coca-Cola", brand="Coca-Cola")

    assert score_same(mercadona, carrefour).reasons[:2] == ("brand", "size")
    assert score_same(single, carrefour).reasons[1] == "size differs"
    assert score_same(single, unknown).reasons[1] == "size unknown"


def test_a_pack_count_keeps_its_container() -> None:
    cans = item(
        "Refresco Coca-Cola Zero Azúcar 33cl Pack lata 6",
        brand="COCA-COLA",
        price="5.22",
        reference="2.64/l",
    )
    bottle = item(
        "Refresco Cola Zero Botella",
        brand="COCA-COLA",
        price="2.15",
        reference="1.08/l",
    )

    match = score_same(("ahorramas", cans), ("consum", bottle))

    assert "container differs" in match.reasons


def test_a_stated_size_without_a_count_is_one_pack() -> None:
    cans = item(
        "Refresco de naranja FANTA ZERO, pack 6x33 cl",
        brand="FANTA",
        price="4.85",
        reference="2.45/l",
    )
    bottle = item("FANTA ZERO NARANJA 2 L", brand="FANTA", price="1.85")
    # the size is read back from the price, so it may be a bottle or a pack
    terse = item(
        "Refresco Naranja Zero Botella", brand="FANTA", price="1.59", reference="0.80/l"
    )

    assert score_same(("caprabo", cans), ("condis", bottle)).reasons[1] == (
        "size differs"
    )
    assert score_same(("consum", terse), ("condis", bottle)).reasons[1] == "size"


def test_a_container_that_differs_contradicts() -> None:
    can = item("Cerveza 0,0 tostada MAHOU, lata 33 cl", brand="MAHOU")
    bottle = item(
        "Cerveza 0,0 tostada Mahou",
        brand="Mahou",
        pack="Botellín",
        price="1.15",
        reference="3.485/l",
    )

    match = score_same(can, bottle)

    assert match.reasons == ("brand", "size", "name 1.00", "container differs")
    assert match.score == 0.5


def test_sugar_claims_and_catalan_read_alike() -> None:
    a = item("COLACAO Cacau soluble 0% sucres afegits", brand="COLACAO", pack="0.325kg")
    b = item("Cacao soluble Cola Cao Cero sin azúcar añadido 325 g.", brand="COLA CAO")
    c = item(
        "Cacao soluble cero ColaCao",
        brand="ColaCao",
        price="5.40",
        reference="16.616/kg",
    )

    assert score_same(a, b).score == 1.0
    assert score_same(a, c).score == 1.0


# ------------------------------------------------------------ alternatives


def test_an_alternative_ignores_brand_and_size() -> None:
    hacendado = (
        "mercadona",
        item("Leche semidesnatada Hacendado", brand="Hacendado", reference="0.83/l"),
    )
    asturiana = (
        "consum",
        item("Leche Semidesnatada Brik", brand="ASTURIANA", reference="1.17/l"),
    )
    consum = (
        "consum",
        item("Leche Semidesnatada Brik", brand="CONSUM", reference="0.84/l"),
    )

    assert score_alternative(asturiana, hacendado).reasons == (
        "kind 1.00",
        "unit l",
        "store brand",
    )
    assert score_alternative(hacendado, consum).score == 1.0
    assert score_alternative(hacendado, asturiana).reasons == ("kind 1.00", "unit l")


def test_an_alternative_needs_a_comparable_unit_and_a_kind() -> None:
    litre = item("Leche entera", reference="0.96/l")
    piece = item("Leche entera", reference="0.20/piece")
    brand_only = item("ColaCao 383 g", brand="ColaCao")

    assert score_alternative(litre, piece).reasons == ("kind 1.00", "unit differs")
    assert score_alternative(litre, piece).score == 0.5
    assert score_alternative(litre, brand_only).reasons == ("kind unknown",)
    assert score_alternative(litre, brand_only).score == 0.0


# ------------------------------------------------------------------- lists

MILK = [
    (
        "eroski",
        item(
            "Leche semidesnatada ASTURIANA, brik 1 litro",
            id="735399",
            brand="ASTURIANA",
        ),
    ),
    (
        "caprabo",
        item(
            "Leche semidesnatada ASTURIANA, brik 1 litro",
            id="735399",
            brand="ASTURIANA",
        ),
    ),
    (
        "consum",
        item(
            "Leche Semidesnatada Brik",
            id="735399",
            brand="ASTURIANA",
            ean="8410297012150",
            price="1.17",
            reference="1.17/l",
        ),
    ),
    (
        "carrefour",
        item(
            "Leche semidesnatada Central Lechera Asturiana brik 1 l.",
            id="521007075",
            brand="CENTRAL LECHERA ASTURIANA",
            ean="8410297012150",
            price="1.09",
            reference="1.09/l",
        ),
    ),
    (
        "eroski",
        item(
            "Leche semidesnatada sin lactosa ASTURIANA, brik 1 litro",
            id="15617756",
            brand="ASTURIANA",
        ),
    ),
    (
        "mercadona",
        item(
            "Leche semidesnatada Hacendado",
            id="10382",
            brand="Hacendado",
            pack="Brik",
            price="0.83",
            reference="0.83/l",
        ),
    ),
    (
        "dia",
        item(
            "Leche semidesnatada Dia Láctea 1 L",
            id="504",
            brand="Dia Láctea",
            price="0.84",
            reference="0.84/l",
        ),
    ),
]


def test_find_same_keeps_the_best_listing_of_each_other_store() -> None:
    found = find_same(MILK[0], MILK)

    assert [(m.right.store, m.right.product.id) for m in found] == [
        ("caprabo", "735399"),
        ("consum", "735399"),
        ("carrefour", "521007075"),
    ]
    assert all(m.left.store == "eroski" for m in found)


def test_find_same_takes_bare_summaries() -> None:
    target = MILK[0][1]
    others = [summary for _, summary in MILK[1:]]

    found = find_same(target, others, threshold=0.9)

    assert [m.right.product.id for m in found] == ["735399", "735399", "521007075"]
    assert found[0].left.store is None


def test_find_alternatives_lists_the_cheapest_first() -> None:
    target = MILK[2]
    candidates = [*MILK, ("aldi", item("Leche semidesnatada", id="9", brand="MILSANI"))]

    found = find_alternatives(target, candidates)

    assert [(m.right.store, m.right.product.id) for m in found] == [
        ("mercadona", "10382"),
        ("dia", "504"),
        ("carrefour", "521007075"),
        ("eroski", "735399"),
        ("caprabo", "735399"),
        ("aldi", "9"),
    ]
    assert found[0].reasons == ("kind 1.00", "unit l", "store brand")


def test_pair_same_matches_two_lists_one_to_one() -> None:
    # all four listings are one milk; each side keeps one partner
    basket = [MILK[5], MILK[0], MILK[1]]
    page = [MILK[2], MILK[3]]

    pairs = pair_same(basket, page)

    assert [(m.left.store, m.right.store) for m in pairs] == [
        ("eroski", "consum"),
        ("caprabo", "carrefour"),
    ]


def test_group_same_splits_listings_into_products() -> None:
    groups = group_same(MILK)

    assert [
        [(listing.store, listing.product.id) for listing in g.listings] for g in groups
    ] == [
        [
            ("eroski", "735399"),
            ("caprabo", "735399"),
            ("consum", "735399"),
            ("carrefour", "521007075"),
        ],
        [("eroski", "15617756")],
        [("mercadona", "10382")],
        [("dia", "504")],
    ]
    assert groups[0].score == 1.0
    assert groups[0].reasons == ("brand", "size", "name 1.00")
    assert groups[1] == ProductGroup(
        listings=(Listing(store="eroski", product=MILK[4][1]),)
    )


def test_a_shared_barcode_lets_a_renamed_product_join() -> None:
    eroski = (
        "eroski",
        item("Cerveza MAHOU 5 Estrellas, lata 33 cl", id="465708", brand="MAHOU"),
    )
    caprabo = (
        "caprabo",
        item("Cerveza MAHOU 5 Estrellas, lata 33 cl", id="465708", brand="MAHOU"),
    )
    consum = (
        "consum",
        item(
            "Cerveza Lata 5 Estrellas",
            id="465708",
            brand="MAHOU 5 ESTRELLAS",
            ean="8411327122016",
            price="0.72",
            reference="2.18/l",
        ),
    )
    carrefour = (
        "carrefour",
        item(
            "Cerveza Mahou 5 estrellas la velada del año lata 33 cl.",
            id="521007938",
            brand="MAHOU",
            ean="8411327122016",
            pack="33 cl",
        ),
    )

    assert score_same(carrefour, eroski).score < 0.8
    (group,) = group_same([eroski, caprabo, consum, carrefour])

    assert len(group.listings) == 4


def test_group_same_needs_every_pair_across_two_groups() -> None:
    # 1.015 l is within reach of both 1 l and 1.03 l, which are not of each other
    a = ("dia", item("Leche semidesnatada", brand="ASTURIANA", pack="1 l"))
    b = ("consum", item("Leche semidesnatada", brand="ASTURIANA", pack="1015 ml"))
    c = ("eroski", item("Leche semidesnatada", brand="ASTURIANA", pack="1030 ml"))

    groups = group_same([a, b, c])

    assert score_same(b, c).score == 1.0
    assert score_same(a, c).reasons[1] == "size differs"
    assert [[listing.store for listing in g.listings] for g in groups] == [
        ["dia", "consum"],
        ["eroski"],
    ]


def test_group_same_keeps_one_listing_per_store() -> None:
    first = ("dia", item("Coca-Cola 2 L", id="1", brand="Coca-Cola"))
    second = ("dia", item("Coca-Cola 2 L", id="2", brand="Coca-Cola"))
    other = ("carrefour", item("Coca Cola botella 2 l.", id="3", brand="COCA COLA"))
    bare = item("Coca-Cola 2 L", id="4", brand="Coca-Cola")

    groups = group_same([first, second, other, bare])

    assert [[listing.product.id for listing in g.listings] for g in groups] == [
        ["1", "3", "4"],
        ["2"],
    ]


def test_a_listing_is_never_matched_with_itself() -> None:
    listing = MILK[0]

    assert find_same(listing, [listing]) == ()
    assert (
        find_alternatives(
            listing,
            [listing, Listing(store="eroski", product=item("Leche", id="735399"))],
        )
        == ()
    )
    assert group_same([listing, listing])[0].score is None


# --------------------------------------------------------------- arguments


@pytest.mark.parametrize("threshold", [-0.1, 1.5, math.nan, True, "0.8"])
def test_a_threshold_outside_zero_to_one_is_refused(threshold: Any) -> None:
    for call in (
        lambda: find_same(MILK[0], MILK, threshold=threshold),
        lambda: find_alternatives(MILK[0], MILK, threshold=threshold),
        lambda: pair_same(MILK, MILK, threshold=threshold),
        lambda: group_same(MILK, threshold=threshold),
    ):
        with pytest.raises(ConfigurationError, match="threshold must be a number"):
            call()


def test_listings_must_be_summaries() -> None:
    with pytest.raises(TypeError, match="expected a Listing"):
        score_same("milk", MILK[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="expected a Listing"):
        score_same(("consum", "milk"), MILK[0])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="collection of listings"):
        group_same(MILK[0][1])  # type: ignore[arg-type]


def test_matches_serialise_to_plain_data() -> None:
    match = score_same(MILK[0], MILK[1])
    group = group_same(MILK[:2])[0]

    document = to_dict(match)
    assert document["kind"] == "same"
    assert document["score"] == 1.0
    assert document["reasons"] == ["brand", "size", "name 1.00"]
    assert document["left"]["store"] == "eroski"
    assert document["right"]["product"]["id"] == "735399"
    assert json.loads(to_json(group))["reasons"] == ["brand", "size", "name 1.00"]
    assert isinstance(match, Match)


# ------------------------------------------------------------ text signals


@pytest.mark.parametrize(
    ("text", "count", "total"),
    [
        ("Coca Cola pack de 2 botellas de 2 l.", 2, "4 l"),
        ("Leche semidesnatada pack 6 x 1.5 L", 6, "9.0 l"),
        ("Galletas Maria dorada 0% azúcares añadidos 2x200gr", 2, "0.400 kg"),
        ("Leche semidesnatada paq. 3 u. de 200 ml", 3, "0.600 l"),
        ("Papel higiénico FOXY SEDA, paquete 4+2 rollos", 6, None),
        ("Cerveza Heineken pack 5+1 x 25 cl", 6, "1.50 l"),
        ("Higiénico Bouquet Color 4 Rollos Más 2 Gratis", 6, None),
        ("Estropajos Salvauñas Classic 2u más 1 regalo", 3, None),
        ("Kill paff anti-mosquitos recambio 2 uds + 1 aparato gratis", 2, None),
        ("Cerveza Rubia Mahou 5* 33CL P16", 16, None),
        ("Pasta dental Oral-B 75ml Pack2 Pro Expert", 2, None),
        ("Refresco cola Coca-Cola 33cl pack lata 12 zero azúcar", 12, None),
        ("Detergente en gel COLON, garrafa 40+5 dosis", None, None),
        ("Leche semidesnatada paq. de 6 brics", 6, None),
        ("Pack-6", 6, None),
        ("6 per paquet", 6, None),
        ("Cerveza 0,0 MAHOU, lata 33 cl", None, None),
        ("Cerveza Mahou 5 Estrellas", None, None),
    ],
)
def test_pack_sizes_are_read_from_text(
    text: str, count: int | None, total: str | None
) -> None:
    from supermercapy._match_text import plain

    read, _ = read_size(plain(text))

    assert read.count == count
    if total is None:
        assert read.total is None
    else:
        amount, unit = total.split()
        assert read.total == Quantity(amount=Decimal(amount), unit=Unit(unit))


@pytest.mark.parametrize(
    ("text", "loose"),
    [
        ("MOSTAZA PRIMA ORIGINAL PET +35G 300 G", "0.300 kg"),
        ("Cacahuete en polvo desgrasado +Proteínas 14 g 70% reducido en grasa", None),
        ("Dodot etapas T/6 +13kg", None),
        ("HELLMANN'S Mayonesa frasco 440 +10 ml.", "0.450 l"),
        ("Galleta digestive zero sin azúcares Gullón 300g + 100g", "0.400 kg"),
        ("Galleta Digestive sin Azúcar + 100g Gratis", None),
        ("Gofre con Chocolate 120 Más 20 g", "0.140 kg"),
        ("Natillas con chocolate +Proteínas 10 g 1,3 g MG", None),
        ("Huevo Campero 1/2 Docena", "6 piece"),
        ("Huevos frescos media docena", "6 piece"),
    ],
)
def test_extras_claims_and_halves_in_a_quantity(text: str, loose: str | None) -> None:
    from supermercapy._match_text import plain

    read, _ = read_size(plain(text))

    if loose is None:
        assert read.loose is None
    else:
        amount, unit = loose.split()
        assert read.loose == Quantity(amount=Decimal(amount), unit=Unit(unit))


def test_a_loose_quantity_is_a_total_unless_the_price_says_each() -> None:
    plusfresc = item(
        "Refresc COCA-COLA, pack 2 unitats 4 l", price="4.84", reference="1.21/l"
    )
    carrefour = item(
        "Coca Cola zero pack 2 botellas 2 l.", price="4.10", reference="1.025/l"
    )
    from supermercapy._match_text import features

    assert features(None, plusfresc).size == Size(
        count=2, total=Quantity(amount=Decimal(4), unit=Unit.LITRE)
    )
    size = features(None, carrefour).size
    assert size is not None and size.total == Quantity(
        amount=Decimal(4), unit=Unit.LITRE
    )


def test_a_stated_count_beats_a_piece_count_from_the_unit_price() -> None:
    # plusfresc prices this twelve-roll pack at 3.99 per piece
    rolls = item(
        "Paper higiènic doble capa original SCOTTEX, 12 rotlles",
        price="3.99",
        reference="3.99/piece",
    )
    named, _ = read_size("paper higienic 12 rotlles")

    size = pack_size(rolls, named, SizeText())

    assert size == Size(count=12, total=Quantity(amount=Decimal(12), unit=Unit.PIECE))


def test_implied_sizes_are_used_only_while_rounding_is_small() -> None:
    assert implied_total(item("x", price="4.98", reference="0.83/l")) is not None
    assert implied_total(item("x", price="1.19", reference="0.01/piece")) is None
    assert implied_total(item("x", price="1.19")) is None
    assert implied_total(item("x", price="0", reference="1.00/kg")) is None
    assert (
        implied_total(item("x", price="5.59", reference="5.59/kg", variable=True))
        is None
    )


def test_sizes_compare_by_count_then_total() -> None:
    six = Size(count=6, total=Quantity(amount=Decimal(96), unit=Unit.KILOGRAM))
    six_pieces = Size(count=6, total=Quantity(amount=Decimal(6), unit=Unit.PIECE))
    one = Size(count=1, total=Quantity(amount=Decimal(1), unit=Unit.PIECE))
    litre = Size(count=None, total=Quantity(amount=Decimal(1), unit=Unit.LITRE))

    assert same_size(six, six_pieces) is True
    assert same_size(one, litre) is None
    assert same_size(None, litre) is None
    assert same_size(six, Size(count=4, total=None)) is False


@pytest.mark.parametrize(
    ("ean", "expected"),
    [
        ("5449000009067", "05449000009067"),
        ("54491472", "00000054491472"),
        ("5449000009068", None),
        ("2200470000000", None),
        ("20758837", None),
        ("11120161717X", None),
        ("1234", None),
        (None, None),
    ],
)
def test_barcodes_that_name_no_product_are_dropped(
    ean: str | None, expected: str | None
) -> None:
    assert gtin(ean) == expected


def test_brands_lose_legal_noise_and_know_their_owner() -> None:
    assert brand_words("Productos Ramón S.L.") == ("productos", "ramon")
    assert brand_words(None) == ()
    assert owners(brand_words("Galleteca de Dia")) == frozenset({"dia"})
    assert owners(brand_words("E.BASIC")) == frozenset({"eroski", "caprabo"})
    assert owners(brand_words("Mendia")) == frozenset()


def test_tokens_read_negations_and_drop_noise() -> None:
    words = ("leche", "semidesnatada", "sin", "lactosa", "brik", "de", "llet")

    assert tokens(words) == frozenset({"lech", "semidesnatad", "sin-lactos"})
    assert tokens(words, drop=("llet",)) == frozenset({"semidesnatad", "sin-lactos"})


def test_likeness_counts_only_the_differences() -> None:
    assert likeness(frozenset(), frozenset()) == 1.0
    assert likeness(frozenset({"refresc"}), frozenset()) == pytest.approx(1 / 1.15)
    assert likeness(frozenset({"intantane"}), frozenset()) == pytest.approx(1 / 1.15)
    assert likeness(frozenset({"semidesntad"}), frozenset({"semidesnatad"})) > 0.9
    assert likeness(frozenset({"0"}), frozenset()) == 0.5


def test_find_same_keeps_the_first_of_two_equal_listings() -> None:
    first = (
        "caprabo",
        item("Leche semidesnatada ASTURIANA, brik 1 litro", id="a", brand="ASTURIANA"),
    )
    second = (
        "caprabo",
        item("Leche semidesnatada ASTURIANA, brik 1 litro", id="b", brand="ASTURIANA"),
    )

    (found,) = find_same(MILK[0], [first, second])

    assert found.right.product.id == "a"


def test_a_piece_count_is_kept_only_when_it_is_whole() -> None:
    half = item("Pastillas lavavajillas", price="2.50", reference="1.00/piece")

    size = pack_size(half, SizeText(), SizeText())

    assert size is not None and size.count is None
    assert size.total == Quantity(amount=Decimal("2.5"), unit=Unit.PIECE)


def test_an_empty_phrase_is_never_found() -> None:
    from supermercapy._match_text import find, remove

    assert find(("leche",), ()) is None
    assert remove(("leche",), [()]) == ("leche",)
