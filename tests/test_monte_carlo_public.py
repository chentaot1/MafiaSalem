"""Tests for public Monte Carlo showcase modules (no engine required)."""

from scripts.monte_carlo.config import (
    LOBBY_SKILL,
    LYNCH_PROB_CAP,
    LYNCH_PROB_PER_SUSPICION,
    _competence_for_axis,
    _competence_for_role,
)
from scripts.monte_carlo.day import lynch_attempt_probability


def test_lynch_probability_zero_suspicion() -> None:
    assert lynch_attempt_probability(0) == 0.0


def test_lynch_probability_scales_with_suspicion() -> None:
    assert lynch_attempt_probability(3) == min(LYNCH_PROB_CAP, 3 * LYNCH_PROB_PER_SUSPICION)


def test_competence_clamped_for_hard_roles() -> None:
    transporter = _competence_for_axis("Transporter", "targeting")
    psychic = _competence_for_axis("Psychic", "targeting")
    assert transporter < psychic
    assert 0.12 <= transporter <= 1.0


def test_average_lobby_skill_near_coin_flip_on_moderate_roles() -> None:
    assert LOBBY_SKILL == 0.5
    sheriff = _competence_for_role("Sheriff")
    assert 0.35 <= sheriff <= 0.75
