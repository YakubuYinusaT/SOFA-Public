"""Faults found on the first real phone calls (2026-10-09): what the caller said was heard, but the shop did not understand it."""

import io
import struct
import wave

import pytest
from sqlalchemy import select

from scripts.seed_agro import seed_agro
from sofa.audio import wav_rms
from sofa.clients.llm import TurnJSON
from sofa.config import Settings
from sofa.db import init_db, make_engine, make_session_factory
from sofa.dialogue.matching import match_product
from sofa.models import Merchant


@pytest.fixture
def anyio_backend():
    return "asyncio"


class NoRerank:
    async def rerank(self, phrase, names):
        return None


@pytest.fixture
def agro():
    factory = make_session_factory(make_engine("sqlite://"))
    init_db(factory.kw["bind"])
    seed_agro(factory)
    with factory() as db:
        yield db, db.scalar(select(Merchant))


@pytest.mark.anyio
async def test_the_plain_word_fertilizer_no_longer_means_can_fertilizer(agro):
    """'can' is a filler word ("can I have"), and removing it made 'CAN Fertilizer' the same as 'fertilizer'."""
    db, m = agro
    plain = await match_product(db, m.id, "a fertilizer", NoRerank(), Settings(_env_file=None))
    assert plain.status == "ambiguous" and {p.name for p, _ in plain.candidates} >= {"Urea Fertilizer", "NPK 15-15-15 Fertilizer"}
    can = await match_product(db, m.id, "can fertilizer", NoRerank(), Settings(_env_file=None))
    assert can.status == "match" and can.product.name == "CAN Fertilizer"
    assert (await match_product(db, m.id, "CAN", NoRerank(), Settings(_env_file=None))).product.name == "CAN Fertilizer"


def test_what_do_you_have_lists_the_shops_products(call):
    replies = call(["My name is Bola", "what do you have in the store?"])
    assert "We have" in replies[2] and "Which one" in replies[2] and "not_available" not in replies[2]


def test_a_request_to_buy_that_the_model_read_as_advice_is_answered_as_a_request(call, app, monkeypatch):
    llm = app.state.svc.llm

    async def as_advice(prompt, transcript, alternatives=None):
        return TurnJSON(intent="ask_advice"), {"intent": "ask_advice"}

    replies0 = call(["My name is Bola"])  # warm the call up with the normal model
    monkeypatch.setattr(llm, "parse_turn", as_advice)
    replies = call(["My name is Bola", "I need Indomie"])
    assert "I do not have a recommendation" not in replies[2]
    assert "Indomie" in replies[2]
    assert replies0


def wav(samples):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return buf.getvalue()


def test_loudness_tells_speech_from_a_quiet_line():
    assert wav_rms(wav([0] * 1600)) == 0.0
    assert wav_rms(wav([30, -30] * 800)) < 50
    assert wav_rms(wav([1500, -1500] * 800)) > 1000
    assert wav_rms(b"MOCK[en]:hello") > 1000  # the simulator's stand-in recordings count as speech


def test_a_recording_with_nobody_speaking_is_treated_as_silence_not_sent_to_the_recogniser(call, app, monkeypatch):
    from sofa.routes import voice

    async def quiet_recording(url, **kw):
        return wav([0] * 16000)

    heard = []
    real = app.state.svc.asr.transcribe

    async def spy(audio, language):
        heard.append(language)
        return await real(audio, language)

    monkeypatch.setattr(voice, "fetch_recording", quiet_recording)
    monkeypatch.setattr(voice, "to_16k_mono", lambda raw: raw)
    monkeypatch.setattr(app.state.svc.asr, "transcribe", spy)
    replies = call(["My name is Bola", "hello there"])
    assert "still there" in replies[1].lower() or "still there" in replies[2].lower()
    assert heard == []
