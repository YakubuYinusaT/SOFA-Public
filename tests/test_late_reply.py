"""An answer that arrives after the caller was told it is taking too long is not lost: SOFA phones back with it."""

import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sofa.main import create_app
from sofa.models import Customer, LateReply
from sofa.routes import voice
from sofa.services import latereply
from tests.conftest import SECRET
from tests.test_outbound import FakeVoice

NOW = datetime.now(timezone.utc)


def redirect_of(text):
    return re.search(r"<Redirect>(.*?)</Redirect>", text).group(1).replace("&amp;", "&").replace("http://test", "")


@pytest.fixture
def slow_app(settings, monkeypatch):
    settings.filler_after_seconds = 0
    settings.filler_every_seconds = 0.02
    settings.gateway_number = "+2347000000000"
    gate = {"open": False}

    async def slow(svc, session_id, recording_url, secret, turn_id):
        while not gate["open"]:
            await asyncio.sleep(0.01)
        urls = ["http://test/audio/abc.wav"]
        return voice.voicexml.record(urls, "http://test/voice/turn/x"), "Your balance is ready. Anything else?", False

    monkeypatch.setattr(voice, "_process_turn", slow)
    app = create_app(settings)
    app.state.gate = gate
    return app


def give_up(client):
    r = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "s1"})
    for _ in range(voice.WAIT_STAGES + 1):
        r = client.post(redirect_of(r.text), data={"sessionId": "s1"})
    assert "<Record" in r.text and "/voice/turn/" in r.text  # the apology, "anything else?", and a listening line


def test_the_answer_that_arrives_after_the_apology_is_phoned_back(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app) as client:
        with svc.session_factory() as db:
            customer = Customer(phone="+2348011111111", language="en")
            db.add(customer)
            db.commit()
            cid = str(customer.id)
        svc.sessions.set("s1", {"customer_id": cid, "lang": "en", "secret": SECRET, "gateway": True})
        give_up(client)
        slow_app.state.gate["open"] = True
        client.post(f"/voice/turn/{SECRET}", data={"sessionId": "nothing-here"})  # lets the loop run so the work can finish
        for _ in range(50):
            with svc.session_factory() as db:
                row = db.scalar(select(LateReply))
            if row:
                break
            asyncio.run(asyncio.sleep(0.02))
        assert row and row.status == "queued" and "balance" in row.text and not row.ends
        # the scheduler phones back
        svc.settings.at_api_key = "test-key"
        svc.voice = FakeVoice()
        with svc.session_factory() as db:
            assert asyncio.run(latereply.place_due(db, svc, NOW)) == 1
        assert svc.voice.calls[0][:2] == ("+2347000000000", "+2348011111111") and svc.voice.calls[0][2].startswith("late-")
        # the caller answers: an apology, the answer, and the line stays open with the same session
        r = client.post(f"/voice/inbound/{SECRET}", data={"sessionId": "ATV-9", "clientRequestId": svc.voice.calls[0][2], "direction": "Outbound"})
        assert r.text.count("<Play") == 2 and "<Record" in r.text and "Hangup" not in r.text  # the apology, then the stored answer
        assert svc.sessions.get("ATV-9")["customer_id"] == cid
        with svc.session_factory() as db:
            assert db.scalar(select(LateReply)).status == "answered"


def test_an_answer_the_caller_never_needed_to_wait_for_is_not_phoned_back(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app) as client:
        svc.sessions.set("s1", {"customer_id": "00000000-0000-0000-0000-000000000001", "lang": "en"})
        slow_app.state.gate["open"] = True
        r = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "s1"})
        assert "<Record" in r.text and "Redirect" not in r.text  # the answer was ready in time: played at once
        with svc.session_factory() as db:
            assert db.scalar(select(LateReply)) is None


def test_an_old_late_answer_is_dropped_not_phoned(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app):
        with svc.session_factory() as db:
            customer = Customer(phone="+2348022222222")
            db.add(customer)
            db.flush()
            row = LateReply(customer_id=customer.id, session_id="s1", text="old", audio="[]")
            db.add(row)
            db.commit()
            svc.settings.at_api_key = "test-key"
            svc.voice = FakeVoice()
            later = NOW + timedelta(minutes=svc.settings.late_reply_expire_minutes + 5)
            assert asyncio.run(latereply.place_due(db, svc, later)) == 0
            db.refresh(row)
            assert row.status == "expired" and not svc.voice.calls


def _customer(svc, link=False):
    from sofa.gateway import links

    with svc.session_factory() as db:
        customer = Customer(phone="+2348033333333", language="en")
        db.add(customer)
        db.flush()
        if link:
            links.link(db, customer.id, "banking", "demobank")
        db.commit()
        return str(customer.id)


def _wait_for_row(svc):
    for _ in range(50):
        with svc.session_factory() as db:
            row = db.scalar(select(LateReply))
        if row:
            return row
        asyncio.run(asyncio.sleep(0.02))


def test_an_answer_about_a_bank_account_is_never_phoned_the_caller_is_told_to_call_back(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app) as client:
        cid = _customer(svc, link=True)
        svc.sessions.set("s1", {"customer_id": cid, "lang": "en", "domain": "banking", "gateway": True})
        r = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "s1"})
        for _ in range(voice.WAIT_STAGES + 1):
            r = client.post(redirect_of(r.text), data={"sessionId": "s1"})
        assert "<Record" in r.text and "/voice/turn/" in r.text  # the apology and "anything else?", then it listens
        slow_app.state.gate["open"] = True
        client.post(f"/voice/turn/{SECRET}", data={"sessionId": "nothing-here"})
        asyncio.run(asyncio.sleep(0.2))
        with svc.session_factory() as db:
            assert db.scalar(select(LateReply)) is None  # nothing queued: no call back with account details


def test_the_call_back_asks_for_the_pin_first_and_only_then_plays_the_answer(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app) as client:
        cid = _customer(svc, link=True)
        svc.sessions.set("s1", {"customer_id": cid, "lang": "en", "gateway": True})  # not a banking turn: callable
        give_up(client)
        slow_app.state.gate["open"] = True
        client.post(f"/voice/turn/{SECRET}", data={"sessionId": "nothing-here"})
        row = _wait_for_row(svc)
        svc.settings.at_api_key = "test-key"
        svc.voice = FakeVoice()
        with svc.session_factory() as db:
            asyncio.run(latereply.place_due(db, svc, NOW))
        r = client.post(f"/voice/inbound/{SECRET}", data={"sessionId": "ATV-5", "clientRequestId": svc.voice.calls[0][2], "direction": "Outbound"})
        assert "<GetDigits" in r.text and "abc.wav" not in r.text  # the answer is not played yet
        r = client.post(f"/voice/late-pin/{SECRET}/{row.id}", data={"sessionId": "ATV-5", "dtmfDigits": "9999"})  # wrong
        assert "<GetDigits" in r.text and "abc.wav" not in r.text
        r = client.post(f"/voice/late-pin/{SECRET}/{row.id}", data={"sessionId": "ATV-5", "dtmfDigits": svc.settings.mock_bank_pin})
        assert "abc.wav" in r.text and "<Record" in r.text
        assert svc.sessions.get("ATV-5")["customer_id"] == cid


def test_three_wrong_pins_on_the_call_back_and_the_answer_is_never_read_out(slow_app):
    svc = slow_app.state.svc
    with TestClient(slow_app) as client:
        cid = _customer(svc, link=True)
        svc.sessions.set("s1", {"customer_id": cid, "lang": "en", "gateway": True})
        give_up(client)
        slow_app.state.gate["open"] = True
        client.post(f"/voice/turn/{SECRET}", data={"sessionId": "nothing-here"})
        row = _wait_for_row(svc)
        for _ in range(svc.settings.late_reply_pin_attempts):
            r = client.post(f"/voice/late-pin/{SECRET}/{row.id}", data={"sessionId": "ATV-6", "dtmfDigits": "0000"})
            assert "abc.wav" not in r.text
        assert "<GetDigits" not in r.text  # no more tries
