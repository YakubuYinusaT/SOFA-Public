"""Audio plumbing: recording download, 16 kHz mono resampling, TTS cache, call audio storage."""

import asyncio
import hashlib
import subprocess
from pathlib import Path
from urllib.parse import unquote

import httpx


def mock_recording_url(text: str, lang: str = "en") -> str:
    """Simulator helper: a 'recording' whose bytes are MOCK[lang]:text (see mocks/gpu_mock.py)."""
    from urllib.parse import quote

    return "mock:" + quote(f"MOCK[{lang}]:{text}", safe="")


def mock_mixed_url(readings: dict) -> str:
    """Simulator helper: a code-mixed 'recording' that each language model reads differently:
    {"en": ("Mo fe carton two ti Peak", -0.9), "yo": ("mo fe carton meji ti pik", -0.5)}"""
    import json
    from urllib.parse import quote

    return "mock:" + quote("MOCKX:" + json.dumps({k: list(v) for k, v in readings.items()}), safe="")


async def fetch_recording(url: str, attempts: int = 6, wait: float = 0.6, http: httpx.AsyncClient | None = None) -> bytes:
    """Africa's Talking recording URLs expire, so copy the audio to our own storage per turn.

    The address arrives the moment the recording ends, and on a real call the file was not yet on their server ("404 Not Found"). So a missing or
    busy file is asked for again a few times, a little later each time, before the turn is given up."""
    if url.startswith("mock:"):
        return unquote(url[len("mock:"):]).encode("utf-8")
    own = http is None
    http = http or httpx.AsyncClient(timeout=15, follow_redirects=True)
    try:
        for attempt in range(attempts):
            try:
                resp = await http.get(url)
                if resp.status_code not in (403, 404, 408, 425, 429) and resp.status_code < 500:
                    resp.raise_for_status()
                    return resp.content
                if attempt == attempts - 1:
                    resp.raise_for_status()
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError):
                if attempt == attempts - 1:
                    raise
            await asyncio.sleep(wait * (attempt + 1))
        raise RuntimeError("unreachable")
    finally:
        if own:
            await http.aclose()


def to_16k_mono(raw: bytes) -> bytes:
    """Resample phone audio (8 kHz) to the 16 kHz mono the N-ATLAS ASR models expect.
    Resampling cannot restore lost frequencies: measure accuracy on real calls (scripts/asr_gate.py)."""
    if raw.startswith((b"MOCK[", b"MOCKX:")):
        return raw
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"],
        input=raw, capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg resample failed: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


class AudioService:
    """Reply audio cached by hash of (language, text). Fixed phrases are pre-generated at deploy time."""

    def __init__(self, settings, tts):
        self.s, self.tts = settings, tts
        AudioService.voice_tag = getattr(tts, "tag", "")
        self.out_dir = Path(settings.storage_dir) / "audio_out"
        self.in_dir = Path(settings.storage_dir) / "audio_in"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.in_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    voice_tag = ""  # set when a hosted voice is chosen, so audio made by another voice is not reused

    @classmethod
    def key(cls, text: str, lang: str) -> str:
        salt = f"{cls.voice_tag}|" if cls.voice_tag else ""
        return hashlib.sha256(f"{salt}{lang}|{text}".encode()).hexdigest()[:24]

    def url_for_key(self, key: str) -> str:
        return f"{self.s.public_base_url.rstrip('/')}/audio/{key}.wav"

    async def speak(self, text: str, lang: str) -> tuple[str, str]:
        """Return (public url, storage path), synthesising only on a cache miss."""
        key = self.key(text, lang)
        path = self.out_dir / f"{key}.wav"
        if not path.exists():
            lock = self._locks.setdefault(key, asyncio.Lock())
            async with lock:
                if not path.exists():
                    path.write_bytes(await self.tts.synthesize(text, lang))
        return self.url_for_key(key), str(path)

    def save_call_audio(self, call_id, seq: int, raw: bytes) -> str:
        path = self.in_dir / f"{call_id}_{seq:03d}.wav"
        path.write_bytes(raw)
        return str(path)
