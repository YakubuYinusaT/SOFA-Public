"""What the model is told when it reads a caller's turn at a shop: the intents, and worked examples.

The first run on the real model (N-ATLaS, GPU smoke test) answered "Do you have Peak milk tin?", "Where is my order?" and "No, that's wrong"
all with the first intent in the list. Descriptions of each intent plus examples that show every field fixed that for the banking
front desk, so the shop gets the same treatment.
"""

import json

INTENTS = (
    "- check_availability: asks whether the shop has something (\"do you have ...?\")\n"
    "- ask_price: asks how much something costs\n"
    "- place_order: wants to buy something (items with quantity and unit)\n"
    "- modify_order: changes the order (add more, remove, change the number)\n"
    "- confirm: says yes to Sofa's question\n"
    "- deny: says no to Sofa's question, or that something is wrong\n"
    "- repeat_last_order: wants the same as last time\n"
    "- track_order: asks where their order is\n"
    "- payment_status: asks whether the shop got their payment\n"
    "- cancel_order: cancels the order\n"
    "- speak_to_human: asks for the owner or a person\n"
    "- give_name / give_address: says their name or their delivery address\n"
    "- ask_advice: asks HOW or WHEN to use a product, how much to apply, or what suits their crop, animal or problem\n"
    "- unknown: none of the above\n"
)


def _example(said: str, intent: str, items: list[dict] | None = None, **extra) -> str:
    out = {"intent": intent, "items": items or [], "customer_name": None, "delivery_note": None, "needs_clarification": False,
           "clarification_topic": None, "say": ""}
    out.update(extra)
    return f'CALLER: {said}\n{json.dumps(out, ensure_ascii=False)}'


def _item(name: str, quantity=None, unit=None, action="add") -> dict:
    return {"spoken_name": name, "quantity": quantity, "unit": unit, "price": None, "action": action}


EXAMPLES = "\n".join([
    _example("Do you have Peak milk tin?", "check_availability", [_item("Peak milk tin")]),
    _example("How much is a carton of Indomie?", "ask_price", [_item("Indomie", None, "carton")]),
    _example("Give me two bags of urea", "place_order", [_item("urea", 2, "bag")]),
    _example("Abeg add one more bag", "modify_order", [_item("", 1, "bag")]),
    _example("Yes please", "confirm"),
    _example("No, that's wrong", "deny"),
    _example("Where is my order?", "track_order"),
    _example("Did you receive my payment?", "payment_status"),
    _example("Let me talk to the owner", "speak_to_human"),
    _example("I live at 12 Allen Avenue Ikeja", "give_address", delivery_note="12 Allen Avenue Ikeja"),
    _example("My name is Musa", "give_name", customer_name="Musa"),
    _example("When should I apply urea on my maize?", "ask_advice", [_item("urea")]),
    _example("How much NPK should I use for one acre?", "ask_advice", [_item("NPK")]),
    _example("Which weedicide is good for my farm?", "ask_advice"),
    _example("Hello, I wanted something", "unknown"),
])


def block(topics: list[str]) -> str:
    """The intent list, the shop's advice topics (so a farming question is recognised as one) and the worked examples."""
    advice = ("ADVICE THE SHOP GIVES: " + "; ".join(topics) + "\n") if topics else ""
    return f"INTENTS:\n{INTENTS}{advice}EXAMPLES (every field is always present):\n{EXAMPLES}\n"
