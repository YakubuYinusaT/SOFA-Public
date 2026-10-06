"""The strict yes/no safety net, and the rule that only what the caller said can fill in a detail."""

import pytest

from sofa.gateway.answers import quick_answer
from sofa.gateway.manager import GatewayManager
from sofa.gateway.understanding import Understanding


@pytest.mark.parametrize("said, answer", [
    ("Yes", "yes"), ("yes please", "yes"), ("Okay go ahead", "yes"), ("That's right", "yes"), ("Yeah", "yes"),
    ("Beeni", "yes"), ("B\u1eb9\u0301\u1eb9\u0300 ni", "yes"),
    ("No", "no"), ("No, that's wrong", "no"), ("Nope, not that one", "no"), ("No thanks", "no"), ("Rara", "no"), ("R\u00e1r\u00e1 o", "no"),
])
def test_a_plain_yes_or_no_is_recognised(said, answer):
    assert quick_answer(said) == answer


@pytest.mark.parametrize("said", [
    "ok three thousand naira",          # a yes word, but it is an amount
    "no I want to send to Tunde",       # a no, but also a new request
    "yes and also check my balance",
    "send the usual to mama",
    "yes no",                            # both: leave it to the model
    "hello",
    "",
])
def test_anything_more_than_a_yes_or_no_is_left_to_the_model(said):
    assert quick_answer(said) is None


def test_a_recipient_the_caller_never_said_is_dropped():
    u = Understanding(action="bank.transfer", beneficiary="Hauwa Bello", biller="electricity", amount_naira=3000)
    GatewayManager.ground(u, "okay go ahead", None)
    assert u.beneficiary is None and u.biller is None and u.amount_naira == 3000


def test_a_recipient_the_caller_did_say_is_kept_whatever_the_capital_letters():
    u = Understanding(action="bank.transfer", beneficiary="Mama")
    GatewayManager.ground(u, "send some money to mama", None)
    assert u.beneficiary == "Mama"


def test_the_other_readings_of_the_audio_count_too():
    u = Understanding(action="bank.transfer", beneficiary="Hauwa")
    GatewayManager.ground(u, "send to how what", ["send money to Hauwa"])
    assert u.beneficiary == "Hauwa"


from sofa.gateway.answers import closing


@pytest.mark.parametrize("said", ["No", "No thanks", "no that is all", "That's all", "Nothing else, thank you", "No, thank you, bye", "Rara", "Goodbye", "bye bye"])
def test_the_caller_closing_the_call_after_anything_else(said):
    assert closing(said, asked_more=True)


@pytest.mark.parametrize("said", ["No, check my balance", "no I want to send money to Tunde", "yes check my balance", "send three thousand", ""])
def test_a_request_is_never_mistaken_for_closing(said):
    assert not closing(said, asked_more=True)


def test_a_plain_no_closes_only_after_anything_else_but_goodbye_always_does():
    assert not closing("no", asked_more=False)  # a no to some other question
    assert closing("goodbye", asked_more=False)
