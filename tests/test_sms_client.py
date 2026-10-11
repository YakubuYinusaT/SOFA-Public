"""The text message client: a message counts as sent only when Africa's Talking accepted it for that number, and a refusal says why."""

import asyncio
import logging

import httpx

from sofa.clients import sms as sms_module
from sofa.clients.sms import SMSClient
from sofa.config import Settings

REAL_CLIENT = httpx.AsyncClient          # kept here: the patch below replaces httpx.AsyncClient for the whole module


def client_with(monkeypatch, handler, **settings):
    """An SMSClient whose HTTP calls go to `handler` instead of the internet. Returns (client, the requests it received)."""
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(sms_module.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(respond), **kw))
    base = dict(_env_file=None, at_api_key="a-key", at_username="CiitAfrica", at_sandbox=False)
    return SMSClient(Settings(**{**base, **settings})), seen


def answer(code, status, **extra):
    recipient = {"statusCode": code, "number": "+2348012345678", "status": status, "cost": "NGN 4.0000", "messageId": "ATXid_1", **extra}
    return httpx.Response(201, json={"SMSMessageData": {"Message": "Sent to 1/1 Total Cost: NGN 4.0000", "Recipients": [recipient]}})


def run(coro):
    return asyncio.run(coro)


def test_a_message_africas_talking_accepted_is_sent_and_goes_to_the_live_address(monkeypatch):
    client, seen = client_with(monkeypatch, lambda r: answer(101, "Success"), sms_sender_id="Talk")
    assert run(client.send("+2348012345678", "hello")) == ("sent", "ATXid_1")
    request = seen[0]
    assert request.url.host == "api.africastalking.com" and request.headers["apiKey"] == "a-key"
    body = request.content.decode()
    assert "username=CiitAfrica" in body and "from=Talk" in body and "to=%2B2348012345678" in body


def test_the_sandbox_address_is_used_only_when_asked_for(monkeypatch):
    client, seen = client_with(monkeypatch, lambda r: answer(101, "Success"), at_sandbox=True)
    run(client.send("+2348012345678", "hello"))
    assert seen[0].url.host == "api.sandbox.africastalking.com"
    client, seen = client_with(monkeypatch, lambda r: answer(101, "Success"))
    run(client.send("+2348012345678", "hello"))
    assert "from=" not in seen[0].content.decode()          # no sender id set: none is sent, and the default is used


def test_a_message_refused_inside_an_ok_answer_is_a_failure_with_its_reason(monkeypatch, caplog):
    for code, status in ((402, "InvalidSenderId"), (405, "InsufficientBalance"), (406, "UserInBlacklist"), (403, "InvalidPhoneNumber")):
        client, _ = client_with(monkeypatch, lambda r, c=code, s=status: answer(c, s))
        with caplog.at_level(logging.ERROR, logger="sofa.sms"):
            caplog.clear()
            status_out, message_id, reason = run(client.deliver("+2348012345678", "hello"))
        assert (status_out, message_id) == ("failed", None) and status in reason and str(code) in reason
        assert status in caplog.text                       # and the server's log says why: it is never silent


def test_a_missing_recipient_or_an_http_error_is_a_failure(monkeypatch):
    client, _ = client_with(monkeypatch, lambda r: httpx.Response(201, json={"SMSMessageData": {"Message": "nothing", "Recipients": []}}))
    assert run(client.send("+2348012345678", "hello")) == ("failed", None)
    client, _ = client_with(monkeypatch, lambda r: httpx.Response(401, text="The supplied authentication is invalid"))
    status, message_id, reason = run(client.deliver("+2348012345678", "hello"))
    assert (status, message_id) == ("failed", None) and "401" in reason
    client, _ = client_with(monkeypatch, lambda r: httpx.Response(200, text="not json"))
    assert run(client.send("+2348012345678", "hello")) == ("failed", None)


def test_queued_and_processed_count_as_accepted(monkeypatch):
    for code, status in ((100, "Processed"), (102, "Queued")):
        client, _ = client_with(monkeypatch, lambda r, c=code, s=status: answer(c, s))
        assert run(client.send("+2348012345678", "hello")) == ("sent", "ATXid_1")


def test_with_no_key_nothing_is_sent(monkeypatch):
    client, seen = client_with(monkeypatch, lambda r: answer(101, "Success"), at_api_key="")
    assert run(client.send("+2348012345678", "hello")) == ("mock_sent", None) and not seen


# ---- delivery reports -------------------------------------------------------------------------------------------------

from sqlalchemy import select                                # noqa: E402  (kept with the tests that use them)

from sofa.models import Notification                         # noqa: E402
from tests.conftest import SECRET                            # noqa: E402


def sent_text(app, message_id="ATXid_1"):
    with app.state.svc.session_factory() as db:
        db.add(Notification(channel="sms", template="invoice", to_number="+2348012345678", body="Pay NGN 5,000", status="sent", provider_message_id=message_id))
        db.commit()


def status_of(app, message_id="ATXid_1"):
    with app.state.svc.session_factory() as db:
        return db.scalar(select(Notification).where(Notification.provider_message_id == message_id)).status


def test_the_delivery_address_answers_a_visit_only_with_the_right_secret(client):
    assert client.get(f"/sms/delivery/{SECRET}").text == "ok" and client.head(f"/sms/delivery/{SECRET}").status_code == 200
    assert client.get("/sms/delivery/wrong").status_code == 404
    assert client.post("/sms/delivery/wrong", data={"id": "ATXid_1", "status": "Success"}).status_code == 404


def test_a_delivery_report_updates_the_text_it_belongs_to(client, app):
    sent_text(app)
    client.post(f"/sms/delivery/{SECRET}", data={"id": "ATXid_1", "status": "Buffered", "phoneNumber": "+2348012345678", "networkCode": "62130"})
    assert status_of(app) == "buffered"
    client.post(f"/sms/delivery/{SECRET}", data={"id": "ATXid_1", "status": "Success", "phoneNumber": "+2348012345678"})
    assert status_of(app) == "delivered"
    client.post(f"/sms/delivery/{SECRET}", data={"id": "ATXid_1", "status": "Sent"})       # a late, older report does not undo it
    assert status_of(app) == "delivered"


def test_a_failed_delivery_is_recorded_and_the_reason_is_logged(client, app, caplog):
    sent_text(app)
    with caplog.at_level(logging.ERROR, logger="sofa.voice"):
        res = client.post(f"/sms/delivery/{SECRET}", data={"id": "ATXid_1", "status": "Failed", "phoneNumber": "+2348012345678", "failureReason": "AbsentSubscriber"})
    assert res.status_code == 200 and status_of(app) == "failed"
    assert "AbsentSubscriber" in caplog.text and "2348012345678" not in caplog.text       # the reason, never the whole number


def test_a_report_for_an_unknown_text_or_an_unknown_status_is_ignored(client, app):
    sent_text(app)
    assert client.post(f"/sms/delivery/{SECRET}", data={"id": "nobody", "status": "Success"}).status_code == 200
    assert client.post(f"/sms/delivery/{SECRET}", data={"id": "ATXid_1", "status": "Strange"}).status_code == 200
    assert client.post(f"/sms/delivery/{SECRET}", data={}).status_code == 200
    assert status_of(app) == "sent"
