"""Monte Carlo balance simulator.

Public showcase (this repo): competence model, day/lynch heuristics, night AI targeting.
Private full repo: engine-backed ``simulate_once`` via ``bridge.py`` + ``engine/night.py``.
"""
from scripts.monte_carlo.audit import audit_against_bot_config
from scripts.monte_carlo.config import (
    ALL_ROLES,
    ENGINE_NIGHT_INVARIANTS,
    GATEKEEPER_BLOCKS_ONE,
    LOBBY_SKILL,
    REALISTIC_N1_VIG_SHOOT,
    REALISTIC_NIGHT_ACTIONS,
    ROLE_META,
    ROLE_OPTIMAL_DIFFICULTY,
    ROLE_TARGETING_DIFFICULTY,
    ROLE_USAGE_DIFFICULTY,
    ROLE_DAY_DIFFICULTY,
    USE_DIFFICULTY_LAYER,
    _competence_for_axis,
    _competence_for_role,
)
from scripts.monte_carlo.runtime import default_trial_workers

try:
    from scripts.monte_carlo.generator import (
        enumerate_role_sets,
        run_enumeration,
        run_generator_weighted_trials,
        run_generator_weighted_trials_parallel,
        run_monte_carlo,
        sample_generator_roles,
        sample_generator_roles_constraints,
    )
    from scripts.monte_carlo.simulate import simulate_once
except ImportError:  # public showcase checkout (no engine bridge)
    enumerate_role_sets = None  # type: ignore[assignment,misc]
    run_enumeration = None
    run_generator_weighted_trials = None
    run_generator_weighted_trials_parallel = None
    run_monte_carlo = None
    sample_generator_roles = None
    sample_generator_roles_constraints = None
    simulate_once = None

__all__ = [
    "ALL_ROLES",
    "ENGINE_NIGHT_INVARIANTS",
    "GATEKEEPER_BLOCKS_ONE",
    "LOBBY_SKILL",
    "REALISTIC_N1_VIG_SHOOT",
    "REALISTIC_NIGHT_ACTIONS",
    "ROLE_META",
    "ROLE_OPTIMAL_DIFFICULTY",
    "ROLE_TARGETING_DIFFICULTY",
    "ROLE_USAGE_DIFFICULTY",
    "ROLE_DAY_DIFFICULTY",
    "USE_DIFFICULTY_LAYER",
    "_competence_for_axis",
    "_competence_for_role",
    "audit_against_bot_config",
    "default_trial_workers",
    "enumerate_role_sets",
    "run_enumeration",
    "run_generator_weighted_trials",
    "run_generator_weighted_trials_parallel",
    "run_monte_carlo",
    "sample_generator_roles",
    "sample_generator_roles_constraints",
    "simulate_once",
]
