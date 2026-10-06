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
