"""YarnGPT2 text-to-speech service for the GPU host.

    POST /tts  {"text": "...", "language": "en|yo|ha|ig", "speaker": optional, "sample_rate": 8000}
    -> audio/wav (mono, 16-bit, 8 kHz by default: what Africa's Talking plays over the phone)

Same contract as mocks/gpu_mock.py. Run (see gpu/README.md for the full setup):

    GPU_API_KEY=... PYTHONPATH=/opt/yarn uvicorn gpu.tts_server:app_factory --factory --port 9002

The request handling, chunking, number spelling and resampling are covered by tests/test_gpu_services.py (with a fake engine);
YarnGPT2Engine below follows the model card. Speech output uses YarnGPT2; speech recognition and understanding use N-ATLaS.
"""

import asyncio
import logging
import os
import time
from typing import Protocol

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .audioutil import resample, silence, to_wav_bytes
from .common import require_key
from .textnorm import chunk_text, normalize_for_tts

log = logging.getLogger("sofa.tts")

LANG_NAMES = {"en": "english", "yo": "yoruba", "ha": "hausa", "ig": "igbo"}
# One fixed voice per language for the whole pilot (spec). Override with SPEAKER_EN etc.
DEFAULT_SPEAKERS = {"en": "idera", "yo": "yoruba_female2", "ha": "hausa_female1", "ig": "igbo_female1"}
MODEL_SAMPLE_RATE = 24000
MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "200"))
MAX_TEXT = 600


class Engine(Protocol):
    name: str

    def synthesize(self, text: str, language: str, speaker: str) -> tuple[np.ndarray, int]:
        """One short chunk in, mono float32 audio and its sample rate out. May raise ValueError."""


class YarnGPT2Engine:
    """saheedniyi/YarnGPT2 (SmolLM2-360M base) + WavTokenizer, as in the model card.

    Needs, on the host:
      * the yarngpt repo cloned so that `yarngpt.audiotokenizer` imports (PYTHONPATH = its parent dir)
      * WAVTOKENIZER_CKPT and WAVTOKENIZER_CONFIG pointing at the two WavTokenizer files
    """

    name = "saheedniyi/YarnGPT2"

    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForCausalLM

        try:
            from yarngpt.audiotokenizer import AudioTokenizerV2
        except ImportError:
            from audiotokenizer import AudioTokenizerV2  # repo dir itself on PYTHONPATH

        self.torch = torch
        self.tokenizer = AudioTokenizerV2(
            "saheedniyi/YarnGPT2", os.environ["WAVTOKENIZER_CKPT"], os.environ["WAVTOKENIZER_CONFIG"]
        )
        self.model = (
            AutoModelForCausalLM.from_pretrained("saheedniyi/YarnGPT2", torch_dtype="auto")
            .to(self.tokenizer.device)
            .eval()
        )

    def synthesize(self, text: str, language: str, speaker: str) -> tuple[np.ndarray, int]:
        try:
            prompt = self.tokenizer.create_prompt(text, lang=LANG_NAMES[language], speaker_name=speaker)
        except Exception as exc:  # unknown speaker/language names surface as a 400, not a 500
            raise ValueError(f"cannot build prompt for {language}/{speaker}: {exc}") from exc
        input_ids = self.tokenizer.tokenize_prompt(prompt)
        with self.torch.inference_mode():
            output = self.model.generate(
                input_ids=input_ids, temperature=0.1, repetition_penalty=1.1, max_length=4000
            )
        codes = self.tokenizer.get_codes(output)
        audio = self.tokenizer.get_audio(codes)  # torch tensor [1, T] at 24 kHz
        return audio.squeeze().float().cpu().numpy(), MODEL_SAMPLE_RATE


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    language: str = "en"
    speaker: str | None = None
    sample_rate: int = 8000


def speaker_for(language: str, requested: str | None) -> str:
    return requested or os.environ.get(f"SPEAKER_{language.upper()}") or DEFAULT_SPEAKERS[language]


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="SOFA TTS (YarnGPT2)", docs_url=None, redoc_url=None, openapi_url=None)  # public host: no /docs
    lock = asyncio.Lock()  # one model, one generation at a time; latency comes from the audio cache upstream

    @app.get("/health")
    def health():
        return {"ok": True, "engine": engine.name, "speakers": {l: speaker_for(l, None) for l in LANG_NAMES}}

    @app.post("/tts", dependencies=[Depends(require_key)])
    async def tts(req: TTSRequest):
        if req.language not in LANG_NAMES:
            raise HTTPException(400, f"language must be one of {sorted(LANG_NAMES)}")
        if req.sample_rate not in (8000, 16000, 24000):
            raise HTTPException(400, "sample_rate must be 8000, 16000 or 24000")
        speaker = speaker_for(req.language, req.speaker)
        chunks = chunk_text(normalize_for_tts(req.text, req.language), MAX_CHARS)
        if not chunks:
            raise HTTPException(400, "empty text")

        started = time.perf_counter()
        parts: list[np.ndarray] = []
        try:
            async with lock:
                for chunk in chunks:
                    audio, sr = await asyncio.to_thread(engine.synthesize, chunk, req.language, speaker)
                    parts.append(resample(audio, sr, req.sample_rate))
                    parts.append(silence(0.15, req.sample_rate))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        wav = to_wav_bytes(np.concatenate(parts[:-1]), req.sample_rate)  # drop trailing pause
        seconds = sum(len(p) for p in parts) / req.sample_rate
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("tts %s/%s %d chunk(s) %.1fs audio in %d ms", req.language, speaker, len(chunks), seconds, elapsed_ms)
        return Response(wav, media_type="audio/wav",
                        headers={"X-Audio-Seconds": f"{seconds:.2f}", "X-Synth-Ms": str(elapsed_ms)})

    return app


def warmup(engine: Engine) -> None:
    """First generation is slow (CUDA kernels, caches). Pay it at startup, not on a caller's turn."""
    try:
        started = time.perf_counter()
        engine.synthesize("Hello.", "en", speaker_for("en", None))
        log.info("tts warmup done in %.1fs", time.perf_counter() - started)
    except Exception:
        log.exception("tts warmup failed (service will still start)")


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    engine = YarnGPT2Engine()
    warmup(engine)
    return create_app(engine)
