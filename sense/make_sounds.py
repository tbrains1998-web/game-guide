"""Make the three Sense sounds as WAV files in sense\\sounds\\.

Run once:  python sense\\make_sounds.py
Writes chime.wav, knock.wav and harp.wav (44.1 kHz, 16-bit, mono,
0.35-0.6 s each, peak -6 dBFS, soft attack and release so there are no clicks).
"""
import wave
from pathlib import Path

import numpy as np

SR = 44100
PEAK = 10 ** (-6 / 20)          # -6 dBFS
OUT = Path(__file__).resolve().parent / "sounds"


def t_axis(seconds):
    return np.arange(int(SR * seconds)) / SR


def envelope(n, attack_s=0.01, release_s=0.05):
    """Flat envelope with a soft ramp in and out (removes clicks)."""
    env = np.ones(n)
    a = min(int(SR * attack_s), n // 2)
    r = min(int(SR * release_s), n // 2)
    if a:
        env[:a] = np.linspace(0.0, 1.0, a)
    if r:
        env[-r:] = np.linspace(1.0, 0.0, r)
    return env


def finish(x, attack_s, release_s):
    """Remove DC, then ramp the edges (so they end at exactly 0, no click),
    then scale so the loudest sample sits at -6 dBFS."""
    x = x - x.mean()
    x = x * envelope(len(x), attack_s, release_s)
    m = np.max(np.abs(x))
    if m > 0:
        x = x / m * PEAK
    return x


def write_wav(name, samples):
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    pcm = np.clip(np.round(samples * 32767), -32767, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"wrote {path}  {len(pcm) / SR:.2f} s  peak {20 * np.log10(np.max(np.abs(samples))):.1f} dBFS")


def chime():
    """Two soft sine notes, E5 then G5, each with a little 2nd harmonic."""
    def note(freq, seconds):
        t = t_axis(seconds)
        tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t)
        decay = np.exp(-t * 6.0)              # bell-like fade
        return tone * decay * envelope(len(t), 0.008, 0.03)

    n1 = note(659.25, 0.28)                   # E5
    n2 = note(783.99, 0.32)                   # G5
    gap = int(SR * 0.02)
    total = len(n1) + gap + len(n2)
    out = np.zeros(total)
    out[:len(n1)] += n1
    out[len(n1) + gap:] += n2
    # 0.28 + 0.02 + 0.32 = 0.62 s; trim to stay inside the 0.35-0.6 s window
    out = out[:int(SR * 0.58)]
    return finish(out, 0.005, 0.03)


def knock():
    """Short filtered noise burst plus a low thump."""
    rng = np.random.default_rng(7)
    t = t_axis(0.40)
    noise = rng.standard_normal(len(t))
    # cheap low-pass: moving average twice (keeps the "wood" part, drops the hiss)
    k = np.ones(24) / 24
    noise = np.convolve(noise, k, mode="same")
    noise = np.convolve(noise, k, mode="same")
    burst = noise * np.exp(-t * 45.0)
    # thump: a sine that drops in pitch from 150 Hz to 60 Hz
    freq = 60 + 90 * np.exp(-t * 25.0)
    phase = 2 * np.pi * np.cumsum(freq) / SR
    thump = np.sin(phase) * np.exp(-t * 18.0)
    out = 0.6 * burst / (np.max(np.abs(burst)) or 1) + 0.9 * thump
    return finish(out, 0.003, 0.06)


def harp():
    """Plucked string (Karplus-Strong) on A4, with a soft release."""
    seconds = 0.55
    freq = 440.0
    n = int(SR * seconds)
    period = int(round(SR / freq))
    rng = np.random.default_rng(3)
    buf = rng.uniform(-1.0, 1.0, period)
    # smooth the starting noise a little so the pluck is not a harsh click
    buf = np.convolve(buf, np.ones(3) / 3, mode="same")
    out = np.zeros(n)
    for i in range(n):
        out[i] = buf[i % period]
        # average with the next sample = the string loses its high notes over time
        buf[i % period] = 0.5 * (buf[i % period] + buf[(i + 1) % period]) * 0.996
    # the raw pluck is bright; one moving average softens it
    out = np.convolve(out, np.ones(4) / 4, mode="same")
    return finish(out, 0.006, 0.08)


def main():
    write_wav("chime.wav", chime())
    write_wav("knock.wav", knock())
    write_wav("harp.wav", harp())


if __name__ == "__main__":
    main()
