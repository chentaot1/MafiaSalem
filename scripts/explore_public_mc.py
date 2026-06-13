#!/usr/bin/env python3
"""Runnable demo for the public MC showcase (no night engine).

Usage (from repo root after clone):
    python scripts/explore_public_mc.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.monte_carlo.config import (  # noqa: E402
    LOBBY_SKILL,
    LYNCH_PROB_CAP,
    LYNCH_PROB_PER_SUSPICION,
    ROLE_TARGETING_DIFFICULTY,
    _competence_for_axis,
    _competence_for_role,
)
from scripts.monte_carlo.day import (  # noqa: E402
    _vote_guilty_probability,
    _vote_innocent_probability,
    lynch_attempt_probability,
)
from scripts.monte_carlo.state import Player  # noqa: E402


def _print_competence_sample() -> None:
    print("=== Competence model (LOBBY_SKILL=%.1f = average pub) ===" % LOBBY_SKILL)
    print(f"{'Role':<18} {'P(opt)':>7}  {'targeting':>9}  {'usage':>7}  {'day':>7}")
    roles = [
        "Psychic",
        "Sheriff",
        "Doctor",
        "Transporter",
        "Vigilante",
        "Witch",
        "Mobster",
        "Arsonist",
        "Mayor",
    ]
    for role in roles:
        p = _competence_for_role(role)
        t = ROLE_TARGETING_DIFFICULTY.get(role, 0.5)
        print(f"{role:<18} {p:>7.3f}  {t:>9.2f}  {_competence_for_axis(role, 'usage'):>7.3f}  {_competence_for_axis(role, 'day'):>7.3f}")
    print()


def _print_lynch_curve() -> None:
    print("=== Lynch attempt probability (statistical day model) ===")
    print(f"formula: min({LYNCH_PROB_CAP}, suspicion * {LYNCH_PROB_PER_SUSPICION})")
    for s in range(0, 6):
        print(f"  suspicion {s}: P(trial) = {lynch_attempt_probability(s):.0%}")
    print("  (Sheriff +3 on mafia -> suspicion 3 -> ~66% trial chance)")
    print()


def _print_tribunal_weights() -> None:
    print("=== Sample tribunal vote weights (suspicion=3 on defendant) ===")
    roles = ["Sheriff", "Mobster", "Doctor", "Townie", "Jester"]
    players = [Player(i=i, role=r) for i, r in enumerate(roles)]
    defendant = 1  # Mobster on trial
    suspect_score = 3
    for pid in range(len(players)):
        g = _vote_guilty_probability(players, pid, defendant, suspect_score)
        inn = _vote_innocent_probability(players, pid, defendant, suspect_score)
        print(
            f"  P{pid} {players[pid].role:<10}  P(guilty)={g:.2f}  P(innocent)={inn:.2f}"
        )
    print()


def main() -> None:
    print("MafiaSalem — public Monte Carlo methodology demo")
    print("(Night engine and full trials are not in this repo.)\n")
    _print_competence_sample()
    _print_lynch_curve()
    _print_tribunal_weights()
    print("Next: read scripts/monte_carlo/config.py and day.py, sim_test catalog at scripts/sim_test/README.md")
    print("Verify: python -m pytest tests/test_monte_carlo_public.py -q")


if __name__ == "__main__":
    main()
