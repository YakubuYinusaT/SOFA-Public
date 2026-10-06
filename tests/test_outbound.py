import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from scripts.seed import DEMO_NUMBER
from sofa.audio import mock_recording_url
from sofa.clients.voice import Placed, VoiceClient
from sofa.models import Call, Customer, Order, OutboundCall
from sofa.services import outbound
from tests.conftest import SECRET
from tests.test_call_flow import MUSA, SPEC_CONVERSATION

UTC = timezone.utc
NOON = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)     # 13:00 in Lagos: calls allowed
LATE = datetime(2026, 9, 29, 22, 0, tzinfo=UTC)     # 23:00 in Lagos: quiet hours


class FakeVoice:
    """Stands in for the Africa's Talking Voice API."""

    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []

    async def call(self, from_number, to_number, client_request_id):
        self.calls.append((from_number, to_number, client_request_id))
        return self.results.pop(0) if self.results else Placed(ok=True, session_id=f"ATV-{len(self.calls)}")


@pytest.fixture
def live(app, job):
    """Live mode: an API key is set and the phone provider is a fake. Depends on `job` so the setup call and its
    SMS run in mock mode first."""
    svc = app.state.svc
    svc.settings.at_api_key = "test-key"
    svc.voice = FakeVoice()
    return svc


@pytest.fixture
def job(call, db):
    call(SPEC_CONVERSATION)  # Musa places an order for 16,400 naira
    order = db.scalar(select(Order))
    j = outbound.schedule_callback(db, order, "payment_received")
    db.commit()
    return j


def place(svc, db, now=NOON):
    return asyncio.run(outbound.place_due_calls(db, svc, now))


def answer(client, session="ATV-1", **extra):
    data = {"sessionId": session, "direction": "Outbound", "isActive": "1", **extra}
    return client.post(f"/voice/inbound/{SECRET}", data=data)


# ---- placing calls ----------------------------------------------------------------------------------


def test_mock_mode_marks_the_call_simulated_and_dials_nobody(app, client, db, job):
    assert app.state.svc.settings.sms_is_mock  # no AT_API_KEY: the real client only logs
    assert place(app.state.svc, db) == 1
    db.expire_all()
    assert db.get(OutboundCall, job.id).status == "simulated"


def test_live_call_uses_the_shop_number_as_caller_id_and_the_job_id(live, client, db, job):
    assert place(live, db) == 1
    db.expire_all()
    j = db.get(OutboundCall, job.id)
    assert (j.status, j.attempt, j.provider_session_id) == ("calling", 1, "ATV-1")
    assert live.voice.calls == [(DEMO_NUMBER, MUSA, str(job.id))]
    assert place(live, db) == 0  # the same job is not dialled twice


def test_quiet_hours_hold_the_call(live, client, db, job):
    assert place(live, db, LATE) == 0
    assert live.voice.calls == [] and db.get(OutboundCall, job.id).status == "queued"


def test_switch_off_stops_all_calls(live, client, db, job):
    live.settings.outbound_calls_enabled = False
    assert place(live, db) == 0 and live.voice.calls == []


def test_api_error_retries_then_fails(live, client, db, job):
    live.voice = FakeVoice([Placed(ok=False, error="InsufficientBalance")] * 2)
    place(live, db)
    db.expire_all()
    j = db.get(OutboundCall, job.id)
    assert j.status == "queued" and j.last_error == "InsufficientBalance"
    assert place(live, db, NOON + timedelta(minutes=5)) == 0  # not before the retry time
    place(live, db, NOON + timedelta(minutes=21))
    db.expire_all()
    assert db.get(OutboundCall, job.id).status == "failed" and len(live.voice.calls) == 2


def test_cancelled_order_is_not_phoned(live, client, db, job):
    db.scalar(select(Order)).status = "cancelled"
    db.commit()
    assert place(live, db) == 0 and db.get(OutboundCall, job.id).status == "expired"


def test_old_call_is_dropped(live, client, db, job):
    # Two days after the call was queued, measured from the real clock (the job is stamped with the real time it was created, so a fixed
    # date in the test went stale as soon as that date had passed), at a time of day when calls are allowed.
    created = job.created_at if job.created_at.tzinfo else job.created_at.replace(tzinfo=UTC)
    assert place(live, db, (created + timedelta(days=2)).replace(hour=12, minute=0)) == 0
    assert db.get(OutboundCall, job.id).status == "expired"


def test_one_customer_is_not_called_more_than_the_daily_limit(live, client, db, job):
    live.settings.outbound_max_per_customer_per_day = 1
    second = outbound.schedule_callback(db, db.scalar(select(Order)), "dispatched")
    db.commit()
    assert place(live, db) == 1
    db.expire_all()
    assert {db.get(OutboundCall, i).status for i in (job.id, second.id)} == {"calling", "skipped_limit"}


# ---- no answer --------------------------------------------------------------------------------------


def test_unanswered_call_is_retried_once_then_given_up(live, client, db, job):
    place(live, db)
    client.post(f"/voice/events/{SECRET}", data={"sessionId": "ATV-1", "isActive": "0", "hangupCause": "NoAnswer"})
    db.expire_all()
    j = db.get(OutboundCall, job.id)
    assert j.status == "queued" and j.last_error == "NoAnswer"
    j.not_before = NOON + timedelta(minutes=20)  # the event handler used the real clock; the test clock is fixed at NOON
    db.commit()
    place(live, db, NOON + timedelta(minutes=21))
    client.post(f"/voice/events/{SECRET}", data={"sessionId": "ATV-2", "isActive": "0"})
    db.expire_all()
    assert db.get(OutboundCall, job.id).status == "no_answer" and len(live.voice.calls) == 2


def test_a_call_the_provider_never_reports_is_treated_as_unanswered(live, client, db, job):
    place(live, db)
    place(live, db, NOON + timedelta(minutes=4))  # ring time (3 minutes) has passed
    db.expire_all()
    assert db.get(OutboundCall, job.id).status == "queued"


# ---- the customer picks up --------------------------------------------------------------------------


def test_answered_call_says_the_news_and_hangs_up(live, client, db, job):
    place(live, db)
    r = answer(client, clientRequestId=str(job.id))
    assert "Your payment of 16,400 naira has been received" in r.headers["X-Sofa-Reply-Text"]
    assert "<Record" not in r.text and "<Play" in r.text  # nothing to ask, so no listening: it plays and the call ends
    db.expire_all()
    j = db.get(OutboundCall, job.id)
    c = db.get(Call, j.call_id)
    assert j.status == "answered" and (c.direction, c.from_number, c.to_number) == ("outbound", DEMO_NUMBER, MUSA)


def test_answer_is_matched_by_the_customer_number_when_the_id_is_not_echoed(live, client, db, job):
    place(live, db)
    r = answer(client, callerNumber=MUSA, destinationNumber=DEMO_NUMBER)
    assert "payment of 16,400 naira" in r.headers["X-Sofa-Reply-Text"]


def test_an_outbound_call_we_do_not_know_gets_a_polite_goodbye(live, client, db, job):
    r = answer(client, clientRequestId=str(uuid.uuid4()))
    assert "Thank you" in r.text and "<Record" not in r.text


def test_delivered_call_ends_with_a_question_and_listens(live, client, db, job):
    j = outbound.schedule_callback(db, db.scalar(select(Order)), "delivered")
    job_ = db.get(OutboundCall, job.id)
    job_.status = "answered"  # the payment call is done; only the delivery call is pending
    db.commit()
    place(live, db)
    r = answer(client, clientRequestId=str(j.id))
    assert "Is there anything else you need" in r.headers["X-Sofa-Reply-Text"] and "<Record" in r.text
    turn = client.post(f"/voice/turn/{SECRET}", data={"sessionId": "ATV-1", "recordingUrl": mock_recording_url("No that is all")})
    assert turn.status_code == 200 and "X-Sofa-Reply-Text" in turn.headers
    db.expire_all()
    assert len(db.get(Call, db.get(OutboundCall, j.id).call_id).turns) == 1


def test_direct_outbound_route_is_secret_protected(live, client, db, job):
    assert client.post(f"/voice/outbound/wrong/{job.id}").status_code == 404
    r = client.post(f"/voice/outbound/{SECRET}/{job.id}", data={"sessionId": "S9"})
    assert "payment of 16,400 naira" in r.headers["X-Sofa-Reply-Text"]


# ---- the Africa's Talking request -------------------------------------------------------------------


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


def voice_client(monkeypatch, resp):
    sent = {}

    class Http:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, data=None, headers=None):
            sent.update(url=url, data=data, headers=headers)
            if isinstance(resp, Exception):
                raise resp
            return resp

    monkeypatch.setattr("sofa.clients.voice.httpx.AsyncClient", Http)
    from sofa.config import Settings

    return VoiceClient(Settings(at_api_key="k", at_username="acme", _env_file=None)), sent


def test_request_matches_the_africas_talking_call_api(monkeypatch):
    vc, sent = voice_client(monkeypatch, Resp(200, {"entries": [{"phoneNumber": "+2348055550001", "status": "Queued", "sessionId": "ATVId_1"}]}))
    got = asyncio.run(vc.call("+2348001112222", "+2348055550001", "job-1"))
    assert got == Placed(ok=True, session_id="ATVId_1")
    assert sent["url"] == "https://voice.africastalking.com/call" and sent["headers"]["apiKey"] == "k"
    assert sent["data"] == {"username": "acme", "from": "+2348001112222", "to": "+2348055550001", "clientRequestId": "job-1"}


@pytest.mark.parametrize("resp,error", [
    (Resp(200, {"entries": [{"status": "InvalidPhoneNumber"}]}), "InvalidPhoneNumber"),
    (Resp(401, {}), "http 401"),
    (Resp(200, {"entries": [], "errorMessage": "Insufficient balance"}), "Insufficient balance"),
])
def test_rejected_calls_report_why(monkeypatch, resp, error):
    vc, _ = voice_client(monkeypatch, resp)
    got = asyncio.run(vc.call("+2348001112222", "+2348055550001", "job-1"))
    assert not got.ok and got.error == error


def test_network_failure_is_an_error_not_a_crash(monkeypatch):
    import httpx

    vc, _ = voice_client(monkeypatch, httpx.ConnectTimeout("slow"))
    got = asyncio.run(vc.call("+2348001112222", "+2348055550001", "job-1"))
    assert not got.ok and got.error.startswith("network")


def test_order_page_shows_the_status_calls(live, client, db, job):
    from tests.test_admin_pages import login

    place(live, db)
    login(client)
    page = client.get(f"/admin/orders/{job.order_id}")
    assert page.status_code == 200
    assert "Status calls to the customer" in page.text and "payment received" in page.text and "calling" in page.text
