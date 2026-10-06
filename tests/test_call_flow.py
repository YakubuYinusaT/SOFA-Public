import hashlib
import hmac
import json

from sqlalchemy import select

from sofa.models import (Call, CallTurn, Customer, Handoff, Invoice, MissedDemand, Notification, Order, Payment,
                         Product, ProductAlias, StockMovement)
from tests.conftest import OWNER, SECRET

MUSA = "+2348055550001"

SPEC_CONVERSATION = [
    "My name is Musa, I want Indomie big one.",
    "Super pack.",
    "Carton. Two carton.",
    "Add Peak milk.",
    "Tin, three.",
    "No, that's all.",
    "Ajao Estate, near the mosque on Adeyemi Street.",
    "Yes.",
]


def product(db, name):
    return db.scalar(select(Product).where(Product.name == name))


def test_spec_conversation_places_order(call, db):
    replies = call(SPEC_CONVERSATION)
    assert "Who am I speaking with" in replies[0]
    assert "Indomie Super Pack at 350 naira" in replies[1] and "Indomie Hungry Man at 500 naira" in replies[1]
    assert "pack" in replies[2] and "carton" in replies[2]
    assert "2 cartons of Indomie Super Pack" in replies[3] and "14,000 naira" in replies[3]
    assert "Peak Milk Tin" in replies[4] and "Peak Milk Sachet" in replies[4]
    assert "3 tins of Peak Milk Tin" in replies[5]
    assert "Where should we deliver" in replies[6]
    assert "16,400 naira in total" in replies[7] and "Ajao Estate" in replies[7]
    assert "sent the account number" in replies[8] and "Musa" in replies[8]

    order = db.scalar(select(Order).where(Order.status == "awaiting_payment"))
    assert order.total_kobo == 1_640_000
    assert order.delivery_address.startswith("Ajao Estate")
    assert {(i.unit, i.qty) for i in order.items} == {("carton", 2), ("tin", 3)}

    # stock reserved through movements: 2 cartons = 80 packs, 3 tins
    assert product(db, "Indomie Super Pack").stock_qty == 800 - 80
    assert product(db, "Peak Milk Tin").stock_qty == 200 - 3
    assert db.scalars(select(StockMovement).where(StockMovement.reason == "order")).all()

    invoice = db.scalar(select(Invoice))
    assert invoice.reference.startswith("SOFA-") and invoice.amount_kobo == 1_640_000
    sms = db.scalars(select(Notification).where(Notification.channel == "sms")).all()
    assert {n.template for n in sms} >= {"invoice", "merchant_new_order"}
    assert invoice.reference in next(n.body for n in sms if n.template == "invoice")

    customer = db.scalar(select(Customer).where(Customer.phone == MUSA))
    assert customer.name == "Musa" and customer.language == "en" and customer.consent_recorded_at


def test_every_turn_is_logged_with_evidence(call, db):
    call(SPEC_CONVERSATION)
    c = db.scalar(select(Call))
    assert c.outcome == "order_confirmed" and c.is_test is False
    turns = list(db.scalars(select(CallTurn).order_by(CallTurn.seq)))
    assert len(turns) == len(SPEC_CONVERSATION)
    for t in turns:
        assert t.transcript and t.asr_model and t.asr_confidence is not None
        assert t.llm_json and t.reply_text and t.reply_audio_path and t.audio_path
        assert {"asr", "total"} <= set(t.latency_ms)
    assert turns[0].match_candidates and turns[0].match_candidates[0]["status"] == "ambiguous"


def test_confirmed_call_teaches_aliases(call, db):
    call(SPEC_CONVERSATION)
    learned = db.scalars(select(ProductAlias).where(ProductAlias.source == "confirmed_call")).all()
    assert learned, "read-back confirmation should write aliases"


def test_returning_customer_gets_repeat_offer(call, db):
    call(SPEC_CONVERSATION)
    replies = call(["Yes."])
    assert "Welcome back, Musa" in replies[0] and "Same as last time" in replies[0]
    assert "2 cartons of Indomie Super Pack" in replies[0]
    assert "16,400 naira in total" in replies[1] and "Ajao Estate" in replies[1]  # saved address, straight to read-back


def test_price_and_availability_and_missed_demand(call, db):
    replies = call(["How much is Peak milk tin?", "Do you have Golden Penny spaghetti?", "Do you have pizza?"])
    assert "800 naira per tin" in replies[1]
    assert "out of stock" in replies[2] and "Dangote Spaghetti" in replies[2]  # closest in-stock substitute
    assert "don't have pizza" in replies[3]
    assert db.scalar(select(MissedDemand).where(MissedDemand.spoken_name == "pizza"))


def test_deny_at_readback_returns_to_editing(call, db):
    replies = call(["Two cartons of Hungry Man", "No.", "Lekki phase one", "No.", "Make it three"])
    assert "Where should we deliver" in replies[2]
    assert "What should I change" in replies[4]
    assert "3 cartons" in replies[5]
    assert db.scalar(select(Order).where(Order.status == "draft"))  # nothing left draft without a spoken yes... still draft


def test_track_and_payment_status_and_cancel(call, db):
    call(SPEC_CONVERSATION)
    replies = call(["Where is my order?", "Have you received my payment?", "Cancel it", "Cancel it"], caller=MUSA)
    assert "waiting for your payment" in replies[1]
    assert "Not yet" in replies[2] and "16,400 naira" in replies[2]
    assert "cancelled" in replies[3]
    assert "can't find an order to cancel" in replies[4]
    assert product(db, "Peak Milk Tin").stock_qty == 200  # cancel releases reserved stock
    assert db.scalar(select(Invoice)).status == "void"


def test_speak_to_human_creates_handoff_and_sms(call, db):
    replies = call(["Let me talk to the owner"])
    assert "call you back" in replies[1]
    h = db.scalar(select(Handoff))
    assert h.reason == "speak_to_human"
    note = db.scalar(select(Notification).where(Notification.template == "handoff"))
    assert note.to_number == OWNER and MUSA in note.body


def test_two_failed_understandings_hand_off(call, db):
    replies = call(["blah blah zzz", "flim flam"])
    assert "say it again" in replies[1]
    assert "call you back" in replies[2]
    assert db.scalar(select(Handoff)).reason == "two_unknown_turns"


def test_silence_prompts_then_goodbye_with_sms(call, db):
    replies = call(["", ""])
    assert "still there" in replies[1]
    assert "goodbye" in replies[2].lower()
    assert db.scalar(select(Notification).where(Notification.template == "goodbye"))


def test_language_is_detected_from_first_utterance(call, db):
    call(["I want Peak milk tin three"], lang="yo")
    assert db.scalar(select(Customer).where(Customer.phone == MUSA)).language == "yo"


def test_slow_turn_uses_filler_and_redirect(settings, tmp_path):
    from fastapi.testclient import TestClient
    from scripts.seed import DEMO_NUMBER, seed
    from scripts.simulate_call import run_call
    from sofa.main import create_app

    settings.filler_after_seconds = 0  # every turn takes the filler + Redirect path
    app = create_app(settings)
    seed(app.state.svc.session_factory)
    with TestClient(app) as client:
        replies = run_call(client, ["How much is Peak milk tin?"], MUSA, DEMO_NUMBER, "en", SECRET, echo=lambda *_: None)
    assert "800 naira per tin" in replies[-1]


def test_owner_updates_stock_and_price_by_voice(call, db):
    replies = call(["Add 20 cartons of Indomie Super Pack, price is 7500", "Yes."], caller=OWNER)
    assert "Hello Ade" in replies[0]
    assert "Add 20 cartons of Indomie Super Pack at 7,500 naira" in replies[1]
    p = product(db, "Indomie Super Pack")
    assert p.stock_qty == 800 + 20 * 40
    assert next(u for u in p.units if u.unit == "carton").price_kobo == 750_000
    assert db.scalar(select(StockMovement).where(StockMovement.reason == "restock", StockMovement.source == "voice"))
    # the next customer hears the new price
    replies = call(["How much is a carton of Super Pack?"])
    assert "7,500 naira per carton" in replies[1]


def test_owner_large_price_change_needs_web_confirmation(call, db):
    replies = call(["Add 5 cartons of Indomie Super Pack, price is 20000", "Yes."], caller=OWNER)
    assert "web page" in replies[2]
    assert product(db, "Indomie Super Pack").stock_qty == 800


# ---- Paystack -----------------------------------------------------------------------------


def charge(client, account, amount_kobo, event_id=1001):
    body = json.dumps({"event": "charge.success", "data": {
        "id": event_id, "reference": f"ref-{event_id}", "amount": amount_kobo,
        "authorization": {"channel": "dedicated_nuban", "receiver_bank_account_number": account, "sender_name": "MUSA A"}}}).encode()
    sig = hmac.new(b"dev-paystack-secret", body, hashlib.sha512).hexdigest()
    return client.post("/webhooks/paystack", content=body, headers={"x-paystack-signature": sig})


def test_paystack_transfer_marks_invoice_paid_and_queues_callback(call, client, db):
    from sofa.models import OutboundCall, VirtualAccount

    call(SPEC_CONVERSATION)
    acct = db.scalar(select(VirtualAccount))
    assert charge(client, acct.account_number, 1_640_000).status_code == 200
    db.expire_all()
    order = db.scalar(select(Order))
    assert order.status == "paid" and db.scalar(select(Invoice)).status == "paid"
    assert db.scalar(select(Payment)).match_status == "auto"
    assert db.scalar(select(OutboundCall)).trigger == "payment_received"
    assert db.scalar(select(Notification).where(Notification.template == "payment_received"))
    # duplicate delivery is ignored
    charge(client, acct.account_number, 1_640_000)
    db.expire_all()
    assert len(db.scalars(select(Payment)).all()) == 1


def test_paystack_bad_signature_rejected(client):
    r = client.post("/webhooks/paystack", content=b'{"event":"charge.success"}', headers={"x-paystack-signature": "nope"})
    assert r.status_code == 401


def test_paystack_short_payment_is_unmatched_with_handoff(call, client, db):
    from sofa.models import VirtualAccount

    call(SPEC_CONVERSATION)
    acct = db.scalar(select(VirtualAccount))
    charge(client, acct.account_number, 500_000, event_id=2002)
    db.expire_all()
    assert db.scalar(select(Order)).status == "awaiting_payment"
    assert db.scalar(select(Payment)).match_status == "unmatched"
    assert db.scalar(select(Handoff)).reason == "unmatched_payment"


# ---- security and admin ---------------------------------------------------------------------


def test_callback_secret_is_enforced(client):
    r = client.post("/voice/inbound/wrong-secret", data={"sessionId": "x"})
    assert r.status_code == 404


def test_unknown_number_gets_polite_hangup(client):
    r = client.post(f"/voice/inbound/{SECRET}", data={"sessionId": "x", "callerNumber": MUSA, "destinationNumber": "+2340000000"})
    assert "not in service" in r.text


def test_admin_requires_token_and_reports_metrics(call, client):
    assert client.get("/api/handoffs").status_code == 401
    call(SPEC_CONVERSATION, caller="+2348055550009")
    h = {"Authorization": "Bearer dev-admin-token"}
    m = client.get("/api/metrics/validation", headers=h).json()
    assert m["real_interactions"] == 1 and m["confirmed_orders"] == 1 and m["order_value_naira"] == 16400.0
    assert m["median_latency_ms"] is not None


def test_a_slow_reply_keeps_the_caller_company_with_holding_messages_and_never_hangs_up(settings, monkeypatch):
    import asyncio
    import re

    from fastapi.testclient import TestClient
    from sofa.main import create_app
    from sofa.routes import voice

    settings.filler_after_seconds = 0
    settings.filler_every_seconds = 0.05
    gate = {"open": False}

    async def slow(svc, session_id, recording_url, secret, turn_id):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        return "<Response><Say>the answer</Say></Response>", "the answer", False

    monkeypatch.setattr(voice, "_process_turn", slow)
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "s1"})
        assert "<Redirect>" in r.text and "Hangup" not in r.text
        seen = 0
        for _ in range(voice.WAIT_STAGES + 1):  # one more holding message each time the reply is still not ready
            nxt = re.search(r"<Redirect>(.*?)</Redirect>", r.text).group(1).replace("&amp;", "&")
            if "/continue/" not in nxt:
                break
            seen += 1
            r = client.post(nxt.replace("http://test", ""), data={"sessionId": "s1"})
            assert "Hangup" not in r.text
        assert seen == voice.WAIT_STAGES + 1
        assert "<Record" in r.text and "/voice/turn/" in r.text  # gave up politely, asked "anything else?" and listens


def test_a_reply_that_arrives_during_the_holding_messages_is_played(settings, monkeypatch):
    import asyncio
    import re

    from fastapi.testclient import TestClient
    from sofa.main import create_app
    from sofa.routes import voice

    settings.filler_after_seconds = 0
    settings.filler_every_seconds = 0.3

    async def slowish(svc, session_id, recording_url, secret, turn_id):
        await asyncio.sleep(0.1)
        return "<Response><Say>the answer</Say></Response>", "the answer", False

    monkeypatch.setattr(voice, "_process_turn", slowish)
    app = create_app(settings)
    with TestClient(app) as client:
        r = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "s1"})
        nxt = re.search(r"<Redirect>(.*?)</Redirect>", r.text).group(1).replace("&amp;", "&")
        r = client.post(nxt.replace("http://test", ""), data={"sessionId": "s1"})
        assert "the answer" in r.text


def test_a_worded_sentence_the_voice_is_too_slow_for_is_replaced_by_its_template():
    import asyncio
    from types import SimpleNamespace

    from sofa.dialogue.manager import Outcome
    from sofa.routes.voice import _speak_outcome

    async def speak(text, lang):
        if text == "Brand new wording.":
            await asyncio.sleep(5)  # the voice model needs far longer than the budget for a sentence it has never made
        return f"url:{text}", "path"

    svc = SimpleNamespace(audio=SimpleNamespace(speak=speak), settings=SimpleNamespace(tts_budget_seconds=0.05))
    out = Outcome("Brand new wording.", "gateway_chat", action="chat|generated", extra={"template": ("Stored wording.", "en", None)})
    assert asyncio.run(_speak_outcome(svc, out)) == ["url:Stored wording."]
    assert out.text == "Stored wording." and out.action == "chat|template_fallback"
    fixed = Outcome("Stored wording.", "gateway_chat", action="chat|template")
    assert asyncio.run(_speak_outcome(svc, fixed)) == ["url:Stored wording."]
