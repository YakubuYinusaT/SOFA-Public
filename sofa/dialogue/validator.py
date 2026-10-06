"""Placeholder filling and validation of model-written replies.

N-ATLaS writes a sentence with placeholders; the backend fills them from the database. The reply
is only used if every product and number comes from a placeholder, it is short, and it asks
exactly the expected number of questions. Otherwise the caller hears the template instead.
"""

import re

from ..textutil import NUMBER_WORDS

PLACEHOLDER = re.compile(r"\{(\w+)(?:\.(\w+))?\}")
SPELLED_NUMBERS = set(NUMBER_WORDS) - {"one"}  # "which one?" is ordinary speech, not an invented quantity
MAX_WORDS = 25
MAX_SENTENCES = 2


def fill(template: str, facts: dict) -> str:
    """Fill {name} and {obj.field}. Raises KeyError on an unknown placeholder."""

    def sub(m: re.Match) -> str:
        obj, field = m.group(1), m.group(2)
        value = facts[obj]
        if field is not None:
            value = value[field] if isinstance(value, dict) else getattr(value, field)
        return str(value)

    return PLACEHOLDER.sub(sub, template)


def check_say(say: str, facts: dict, expect_question: bool = True, max_words: int = MAX_WORDS, max_sentences: int = MAX_SENTENCES) -> str | None:
    """Return the filled reply if `say` is safe to speak, else None (caller uses the template)."""
    say = (say or "").strip()
    if not say:
        return None
    try:
        filled = fill(say, facts)
    except (KeyError, AttributeError):
        return None
    literal = PLACEHOLDER.sub("", say).lower()
    # No number may come from the model: digits or spelled-out numbers outside placeholders.
    if re.search(r"\d", literal) or any(w in SPELLED_NUMBERS for w in re.findall(r"[a-z]+", literal)):
        return None
    if len(say.split()) > max_words:
        return None
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", filled) if s.strip()]
    if len(sentences) > max_sentences:
        return None
    if filled.count("?") != (1 if expect_question else 0):
        return None
    return filled
