"""Verifying the caller before a protected banking action: PIN and code on the keypad, re-asked by rule."""

import logging
import random
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from scripts.seed import seed
from sofa.audio import mock_recording_url
from sofa.config import Settings
from sofa.gateway import links, mockbank
from sofa.gateway.bankauth import MockBankAuth
from sofa.gateway.hub import build_gateway
from sofa.gateway.policy import VerificationPolicy
from sofa.main import create_app
from sofa.models import Base, Call, CallTurn, Customer, Handoff, Notification, ServiceLink

SECRET, GATEWAY = "test-secret", "+2348000009999"
PIN, OTP = "4821", "907315"
AMINA = "+2348055550101"


HEX_RUN = re.compile(r"[0-9a-f]{12,}")  # ids and file hashes: random digits that can contain any short number by chance
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2})?")


def stored_text(db) -> str:
    """Everything SOFA has stored, as one string, minus the parts that are random by nature (ids, hashes, timestamps) and the
    bank's own mock tables. A secret that was kept would be found in what is left."""
    rows = []
    for table in Base.metadata.sorted_tables:
        if table.name.startswith("mockbank_"):
            continue
        for row in db.execute(text(f'select * from "{table.name}"')).all():
            rows.append(" ".join(str(c) for c in row))
    return TIMESTAMP.sub("", HEX_RUN.sub("", "\n".join(rows).lower()))


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FixedOtp(MockBankAuth):
    def _new_code(self):
        return OTP


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def v_app(tmp_path, clock):
    app = create_app(Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret=SECRET, public_base_url="http://test",
                              debug_headers=True, scheduler_enabled=False, gateway_number=GATEWAY, verify_random_rate=0.0,
                              mock_bank_pin=PIN, _env_file=None))
    seed(app.state.svc.session_factory)
    svc = app.state.svc
    svc.bankauth = FixedOtp(svc.settings, svc.sms)
    svc.gateway.clock = clock
    with svc.session_factory() as db:
        c = Customer(phone=AMINA, name="Amina")
        db.add(c)
        db.flush()
        links.link(db, c.id, "banking", "demobank")
        mockbank.open_account(db, c, "demobank", 42_500)
        mockbank.add_beneficiary(db, c, "demobank", "Hauwa Bello", "mama, mum", 10_000, "GT Bank", "0123456789")
        db.commit()
    return app


@pytest.fixture
def v_client(v_app):
    with TestClient(v_app) as c:
        yield c


@pytest.fixture
def v_db(v_app):
    with v_app.state.svc.session_factory() as session:
        yield session


class Caller:
    """One phone call, driven by hand so a test can move the clock between turns."""

    def __init__(self, client, phone=AMINA):
        self.c, self.phone, self.sid = client, phone, f"v-{uuid.uuid4().hex[:8]}"
        self.last = client.post(f"/voice/inbound/{SECRET}", data={"sessionId": self.sid, "callerNumber": phone,
                                                                  "destinationNumber": GATEWAY, "isActive": "1", "direction": "Inbound"})

    @property
    def keypad(self):
        return "<GetDigits" in self.last.text

    def say(self, words):
        self.last = self.c.post(f"/voice/turn/{SECRET}", data={"sessionId": self.sid, "callerNumber": self.phone, "destinationNumber": GATEWAY,
                                                              "isActive": "1", "recordingUrl": mock_recording_url(words, "en")})
        return self.reply

    def type(self, digits):
        data = {"sessionId": self.sid, "callerNumber": self.phone, "destinationNumber": GATEWAY, "isActive": "1"}
        if digits:
            data["dtmfDigits"] = digits
        self.last = self.c.post(f"/voice/digits/{SECRET}", data=data)
        return self.reply

    @property
    def reply(self):
        return self.last.headers.get("X-Sofa-Reply-Text", "")

    def verified(self):
        """Walk the entry check for a balance request: say it, confirm the bank, PIN, code."""
        self.say("I want my bank balance")
        self.say("yes")
        self.type(PIN)
        return self.type(OTP)


# ---- the policy on its own --------------------------------------------------------------------------------

@pytest.fixture
def policy():
    return VerificationPolicy(Settings(_env_file=None, verify_random_rate=0.0), random.Random(1))


@pytest.fixture
def tools():
    return build_gateway().tools


def auth(**kw):
    return {"bank": "demobank", "verified_at": 100.0, "last_pin_at": 100.0, "resources": ["account_info"], **kw}


def test_open_tools_are_never_challenged(policy, tools):
    assert policy.required(tools.get("bank.product_info"), None, 0, "demobank") is None
    assert policy.required(tools.get("commerce.ask_price"), None, 0, None) is None


def test_the_first_protected_action_needs_pin_and_code(policy, tools):
    ch = policy.required(tools.get("bank.balance"), None, 200, "demobank")
    assert (ch.kind, ch.reason) == ("pin_otp", "entry")


def test_a_different_bank_starts_again(policy, tools):
    ch = policy.required(tools.get("bank.balance"), auth(bank="gtbank"), 110, "demobank")
    assert ch.reason == "entry"


def test_within_the_window_and_same_resource_needs_nothing(policy, tools):
    assert policy.required(tools.get("bank.balance"), auth(), 150, "demobank") is None


def test_the_pin_is_asked_again_after_a_few_minutes(policy, tools):
    ch = policy.required(tools.get("bank.balance"), auth(), 100 + 181, "demobank")
    assert (ch.kind, ch.reason) == ("pin", "stale")


def test_a_new_sensitive_resource_asks_again(policy, tools):
    assert policy.required(tools.get("bank.complaint"), auth(), 150, "demobank").reason == "new_resource"


def test_the_policy_gates_access_and_each_money_move_is_authorised_after_the_read_back(policy, tools):
    """Access to the account is the policy's job. The PIN that authorises a transfer is asked later, after the read-back and the
    caller's yes (bankops.advance_op), so a PIN given earlier never covers it: see test_bank_ops."""
    a = auth(resources=["account_info", "transfers"])
    assert policy.required(tools.get("bank.transfer"), a, 150, "demobank") is None


def test_random_checks_follow_the_configured_rate(tools):
    always = VerificationPolicy(Settings(_env_file=None, verify_random_rate=1.0), random.Random(1))
    never = VerificationPolicy(Settings(_env_file=None, verify_random_rate=0.0), random.Random(1))
    assert always.required(tools.get("bank.balance"), auth(), 150, "demobank").reason == "random"
    assert always.required(tools.get("bank.balance"), auth(), 110, "demobank") is None  # never right after a PIN (within 30 s)
    assert never.required(tools.get("bank.balance"), auth(), 150, "demobank") is None


def test_the_registry_reports_the_challenge_with_the_decision(policy, tools):
    d = tools.authorize("bank.balance", None, policy=policy, now=200, provider="demobank")
    assert not d.allowed and d.reason == "needs_verification" and d.challenge.reason == "entry"


# ---- the conversation ---------------------------------------------------------------------------------------

def test_entry_needs_pin_then_code_then_the_action_runs(v_client, v_db):
    call = Caller(v_client)
    assert "Welcome back" in call.reply or "Amina" in call.reply
    call.say("I want my bank balance")
    assert "Shall I go ahead with Demo Bank" in call.reply
    call.say("yes")
    assert call.keypad and "enter your PIN on your keypad" in call.reply
    call.type(PIN)
    assert call.keypad and "sent a code to your phone" in call.reply
    call.type(OTP)
    assert not call.keypad and "you are verified" in call.reply
    last = v_db.scalars(select(CallTurn).order_by(CallTurn.seq.desc())).first()
    assert "Your Demo Bank balance is 42,500 naira" in last.reply_text  # the verified action runs at the bank


def test_a_wrong_pin_can_be_retried(v_client):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    call.type("0000")
    assert call.keypad and "PIN was not correct" in call.reply
    call.type(PIN)
    assert "sent a code" in call.reply


def test_a_wrong_code_can_be_retried(v_client):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    call.type(PIN)
    call.type("111111")
    assert call.keypad and "code was not correct" in call.reply
    call.type(OTP)
    assert "you are verified" in call.reply


def test_three_wrong_pins_lock_the_link_and_tell_the_bank(v_client, v_db, clock):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    for wrong in ("0000", "1111"):
        call.type(wrong)
        assert "not correct" in call.reply
    call.type("2222")
    assert not call.keypad and "I will let Demo Bank know" in call.reply and "anything else" in call.reply
    handoff = v_db.scalar(select(Handoff))
    assert (handoff.reason, handoff.provider) == ("verification_failed", "demobank")
    assert links.locked(v_db.scalar(select(ServiceLink)), clock())


def test_the_lock_survives_hanging_up_and_calling_back_and_ends(v_client, v_db, clock):
    first = Caller(v_client)
    first.say("I want my bank balance")
    first.say("yes")
    for wrong in ("0000", "1111", "2222"):
        first.type(wrong)
    second = Caller(v_client)
    second.say("I want my bank balance")
    second.say("yes")
    assert not second.keypad and "cannot continue with Demo Bank right now" in second.reply
    clock.advance(16 * 60)  # the lockout is 15 minutes
    third = Caller(v_client)
    third.say("I want my bank balance")
    third.say("yes")
    assert third.keypad and "enter your PIN" in third.reply


def test_a_success_clears_earlier_failures(v_client, v_db):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    call.type("0000")
    call.type(PIN)
    call.type(OTP)
    link = v_db.scalar(select(ServiceLink))
    v_db.refresh(link)
    assert link.failed_attempts == 0 and link.last_verified_at is not None


def test_typing_nothing_before_the_timeout_stops_asking_politely(v_client):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    call.type("")
    assert not call.keypad and "No problem" in call.reply and "anything else" in call.reply
    assert "CI Store" in call.say("I want to buy something")  # the call carries on with something else


def test_a_second_balance_check_inside_the_window_asks_nothing(v_client, v_db):
    call = Caller(v_client)
    call.verified()
    call.say("my balance again please")
    assert not call.keypad
    assert "Your Demo Bank balance is 42,500 naira" in call.reply


def test_the_pin_is_asked_again_after_a_few_minutes_in_the_call(v_client, clock):
    call = Caller(v_client)
    call.verified()
    clock.advance(200)
    call.say("my balance again please")
    assert call.keypad and "It has been a few minutes" in call.reply
    call.type(PIN)
    assert "you are verified" in call.reply  # no second code: only the PIN is repeated


def test_a_different_sensitive_resource_asks_again(v_client):
    call = Caller(v_client)
    call.verified()
    call.say("I want to send money to my mum")
    assert call.keypad and "different part of your account" in call.reply


def test_random_checks_can_be_switched_on(v_client, v_app, clock):
    v_app.state.svc.settings.verify_random_rate = 1.0
    call = Caller(v_client)
    call.verified()
    clock.advance(60)  # a random check never comes right after a PIN
    call.say("my balance again please")
    assert call.keypad and "Just to be sure it is still you" in call.reply


def test_open_banking_information_needs_no_verification(v_client):
    call = Caller(v_client)
    call.say("what are your fees")
    call.say("yes")
    assert not call.keypad and "Before I open" not in call.reply


def test_speech_during_a_pin_step_is_dropped_unheard(v_client, v_db):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    assert call.keypad
    call.say("my pin is four eight two one")
    assert call.keypad and "Never say your PIN or code out loud" in call.reply
    turn = v_db.scalars(select(CallTurn).order_by(CallTurn.seq.desc())).first()
    assert turn.transcript is None and turn.audio_path is None and turn.recording_url is None and turn.asr_model is None


def test_the_pin_and_code_are_never_stored_or_logged(v_client, v_db, caplog):
    caplog.set_level(logging.DEBUG)
    call = Caller(v_client)
    call.verified()
    stored = stored_text(v_db)
    assert PIN not in stored and OTP not in stored
    assert PIN not in caplog.text
    sms = v_db.scalar(select(Notification).where(Notification.template == "otp"))
    assert sms and OTP not in sms.body  # the audit row says a code was sent, not what it was


def test_keypad_prompts_collect_digits_and_do_not_record(v_client):
    call = Caller(v_client)
    call.say("I want my bank balance")
    call.say("yes")
    xml = call.last.text
    assert "<GetDigits" in xml and 'numDigits="4"' in xml and "<Record" not in xml and "<Redirect>" in xml  # a timeout falls through to Redirect


def test_the_call_record_notes_the_steps_without_digits(v_client, v_db):
    call = Caller(v_client)
    call.verified()
    turns = list(v_db.scalars(select(CallTurn).order_by(CallTurn.seq)))
    keypad = [t for t in turns if t.asr_model == "keypad"]
    assert len(keypad) == 2 and all(t.transcript is None and t.audio_path is None for t in keypad)
    assert [t.llm_json for t in keypad] == [{"verify": "pin", "ok": True}, {"verify": "otp", "ok": True}]
