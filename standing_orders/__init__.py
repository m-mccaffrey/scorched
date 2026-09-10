"""Standing Orders -- a turn-based skirmish RTS for LAN play.

Build times and unit speeds are measured in turns, not seconds. Every turn all
players secretly and simultaneously queue orders; the server then plays the
whole turn out at once and every client watches a replay of what happened.

Nobody ever needs faster hands than anybody else, and -- because the turn is
resolved exactly once, on the server -- a Raspberry Pi and a desktop can never
disagree about the result.
"""

__version__ = "0.1.0"
