"""Numpy-only audio helpers: resampling to telephone rates and WAV encoding."""

import io
import wave

import numpy as np


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Windowed-sinc low-pass then linear interpolation. Good enough for 24 kHz -> 8 kHz speech
    that will be played over a phone line (which is band-limited to about 3.4 kHz anyway)."""
    if sr_in == sr_out:
        return x.astype(np.float32)
    if sr_out < sr_in:
        cutoff = 0.45 * sr_out / sr_in  # fraction of the input sample rate
        taps = 8 * (sr_in // sr_out) * 2 + 1
        n = np.arange(taps) - (taps - 1) / 2
        kernel = np.sinc(2 * cutoff * n) * np.hamming(taps)
        x = np.convolve(x, kernel / kernel.sum(), mode="same")
    n_out = int(round(len(x) * sr_out / sr_in))
    positions = np.linspace(0, len(x) - 1, n_out) if n_out > 1 else np.array([0.0])
    return np.interp(positions, np.arange(len(x)), x).astype(np.float32)


def to_wav_bytes(x: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)
