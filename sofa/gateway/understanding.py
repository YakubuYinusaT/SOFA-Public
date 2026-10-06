"""One model call per turn that works out everything the caller meant, in whatever language mix they used.

Callers speak English, Yoruba, Pidgin, or several in one sentence. Instead of keyword rules and a chain of narrow questions,
the model is shown the registered tools and where the conversation is, and returns one structured answer: what the caller
wants (a single `action`), whether they answered a question SOFA had just asked, the language, and the details they gave.
The backend then validates that answer against the tool registry and runs it. The model decides what was meant; it never
supplies a fact.

What the first run on the real GPU taught us (N-ATLaS 8B, scripts/gpu_smoke.py):
  * every field must be REQUIRED in the answer format, or the model skips most of them (it filled 4 to 6 of 12);
  * one flat `action` decision works far better than a `domain` and then a `tool` (the model picked the domain badly first);
  * a dozen example sentences, including Yoruba and Pidgin, are what make Yoruba amounts and yes/no answers come out right;
  * every field costs about 0.1 s of answer time, so only the fields a situation needs are asked for.
"""

import json

from pydantic import BaseModel, model_validator

SPECIAL = ["banking", "commerce", "lookup", "chat", "human", "unclear"]
CORE_FIELDS = ["action", "answer", "language", "mixed", "amount_naira", "beneficiary", "biller", "usual"]


class Understanding(BaseModel):
    action: str = "unclear"   # a registered tool id (bank.transfer ...), or commerce | lookup | chat | human | unclear
    answer: str | None = None  # yes | no | other: only when SOFA had just asked a question
    language: str = ""        # the base language of the sentence: en | yo ("" if unsure)
    mixed: bool = False       # two or more languages share the sentence
    amount_naira: float | None = None
    beneficiary: str | None = None  # who to send to, exactly as they said it (a name or a nickname)
    biller: str | None = None
    usual: bool = False       # "the usual", "same as last time"
    choice: str | None = None  # which option they picked when SOFA offered several, as they said it (bank, ordinal, last digits)
    full_name: str | None = None  # their own full name, when they say it (opening an account)
    address: str | None = None    # their home address, when they say it
    # worked out from `action`, never asked of the model:
    domain: str = "unclear"   # banking | commerce | lookup | chat | human | unclear
    tool: str | None = None   # the registered tool id, for a banking action

    @model_validator(mode="after")
    def _derive(self):
        if self.answer in ("none", ""):
            self.answer = None
        self.rederive()
        return self

    def rederive(self) -> None:
        if "." in self.action:
            self.domain, self.tool = "banking", self.action
        else:
            self.domain, self.tool = (self.action if self.action in SPECIAL else "unclear"), None

    def settle(self, registered: set[str]) -> None:
        """Only a registered operation may stand. A banking action that is not on the registry is treated as 'something at the
        bank, nothing specific', so the invented name cannot come back later from the saved action."""
        if "." in self.action and self.action not in registered:
            self.action = "banking"
        self.rederive()


def extra_fields(st: dict) -> tuple[str, ...]:
    """The extra fields this moment needs on top of the core ones: a name, an address, or which option they picked."""
    pending = st.get("pending") or {}
    wanted = pending.get("field") or pending.get("expect") or ""
    if wanted in ("full_name", "address"):
        return (wanted,)
    if wanted == "beneficiary_choice" or str(pending.get("kind", "")).startswith(("choose_", "shop_choose", "onboard_bank")):
        return ("choice",)
    return ()


def schema(enabled: tuple[str, ...], tool_ids: list[str], extra: tuple[str, ...] = ()) -> dict:
    """The answer format. Every field is required, and the choices the model may use are written into it, so the output
    cannot name an operation that does not exist."""
    props = Understanding.model_json_schema()["properties"]
    wire = {name: props[name] for name in [*CORE_FIELDS, *extra]}
    wire["action"] = {"enum": [*tool_ids, *SPECIAL], "type": "string"}
    wire["answer"] = {"enum": ["yes", "no", "none"], "type": "string"}
    wire["language"] = {"enum": list(enabled), "type": "string"}
    return {"type": "object", "properties": wire, "required": list(wire), "additionalProperties": False}


def _example(action: str, said: str, *, lang: str = "en", mixed: bool = False, amount=None, who=None, answer: str = "none",
             asked: str | None = None, usual: bool = False) -> str:
    row = {"action": action, "answer": answer, "language": lang, "mixed": mixed, "amount_naira": amount, "beneficiary": who,
           "biller": None, "usual": usual}
    return f"Caller: {said}" + (f'  (SOFA had just asked: "{asked}")' if asked else "") + "\n" + json.dumps(row, ensure_ascii=False)


_Q = "That is 500 naira to Ngozi. Shall I go ahead?"
_HOW = "How much would you like to send?"
_WHO = "Who would you like to send it to?"
EXAMPLES = "\n".join([
    # what they want (names and amounts vary on purpose: the model copies whatever the examples repeat)
    _example("bank.transfer", "Please send five hundred naira to my brother Musa", amount=500, who="Musa"),
    _example("bank.transfer", "I need to send money to my sister Ngozi", who="Ngozi"),
    _example("bank.transfer", "Send the usual to Mama", who="Mama", usual=True),
    _example("bank.balance", "How much do I have in my account"),
    _example("bank.buy_airtime", "Abeg buy airtime for me, five hundred naira", mixed=True, amount=500),
    _example("bank.balance", "Mo fẹ́ mọ̀ iye owó tó wà nínú account mi", lang="yo", mixed=True),
    _example("bank.transfer", "Fi ẹgbẹ̀rún márùn-ún ránṣẹ́ sí Tunde", lang="yo", amount=5000, who="Tunde"),
    _example("bank.transfer", "Ranse egberun meji si Chidi", lang="yo", amount=2000, who="Chidi"),
    _example("commerce", "I wan buy bread and egg from the shop", mixed=True),
    _example("commerce", "Where is my order"),
    _example("commerce", "Do you have Peak milk"),
    _example("chat", "Good morning o"),
    _example("chat", "Thank you very much"),
    _example("human", "I want to speak to a person"),
    _example("lookup", "What is the capital of Ghana"),
    # answering a question SOFA just asked (a bare yes or no is the answer, not a request)
    _example("unclear", "Yes please", answer="yes", asked=_Q),
    _example("unclear", "Yeah that's right", answer="yes", asked=_Q),
    _example("unclear", "No, that is wrong", answer="no", asked=_Q),
    _example("unclear", "No no no", answer="no", asked=_Q),
    _example("unclear", "Bẹ́ẹ̀ ni, ó tọ̀nà", lang="yo", answer="yes", asked=_Q),
    _example("unclear", "Beeni", lang="yo", answer="yes", asked=_Q),
    _example("unclear", "Rárá", lang="yo", answer="no", asked=_Q),
    _example("unclear", "Rara o", lang="yo", answer="no", asked=_Q),
    # answering for a missing detail: only what was said is filled in
    _example("bank.transfer", "Three thousand naira", amount=3000, asked=_HOW),
    _example("bank.transfer", "Egberun mefa", lang="yo", amount=6000, asked=_HOW),
    _example("bank.transfer", "To Ngozi", who="Ngozi", asked=_WHO),
])


def prompt(gw) -> str:
    """The instructions and the state of the conversation, for the model, for a live call."""
    from . import links

    return build(gw.st, gw.svc.gateway.tools, [l.provider for l in links.active(gw.db, gw.customer.id, "banking")])


def build(st: dict, tools, linked: list[str]) -> str:
    """The instructions and the state of the conversation, for the model. Never contains a PIN, a code or an account figure."""
    tool_lines = "\n".join(
        f"- {t.tool_id}: {t.description}" + (f" (needs: {', '.join(t.required)})" if t.required else "") for t in tools.for_domain("banking")
    )
    pending = st.get("pending") or {}
    op, ob = st.get("bank_op"), st.get("onboarding")
    doing = "nothing"
    if ob:
        doing = f"opening an account; just asked for: {pending.get('field') or ob.get('collecting') or 'a yes or no'}"
    if op:
        got = [k for k in ("amount_kobo", "beneficiary", "biller", "details") if op.get("params", {}).get(k)]
        doing = f"{op['tool']}; already have: {', '.join(got) or 'nothing yet'}"
    history = " | ".join((st.get("said") or [])[-2:]) or "none"
    return (
        "UNDERSTAND. You are the language and intent layer of Sofa, a voice assistant answering a phone call in Nigeria. Callers speak "
        "English, Yoruba, Nigerian Pidgin, or a mix inside one sentence. Work out what the caller means and answer with JSON only, "
        "with every field present.\n"
        "action: what the caller wants. Exactly one of:\n" + tool_lines + "\n"
        "- commerce: buying something from a shop, prices, orders\n- lookup: asking to look up general information\n"
        "- banking: wants something from their bank but no operation above fits yet (for example 'I want to make a transaction')\n"
        "- chat: only a greeting, thanks or small talk\n- human: asks for a person\n- unclear: none of the above, or only an answer to a question\n"
        "answer: yes or no if the caller is answering the question in ASKED, else none.\n"
        "language: the base language of the sentence (the one its grammar is in), not of borrowed words. mixed: true if two languages share the sentence.\n"
        "amount_naira: a plain number of naira whatever the language it was said in. beneficiary, biller, full_name, address, choice: exactly as said, else null. "
        "usual: true for 'the usual' or 'same as last time'. Never guess: null if not said.\n"
        f"EXAMPLES:\n{EXAMPLES}\n"
        f"BANK IN USE: {st.get('bank') or 'none'}\nASKED: {pending.get('question') or 'nothing'}\n"
        f"FOCUS: {pending.get('expect') or pending.get('field') or 'none'}\nDOING: {doing}\nRECENT: {history}"
    )
