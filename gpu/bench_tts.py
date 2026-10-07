"""How fast can YarnGPT2 make speech on this card? Run from the repo root inside the TTS venv:  python -u -m gpu.bench_tts

Prints, for one typical reply: the time and tokens per second of the current method, then of each known speed-up. Nothing here changes the service.
"""

import sys
import time

import torch

from gpu.textnorm import chunk_text, normalize_for_tts
from gpu.tts_server import DEFAULT_SPEAKERS, YarnGPT2Engine

TEXTS = [
    "Okay. 2 bags of NPK 15-15-15 Fertilizer, that's 76,000 naira. Anything else?",
    "Urea Fertilizer is 34,000 naira per bag.",
    "For rice, put NPK in about two weeks after sowing or transplanting, and urea in two parts.",
]


def log(*args):
    print(*args, flush=True)


def prompt_ids(engine, text):
    chunk = chunk_text(normalize_for_tts(text, "en"), 200)[0]
    prompt = engine.tokenizer.create_prompt(chunk, lang="english", speaker_name=DEFAULT_SPEAKERS["en"])
    return engine.tokenizer.tokenize_prompt(prompt)


def timed(engine, label, text, **extra):
    ids = prompt_ids(engine, text)
    torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.inference_mode():
        out = engine.model.generate(input_ids=ids, temperature=0.1, repetition_penalty=1.1, max_length=4000, **extra)
    torch.cuda.synchronize()
    took = time.perf_counter() - started
    new = out.shape[1] - ids.shape[1]
    log(f"  {label}: {took:.2f}s for {new} new tokens = {new / took:.1f} tokens/s  (about {new / 75:.1f}s of speech)")
    return out, took


def attempt(label, fn):
    log(f"--- {label}")
    try:
        fn()
    except Exception as exc:  # a variant that does not work here is reported, not fatal
        log(f"  FAILED: {type(exc).__name__}: {str(exc)[:300]}")


def main():
    import transformers

    engine = YarnGPT2Engine()
    param = next(engine.model.parameters())
    log(f"torch {torch.__version__}, transformers {transformers.__version__}, gpu {torch.cuda.get_device_name(0)}")
    log(f"model dtype {param.dtype}, device {param.device}, layers {engine.model.config.num_hidden_layers}, hidden {engine.model.config.hidden_size}")

    def baseline():
        for i, text in enumerate(TEXTS * 2):
            timed(engine, f"run {i}", text)
        out, _ = timed(engine, "run for the audio steps", TEXTS[0])
        torch.cuda.synchronize()
        started = time.perf_counter()
        codes = engine.tokenizer.get_codes(out)
        audio = engine.tokenizer.get_audio(codes)
        torch.cuda.synchronize()
        log(f"  turning tokens into sound took {time.perf_counter() - started:.2f}s ({audio.shape[-1] / 24000:.1f}s of speech)")

    attempt("1. today's method (eager)", baseline)

    def half():
        if param.dtype == torch.float32:
            engine.model.to(torch.bfloat16)
            log("  converted the model to bfloat16")
        for i, text in enumerate(TEXTS):
            timed(engine, f"bf16 run {i}", text)

    attempt("2. half precision", half)

    def static():
        for i, text in enumerate(TEXTS * 2):
            timed(engine, f"static cache run {i}", text, cache_implementation="static")

    attempt("3. static cache (HF compiles the token step by itself where it can)", static)

    def compiled():
        engine.model.forward = torch.compile(engine.model.forward, mode="reduce-overhead", fullgraph=False)
        for i, text in enumerate(TEXTS * 2):
            timed(engine, f"compiled run {i}", text, cache_implementation="static")

    attempt("4. compile the token step by hand (the first runs include the compile)", compiled)
    log("BENCH_DONE")


if __name__ == "__main__":
    sys.exit(main())
