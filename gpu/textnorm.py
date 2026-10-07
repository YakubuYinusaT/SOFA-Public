"""Text preparation for TTS: spell out numbers, split long replies into short chunks.

Pure Python (no torch), so it runs and is tested without a GPU.

OPEN DECISION (spec: "Risks and open decisions"): whether prices and quantities are spoken in
English or the local language. English is implemented. For yo/ha/ig, digits are left as they
are until native-speaker number words are agreed; check how YarnGPT2 reads them before the pilot.
"""

import re

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
        "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")]


def spell_number(n: int) -> str:
    """1640000 -> 'one million six hundred and forty thousand' (British style, as Nigerians say it)."""
    if n < 0:
        return "minus " + spell_number(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        return TENS[n // 10] + ("" if n % 10 == 0 else " " + ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + spell_number(rest))
    for size, name in SCALES:
        if n >= size:
            head, rest = divmod(n, size)
            text = f"{spell_number(head)} {name}"
            if rest == 0:
                return text
            return text + (" and " if rest < 100 else " ") + spell_number(rest)
    return str(n)


_MONEY = re.compile(r"(\d[\d,]*)\.(\d{2})\s+naira")
_NUMBER = re.compile(r"\d[\d,]*")


def normalize_for_tts(text: str, language: str = "en") -> str:
    text = text.replace("₦", "naira ").replace("&", " and ")
    if language != "en":
        return re.sub(r"\s+", " ", text).strip()

    def money(m: re.Match) -> str:
        whole = spell_number(int(m.group(1).replace(",", "")))
        kobo = int(m.group(2))
        return f"{whole} naira" + (f" {spell_number(kobo)} kobo" if kobo else "")

    text = expand_contractions(text)
    text = _MONEY.sub(money, text)
    text = _NUMBER.sub(lambda m: spell_number(int(m.group(0).replace(",", ""))), text)
    return re.sub(r"\s+", " ", text).strip()


def chunk_text(text: str, max_chars: int = 200) -> list[str]:
    """Short chunks keep YarnGPT2 stable and each clip under ~12 s. Splits on sentence ends, then
    commas, then spaces; never inside a word."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        for part in re.split(r"(?<=,)\s+", sentence):
            while len(part) > max_chars:
                cut = part.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                pieces.append(part[:cut].strip())
                part = part[cut:].strip()
            if part:
                pieces.append(part)
    chunks: list[str] = []
    for piece in pieces:  # merge neighbours while they still fit
        if chunks and len(chunks[-1]) + 1 + len(piece) <= max_chars:
            chunks[-1] += " " + piece
        else:
            chunks.append(piece)
    return chunks


# ---- speaking the punctuation ----------------------------------------------------------------
# YarnGPT2 never sees punctuation: its own text step deletes every comma, full stop, question mark and apostrophe, so the model gets a flat
# list of words ("that's" becomes "thats"). It cannot pause at a comma or start afresh after a full stop. So the pieces are made one at a
# time here and joined with real silence, and contractions are written out in full before the model sees them.

_CONTRACTIONS = [
    (re.compile(r"\bcan['’]t\b", re.I), "cannot"),
    (re.compile(r"\bwon['’]t\b", re.I), "will not"),
    (re.compile(r"\bshan['’]t\b", re.I), "shall not"),
    (re.compile(r"\blet['’]s\b", re.I), "let us"),
    (re.compile(r"\b(\w+)n['’]t\b", re.I), r"\1 not"),
    (re.compile(r"\bI['’]m\b", re.I), "I am"),
    (re.compile(r"\b(\w+)['’]re\b", re.I), r"\1 are"),
    (re.compile(r"\b(\w+)['’]ve\b", re.I), r"\1 have"),
    (re.compile(r"\b(\w+)['’]ll\b", re.I), r"\1 will"),
    (re.compile(r"\b(that|it|what|there|here|who|how|where|when|he|she)['’]s\b", re.I), r"\1 is"),
    (re.compile(r"\b(\w+)['’]d\b", re.I), r"\1 would"),
]


def expand_contractions(text: str) -> str:
    """"that's" -> "that is", "didn't" -> "did not", "we'll" -> "we will": the voice reads "thats" and "didnt" badly."""
    for pattern, replacement in _CONTRACTIONS:
        text = pattern.sub(replacement, text)
    return text


PAUSE_AFTER = {",": 0.22, ";": 0.30, ":": 0.30, ".": 0.50, "?": 0.55, "!": 0.50}  # seconds of silence after a piece that ended with this mark
MIN_WORDS = 3  # the voice rushes and smears a piece of one or two words, so such a piece is joined to its neighbour


def _word_count(piece: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", piece))


def speech_plan(text: str, mode: str = "clause", max_chars: int = 200) -> list[tuple[str, float, list[tuple[float, float]]]]:
    """What to speak and where to pause: [(text, silence_after, inside_pauses), ...].

    `silence_after` is the silence to put after the piece. `inside_pauses` are (fraction of the way through the piece, seconds) for the marks inside it:
    a piece too short to be said clearly (fewer than MIN_WORDS words) is spoken together with its neighbour, and the pauses between them are put
    back afterwards at the quietest moment near where they belong (see audioutil.insert_pauses).

    mode "off"      the old way: length-based chunks with a short fixed pause (kept to compare)
         "sentence" a pause after . ? !
         "clause"   a pause after , ; : as well (the default)
         "all"      every piece spoken on its own, however short
    """
    text = text.strip()
    if not text:
        return []
    if mode == "off":
        return [(piece, 0.15, []) for piece in chunk_text(text, max_chars)]
    splitter = r"(?<=[.?!])\s+" if mode == "sentence" else r"(?<=[.?!,;:])\s+"
    pieces: list[tuple[str, float]] = []
    for raw in re.split(splitter, text):
        raw = raw.strip()
        if not raw:
            continue
        mark = raw[-1] if raw[-1] in PAUSE_AFTER else "."
        body = raw.rstrip(".?!,;:").strip()
        if body:
            pieces.append((body, PAUSE_AFTER[mark]))
    groups: list[dict] = []  # each: text, the silence after it, and the boundaries inside it as (character offset, seconds)
    for body, pause in pieces:
        last = groups[-1] if groups else None
        if mode != "all" and last is not None and _word_count(last["text"]) < MIN_WORDS:
            last["bounds"].append((len(last["text"]), last["pause"]))
            last["text"] += " " + body
            last["pause"] = pause
        else:
            groups.append({"text": body, "pause": pause, "bounds": []})
    if mode != "all" and len(groups) > 1 and _word_count(groups[-1]["text"]) < MIN_WORDS:
        tail, head = groups.pop(), groups[-1]
        head["bounds"].append((len(head["text"]), head["pause"]))
        head["text"] += " " + tail["text"]
        head["pause"] = tail["pause"]
    plan: list[tuple[str, float, list[tuple[float, float]]]] = []
    for group in groups:
        parts = chunk_text(group["text"], max_chars)
        if len(parts) == 1:
            marks = [(offset / len(group["text"]), seconds) for offset, seconds in group["bounds"]]
            plan.append((parts[0], group["pause"], marks))
        else:  # a long piece is cut by length; its inner marks are not placed
            for i, part in enumerate(parts):
                plan.append((part, 0.12 if i < len(parts) - 1 else group["pause"], []))
    return plan
