"""REAL N-ATLAS ASR service for the GPU host. Same contract as mocks/gpu_mock.py:
POST /asr (multipart audio + language) -> {text, confidence, model, language}.

Written against the transformers Whisper API; it needs a GPU, an HF token with access to the NCAIR1 repos, and audio. Settings to check per model:
  * read each model card for forced language / task tokens and pass them to generate() if needed
  * confirm `confidence` (mean token log-prob) separates good from bad transcripts on real calls

    pip install torch transformers librosa fastapi uvicorn python-multipart
    ASR_LANGUAGES=en,yo GPU_API_KEY=... HF_TOKEN=... uvicorn gpu.asr_server:app --host 0.0.0.0 --port 9001

Use it for the Day-1 gate:  python -m scripts.asr_gate --asr-url http://HOST:9001 --lang yo --dir recordings/yo
"""

import io
import os

import librosa
import torch
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from transformers import WhisperForConditionalGeneration, WhisperProcessor

from .common import require_key

MODELS = {
    "en": "NCAIR1/NigerianAccentedEnglish",
    "yo": "NCAIR1/Yoruba-ASR",
    "ha": "NCAIR1/Hausa-ASR",
    "ig": "NCAIR1/Igbo-ASR",
}
# Only load the languages the backend has switched on (same ENABLED_LANGUAGES list): every model not loaded is
# memory and startup time saved. English is always loaded.
_ENABLED = ["en"] + [l.strip() for l in os.environ.get("ASR_LANGUAGES", "en,yo").split(",") if l.strip() in MODELS and l.strip() != "en"]
MODELS = {l: m for l, m in MODELS.items() if l in _ENABLED}
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32
TOKEN = os.environ.get("HF_TOKEN")

# Load all four models once at startup and route by session language.
processors = {l: WhisperProcessor.from_pretrained(m, token=TOKEN) for l, m in MODELS.items()}
models = {
    l: WhisperForConditionalGeneration.from_pretrained(m, torch_dtype=DTYPE, token=TOKEN).to(DEVICE).eval()
    for l, m in MODELS.items()
}

def _warmup() -> None:
    """One second of silence through each model: pays CUDA warmup at startup, not on a caller's turn."""
    import numpy as np

    silence = np.zeros(16000, dtype=np.float32)
    for lang, proc in processors.items():
        feats = proc(silence, sampling_rate=16000, return_tensors="pt").input_features.to(DEVICE, dtype=DTYPE)
        with torch.no_grad():
            models[lang].generate(feats, max_new_tokens=4)


_warmup()

app = FastAPI(title="SOFA N-ATLAS ASR", docs_url=None, redoc_url=None, openapi_url=None)  # public host: no /docs


@app.get("/health")
def health():
    return {"ok": True, "device": DEVICE, "models": MODELS}


@app.post("/asr", dependencies=[Depends(require_key)])
async def asr(audio: UploadFile = File(...), language: str = Form("en")):
    if language not in models:
        raise HTTPException(400, f"language {language!r} is not loaded here (ASR_LANGUAGES={sorted(models)})")
    wave, _ = librosa.load(io.BytesIO(await audio.read()), sr=16000, mono=True)
    proc, model = processors[language], models[language]
    feats = proc(wave, sampling_rate=16000, return_tensors="pt").input_features.to(DEVICE, dtype=DTYPE)
    with torch.no_grad():
        out = model.generate(feats, return_dict_in_generate=True, output_scores=True, max_new_tokens=200)
    text = proc.batch_decode(out.sequences, skip_special_tokens=True)[0].strip()
    scores = model.compute_transition_scores(out.sequences, out.scores, normalize_logits=True)
    confidence = float(scores.float().mean()) if scores.numel() else -5.0  # average log-probability
    return {"text": text, "confidence": confidence, "model": MODELS[language], "language": language}
