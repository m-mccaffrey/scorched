"""Shared infrastructure for the LAN games in this repository.

Everything in here is game-agnostic: the wire protocol, LAN discovery, the
immediate-mode widget kit, and the audio synthesiser. A game imports what it
needs and supplies its own rules, art and sound bank.

The split exists because these pieces were written once for Scorched and are
worth exactly nothing if the next game has to copy-paste them.
"""

__all__ = ["protocol", "discovery", "ui", "theme", "audio"]
