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
from typing import Literal, Protocol

import numpy as np
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .audioutil import insert_pauses, resample, silence, to_wav_bytes
from .common import require_key
from .timestretch import stretch
from .textnorm import normalize_for_tts, speech_plan

log = logging.getLogger("sofa.tts")

LANG_NAMES = {"en": "english", "yo": "yoruba", "ha": "hausa", "ig": "igbo"}
# One fixed voice per language for the whole pilot (spec). Override with SPEAKER_EN etc. English is jude: it was read back best by the speech recogniser (docs/model_log.md).
DEFAULT_SPEAKERS = {"en": "jude", "yo": "yoruba_female2", "ha": "hausa_female1", "ig": "igbo_female1"}
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
        self.static_cache = os.environ.get("TTS_STATIC_CACHE", "1") != "0"
        self.tokenizer = AudioTokenizerV2(
            "saheedniyi/YarnGPT2", os.environ["WAVTOKENIZER_CKPT"], os.environ["WAVTOKENIZER_CONFIG"]
        )
        self.model = (
            AutoModelForCausalLM.from_pretrained("saheedniyi/YarnGPT2", torch_dtype="auto")
            .to(self.tokenizer.device)
            .eval()
        )

    def generate(self, input_ids):
        """The speech tokens for a prompt. A fixed-size (static) cache lets PyTorch compile the one-token step, which about doubles the speed
        (35 to 68 tokens a second on an L4; speech needs 75 a second). The first call pays a compile of about a minute, so `warmup` makes it at
        startup. If the host cannot do it, the plain method is used from then on."""
        kwargs = dict(input_ids=input_ids, attention_mask=self.torch.ones_like(input_ids), temperature=0.1, repetition_penalty=1.1, max_length=4000,
                      pad_token_id=0)
        with self.torch.inference_mode():
            if self.static_cache:
                try:
                    return self.model.generate(cache_implementation="static", **kwargs)
                except Exception:
                    log.exception("the static cache did not work on this host: using the plain method from now on")
                    self.static_cache = False
            return self.model.generate(**kwargs)

    def synthesize(self, text: str, language: str, speaker: str) -> tuple[np.ndarray, int]:
        try:
            prompt = self.tokenizer.create_prompt(text, lang=LANG_NAMES[language], speaker_name=speaker)
        except Exception as exc:  # unknown speaker/language names surface as a 400, not a 500
            raise ValueError(f"cannot build prompt for {language}/{speaker}: {exc}") from exc
        input_ids = self.tokenizer.tokenize_prompt(prompt)
        output = self.generate(input_ids)
        codes = self.tokenizer.get_codes(output)
        audio = self.tokenizer.get_audio(codes)  # torch tensor [1, T] at 24 kHz
        return audio.squeeze().float().cpu().numpy(), MODEL_SAMPLE_RATE


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    language: str = "en"
    speaker: str | None = None
    sample_rate: int = 8000
    split: Literal["off", "sentence", "clause", "all"] | None = None  # where to pause (see textnorm.speech_plan); left out, TTS_SPLIT (default clause)
    speed: float | None = Field(default=None, ge=0.6, le=1.4)  # 0.9 = ten percent slower, same pitch; left out, TTS_SPEED (default 1.0) is used


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
        speed = req.speed or float(os.environ.get("TTS_SPEED", "1.0"))
        plan = speech_plan(normalize_for_tts(req.text, req.language), req.split or os.environ.get("TTS_SPLIT", "clause"), MAX_CHARS)
        if not plan:
            raise HTTPException(400, "empty text")

        started = time.perf_counter()
        parts: list[np.ndarray] = []
        try:
            async with lock:
                for chunk, pause, inside in plan:
                    audio, sr = await asyncio.to_thread(engine.synthesize, chunk, req.language, speaker)
                    audio = insert_pauses(audio, sr, inside)  # the commas and full stops inside a piece that was spoken in one go
                    parts.append(resample(stretch(audio, speed), sr, req.sample_rate))
                    parts.append(silence(pause, req.sample_rate))  # the pause after the piece: longer for a full stop than for a comma
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        wav = to_wav_bytes(np.concatenate(parts[:-1]), req.sample_rate)  # drop trailing pause
        seconds = sum(len(p) for p in parts) / req.sample_rate
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        log.info("tts %s/%s %d chunk(s) %.1fs audio in %d ms", req.language, speaker, len(plan), seconds, elapsed_ms)
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
