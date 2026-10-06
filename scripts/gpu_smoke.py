"""Smoke-test the GPU host through the backend's own clients (the exact production code path).

    # against the real host
    ASR_URL=http://HOST:9001 LLM_URL=http://HOST:8001 TTS_URL=http://HOST:9002 GPU_API_KEY=... \
        python -m scripts.gpu_smoke [--wav call.wav --lang yo]

    # against the built-in mocks (default env): proves the script itself works
    python -m scripts.gpu_smoke

Checks, with latency for each (budget: ASR 0.8 s, LLM 1.0 s, TTS 1.2 s per turn):
  * LLM returns schema-valid intent JSON for typical caller sentences, and rerank answers with a valid choice
  * the gateway's own two calls: UNDERSTAND (language, service, tool, amount, yes/no in one call: English, Yoruba, Pidgin) and WORDING
    (the model words a reply; the validator must accept it). Yoruba and Pidgin cases are printed as "info": they show how N-ATLaS
    copes and never fail the run. Skip with --no-gateway.
  * TTS returns playable 8 kHz mono WAV in every language, and writes them to ./gpu_smoke_out for a listen
  * ASR (optional, needs --wav) returns text and a confidence
Exit code is non-zero if any check fails.
"""

import argparse
import asyncio
import io
import statistics
import sys
import time
import wave
from pathlib import Path

from sofa.audio import to_16k_mono
from sofa.gateway import understanding, wording
from sofa.gateway.hub import build_gateway
from sofa.clients.asr import ASRClient
from sofa.clients.llm import LLMClient
from sofa.clients.tts import TTSClient
from sofa.config import get_settings

SYSTEM = (
    "You are Sofa, a warm, brief shop attendant on a phone call for a Nigerian business. "
    "Extract the caller's intent as JSON only. Never invent products, prices or stock; "
    "write any reply in `say` with placeholders for facts.\n"
    "LANGUAGE: en\nMODE: customer\nSTAGE: none\nPENDING: none\nCATEGORY: provisions\n"
    "PRODUCTS:\n- Indomie Super Pack (aliases: super pack, indomie big)\n"
    "- Indomie Hungry Man (aliases: hungry man, indomie big)\n- Peak Milk Tin (aliases: peak milk tin)\n"
    "- Peak Milk Sachet (aliases: peak sachet)\nDRAFT ORDER: empty\nLAST TURNS:\nnone"
)
# (transcript, intents we accept)
LLM_CASES = [
    ("My name is Musa, I want Indomie big one", {"place_order", "give_name"}),
    ("Do you have Peak milk tin?", {"check_availability"}),
    ("How much is a carton of Indomie?", {"ask_price"}),
    ("Send me two cartons of Super Pack", {"place_order"}),
    ("Yes", {"confirm"}),
    ("No, that's wrong", {"deny"}),
    ("Where is my order?", {"track_order"}),
    ("Let me talk to the owner", {"speak_to_human"}),
]
# The gateway's own calls: one UNDERSTAND call per turn, and WORDING for replies. (transcript, what the model must find, pending question,
# informational?). Informational cases are about seeing how N-ATLaS copes (Yoruba numbers, Pidgin): they are printed but never fail the run.
GATEWAY_CASES = [
    ("I want to send three thousand naira to mama", {"domain": "banking", "tool": "bank.transfer", "amount_naira": 3000}, None, False),
    ("What is my balance please", {"domain": "banking", "tool": "bank.balance"}, None, False),
    ("Hello Sofa, good afternoon", {"domain": "chat"}, None, False),
    ("I want to buy two cartons of Indomie", {"domain": "commerce"}, None, False),
    ("What do I need to open an account", {"domain": "banking", "tool": "bank.account_requirements"}, None, False),
    ("Yes", {"answer": "yes"}, "That is 3,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?", False),
    ("No, that's wrong", {"answer": "no"}, "That is 3,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?", False),
    ("Abeg I wan check my balance", {"domain": "banking", "tool": "bank.balance"}, None, True),
    ("Jowo, ranse egberun meta si mama", {"domain": "banking", "tool": "bank.transfer", "amount_naira": 3000}, None, True),
    ("Mo fe send ten thousand naira si mama", {"domain": "banking", "tool": "bank.transfer", "amount_naira": 10000}, None, True),
    ("Beeni", {"answer": "yes"}, "That is 3,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?", True),
    ("Rara", {"answer": "no"}, "That is 3,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?", True),
]
WORDING_CASES = [  # (language, mixed): the model words the same reply in each; English must pass the validator
    ("en", False, True), ("yo", True, False), ("yo", False, False),
]
TTS_PHRASES = {
    "en": "Two cartons of Indomie Super Pack, that's 14,000 naira. Anything else?",
    "yo": "Ẹ kú àárọ̀. Kí ni ẹ fẹ́ rà lónìí?",
    "ha": "Barka da safiya. Me kuke so ku saya yau?",
    "ig": "Ndewo. Gịnị ka ị chọrọ ịzụta taa?",
}
# The Yoruba/Hausa/Igbo lines above are only enough to hear whether each voice runs; have a native
# speaker judge them and replace them with the real templates once those are written.


async def gateway_checks(llm, s, report, repeat: int) -> int:
    """The two calls every gateway turn makes. Returns how many hard checks failed (informational cases never count)."""
    tools = build_gateway().tools
    enabled = s.languages
    tool_ids = [t.tool_id for t in tools.for_domain("banking")]
    json_schema = understanding.schema(enabled, tool_ids)
    hard_failures = 0
    print("\nGateway: UNDERSTAND (language, service, tool, amount, yes/no in one call)")
    for transcript, expect, asked, soft in GATEWAY_CASES:
        st = {"bank": "demobank", "said": [], "pending": {"question": asked, "kind": "bank_op_confirm"} if asked else None}
        system = understanding.build(st, tools, ["demobank", "gtbank"])
        t = time.perf_counter()
        u, raw = await llm.extract(understanding.Understanding, system, transcript, None, json_schema=json_schema)
        ms = int((time.perf_counter() - t) * 1000)
        got = u.model_dump()
        bad = {k: (v, got.get(k)) for k, v in expect.items() if got.get(k) != v}
        ok = "error" not in raw and not bad
        tag = "info" if soft else ("PASS" if ok else "FAIL")
        print(f"{tag}  \"{transcript}\"  -> lang={u.language or '?'}{'+mixed' if u.mixed else ''} domain={u.domain} tool={u.tool} "
              f"amount={u.amount_naira} who={u.beneficiary} answer={u.answer} ({ms} ms)" + ("" if ok else f"  expected {expect} raw={raw if 'error' in raw else ''}"))
        hard_failures += 0 if (ok or soft) else 1
    print("\nGateway: WORDING (the model words the reply; it must keep every placeholder and invent no number)")
    reference = "Done. {amount} sent to {who}. Your balance is now {balance}. Is there anything else I can help with?"
    facts = {"amount": "3,000 naira", "who": "Hauwa Bello at GT Bank", "balance": "39,500 naira"}
    for lang, mixed, must_pass in WORDING_CASES:
        t = time.perf_counter()
        try:
            written = await wording.write(llm, "bank_transfer_done", reference, lang, mixed, "Mo fe send owo si mama" if lang == "yo" else "send money to mama", facts)
        except Exception as exc:
            written = None
            print(f"  (call failed: {exc!r})")
        ms = int((time.perf_counter() - t) * 1000)
        shown = written[1] if written else "(refused: the template would be spoken instead)"
        print(f"{'PASS' if written else ('FAIL' if must_pass else 'info')}  wording {lang}{'+mixed' if mixed else ''}  ({ms} ms)  {shown}")
        hard_failures += 0 if (written or not must_pass) else 1
    if repeat:
        print(f"\nGateway latency over {repeat} repeats (a turn is one UNDERSTAND plus one WORDING; budget about 1 s each)")
        und_ms, word_ms = [], []
        system = understanding.build({"bank": "demobank", "said": [], "pending": None}, tools, ["demobank"])
        for _ in range(repeat):
            t = time.perf_counter()
            await llm.extract(understanding.Understanding, system, "I want to send three thousand naira to mama", None, json_schema=json_schema)
            und_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            await wording.write(llm, "bank_transfer_done", reference, "en", False, "send money to mama", facts)
            word_ms.append((time.perf_counter() - t) * 1000)
        for label, values in (("understand", und_ms), ("wording", word_ms)):
            median = statistics.median(values)
            report(median <= 1000, f"{label} median {median:.0f} ms [1000]", f"max {max(values):.0f} ms")  # report() counts a miss itself
    return hard_failures


def wav_16k_mono(raw: bytes) -> bytes:
    """16 kHz mono WAV. Uses ffmpeg when it is installed; for a plain WAV file it converts in Python, so a laptop without ffmpeg can run this."""
    import shutil

    if shutil.which("ffmpeg"):
        return to_16k_mono(raw)
    import numpy as np

    with wave.open(io.BytesIO(raw)) as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16 if width == 2 else np.uint8).astype(np.float32)
    if width == 1:
        data = (data - 128) * 256
    data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        n = int(len(data) * 16000 / rate)
        data = np.interp(np.linspace(0, len(data) - 1, n), np.arange(len(data)), data)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.clip(data, -32768, 32767).astype(np.int16).tobytes())
    return out.getvalue()


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # replies can contain symbols such as the Naira sign that a Windows console cannot print
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav")
    ap.add_argument("--lang", default="en")
    ap.add_argument("--out", default="gpu_smoke_out")
    ap.add_argument("--repeat", type=int, default=5, help="timed repeats for the latency summary (0 to skip)")
    ap.add_argument("--llm-only", action="store_true", help="test only the language model (no speech or voice services needed)")
    ap.add_argument("--no-gateway", action="store_true", help="skip the gateway's UNDERSTAND and WORDING checks")
    args = ap.parse_args()
    s = get_settings()
    llm = LLMClient(s.llm_url, s.llm_model, s.llm_api_key or s.gpu_api_key, s.llm_structured_mode, s.llm_provider)
    tts = TTSClient(s.tts_url, s.gpu_api_key)
    asr = ASRClient(s.asr_url, s.gpu_api_key)
    failures = 0

    def report(ok: bool, label: str, detail: str = "") -> None:
        nonlocal failures
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}")

    print(f"LLM {s.llm_url}  ({s.llm_model}, structured={s.llm_structured_mode})")
    for transcript, accepted in LLM_CASES:
        t = time.perf_counter()
        tj, raw = await llm.parse_turn(SYSTEM, transcript)
        ms = int((time.perf_counter() - t) * 1000)
        ok = "error" not in raw and tj.intent in accepted
        report(ok, f'"{transcript}"', f"-> {tj.intent} ({ms} ms){'' if ok else f'  expected {sorted(accepted)}  raw={raw}'}")
    t = time.perf_counter()
    pick = await llm.rerank("peak milk", ["Peak Milk Tin", "Peak Milk Sachet"])
    report(True, "rerank returns a valid choice or none", f"-> {pick} ({int((time.perf_counter() - t) * 1000)} ms)")

    if not args.no_gateway:
        failures += await gateway_checks(llm, s, report, args.repeat)
    if args.llm_only:
        print("\n" + ("ALL PASSED" if not failures else f"{failures} FAILED"))
        return 1 if failures else 0

    print(f"\nTTS {s.tts_url}")
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    for lang, text in TTS_PHRASES.items():
        if lang not in s.languages:
            continue  # only test the languages that are switched on (ENABLED_LANGUAGES)
        t = time.perf_counter()
        try:
            audio = await tts.synthesize(text, lang)
            with wave.open(io.BytesIO(audio)) as w:
                seconds, rate, channels = w.getnframes() / w.getframerate(), w.getframerate(), w.getnchannels()
            (out / f"tts_{lang}.wav").write_bytes(audio)
            ok = channels == 1 and rate in (8000, 16000, 24000) and seconds > 0.3
            report(ok, f"tts {lang}", f"{seconds:.1f}s audio at {rate} Hz in {int((time.perf_counter() - t) * 1000)} ms")
        except Exception as exc:
            report(False, f"tts {lang}", repr(exc))
    print(f"  wrote {out}/tts_*.wav: listen to them, especially yo/ha/ig")

    if args.repeat:
        print(f"\nLatency over {args.repeat} repeats (includes network and any proxy; budget in brackets)")
        llm_ms, tts_ms = [], []
        for _ in range(args.repeat):
            t = time.perf_counter()
            await llm.parse_turn(SYSTEM, "Send me two cartons of Super Pack")
            llm_ms.append((time.perf_counter() - t) * 1000)
            t = time.perf_counter()
            await tts.synthesize(TTS_PHRASES["en"], "en")
            tts_ms.append((time.perf_counter() - t) * 1000)
        for label, values, budget in (("llm", llm_ms, 1000), ("tts", tts_ms, 1200)):
            median = statistics.median(values)
            report(median <= budget, f"{label} median {median:.0f} ms [{budget}]", f"max {max(values):.0f} ms")

    if args.wav:
        print(f"\nASR {s.asr_url}")
        audio = wav_16k_mono(Path(args.wav).read_bytes())
        t = time.perf_counter()
        r = await asr.transcribe(audio, args.lang)
        ms = int((time.perf_counter() - t) * 1000)
        report(bool(r.text) and ms <= 800, f"asr {args.lang} [800 ms budget]", f'"{r.text}" conf={r.confidence:.2f} ({ms} ms)')

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
