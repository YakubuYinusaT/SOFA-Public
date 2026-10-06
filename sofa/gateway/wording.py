"""Letting the model word SOFA's replies, in the caller's own language mix, without letting it touch a fact.

The backend decides WHAT is said (the reply key and the real figures and names, from the bank or the shop). The model
only writes the sentence around them, using placeholders such as {amount} and {who}. The sentence is accepted only if it
keeps every placeholder, makes up no number of its own, asks the same number of questions as the reference, and stays
short. Anything else, or any failure, and the caller hears the reference wording instead. Security prompts (PIN and code
requests) are never model-written.
"""

from pydantic import BaseModel

from ..config import LANGUAGE_NAMES
from ..dialogue import templates
from ..dialogue.validator import PLACEHOLDER, check_say, fill


# A sentence that carries a figure, a name or any text that came from the bank is never model-worded. On the first GPU run the model
# invented a balance ("42,500 naira") for "what is my balance", and garbled or answered instead of rewording the transfer and
# read-back sentences. The checker refused every one, but the safest rule is not to ask: those sentences stay exact templates
# (in the caller's language once translated), and the model words only the conversation around them.
SENSITIVE = templates.VALUE_KEYS | {"who", "full_name", "date", "ref", "last4", "summary", "state", "text", "people", "answer"}


def carries_facts(reference: str) -> bool:
    return bool({m.group(1) for m in PLACEHOLDER.finditer(reference)} & SENSITIVE)


class Wording(BaseModel):
    say: str = ""


# The answer format makes `say` required and non-trivial. Left optional, the real model answered with an empty string whenever the
# reference sentence contained placeholders (first GPU run); it is the same lesson as the understanding call.
SCHEMA = {"type": "object", "properties": {"say": {"type": "string", "minLength": 10}}, "required": ["say"], "additionalProperties": False}

EXAMPLES = (
    "EXAMPLES (the placeholders stay exactly as written):\n"
    "REFERENCE: Done. {amount} sent to {who}. Your balance is now {balance}. Is there anything else I can help with?\n"
    '{"say": "All done. {amount} has gone to {who}, and your balance is now {balance}. Anything else I can help with?"}\n'
    "REFERENCE: How much would you like to send?\n"
    '{"say": "Okay, how much do you want to send?"}\n'
    "REFERENCE: Your {bank} balance is {balance}. Is there anything else I can help with?\n"
    '{"say": "Your {bank} balance na {balance}. Anything else I fit help you with?"}\n'
)


def prompt(key: str, reference: str, lang: str, mixed: bool, placeholders: list[str], questions: int, max_sentences: int,
           caller_said: str = "") -> str:
    mix = (" The caller mixes languages, so mix them the same natural way (for example Yoruba or Pidgin with English words)."
           if mixed else "")
    return (
        f"WORDING. You are Sofa, a warm, brief voice assistant on a phone call in Nigeria. Say the MEANING below in "
        f"{LANGUAGE_NAMES.get(lang, 'English')}.{mix} Sound like a helpful person, not a script.\n"
        f"MEANING: {templates.CONTEXT.get(key, '')}\n"
        f"REFERENCE: {reference}\n"
        f"RULES: keep every placeholder exactly as written, {', '.join('{' + p + '}' for p in placeholders) or 'none needed'}: the system fills "
        f"them with real figures and names. Write no digits and no amounts yourself. Ask exactly {questions} question(s). "
        f"At most {max_sentences} sentences. Say nothing the REFERENCE does not say. Do NOT answer the caller and do NOT invent any "
        "figure: you are only putting the REFERENCE into natural words.\n"
        + (f'THE CALLER JUST SAID (for tone and language only): "{caller_said[:200]}"\n' if caller_said else "")
        + EXAMPLES
    )


def accept(say: str, reference: str, facts: dict) -> tuple[str, str] | None:
    """(raw say, filled say) if it is safe to speak, else None."""
    say = (say or "").strip()
    required = {m.group(1) for m in PLACEHOLDER.finditer(reference)}
    if not say or not required <= {m.group(1) for m in PLACEHOLDER.finditer(say)}:
        return None  # a fact the backend meant to say would be missing
    questions = reference.count("?")
    if questions > 1:
        return None
    sentences = len([s for s in PLACEHOLDER.sub("x", reference).replace("?", ".").replace("!", ".").split(".") if s.strip()])
    filled = check_say(say, facts, expect_question=questions == 1, max_words=max(25, len(reference.split()) + 12),
                       max_sentences=max(2, sentences))
    return (say, filled) if filled else None


async def write(llm, key: str, reference: str, lang: str, mixed: bool, caller_said: str, facts: dict) -> tuple[str, str] | None:
    placeholders = sorted({m.group(1) for m in PLACEHOLDER.finditer(reference)})
    questions = reference.count("?")
    sentences = max(2, len([s for s in PLACEHOLDER.sub("x", reference).replace("?", ".").replace("!", ".").split(".") if s.strip()]))
    # The caller's words go inside the instructions, not in the message the model treats as the conversation: put there, the model
    # answered the caller ("I'm sorry, I didn't catch that") instead of rewording the sentence.
    obj, _ = await llm.extract(Wording, prompt(key, reference, lang, mixed, placeholders, questions, sentences, caller_said or ""),
                               "Write the reply now.", json_schema=SCHEMA)
    return accept(obj.say, reference, facts)
