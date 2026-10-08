"""Does the voice say what it was asked to say? The voice speaks each phrase, the speech recogniser writes down what it heard, and the two are compared.

    TTS_URL=https://HOST:9002 ASR_URL=https://HOST:9001 GPU_API_KEY=... python -m scripts.voice_check [--speaker idera] [--speed 0.92] [--out voice_check_out] [--hosted]

A phrase the recogniser cannot read back is a phrase a caller will struggle to hear. Short phrases are the usual trouble: the voice model squeezes them.
Numbers are compared by their non-number words only, since the recogniser may write them as digits or as words.
"""

import argparse
import asyncio
import io
import re
import time
import wave
from pathlib import Path

import httpx
import numpy as np

from sofa.clients.asr import ASRClient
from sofa.clients.http import _tls_context
from sofa.config import get_settings

PHRASES = [
    "Where should we deliver?",
    "Anything else?",
    "Thank you.",
    "Done.",
    "Okay.",
    "Yes.",
    "How can I help you today?",
    "What would you like today?",
    "Are you still there?",
    "Hold on while I retrieve the information.",
    "Sorry, I didn't catch that. Could you say it again?",
    "Okay. Where should we deliver the order to?",
    "What else would you like?",
    "Okay. 2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira. Anything else?",
    "Done. I've sent the account number to this phone by SMS. Once you pay, we'll call you back to confirm. Thank you.",
    "Good evening, this is Sofa from CI Store. Who am I speaking with, and what would you like today?",
]
NUMBERISH = re.compile(r"^\d[\d,.\-]*$")


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", text.lower().replace("-", " "))]


def score(expected: str, heard: str) -> float:
    want = [w for w in expected.lower().replace("-", " ").split() if not NUMBERISH.match(w.strip(".,?!"))]
    want = [re.sub(r"[^a-z']", "", w) for w in want]
    want = [w for w in want if w]
    got = set(words(heard))
    return sum(1 for w in want if w in got) / len(want) if want else 1.0


def to_16k(wav: bytes) -> bytes:
    """The phone-rate clip the voice returns, as the 16 kHz mono the recogniser expects (no ffmpeg needed)."""
    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    n = int(round(len(x) * 16000 / rate))
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(y.tobytes())
    return buf.getvalue()


def seconds(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav)) as w:
        return w.getnframes() / w.getframerate()


def speak(base: str, key: str, text: str, speaker: str, speed: float | None, rate: int = 8000) -> bytes | None:
    body = {"text": text, "language": "en", "speaker": speaker, "sample_rate": rate}
    if speed:
        body["speed"] = speed
    for _ in range(8):
        try:
            with httpx.Client(timeout=httpx.Timeout(120, connect=15), verify=_tls_context()) as client:
                r = client.post(f"{base}/tts", json=body, headers={"Authorization": f"Bearer {key}"})
            return r.content if r.status_code == 200 else None
        except httpx.HTTPError:
            time.sleep(3)
    return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--speaker", default="idera")
    ap.add_argument("--speed", type=float, default=None)
    ap.add_argument("--out", default="voice_check_out")
    ap.add_argument("--rate", type=int, default=8000, choices=(8000, 16000, 24000), help="sample rate asked of the voice (8000 is phone quality)")
    ap.add_argument("--only", help="check only phrases containing this text")
    ap.add_argument("--phrase", action="append", help="check this wording instead of the built-in list (repeat for several)")
    ap.add_argument("--hosted", action="store_true", help="score the voice chosen by TTS_PROVIDER in .env (ElevenLabs, Azure or Spitch) instead of the GPU host's voice")
    args = ap.parse_args()
    s = get_settings()
    hosted = None
    if args.hosted:
        from sofa.clients.tts import build_tts
        hosted = build_tts(s)
    asr = ASRClient(s.asr_url, s.gpu_api_key)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    total, count = 0.0, 0
    for i, phrase in enumerate(args.phrase or PHRASES):
        if args.only and args.only.lower() not in phrase.lower():
            continue
        if hosted:
            try:
                wav = await hosted.synthesize(phrase, "en")
            except Exception as exc:
                print(f"?  {phrase!r}: the hosted voice failed ({type(exc).__name__})")
                continue
        else:
            wav = speak(s.tts_url, s.gpu_api_key, phrase, args.speaker, args.speed, args.rate)
        if wav is None:
            print(f"?  {phrase!r}: the voice did not answer")
            continue
        (out / f"{i:02d}.wav").write_bytes(wav)
        try:
            heard = (await asr.transcribe(to_16k(wav), "en")).text.strip()
        except Exception as exc:
            print(f"?  {phrase!r}: the recogniser failed ({type(exc).__name__})")
            continue
        sc = score(phrase, heard)
        total, count = total + sc, count + 1
        flag = "ok " if sc >= 0.85 else "BAD" if sc < 0.6 else "meh"
        print(f"{flag} {sc:4.0%}  {seconds(wav):4.1f}s  said {phrase!r}\n              heard {heard!r}")
    if count:
        print(f"\naverage {total / count:.0%} of the words came back, over {count} phrases (speaker {args.speaker}, speed {args.speed or 'server default'})")


if __name__ == "__main__":
    asyncio.run(main())
