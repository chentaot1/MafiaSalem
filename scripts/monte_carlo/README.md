# Monte Carlo — methodology (public) vs engine (private)

This folder on GitHub shows **how** balance simulation models pub lobbies. It does **not** include night resolution — that lives in private `engine/night.py` behind `bridge.py`.

## What you can read here

| Module | Teaches you… |
|--------|----------------|
| `config.py` | Three-axis competence model (`targeting` / `usage` / `day`), role difficulties, `MC_ACTION_JITTER` |
| `day.py` | Suspicion scores → lynch attempt probability → faction-aware guilty/innocent weights |
| `night_ai.py` | Heuristic night targeting (who Town/Mafia/neutrals aim at when skill &lt; optimal) |
| `state.py` | Lightweight `Player` model + pick helpers (no engine state machine) |
| `role_universe.py` | 32-role taxonomy for sim (no Discord deployment IDs) |

## What stays private

`bridge.py`, `simulate.py`, `generator.py`, `wins.py`, `monte_carlo_sim.py` — wire night actions into `run_night_pipeline` and aggregate millions of trials.

## Hybrid model (why this split is intentional)

```text
  [published]  night_ai → picks actions (heuristic)
  [private]    bridge   → run_night_pipeline → deaths, DMs, visit logs
  [published]  day.py   → evidence → lynch (statistical, not Discord tribunal)
```

Nights are deterministic given actions; randomness is in **lobby draws, AI choices, and day votes**. Publishing `day.py` + `config.py` shows the balance *science* without shipping kill-order or transport algebra.

See root [`README.md`](../../README.md) for full architecture and design decisions.

## Quick start (public repo)

```bash
cd MafiaSalem   # repo root
pip install -r requirements.txt
python scripts/explore_public_mc.py
python -m pytest tests/test_monte_carlo_public.py -q
```

## Quick start (private repo — full trials)

```text
python scripts/monte_carlo_sim.py --quiet --generator-trials 2000 --player-count 7 --seed 42
python scripts/mc_preflight.py --parity
```

## Role / balance change checklist (private repo paths)

| Step | File |
|------|------|
| 1 | `config.py` — lists, immunities |
| 2 | `game_roles.py` — generator |
| 3 | `engine/night.py` — resolution (**private**) |
| 4 | `scripts/monte_carlo/config.py` — `ROLE_META`, difficulties (**public**) |
| 5 | `scripts/monte_carlo/night_ai.py` (**public**) |
| 6 | `scripts/monte_carlo/bridge.py` (**private**) |
| 7 | `game.py` / `faction_win_logic.py` — wins (**private**) |
| 8 | `scripts/monte_carlo/day.py` — lynch behavior (**public**) |
| 9 | `scripts/sim_test.py` — edge-case scenario (**private**) |
