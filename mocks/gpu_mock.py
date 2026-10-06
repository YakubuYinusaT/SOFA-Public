"""Mock GPU host: same HTTP contracts as the real ASR / LLM / TTS services.

Run standalone:  uvicorn mocks.gpu_mock:app --port 9000
or in-process:   ASR_URL=inprocess://mock (default)

"Audio" for the mock ASR is UTF-8 text shaped like  MOCK[<lang>]:<what the caller said>.
The mock reports high confidence when <lang> matches the requested model and low
confidence otherwise, so language detection by highest confidence works in simulation.
"""

import io
import json
import re
import wave

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import Response

from .rules import parse, rerank, search_answer, understand, wording

app = FastAPI(title="SOFA mock GPU host")

MOCK_AUDIO = re.compile(rb"^MOCK\[(\w\w)\]:(.*)$", re.S)


@app.get("/health")
def health():
    return {"ok": True, "mock": True}


@app.post("/asr")
async def asr(audio: UploadFile = File(...), language: str = Form("en")):
    raw = await audio.read()
    if raw.startswith(b"MOCKX:"):  # per-model readings: {"en": ["text", confidence], "yo": [...]}, for code-mixing tests
        heard = json.loads(raw[len(b"MOCKX:"):].decode("utf-8")).get(language)
        text, confidence = (heard[0], heard[1]) if heard else ("", -3.0)
        return {"text": text, "confidence": confidence, "model": f"mock-asr-{language}", "language": language}
    m = MOCK_AUDIO.match(raw)
    if not m:
        return {"text": "", "confidence": -3.0, "model": f"mock-asr-{language}", "language": language}
    spoken_lang, text = m.group(1).decode(), m.group(2).decode("utf-8")
    confidence = -0.25 if spoken_lang == language else -1.6
    return {"text": text, "confidence": confidence, "model": f"mock-asr-{language}", "language": language}


@app.post("/tts")
async def tts(request: Request):
    body = await request.json()
    words = max(1, len(str(body.get("text", "")).split()))
    rate, seconds = 8000, min(12.0, 0.35 * words)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return Response(buf.getvalue(), media_type="audio/wav", headers={"X-Mock": "1"})


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^{name}:\s*(.+)$", text, re.M)
    return m.group(1).strip() if m else None


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    if system.startswith("RERANK"):
        content = rerank(system, user)
    elif system.startswith("UNDERSTAND"):
        content = json.dumps(understand(system, user))
    elif system.startswith("ANSWER"):
        content = json.dumps({"say": search_answer(system)})
    elif system.startswith("WORDING"):
        content = json.dumps(wording(system))
    else:
        result = parse(_field(user, "TRANSCRIPT") or user, stage=_field(system, "STAGE"), mode=_field(system, "MODE") or "customer",
                       pending=_field(system, "PENDING"))
        content = json.dumps(result)
    return {
        "id": "mock",
        "object": "chat.completion",
        "model": body.get("model", "mock"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
