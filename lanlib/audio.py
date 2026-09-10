"""Procedural sound effects.

Every sound is synthesised from arithmetic at start-up, so a game ships no
audio files and sounds identical on every machine. The whole soundtrack costs
a couple of hundred kilobytes of RAM and no disk I/O -- which is the kind of
thing you notice on a Pi booting from an SD card.

This module owns the machinery: the mixer lifecycle, a named bank of sounds,
and a set of waveform builders to compose them from. A game subclasses
:class:`Sfx` and fills in ``build`` with its own bank.

If the machine has no working audio device (a headless server, a Pi with HDMI
audio not yet configured) initialisation fails softly and every call becomes a
no-op. Nobody should lose a game night to ALSA.
"""

from __future__ import annotations

import array
import math
import random

import pygame

RATE = 22050
AMPLITUDE = 10000


class Sfx:
    """A bank of synthesised effects with a master volume.

    Subclass and override :meth:`build` to register sounds. Each name maps to
    a list of variants; :meth:`play` picks one at random, or -- when the caller
    passes a magnitude -- the variant matching that intensity, which is how a
    small pop and a large detonation share one name.
    """

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
            self.build()
            self.ok = True
        except (pygame.error, MemoryError):
            self.ok = False

    # -- to override -------------------------------------------------------
    def build(self) -> None:
        """Register this game's sounds. Override me."""

    def add(self, name: str, *samples: array.array) -> None:
        """Register one or more variants of a named effect."""
        self.sounds[name] = [sound(s) for s in samples]

    # -- playback ----------------------------------------------------------
    def play(self, name: str, magnitude: float | None = None) -> None:
        """Play a sound. ``magnitude`` (0..1) picks across graded variants."""
        if not self.ok:
            return
        bank = self.sounds.get(name)
        if not bank:
            return
        if magnitude is None or len(bank) == 1:
            chosen = random.choice(bank)
        else:
            index = int(max(0.0, min(0.999, magnitude)) * len(bank))
            chosen = bank[min(index, len(bank) - 1)]
        try:
            channel = pygame.mixer.find_channel(True)
            if channel is not None:
                channel.set_volume(self.volume)
                channel.play(chosen)
        except pygame.error:
            pass

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(1.0, volume))


# ---------------------------------------------------------------------------
# Waveform builders
# ---------------------------------------------------------------------------

def sound(samples: array.array) -> pygame.mixer.Sound:
    return pygame.mixer.Sound(buffer=samples.tobytes())


def blank(seconds: float) -> array.array:
    return array.array("h", bytes(2 * int(RATE * seconds)))


def _clip(value: float) -> int:
    return max(-32767, min(32767, int(value)))


def sweep(f0: float, f1: float, seconds: float, level: float = 0.35,
          square: bool = False) -> array.array:
    """A tone gliding between two frequencies, with a soft attack and decay."""
    out = blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (f0 + (f1 - f0) * t) / RATE
        wave = (1.0 if math.sin(phase) >= 0 else -1.0) if square else math.sin(phase)
        env = math.sin(math.pi * t) ** 0.7
        out[i] = _clip(wave * AMPLITUDE * level * env)
    return out


def blip(freq: float, seconds: float, level: float = 0.25,
         square: bool = False) -> array.array:
    """A short plucked tone. The workhorse of interface feedback."""
    out = blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * freq / RATE
        wave = (1.0 if math.sin(phase) >= 0 else -1.0) if square else math.sin(phase)
        out[i] = _clip(wave * AMPLITUDE * level * math.exp(-5.0 * t))
    return out


def noise_burst(seconds: float, cutoff: float, weight: float = 1.0,
                rumble: float = 48.0) -> array.array:
    """Filtered noise with a fast attack and a long tail.

    A one-pole low-pass over white noise is a remarkably convincing explosion,
    and the lower the cutoff the bigger the bang feels.
    """
    out = blank(seconds)
    n = len(out)
    rng = random.Random(int(cutoff))
    alpha = min(1.0, 2 * math.pi * cutoff / RATE)
    low = 0.0
    phase = 0.0
    for i in range(n):
        t = i / n
        low += alpha * (rng.uniform(-1.0, 1.0) - low)
        env = math.exp(-3.4 * t) * (1.0 if t > 0.004 else t / 0.004)
        phase += 2 * math.pi * (rumble + 26 * (1 - t)) / RATE
        body = low * 0.8 + math.sin(phase) * 0.45 * weight
        out[i] = _clip(body * AMPLITUDE * env * (0.7 + 0.5 * weight))
    return out


def thud(seconds: float = 0.16, freq: float = 150.0,
         level: float = 0.7) -> array.array:
    """A dull impact: a falling sine with a little grit on top."""
    out = blank(seconds)
    n = len(out)
    phase = 0.0
    rng = random.Random(7)
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (freq - freq * 0.47 * t) / RATE
        env = math.exp(-7.0 * t)
        out[i] = _clip((math.sin(phase) * 0.7 + rng.uniform(-0.3, 0.3))
                       * AMPLITUDE * level * env)
    return out


def warble(seconds: float, freq: float, depth: float, rate: float,
           level: float = 0.28) -> array.array:
    """A wobbling tone -- shields, force fields, anything energised."""
    out = blank(seconds)
    n = len(out)
    phase = 0.0
    for i in range(n):
        t = i / n
        phase += 2 * math.pi * (freq + depth * math.sin(t * rate)) / RATE
        out[i] = _clip(math.sin(phase) * AMPLITUDE * level * math.exp(-5.5 * t))
    return out


def arp(freqs, note: float = 0.09, level: float = 0.2) -> array.array:
    """A little square-wave arpeggio. Fanfares, confirmations, defeat stings."""
    out = blank(note * len(freqs))
    per = int(RATE * note)
    for index, freq in enumerate(freqs):
        phase = 0.0
        for i in range(per):
            pos = index * per + i
            if pos >= len(out):
                break
            t = i / per
            phase += 2 * math.pi * freq / RATE
            out[pos] = _clip((1.0 if math.sin(phase) >= 0 else -1.0)
                             * AMPLITUDE * level * math.exp(-3.0 * t))
    return out
