"""Text normalisation, numbers, units and trigram similarity (pg_trgm equivalent)."""

import re
import unicodedata

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "hundred": 100,
    "dozen": 12,
}

# Yoruba numerals as they arrive from speech recognition once tone marks and dots are stripped
# (mẹ́ta -> meta). Only the forms used when counting things ("carton meji", "meta ni Peak"). Bare forms that
# are also everyday words (ewa = beans, arun = illness) are left out on purpose. NEEDS a Yoruba speaker's
# review: docs/code_mixing.md.
YORUBA_NUMBERS = {
    "kan": 1, "okan": 1, "meji": 2, "eji": 2, "meta": 3, "eta": 3, "merin": 4, "erin": 4, "marun": 5,
    "mefa": 6, "efa": 6, "meje": 7, "mejo": 8, "mesan": 9, "mewa": 10, "mewaa": 10,
    "ogun": 20, "ogbon": 30, "ogoji": 40, "aadota": 50, "ogorun": 100,
}
NUMBER_WORDS.update(YORUBA_NUMBERS)

# Shared unit vocabulary (mirrors the `units` table seed). Local-language variants get added
# here / in the table as they are collected from real calls.
UNIT_VARIANTS: dict[str, list[str]] = {
    "carton": ["carton", "cartons", "ctn", "ctns", "kaatini"],
    "pack": ["pack", "packs", "packet", "packets"],
    "piece": ["piece", "pieces", "pcs", "pc"],
    "bag": ["bag", "bags"],
    "crate": ["crate", "crates"],
    "tin": ["tin", "tins"],
    "sachet": ["sachet", "sachets"],
    "derica": ["derica", "dericas"],
    "mudu": ["mudu", "mudus"],
    "kg": ["kg", "kgs", "kilo", "kilos", "kilogram", "kilograms"],
    "litre": ["litre", "litres", "liter", "liters"],
    "bottle": ["bottle", "bottles"],
    "drum": ["drum", "drums"],
}
UNIT_LOOKUP = {v: canon for canon, vs in UNIT_VARIANTS.items() for v in vs}

FILLERS = {
    "i", "want", "need", "please", "abeg", "give", "me", "send", "add", "get", "bring", "the",
    "a", "an", "of", "some", "my", "name", "is", "and", "to", "like", "would", "can", "could",
    "you", "we", "us", "for", "buy", "remove", "make", "it", "am", "im", "this", "have", "do",
    "how", "much", "price", "what", "much", "is", "there", "any",
    # Yoruba and Nigerian Pidgin filler around an order (plain-letter forms): please, I / I want, give, me, of, buy...
    "jowo", "abeg", "mo", "fe", "fun", "mi", "ti", "ni", "ra", "wan", "dey", "una", "na", "be",
}

PLURALS = {"pack": "packs", "carton": "cartons", "piece": "pieces", "bag": "bags",
           "crate": "crates", "tin": "tins", "sachet": "sachets", "derica": "dericas", "mudu": "mudus"}


def clean(text: str) -> str:
    # Speech recognisers write Yoruba with tone marks and dots under letters (mẹ́ta, jọ̀wọ́, ṣé). Reduce every letter to
    # its plain Latin base FIRST; otherwise those letters are deleted below and the words are destroyed.
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = text.lower().replace("'", "")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 9,500 -> 9500
    text = re.sub(r"[^a-z0-9\s.]", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_unit(word: str | None) -> str | None:
    if not word:
        return None
    return UNIT_LOOKUP.get(word.lower().strip())


def parse_number(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token)


def normalize_phrase(text: str) -> str:
    """Normalise a spoken product phrase for matching: lowercase, no filler words. Unit words stay:
    they can be part of a product's identity ("Peak Milk Tin", "Super Pack")."""
    words = [w for w in clean(text).split() if w not in FILLERS]
    return " ".join(words)


def tokens(text: str) -> set[str]:
    return {w for w in clean(text).split() if w not in FILLERS and parse_number(w) is None}


def _trigrams(text: str) -> set[str]:
    grams: set[str] = set()
    for word in clean(text).split():
        padded = f"  {word} "
        grams.update(padded[i : i + 3] for i in range(len(padded) - 2))
    return grams


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of trigram sets, the same measure as pg_trgm's similarity()."""
    ta, tb = _trigrams(a), _trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def containment(phrase: str, candidate: str) -> float:
    """Share of the phrase's trigrams found in the candidate. Unlike Jaccard it does not punish a
    longer product name, so 'peak milk' is equally close to 'Peak Milk Tin' and 'Peak Milk Sachet'."""
    tp, tc = _trigrams(phrase), _trigrams(candidate)
    return len(tp & tc) / len(tp) if tp else 0.0


def naira(kobo: int) -> str:
    naira_part, kobo_part = divmod(int(kobo), 100)
    if kobo_part:
        return f"{naira_part:,}.{kobo_part:02d} naira"
    return f"{naira_part:,} naira"


def plural_unit(unit: str, qty: int) -> str:
    return unit if qty == 1 else PLURALS.get(unit, unit + "s")


def e164(number: str) -> str:
    n = re.sub(r"[\s\-()]", "", number or "")
    if n.startswith("+"):
        return n
    if n.startswith("234"):
        return "+" + n
    if n.startswith("0"):
        return "+234" + n[1:]
    return "+" + n if n else n


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate: word-level edit distance divided by the reference length."""
    r, h = clean(reference).split(), clean(hypothesis).split()
    if not r:
        return 0.0 if not h else 1.0
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)] / len(r)
