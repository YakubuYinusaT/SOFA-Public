"""Speak slower or faster without changing the pitch: a phase vocoder, numpy only.

The voice model has no speed control, so a finished clip is stretched instead. `rate` below 1 makes it longer (slower); above 1, shorter.
"""

import numpy as np

N_FFT = 1024
HOP = 256


def stretch(x: np.ndarray, rate: float) -> np.ndarray:
    """`x` is mono float audio at any sample rate. The result is about len(x) / rate samples long, at the same pitch."""
    if abs(rate - 1.0) < 0.01 or len(x) < N_FFT * 2:
        return x.astype(np.float32)
    x = x.astype(np.float64)
    window = np.hanning(N_FFT)
    padded = np.concatenate([np.zeros(N_FFT // 2), x, np.zeros(N_FFT // 2)])
    n_frames = 1 + (len(padded) - N_FFT) // HOP
    frames = np.stack([padded[i * HOP:i * HOP + N_FFT] * window for i in range(n_frames)])
    spec = np.fft.rfft(frames, axis=1)
    mag, phase = np.abs(spec), np.angle(spec)
    expected = 2 * np.pi * HOP * np.fft.rfftfreq(N_FFT)  # how far each frequency bin's phase moves in one hop
    steps = np.arange(0, n_frames - 1, rate)
    out_mag = np.empty((len(steps), spec.shape[1]))
    out_phase = np.empty_like(out_mag)
    running = phase[0].copy()
    for k, t in enumerate(steps):
        i = int(t)
        frac = t - i
        out_mag[k] = (1 - frac) * mag[i] + frac * mag[i + 1]
        out_phase[k] = running
        delta = phase[i + 1] - phase[i] - expected
        delta -= 2 * np.pi * np.round(delta / (2 * np.pi))
        running = running + expected + delta
    out_frames = np.fft.irfft(out_mag * np.exp(1j * out_phase), n=N_FFT, axis=1) * window
    length = HOP * (len(steps) - 1) + N_FFT
    y, norm = np.zeros(length), np.zeros(length)
    for k in range(len(steps)):
        y[k * HOP:k * HOP + N_FFT] += out_frames[k]
        norm[k * HOP:k * HOP + N_FFT] += window ** 2
    y = y / np.maximum(norm, 1e-8)
    return y[N_FFT // 2:length - N_FFT // 2].astype(np.float32)
