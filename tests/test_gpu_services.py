"""GPU-host code that can be tested without a GPU: TTS request handling, text/audio helpers,
and the exact request bodies the backend sends to vLLM."""

import io
import json
import wave

import httpx
import numpy as np
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gpu.audioutil import resample, to_wav_bytes
from gpu.textnorm import chunk_text, normalize_for_tts, spell_number
from gpu.tts_server import create_app
from sofa.clients.llm import LLMClient


class FakeEngine:
    name = "fake"

    def __init__(self):
        self.calls = []

    def synthesize(self, text, language, speaker):
        self.calls.append((text, language, speaker))
        if speaker == "no_such_voice":
            raise ValueError("unknown speaker")
        t = np.linspace(0, 0.5, 12000, endpoint=False)  # 0.5 s at 24 kHz
        return (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), 24000


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def tts(engine):
    return TestClient(create_app(engine))


def read_wav(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getframerate(), w.getnframes() / w.getframerate()


def test_tts_returns_8khz_mono_wav(tts, engine):
    r = tts.post("/tts", json={"text": "Two cartons of Indomie Super Pack, that's 14,000 naira. Anything else?", "language": "en"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    channels, rate, seconds = read_wav(r.content)
    assert (channels, rate) == (1, 8000) and seconds > 0.4
    assert "fourteen thousand naira" in engine.calls[0][0]  # digits are spelled out before synthesis
    assert engine.calls[0][1:] == ("en", "idera")  # fixed default voice


def test_tts_speaker_language_and_sample_rate(tts, engine, monkeypatch):
    monkeypatch.setenv("SPEAKER_YO", "yoruba_male2")
    assert tts.post("/tts", json={"text": "Bawo ni", "language": "yo"}).status_code == 200
    assert engine.calls[-1][2] == "yoruba_male2"
    r = tts.post("/tts", json={"text": "Hello there", "language": "en", "sample_rate": 16000})
    assert read_wav(r.content)[1] == 16000


def test_tts_long_reply_is_chunked_into_one_clip(tts, engine):
    text = " ".join(f"This is sentence number {i} in a long reply." for i in range(12))
    r = tts.post("/tts", json={"text": text, "language": "en"})
    assert r.status_code == 200 and len(engine.calls) > 1
    assert all(len(c[0]) <= 200 for c in engine.calls)
    assert read_wav(r.content)[2] > 0.5 * len(engine.calls)  # clips joined, not just the first


@pytest.mark.parametrize("body,code", [
    ({"text": "hi", "language": "fr"}, 400),
    ({"text": "hi", "language": "en", "sample_rate": 44100}, 400),
    ({"text": "", "language": "en"}, 422),
    ({"text": "hi", "language": "en", "speaker": "no_such_voice"}, 400),
])
def test_tts_rejects_bad_requests(tts, body, code):
    assert tts.post("/tts", json=body).status_code == code


def test_tts_requires_api_key_when_configured(tts, monkeypatch):
    monkeypatch.setenv("GPU_API_KEY", "s3cret")
    body = {"text": "hi", "language": "en"}
    assert tts.post("/tts", json=body).status_code == 401
    assert tts.post("/tts", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert tts.post("/tts", json=body, headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_spell_number_and_money():
    assert spell_number(14000) == "fourteen thousand"
    assert spell_number(1640000) == "one million six hundred and forty thousand"
    assert spell_number(350) == "three hundred and fifty"
    assert spell_number(2400) == "two thousand four hundred"
    assert normalize_for_tts("16,400 naira in total", "en") == "sixteen thousand four hundred naira in total"
    assert normalize_for_tts("1,234.50 naira", "en") == "one thousand two hundred and thirty four naira fifty kobo"
    assert normalize_for_tts("Add 20 cartons", "en") == "Add twenty cartons"
    assert normalize_for_tts("Iye 350", "yo") == "Iye 350"  # local-language numbers: open decision, untouched


def test_chunk_text_never_splits_words_and_respects_limit():
    long_sentence = "word " * 100
    chunks = chunk_text(long_sentence, 50)
    assert all(len(c) <= 50 for c in chunks) and " ".join(chunks).split() == long_sentence.split()
    assert chunk_text("One. Two. Three.", 200) == ["One. Two. Three."]
    assert chunk_text("   ") == []


def test_resample_24k_to_8k_keeps_a_speech_tone_and_length():
    t = np.linspace(0, 1, 24000, endpoint=False)
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = resample(tone, 24000, 8000)
    assert len(out) == 8000
    assert 0.6 < np.abs(out[500:-500]).max() < 1.05  # 1 kHz passes the anti-alias filter
    high = resample(np.sin(2 * np.pi * 9000 * t).astype(np.float32), 24000, 8000)
    assert np.abs(high[500:-500]).max() < 0.2  # 9 kHz would alias into the band: filtered out
    assert read_wav(to_wav_bytes(out, 8000))[1] == 8000


# ---- the request bodies the backend sends to vLLM -----------------------------------------------


def fake_vllm(captured: list, content: str):
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        captured.append(await request.json())
        captured_headers.append(dict(request.headers))
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    return app


captured_headers: list = []


def llm_with(app, mode):
    client = LLMClient("http://vllm", "NCAIR1/N-ATLaS", api_key="k123", structured_mode=mode)
    client.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers={"Authorization": "Bearer k123"})
    client.base = "http://vllm"
    return client


@pytest.mark.anyio
async def test_modern_vllm_request_shape():
    sent: list = []
    llm = llm_with(fake_vllm(sent, json.dumps({"intent": "confirm"})), "modern")
    tj, _ = await llm.parse_turn("system", "yes")
    assert tj.intent == "confirm"
    body = sent[0]
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"]["properties"]["intent"]
    assert "guided_json" not in body and body["temperature"] == 0.1 and body["max_tokens"] == 200
    assert body["model"] == "NCAIR1/N-ATLaS"
    assert captured_headers[-1]["authorization"] == "Bearer k123"

    sent.clear()
    llm.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_vllm(sent, "2")))
    assert await llm.rerank("peak milk", ["Tin", "Sachet"]) == 2
    assert sent[0]["structured_outputs"] == {"choice": ["1", "2", "none"]} and "guided_choice" not in sent[0]


@pytest.mark.anyio
async def test_legacy_vllm_request_shape():
    sent: list = []
    llm = llm_with(fake_vllm(sent, json.dumps({"intent": "deny"})), "legacy")
    await llm.parse_turn("system", "no")
    assert "guided_json" in sent[0] and "response_format" not in sent[0]
    llm.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_vllm(sent, "none")))
    assert await llm.rerank("x", ["A", "B"]) is None
    assert sent[-1]["guided_choice"] == ["1", "2", "none"]


@pytest.mark.anyio
async def test_unparseable_model_output_degrades_to_unknown():
    llm = llm_with(fake_vllm([], "not json at all"), "modern")
    tj, raw = await llm.parse_turn("system", "hello")
    assert tj.intent == "unknown" and "error" in raw


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_tts_warmup_runs_once_and_never_blocks_startup():
    from gpu.tts_server import warmup

    engine = FakeEngine()
    warmup(engine)
    assert engine.calls == [("Hello.", "en", "idera")]

    class Broken(FakeEngine):
        def synthesize(self, *a):
            raise RuntimeError("cuda oom")

    warmup(Broken())  # logged, not raised: the service still starts


def test_gpu_services_hide_api_docs(tts):
    assert tts.get("/docs").status_code == 404 and tts.get("/openapi.json").status_code == 404
