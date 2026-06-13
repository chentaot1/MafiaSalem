# sim_test — night-engine QA harness (private implementation)

> **Source is private.** This document describes the harness that lives in `scripts/sim_test.py` in the full local repo. It is **not** published — it depends on `engine/night.run_night_pipeline` and `game.py`.

## What it is

A **second runtime** for the production night pipeline:

- Fake Discord (`FakeGuild`, `FakeMember`, DM inboxes) — no bot token, no guild IDs
- **47 behavioral scenarios** with exact outcome oracles (deaths, DMs, buckets, visit logs)
- **Fuzz** — random legal-ish `night_actions`, must never crash
- **Exhaustive 7p** — all generator role sets × N nights
- **Systematic cartesian** — seat tuples × per-role action variants (`--deep`, `--quad`, `--penta`)

Every scenario calls the same `run_night_pipeline` as live Discord play, then `assert_post_night_pipeline_invariants`.

## Four layers

| Layer | Flag / preset | Purpose |
|-------|----------------|---------|
| **1. Scenarios** | `--scenarios-only` (~47 nights, seconds) | Human-written regression oracles |
| **2. Fuzz** | part of `--deep` | Survival + invariant property tests |
| **3. Exhaustive** | part of `--deep` | All legal 7p role combinations |
| **4. Systematic** | `--deep`, `--quad`, `--penta` | Cartesian action matrices, parallel workers |

Failures write JSON repros under `tests/repros/` (seed, roles, actions, stack trace).

## Scenario catalog (47)

| Scenario | What it guards |
|----------|----------------|
| `scenario_transport_redirects_target` | Transporter swaps visit destination |
| `scenario_control_immune_role_not_redirected` | Witch cannot redirect control-immune roles |
| `scenario_corrupted_actions_do_not_crash` | Malformed payloads must not crash pipeline |
| `scenario_gatekeeper_corrupted_guard_target_does_not_crash` | Bad GK guard target shape |
| `scenario_graveyard_corruption_does_not_crash_sync` | Corrupt graveyard sync |
| `scenario_ignite_is_unstoppable_even_through_heal` | Arsonist ignite pierces heal |
| `scenario_transport_redirects_witch_control_targets` | Transport + Witch control chain |
| `scenario_transport_does_not_redirect_self_only_actions` | Self-only actions stay on actor |
| `scenario_witch_cannot_retarget_self_only_actions` | Witch vest/clean/ward/protect rules |
| `scenario_witch_can_prevent_arsonist_ignite_by_forcing_douse` | Control forces douse instead of ignite |
| `scenario_arsonist_ignite_while_doused_kills_self` | Self-douse + ignite suicide |
| `scenario_arsonist_basic_defense_survives_normal_kill` | Basic defense tier |
| `scenario_transport_does_not_redirect_pirate_plunder` | Pirate plunder not transported |
| `scenario_witch_redirects_mafia_kill_target` | Witch on Mobster kill |
| `scenario_executioner_converts_to_jester_on_target_non_lynch` | Exe → Jester conversion |
| `scenario_executioner_marks_win_on_lynch` | Exe win on target lynch |
| `scenario_arsonist_clean_removes_douse` | Clean removes douse |
| `scenario_bodyguard_blocked_does_not_protect` | RB'd BG does not protect |
| `scenario_vigilante_guilt_town_vs_mafia` | Guilt on town kill, not mafia |
| `scenario_blocked_investigator_gets_interrupt` | RB interrupts Investigator |
| `scenario_blocked_tracker_gets_track_interrupt` | RB interrupts Tracker |
| `scenario_blocked_lookout_gets_watch_interrupt` | RB interrupts Lookout |
| `scenario_witch_receives_controlled_sheriff_result` | Witch gets controlled invest DM |
| `scenario_witch_receives_controlled_investigator_bucket` | Witch gets Inv bucket |
| `scenario_mobster_investigator_protective_shield_bucket` | Mobster + BG protective bucket |
| `scenario_chaos_records_visit_targets` | Chaos visit log targets |
| `scenario_corrupt_misc_snapshot_still_heals` | Checkpoint resume + heal |
| `scenario_investigative_checkpoint_no_duplicate_watch_dm` | Crash-resume: no duplicate LO DM |
| `scenario_lookout_excludes_self_heal_from_visitors` | LO visitor list excludes self-heal |
| `scenario_witch_steals_psychic_vision` | Witch receives psychic vision |
| `scenario_transport_ack_uses_effective_visit_house` | Transport ack uses effective visits |
| `scenario_blocked_sheriff_gets_investigate_interrupt` | RB interrupts Sheriff |
| `scenario_mole_consig_reveals_exact_role` | Mole exact role reveal |
| `scenario_seer_gaze_two_town_reads_friends` | Seer Friends bucket |
| `scenario_investigator_frame_beats_douse` | Frame > douse on Inv bucket |
| `scenario_framer_alters_investigations` | Framer frame on investigations |
| `scenario_roleblocked_visitor_does_not_trigger_alert` | RB visitor doesn't trigger SG alert |
| `scenario_gatekeeper_blocks_non_mafia_visitors` | GK blocks Doctor heal to guarded target |
| `scenario_revealed_mayor_cannot_be_healed` | Revealed Mayor heal block |
| `scenario_witch_night1_shield` | Witch N1 survival shield |
| `scenario_jester_night1_shield` | Jester N1 survival shield |
| `scenario_retributionist_reanimate_doctor_heals` | Retri reanimate Doctor heal |
| `scenario_pirate_plunder_roleblocks_even_on_loss` | Pirate RB on duel loss |
| `scenario_pirate_win_requires_plunder_kill` | Pirate win condition |
| `scenario_first_healer_wins_on_duplicate_heal_target` | Duplicate heal target: first wins |
| `scenario_lookout_dm_lists_visitors` | LO DM visitor list |
| `scenario_chain_roleblock_fixed_point` | RB chain converges |

Plus **8 broken scenarios** (`--probe-failure`) to verify the harness catches failures.

## Example pattern (representative)

```python
async def scenario_witch_receives_controlled_sheriff_result():
    game, guild, members = make_game(seed=..., n=7)
    game.player_roles.update({1: "Witch", 2: "Sheriff", 3: "Mobster", ...})
    game.night_actions[1] = {"type": "control", "actor": 1, "target": 2, ...}
    game.night_actions[2] = {"type": "investigate", "actor": 2, "role": "Sheriff", "target": 3}
    out = await run_night_pipeline(game, guild)
    assert_dm_received(members[1], ...)  # Witch gets Sheriff result
    assert_post_night_invariants(game, out)
```

## Presets (private repo)

```bash
python scripts/sim_test.py --scenarios-only          # fast regression
python scripts/sim_test.py --deep                    # ~30 min · 10M+ pipeline nights
python scripts/sim_test.py --quad                    # 4-way systematic soak ~6 min
python scripts/sim_test.py --probe-failure           # negative harness check
```
