# Repro artifacts

This directory contains JSON repro artifacts automatically written by `scripts/sim_test.py` when a
property/invariant check fails.

- Each file includes the **role-set**, **night_actions payload**, and **seed/context** needed to replay/debug.
- Replay engine repros: `python scripts/replay_repros.py` (all `tests/repros/*.json` with `night_actions`).

