"""Scorched's sound bank.

The synthesis machinery lives in :mod:`lanlib.audio`; this is just the list of
noises an artillery game needs.
"""

from __future__ import annotations

from lanlib.audio import Sfx, arp, blip, noise_burst, sweep, thud, warble

#: Blast radius that counts as "as loud as it gets", for picking a boom sample.
LOUDEST_BLAST = 70.0


class ScorchedSfx(Sfx):
    def build(self) -> None:
        self.add("launch", sweep(300, 1200, 0.20, level=0.35))
        # Three graded detonations, chosen by blast radius.
        self.add("boom",
                 noise_burst(0.34, 620, 0.7),
                 noise_burst(0.55, 380, 0.85),
                 noise_burst(0.85, 210, 1.0, rumble=40))
        self.add("thud", thud())
        self.add("split", sweep(880, 1760, 0.10, level=0.22, square=True))
        self.add("shield", warble(0.22, 1200, 400, 40))
        self.add("click", blip(1400, 0.035, 0.25))
        self.add("move", blip(260, 0.045, 0.18, square=True))
        self.add("select", sweep(520, 1040, 0.09, level=0.22, square=True))
        self.add("deny", sweep(400, 160, 0.14, level=0.22, square=True))
        self.add("turn", arp((523, 659, 784), 0.06))
        self.add("win", arp((523, 659, 784, 1047), 0.12))
