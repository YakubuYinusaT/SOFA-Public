import io
import wave

import httpx
import pytest

from sofa.audio import AudioService
from sofa.clients.tts import AzureTTS, ElevenLabsTTS, RoutedTTS, SpitchTTS, TTSClient, build_tts
from sofa.config import Settings


def _clip(name: str, frames: int = 2400) -> bytes:
    """A tiny WAV; its loudness differs by name so tests can tell which voice made it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes([len(name) % 250 + 1, 0]) * frames)
    return buf.getvalue()


def _transport(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"\x01\x00" * 1600)
    return httpx.MockTransport(handler)


@pytest.mark.anyio
async def test_elevenlabs_returns_a_wav_and_sends_the_key():
    seen = []
    tts = ElevenLabsTTS("k1", "voiceX", "m1", 0.9)
    tts.http = httpx.AsyncClient(base_url="https://api.elevenlabs.io", transport=_transport(seen), headers={"xi-api-key": "k1"})
    wav = await tts.synthesize("Hello there", "en")
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == 16000 and w.getnframes() == 1600
    req = seen[0]
    assert req.url.path == "/v1/text-to-speech/voiceX" and req.headers["xi-api-key"] == "k1"
    assert b'"speed":0.9' in req.content.replace(b" ", b"")


@pytest.mark.anyio
async def test_azure_sends_escaped_ssml():
    seen = []
    tts = AzureTTS("k2", "westeurope", "en-NG-EzinneNeural", 0.95)
    tts.http = httpx.AsyncClient(base_url="https://x", transport=_transport(seen),
                                 headers={"Ocp-Apim-Subscription-Key": "k2"})
    out = await tts.synthesize("Tom & Jerry <ok>", "en")
    assert out.startswith(b"\x01")
    body = seen[0].content.decode()
    assert "Tom &amp; Jerry &lt;ok&gt;" in body and "en-NG-EzinneNeural" in body and "-5%" in body


@pytest.mark.anyio
async def test_routing_keeps_other_languages_on_the_gpu_voice():
    class Fake:
        def __init__(self, name): self.name, self.tag = name, name
        async def synthesize(self, text, language): return _clip(self.name)
    r = RoutedTTS(Fake("hosted"), Fake("gpu"), {"en"})
    assert await r.synthesize("hi", "en") != _clip("gpu")
    assert await r.synthesize("bawo", "yo") == _clip("gpu")


def test_build_tts_picks_the_provider_and_checks_keys():
    assert isinstance(build_tts(Settings(_env_file=None)), TTSClient)
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="elevenlabs"))
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="nonsense"))
    t = build_tts(Settings(_env_file=None, tts_provider="azure", azure_speech_key="k", azure_speech_region="r"))
    assert isinstance(t, RoutedTTS) and t.languages == {"en"}


def test_the_audio_cache_key_changes_with_the_voice(tmp_path):
    base = AudioService.key("Hello", "en")
    try:
        AudioService(Settings(_env_file=None, storage_dir=str(tmp_path)), type("T", (), {"tag": "azure-x"})())
        assert AudioService.key("Hello", "en") != base
    finally:
        AudioService.voice_tag = ""
    assert AudioService.key("Hello", "en") == base


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_spitch_sends_the_voice_for_the_language():
    seen = []
    tts = SpitchTTS("k3", {"en": "jude", "yo": "sade"}, 1.5)
    tts.http = httpx.AsyncClient(base_url="https://api.spitch.app", transport=_transport(seen),
                                 headers={"Authorization": "Bearer k3"})
    await tts.synthesize("Bawo ni", "yo")
    req = seen[0]
    assert req.url.path == "/v1/speech" and req.headers["authorization"] == "Bearer k3"
    body = req.content.replace(b" ", b"")
    assert b'"voice":"sade"' in body and b'"language":"yo"' in body and b'"speed":1.2' in body and b'"format":"wav"' in body


def test_spitch_covers_its_languages_by_default():
    with pytest.raises(ValueError):
        build_tts(Settings(_env_file=None, tts_provider="spitch"))
    t = build_tts(Settings(_env_file=None, tts_provider="spitch", spitch_api_key="k"))
    assert t.languages == {"en", "yo", "ha", "ig"}
    assert build_tts(Settings(_env_file=None, tts_provider="spitch", spitch_api_key="k", tts_provider_languages="en")).languages == {"en"}


def test_a_streamed_wav_with_unknown_length_and_an_extra_chunk_is_repaired():
    import struct
    from sofa.clients.tts import _repair_wav
    pcm = b"\x01\x00" * 2400
    raw = (b"RIFF" + b"\xff\xff\xff\xff" + b"WAVE" + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
           + b"spch" + struct.pack("<I", 4) + b"abcd" + b"data" + b"\xff\xff\xff\xff" + pcm)
    with wave.open(io.BytesIO(_repair_wav(raw))) as w:
        assert w.getframerate() == 24000 and w.getnframes() == 2400


@pytest.mark.anyio
async def test_a_failing_hosted_voice_falls_back_to_the_gpu_voice():
    class Broken:
        tag = "x"
        async def synthesize(self, text, language): raise httpx.HTTPStatusError("402", request=None, response=None)
    class Gpu:
        async def synthesize(self, text, language): return b"gpu"
    assert await RoutedTTS(Broken(), Gpu(), {"en"}).synthesize("hi", "en") == b"gpu"


def test_every_sentence_is_its_own_piece():
    from sofa.clients.tts import split_sentences
    assert split_sentences("Okay. 2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira. Anything else?") == [
        "Okay.", "2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira.", "Anything else?"]
    assert split_sentences("Done. I've sent the account number to this phone by SMS. Once you pay, we'll call you back to confirm. Thank you, Bola.") == [
        "Done.", "I've sent the account number to this phone by SMS.", "Once you pay, we'll call you back to confirm.", "Thank you, Bola."]
    assert split_sentences("Where should we deliver?") == ["Where should we deliver?"]
    assert split_sentences("Urea is 34,000 naira per bag.") == ["Urea is 34,000 naira per bag."]


@pytest.mark.anyio
async def test_each_piece_is_made_separately_and_joined_with_a_pause():
    calls = []

    class Hosted:
        tag = "h"
        async def synthesize(self, text, language):
            calls.append(text)
            return _wav_of(2400)

    def _wav_of(n):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x01\x00" * n)
        return buf.getvalue()

    out = await RoutedTTS(Hosted(), None, {"en"}).synthesize("Your total is 5,000 naira. Anything else?", "en")
    assert calls == ["Your total is 5,000 naira.", "Anything else?"]
    with wave.open(io.BytesIO(out)) as w:
        assert w.getnframes() == 2400 * 2 + int(24000 * 0.3) + int(24000 * 0.25)  # one gap, and the lead-in


@pytest.mark.anyio
async def test_spitch_never_has_more_than_three_calls_running_and_retries_a_429_once():
    import asyncio
    running, peak, answers = 0, 0, {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        answers["n"] += 1
        return httpx.Response(429 if answers["n"] == 1 else 200, content=b"\x00" * 100)

    tts = SpitchTTS("k", {"en": "jude"})
    tts.http = httpx.AsyncClient(base_url="https://api.spitch.app", transport=httpx.MockTransport(handler))
    results = await asyncio.gather(*(tts.synthesize(f"Sentence {i}.", "en") for i in range(6)))
    assert len(results) == 6 and peak <= 3
