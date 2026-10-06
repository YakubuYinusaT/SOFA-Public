"""Commerce behind the gateway: choosing the shop, then the same ordering dialogue, without the shop flow's own number."""

import dataclasses

from sqlalchemy import select

from sofa.models import Call, CallTurn, Handoff, Order
from tests.test_gateway import gw_app, gw_call, gw_client, gw_db  # noqa: F401  (fixtures)
from tests.test_call_flow import SPEC_CONVERSATION

MUSA = "+2348055550001"


def test_naming_the_shop_starts_shopping_there(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store"])
    assert "You are shopping at CI Store" in replies[1]
    assert gw_db.scalar(select(Call)).merchant_id is not None  # the call now belongs to that shop


def test_the_whole_order_runs_through_the_gateway(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store", *SPEC_CONVERSATION], caller=MUSA)
    assert "Indomie Super Pack at 350 naira" in replies[2]
    assert "16,400 naira in total" in replies[8]
    assert "sent the account number" in replies[9]
    last = gw_db.scalars(select(CallTurn).order_by(CallTurn.seq.desc())).first()
    assert "anything else I can help with" in last.reply_text  # spoken as a second clip: SOFA never ends the call itself
    order = gw_db.scalar(select(Order))
    assert order.status == "awaiting_payment" and order.total_kobo == 1_640_000
    assert order.merchant_id == gw_db.scalar(select(Call)).merchant_id


def test_the_call_stays_open_after_an_order(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store", *SPEC_CONVERSATION, "check my bank balance"], caller=MUSA)
    assert len(replies) == 11 and "bank" in replies[10]  # SOFA did not hang up, and the caller moved to another service


def test_the_order_can_be_said_in_one_breath(gw_call, gw_db):
    replies = gw_call(["two cartons of indomie super pack from CI Store"])
    assert "2 cartons of Indomie Super Pack" in replies[1]


def test_with_one_shop_connected_and_none_named_it_confirms_the_shop(gw_call, gw_db):
    replies = gw_call(["I want to buy something", "yes"])
    assert "I can order from CI Store for you" in replies[1]
    assert "You are shopping at CI Store" in replies[2]


def test_declining_the_only_shop(gw_call, gw_db):
    replies = gw_call(["I want to buy something", "no thanks"])
    assert "No problem" in replies[2]
    assert gw_db.scalar(select(Call)).merchant_id is None


def test_a_bare_product_name_is_understood_once_a_shop_is_chosen(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store", "peak milk tin"])
    assert "How many" in replies[2]  # it matched the product and needs a quantity


def test_the_shop_flow_never_promises_a_call_back_from_the_owner(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store", "blah blah", "hmm hmm"])
    handoff_reply = replies[3]
    assert "pass the details to CI Store" in handoff_reply and "follow up" in handoff_reply and "anything else" in handoff_reply
    assert "owner" not in handoff_reply and "Goodbye" not in handoff_reply
    assert gw_db.scalar(select(Handoff)).service_domain == "commerce"


def test_changing_to_banking_after_shopping(gw_call, gw_db):
    replies = gw_call(["I want to buy something on CI Store", "actually check my bank balance"])
    assert "bank" in replies[2]


def test_a_blocked_tool_is_refused_before_the_shop_runs(gw_app, gw_call):
    registry = gw_app.state.svc.gateway.tools
    registry._tools["commerce.ask_price"] = dataclasses.replace(registry.get("commerce.ask_price"), protected=True)
    replies = gw_call(["I want to buy something on CI Store", "how much is peak milk tin"])
    assert "cannot do that yet" in replies[2]
