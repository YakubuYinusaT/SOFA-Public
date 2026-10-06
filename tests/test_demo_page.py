"""The judges' page: off unless switched on, and when on it shows the calls without exposing phone numbers or long digit strings."""

from sqlalchemy import select

from sofa.models import Call, CallTurn
from sofa.routes.demo import mask_phone, scrub
from tests.test_call_flow import MUSA, SPEC_CONVERSATION


def test_the_page_does_not_exist_unless_it_is_switched_on(client):
    assert client.get("/demo").status_code == 404


def test_masking_and_scrubbing():
    assert mask_phone("+2348055550101") == "**********0101"
    assert scrub("my account is 2012345678 and my card 123456") == "my account is [hidden] and my card [hidden]"
    assert scrub("send 5000 to Tunde") == "send 5000 to Tunde"


def test_the_page_shows_calls_with_the_numbers_masked(client, app, call, db):
    app.state.svc.settings.demo_page_enabled = True
    call(SPEC_CONVERSATION, caller=MUSA)
    html = client.get("/demo").text
    assert "Everyday services, by voice" in html and "Latest calls" in html
    assert MUSA not in html and MUSA[-4:] in html  # only the last four digits
    assert "Indomie" in html  # what was said and what Sofa did are shown
    assert "Bank service desk" in html and "dev-provider-token" in html  # demo logins are shown while the passwords are still the demo ones


def test_the_demo_logins_are_hidden_once_real_passwords_are_set(client, app):
    s = app.state.svc.settings
    s.demo_page_enabled, s.provider_token = True, "a-long-random-password"
    html = client.get("/demo").text
    assert "a-long-random-password" not in html and "Demo passwords" not in html


def test_what_is_typed_on_the_keypad_is_never_shown(client, app, db):
    app.state.svc.settings.demo_page_enabled = True
    c = Call(provider_session_id="s", from_number="+2348055550101", to_number="+2347000000000")
    db.add(c)
    db.flush()
    db.add(CallTurn(call_id=c.id, seq=1, asr_model="keypad", transcript=None, reply_text="Thank you. Your balance is 42,500 naira."))
    db.commit()
    html = client.get("/demo").text
    assert "(typed on the keypad)" in html
