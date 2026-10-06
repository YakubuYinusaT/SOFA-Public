"""A strict safety net for plain yes and no answers.

The model usually reads "yes" and "no" correctly, but on the first GPU run it missed "No, that's wrong" and "Okay go ahead", and
a missed answer in a money flow is a risk. This only decides when EVERYTHING the caller said is a yes or no (or a filler word),
so "ok, three thousand naira" is never mistaken for a yes. Anything else is left to the model.
"""

from ..textutil import clean

YES_WORDS = {"yes", "yeah", "yep", "yup", "ok", "okay", "sure", "correct", "right", "beeni"}
NO_WORDS = {"no", "nope", "nah", "rara", "never"}
YES_PHRASES = ("go ahead", "thats right", "that is right", "that is correct", "thats correct", "do it", "bee ni", "e se", "o da")
NO_PHRASES = ("thats wrong", "that is wrong", "not correct", "not right", "not that one", "kii se bee", "do not")
FILLERS = {"please", "o", "oo", "ooo", "sir", "ma", "abeg", "sha", "thanks", "thank", "you", "ni", "so", "too", "the"}


def quick_answer(transcript: str) -> str | None:
    """'yes', 'no', or None when the reply is not purely a yes or a no."""
    text = f" {clean(transcript)} "
    yes = [p for p in YES_PHRASES if f" {p} " in text]
    no = [p for p in NO_PHRASES if f" {p} " in text]
    for phrase in (*yes, *no):
        text = text.replace(f" {phrase} ", " ")
    words = text.split()
    said_yes = bool(yes) or any(w in YES_WORDS for w in words)
    said_no = bool(no) or any(w in NO_WORDS for w in words)
    left = [w for w in words if w not in YES_WORDS | NO_WORDS | FILLERS]
    if left or said_yes == said_no:  # something else was said, or it is both or neither
        return None
    return "yes" if said_yes else "no"


# Closing a call. After SOFA asks "is there anything else?", a "no" (or "that's all", "thank you, bye") means the caller is done: SOFA says
# goodbye and ends the call, because an open line costs money. Anything more than closing words ("no, check my balance") is a request.
CLOSING = ("no", "nope", "nah", "nothing", "rara", "ko si", "thats all", "that is all", "that will be all", "i am good", "im good", "all good",
           "i am done", "im done", "no more", "nothing else", "we are done", "o dabo", "bye", "goodbye", "good bye")
CLOSING_FILLERS = FILLERS | {"thats", "that", "is", "all", "i", "am", "im", "good", "fine", "nothing", "else", "more", "for", "now", "we", "are",
                              "done", "it", "my", "friend", "sofa", "bye", "goodbye", "good", "o", "dabo", "ko", "si", "rara", "no", "nope", "nah",
                              "will", "be", "that", "thanks", "thank", "you", "very", "much", "ok", "okay", "yes"}
FAREWELLS = {"bye", "goodbye", "dabo"}


def closing(transcript: str, asked_more: bool) -> bool:
    """True when the caller is saying they are finished. Only after "anything else?" does a plain "no" count; a goodbye counts any time."""
    words = clean(transcript).split()
    if not words or len(words) > 8:
        return False
    if any(w in FAREWELLS for w in words) and all(w in CLOSING_FILLERS for w in words):
        return True
    if not asked_more or not all(w in CLOSING_FILLERS for w in words):
        return False
    text = " ".join(words)
    return any(text == p or text.startswith(p + " ") or f" {p} " in f" {text} " for p in CLOSING)
