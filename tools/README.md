# tools/

Offline tooling. **Nothing here is imported by the game**, ships to a player,
or runs on a Pi 400. Install it separately:

    pip install -e ".[tune]"

## The parameter search

The bot's difficulty tiers do not form a ladder: Moderate beats both Veteran
and Cyborg. Hand-sweeping single constants does not move it — the
mass-before-committing threshold was swept across every value from 3 to 7 and
the result stayed at 1-6 — which is the signature of an interaction between
knobs rather than one wrong number. There are 35 of them and nobody can hold
that in their head, so a black-box optimiser searches them instead.

    python3 -m tools.tune --generations 50 --seeds 6 --holdout 18

- `aiparams.py` — every tunable number, its sane range, and one call to install
  a candidate. The booleans in `Skill` are deliberately *not* searched: whether
  a Novice researches is design, not tuning.
- `arena.py` — bot-versus-bot matches and the loss. Every candidate plays the
  same seeds (so candidates are compared on the same games, not on their luck)
  and every pairing is played from both sides (so the search cannot learn which
  corner of the map is better). Budget is concentrated on the three pairings
  that are actually broken.
- `tune.py` — the CMA-ES driver.

The search **never edits the game**. It writes its candidate to
`tools/tuned.json` and prints the evidence, including a re-check on seeds the
candidate has never played. Whether those numbers go into `ai.py` is a human
decision, and the held-out result is what it should be decided on.
