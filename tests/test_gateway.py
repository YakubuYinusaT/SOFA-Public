"""The front desk: the gateway number greets like a customer-care agent and routes by what the caller says."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from scripts.seed import seed
from scripts.simulate_call import run_call
from sofa.config import Settings
from sofa.main import create_app
from sofa.models import Call, CallTurn, Handoff

SECRET = "test-secret"
GATEWAY = "+2348000009999"


@pytest.fixture
def gw_app(tmp_path):
    app = create_app(Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret=SECRET,
                              public_base_url="http://test", debug_headers=True, scheduler_enabled=False,
                              gateway_number=GATEWAY, verify_random_rate=0.0, _env_file=None))
    seed(app.state.svc.session_factory)
    return app


@pytest.fixture
def gw_client(gw_app):
    with TestClient(gw_app) as c:
        yield c


@pytest.fixture
def gw_call(gw_client):
    def _call(lines, caller="+2348055550001", lang="en"):
        return run_call(gw_client, lines, caller, GATEWAY, lang, SECRET, echo=lambda *_: None)
    return _call


@pytest.fixture
def gw_db(gw_app):
    with gw_app.state.svc.session_factory() as session:
        yield session


def test_greeting_is_sofa_not_a_shop(gw_call):
    greeting = gw_call([])[0]
    assert "Sofa" in greeting and "personal assistant" in greeting
    assert "CI Store" not in greeting


def test_greeting_varies_between_calls(gw_call):
    greetings = {gw_call([], caller=f"+23480555500{i:02d}")[0] for i in range(12)}
    assert len(greetings) > 1


@pytest.mark.parametrize("line, service", [
    ("Hi Sofa I would like to make a transaction", "any bank under your number"),  # no bank connected to this caller yet
    ("I want to buy something on Jumia", "order from CI Store"),  # one shop connected, none named: it offers that one
    ("Can you look something up for me", "look up"),
])
def test_request_is_routed_to_a_service(gw_call, gw_db, line, service):
    replies = gw_call([line])
    assert service in replies[1]
    call = gw_db.scalar(select(Call))
    assert call.merchant_id is None and call.service_domain is not None
    turn = gw_db.scalar(select(CallTurn))
    assert turn.llm_json["domain"] == call.service_domain  # the evidence also keeps the language mix and the tool the model chose


def test_unclear_with_no_service_known_asks_again_and_keeps_the_call_open(gw_call, gw_db):
    replies = gw_call(["mmm", "uhh", "hmm", "ok I want to buy something"])
    assert "bank" in replies[1] and "bank" in replies[2] and "What do you need" in replies[3]
    assert "CI Store" in replies[4]  # SOFA did not hang up: the caller carried on
    assert gw_db.scalar(select(Handoff)) is None  # no provider is known, so nobody to pass it to


def test_unresolved_request_for_a_known_service_is_handed_to_its_provider(gw_call, gw_db):
    replies = gw_call(["I want my bank balance", "hmm", "mmm", "uhh", "I want to buy something"])
    assert "your bank" in replies[4] and "follow up" in replies[4] and "anything else" in replies[4]
    assert "CI Store" in replies[5]  # the call stayed open and the next request started fresh
    assert "promise" not in replies[4] and "call you back" not in replies[4]  # no time and no callback is promised
    handoff = gw_db.scalar(select(Handoff))
    assert handoff.service_domain == "banking" and handoff.merchant_id is None
    assert "bank balance" in handoff.summary and gw_db.scalar(select(Call)).outcome == "handoff"


def test_asking_for_a_person_mid_service_hands_over_to_the_provider(gw_call, gw_db):
    replies = gw_call(["I want to buy something", "let me speak to a person", "I want my bank balance"])
    assert "the shop" in replies[2] and "anything else" in replies[2]
    assert "bank under your number" in replies[3]  # still on the line
    assert gw_db.scalar(select(Handoff)).reason == "asked_for_person"


def test_asking_for_a_person_before_a_service_is_known_asks_which_service(gw_call, gw_db):
    replies = gw_call(["I want to speak to a human", "ok I want to buy something"])
    assert "AI assistant" in replies[1] and "follow up" in replies[1]
    assert "CI Store" in replies[2]
    assert gw_db.scalar(select(Handoff)) is None


def test_silence_is_met_with_gentle_prompts_and_only_a_long_silence_ends_the_call(gw_call):
    replies = gw_call(["", ""])
    assert len(replies) == 3  # an open line costs money: one "are you still there?", then goodbye
    assert "still" in replies[1]
    assert "let you go" in replies[2]


def test_a_caller_who_speaks_again_resets_the_silence_count(gw_call):
    replies = gw_call(["", "I want to buy something", "", ""])
    assert len(replies) == 5 and "CI Store" in replies[2]


@pytest.mark.parametrize("line", ["Hello Sofa", "Hi, good afternoon", "thanks a lot", "how are you"])
def test_a_greeting_or_thanks_is_answered_warmly_not_treated_as_not_understood(gw_call, gw_db, line):
    replies = gw_call([line])
    assert "Sorry, I did not quite get" not in replies[1] and "?" in replies[1]  # it asks what they need


def test_greetings_never_add_up_to_a_failure_to_understand(gw_call, gw_db):
    replies = gw_call(["hello", "hello again", "hi", "hey Sofa", "good morning", "I want to buy something"])
    assert all("Sorry, I did not quite get" not in r for r in replies)
    assert "CI Store" in replies[6]
    assert gw_db.scalar(select(Handoff)) is None and gw_db.scalar(select(Call)).outcome in (None, "ended")  # never "unresolved"


def test_a_caller_with_a_bank_is_offered_the_usual_things_in_the_greeting(gw_call, gw_db):
    from sofa.gateway import links
    from sofa.models import Customer
    c = Customer(phone="+2348055550321", name="Tunde")
    gw_db.add(c)
    gw_db.flush()
    links.link(gw_db, c.id, "banking", "demobank")
    gw_db.commit()
    greeting = gw_call([], caller="+2348055550321")[0]  # one call: each call picks its own wording at random
    assert "balance" in greeting or "account" in greeting
    stranger = gw_call([], caller="+2348055550322")[0]
    assert "balance" not in stranger and "account" not in stranger  # someone with no bank is not offered one


def test_returning_caller_is_greeted_by_name(gw_call, gw_db):
    gw_call(["my name is Amina and I want my bank balance"])
    # name capture is the shop flow's job for now; set it the way a connected service will
    from sofa.models import Customer
    gw_db.scalar(select(Customer)).name = "Amina"
    gw_db.commit()
    assert "Amina" in gw_call([])[0]


def test_shop_numbers_still_work_alongside_the_gateway(gw_client, gw_app):
    from scripts.seed import DEMO_NUMBER
    replies = run_call(gw_client, ["two cartons of indomie super pack"], "+2348055550002", DEMO_NUMBER, "en", SECRET, echo=lambda *_: None)
    assert "CI Store" in replies[0]
