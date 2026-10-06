"""Which language does the caller want? No menu, no key presses: Sofa listens, and if she is not sure she
simply asks out loud ("Sorry, I didn't catch your language. Would you like to speak English or Yoruba?")
and understands the spoken answer.

Flow on a new caller's first turns (see routes/voice.py):
  1. Every enabled speech model listens to the first utterance; the most confident one sets the language.
  2. If none is confident, Sofa asks the question above, in English and, when we have the wording, in the
     other languages too, so a Yoruba speaker hears the question in Yoruba as well.
  3. The answer can be a language name ("Yoruba", "English please") or just the caller carrying on in their
     language: either works. A second unclear answer gets one more ask, then Sofa continues in English.
"""

import unicodedata
from difflib import SequenceMatcher

from . import templates

# Words that name a language, without accents (matching strips them). Spelling is fuzzy because speech
# recognisers spell "Yoruba" many ways; "geesi" is the Yoruba word for English.
KEYWORDS = {
    "en": ("english", "geesi", "gesi", "oyinbo"),
    "yo": ("yoruba", "yooba"),
    "ha": ("hausa",),
    "ig": ("igbo",),
}
FUZZY = 0.75
MAX_ASKS = 2


def _words(text: str) -> list[str]:
    plain = "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in plain).split()


def mentions(text: str, lang: str) -> bool:
    return any(SequenceMatcher(None, w, k).ratio() >= FUZZY for w in _words(text) for k in KEYWORDS.get(lang, ()))


def spoken_choice(texts: list[str], enabled: tuple[str, ...]) -> str | None:
    """The one language the caller named in any of these transcripts, else None (nothing named, or several)."""
    named = [l for l in enabled if any(mentions(t, l) for t in texts)]
    return named[0] if len(named) == 1 else None


def names(enabled: tuple[str, ...], lang: str) -> str:
    """'English or Yoruba', in the wording of `lang` when we have it (each name and the word 'or')."""
    def name(l: str) -> str:
        return templates.pick(f"lang_name_{l}", lang if templates.has(f"lang_name_{l}", lang) else "en")

    return templates.join_list([name(l) for l in enabled], lang if templates.has("word_or", lang) else "en", "word_or")


def ask_prompts(enabled: tuple[str, ...], attempt: int) -> list[tuple[str, str]]:
    """[(text, voice language)] for the spoken question: English first, then each other language we can say it in."""
    out = [(templates.render("language_ask", "en", {"languages": names(enabled, "en")}, variant=attempt - 1), "en")]
    for lang in enabled:
        if lang != "en" and templates.has("language_ask", lang):
            out.append((templates.render("language_ask", lang, {"languages": names(enabled, lang)}, variant=attempt - 1), lang))
    return out
