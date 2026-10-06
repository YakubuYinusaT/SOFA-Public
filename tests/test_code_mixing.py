"""Lagos callers mix Yoruba, English and Pidgin inside one sentence. These tests cover what that needs:
Yoruba words surviving text cleaning, both speech models hearing every turn, a mixed first sentence not
being interrupted by a "which language?" question, and the reply language following the caller calmly."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from mocks.rules import parse
from scripts.seed import DEMO_NUMBER, seed
from sofa.audio import mock_mixed_url, mock_recording_url
from sofa.config import Settings
from sofa.main import create_app
from sofa.models import Call, CallTurn, Customer
from sofa.textutil import clean, normalize_phrase, parse_number
from tests.conftest import SECRET
from tests.test_admin_pages import login, post
from tests.test_languages import app_with

WHO = "+2348055550001"


# ---- Yoruba words must survive text cleaning ---------------------------------------------------------------------


def test_tone_marks_and_dots_no_longer_delete_letters():
    """Before this fix the cleaner deleted every letter with a tone mark or dot under it: 'mẹ́ta' became 'm ta'."""
    assert clean("mẹ́ta") == "meta"
    assert clean("méjì") == "meji"
    assert clean("Bẹ́ẹ̀ ni, ó tọ̀nà") == "bee ni o tona"
    assert clean("Ẹ jọ̀wọ́, ṣé ẹ ní Peak milk?") == "e jowo se e ni peak milk"
    assert clean("Peak Milk Tin") == "peak milk tin"  # English is untouched


@pytest.mark.parametrize("said,number", [("kan", 1), ("méjì", 2), ("mẹ́ta", 3), ("mẹ́rin", 4), ("márùn-ún", 5), ("mẹ́fà", 6),
                                          ("mẹ́je", 7), ("mẹ́jọ", 8), ("mẹ́wàá", 10), ("ogún", 20), ("ọgbọ̀n", 30), ("three", 3)])
def test_yoruba_and_english_number_words_both_count(said, number):
    words = clean(said).split()
    assert parse_number(words[0]) == number


def test_everyday_yoruba_words_are_not_mistaken_for_numbers():
    for word in ("ewa", "arun", "eran"):  # beans, illness, meat: real shop words, not numerals
        assert parse_number(clean(word)) is None


def test_mixed_yoruba_english_order_is_read_as_one_request():
    got = parse("Mo fẹ́ carton méjì ti Indomie Super Pack")
    assert got["intent"] == "place_order"
    item = got["items"][0]
    assert (item["quantity"], item["unit"], item["spoken_name"]) == (2, "carton", "indomie super pack")
    assert parse("Bẹ́ẹ̀ ni", stage="await_confirm")["intent"] == "confirm"
    assert parse("Rárá", stage="await_confirm")["intent"] == "deny"
    assert normalize_phrase("abeg give me Peak milk") == "peak milk"  # Pidgin and Yoruba filler is ignored when matching


# ---- what the call does with mixed speech ------------------------------------------------------------------------------


def send(client, sid, readings, who=WHO):
    return client.post(f"/voice/turn/{SECRET}", data={"sessionId": sid, "callerNumber": who, "destinationNumber": DEMO_NUMBER,
                                                        "recordingUrl": mock_mixed_url(readings)})


def begin(client, sid, who=WHO):
    client.post(f"/voice/inbound/{SECRET}", data={"sessionId": sid, "callerNumber": who, "destinationNumber": DEMO_NUMBER})


def reply(response):
    return response.headers["X-Sofa-Reply-Text"]


def state_of(app, who=WHO):
    with app.state.svc.session_factory() as db:
        call = db.scalar(select(Call).where(Call.from_number == who).order_by(Call.started_at.desc()))
        cust = db.scalar(select(Customer).where(Customer.phone == who))
        return call.language, cust.language


def test_every_turn_is_heard_by_both_models_and_the_second_reading_reaches_the_llm(settings, monkeypatch):
    app, asked = app_with(settings, "en,yo")
    seen = {}
    real = app.state.svc.llm.parse_turn

    async def spy(system, transcript, alternatives=None):
        seen.update(system=system, transcript=transcript, alternatives=alternatives)
        return await real(system, transcript, alternatives)

    monkeypatch.setattr(app.state.svc.llm, "parse_turn", spy)
    with TestClient(app) as client:
        begin(client, "m1")
        r = send(client, "m1", {"en": ("more fair carton major Indomie Super Pack", -0.9),
                                "yo": ("Mo fe carton meji ti Indomie Super Pack", -0.5)})
        assert "2 cartons of Indomie Super Pack" in reply(r) and "14,000 naira" in reply(r)
        assert sorted(asked) == ["en", "yo"]  # both models, on a Yoruba-led sentence
        assert seen["transcript"] == "Mo fe carton meji ti Indomie Super Pack"  # the more confident reading
        assert seen["alternatives"] == ["more fair carton major Indomie Super Pack"]  # the other one, as a second opinion
        assert "mix Yoruba, English and Nigerian Pidgin" in seen["system"] and "Bẹ́ẹ̀ ni" in seen["system"]
        with app.state.svc.session_factory() as db:
            t = db.scalar(select(CallTurn))
            assert t.asr_alternatives == [{"language": "en", "model": "mock-asr-en", "text": "more fair carton major Indomie Super Pack", "confidence": -0.9}]


def test_identical_readings_are_not_stored_as_alternatives(settings):
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m2")
        send(client, "m2", {"en": ("How much is Peak milk tin?", -0.6), "yo": ("how much is peak milk tin", -0.4)})
        with app.state.svc.session_factory() as db:
            assert db.scalar(select(CallTurn)).asr_alternatives is None  # same words: nothing to compare


def test_a_mixed_first_sentence_is_not_interrupted_by_a_language_question(settings):
    """Neither model is confident (normal for mixed speech) but there is clearly an order in it: just take it."""
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m3")
        r = send(client, "m3", {"en": ("Peak milk tin three", -1.5), "yo": ("pik milk tin meta", -1.8)})
        assert "didn't catch your language" not in reply(r)
        assert "3 tins of Peak Milk Tin" in reply(r)
        assert state_of(app)[0] == "en"


def test_gibberish_still_gets_the_spoken_language_question(settings):
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m4")
        r = send(client, "m4", {"en": ("blah blah", -1.6), "yo": ("bla bla", -1.7)})
        assert "didn't catch your language" in reply(r)


# ---- the reply language follows the caller, calmly ----------------------------------------------------------------------------


YO_LEADS = {"en": ("Peak milk tin meji", -1.0), "yo": ("Peak milk tin meji", -0.4)}
EN_LEADS = {"en": ("Peak milk tin two", -0.4), "yo": ("Peak milk tin two", -1.0)}


def test_one_english_sentence_does_not_flip_a_yoruba_call(settings):
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m5")
        send(client, "m5", YO_LEADS)
        assert state_of(app)[0] == "yo"
        send(client, "m5", EN_LEADS)  # one English-leaning turn
        assert state_of(app)[0] == "yo"
        send(client, "m5", YO_LEADS)
        send(client, "m5", EN_LEADS)  # still not two in a row
        assert state_of(app)[0] == "yo"


def test_two_english_turns_in_a_row_move_the_conversation_to_english(settings):
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m6")
        send(client, "m6", YO_LEADS)
        send(client, "m6", EN_LEADS)
        send(client, "m6", EN_LEADS)
        assert state_of(app) == ("en", "en")  # and it is remembered for next time


def test_a_small_confidence_edge_never_switches_language(settings):
    app, _ = app_with(settings, "en,yo")
    close = {"en": ("Peak milk tin two", -0.5), "yo": ("Peak milk tin two", -0.6)}
    with TestClient(app) as client:
        begin(client, "m7")
        send(client, "m7", YO_LEADS)
        for _ in range(4):
            send(client, "m7", close)
        assert state_of(app)[0] == "yo"


def test_asking_for_a_language_by_name_switches_at_once(settings):
    app, _ = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m8")
        send(client, "m8", YO_LEADS)
        send(client, "m8", {"en": ("English please", -0.3), "yo": ("English please", -1.5)})
        assert state_of(app)[0] == "en"
        send(client, "m8", {"en": ("Yoruba", -1.0), "yo": ("Yoruba", -0.2)})
        assert state_of(app)[0] == "yo"


def test_a_language_name_inside_a_long_order_does_not_switch(settings):
    app, _ = app_with(settings, "en,yo")
    long_order = "I want three tins of Peak milk and the Yoruba style bread please"
    with TestClient(app) as client:
        begin(client, "m9")
        send(client, "m9", YO_LEADS)
        send(client, "m9", {"en": (long_order, -0.3), "yo": (long_order, -1.5)})
        assert state_of(app)[0] == "yo"  # only a short utterance that names a language counts as a request


def test_single_language_setup_does_not_run_two_models(settings):
    app, asked = app_with(settings, "en")
    with TestClient(app) as client:
        begin(client, "m10")
        send(client, "m10", {"en": ("How much is Peak milk tin?", -0.5)})
        assert asked == ["en"]


def test_dual_listening_can_be_switched_off(settings):
    settings.dual_asr = False
    app, asked = app_with(settings, "en,yo")
    with TestClient(app) as client:
        begin(client, "m11")
        send(client, "m11", {"en": ("How much is Peak milk tin?", -0.5), "yo": ("how much is peak milk tin", -0.4)})  # 1st turn: detection
        asked.clear()
        send(client, "m11", {"en": ("Do you have Peak milk tin?", -0.5), "yo": ("x", -0.4)})
        assert asked == ["yo"]  # locked language only, as before


# ---- evidence: code-mixing shows up in labels, metrics and the console ------------------------------------------------------------


def test_code_switching_is_counted_in_the_metrics_and_both_readings_are_shown(client, call, db):
    call(["How much is Peak milk tin?"])
    login(client)
    t = db.scalar(select(CallTurn))
    t.asr_alternatives = [{"language": "yo", "model": "m", "text": "pik milk tin meji", "confidence": -0.9}]
    db.commit()
    assert "Also heard" in client.get(f"/admin/calls/{t.call_id}").text
    assert "The other speech model heard" in client.get("/admin/label").text
    form = {"transcript": "How much is Peak milk tin?", "language": "en", "intent": "ask_price", "n_matches": "0", "flags": ["code-switching"]}
    post(client, f"/admin/label/{t.id}", form, page="/admin/label")
    page = client.get("/admin/metrics").text
    assert "Code-mixed" in page and "1 of 1" in page
