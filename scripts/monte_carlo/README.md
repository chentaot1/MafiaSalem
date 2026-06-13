# Monte Carlo balance simulator

Engine-backed nights via `bridge.py` → `engine/night.run_night_pipeline`. Days/lynch are **statistical** (`day.py`), not live Discord tribunal.

**Monte Carlo type:** many randomized trials → win-rate estimates; nights are **deterministic given actions**; days are a **heuristic tribunal model** (not production UX). See root [`README.md`](../../README.md#is-it-a-true-monte-carlo) for the full breakdown.

## Quick start

```text
python scripts/monte_carlo_sim.py --quiet --generator-trials 2000 --player-count 7 --seed 42
python scripts/monte_carlo_sim.py --fixed-lobby --n 5000 --quiet
python scripts/mc_preflight.py
python scripts/mc_preflight.py --parity
```

## Before big baselines (1M+ trials)

1. `python scripts/mc_preflight.py --parity`
2. Or manually: `--audit`, `--parity-nights`, pytest per `MAFIASALEM_ROADMAP.md` §8.1

Large scripts (`_baseline_final_5_6_7_1m.py`, etc.) often disable engine invariants for speed — run preflight after **any** `engine/night.py` change.

## Role / balance change checklist

| Step | File |
|------|------|
| 1 | `config.py` — lists, immunities |
| 2 | `game_roles.py` — generator |
| 3 | `engine/night.py` — resolution |
| 4 | `scripts/monte_carlo/config.py` — `ROLE_META`, difficulties |
| 5 | `scripts/monte_carlo/night_ai.py` |
| 6 | `scripts/monte_carlo/bridge.py` — `_player_to_role_state` |
| 7 | `game.py` / `faction_win_logic.py` — wins |
| 8 | `scripts/monte_carlo/day.py` — if day/lynch behavior changes |
| 9 | `scripts/sim_test.py` — edge-case scenario |
| 10 | `reanimate_expand.py` — if Retributionist corpses change |
| 11 | `python scripts/mc_preflight.py --parity` |

## Shared with production

- Nights: `reanimate_expand.py`, `run_night_pipeline`
- Faction wins: `faction_win_logic.py`
- Two-player stalemate: `stalemate_wins.py`
- Draw overrides: `draw_override_wins.py`

## Engine QA (separate tool)

`python scripts/sim_test.py` — behavioral scenarios (outcome asserts), fuzz, optional systematic matrices.

Fast regression: `python scripts/sim_test.py --scenarios-only` (~47 scripted nights, ~few seconds).

Power deep (recommended): `python scripts/sim_test.py --deep` — scenarios, fuzz, parallel exhaustive (all 7p ×5 nights), sampled 2/3/4-way systematic (~30 min parallel).

Soak: `python scripts/sim_test.py --quad` (~6 min) or `--penta` (~35 min) — full cartesian on 4 role-sets; crash hunt, not logic oracles.

Middle ground: `--systematic-actions --systematic-role-sets 500 --systematic-sample 24 --jobs 12`
