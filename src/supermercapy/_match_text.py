"""the signals product matching reads from a summary: words, brand, pack size.

everything here is plain text work on the strings a :class:`ProductSummary`
already carries, in spanish and catalan, so none of it performs i/o. the
public scoring lives in :mod:`supermercapy.match`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache
from types import MappingProxyType

from ._core.models import ProductSummary
from ._core.units import SHARED_UNITS, Quantity, Unit, UnitPrice

Phrase = tuple[str, ...]

# ------------------------------------------------------------------ plain text

_MARKS = str.maketrans({"®": " ", "™": " ", "©": " ", "&": " ", "\u2019": "'"})
_HYPHENATED = re.compile(r"(?<=[a-z])-(?=[a-z])")
_NEGATION_SLASH = re.compile(r"\bs/\s*")
_WORD = re.compile(r"\d+(?:[.,]\d+)?|[a-z]+")


def plain(text: str) -> str:
    """fold case and accents, so ``"LLET Sencera"`` reads ``"llet sencera"``."""

    folded = unicodedata.normalize("NFKD", text.translate(_MARKS).casefold())
    stripped = "".join(char for char in folded if not unicodedata.combining(char))
    return stripped.replace("l·l", "ll").replace("·", "")


def _words(text: str) -> tuple[str, ...]:
    return tuple(word.replace(",", ".") for word in _WORD.findall(text))


# ----------------------------------------------------------------------- brand

_LEGAL_SUFFIX = re.compile(r"[\s,]+(?:s\.?\s?a\.?\s?u?\.?|s\.?\s?l\.?\s?u?\.?)$")

# store brands as plain words, by the store that sells them. a brand is a
# store's own when one of these phrases appears in it, so "dia lactea" and
# "galleteca de dia" are dia's and "carrefour bio" is carrefour's. caprabo
# belongs to the eroski group and sells its brands.
STORE_BRANDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "mercadona": ("hacendado", "deliplus", "bosque verde", "compy"),
        "consum": ("consum",),
        "carrefour": ("carrefour", "simpl"),
        "bonarea": ("bonarea",),
        "lidl": (
            *("milbona", "pilos", "solevita", "freeway", "realvalle", "nixe"),
            *("chef select", "vita d or", "kania", "alesto", "formil", "floralys"),
            *("dentalux", "aromata", "baresa", "dulano", "sol mar", "silvercrest"),
            *("livarno", "parkside", "esmara", "crivit", "lupilu", "deluxe"),
        ),
        "aldi": (
            *("milsani", "tandil", "esselt", "biscotto", "sal de plata"),
            *("gut bio", "choceur", "moser roth", "lacura", "almare"),
        ),
        "dia": ("dia",),
        "eroski": ("eroski", "e basic", "seleqtia", "belle"),
        "caprabo": ("caprabo", "eroski", "e basic", "seleqtia", "belle"),
        "bonpreu": ("bonpreu", "esclat"),
        "alcampo": ("alcampo", "auchan"),
        "ahorramas": ("alipende",),
        "condis": ("condis",),
    }
)

_STORE_BRAND_PHRASES: tuple[tuple[Phrase, str], ...] = tuple(
    (tuple(phrase.split()), store)
    for store, phrases in STORE_BRANDS.items()
    for phrase in phrases
)


def brand_words(brand: str | None) -> Phrase:
    """return a brand as plain words, without legal noise such as ``"s.a."``."""

    if not brand:
        return ()
    text = _LEGAL_SUFFIX.sub("", plain(brand).strip())
    return _words(text.replace("-", " "))


def find(words: Sequence[str], phrase: Phrase) -> tuple[int, int] | None:
    """return the first span of ``words`` that spells ``phrase``, if any.

    a span matches when its words are the phrase's words or when they run
    together into the phrase written without spaces, so ``"cola cao"`` and
    ``"colacao"`` both spell the brand ``COLACAO`` and ``"cocacola"`` spells
    ``"coca cola"``.
    """

    if not phrase:
        return None
    key = "".join(phrase)
    size = len(phrase)
    for start in range(len(words)):
        if tuple(words[start : start + size]) == phrase:
            return start, start + size
        joined = ""
        for end in range(start, len(words)):
            joined += words[end]
            if joined == key:
                return start, end + 1
            if not key.startswith(joined):
                break
    return None


def remove(words: Sequence[str], phrases: Iterable[Phrase]) -> tuple[str, ...]:
    """drop every span of ``words`` that spells one of ``phrases``, longest first.

    the longest goes first so ``"central lechera asturiana"`` leaves nothing
    behind, where taking ``"asturiana"`` first would strand its maker.
    """

    result = list(words)
    for phrase in sorted(phrases, key=len, reverse=True):
        while (span := find(result, phrase)) is not None:
            del result[span[0] : span[1]]
    return tuple(result)


def owners(brand: Phrase) -> frozenset[str]:
    """return the stores whose own brand ``brand`` is."""

    return frozenset(
        store
        for phrase, store in _STORE_BRAND_PHRASES
        if find(brand, phrase) is not None
    )


# ------------------------------------------------------------------- pack size

_NUMBER = r"\d+(?:[.,]\d+)?"
_COUNT_WORDS = (
    r"u|ud|uds|unid|unidad|unidades|unitat|unitats|latas?|llaunes|llauna|"
    r"botellas?|botellines|ampolles|bri(?:c|ck|k)s?|rollos?|rotlles?|sobres?|"
    r"capsulas?|capsules|pastillas?|pastilles|paquets?|bolsitas?|botes|"
    r"piezas?|peces|tarrinas?|tabletas?"
)
# 6 x 1 l, 12x33 cl, 3 x 0.052kg, 5+1 x 25 cl
_MULTI = re.compile(
    rf"(?<![\d.,])(\d+)(?:\s*\+\s*(\d+))?\s*[x\u00d7]\s*({_NUMBER})\s*([a-z]+)\b"
)
# 2 botellas de 2 l, 3 u. de 200 ml
_COUNT_OF = re.compile(
    rf"(?<![\d.,])(\d+)(?:\s*\+\s*(\d+))?\s*(?:{_COUNT_WORDS})\b\.?"
    rf"\s+de\s+({_NUMBER})\s*([a-z]+)\b"
)
# 6 latas, 4+2 rollos, 6 per paquet
_COUNTED = re.compile(
    rf"(?<![\d.,])(\d+)(?:\s*\+\s*(\d+))?\s*(?:(?:{_COUNT_WORDS})\b\.?|per paquet\b)"
)
# más 2 gratis, más 4 ud de regalo, més 1 de regal: free units of the same
# kind, which "+ 1 aparato gratis" is not
_BONUS = re.compile(
    rf"(?:\+|\bmas\b|\bmes\b)\s*(\d+)\s*(?:(?:{_COUNT_WORDS})\b\.?\s*)?"
    r"(?:de\s+)?(?:gratis|regalo|regal)\b"
)
# 200 g, 1,5 l, 40+5 dosis, 120 más 20 g, 300 g + 100 g
_QUANTITY = re.compile(
    rf"(?<![\d.,/])({_NUMBER})(?:\s*(?:\+|\bmas\b|\bmes\b)\s*(\d+))?\s*([a-z]+)\b\.?"
    rf"(?:\s*\+\s*({_NUMBER})\s*\3\b\.?)?"
)
# 1/2 docena, media docena, 1/2 kg
_HALF = re.compile(r"(?<![\d.,/])1/2(?=\s*[a-z])|\bmedia(?=\s+docenas?\b)")
# a quantity followed by one of these words measures a claim, not the pack:
# "1,3 g mg" of fat, "10 g de proteinas"
_CLAIM_AFTER = re.compile(
    r"\s*(?:de\s+)?(?:mg|materia grasa|grasas?|proteinas?|fibra|azucares?)\b"
)
# what comes right after a "+" is extra, never the pack: "+35 g" free on a
# 300 g bottle, a nappy for "+13 kg", or a claim such as "+proteinas 14 g"
_AFTER_PLUS = re.compile(r"\+\s*(?:(?:proteinas?|fibra|calcio|hierro)\s+)?$")
# pack 6, paq. de 6, pack-6, pack2, pack lata 12, and ahorramás's p16
_PACK = re.compile(
    r"\b(?:(?:pack|paq|paquete|paquet|caja|caixa|estoig|lote)(?![a-z])\.?[\s-]*"
    r"(?:(?:latas?|botellas?|botellines|bri(?:c|ck|k)s?)\s+)?(?:de\s+)?(\d+)"
    r"|p(\d{1,2}))\b(?![.,]\d)"
)

# a published unit price is rounded, so a pack size read back from it is only
# used while that rounding moves it by less than this share
_IMPLIED_LIMIT = Decimal("0.05")
# how far two pack sizes may differ and still be the same pack
_SIZE_SLACK = Decimal("0.02")


@dataclass(frozen=True, slots=True)
class Size:
    """what a listing holds: how many selling units, and how much in all.

    ``slack`` is the share by which ``total`` may be off, when it was read
    back from a rounded unit price rather than from text.
    """

    count: int | None
    total: Quantity | None
    slack: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class SizeText:
    """what one piece of text says about size, before it is combined."""

    count: int | None = None
    total: Quantity | None = None
    loose: Quantity | None = None


def _quantity(amount: str, unit: str, extra: str | None = None) -> Quantity | None:
    quantity = SHARED_UNITS.quantity(f"{amount.replace(',', '.')} {unit}")
    if quantity is None or extra is None:
        return quantity
    each = quantity.amount / Decimal(amount.replace(",", "."))
    return Quantity(amount=quantity.amount + each * int(extra), unit=quantity.unit)


def _times(count: int, quantity: Quantity) -> Quantity:
    return Quantity(amount=quantity.amount * count, unit=quantity.unit)


def read_size(text: str) -> tuple[SizeText, str]:
    """read the pack size out of plain text and return the text without it.

    a multipack (``6 x 1 l``, ``2 botellas de 2 l``) gives a count and a
    total. a count alone (``6 latas``, ``pack 6``) and a quantity alone
    (``200 g``) are kept apart, because ``pack 2 unitats 4 l`` means four
    litres in all while ``pack 2 botellas 2 l`` means two litres each.
    """

    count: int | None = None
    total: Quantity | None = None
    loose: Quantity | None = None
    spans: list[tuple[int, int]] = []
    text = _HALF.sub("0.5", text)

    def claim(match: re.Match[str]) -> bool:
        start, end = match.span()
        if any(start < last and first < end for first, last in spans):
            return False
        spans.append((start, end))
        return True

    for pattern in (_MULTI, _COUNT_OF):
        for match in pattern.finditer(text):
            each = _quantity(match.group(3), match.group(4))
            if each is None or not claim(match) or total is not None:
                continue
            count = int(match.group(1)) + int(match.group(2) or 0)
            total = _times(count, each)
    # read before the counts, so the "4 ud" of "mas 4 ud de regalo" is free
    bonus = sum(int(match.group(1)) for match in _BONUS.finditer(text) if claim(match))
    for match in _COUNTED.finditer(text):
        if claim(match) and count is None:
            count = int(match.group(1)) + int(match.group(2) or 0)
    if count is not None and total is None:
        count += bonus
    for match in _QUANTITY.finditer(text):
        quantity = _quantity(match.group(1), match.group(3), match.group(2))
        if quantity is None or not claim(match):
            continue
        if match.group(4) and (more := _quantity(match.group(4), match.group(3))):
            quantity = Quantity(
                amount=quantity.amount + more.amount, unit=quantity.unit
            )
        extra = match.group(2)
        # a bonus is never bigger than its pack, so "talla 6 +13 kg" is a band
        banded = extra is not None and int(extra) > Decimal(
            match.group(1).replace(",", ".")
        )
        extra_or_claim = (
            banded
            or _AFTER_PLUS.search(text, 0, match.start()) is not None
            or _CLAIM_AFTER.match(text, match.end()) is not None
        )
        if not extra_or_claim:
            loose = loose or quantity
    for match in _PACK.finditer(text):
        if claim(match) and count is None:
            count = int(match.group(1) or match.group(2))
    remaining = text
    for start, end in sorted(spans, reverse=True):
        remaining = f"{remaining[:start]} {remaining[end:]}"
    return SizeText(count=count, total=total, loose=loose), remaining


def implied_total(product: ProductSummary) -> tuple[Quantity, Decimal] | None:
    """return the pack size the price and the unit price imply, with its slack.

    ``4.98`` at ``0.83`` per litre is six litres. the unit price is rounded,
    so the answer carries how far that rounding could move it, and a figure
    the rounding could move by more than five percent is not used at all.
    """

    price = product.price
    reference = price.reference
    if reference is None or price.amount is None or product.is_variable_weight:
        return None
    if reference.amount <= 0 or price.amount <= 0:
        return None
    exponent = reference.amount.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    slack = Decimal(5).scaleb(-places - 1) / reference.amount
    if slack > _IMPLIED_LIMIT:
        return None
    return Quantity(amount=price.amount / reference.amount, unit=reference.unit), slack


def pack_size(product: ProductSummary, name: SizeText, pack: SizeText) -> Size | None:
    """combine what the name, the pack text, and the unit price say."""

    count = pack.count if pack.count is not None else name.count
    total = pack.total or name.total
    slack = Decimal(0)
    implied = implied_total(product)
    loose = pack.loose or name.loose
    if total is None and loose is not None:
        total = loose
        # "pack 2 botellas 2 l" may mean two litres or four: ask the price
        if (
            count is not None
            and count > 1
            and implied is not None
            and close(_times(count, loose), *implied)
        ):
            total = _times(count, loose)
    if total is None and implied is not None:
        total, slack = implied
        if total.unit is Unit.PIECE and count is not None:
            # the text says how many pieces; a unit price can be per pack
            total, slack = Quantity(amount=Decimal(count), unit=Unit.PIECE), Decimal(0)
    if total is not None and total.unit is Unit.PIECE and count is None:
        whole = total.amount.to_integral_value()
        if whole and close(Quantity(amount=whole, unit=Unit.PIECE), total, slack):
            count = int(whole)
    if count is None and total is loose and total is not None:
        # "2 l" names one bottle, so six cans of 33 cl are not it; a size read
        # back from the price says nothing about how many packs it covers
        count = 1
    if count is None and total is None:
        return None
    return Size(count=count, total=total, slack=slack)


def close(a: Quantity, b: Quantity, slack: Decimal = Decimal(0)) -> bool:
    """say whether two quantities are the same within the size slack."""

    if a.unit is not b.unit:
        return False
    bigger = max(a.amount, b.amount)
    return abs(a.amount - b.amount) <= bigger * (_SIZE_SLACK + slack)


def same_size(a: Size | None, b: Size | None) -> bool | None:
    """say whether two sizes agree, disagree, or cannot be compared."""

    if a is None or b is None:
        return None
    if a.count is not None and b.count is not None and a.count != b.count:
        return False
    if a.total is None or b.total is None or a.total.unit is not b.total.unit:
        # six sachets and six sachets agree even when one says 96 g
        return True if a.count is not None and a.count == b.count != 1 else None
    return close(a.total, b.total, a.slack + b.slack)


# ----------------------------------------------------------------------- words

_STOP = frozenset(
    {
        *("de", "del", "la", "las", "el", "els", "los", "lo", "les", "en", "con"),
        *("y", "e", "i", "o", "a", "al", "als", "amb", "per", "para", "por", "d"),
        *("l", "un", "una", "uns", "unes", "dels", "pel", "pels", "tipo", "x"),
        *("mas", "gratis", "regalo", "oferta", "promocion", "ahorro", "formato"),
        *("anadido", "anadidos", "anadida", "anadidas", "afegit", "afegits"),
    }
)
_PACKAGING = frozenset(
    {
        *("botella", "botellas", "botellin", "botellines", "ampolla", "ampolles"),
        *("lata", "latas", "llauna", "llaunes", "brik", "briks", "brick", "bricks"),
        *("bric", "brics", "carton", "cartro", "tetrabrik", "bote", "botes", "pot"),
        *("tarro", "vaso", "got", "cristal", "frasco", "garrafa", "envase", "bolsa"),
        *("bossa", "ecobolsa", "ecobossa", "caja", "caixa", "estoig", "paquete"),
        *("paquet", "paquets", "pack", "paq", "lote", "malla", "bandeja", "safata"),
        *("tarrina", "maleta", "unidad", "unidades", "ud", "uds", "u", "unitat"),
        *("unitats", "unid", "bocabajo", "dosificador"),
    }
)
_CONTAINERS: Mapping[str, str] = MappingProxyType(
    {
        **dict.fromkeys(("lata", "latas", "llauna", "llaunes"), "can"),
        **dict.fromkeys(
            ("botella", "botellas", "botellin", "botellines", "ampolla", "ampolles"),
            "bottle",
        ),
        **dict.fromkeys(
            ("brik", "briks", "brick", "bricks", "bric", "brics", "tetrabrik"),
            "brick",
        ),
        **dict.fromkeys(("carton", "cartro"), "brick"),
        **dict.fromkeys(("frasco", "tarro", "pot"), "jar"),
        **dict.fromkeys(("bocabajo", "dosificador"), "squeeze"),
    }
)
_NEGATIONS = frozenset({"sin", "sense"})

# catalan words and spanish spellings with the same meaning, mapped onto one
# spanish word before stemming
_SYNONYMS: Mapping[str, str] = MappingProxyType(
    {
        **{"llet": "leche", "oli": "aceite", "olis": "aceite", "cacau": "cacao"},
        **{"avellanes": "avellanas", "galeta": "galleta", "galetes": "galletas"},
        **{"tonyina": "atun", "cervesa": "cerveza", "cerveses": "cervezas"},
        **{"paper": "papel", "higienic": "higienico", "detergent": "detergente"},
        **{"rentaplats": "lavavajillas", "rentavaixelles": "lavavajillas"},
        **{"verge": "virgen", "aigua": "agua", "suc": "zumo", "taronja": "naranja"},
        **{"poma": "manzana", "formatge": "queso", "pernil": "jamon"},
        **{"iogurt": "yogur", "iogurts": "yogures", "ou": "huevo", "ous": "huevos"},
        **{"arros": "arroz", "sucre": "azucar", "sucres": "azucares"},
        **{"farina": "harina", "pa": "pan", "mantega": "mantequilla"},
        **{"xocolata": "chocolate", "torrada": "tostada", "torrat": "tostado"},
        **{"sencera": "entera", "calci": "calcio", "suau": "suave", "humit": "humedo"},
        **{"rotlle": "rollo", "rotlles": "rollos", "megarotlle": "megarollo"},
        **{"capes": "capas", "pols": "polvo", "liquid": "liquido", "blanc": "blanco"},
        **{"negre": "negro", "afegit": "anadido", "afegits": "anadidos"},
        **{"llimona": "limon", "maduixa": "fresa", "pollastre": "pollo"},
        **{"porc": "cerdo", "vedella": "ternera", "tomaquet": "tomate"},
        **{"patates": "patatas", "olives": "aceitunas", "cereals": "cereales"},
        **{"melmelada": "mermelada", "mel": "miel", "batut": "batido"},
        **{"refresc": "refresco", "gasosa": "gaseosa", "vi": "vino", "sabo": "jabon"},
        **{"suavitzant": "suavizante", "lleixiu": "lejia", "cuina": "cocina"},
        **{"tovallons": "servilletas", "mocadors": "panuelos", "peces": "piezas"},
        **{"uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "quatre": "4"},
        **{"cinco": "5", "cinc": "5", "seis": "6", "sis": "6"},
        **{"cero": "0", "zero": "0", "maionesa": "mayonesa", "civada": "avena"},
        **{"ensucrat": "azucarado", "ensucrada": "azucarada", "grec": "griego"},
        **{"casolana": "casera", "casola": "casero", "desnatat": "desnatado"},
        **{"pressec": "melocoton", "nabius": "arandanos", "tofona": "trufa"},
        **{"rodo": "redondo", "rodona": "redonda", "blat": "trigo", "pit": "pechuga"},
        **{"cigro": "garbanzo", "cigrons": "garbanzos", "molt": "molido"},
        **{"cuit": "cocido", "cuits": "cocidos", "ceba": "cebolla"},
        **{"cebes": "cebollas"},
        # the italian spellings spanish shelves use
        **{"spaghetti": "espagueti", "spagueti": "espagueti", "spaguetti": "espagueti"},
        **{"spaguettis": "espaguetis", "spaghettis": "espaguetis"},
    }
)
_VOWELS = frozenset("aeiou")
_SUGAR = "azucar"
_ZERO = "0"
_FUZZY_LENGTH = 5
_FUZZY_RATIO = 0.85

# what one word that only one of two names has costs, by kind of word. a
# descriptor says what sort of product it is or how it is presented, and
# stores add or drop it freely; a number or a negation almost always names a
# variant, such as the 0 of "0% azucar" or "sin lactosa"
_DESCRIPTOR_COST = 0.15
_WORD_COST = 0.7
_VARIANT_COST = 1.0
_DESCRIPTOR_WORDS = (
    *("refresco", "bebida", "original", "soluble", "instantaneo", "polvo"),
    *("cacao", "untar", "papel", "producto", "nuevo", "edicion", "limitada"),
    *("receta", "estilo", "sabor"),
)


def name_words(text: str) -> tuple[str, ...]:
    """split plain text into words, joining ``gira-sol`` and reading ``s/``."""

    return _words(_NEGATION_SLASH.sub(" sin ", _HYPHENATED.sub("", text)))


def containers(words: Iterable[str]) -> frozenset[str]:
    """return the containers a listing names: can, bottle, brick, jar, squeeze."""

    return frozenset(_CONTAINERS[word] for word in words if word in _CONTAINERS)


@lru_cache(maxsize=8192)
def stem(word: str) -> str:
    """reduce a word to a crude stem its plural and gender forms share."""

    word = _SYNONYMS.get(word, word)
    if not word.isalpha():
        return word
    if len(word) > 4 and word.endswith("es") and word[-3] not in _VOWELS:
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s"):
        word = word[:-1]
    if len(word) > 4 and word[-1] in "aeo":
        word = word[:-1]
    return word


_DESCRIPTORS = frozenset(stem(word) for word in _DESCRIPTOR_WORDS)


def tokens(words: Iterable[str], drop: Iterable[str] = ()) -> frozenset[str]:
    """return the tokens that say what a listing is.

    stop words, packaging and unit words go, and so does every word whose stem
    is in ``drop``. a negation joins the word it negates, so ``"sin
    lactosa"`` becomes ``"sin-lactos"``, and the sugar claims ``"zero"``,
    ``"0% azucar"`` and ``"sin azucar"`` all read ``"0"``.
    """

    dropped = {stem(word) for word in drop}
    result: set[str] = set()
    negate = False
    previous = ""
    for word in words:
        if word in _NEGATIONS:
            negate = True
            continue
        if word in _STOP or word in _PACKAGING:
            continue
        token = stem(word)
        if token in dropped:
            continue
        if token == _SUGAR and (negate or previous == _ZERO):
            token = _ZERO
        elif negate:
            token = f"sin-{token}"
        negate = False
        previous = token
        result.add(token)
    return frozenset(result)


@lru_cache(maxsize=16384)
def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@lru_cache(maxsize=4096)
def cost(token: str) -> float:
    """return what a token costs when only one of two names has it.

    a misspelt descriptor, such as bonarea's ``"intantaneo"``, still costs
    what a descriptor costs.
    """

    if token[0].isdigit() or token.startswith("sin-"):
        return _VARIANT_COST
    if token in _DESCRIPTORS or (
        len(token) >= _FUZZY_LENGTH
        and any(
            _ratio(token, descriptor) >= _FUZZY_RATIO for descriptor in _DESCRIPTORS
        )
    ):
        return _DESCRIPTOR_COST
    return _WORD_COST


def likeness(a: frozenset[str], b: frozenset[str]) -> float:
    """return how alike two token sets are, from ``1.0`` down towards ``0``.

    only the differences count: every token one side has and the other lacks
    adds its :func:`cost`, and the answer is ``1 / (1 + total cost)``. a
    descriptor such as ``"refresco"`` costs little, so ``"Refresco
    Coca-Cola"`` stays close to ``"Coca-Cola"``, while one unknown word such
    as ``"picual"`` against ``"hojiblanca"`` is enough to keep two olive
    oils apart. tokens of five letters or more that differ by a typo, such as
    ``"semidesntad"`` and ``"semidesnatad"``, cost only the share in which
    they differ.
    """

    pending = sorted(b - a)
    total = 0.0
    for token in sorted(a - b):
        candidates = [other for other in pending if len(other) >= _FUZZY_LENGTH]
        if len(token) >= _FUZZY_LENGTH and candidates:
            ratio, partner = max((_ratio(token, other), other) for other in candidates)
            if ratio >= _FUZZY_RATIO:
                pending.remove(partner)
                total += (1 - ratio) * (cost(token) + cost(partner))
                continue
        total += cost(token)
    total += sum(cost(token) for token in pending)
    return 1 / (1 + total)


# ------------------------------------------------------------------------ ean


def gtin(value: str | None) -> str | None:
    """return a barcode as 14 digits, or ``None`` when it names no product.

    a bad check digit and the restricted prefixes a store gives its own
    weighed items (``2`` on thirteen digits, ``0`` or ``2`` on eight) all give
    ``None``: none of them identifies a product across stores.
    """

    if not value:
        return None
    digits = value.strip()
    if not digits.isdigit() or not 8 <= len(digits) <= 14:
        return None
    if (len(digits) == 13 and digits[0] == "2") or (
        len(digits) == 8 and digits[0] in "02"
    ):
        return None
    padded = digits.zfill(14)
    weighted = sum(
        int(digit) * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(padded[:-1])
    )
    if (10 - weighted % 10) % 10 != int(padded[-1]):
        return None
    return padded


# ------------------------------------------------------------------- features


@dataclass(frozen=True, slots=True)
class Features:
    """everything matching reads from one listing, read once."""

    store: str | None
    ean: str | None
    brand: Phrase
    owners: frozenset[str]
    words: tuple[str, ...]
    containers: frozenset[str]
    size: Size | None
    reference: UnitPrice | None


def features(store: str | None, product: ProductSummary) -> Features:
    """read the matching signals of one listing."""

    name_size, name_rest = read_size(plain(product.name))
    pack_size_, pack_rest = read_size(plain(product.pack_size_text or ""))
    words = name_words(name_rest)
    brand = brand_words(product.brand)
    return Features(
        store=None if store is None else plain(store).strip(),
        ean=gtin(product.ean),
        brand=brand,
        owners=owners(brand),
        words=words,
        containers=containers((*words, *name_words(pack_rest))),
        size=pack_size(product, name_size, pack_size_),
        reference=product.price.reference,
    )
