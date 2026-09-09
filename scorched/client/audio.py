"""Procedural sound effects.

Every sound is synthesised at start-up from a few lines of arithmetic, so the
game ships no audio files and sounds identical on every machine.  It also means
the whole soundtrack costs about 200 KB of RAM and no disk I/O -- which is the
kind of thing you notice on a Pi booting from an SD card.

If the machine has no working audio device (a headless server, a Pi with HDMI
audio not yet configured) initialisation fails softly and every call becomes a
no-op.  Nobody should lose a game night to ALSA.
"""

from __future__ import annotations

import array
import math
import random

import pygame

RATE = 22050
AMPLITUDE = 10000


class Sfx:
    """Small bank of synthesised effects with a master volume."""

    def __init__(self, enabled: bool = True, volume: float = 0.6) -> None:
        self.ok = False
        self.volume = max(0.0, min(1.0, volume))
        self.sounds: dict[str, list] = {}
        if not enabled:
            return
        try:
            pygame.mixer.pre_init(RATE, -16, 1, 512)
            pygame.mixer.init(RATE, -16, 1, 512)
            pygame.mixer.set_num_channels(12)
        except pygame.error:
            return
        try:
            self._build()
            self.ok = True
        except (pygame.error, MemoryError):
            self.ok = False

    # -- playback ---------------------------------------------------------
    def play(self, name: str, magnitude: float = 0.0) -> None:
        if not self.ok:
            return
        bank = self.sounds.get(name)
        if not bank:
            return
        if name == "boom":
            # Pick the blast sample whose size best matches the crater.
            index = 0 if magnitude < 20 else (1 if magnitude < 42 else 2)
            sound = bank[min(index, len(bank) - 1)]
        else:
            sound = random.choice(bank)
        try:
            channel = pygame.mixer.find_channel(True)
            if channel is not None:
                channel.set_volume(self.volume)
                channel.play(sound)
        except pygame.error:
            pass

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, volume))

    # -- synthesis ---------------------------------------------------------
    def _build(self) -> None:
        self.sounds["launch"] = [_sound(_launch())]
        self.sounds["boom"] = [_sound(_boom(0.34, 620, 0.7)),
                               _sound(_boom(0.55, 380, 0.85)),
                               _sound(_boom(0.85, 210, 1.0))]
        self.sounds["thud"] = [_sound(_thud())]
        self.sounds["split"] = [_sound(_chirp(880, 1760, 0.10))]
        self.sounds["shield"] = [_sound(_shield())]
        self.sounds["click"] = [_sound(_blip(1400, 0.035, 0.25))]
        self.sounds["move"] = [_sound(_blip(260, 0.045, 0.18, square=True))]
        self.sounds["select"] = [_sound(_chirp(520, 1040, 0.09))]
        self.sounds["deny"] = [_sound(_chirp(400, 160, 0.14))]
        self.sounds["turn"] = [_sound(_arp((523, 659, 784), 0.06))]
        self.sounds["win"] = [_sound(_arp((523, 659, 784, 1047), 0.12))]


def _sound(samples: array.array) -> pygame.mixer.Sound:
    return pygame.mixer.Sound(buffer=samples.tobytes())


def _blank(seconds: float) -> array.array:
    return array.array("h", bytes(2 * int(RATE * seconds)))


def _clip(value: float) -> int:
    return max(-32767, min(32767, int(value)))


def _launch() -> array.array:
    """A short rising whistle -- the shell leaving the barrel."""
    seconds = 0.20
    out = _blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        freq = 300 + 900 * t
        phase += 2 * math.pi * freq / RATE
        env = math.sin(math.pi * t) ** 0.7
        out[i] = _clip(math.sin(phase) * AMPLITUDE * 0.35 * env)
    return out


def _boom(seconds: float, cutoff: float, weight: float) -> array.array:
    """Filtered noise with a fast attack and a long tail.

    A one-pole low-pass over white noise is a remarkably convincing explosion,
    and the lower the cutoff the bigger the bang feels.
    """
    out = _blank(seconds)
    n = len(out)
    rng = random.Random(int(cutoff))
    alpha = min(1.0, 2 * math.pi * cutoff / RATE)
    low = 0.0
    rumble_phase = 0.0
    for i in range(n):
        t = i / n
        noise = rng.uniform(-1.0, 1.0)
        low += alpha * (noise - low)
        # Decay: near-instant attack, exponential release.
        env = math.exp(-3.4 * t) * (1.0 if t > 0.004 else t / 0.004)
        rumble_phase += 2 * math.pi * (48 + 26 * (1 - t)) / RATE
        body = low * 0.8 + math.sin(rumble_phase) * 0.45 * weight
        out[i] = _clip(body * AMPLITUDE * env * (0.7 + 0.5 * weight))
    return out


def _thud() -> array.array:
    out = _blank(0.16)
    n = len(out)
    phase = 0.0
    rng = random.Random(7)
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (150 - 70 * t) / RATE
        env = math.exp(-7.0 * t)
        out[i] = _clip((math.sin(phase) * 0.7 + rng.uniform(-0.3, 0.3))
                       * AMPLITUDE * 0.7 * env)
    return out


def _chirp(f0: float, f1: float, seconds: float) -> array.array:
    out = _blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (f0 + (f1 - f0) * t) / RATE
        env = math.sin(math.pi * t) ** 0.6
        # Square wave: unmistakably 1990s.
        out[i] = _clip((1.0 if math.sin(phase) >= 0 else -1.0)
                       * AMPLITUDE * 0.22 * env)
    return out


def _blip(freq: float, seconds: float, level: float,
          square: bool = False) -> array.array:
    out = _blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * freq / RATE
        wave = (1.0 if math.sin(phase) >= 0 else -1.0) if square else math.sin(phase)
        out[i] = _clip(wave * AMPLITUDE * level * math.exp(-5.0 * t))
    return out


def _shield() -> array.array:
    out = _blank(0.22)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        freq = 1200 + 400 * math.sin(t * 40)
        phase += 2 * math.pi * freq / RATE
        env = math.exp(-5.5 * t)
        out[i] = _clip(math.sin(phase) * AMPLITUDE * 0.28 * env)
    return out


def _arp(freqs, note: float) -> array.array:
    out = _blank(note * len(freqs))
    per = int(RATE * note)
    for index, freq in enumerate(freqs):
        phase = 0.0
        for i in range(per):
            pos = index * per + i
            if pos >= len(out):
                break
            t = i / per
            phase += 2 * math.pi * freq / RATE
            env = math.exp(-3.0 * t)
            out[pos] = _clip((1.0 if math.sin(phase) >= 0 else -1.0)
                             * AMPLITUDE * 0.2 * env)
    return out
