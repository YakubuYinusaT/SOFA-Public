"""Rule-based stand-in for the N-ATLaS intent JSON. Deterministic test double, not a model.

It reads the same prompt shape the backend sends to vLLM: a system message with `STAGE:`
and `MODE:` lines, and the caller transcript as the user message.
"""

import re

from sofa.textutil import FILLERS, NUMBER_WORDS, UNIT_LOOKUP, canonical_unit, clean, parse_number

CONFIRM = {"yes", "yeah", "yep", "yup", "ok", "okay", "correct", "sure", "right", "please do"}
CONFIRM_PHRASES = ("thats right", "place it", "yes please", "go ahead", "do it", "confirm", "bee ni", "beeni", "abeg go ahead")
DENY_WORDS = {"no", "nope", "nah", "nothing", "wrong", "rara"}
DENY_PHRASES = ("thats all", "that is all", "no thanks", "thats wrong", "not correct", "nothing else")
MODIFY_VERBS = ("remove", "make it", "change", "reduce", "set it")


def _empty(intent: str = "unknown") -> dict:
    return {
        "intent": intent,
        "items": [],
        "customer_name": None,
        "delivery_note": None,
        "needs_clarification": False,
        "clarification_topic": None,
        "say": "",
    }


def _quantity_ok(idx: int, words: list[str]) -> bool:
    """A number word counts as a quantity at the start, next to a unit, or in a tiny segment.
    'indomie big one' keeps 'one' as part of the name."""
    if len(words) <= 2 or idx == 0:
        return True
    near = [words[i] for i in (idx - 1, idx + 1) if 0 <= i < len(words)]
    return any(w in UNIT_LOOKUP for w in near)


def extract_items(text: str, action: str | None = None) -> list[dict]:
    items = []
    for segment in re.split(r"\b(?:and|plus)\b", clean(text)):
        words = [w for w in segment.split() if w not in FILLERS]
        if not words:
            continue
        qty = unit = None
        keep = []
        for i, w in enumerate(words):
            if qty is None and parse_number(w) is not None and _quantity_ok(i, words):
                qty = parse_number(w)
            elif unit is None and w in UNIT_LOOKUP:
                unit = canonical_unit(w)
            else:
                keep.append(w)
        name = " ".join(keep)
        if not name and qty is None and unit is None:
            continue
        items.append({"spoken_name": name, "quantity": qty, "unit": unit, "price": None, "action": action})
    return items


def parse(transcript: str, stage: str | None = None, mode: str = "customer", pending: str | None = None) -> dict:
    raw = transcript
    text = clean(transcript)
    out = _empty()
    if not text:
        return out

    m = re.search(r"\b(?:my name is|i am|im|this is)\s+([a-z]+)", text)
    if m and m.group(1) not in {"looking", "calling"}:
        out["customer_name"] = m.group(1).capitalize()
        text = (text[: m.start()] + " " + text[m.end():]).strip()
        raw = text

    words = set(text.split())

    def has(*phrases: str) -> bool:
        return any(p in text for p in phrases)

    if has("talk to the owner", "speak to", "talk to a", "the owner", "human", "manager"):
        return {**out, "intent": "speak_to_human"}
    if mode == "owner" and has("how did we do", "how was today", "how is today", "todays sales", "sales today",
                               "how many orders", "daily report", "report"):
        return {**out, "intent": "daily_report"}
    if mode == "owner" and re.search(r"\b(add|restock|price)\b", text):
        items = extract_items(text.split("price")[0], "add")
        price = re.search(r"price\s+(?:is\s+)?(?:now\s+)?(\d+)", text)
        for it in items:
            it["price"] = float(price.group(1)) if price else None
        return {**out, "intent": "stock_update", "items": items}
    if has("how do i apply", "how much fertilizer", "how much npk", "how much urea", "when should i apply", "when do i apply", "how do i use",
           "how to use", "how should i use", "should i spray", "which weedicide", "which fertilizer", "what fertilizer", "advice", "recommend",
           "how many bags should", "how often"):
        return {**out, "intent": "ask_advice", "items": extract_items(text)}
    if has("cancel"):
        return {**out, "intent": "cancel_order"}
    if has("received my payment", "have you received", "did you get my payment", "my payment", "i have paid", "i paid"):
        return {**out, "intent": "payment_status"}
    if has("where is my order", "track", "my order", "where is it"):
        return {**out, "intent": "track_order"}
    if has("same as last time", "same again", "last time", "repeat"):
        return {**out, "intent": "repeat_last_order"}

    if stage == "await_address" and not has("cancel"):
        return {**out, "intent": "give_address", "delivery_note": raw.strip()}

    if text in CONFIRM or has(*CONFIRM_PHRASES):
        return {**out, "intent": "confirm"}
    if text in DENY_WORDS or (words & DENY_WORDS and len(words) <= 3) or text.split()[0] in DENY_WORDS or has(*DENY_PHRASES):
        return {**out, "intent": "deny"}

    if has("do you have", "have you got", "is there", "available"):
        return {**out, "intent": "check_availability", "items": extract_items(text)}
    if has("how much", "price of", "what is the price", "cost of"):
        return {**out, "intent": "ask_price", "items": extract_items(text)}

    if out["customer_name"] and not extract_items(text):
        return {**out, "intent": "give_name"}

    order_words = ("want", "need", "give", "send", "add", "get", "bring", "buy", "order", "take", "remove", "make it", "change")
    if pending in (None, "none") and not (has(*order_words) or words & set(NUMBER_WORDS) or words & set(UNIT_LOOKUP) or any(w.isdigit() for w in words)):
        return out  # a bare noun phrase is only an answer when a clarification is pending
    action = "remove" if has("remove") else "set" if has("make it", "change it to", "set it") else "add"
    items = extract_items(text, action)
    if items:
        intent = "modify_order" if has(*MODIFY_VERBS) else "place_order"
        return {**out, "intent": intent, "items": items}
    return out


DOMAIN_WORDS = {
    "human": {"human", "person", "agent", "someone", "representative", "operator"},
    "lookup": {"google", "search", "weather", "news", "lookup"},
    "commerce": {"buy", "order", "shop", "store", "jumia", "konga", "carton", "indomie", "cartons", "purchase", "price"},
    "banking": {"bank", "account", "balance", "transfer", "transaction", "airtime", "bill", "bills", "statement", "card", "demobank", "pin", "loan", "money", "complain", "complaint", "send", "payment", "electricity", "dstv", "gotv", "ranse", "owo"},
}
DOMAIN_PHRASES = {
    "human": ("customer care", "customer service", "speak to", "talk to a"),
    "lookup": ("look up", "find out", "look something up", "what is", "who is"),
    "commerce": ("how much",),
    "banking": ("send money", "open an account"),
}


def classify_domain(user: str) -> str:
    """Front-desk routing stand-in: keyword match, first hit wins in this order. Not a model."""
    m = re.search(r"TRANSCRIPT:\s*(.*)", user)
    text = clean(m.group(1) if m else user)
    words = set(text.split())
    if words & {"airtime", "statement", "balance", "transfer", "bank"}:  # unmistakably banking, even next to "buy"
        return "banking"
    for domain in ("human", "lookup", "commerce", "banking"):
        if words & DOMAIN_WORDS[domain] or any(p in text for p in DOMAIN_PHRASES[domain]):
            return domain
    if words & {"hello", "hi", "hey", "thanks", "thank", "bye", "morning", "afternoon", "evening"} or "how are you" in text:
        return "chat"  # no request in it: a greeting or thanks
    return "unclear"


TOOL_WORDS = (
    ("bank.resume_registration", ("i started", "could not finish", "continue my registration", "continue my application", "resume my")),
    ("bank.application_status", ("my application", "application status", "registration status", "verification fail")),
    ("bank.account_requirements", ("what do i need", "requirements", "documents")),
    ("bank.open_account", ("open an account", "open account", "new account", "create an account", "open a bank account")),
    ("bank.funding_instructions", ("fund my account", "fund the account", "how do i fund")),
    ("bank.statement", ("statement",)),
    ("bank.transfer", ("transfer", "send ", "wire", "ranse")),
    ("bank.buy_airtime", ("airtime", "recharge")),
    ("bank.pay_bill", ("pay bill", "pay my bill", "electricity", "dstv", "bill")),
    ("bank.block_card", ("block my card", "block the card", "lost my card", "block card")),
    ("bank.transaction_status", ("did my payment", "transaction status", "failed transaction")),
    ("bank.complaint", ("complain", "complaint")),
    ("bank.balance", ("balance", "how much do i have", "how much is in")),
    ("bank.product_info", ("fees", "charges", "branch")),
)


SMALL = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
BILLERS = {"electricity": ("electricity", "light", "phcn", "ikedc", "nepa"), "cable tv": ("dstv", "gotv", "startimes", "cable"),
           "water": ("water",), "internet": ("internet", "wifi")}
NOT_PEOPLE = {"send", "pay", "buy", "check", "make", "do", "get", "see", "transfer", "my", "the", "a", "me", "this", "that"}


def words_to_number(words: list[str]) -> int | None:
    """'ten thousand' -> 10000, 'two hundred and fifty' -> 250. A test double for what the model does."""
    total = current = 0
    seen = False
    for w in words:
        if w in SMALL:
            current, seen = current + SMALL[w], True
        elif w in TENS:
            current, seen = current + TENS[w], True
        elif w == "hundred":
            current, seen = (current or 1) * 100, True
        elif w == "thousand":
            total, current, seen = total + (current or 1) * 1000, 0, True
        elif w == "and" and seen:
            continue
    return (total + current) if seen else None


def extract_bank_params(user: str, system: str) -> dict:
    """What the caller said for a banking operation: amount, who, which bill, 'the usual'. Stand-in, not a model."""
    m = re.search(r"TRANSCRIPT:\s*(.*)", user)
    text = clean(m.group(1) if m else user)
    focus = (re.search(r"^FOCUS:\s*(.*)$", system, re.M) or [None, ""])[1].strip()
    amount = None
    num = re.search(r"\d+(?:\.\d+)?", text)
    if num:
        amount = float(num.group())
        if re.search(rf"{re.escape(num.group())}\s*thousand", text):
            amount *= 1000
    else:
        amount = words_to_number(text.split())
    beneficiary = None
    for to in re.finditer(r"\b(?:to|si) (?:my |the )?([a-z]+(?: [a-z]+)?)", text):  # "I want to send money to mama": skip "to send"
        if to.group(1).split()[0] not in NOT_PEOPLE:
            beneficiary = " ".join(w for w in to.group(1).split() if w not in {"for", "please", "now", "today", "again", "from"})
            break
    if not beneficiary and focus in ("beneficiary", "beneficiary_choice"):
        beneficiary = " ".join(w for w in text.split() if w not in NOT_PEOPLE | {"please", "to", "it", "them"}) or None
    biller = next((name for name, keys in BILLERS.items() if any(k in text.split() for k in keys)), None)
    return {"amount_naira": amount, "beneficiary": beneficiary, "biller": biller,
            "usual": "usual" in text or "same as last time" in text}


def extract_identity(raw: str, system: str) -> dict:
    """A name or an address, exactly as said, for the account-opening questions. Stand-in, not a model."""
    focus = (re.search(r"^FOCUS:\s*(.*)$", system, re.M) or [None, ""])[1].strip()
    named = re.search(r"my name is ([A-Za-z' -]+)", raw, re.I)
    return {"full_name": named.group(1).strip() if named else (raw.strip() if focus == "full_name" else None),
            "address": raw.strip() if focus == "address" else None}


YORUBA_WORDS = {"jowo", "ranse", "bawo", "kini", "owo", "abeg"}
ENGLISH_WORDS = {"send", "money", "balance", "transfer", "buy", "bank", "airtime", "please", "bill", "statement"}


def detect_language(raw: str) -> tuple[str, bool]:
    """(base language, mixed) from the words: a test double for what the model works out from the whole sentence."""
    words = set(clean(raw).split())
    if re.search(r"[\u1eb9\u1ecd\u1e63]", raw.lower()) or words & YORUBA_WORDS:
        return "yo", bool(words & ENGLISH_WORDS)
    return "en", False


def understand(system: str, user: str) -> dict:
    """One answer for the whole turn: language mix, service, tool, details and yes/no. Stand-in, not a model."""
    m = re.search(r"TRANSCRIPT:\s*(.*)", user)
    raw = m.group(1) if m else user
    tool = classify_tool(user)
    domain = classify_domain(user)
    if tool != "none":  # a banking tool was picked, so it is a banking request, whatever else the words suggest ("what is my application status")
        domain = "banking"
    asked = re.search(r"^ASKED:\s*(.*)$", system, re.M)
    nothing_asked = not asked or asked.group(1).strip() == "nothing"
    language, mixed = detect_language(raw)
    return {"language": language, "mixed": mixed, "action": domain if tool == "none" else tool,
            "answer": None if nothing_asked else classify_answer(user), **extract_bank_params(user, system), **extract_identity(raw, system),
            "choice": None if nothing_asked else raw}


def wording(system: str) -> dict:
    """The model's wording step, as a double: say the reference sentence as it is."""
    m = re.search(r"^REFERENCE:\s*(.*)$", system, re.M)
    return {"say": m.group(1).strip() if m else ""}


def classify_tool(user: str) -> str:
    """Which banking operation did the caller ask for? Keyword stand-in, not a model."""
    m = re.search(r"TRANSCRIPT:\s*(.*)", user)
    text = clean(m.group(1) if m else user)
    for tool_id, phrases in TOOL_WORDS:
        if any(p in text for p in phrases):
            return tool_id
    return "none"


def classify_answer(user: str) -> str:
    """Yes / no / other for a question the assistant just asked. Stand-in, not a model."""
    m = re.search(r"TRANSCRIPT:\s*(.*)", user)
    text = clean(m.group(1) if m else user)
    words = set(text.split())
    if text in CONFIRM or any(p in text for p in CONFIRM_PHRASES) or (words & CONFIRM and len(words) <= 3):
        return "yes"
    if text in DENY_WORDS or any(p in text for p in DENY_PHRASES) or (words & DENY_WORDS and len(words) <= 3):
        return "no"
    return "other"


def rerank(system: str, user: str) -> str:
    """Pick the candidate with the most word overlap with the spoken phrase, else 'none'."""
    phrase = re.search(r'PHRASE:\s*"(.*)"', user)
    spoken = set(clean(phrase.group(1)).split()) if phrase else set()
    best, best_score = "none", 0
    for line in user.splitlines():
        m = re.match(r"\s*(\d+)\.\s*(.+)", line)
        if not m:
            continue
        score = len(spoken & set(clean(m.group(2)).split()))
        if score > best_score:
            best, best_score = m.group(1), score
    return best


def search_answer(system: str) -> str:
    """The model's search answer, as a double: say the first snippet as it is."""
    m = re.search(r"^SNIPPET 1:\s*(.*)$", system, re.M)
    return m.group(1).strip() if m else "I could not find that."
