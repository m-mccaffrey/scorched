"""Standing Orders' sound bank -- terse military blips, nothing melodramatic."""

from __future__ import annotations

from lanlib.audio import Sfx, arp, blip, noise_burst, sweep, thud

class OrdersSfx(Sfx):
    def build(self) -> None:
        self.add("click", blip(1200, 0.035, 0.22))
        self.add("select", blip(880, 0.05, 0.20, square=True))
        self.add("order", sweep(600, 900, 0.07, level=0.18, square=True))
        self.add("deny", sweep(420, 170, 0.13, level=0.20, square=True))
        self.add("ready", arp((660, 880), 0.06))
        self.add("turn", arp((523, 659, 784), 0.055))
        self.add("shot", blip(2200, 0.028, 0.10))
        self.add("kill", thud(0.13, 190, 0.5))
        self.add("boom", noise_burst(0.5, 300, 0.9))
        self.add("spawn", blip(520, 0.06, 0.16, square=True))
        self.add("ready_building", arp((440, 660), 0.07))
        self.add("capture", arp((784, 988), 0.07))
        self.add("win", arp((523, 659, 784, 1047), 0.11))
