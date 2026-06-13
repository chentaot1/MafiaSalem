"""GM / lobby / day-phase overseer commands."""

from __future__ import annotations

from io import BytesIO

from bot_app.imports import *  # noqa: F403
from night_guilt import tally_guilt_and_jester_deaths


async def _tally_guilt_and_jester_deaths(
    game: Game,
    guild: discord.Guild,
    deaths: Set[int],
    night_kill_deaths: Set[int],
) -> tuple[List[int], List[int]]:
    """Backward-compatible wrapper; implementation in ``night_guilt``."""
    return await tally_guilt_and_jester_deaths(game, guild, deaths, night_kill_deaths)
from night_resume import (
    coerce_snap_id_list as _coerce_snap_id_list,
    night_kill_deaths_from_snap,
    normalize_night_completion_snapshot,
    parse_night_resume_state as _parse_night_resume_state,
)
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay
from bot_app.bootstrap import enforce_allowed_guild_check
from bot_app.shared import (
    _build_leaderboard_embed,
    _build_server_stats_embed,
    build_server_stats_export_bytes,
    safe_reply,
)
from bot_app.stats_board import (
    refresh_guild_stats_board,
    setup_stats_board_in_channel,
)
from bot_app.ui import LeaderboardView, ServerStatsView, WillModal, WillView


def _ga_bind_slot(game: Game, state: Dict) -> Optional[int]:
    bid = state.get("ga_target_id")
    try:
        bint = int(bid) if bid is not None else None
    except (TypeError, ValueError):
        bint = None
    if bint is None:
        return None
    return game.player_slots.get(bint)


async def _post_game_start_to_player_private_channel(
    game: Game,
    guild: discord.Guild,
    player: discord.Member,
    role: str,
) -> None:
    """
    Ping the player in their mapped private channel with role + command hints.
    Complements DM / outbox delivery; no-op if no channel is configured or send fails.
    """
    ch_id = private_text_channel_id_for_user(player.id)
    if ch_id is None:
        return
    ch = guild.get_channel(ch_id)
    if not isinstance(ch, discord.TextChannel):
        return
    me = guild.me
    if me is not None:
        perms = ch.permissions_for(me)
        if not (perms.view_channel and perms.send_messages):
            logging.warning(
                "Game start: cannot send to private channel guild_id=%s channel_id=%s (missing perms)",
                guild.id,
                ch_id,
            )
            return

    state = game.role_states.get(player.id, {}) or {}
    lines: List[str] = [
        f"{player.mention} **The game has started.**",
        "",
        f"Your role: **{role}**",
        "Use `!myrole` anytime for the full role card and abilities.",
    ]
    night_hint = game._get_night_prompt(player, role)
    if night_hint:
        lines.extend(["", "**When night begins, your action:**", night_hint])

    if role in ALL_MAFIA_ROLES and game.mafia_tc_id:
        mtc = guild.get_channel(game.mafia_tc_id)
        if isinstance(mtc, discord.TextChannel):
            lines.extend(["", f"**Mafia chat:** {mtc.mention} — coordinate with your team there."])

    exe_name: Optional[str] = None
    if role == "Executioner" and state.get("exe_target"):
        target_user = guild.get_member(int(state["exe_target"]))
        if target_user:
            exe_name = target_user.display_name
    bind_slot = _ga_bind_slot(game, state) if role == "Guardian Angel" else None
    extra = role_start_private_channel_lines(role, bind_slot=bind_slot, exe_target_display=exe_name)
    if extra:
        lines.append("")
        lines.extend(extra)

    try:
        await ch.send("\n".join(lines))
    except discord.HTTPException as e:
        logging.warning(
            "Game start: failed to post to player private channel guild_id=%s channel_id=%s user_id=%s: %s",
            guild.id,
            ch_id,
            player.id,
            e,
        )


def _clear_night_resolve_checkpoints(game: Game) -> None:
    """Drop crash-resume markers once night→day (or endgame) completed."""
    game.night_completion_snapshot = None
    game.psychic_visions_delivered_this_night = False


def _snap_bool(snap: object, key: str) -> bool:
    from persist_schema import coerce_bool

    if not isinstance(snap, dict):
        return False
    return coerce_bool(snap.get(key))


async def _deliver_night_feedback_once(
    game: Game,
    guild: discord.Guild,
    blocked: Collection[int],
    *,
    deaths: Set[int],
    healed_by_map: Dict[int, int],
) -> None:
    """Send night feedback DMs once and persist ``night_feedback_sent`` on the snapshot."""
    from engine.night import send_night_feedback

    snap = normalize_night_completion_snapshot(
        getattr(game, "night_completion_snapshot", None)
    )
    if snap is not None and _snap_bool(snap, "night_feedback_sent"):
        return
    await send_night_feedback(
        game,
        list(blocked),
        guild,
        deaths=deaths,
        healed_by_map=healed_by_map,
    )
    norm = snap or {
        "day": int(game.day_number),
        "game_key": game.game_key,
    }
    norm["night_feedback_sent"] = True
    game.night_completion_snapshot = norm
    await game.persist_flush()


def _sync_psychic_delivered_from_snap(game: Game) -> bool:
    """If snapshot says visions were sent, mirror flag onto ``Game`` (crash resume)."""
    snap = normalize_night_completion_snapshot(
        getattr(game, "night_completion_snapshot", None)
    )
    if snap is not None and _snap_bool(snap, "psychic_visions_delivered"):
        game.psychic_visions_delivered_this_night = True
        return True
    return bool(getattr(game, "psychic_visions_delivered_this_night", False))


async def _send_pirate_personal_wins_if_needed(game: Game, guild: discord.Guild) -> None:
    from personal_win_notify import personal_win_condition_met, send_personal_win_dm_if_needed

    for pid, role in list(game.player_roles.items()):
        if role == "Pirate" and personal_win_condition_met(game, int(pid), "Pirate"):
            await send_personal_win_dm_if_needed(game, guild, int(pid))


async def _resolve_night_post_pipeline(
    game: Game,
    ctx: commands.Context,
    *,
    deaths: Set[int],
    blocked: Collection[int],
    guilty_vigs: Collection[int],
    jester_haunts: Collection[int],
) -> None:
    """Death announcements, Psychic visions, win check, day start — shared by normal resolve and crash resume."""
    gv = {int(x) for x in guilty_vigs}
    jh = {int(x) for x in jester_haunts}

    if not deaths:
        if not _snap_bool(getattr(game, "night_completion_snapshot", None), "peaceful_night_announced"):
            await post_game_channel(game, ctx.guild, tos_msg.peaceful_night())
            from night_resume import normalize_night_completion_snapshot

            snap = normalize_night_completion_snapshot(
                getattr(game, "night_completion_snapshot", None)
            ) or {
                "day": int(game.day_number),
                "game_key": game.game_key,
            }
            snap["peaceful_night_announced"] = True
            game.night_completion_snapshot = snap
            try:
                await game.persist_flush()
            except Exception:
                logging.exception(
                    "persist_flush failed after peaceful_night marker guild_id=%s",
                    game.guild_id,
                )
    else:
        ordered_deaths = sorted(
            (int(x) for x in deaths if x is not None),
            key=lambda pid: game.player_slots.get(pid, 9999),
        )
        for p_id in ordered_deaths:
            player = await game.get_member_safe(ctx.guild, p_id)
            if p_id in jh:
                cause = "haunt"
                custom = f"👻 The Jester's spirit has claimed its revenge! **<@{p_id}>** was found dead."
            elif p_id in gv:
                cause = "guilt"
                custom = f"Overcome with guilt, <@{p_id}> took their own life."
            else:
                cause = game.night_death_causes.get(p_id, "night_kill")
                custom = None

            if player:
                await game.process_death(ctx, player, cause, custom_message=custom)
            else:
                await game.process_death_by_id(ctx, ctx.guild, p_id, cause, custom_message=custom)
            if p_id not in jh and p_id not in gv:
                game.night_death_causes.pop(p_id, None)

    await game.sync_living_players(ctx.guild)
    if not _sync_psychic_delivered_from_snap(game):
        await deliver_psychic_visions(game, ctx.guild, blocked)
        if game.in_progress and not getattr(game, "ending", False):
            snap_psy = normalize_night_completion_snapshot(
                getattr(game, "night_completion_snapshot", None)
            ) or {
                "day": int(game.day_number),
                "game_key": game.game_key,
            }
            snap_psy["psychic_visions_delivered"] = True
            game.night_completion_snapshot = snap_psy
            persisted = False
            for attempt in range(3):
                try:
                    await game.persist_flush()
                    persisted = True
                    break
                except Exception:
                    logging.exception(
                        "persist_flush failed after psychic visions guild_id=%s attempt=%s",
                        game.guild_id,
                        attempt + 1,
                    )
            if persisted:
                game.psychic_visions_delivered_this_night = True
            else:
                logging.error(
                    "Psychic visions delivered but not persisted; restart may resend guild_id=%s",
                    game.guild_id,
                )
        else:
            game.psychic_visions_delivered_this_night = True

    if await game.check_win_conditions():
        _clear_night_resolve_checkpoints(game)
        return
    # Clear before start_day awaits so day commands (!vote, !reveal) are not blocked
    # while phase is already "day" but resolve has not finished its finally block.
    game.resolving = False
    await game.start_day(ctx)
    clear_attacked_tonight_reasons(game)
    _clear_night_resolve_checkpoints(game)


# ==========================================
# SLASH: LEADERBOARD
# ==========================================
@bot.tree.command(name="leaderboard", description="Show leaderboards for this server.")
async def leaderboard_slash(interaction: discord.Interaction) -> None:
    # Explicit allowed-guild enforcement (prefix @bot.check does not apply to app commands).
    if not interaction.guild or interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("🛑 This bot is locked to a different server.", ephemeral=True)

    db = getattr(bot, "db", None)
    if not db:
        return await interaction.response.send_message("Leaderboards are not available (DB not initialized).", ephemeral=True)

    # Defer immediately to avoid Discord's ~3s response deadline.
    await interaction.response.defer()
    embed = await _build_leaderboard_embed(db=db, guild_id=interaction.guild.id, page="total")
    view = LeaderboardView(invoker_id=interaction.user.id, guild_id=interaction.guild.id, db=db)
    msg = await interaction.followup.send(embed=embed, view=view, wait=True)
    view.message = msg


@bot.tree.command(name="serverstats", description="Server-wide winrates, roles, outcomes, and death stats.")
async def serverstats_slash(interaction: discord.Interaction) -> None:
    if not interaction.guild or interaction.guild.id != ALLOWED_GUILD_ID:
        return await interaction.response.send_message("🛑 This bot is locked to a different server.", ephemeral=True)

    db = getattr(bot, "db", None)
    if not db:
        return await interaction.response.send_message("Server stats are not available (DB not initialized).", ephemeral=True)

    await interaction.response.defer()
    embed = await _build_server_stats_embed(db=db, guild_id=interaction.guild.id, page="overview")
    view = ServerStatsView(invoker_id=interaction.user.id, guild_id=interaction.guild.id, db=db)
    msg = await interaction.followup.send(embed=embed, view=view, wait=True)
    view.message = msg


@bot.command(name="setstatschannel")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def setstatschannel(ctx: commands.Context) -> None:
    """GM: post a live auto-updating server stats embed in this channel."""
    if not isinstance(ctx.channel, discord.TextChannel):
        return await safe_reply(ctx, "Run this in a text channel.")
    ok, msg = await setup_stats_board_in_channel(bot=bot, guild=ctx.guild, channel=ctx.channel)
    await safe_reply(ctx, msg if ok else f"🛑 {msg}")


@bot.command(name="clearstatschannel")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def clearstatschannel(ctx: commands.Context) -> None:
    """GM: stop auto-updating the stats board (does not delete the message)."""
    db = getattr(bot, "db", None)
    if not db:
        return await safe_reply(ctx, "SQLite DB not initialized.")
    await asyncio.to_thread(db.delete_guild_stats_board, guild_id=ctx.guild.id)
    await safe_reply(ctx, "✅ Stats board unlinked. The old message will no longer refresh.")


@bot.command(name="bothealth")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def bothealth(ctx: commands.Context) -> None:
    """GM: quick single-server health snapshot (DB, game, cleanup)."""
    from config import validate_deployment_config
    from persistence import guild_stats_path, sqlite_db_path

    lines: list[str] = []
    db = getattr(bot, "db", None)
    db_path = sqlite_db_path()
    if db is not None:
        lines.append(f"SQLite: ok ({db_path.name})")
    elif db_path.is_file():
        lines.append(f"SQLite: file exists but bot.db not bound ({db_path.name})")
    else:
        lines.append(f"SQLite: MISSING ({db_path.name})")

    stats_json = guild_stats_path(ctx.guild.id)
    lines.append(f"Stats JSON: {'present' if stats_json.is_file() else 'absent'} ({stats_json.name})")

    cfg_issues = validate_deployment_config(strict=False)
    if cfg_issues:
        lines.append("Config warnings:")
        lines.extend(f"  • {w}" for w in cfg_issues)
    else:
        lines.append("Config: env looks complete")

    from game import _is_empty_game_placeholder
    from game_recovery import disk_recovery_summary

    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game is None or not game.in_progress:
        lines.append("Game: none in progress")
        disk_hint = disk_recovery_summary(ctx.guild.id)
        if disk_hint:
            lines.append(f"  ⚠ {disk_hint}")
    elif _is_empty_game_placeholder(game):
        lines.append("Game: in-memory placeholder (no active match)")
        disk_hint = disk_recovery_summary(ctx.guild.id)
        if disk_hint:
            lines.append(f"  ⚠ {disk_hint}")
    else:
        lines.append(
            f"Game: in_progress phase={game.phase} day={game.day_number} "
            f"players={len(game.player_roles)} resolving={getattr(game, 'resolving', False)}"
        )
        if getattr(game, "cleanup_pending", False):
            lines.append("  ⚠ cleanup_pending — run reset/cleanup when safe")
        if getattr(game, "vote_in_progress", False):
            lines.append("  ⚠ vote_in_progress — tribunal in flight")
        if getattr(game, "night_completion_snapshot", None):
            lines.append("  ⚠ night_completion_snapshot set — may need !resolve resume")

    lines.append("MC: run `python scripts/mc_preflight.py` after rule changes (see scripts/monte_carlo/README.md)")

    await safe_reply(ctx, "\n".join(lines))


@bot.command(name="refreshstatsboard")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def refreshstatsboard(ctx: commands.Context) -> None:
    """GM: manually refresh the live stats embed now."""
    ok = await refresh_guild_stats_board(bot=bot, guild_id=ctx.guild.id)
    if ok:
        await safe_reply(ctx, "✅ Stats board updated.")
    else:
        await safe_reply(ctx, "🛑 No stats board configured or message missing. Use `!setstatschannel` first.")


@bot.command(name="exportstats")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def exportstats(ctx: commands.Context, *tokens: str) -> None:
    """
    GM-only stats export.

    Default: server summary JSON (same aggregates as ``/serverstats``).
    ``!exportstats mirror``: per-player JSON compatible with ``!importstats``.
    """
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game and game.in_progress:
        return await safe_reply(
            ctx,
            "🛑 Cannot export while a game is **in progress** — finish or `!reset` first "
            "(snapshot may omit the current game).",
        )
    db = getattr(bot, "db", None)
    if not db:
        return await safe_reply(ctx, "SQLite DB not initialized; cannot export.")

    lower = [str(t).strip().lower() for t in tokens]
    if "mirror" in lower:
        from stats_mirror_repair import repair_guild_json_mirror_from_sqlite

        ok = await asyncio.to_thread(
            repair_guild_json_mirror_from_sqlite,
            db,
            guild_id=int(ctx.guild.id),
        )
        if not ok:
            return await safe_reply(ctx, "🛑 Could not build importable mirror from SQLite — check logs.")
        from persistence import load_stats

        mirror = load_stats(ctx.guild.id) or {}
        text = json.dumps(mirror, ensure_ascii=False, indent=2)
        data = text.encode("utf-8")
        fname = f"{ctx.guild.id}.stats.json"
        if len(data) > 7_500_000:
            return await safe_reply(ctx, "Export too large for Discord upload; contact maintainer.")
        file = discord.File(BytesIO(data), filename=fname)
        n_players = len((mirror.get("players") or {}))
        return await safe_reply(
            ctx,
            f"📁 Importable stats mirror (**{n_players}** players). "
            "Use with `!importstats` / `!importstats force confirm stale`.",
            file=file,
        )

    summary = await asyncio.to_thread(db.get_server_stats_summary, guild_id=ctx.guild.id)
    data, fname = build_server_stats_export_bytes(
        guild_id=ctx.guild.id,
        guild_name=getattr(ctx.guild, "name", None),
        summary=summary,
    )
    if len(data) > 7_500_000:
        return await safe_reply(ctx, "Export too large for Discord upload; contact maintainer.")
    file = discord.File(BytesIO(data), filename=fname)
    gc = int(summary.get("games_completed", 0))
    pg = int(summary.get("player_games", 0))
    note = ""
    if gc == 0 and pg > 0:
        note = " (summary only — use `!exportstats mirror` for importable player JSON)"
    await safe_reply(
        ctx,
        f"📁 Server stats summary ({gc} games in DB{note}). "
        "For `!importstats`, use `!exportstats mirror`.",
        file=file,
    )


# ==========================================
# GM / LOBBY COMMANDS
# ==========================================
@bot.command(name='join')
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def join_game_command(ctx: commands.Context) -> None:
    if get_game_by_player_id(ctx.author.id):
        return await safe_reply(ctx,
            "🛑 You are already in an active game in a server! Please finish that game before joining a new one."
        )

    from game_recovery import lobby_join_blocked_reason

    block_reason = await asyncio.to_thread(lobby_join_blocked_reason, ctx.guild.id)
    if block_reason:
        return await safe_reply(ctx, block_reason)

    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game.in_progress:
        return await safe_reply(ctx,"A game is already in progress!")
    async with game._lobby_lock:
        block_reason = await asyncio.to_thread(lobby_join_blocked_reason, ctx.guild.id)
        if block_reason:
            return await safe_reply(ctx, block_reason)
        if active_games.get(ctx.guild.id) is not game:
            return await safe_reply(ctx, "🛑 Lobby state changed — please run `!join` again.")
        await game.ensure_rehydrated(ctx.guild)
        if ctx.author.id in [p.id for p in game.players]:
            return await safe_reply(ctx,"You are already on the waiting list!")
        game.players.append(ctx.author)
        await game.persist_flush()
    await safe_reply(
        ctx,
        f"{ctx.author.mention} joined! Total players: {len(game.players)}.",
        allowed_mentions=discord.AllowedMentions(users=[ctx.author]),
    )


@bot.command(name="leave")
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def leave_game_command(ctx: commands.Context) -> None:
    """
    Leave the join queue (lobby) if a game hasn't started yet.
    """
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game.in_progress:
        return await safe_reply(ctx,"The game has already started; leaving the queue is not available.")

    async with game._lobby_lock:
        if active_games.get(ctx.guild.id) is not game:
            return await safe_reply(ctx, "🛑 Lobby state changed — please run `!leave` again.")
        await game.ensure_rehydrated(ctx.guild)
        if ctx.author.id not in [p.id for p in game.players]:
            return await safe_reply(ctx,"You are not in the join queue. Use `!join` to enter.")
        game.players = [p for p in game.players if p.id != ctx.author.id]
        await game.persist_flush()
    await safe_reply(
        ctx,
        f"{ctx.author.mention} left the queue. Total players: {len(game.players)}.",
        allowed_mentions=discord.AllowedMentions(users=[ctx.author]),
    )


@bot.command(name='players')
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def show_players_command(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        await game.ensure_rehydrated(ctx.guild)
    if not game.players:
        return await safe_reply(ctx,"No one has joined yet. Use `!join` to enter.")

    if game.in_progress:
        await game.sync_living_players(ctx.guild)

    player_mentions = [p.mention for p in game.players]
    living_mentions = [p.mention for p in game.living_players]

    response = f"**Waiting Players ({len(game.players)}):**\n" + ", ".join(player_mentions)
    if game.in_progress and game.living_players:
        response += f"\n\n**Living Players ({len(game.living_players)}):**\n" + ", ".join(living_mentions)

    if not game.in_progress:
        player_count = len(game.players)
        if player_count >= 5:
            embed = game_roles.format_startgame_preview_embed(player_count)
            if embed is None:
                pool = game_roles.start_pool_for_player_count(player_count, rng=random.Random())
                embed = game_roles.format_role_pool_embed(pool, roles_assigned=False)
            try:
                await ctx.send(response, embed=embed)
            except discord.HTTPException:
                await safe_reply(ctx, response)
            return
        response += "\n\n_Join at least **5** players to preview the possible role pool._"

    await safe_reply(ctx, response)


@bot.command(name='startgame')
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def startgame(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if getattr(game, "_reset_in_progress", False):
        return await safe_reply(ctx,"🛑 **Please wait** — the previous game is still resetting.")
    from game_recovery import disk_recovery_blocked_reason

    block_reason = await asyncio.to_thread(disk_recovery_blocked_reason, ctx.guild.id)
    if block_reason:
        return await safe_reply(ctx, block_reason)
    if getattr(game, "ending", False):
        return await safe_reply(ctx,
            "🛑 The previous game ended without full cleanup. Run `!reset` or `!nukereset`, then `!startgame`."
        )
    if game.in_progress:
        return await safe_reply(ctx,"A game is already in progress!")

    try:
        await asyncio.to_thread(Game.commit_pending_endgame_if_any, ctx.guild.id)
    except Exception:
        logging.exception(
            "pending_endgame commit before startgame failed guild_id=%s",
            ctx.guild.id,
        )
    await game.ensure_rehydrated(ctx.guild)

    valid_players = []
    for p in game.players:
        member = await game.get_member_safe(ctx.guild, p.id)
        if member:
            valid_players.append(member)

    if len(valid_players) != len(game.players):
        await safe_reply(ctx,f"⚠️ Removed {len(game.players) - len(valid_players)} player(s) who left before the game started.")

    game.players = valid_players
    player_count = len(game.players)

    if player_count < 5: 
        return await safe_reply(ctx,"You need at least 5 players to start.")

    failed_dms = []
    for p in game.players:
        try:
            msg = await p.send("Checking DM permissions for game start...")
        except discord.HTTPException:
            failed_dms.append(p.display_name)
            continue

        # Deleting the check message is nice-to-have; don't treat delete failures as DM failures.
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
            
    if failed_dms:
        return await safe_reply(ctx,f"🛑 **Game Start Aborted!** The following players have DMs disabled: {', '.join(failed_dms)}. They must enable DMs from server members to play.")

    try:
        await game.setup_infrastructure(ctx.guild)
    except RuntimeError as e:
        return await safe_reply(ctx,f"🛑 **Game Start Aborted!** {e}")
    # Prefer the dedicated day text channel for all announcements, if available.
    if getattr(game, "day_tc_id", None):
        try:
            day_tc = ctx.guild.get_channel(game.day_tc_id)
            me = ctx.guild.me
            if not me and ctx.bot.user:
                me = ctx.guild.get_member(ctx.bot.user.id)

            bot_can_send = False
            players_can_view = False
            if isinstance(day_tc, discord.TextChannel) and me:
                perms_me = day_tc.permissions_for(me)
                perms_default = day_tc.permissions_for(ctx.guild.default_role)
                bot_can_send = perms_me.send_messages and perms_me.view_channel
                players_can_view = perms_default.view_channel

            if isinstance(day_tc, discord.TextChannel) and bot_can_send and players_can_view:
                game.game_channel_id = day_tc.id
                if day_tc.id != ctx.channel.id:
                    await safe_reply(ctx,f"✅ Game channels ready. Use {day_tc.mention} for day chat and announcements.")
            else:
                game.game_channel_id = ctx.channel.id
        except discord.HTTPException:
            game.game_channel_id = ctx.channel.id
    else:
        game.game_channel_id = ctx.channel.id

    pool_rng = random.Random()
    _POST_INFRA_ABORT = (
        " Run `!nukereset` if game channels were created and need cleanup."
    )
    try:
        roles_for_this_game = game_roles.draw_roles_for_startgame(player_count, rng=pool_rng)
    except ValueError as e:
        return await safe_reply(
            ctx, f"🛑 Role draw failed: {e}. Game not started.{_POST_INFRA_ABORT}"
        )

    if len(roles_for_this_game) != player_count:
        return await safe_reply(
            ctx,
            f"⚠️ Role list mismatch ({len(roles_for_this_game)} roles for {player_count} players)."
            f" Game not started.{_POST_INFRA_ABORT}",
        )

    dupes = game_roles.lobby_duplicate_violations(roles_for_this_game)
    if dupes:
        return await safe_reply(
            ctx,
            f"🛑 Duplicate unique roles generated (not allowed): {', '.join(dupes)}."
            f" Game not started.{_POST_INFRA_ABORT}",
        )

    random.shuffle(game.players)
    game.living_players = game.players.copy()
    # Stable targeting numbers: these should NOT shift when the living list order changes.
    game.player_slots = {p.id: i + 1 for i, p in enumerate(game.players)}
    random.shuffle(roles_for_this_game)

    game.player_roles = {p.id: r for p, r in zip(game.players, roles_for_this_game)}
    game.ending = False
    game.phase = "day"
    game.day_number = 1
    # Persisted idempotency key for history/stat commits (before DM outbox enqueue).
    game.started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    game.game_key = f"{ctx.guild.id}:{game.started_at}:{secrets.token_hex(8)}"
    # Endgame stats commit must be per-game idempotent; reset between games.
    game.stats_committed = False
    # Clear any stale per-game state from aborted runs.
    game.resolving = False
    game.vote_in_progress = False
    game.tribunal_state.clear_persisted()
    game.votes_today = 0
    game.graveyard = []
    game.night_actions, game.role_states, game.doused_players = {}, {}, set()
    game.night_death_causes = {}
    game.night_completion_snapshot = None
    game.psychic_visions_delivered_this_night = False
    game.night_transport_swaps = []
    game._transport_pairs_seen = set()
    game._effective_visit_destinations_cache = None
    game.bloodless_cycle_streak = 0
    game.deaths_this_cycle = 0
    game.bloodless_stalemate_pending = False

    alive_role = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
    playing_role = ctx.guild.get_role(PLAYING_ROLE_ID)
    lockdown_role = ctx.guild.get_role(game.lockdown_role_id) if getattr(game, "lockdown_role_id", None) else None

    for p_id, role in game.player_roles.items():
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue

        state: Dict = {}
        if role == "Vigilante":    state = {"shots_remaining": 1, "will_die_of_guilt": False, "guilty_tomorrow": False}
        elif role == "Gravedigger": state = {"uses_remaining": 1}
        elif role == "Survivor":
            state = {"vests_remaining": role_starting_charges(player_count=player_count)}
        elif role == "Mayor":      state = {"is_revealed": False}
        elif role == "Doctor":     state = {"self_heals_remaining": 1}
        elif role == "Bodyguard":  state = {"uses_remaining": 1, "self_protects_remaining": 1}
        elif role == "Witch":      state = {"has_learned_role": False, "night1_shield_used": False}
        elif role == "Gatekeeper":
            state = {"uses_remaining": role_starting_charges(player_count=player_count)}
        elif role == "Scary Grandma":
            state = {"alerts_remaining": role_starting_charges(player_count=player_count)}
        elif role == "Mole":       state = {"uses_remaining": 1}
        elif role == "Tailor":     state = {"uses_remaining": 1}
        elif role == "Pirate":     state = {"wins": 0}
        elif role == "Retributionist":
            state = {
                "uses_remaining": role_starting_charges(player_count=player_count),
                "used_corpses": [],
            }
        elif role == "Chaos":
            state = {
                "uses_remaining": chaos_starting_uses(player_count),
                "night1_shield_used": False,
            }
        elif role == "Psychic":
            state = {}
        elif role == "Deputy":
            state = {"deputy_shots_remaining": 1, "deputy_fired_day": 0}
        elif role == "Seer":
            state = {"seer_pair_history": []}
        elif role == "Serial Killer":
            state = {"sk_cautious": False, "sk_target_id": None}
        elif role == "Guardian Angel":
            state = {"ga_ward_charges": 1, "ga_defeated": False}
        elif role == "Jester":
            state = {"night1_shield_used": False}
        elif role == "Executioner":
            # ToS-like: target starts as a Town role; exclude Mayor (and self).
            targets = [
                p.id for p in game.players
                if p.id != p_id and game.player_roles.get(p.id) in TOWN_ROLES and game.player_roles.get(p.id) != "Mayor"
            ]
            if targets:
                state = {"exe_target": random.choice(targets)}
            else:
                # Audit #15 — ToS-aligned reroll: if no eligible Town target
                # exists (e.g., all Town are Mayors), convert the role to
                # Jester at startgame time. Without this, the Executioner has
                # no exe_target and is permanently unwinnable.
                game.player_roles[p_id] = "Jester"
                role = "Jester"
                state = {"night1_shield_used": False}

        if state:
            game.role_states[p_id] = state

        # Snapshot role_start for honest history/role leaderboards (survives promotions/conversions).
        game.role_states.setdefault(p_id, {})["role_start"] = role
            
        if alive_role:
            try:
                await player.add_roles(alive_role)
            except discord.HTTPException:
                pass
        # Everyone in the game gets Playing (including GM/admin).
        if playing_role:
            try:
                await player.add_roles(playing_role)
            except discord.HTTPException:
                pass

        # Only non-staff get the lockdown role (so staff can still play without losing access).
        if lockdown_role:
            if any(r.id == GAME_OVERSEER_ROLE_ID for r in player.roles) or player.guild_permissions.administrator:
                pass
            else:
                try:
                    await player.add_roles(lockdown_role)
                except discord.HTTPException:
                    pass

    ga_ids = sorted([pid for pid, r in game.player_roles.items() if r == "Guardian Angel"])
    for ga_id in ga_ids:
        bind_pool = guardian_angel_bind_pool_ids([p.id for p in game.players], ga_id)
        if bind_pool:
            bind_id = int(random.choice(bind_pool))
            game.role_states.setdefault(ga_id, {})["ga_target_id"] = bind_id

    db = getattr(bot, "db", None)
    dm_failures: List[str] = []
    for p_id, role in game.player_roles.items():
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue
        state = game.role_states.get(p_id, {}) or {}
        exe_name: Optional[str] = None
        if role == "Executioner" and state.get("exe_target"):
            target_user = ctx.guild.get_member(state["exe_target"])
            if target_user:
                exe_name = target_user.display_name
        bind_slot = _ga_bind_slot(game, state) if role == "Guardian Angel" else None
        if db:
            db.enqueue_dm_outbox(
                guild_id=ctx.guild.id,
                kind="role_deal",
                dedupe_key=f"mafia_role_deal:{ctx.guild.id}:{game.game_key}:{p_id}",
                target_user_id=p_id,
                content=(
                    f"--- GAME STARTED ---\nYour role is: **{role}**\n"
                    f"Use `!myrole` at any time to see your role's description and abilities."
                ),
            )
            for kind, content in role_start_dm_supplements(
                role, bind_slot=bind_slot, exe_target_display=exe_name
            ):
                db.enqueue_dm_outbox(
                    guild_id=ctx.guild.id,
                    kind=kind,
                    dedupe_key=f"mafia_{kind}:{ctx.guild.id}:{game.game_key}:{p_id}",
                    target_user_id=p_id,
                    content=content,
                )
        else:
            try:
                await player.send(
                    f"--- GAME STARTED ---\nYour role is: **{role}**\n"
                    f"Use `!myrole` at any time to see your role's description and abilities."
                )
                for _kind, content in role_start_dm_supplements(
                    role, bind_slot=bind_slot, exe_target_display=exe_name
                ):
                    await player.send(content)
            except discord.HTTPException:
                dm_failures.append(player.display_name)

        await _post_game_start_to_player_private_channel(game, ctx.guild, player, role)

    if dm_failures and not db:
        return await safe_reply(
            ctx,
            "🛑 **Game Start Aborted!** Could not DM: "
            f"{', '.join(dm_failures)}. They must enable DMs from server members."
            f"{_POST_INFRA_ABORT}",
        )

    game.in_progress = True

    role_pool = game_roles.start_pool_for_player_count(player_count, rng=pool_rng)
    pool_embed = game_roles.format_role_pool_embed(role_pool)
    announce_ch = ctx.guild.get_channel(game.game_channel_id) if game.game_channel_id else ctx.channel
    if isinstance(announce_ch, discord.TextChannel):
        try:
            await announce_ch.send(embed=pool_embed)
        except discord.HTTPException as e:
            logging.warning("Role pool embed send failed: %s", e)
            try:
                await safe_reply(ctx,"⚠️ Could not post the role pool embed to the game channel.")
            except discord.HTTPException:
                pass

    await game.apply_first_day_discord_setup(ctx.guild)
    if not await post_game_channel(
        game,
        ctx.guild,
        tos_msg.day_header(1),
        tos_msg.game_begun(),
        tos_msg.living_count(len(game.living_players)),
    ):
        try:
            await safe_reply(ctx,"⚠️ Could not post day-open messages to the game channel.")
        except discord.HTTPException:
            pass
    logging.info(f"Game started on guild {ctx.guild.id} with {player_count} players.")

    mafia_tc = ctx.guild.get_channel(game.mafia_tc_id)
    if mafia_tc:
        for p in game.players:
            if game.player_roles.get(p.id) in ALL_MAFIA_ROLES:
                await game.grant_mafia_channel_access(ctx.guild, p)
        try:
            await mafia_tc.send("Welcome, Mafiosi. This is your private channel.")
        except discord.HTTPException:
            pass

    await game.persist_flush()


_RESOLVING_GM_BLOCK = "🛑 **Night resolution in progress.** Wait for `!resolve` to finish."


def _night_resolve_blocks_gm_commands(game: Game) -> bool:
    from game import night_resolve_in_progress

    return night_resolve_in_progress(game)


async def _consume_retributionist_if_needed(
    game: Game,
    snap: object,
    blocked: Collection[int],
    healed_by_map: Dict[int, int],
) -> None:
    if _snap_bool(snap, "retri_consumption_done"):
        return
    from retributionist_consumption import consume_retributionist_uses

    consume_retributionist_uses(game, blocked, healed_by_map)
    norm = normalize_night_completion_snapshot(
        getattr(game, "night_completion_snapshot", None)
    ) or normalize_night_completion_snapshot(snap) or {}
    norm["retri_consumption_done"] = True
    norm["day"] = int(game.day_number)
    norm["game_key"] = game.game_key
    game.night_completion_snapshot = norm
    await game.persist_flush()


async def _persist_post_engine_checkpoint(
    game: Game,
    *,
    deaths: Set[int],
    engine_deaths: List[int],
    blocked: Collection[int],
    healed_by_map: Dict[int, int],
    retri_consumption_done: bool,
    post_pipeline_pending: bool,
    guilty_vigs: Optional[List[int]] = None,
    jester_haunts: Optional[List[int]] = None,
    night_feedback_sent: bool = True,
) -> None:
    """Durable night snapshot after engine (and optional post-engine bookkeeping)."""
    from night_engine_checkpoint import _merge_snap

    gv = guilty_vigs if guilty_vigs is not None else []
    jh = jester_haunts if jester_haunts is not None else []
    _merge_snap(
        game,
        {
            "day": int(game.day_number),
            "game_key": game.game_key,
            "deaths": sorted(int(x) for x in deaths if x is not None),
            "engine_deaths": list(engine_deaths),
            "blocked": [int(x) for x in blocked],
            "healed_by": snapshot_healed_by_map(healed_by_map),
            "guilty_vigs": sorted(int(x) for x in gv),
            "jester_haunts": sorted(int(x) for x in jh),
            "pre_pipeline": False,
            "post_pipeline_pending": post_pipeline_pending,
            "night_engine_completed": True,
            "night_engine_running": False,
            "night_feedback_sent": night_feedback_sent,
            "retri_consumption_done": retri_consumption_done,
            "attacked_reasons": snapshot_attacked_tonight_reasons(game),
            "psychic_visions_delivered": bool(
                getattr(game, "psychic_visions_delivered_this_night", False)
            ),
        },
    )
    await game.persist_flush()


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def night(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await safe_reply(ctx,"No game is in progress.")
    if _night_resolve_blocks_gm_commands(game):
        return await safe_reply(ctx,_RESOLVING_GM_BLOCK)
    # Audit #6: refuse to start night while a tribunal vote is in flight.
    # Otherwise the running vote() coroutine wakes mid-asyncio.sleep, sees
    # is_active("day") is False, returns, and the finally never removes the
    # defendant's stand_role — leaving them able to talk during the night
    # because stand_role is configured with connect=True/speak=True.
    if getattr(game, "vote_in_progress", False):
        return await safe_reply(ctx,
            "🛑 A trial is currently in progress. Finish the tribunal before starting night."
        )
    await game.start_night(ctx)


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def day(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await safe_reply(ctx,"No game is in progress.")
    if _night_resolve_blocks_gm_commands(game):
        return await safe_reply(ctx,_RESOLVING_GM_BLOCK)
    if game.phase == "day":
        return await safe_reply(ctx,"It is already day.")
    if game.phase == "night":
        return await safe_reply(ctx,
            "🛑 It is **night** — run `!resolve` to finish the night before forcing day."
        )
    await game.start_day(ctx)


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def resolve(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress or game.phase != "night":
        return await safe_reply(ctx,"This can only be used during the night phase.")

    async with game._night_resolve_guard:
        if game.resolving:
            return await safe_reply(ctx, "Resolution is already in progress.")

        snap = normalize_night_completion_snapshot(
            getattr(game, "night_completion_snapshot", None)
        )
        if snap is not None:
            game.night_completion_snapshot = snap

        night_resume = _parse_night_resume_state(
            snap,
            day_number=int(game.day_number),
            game_key=getattr(game, "game_key", None),
        )
        if snap is not None and not night_resume.resuming:
            game.night_completion_snapshot = None
            snap = None
            night_resume = _parse_night_resume_state(
                None,
                day_number=int(game.day_number),
                game_key=getattr(game, "game_key", None),
            )
        resume_post_pipeline_only = night_resume.resume_post_pipeline_only
        resume_engine_incomplete = night_resume.resume_engine_incomplete

        pending_duels = [
            a
            for a in game.night_actions.values()
            if a.get("type") == "plunder"
            and not a.get("duel_outcome_ready", False)
        ]
        if pending_duels:
            return await safe_reply(ctx,
                "⚔️ A Pirate duel is still in progress. Please wait for it to finish before resolving the night."
            )

        game.resolving = True
        try:
            if not game.in_progress:
                _clear_night_resolve_checkpoints(game)
                return

            await game.ensure_rehydrated(ctx.guild)
            await game.sync_living_players(ctx.guild)

            from engine.night import restore_attacked_tonight_reasons, restore_healed_by_map

            if resume_post_pipeline_only:
                await safe_reply(ctx,
                    "🌅 **Resuming interrupted night resolution** — night engine already ran; finishing deaths and day…"
                )
                blocked = _coerce_snap_id_list(snap, "blocked")
                healed_by_map = restore_healed_by_map((snap or {}).get("healed_by"))
                night_kill_deaths = night_kill_deaths_from_snap(snap)
                deaths = set(_coerce_snap_id_list(snap, "deaths")) or set(night_kill_deaths)
                if not _snap_bool(snap, "night_feedback_sent"):
                    restore_attacked_tonight_reasons(game, (snap or {}).get("attacked_reasons"))
                await _deliver_night_feedback_once(
                    game,
                    ctx.guild,
                    blocked,
                    deaths=set(night_kill_deaths),
                    healed_by_map=healed_by_map,
                )
                await _send_pirate_personal_wins_if_needed(game, ctx.guild)
            else:
                from night_resume import apply_stale_night_feedback_recovery

                snap_norm, feedback_recovered = apply_stale_night_feedback_recovery(
                    getattr(game, "night_completion_snapshot", None)
                )
                if feedback_recovered and snap_norm is not None:
                    game.night_completion_snapshot = snap_norm
                    game.psychic_visions_delivered_this_night = False
                    await game.persist_flush()
                if resume_engine_incomplete:
                    if _snap_bool(snap, "night_engine_running"):
                        await safe_reply(ctx,
                            "🌅 **Resuming interrupted night resolution** — finishing night engine…"
                        )
                    else:
                        await safe_reply(ctx,
                            "🌅 **Resuming interrupted night resolution** — running night engine…"
                        )
                elif snap is None:
                    game.night_completion_snapshot = {
                        "day": int(game.day_number),
                        "game_key": game.game_key,
                        "pre_pipeline": True,
                        "post_pipeline_pending": False,
                        "night_feedback_sent": False,
                        "night_engine_completed": False,
                        "night_engine_running": False,
                        "retri_consumption_done": False,
                        "deaths": [],
                        "engine_deaths": [],
                        "blocked": [],
                        "healed_by": {},
                        "guilty_vigs": [],
                        "jester_haunts": [],
                        "attacked_reasons": snapshot_attacked_tonight_reasons(game),
                    }
                    await game.persist_flush()
                _visit_log, blocked, healed_by_map, _protected_by_map, deaths = await run_night_pipeline(
                    game, ctx.guild, deliver_feedback=False
                )
                deaths = set(deaths)
                night_kill_deaths = set(deaths)
                if resume_engine_incomplete and not _snap_bool(snap, "night_feedback_sent"):
                    restore_attacked_tonight_reasons(game, (snap or {}).get("attacked_reasons"))
                await _deliver_night_feedback_once(
                    game,
                    ctx.guild,
                    blocked,
                    deaths=night_kill_deaths,
                    healed_by_map=healed_by_map,
                )
                await _send_pirate_personal_wins_if_needed(game, ctx.guild)
                await _persist_post_engine_checkpoint(
                    game,
                    deaths=deaths,
                    engine_deaths=sorted(int(x) for x in night_kill_deaths),
                    blocked=blocked,
                    healed_by_map=healed_by_map,
                    retri_consumption_done=False,
                    post_pipeline_pending=True,
                    night_feedback_sent=True,
                )
                snap = normalize_night_completion_snapshot(
                    getattr(game, "night_completion_snapshot", None)
                )

            await _consume_retributionist_if_needed(game, snap, blocked, healed_by_map)
            guilty_vigs, jester_haunts = await tally_guilt_and_jester_deaths(
                game, ctx.guild, deaths, night_kill_deaths
            )
            await _persist_post_engine_checkpoint(
                game,
                deaths=deaths,
                engine_deaths=sorted(int(x) for x in night_kill_deaths),
                blocked=blocked,
                healed_by_map=healed_by_map,
                retri_consumption_done=True,
                post_pipeline_pending=False,
                guilty_vigs=guilty_vigs,
                jester_haunts=jester_haunts,
            )

            await _resolve_night_post_pipeline(
                game,
                ctx,
                deaths=set(deaths),
                blocked=blocked,
                guilty_vigs=guilty_vigs,
                jester_haunts=jester_haunts,
            )
        finally:
            game.resolving = False
            if game.in_progress and not getattr(game, "ending", False):
                await game.persist_flush()

@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def status(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress or game.phase != "night":
        return await safe_reply(ctx,"This command can only be used during the night phase.", delete_after=10)
    
    await game.sync_living_players(ctx.guild)
    living_ids = await game.get_living_ids(ctx.guild)
    if ctx.author.id in living_ids:
        return await safe_reply(ctx,"🛑 **Anti-Cheat:** You cannot check the status list while you are an active player!", delete_after=10)

    acted, waiting_for = [], []
    action_roles = {
        "Mobster", "Doctor", "Escort", "Consort", "Sheriff", "Investigator", "Framer", "Gravedigger", "Vigilante",
        "Transporter", "Bodyguard", "Lookout", "Tracker", "Witch", "Arsonist", "Hypnotist", "Mole", "Tailor", "Pirate", "Gatekeeper", "Survivor", "Scary Grandma",
        "Retributionist", "Chaos", "Serial Killer", "Seer", "Guardian Angel",
    }
    
    for p_id, role in game.player_roles.items():
        if p_id not in living_ids or role not in action_roles:
            continue
        player = await game.get_member_safe(ctx.guild, p_id)
        if not player:
            continue
        
        has_acted = p_id in game.night_actions and (
            role != "Pirate" or game.night_actions[p_id].get("duel_outcome_ready", False)
        )
        if role in ["Survivor", "Scary Grandma"] and not has_acted:
            continue
        (acted if has_acted else waiting_for).append(f"- {player.display_name} ({role})")

    embed = discord.Embed(title=f"🌙 Night {game.day_number} Status", color=discord.Color.blue())
    embed.add_field(name="✅ Acted",       value="\n".join(acted)       or "None yet.", inline=False)
    embed.add_field(name="⏳ Waiting For", value="\n".join(waiting_for) or "All in!",   inline=False)
    
    try:
        await ctx.author.send(embed=embed)
        await ctx.message.delete()
        await safe_reply(ctx,"Night status sent to your DMs.", delete_after=5)
    except discord.HTTPException:
        await safe_reply(ctx,"I can't DM you! Check your privacy settings.", delete_after=10)


@bot.command(name='slay')
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def slay_command(ctx: commands.Context, member: discord.Member, *, message: Optional[str] = None) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await safe_reply(ctx,"No game is in progress.")
    if _night_resolve_blocks_gm_commands(game):
        return await safe_reply(ctx,_RESOLVING_GM_BLOCK)
    if getattr(game, "vote_in_progress", False):
        return await safe_reply(ctx,
            "🛑 A trial is currently in progress. Finish the tribunal before using `!slay`."
        )
    await game.process_death(ctx, member, cause="manual", custom_message=message)
    await game.check_win_conditions()


@bot.command()
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def reset(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if _night_resolve_blocks_gm_commands(game):
        return await safe_reply(ctx,_RESOLVING_GM_BLOCK)
    if getattr(game, "vote_in_progress", False):
        return await safe_reply(
            ctx,
            "🛑 A trial is currently in progress. Finish or cancel the tribunal before `!reset`.",
        )
    from game_recovery import commit_pending_endgame_before_state_delete

    await asyncio.to_thread(commit_pending_endgame_before_state_delete, ctx.guild.id)
    await game.reset(ctx.guild)
    await safe_reply(
        ctx,
        "🔄 **Game manually reset.** Discord state cleared. "
        "SQLite leaderboards and `state/*.stats.json` are **unchanged** — use `!nukereset` only for infra wipe.",
    )


@bot.command(name="nukereset")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def nukereset(ctx: commands.Context) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if _night_resolve_blocks_gm_commands(game):
        return await safe_reply(ctx, _RESOLVING_GM_BLOCK)
    if getattr(game, "vote_in_progress", False):
        return await safe_reply(
            ctx,
            "🛑 A trial is currently in progress. Finish or cancel the tribunal before `!nukereset`.",
        )
    if getattr(game, "_reset_in_progress", False):
        return await safe_reply(ctx, "🛑 **Please wait** — the previous game is still resetting.")
    await safe_reply(ctx,"⚠️ **NUKE RESET** in progress… this may take a bit.")
    from game_recovery import commit_pending_endgame_before_state_delete

    await asyncio.to_thread(commit_pending_endgame_before_state_delete, ctx.guild.id)
    try:
        await game.nuke_reset(ctx.guild)
    except Exception:
        logging.exception("Nuke reset failed")
        return await safe_reply(ctx,"🛑 Nuke reset failed — check logs. You may need to fix permissions or delete channels manually.")
    await safe_reply(
        ctx,
        "☢️ **Nuke reset complete.** Roles, permissions, channels, and game JSON were cleaned up. "
        "SQLite/history stats were **not** wiped.",
    )


