"""Events, gateway watchdog, DM outbox, connect loop."""

from __future__ import annotations

from bot_app.imports import *  # noqa: F403
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay
from bot_app.shared import safe_channel_send, safe_interaction_ephemeral
from game import active_games

_instance_lock_fd: Optional[int] = None


def _spawn_tribunal_resume_once(bot: object, key: str, coro: object) -> None:
    """Idempotent tribunal resume spawn guard (audit #24)."""
    spawned = getattr(bot, "_tribunal_resume_spawned", None)
    if spawned is None:
        spawned = set()
        setattr(bot, "_tribunal_resume_spawned", spawned)
    if key in spawned:
        return
    spawned.add(key)
    bot.loop.create_task(coro)  # type: ignore[attr-defined]


def _strict_config_enabled() -> bool:
    return os.environ.get("MAFIABOT_STRICT_CONFIG", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _pid_is_alive(pid: int) -> bool:
    """Best-effort liveness probe for the single-instance lock (Windows-safe)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        kernel32.SetLastError(0)
        handle = kernel32.OpenProcess(synchronize, 0, int(pid))
        if handle:
            kernel32.CloseHandle(handle)
            return True
        err = kernel32.GetLastError()
        # ERROR_ACCESS_DENIED: process exists but we cannot open it with SYNCHRONIZE.
        return bool(err == 5)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _acquire_single_instance_lock() -> None:
    """
    Best-effort local single-instance guard.

    Prevents accidentally running both source + dist_runtime copies at once on the same machine.
    Opt out with MAFIABOT_ALLOW_MULTI=1.
    """
    if os.environ.get("MAFIABOT_ALLOW_MULTI", "").strip().lower() in ("1", "true", "yes", "on"):
        logging.warning(
            "MAFIABOT_ALLOW_MULTI is enabled: concurrent bot processes may corrupt SQLite/JSON state."
        )
        return

    # Use a machine-wide lock location so "source" and "dist_runtime" builds collide.
    lock_override = os.environ.get("MAFIABOT_INSTANCE_LOCK_DIR", "").strip()
    if lock_override:
        lock_path = Path(lock_override) / "bot.instance.lock"
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
        lock_path = Path(base) / "Mafiabot" / "bot.instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            # Check for stale lock.
            existing_txt = ""
            existing_pid: Optional[int] = None
            try:
                existing_txt = lock_path.read_text(encoding="utf-8")[:2000]
                existing = json.loads(existing_txt) if existing_txt else {}
                if isinstance(existing, dict) and "pid" in existing:
                    existing_pid = int(existing["pid"])
            except Exception:
                existing_pid = None

            alive = bool(existing_pid) and _pid_is_alive(int(existing_pid))

            if alive:
                raise RuntimeError(
                    f"Another Mafia Bot instance appears to be running (pid={existing_pid}). "
                    f"Lock file exists: {lock_path}"
                )

            # Stale lock: remove and retry.
            logging.warning(
                "Removing stale single-instance lock at %s (previous pid=%s)",
                lock_path,
                existing_pid,
            )
            try:
                lock_path.unlink(missing_ok=True)  # type: ignore[call-arg]
            except TypeError:
                # Python < 3.8 compat (not expected here, but safe).
                try:
                    if lock_path.exists():
                        lock_path.unlink()
                except Exception:
                    pass
            continue
        except OSError as e:
            raise RuntimeError(
                f"Single-instance lock unavailable at {lock_path}: {e!r}. "
                "Concurrent bot processes can corrupt SQLite/JSON state. "
                "Set MAFIABOT_ALLOW_MULTI=1 only if you accept that risk."
            ) from e

    global _instance_lock_fd
    try:
        from persistence import STATE_DIR

        info = {
            "pid": int(os.getpid()),
            "cwd": os.getcwd(),
            "argv": list(getattr(sys, "argv", [])),
            "ts_ms": int(time.time() * 1000),
            "state_dir": str(STATE_DIR),
        }
        os.write(fd, json.dumps(info, sort_keys=True).encode("utf-8"))
        _instance_lock_fd = fd
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise


def _release_single_instance_lock() -> None:
    """Release the OS file lock acquired at process start (best-effort on shutdown)."""
    global _instance_lock_fd
    if _instance_lock_fd is None:
        return
    try:
        os.close(_instance_lock_fd)
    except Exception:
        pass
    _instance_lock_fd = None
@bot.tree.error
async def _tree_error_handler(interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
    await on_app_command_tree_error(interaction, error)


async def _mafia_tree_interaction_check(interaction: discord.Interaction) -> bool:
    """B6.3: central slash/UI gate — single-guild bot."""
    try:
        if interaction.guild_id is not None and interaction.guild_id != ALLOWED_GUILD_ID:
            await safe_interaction_ephemeral(
                interaction, "This bot is locked to one configured server."
            )
            return False
    except Exception:
        logging.exception("interaction_check failed")
        return False
    return True


bot.tree.interaction_check = _mafia_tree_interaction_check


# --- Gateway stuck watchdog (B6.2, parity with StudyBot single-loop policy) ---
def _reset_gateway_watchdog_session() -> None:
    t = getattr(bot, "_gateway_watchdog_task", None)
    if t is not None and not t.done():
        t.cancel()
    bot._gateway_watchdog_task = None  # type: ignore[attr-defined]
    bot._gateway_had_ready = False  # type: ignore[attr-defined]
    bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
    bot._gateway_session_started_at = time.monotonic()  # type: ignore[attr-defined]
    bot._gateway_last_stuck_log_at = 0.0  # type: ignore[attr-defined]


def _ensure_gateway_watchdog_task() -> None:
    t = getattr(bot, "_gateway_watchdog_task", None)
    if t is not None and not t.done():
        return
    bot._gateway_watchdog_task = asyncio.create_task(_gateway_stuck_watchdog(), name="gateway_stuck_watchdog")  # type: ignore[attr-defined]


async def _gateway_stuck_watchdog() -> None:
    disconnect_sec = float(os.getenv("GATEWAY_STUCK_DISCONNECT_SEC", "900"))
    initial_sec = float(os.getenv("GATEWAY_STUCK_INITIAL_CONNECT_SEC", "1200"))
    interval = max(5.0, float(os.getenv("GATEWAY_STUCK_POLL_SEC", "30")))
    log_every = float(os.getenv("GATEWAY_STUCK_LOG_EVERY_SEC", "300"))
    try:
        while not bot.is_closed():
            await asyncio.sleep(interval)
            if bot.is_closed():
                break
            now = time.monotonic()
            if bot.is_ready():
                bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
                continue

            had_ready = getattr(bot, "_gateway_had_ready", False)
            if had_ready and getattr(bot, "_gateway_disconnect_at", None) is None:
                bot._gateway_disconnect_at = now  # type: ignore[attr-defined]

            disc_at = getattr(bot, "_gateway_disconnect_at", None)
            if had_ready and disc_at is not None:
                stale = now - float(disc_at)
                if stale >= disconnect_sec:
                    logging.error(
                        "Gateway stuck disconnected for %.0fs (>= %.0fs) — closing client for full reconnect.",
                        stale,
                        disconnect_sec,
                    )
                    try:
                        await bot.close()
                    except Exception:
                        logging.debug("gateway watchdog close() failed", exc_info=True)
                    return
                if (
                    log_every > 0
                    and stale >= 60
                    and (now - float(getattr(bot, "_gateway_last_stuck_log_at", 0.0))) >= log_every
                ):
                    bot._gateway_last_stuck_log_at = now  # type: ignore[attr-defined]
                    logging.warning(
                        "Gateway still disconnected (%.0fs elapsed, %.0fs until forced reconnect).",
                        stale,
                        max(0.0, disconnect_sec - stale),
                    )

            if not had_ready:
                boot_stale = now - float(getattr(bot, "_gateway_session_started_at", now))
                if boot_stale >= initial_sec:
                    logging.error(
                        "Gateway never reached READY after %.0fs (>= %.0fs) — closing client.",
                        boot_stale,
                        initial_sec,
                    )
                    try:
                        await bot.close()
                    except Exception:
                        logging.debug("gateway watchdog close() failed", exc_info=True)
                    return
                if (
                    log_every > 0
                    and boot_stale >= 60
                    and (now - float(getattr(bot, "_gateway_last_stuck_log_at", 0.0))) >= log_every
                ):
                    bot._gateway_last_stuck_log_at = now  # type: ignore[attr-defined]
                    logging.warning(
                        "Still waiting for first READY (%.0fs elapsed, %.0fs until forced reconnect).",
                        boot_stale,
                        max(0.0, initial_sec - boot_stale),
                    )
    except asyncio.CancelledError:
        logging.debug("Gateway stuck watchdog cancelled")
        raise


async def _dm_outbox_pump_loop() -> None:
    """Drain SQLite-backed DM queue (B6.1).

    All `db.*` calls run via `asyncio.to_thread` so the synchronous SQLite work
    never stalls the gateway. The owning task is re-spawned by `on_ready`
    whenever the previous one is `done()` (e.g., after a watchdog-driven
    `bot.close()`), so the queue keeps draining across reconnects.
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            db = getattr(bot, "db", None)
            if db:
                await asyncio.to_thread(db.requeue_stale_dm_outbox_sending, stale_after_seconds=300)
                rows = await asyncio.to_thread(db.claim_dm_outbox_batch, limit=25)
                allowed_gid = int(ALLOWED_GUILD_ID) if ALLOWED_GUILD_ID else 0
                for row in rows:
                    row_gid = int(row.get("guild_id") or 0)
                    if allowed_gid and row_gid != allowed_gid:
                        mid = int(row["id"])
                        await asyncio.to_thread(
                            db.retry_dm_outbox_later,
                            mid,
                            error="skipped: foreign guild_id",
                            delay_seconds=3600,
                        )
                        continue
                    mid = int(row["id"])
                    uid = int(row["target_user_id"])
                    content = str(row["content"])
                    try:
                        user = bot.get_user(uid) or await bot.fetch_user(uid)
                        await user.send(content)
                        await asyncio.to_thread(db.mark_dm_outbox_sent, mid)
                    except discord.HTTPException as e:
                        delay = 120
                        if e.status == 429:
                            delay = min(600, int(getattr(e, "retry_after", 60) or 60) + 5)
                        await asyncio.to_thread(db.retry_dm_outbox_later, mid, error=str(e), delay_seconds=delay)
                    except Exception as e:
                        await asyncio.to_thread(db.retry_dm_outbox_later, mid, error=str(e), delay_seconds=90)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("dm_outbox pump iteration failed")
        await asyncio.sleep(12)


def _ensure_dm_outbox_pump_task() -> None:
    """Spawn the DM-outbox pump task if it isn't currently running."""
    t = getattr(bot, "_mafia_dm_outbox_task", None)
    if t is not None and not t.done():
        return
    bot._mafia_dm_outbox_task = bot.loop.create_task(  # type: ignore[attr-defined]
        _dm_outbox_pump_loop(), name="dm_outbox_pump"
    )

# ==========================================
# GATEWAY / SESSION OBSERVABILITY
# ==========================================
# discord.py reconnects the websocket automatically for many failures; these hooks help ops logs.
@bot.event
async def on_disconnect() -> None:
    logging.warning("Discord gateway disconnected — client will attempt to reconnect.")
    if getattr(bot, "_gateway_had_ready", False):
        bot._gateway_disconnect_at = time.monotonic()  # type: ignore[attr-defined]


@bot.event
async def on_resumed() -> None:
    logging.info("Discord gateway resumed (same session where supported).")

@bot.event
async def on_ready() -> None:
    # Count ready cycles: first is cold connect; later ones are usually gateway reconnects.
    bot._mafia_ready_count = int(getattr(bot, "_mafia_ready_count", 0)) + 1  # type: ignore[attr-defined]
    _is_reconnect = bot._mafia_ready_count > 1  # type: ignore[attr-defined]

    # Initialize SQLite DB (leaderboards/history) early; non-fatal if it fails.
    # Kept near the top of on_ready so the path under state/mafiabot.db is obvious
    # in source (smoke-test asserts this) and so a slow tree-sync can't block it.
    if not getattr(bot, "db", None):
        try:
            from persistence import migrate_legacy_sqlite_db, sqlite_db_path

            migrate_legacy_sqlite_db()
            db_path = str(sqlite_db_path())
            bot.db = Database(db_path)  # type: ignore[attr-defined]
            bot.db.initialize()  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to initialize SQLite DB (leaderboards disabled).")
            if _strict_config_enabled():
                raise

    try:
        guilds = [f"{g.name} ({g.id})" for g in bot.guilds]
    except Exception:
        guilds = []
    logging.info(
        "Logged in as %s (id=%s). Connected guilds: %s [ready #%s%s]",
        bot.user,
        getattr(bot.user, "id", "unknown"),
        ", ".join(guilds) or "none",
        int(bot._mafia_ready_count),  # type: ignore[attr-defined]
        "; reconnect" if _is_reconnect else "",
    )

    guild_early = bot.get_guild(ALLOWED_GUILD_ID)
    if guild_early is not None:
        from game_recovery import maybe_finish_deferred_cleanup

        existing_early = active_games.get(ALLOWED_GUILD_ID)
        if existing_early is not None:
            await maybe_finish_deferred_cleanup(existing_early, guild_early)

    if int(bot._mafia_ready_count) == 1:  # type: ignore[attr-defined]
        try:
            from config import validate_deployment_config

            if _strict_config_enabled():
                validate_deployment_config(strict=True)
            else:
                for msg in validate_deployment_config(strict=False):
                    logging.warning("Deployment config: %s", msg)
        except RuntimeError:
            raise
        except Exception:
            logging.exception("Deployment config validation failed")

    # Best-effort: ensure hybrid/slash commands are synced.
    # Guild sync is fast; global sync enables DM usage but may take time to propagate.
    # Skip full sync on gateway reconnects to avoid Discord rate limits / unnecessary churn.
    _force_sync = os.environ.get("MAFIA_FORCE_COMMAND_SYNC", "").strip().lower() in ("1", "true", "yes", "on")
    if int(bot._mafia_ready_count) == 1 or _force_sync:  # type: ignore[attr-defined]
        guild_synced_count: Optional[int] = None
        global_synced_count: Optional[int] = None
        try:
            guild_synced = await bot.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
            guild_synced_count = len(guild_synced)
        except Exception:
            logging.exception("Failed to sync app commands for allowed guild.")
        try:
            global_synced = await bot.tree.sync()
            global_synced_count = len(global_synced)
        except Exception:
            logging.exception("Failed to sync global app commands.")
        logging.info(
            "Tree sync complete: guild=%s global=%s",
            guild_synced_count if guild_synced_count is not None else "failed",
            global_synced_count if global_synced_count is not None else "failed",
        )
    else:
        logging.info(
            "Skipping slash command tree sync (ready session #%s). Set MAFIA_FORCE_COMMAND_SYNC=1 to force.",
            int(bot._mafia_ready_count),  # type: ignore[attr-defined]
        )

    bot._gateway_had_ready = True  # type: ignore[attr-defined]
    bot._gateway_disconnect_at = None  # type: ignore[attr-defined]
    _ensure_gateway_watchdog_task()

    _ensure_dm_outbox_pump_task()

    # Best-effort: rebuild absent or empty JSON stats mirror from SQLite (Low #47).
    if int(bot._mafia_ready_count) == 1 and getattr(bot, "db", None) and ALLOWED_GUILD_ID:  # type: ignore[attr-defined]
        try:
            from persistence import guild_stats_path, load_stats
            from stats_mirror_repair import repair_guild_json_mirror_from_sqlite

            gid = int(ALLOWED_GUILD_ID)
            stats_path = guild_stats_path(gid)
            needs_repair = not stats_path.is_file()
            if not needs_repair:
                try:
                    blob = load_stats(gid) or {}
                    players = blob.get("players") if isinstance(blob, dict) else None
                    needs_repair = not isinstance(players, dict) or len(players) == 0
                except Exception:
                    needs_repair = True
            if needs_repair:
                repair_guild_json_mirror_from_sqlite(
                    bot.db,  # type: ignore[attr-defined]
                    guild_id=gid,
                )
        except Exception:
            logging.exception(
                "stats JSON mirror repair on cold boot failed guild_id=%s",
                ALLOWED_GUILD_ID,
            )

    # Refresh live stats board embed(s) after cold start only (not every gateway reconnect).
    if int(bot._mafia_ready_count) == 1 and getattr(bot, "db", None) and ALLOWED_GUILD_ID:  # type: ignore[attr-defined]
        try:
            from bot_app.stats_board import refresh_guild_stats_board

            ok = await refresh_guild_stats_board(bot=bot, guild_id=int(ALLOWED_GUILD_ID))
            if ok:
                logging.info("Refreshed guild stats board on ready guild_id=%s", ALLOWED_GUILD_ID)
        except Exception:
            logging.exception("Failed to refresh stats board on ready guild_id=%s", ALLOWED_GUILD_ID)

    # Attempt to restore persisted game state for the allowed guild.
    # IMPORTANT: only restore on the first on_ready of this process. Subsequent
    # on_ready events come from gateway IDENTIFY/reconnect cycles and must NOT
    # overwrite the in-memory Game, or we get split-brain Game objects (in-flight
    # vote/duel coroutines reference the old object while new commands hit a new
    # one) and the tribunal-defense resume task can spawn a second judgment.
    guild = bot.get_guild(ALLOWED_GUILD_ID)
    if guild and int(bot._mafia_ready_count) == 1:  # type: ignore[attr-defined]
        from game import Game, _is_empty_game_placeholder
        from persistence import is_stale_ended_state, load_state

        from game_recovery import commit_pending_endgame_before_state_delete

        data = load_state(ALLOWED_GUILD_ID)
        existing = active_games.get(ALLOWED_GUILD_ID)
        game: Optional[Game] = None

        if existing is not None and getattr(existing, "cleanup_pending", False):
            from game_recovery import maybe_finish_deferred_cleanup

            await maybe_finish_deferred_cleanup(existing, guild)
            existing = active_games.get(ALLOWED_GUILD_ID)

        if data and is_stale_ended_state(data):
            try:
                await asyncio.to_thread(
                    commit_pending_endgame_before_state_delete,
                    ALLOWED_GUILD_ID,
                )
                stale = Game.from_persisted(data)
                active_games[ALLOWED_GUILD_ID] = stale
                await stale.rehydrate_members(guild)
                await stale.reset(guild)
            except Exception:
                logging.exception(
                    "Stale ended game JSON cleanup failed guild_id=%s", ALLOWED_GUILD_ID
                )
            data = None
            existing = active_games.get(ALLOWED_GUILD_ID)

        if data and (existing is None or _is_empty_game_placeholder(existing)):
            try:
                game = Game.from_persisted(data)
                active_games[ALLOWED_GUILD_ID] = game
                await game.rehydrate_members(guild)
                game._rehydrate_pending = False
                logging.info(f"Restored persisted game state for guild {ALLOWED_GUILD_ID}.")
            except Exception:
                logging.exception("Failed to restore persisted game for guild %s", ALLOWED_GUILD_ID)
                game = None
        elif existing is not None and not _is_empty_game_placeholder(existing):
            game = existing
            if data:
                from persist_schema import coerce_bool

                disk_key = str(data.get("game_key") or "").strip()
                mem_key = str(getattr(existing, "game_key", "") or "").strip()
                if (
                    disk_key
                    and mem_key
                    and disk_key != mem_key
                    and coerce_bool(data.get("in_progress", False))
                ):
                    from game_recovery import game_has_inflight_state

                    if game_has_inflight_state(existing):
                        logging.warning(
                            "Skipping disk replace — in-flight state on memory game "
                            "guild_id=%s disk_key=%s mem_key=%s",
                            ALLOWED_GUILD_ID,
                            disk_key,
                            mem_key,
                        )
                    else:
                        logging.warning(
                            "Replacing in-memory game with disk (game_key mismatch) guild_id=%s",
                            ALLOWED_GUILD_ID,
                        )
                        game = Game.from_persisted(data)
                        active_games[ALLOWED_GUILD_ID] = game
                        await game.rehydrate_members(guild)
                        game._rehydrate_pending = False
                else:
                    logging.info(
                        "Skipping cold-start restore: in-memory game already present guild_id=%s",
                        ALLOWED_GUILD_ID,
                    )

        if game is not None:
            try:
                await game.resume_pending_death_announces(guild)
            except Exception:
                logging.exception("resume_pending_death_announces failed guild_id=%s", ALLOWED_GUILD_ID)

            if not game.stats_committed and game.player_roles:
                try:
                    from game_recovery import _pending_endgame_meta

                    pending = _pending_endgame_meta(int(ALLOWED_GUILD_ID))
                    if isinstance(pending, dict) and pending.get("outcome"):
                        pk = str(pending.get("game_key") or "").strip()
                        gk = str(getattr(game, "game_key", "") or "").strip()
                        if pk and gk and pk == gk:
                            raw_living = pending.get("living_ids")
                            living_retry: List[int] = []
                            if isinstance(raw_living, (list, tuple)):
                                for x in raw_living:
                                    try:
                                        living_retry.append(int(x))
                                    except (TypeError, ValueError):
                                        continue
                            if not living_retry:
                                from game import Game as GameCls

                                living_retry = GameCls.living_ids_excluding_graveyard(game)
                            await game.commit_endgame_stats_async(
                                outcome=str(pending["outcome"]),
                                living_ids=living_retry,
                            )
                            if game.stats_committed:
                                logging.info(
                                    "Retried pending endgame stats commit on cold start guild_id=%s",
                                    ALLOWED_GUILD_ID,
                                )
                                fresh = load_state(int(ALLOWED_GUILD_ID))
                                if isinstance(fresh, dict):
                                    from persist_schema import coerce_bool

                                    game.stats_committed = coerce_bool(
                                        fresh.get("stats_committed", False)
                                    )
                                try:
                                    from bot_app.stats_board import schedule_stats_board_refresh

                                    schedule_stats_board_refresh(
                                        bot=bot,
                                        guild_id=int(ALLOWED_GUILD_ID),
                                    )
                                except Exception:
                                    logging.exception(
                                        "schedule_stats_board_refresh after pending commit "
                                        "failed guild_id=%s",
                                        ALLOWED_GUILD_ID,
                                    )
                except Exception:
                    logging.exception(
                        "Pending endgame stats retry on cold start failed guild_id=%s",
                        ALLOWED_GUILD_ID,
                    )

            # Best-effort repair: ensure Playing/Lockdown roles are applied consistently after restart.
            playing_role = guild.get_role(PLAYING_ROLE_ID)
            lockdown_role = guild.get_role(game.lockdown_role_id) if getattr(game, "lockdown_role_id", None) else None
            if game.in_progress and playing_role:
                for p in list(game.players):
                    try:
                        if playing_role not in p.roles:
                            await p.add_roles(playing_role)
                    except discord.HTTPException as e:
                        logging.warning(
                            "Playing role repair failed guild_id=%s member_id=%s role_id=%s: %s",
                            guild.id,
                            getattr(p, "id", None),
                            getattr(playing_role, "id", None),
                            e,
                        )
                    await asyncio.sleep(0.05)
            if game.in_progress and lockdown_role:
                for p in list(game.players):
                    try:
                        if any(r.id == GAME_OVERSEER_ROLE_ID for r in p.roles) or p.guild_permissions.administrator:
                            continue
                        if lockdown_role not in p.roles:
                            await p.add_roles(lockdown_role)
                    except discord.HTTPException as e:
                        logging.warning(
                            "Lockdown role repair failed guild_id=%s member_id=%s role_id=%s: %s",
                            guild.id,
                            getattr(p, "id", None),
                            getattr(lockdown_role, "id", None),
                            e,
                        )
                    await asyncio.sleep(0.05)

            resume_defense = False
            resume_judgment = False
            resume_last_words = False
            tribunal_active = game.in_progress and getattr(game, "vote_in_progress", False)
            if (
                tribunal_active
                and getattr(game, "game_channel_id", None)
                and game_text_channel(game, guild) is None
            ):
                await _bailout_tribunal_resume(
                    game,
                    guild,
                    notice="⚠️ **Trial cancelled:** game channel missing after restart.",
                )
            elif tribunal_active:
                t_sub = getattr(game, "tribunal_subphase", None)
                t_deadline = getattr(game, "tribunal_defense_deadline_utc", None)
                if (
                    game.phase == "day"
                    and t_sub == "defense"
                    and t_deadline
                    and getattr(game, "tribunal_defendant_id", None)
                ):
                    dtp = _parse_iso_utc(t_deadline)
                    if dtp:
                        rem_sec = (dtp - datetime.now(timezone.utc)).total_seconds()
                        if TRIBUNAL_RESUME_MIN_SECONDS <= rem_sec <= 7200:
                            resume_defense = True
                            _spawn_tribunal_resume_once(
                                bot,
                                "defense",
                                _resume_tribunal_defense_after_restart(guild, rem_sec),
                            )

                j_deadline = getattr(game, "tribunal_judgment_deadline_utc", None)
                if (
                    not resume_defense
                    and game.phase == "day"
                    and t_sub == "judgment"
                    and j_deadline
                    and getattr(game, "tribunal_judgment_message_id", None)
                    and getattr(game, "tribunal_defendant_id", None)
                ):
                    jtp = _parse_iso_utc(j_deadline)
                    if jtp:
                        rem_j = (jtp - datetime.now(timezone.utc)).total_seconds()
                        if TRIBUNAL_RESUME_MIN_SECONDS <= rem_j <= 7200:
                            resume_judgment = True
                            _spawn_tribunal_resume_once(
                                bot,
                                "judgment",
                                _resume_tribunal_judgment_after_restart(guild, rem_j),
                            )

                lw_deadline = getattr(game, "tribunal_last_words_deadline_utc", None)
                if (
                    not resume_defense
                    and not resume_judgment
                    and t_sub == "last_words"
                    and lw_deadline
                    and getattr(game, "tribunal_defendant_id", None)
                ):
                    lwtp = _parse_iso_utc(lw_deadline)
                    if lwtp:
                        rem_lw = (lwtp - datetime.now(timezone.utc)).total_seconds()
                        if rem_lw < 0:
                            rem_lw = 0.0
                        if TRIBUNAL_RESUME_MIN_SECONDS <= rem_lw <= 7200:
                            resume_last_words = True
                            _spawn_tribunal_resume_once(
                                bot,
                                "last_words",
                                _resume_tribunal_last_words_after_restart(guild, rem_lw),
                            )

            if (
                not resume_defense
                and not resume_judgment
                and not resume_last_words
                and game.in_progress
                and game.phase == "day"
                and game.vote_in_progress
                and getattr(game, "tribunal_verdict_committed", False)
                and int(getattr(game, "tribunal_guilty_vote_count", 0) or 0)
                > int(getattr(game, "tribunal_innocent_vote_count", 0) or 0)
                and getattr(game, "tribunal_defendant_id", None)
            ):
                resume_last_words = True
                rem_committed = 0.0
                lw_raw = getattr(game, "tribunal_last_words_deadline_utc", None)
                lwtp_c = _parse_iso_utc(lw_raw) if lw_raw else None
                if lwtp_c:
                    rem_committed = max(0.0, (lwtp_c - datetime.now(timezone.utc)).total_seconds())
                if game.tribunal_subphase != "last_words":
                    game.tribunal_subphase = "last_words"
                    try:
                        await game.persist_flush()
                    except Exception:
                        logging.exception(
                            "persist_flush before last_words resume failed guild_id=%s",
                            ALLOWED_GUILD_ID,
                        )
                _spawn_tribunal_resume_once(
                    bot,
                    "last_words_committed",
                    _resume_tribunal_last_words_after_restart(guild, rem_committed),
                )

            # Best-effort repair: unstick day VC permissions unless we're mid-tribunal (resume will continue).
            if game.in_progress and game.phase == "day" and game.day_vc_id and game.alive_role_id:
                if not game.vote_in_progress:
                    day_vc = guild.get_channel(game.day_vc_id)
                    alive_role = guild.get_role(game.alive_role_id)
                    if day_vc and alive_role:
                        try:
                            await day_vc.set_permissions(alive_role, connect=True, speak=True)
                        except discord.HTTPException:
                            pass

            # Crash recovery: abort tribunal if we cannot resume mid-trial.
            if (
                not resume_defense
                and not resume_judgment
                and not resume_last_words
                and game.in_progress
                and game.phase == "day"
                and (getattr(game, "tribunal_muted", False) or getattr(game, "tribunal_defendant_id", None))
            ):
                stand_role = guild.get_role(game.stand_role_id) if getattr(game, "stand_role_id", None) else None
                defendant_id = getattr(game, "tribunal_defendant_id", None)
                ref_deadline_for_msg = getattr(game, "tribunal_judgment_deadline_utc", None) or getattr(
                    game, "tribunal_defense_deadline_utc", None
                )
                # Non-design fix: refund votes_today when aborting mid-trial after someone
                # reached the stand (daily trial was consumed at nomination end).
                if defendant_id is not None:
                    _refund_tribunal_daily_if_consumed(game)
                if defendant_id and stand_role:
                    try:
                        m = await game.get_member_safe(guild, int(defendant_id))
                        if m and stand_role in m.roles:
                            await m.remove_roles(stand_role)
                    except discord.HTTPException:
                        pass

                if game.day_vc_id and game.alive_role_id:
                    day_vc2 = guild.get_channel(game.day_vc_id)
                    alive_role2 = guild.get_role(game.alive_role_id)
                    if day_vc2 and alive_role2:
                        try:
                            await day_vc2.set_permissions(alive_role2, connect=True, speak=True)
                        except discord.HTTPException:
                            pass

                game.tribunal_state.clear_persisted()
                try:
                    await game.persist_flush()
                except Exception:
                    pass
                gc = bot.get_channel(game.game_channel_id) if game.game_channel_id else None
                if isinstance(gc, discord.TextChannel):
                    dtp2 = _parse_iso_utc(ref_deadline_for_msg) if ref_deadline_for_msg else None
                    rem2 = (dtp2 - datetime.now(timezone.utc)).total_seconds() if dtp2 else -99999.0
                    if rem2 < 0:
                        msg = "⚠️ **Trial reset:** bot restarted — tribunal timer already overdue on wall-clock."
                    elif rem2 < TRIBUNAL_RESUME_MIN_SECONDS:
                        msg = (
                            "⚠️ **Trial reset:** bot restarted with insufficient time remaining "
                            f"to resume safely (<{TRIBUNAL_RESUME_MIN_SECONDS}s)."
                        )
                    else:
                        msg = "⚠️ **Trial reset:** bot restarted during tribunal cleanup — could not resume."
                    if not await safe_channel_send(gc, msg):
                        logging.warning(
                            "Tribunal restart announcement failed guild_id=%s channel_id=%s",
                            guild.id,
                            getattr(gc, "id", None),
                        )

    elif guild and int(bot._mafia_ready_count) > 1:  # type: ignore[attr-defined]
        existing = active_games.get(ALLOWED_GUILD_ID)
        if existing is not None:
            from game_recovery import maybe_finish_deferred_cleanup

            if await maybe_finish_deferred_cleanup(existing, guild):
                logging.info(
                    "Deferred cleanup reset on gateway reconnect guild_id=%s",
                    ALLOWED_GUILD_ID,
                )

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    await on_command_error_handler(ctx, error)

# Note: no custom on_message handler — discord.py's default invokes
# Bot.process_commands, which already short-circuits for bot authors.


# ==========================================
# GUILD LOCKING
# ==========================================
@bot.check
async def enforce_allowed_guild_check(ctx: commands.Context) -> bool:
    return await enforce_allowed_guild(ctx, allowed_guild_id=ALLOWED_GUILD_ID)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    if guild.id != ALLOWED_GUILD_ID:
        logging.warning(f"Joined unauthorized guild {guild.id}; leaving.")
        try:
            await guild.leave()
        except discord.HTTPException:
            pass


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild.id != ALLOWED_GUILD_ID:
        return
    game = active_games.get(member.guild.id)
    if game is None or not game.in_progress:
        return
    try:
        await game.reconcile_member_discord_roles(member.guild, member)
    except Exception:
        logging.exception(
            "reconcile_member_discord_roles failed on_member_join guild_id=%s user_id=%s",
            member.guild.id,
            member.id,
        )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if after.guild.id != ALLOWED_GUILD_ID:
        return
    if before.roles == after.roles:
        return
    game = active_games.get(after.guild.id)
    if game is None or not game.in_progress:
        return
    try:
        await game.reconcile_member_discord_roles(after.guild, after)
    except Exception:
        logging.exception(
            "reconcile_member_discord_roles failed on_member_update guild_id=%s user_id=%s",
            after.guild.id,
            after.id,
        )


# ==========================================
# RUN BOT
# ==========================================
async def _connect_forever() -> None:
    """B1: supervised gateway session with backoff on fatal disconnect."""
    _reset_gateway_watchdog_session()
    token = os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable not set. Add it to your .env or environment variables before running."
        )
    backoff_min = float(os.getenv("MAFIABOT_RECONNECT_BACKOFF_MIN_SEC", "5"))
    backoff_max = float(os.getenv("MAFIABOT_RECONNECT_BACKOFF_MAX_SEC", "300"))
    backoff = backoff_min
    while True:
        try:
            await bot.start(token, reconnect=True)
            logging.info("Gateway session ended normally — restarting supervised session.")
            backoff = backoff_min
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                logging.debug("bot.close() after normal session end failed", exc_info=True)
            continue
        except discord.LoginFailure:
            logging.critical("Discord token rejected — fix credentials.")
            raise SystemExit(1)
        except KeyboardInterrupt:
            logging.info("Shutdown requested.")
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                pass
            break
        except Exception:
            logging.exception("Disconnected or fatal error — retrying in %.1fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, backoff_max)
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                logging.debug("bot.close() during reconnect backoff failed", exc_info=True)


def _require_discord_token() -> None:
    if os.getenv("DISCORD_TOKEN") or os.getenv("DISCORD_BOT_TOKEN"):
        return
    raise RuntimeError(
        "DISCORD_TOKEN environment variable not set. Add it to your .env or environment variables before running."
    )




# Import after this module is fully initialized (tribunal imports enforce_allowed_guild_check above).
from bot_app.tribunal import (  # noqa: E402
    _bailout_tribunal_resume,
    _parse_iso_utc,
    _refund_tribunal_daily_if_consumed,
    _resume_tribunal_defense_after_restart,
    _resume_tribunal_judgment_after_restart,
    _resume_tribunal_last_words_after_restart,
)

