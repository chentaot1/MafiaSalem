"""Coverage audit vs live bot config."""
from __future__ import annotations

from scripts.monte_carlo.config import ALL_ROLES, ROLE_META, ROLE_OPTIMAL_DIFFICULTY


def audit_against_bot_config() -> None:
    try:
        import config as bot_config  # noqa: F401
    except ImportError:
        print("SIM AUDIT skipped (production config.py not present — public showcase checkout)")
        return

    bot_roles = set(bot_config.TOWN_ROLES) | set(bot_config.ALL_MAFIA_ROLES) | set(
        bot_config.NEUTRAL_BENIGN_ROLES
    ) | {"Jester", "Executioner", "Witch", "Pirate", "Arsonist", "Chaos", "Serial Killer"}
    if ALL_ROLES != bot_roles:
        missing = sorted(bot_roles - ALL_ROLES)
        extra = sorted(ALL_ROLES - bot_roles)
        raise SystemExit(f"SIM AUDIT FAILED role universe. missing={missing} extra={extra}")

    missing_meta = sorted(bot_roles - set(ROLE_META))
    extra_meta = sorted(set(ROLE_META) - bot_roles)
    missing_diff = sorted(bot_roles - set(ROLE_OPTIMAL_DIFFICULTY))
    extra_diff = sorted(set(ROLE_OPTIMAL_DIFFICULTY) - bot_roles)
    if missing_meta or extra_meta or missing_diff or extra_diff:
        raise SystemExit(
            "SIM AUDIT FAILED coverage. "
            f"missing_meta={missing_meta} extra_meta={extra_meta} "
            f"missing_difficulty={missing_diff} extra_difficulty={extra_diff}"
        )
    if "Civilian" in ROLE_OPTIMAL_DIFFICULTY or "Civilian" in ROLE_META:
        raise SystemExit("SIM AUDIT FAILED: Civilian must not appear in sim role tables")
    print(f"SIM AUDIT OK: {len(bot_roles)} roles, optimal_difficulty + ROLE_META complete")
