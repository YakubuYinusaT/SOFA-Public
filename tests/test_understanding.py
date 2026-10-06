"""One understanding call per turn, model-written wording that cannot touch a fact, and money spoken in English."""

import asyncio

import pytest
from sqlalchemy import select

from sofa.dialogue import templates
from sofa.dialogue.manager import new_state
from sofa.gateway import wording
from sofa.gateway.manager import GatewayManager
from sofa.gateway.understanding import Understanding
from sofa.models import Call, CallTurn, Customer
from tests.test_bank_ops import begin, enter, said
from tests.conftest import SECRET
from tests.test_verification import AMINA, GATEWAY, PIN, Caller, clock, v_app, v_client, v_db  # noqa: F401  (fixtures and helpers)

DONE = "Done. 10,000 naira sent to Hauwa Bello at GT Bank. Your balance is now 32,500 naira. Is there anything else I can help with?"


@pytest.fixture
def llm_calls(v_app):
    """Which kind of model call was made (the first word of its instructions), in order."""
    llm, calls = v_app.state.svc.llm, []
    original = llm._chat

    async def spy(system, user, **extra):
        calls.append(system.split(".")[0].split(" ")[0])
        return await original(system, user, **extra)

    llm._chat = spy
    return calls


def fake_model(v_app, wording_for=None, understanding_patch=None):
    """Replace the structured model call: `wording_for(system)` may return a sentence for a WORDING request (or raise)."""
    llm = v_app.state.svc.llm
    original = llm.extract

    async def fake(model, system, transcript, alternatives=None, json_schema=None):
        if system.startswith("WORDING") and wording_for:
            say = wording_for(system)
            if say is not None:
                return model(say=say), {}
        obj, raw = await original(model, system, transcript, alternatives, json_schema=json_schema)
        if system.startswith("UNDERSTAND") and understanding_patch:
            understanding_patch(obj)
        return obj, raw

    llm.extract = fake


def manager(v_app, v_db, lang="en"):
    svc = v_app.state.svc
    customer = v_db.scalar(select(Customer).where(Customer.phone == AMINA))
    call = Call(provider_session_id="unit", from_number=AMINA, to_number=GATEWAY, customer_id=customer.id)
    v_db.add(call)
    v_db.flush()
    st = new_state(lang, True, False)
    st.update(call_id=str(call.id), customer_id=str(customer.id), gateway=True, domain=None)
    return GatewayManager(v_db, svc, st, call, customer)


# ---- one understanding call per turn -----------------------------------------------------------------------------

def test_a_turn_makes_one_understanding_call_not_a_chain_of_small_ones(v_client, llm_calls):
    call = Caller(v_client)
    llm_calls.clear()
    call.say("send 2000 to mama")
    assert llm_calls.count("UNDERSTAND") == 1
    assert set(llm_calls) <= {"UNDERSTAND", "WORDING"}
    llm_calls.clear()
    call.say("yes")
    assert llm_calls.count("UNDERSTAND") == 1


def test_the_model_can_only_name_registered_tools(v_client, v_app, v_db):
    fake_model(v_app, understanding_patch=lambda u: setattr(u, "action", "bank.empty_the_account"))
    call = Caller(v_client)
    call.say("my bank balance please")
    call.say("yes")
    assert "What would you like to do" in call.reply  # the invented tool was dropped; nothing ran


def test_the_language_mix_the_model_found_is_kept_as_evidence(v_client, v_db):
    call = Caller(v_client)
    call.say("Jowo send 5000 si mama")
    v_db.expire_all()
    turn = v_db.scalars(select(CallTurn).order_by(CallTurn.seq)).first()
    assert turn.llm_json == {"language": "yo", "mixed": True, "domain": "banking", "tool": "bank.transfer"}


def test_the_reply_language_follows_the_caller_but_not_on_one_stray_word(v_app, v_db):
    v_app.state.svc.settings.llm_language_switching = True  # off by default until the model's language label is measured
    mgr = manager(v_app, v_db, "yo")
    english = Understanding(language="en")
    yoruba_with_english_words = Understanding(language="yo", mixed=True)
    mgr.note_language(english)
    assert mgr.st["lang"] == "yo"  # one turn is not a switch
    mgr.note_language(yoruba_with_english_words)
    mgr.note_language(english)
    assert mgr.st["lang"] == "yo"  # and a Yoruba sentence in between resets the count
    mgr.note_language(english)
    mgr.note_language(english)
    assert mgr.st["lang"] == "en" and mgr.customer.language == "en"


# ---- wording the model writes ----------------------------------------------------------------------------------

def test_the_models_language_label_does_not_change_the_reply_language_by_default(v_app, v_db):
    mgr = manager(v_app, v_db, "yo")
    for _ in range(3):
        mgr.note_language(Understanding(language="en"))
    assert mgr.st["lang"] == "yo"  # the first GPU run showed the label is not reliable enough to act on yet


def test_the_model_can_word_the_conversation_in_the_callers_mix_and_the_system_still_fills_in_the_names(v_client, v_app, v_db):
    fake_model(v_app, wording_for=lambda s: "{ack}! We dey for {bank} now. Wetin you wan do?" if "REFERENCE: {ack}. We are on" in s else None)
    call = Caller(v_client)
    begin(call, "I want my bank")
    assert call.reply == "Okay! We dey for Demo Bank now. Wetin you wan do?"  # Pidgin wording, the bank's name filled in by the backend
    v_db.expire_all()
    assert v_db.scalars(select(CallTurn).order_by(CallTurn.created_at.desc())).first().action_taken.endswith("|generated")


def test_a_sentence_that_carries_a_figure_or_a_name_is_never_sent_to_the_model(v_client, v_app, v_db, llm_calls):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    llm_calls.clear()
    call.say("yes")
    call.type(PIN)
    assert said(v_db) == DONE and "WORDING" not in llm_calls  # the transfer result is the bank's exact template, not model wording


@pytest.mark.parametrize("bad", [
    "Okay 5000. We are on {bank}. What would you like to do?",       # a number the model made up
    "Okay. What would you like to do?",                               # a fact the backend meant to say was dropped ({bank})
    "{ack}. We are on {bank}. What would you like to do? Really?",    # more questions than the reference
    "{ack}. We are on {bank}. {invented} What would you like to do?",  # a placeholder that does not exist
    "{ack}. We are on {bank}. Tell me what you need.",                # the question was dropped
    "RAISE",                                                          # the model failed
])
def test_a_bad_wording_is_never_spoken_the_template_is(v_client, v_app, v_db, bad):
    def write(system):
        if bad == "RAISE":
            raise TimeoutError("model did not answer")
        return bad if "REFERENCE: {ack}. We are on" in system else None

    fake_model(v_app, wording_for=write)
    call = Caller(v_client)
    begin(call, "I want my bank")
    assert call.reply == "Okay. We are on Demo Bank. What would you like to do?"  # the template, whatever the model wrote


def balance_after(db):
    from sofa.models import MockBankAccount
    db.expire_all()
    return db.scalar(select(MockBankAccount)).balance_kobo


def test_security_prompts_are_never_model_written(v_client, llm_calls):
    call = Caller(v_client)
    call.say("send 2000 to mama")
    llm_calls.clear()
    call.say("yes")  # this reply is the PIN request
    assert call.keypad and "WORDING" not in llm_calls


def test_wording_can_be_switched_off(v_client, v_app, llm_calls):
    v_app.state.svc.settings.gateway_wording = False
    call = Caller(v_client)
    call.say("send 2000 to mama")
    assert "WORDING" not in llm_calls


def test_yoruba_wording_stays_on_templates_until_it_is_switched_on(v_app, v_db, llm_calls):
    mgr = manager(v_app, v_db, "yo")
    out = mgr.reply("bank_cancelled", action="x")
    assert asyncio.run(mgr.reword(out)) is out and "WORDING" not in llm_calls  # reply_mode_yo defaults to template


def test_every_gateway_sentence_passes_its_own_validator(v_app):
    """If a template drifts (a stray digit, two questions) the model's wording for it would always be refused: catch it here."""
    bad = []
    for key, variants in templates.EN.items():
        if not key.startswith(("bank_", "shop_", "gateway_")) or key.startswith("verify_"):
            continue
        reference = variants[0]
        if reference.count("?") > 1:
            bad.append(key)
            continue
        facts = {name: "x" for name in templates.placeholders(reference)} | {n.strip("{}"): "x" for n in templates.placeholders(reference)}
        if wording.accept(reference, reference, facts) is None:
            bad.append(key)
    assert bad == []


# ---- values are spoken in English ----------------------------------------------------------------------------------

FACTS = {"amount": "10,000 naira", "who": "Hauwa Bello at GT Bank", "balance": "32,500 naira"}


def test_money_is_its_own_english_clip_in_every_other_language():
    parts = templates.value_parts("Done. {amount} sent to {who}. Your balance is now {balance}.", FACTS, "yo")
    assert parts == [("Done.", "yo"), ("10,000 naira", "en"), ("sent to Hauwa Bello at GT Bank. Your balance is now", "yo"), ("32,500 naira.", "en")]


def test_english_replies_and_replies_without_values_stay_one_clip():
    assert templates.value_parts("Done. {amount} sent.", FACTS, "en") is None
    assert templates.value_parts("Sent to {who}.", FACTS, "yo") is None


def test_card_and_account_digits_are_read_one_at_a_time():
    assert templates.spell_digits("4321") == "4 3 2 1"


def test_a_yoruba_conversation_sentence_can_be_model_worded_and_a_money_sentence_cannot(v_app, v_db, llm_calls):
    v_app.state.svc.settings.reply_mode_yo = "generated"
    fake_model(v_app, wording_for=lambda s: "{ack}, a wa ni {bank}. Kini o fe ko ṣe?")
    mgr = manager(v_app, v_db, "yo")
    chat = asyncio.run(mgr.reword(mgr.reply("bank_selected", action="bank_selected", bank="Demo Bank")))
    assert chat.lang == "yo" and chat.action.endswith("|generated") and "Demo Bank" in chat.text
    money = mgr.reply("bank_transfer_done", action="bank_transfer_done", **FACTS)
    assert asyncio.run(mgr.reword(money)) is money and money.action.endswith("|template")  # never asked of the model


def test_after_anything_else_a_no_says_goodbye_and_ends_the_call(v_client, v_app, v_db):
    call = Caller(v_client)
    st = v_app.state.svc.sessions.get(call.sid)
    st["asked_more"] = True  # SOFA has just asked "is there anything else I can help with?"
    v_app.state.svc.sessions.set(call.sid, st)
    assert "Goodbye" in call.say("no that's all") and "<Record" not in call.last.text  # no listening line: the call ends


def test_a_call_past_the_time_limit_ends_politely_at_the_next_turn(v_client, v_app, v_db):
    v_app.state.svc.settings.max_call_seconds = 1
    call = Caller(v_client)
    st = v_app.state.svc.sessions.get(call.sid)
    st["t0"] -= 5  # the call has been going for a while
    v_app.state.svc.sessions.set(call.sid, st)
    assert "let you go" in call.say("send the usual to mama") and "<Record" not in call.last.text
