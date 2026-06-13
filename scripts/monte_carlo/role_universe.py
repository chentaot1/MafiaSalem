"""Public-safe role taxonomy for Monte Carlo (no Discord IDs, no engine imports)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Set

ALL_MAFIA_ROLES: List[str] = [
    "Mobster",
    "Framer",
    "Gravedigger",
    "Consort",
    "Hypnotist",
    "Mole",
    "Tailor",
    "Gatekeeper",
]

TOWN_ROLES: List[str] = [
    "Retributionist",
    "Vigilante",
    "Sheriff",
    "Investigator",
    "Doctor",
    "Escort",
    "Transporter",
    "Mayor",
    "Bodyguard",
    "Lookout",
    "Scary Grandma",
    "Tracker",
    "Psychic",
    "Deputy",
    "Seer",
]

NEUTRAL_BENIGN_ROLES: List[str] = ["Survivor", "Guardian Angel"]

SEER_FRIENDLY_EXTRA_ROLES: List[str] = ["Guardian Angel", "Jester"]

PSYCHIC_ODD_EVIL_NEUTRALS: List[str] = [
    "Witch",
    "Executioner",
    "Jester",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
]

SEER_NEUTRAL_KILLING_ROLES: List[str] = ["Arsonist", "Serial Killer"]
SEER_HOSTILE_NEUTRAL_ROLES: List[str] = ["Survivor", "Chaos", "Executioner", "Witch", "Pirate"]

DEPUTY_GUN_EVIL_NEUTRALS: List[str] = [
    "Witch",
    "Executioner",
    "Jester",
    "Pirate",
    "Arsonist",
    "Chaos",
    "Serial Killer",
]

ROLEBLOCK_IMMUNE_ROLES: List[str] = [
    "Retributionist",
    "Scary Grandma",
    "Witch",
    "Consort",
    "Escort",
    "Pirate",
    "Transporter",
    "Serial Killer",
]

CONTROL_IMMUNE_ROLES: List[str] = [
    "Retributionist",
    "Transporter",
    "Scary Grandma",
    "Witch",
    "Pirate",
    "Chaos",
    "Guardian Angel",
]

ARSONIST_HARMLESS_NEUTRALS: Set[str] = {
    "Witch",
    "Survivor",
    "Executioner",
    "Jester",
    "Chaos",
    "Guardian Angel",
    "Pirate",
    "Psychic",
    "Deputy",
    "Seer",
}

NEUTRAL_KILLING_ROLES: FrozenSet[str] = frozenset({"Arsonist", "Serial Killer"})

CHAOS_STARTING_USES = 2
SMALL_LOBBY_MAX_PLAYER_COUNT = 7


def role_starting_charges(*, player_count: int, full_charges: int = 2) -> int:
    if full_charges <= 1:
        return full_charges
    if player_count <= SMALL_LOBBY_MAX_PLAYER_COUNT:
        return 1
    return full_charges


def chaos_starting_uses(player_count: int) -> int:
    return role_starting_charges(player_count=player_count, full_charges=CHAOS_STARTING_USES)


@dataclass(frozen=True)
class TwoPlayerStalemate:
    applies: bool
    winner: Optional[str] = None


def lookup_two_player_stalemate(role_a: str, role_b: str) -> TwoPlayerStalemate:
    """Subset of production stalemate rules used by MC win checks (showcase copy)."""
    pair = frozenset({role_a, role_b})
    if pair == frozenset({"Mobster", "Serial Killer"}):
        return TwoPlayerStalemate(applies=True, winner="Mafia")
    if pair == frozenset({"Mobster", "Arsonist"}):
        return TwoPlayerStalemate(applies=True, winner=None)
    if "Serial Killer" in pair and "Arsonist" in pair:
        return TwoPlayerStalemate(applies=True, winner=None)
    # Town + SK: Town wins stalemate when SK cannot outnumber
    if "Serial Killer" in pair:
        other = role_b if role_a == "Serial Killer" else role_a
        if other in TOWN_ROLES:
            return TwoPlayerStalemate(applies=True, winner="Town")
    return TwoPlayerStalemate(applies=False)
