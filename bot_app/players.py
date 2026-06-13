"""Player utility commands (myrole, will, whisper, etc.)."""

from __future__ import annotations

from bot_app.imports import *  # noqa: F403
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay
from bot_app.bootstrap import enforce_allowed_guild_check
from bot_app.shared import (
    _GUILD_UNAVAILABLE_MSG,
    _build_server_stats_embed,
    _living_ids_for_context,
    deny_player_command,
    safe_reply,
    safe_send,
)
from stats_personal import personal_wins_for_display
from bot_app.ui import WillModal, WillView


async def _private_or_dm_action_surface_ok(ctx: commands.Context, game: Game) -> bool:
    """DMs or the configured per-player private guild channel (mirrors night-action privacy)."""
    if ctx.guild is None:
        return True
    if int(ctx.guild.id) != int(game.guild_id):
        await safe_reply(ctx, "🛑 This action belongs to a different server's game.")
        return False
    expected_channel_id = PLAYER_PRIVATE_CHANNEL_IDS.get(int(ctx.author.id))
    if not expected_channel_id:
        for k, v in PLAYER_PRIVATE_CHANNEL_IDS.items():
            if int(v) == int(ctx.author.id):
                expected_channel_id = int(k)
                break
    if not expected_channel_id:
        await safe_reply(ctx, "🛑 Your private channel isn't configured yet. Ask a GM to set it up.")
        return False
    if ctx.channel.id != int(expected_channel_id):
        await safe_reply(ctx, "🛑 Use your private channel for this action (or DM me the command).")
        return False
    return True


def _whisper_blocked(game: Game) -> bool:
    sub = getattr(game, "tribunal_subphase", None)
    return bool(getattr(game, "vote_in_progress", False)) or sub in (
        "defense",
        "judgment",
        "last_words",
    )


async def _whisper_guards_ok(
    ctx: commands.Context, game: Game
) -> tuple[Optional[str], Optional[List[int]]]:
    """Return (error_message, living_ids) — living_ids set when guards pass."""
    if not game.in_progress or game.phase != "day":
        return tos_msg.whisper_not_day(), None
    living_ids = await _living_ids_for_context(ctx, game)
    if living_ids is None:
        return _GUILD_UNAVAILABLE_MSG, None
    if ctx.author.id not in living_ids:
        return tos_msg.whisper_not_day(), None
    if _whisper_blocked(game):
        return tos_msg.whisper_not_day(), None
    return None, living_ids


def _whisper_ignore_list(game: Game, pid: int) -> List[int]:
    raw = game.role_states.get(pid, {}).get("whisper_ignored") or []
    out: List[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


# ==========================================
# UTILITY / PLAYER COMMANDS
# ==========================================

@bot.command()
async def myrole(ctx: commands.Context) -> None:
    if not isinstance(ctx.channel, discord.DMChannel):
        return
        
    game = get_game_by_player_id(ctx.author.id)
    if not game:
        return await safe_reply(ctx, "The game hasn't started yet! You don't have a role.")
        
    role = game.player_roles.get(ctx.author.id)
    embed = discord.Embed(
        title=f"Your Role: {role}", 
        description=get_role_description(role), 
        color=discord.Color.green()
    )
    
    if role == "Executioner" and game.role_states.get(ctx.author.id):
        target_id = game.role_states[ctx.author.id].get("exe_target")
        target_user = bot.get_user(target_id)
        if not target_user and target_id:
            guild = bot.get_guild(game.guild_id)
            if guild:
                target_user = guild.get_member(target_id)
                if not target_user:
                    try:
                        target_user = await guild.fetch_member(target_id)
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        target_user = None
        if target_user:
            embed.add_field(
                name="Your Target", 
                value=f"Convince Town to lynch **{target_user.display_name}**.", 
                inline=False
            )
            
    if await safe_send(ctx, embed=embed) is None:
        await safe_reply(ctx, "🛑 Could not send your role card. Check your DMs are open.")


@bot.command()
async def will(ctx: commands.Context, *, text: Optional[str] = None) -> None:
    """
    DM-only Last Will editor.
      - `!will` shows your current will and provides a button to edit via Modal.
      - `!will clear` clears your will.
    """
    # DM-only to keep wills private. If used in a guild channel, delete and instruct via DM, then return.
    if not isinstance(ctx.channel, discord.DMChannel):
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        try:
            await ctx.author.send("📝 Use `!will` here in DMs to view/edit your will.")
        except discord.HTTPException:
            return
        return

    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.in_progress:
        try:
            await ctx.author.send("No active game found for you.")
        except discord.HTTPException:
            pass
        return

    guild = await resolve_game_guild(bot, int(game.guild_id))
    if guild is None:
        return await ctx.author.send(_GUILD_UNAVAILABLE_MSG)
    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if ctx.author.id not in living_ids:
        return await ctx.author.send("You are not alive in this game — wills are locked.")

    state = game.role_states.setdefault(ctx.author.id, {})
    if text is not None and text.strip().lower() in ("clear", "c"):
        state["will"] = ""
        await game.persist_flush()
        return await ctx.author.send(tos_msg.will_cleared())

    current = str(state.get("will", "") or "")
    display = current.strip() or "(empty)"
    await ctx.author.send(
        "**Your Last Will:**\n"
        f"```{display[:1800]}```\n"
        "Use the button below to edit it."
    , view=WillView(owner_id=ctx.author.id, current_text=current))


@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def stats(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    """
    Show personal winrate stats for this guild.
      - `!stats` shows your stats.
      - `!stats @user` is GM-only.
    """
    is_gm = any(r.id == GAME_OVERSEER_ROLE_ID for r in getattr(ctx.author, "roles", []))
    target = member or ctx.author
    if member is not None and not is_gm:
        return await safe_reply(ctx,"Only the Game Overseer can view other players' stats.")

    db = getattr(bot, "db", None)
    if not db:
        return await safe_reply(ctx, "Stats are not available (DB not initialized).")

    try:
        rec = await asyncio.to_thread(
            db.get_player_stats_summary, guild_id=ctx.guild.id, player_id=target.id
        )
    except Exception:
        rec = None

    if rec is None:
        rec = {}

    def _safe_int(v: object) -> int:
        try:
            return int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    games = _safe_int(rec.get("games_played", 0))
    wins = _safe_int(rec.get("wins", 0))
    losses = _safe_int(rec.get("losses", 0))
    draws = _safe_int(rec.get("draws", 0))
    wr = (wins / games * 100.0) if games > 0 else 0.0
    games_excl_draws = max(0, games - draws)
    wr_excl_draws = (wins / games_excl_draws * 100.0) if games_excl_draws > 0 else wr

    def _coerce_int_map(d: object) -> Dict[str, int]:
        if not isinstance(d, dict):
            return {}
        out: Dict[str, int] = {}
        for k, v in d.items():
            out[str(k)] = _safe_int(v)
        return out

    def _top_n(d: Dict[str, int], n: int = 5) -> str:
        if not d:
            return "(none)"
        items = sorted(((str(k), _safe_int(v)) for k, v in d.items()), key=lambda kv: (-kv[1], kv[0].lower()))
        return ", ".join(f"{k}({v})" for k, v in items[:n])

    role_played = _coerce_int_map(rec.get("role_played"))
    role_wins = _coerce_int_map(rec.get("role_wins"))
    faction_played = _coerce_int_map(rec.get("faction_played"))
    faction_wins_raw = _coerce_int_map(rec.get("faction_wins"))
    # Faction wins = Town / Mafia / Arsonist only (neutrals live under Personal wins).
    faction_wins = {
        k: faction_wins_raw[k]
        for k in ("Town", "Mafia", "Arsonist")
        if faction_wins_raw.get(k)
    }
    personal = personal_wins_for_display(rec.get("personal_wins") or {})

    desc_lines = [
        f"Games: **{games}**",
        f"Wins: **{wins}**  Losses: **{losses}**  Draws: **{draws}**",
    ]
    if draws > 0 and games_excl_draws > 0:
        desc_lines.append(
            f"WR: **{wr_excl_draws:.1f}%** ({wins}/{games_excl_draws} decided games)"
        )
        desc_lines.append(f"WR (all games incl. draws): **{wr:.1f}%**")
    else:
        desc_lines.append(f"WR: **{wr:.1f}%** (wins/games)")

    embed = discord.Embed(
        title=f"📊 Stats for {target.display_name}",
        description="\n".join(desc_lines),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Faction played", value=_top_n(faction_played, 3), inline=False)
    embed.add_field(
        name="Faction wins",
        value=_top_n(faction_wins, 3) if faction_wins else "(none)",
        inline=False,
    )
    embed.add_field(name="Top roles played", value=_top_n(role_played, 5), inline=False)
    embed.add_field(name="Top role wins", value=_top_n(role_wins, 5), inline=False)
    if personal:
        embed.add_field(name="Personal wins", value=_top_n(personal, 10), inline=False)
    embed.set_footer(
        text="Faction wins = Town/Mafia/Arsonist only · Pirate, Exe, Jester, GA, etc. under Personal wins"
    )
    if await safe_send(ctx, embed=embed) is None:
        await safe_reply(ctx, "🛑 Could not send stats embed.")


@bot.command(name="serverstats")
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def serverstats(ctx: commands.Context) -> None:
    """Server-wide winrates (faction, role, outcomes, deaths). Auto-updates when games end."""
    db = getattr(bot, "db", None)
    if not db:
        return await safe_reply(ctx, "Server stats are not available (DB not initialized).")

    from bot_app.ui import ServerStatsView

    embed = await _build_server_stats_embed(db=db, guild_id=ctx.guild.id, page="overview")
    view = ServerStatsView(invoker_id=ctx.author.id, guild_id=ctx.guild.id, db=db)
    msg = await safe_send(ctx, embed=embed, view=view)
    if msg is None:
        return await safe_reply(ctx, "🛑 Could not send server stats embed.")
    view.message = msg


@bot.command(name="importstats")
@commands.has_role(GAME_OVERSEER_ROLE_ID)
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def importstats(ctx: commands.Context, *tokens: str) -> None:
    """
    One-time importer: migrate existing JSON stats to SQLite.
    This does NOT delete the JSON stats file.

    Use ``!importstats force confirm stale`` when SQLite is ahead of JSON and you
    still intend to overwrite aggregates from the JSON mirror.
    """
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if game and game.in_progress:
        return await safe_reply(ctx,
            "🛑 Cannot import stats while a game is **in progress** — finish or `!reset` first."
        )
    from game_recovery import _pending_endgame_meta

    if _pending_endgame_meta(ctx.guild.id):
        return await safe_reply(
            ctx,
            "🛑 Cannot import stats while **pending endgame** stats are uncommitted. "
            "Run `!reset` after SQLite recovery or wait for auto-commit.",
        )
    db = getattr(bot, "db", None)
    if not db:
        return await safe_reply(ctx,"SQLite DB not initialized; cannot import.")
    data = load_stats(ctx.guild.id) or {}
    lower = [str(t).strip().lower() for t in tokens]
    force_confirmed = "force" in lower and "confirm" in lower
    if "force" in lower and not force_confirmed:
        return await safe_reply(
            ctx,
            "🛑 Destructive overwrite requires confirmation: `!importstats force confirm`",
        )
    force_import = force_confirmed
    stale_reason: Optional[str] = None
    try:
        stale_reason = await asyncio.to_thread(
            db.assess_import_staleness,
            guild_id=ctx.guild.id,
            stats_data=data,
        )
    except Exception:
        logging.exception("Import staleness check failed.")
        stale_reason = "Could not verify SQLite vs JSON — use `!importstats force confirm stale` if you intend to import anyway."
    if not force_import:
        if stale_reason:
            return await safe_reply(
                ctx,
                f"🛑 Import blocked (staleness guard).\n{stale_reason}",
            )
    elif stale_reason and "stale" not in lower:
        return await safe_reply(
            ctx,
            f"🛑 Force import blocked — SQLite looks newer than JSON.\n{stale_reason}\n"
            "Add **stale** to confirm regression: `!importstats force confirm stale`",
        )
    if force_import:
        try:
            from persistence import backup_file, guild_stats_path, sqlite_db_path

            backup_file(sqlite_db_path())
            backup_file(guild_stats_path(ctx.guild.id))
        except Exception:
            logging.exception("Pre-import backup failed guild_id=%s", ctx.guild.id)
    n = 0
    try:
        n = await asyncio.to_thread(
            db.import_player_stats_from_json,
            guild_id=ctx.guild.id,
            stats_data=data,
        )
    except Exception:
        logging.exception("Stats import failed.")
        return await safe_reply(ctx,"🛑 Import failed — check logs.")
    force_note = " (**force** — SQLite aggregates replaced from JSON)" if force_import else ""
    msg = (
        f"✅ Imported **{n}** player stat record(s) into SQLite{force_note}.\n"
        "Faction/role/personal totals are live. **Outcome %, lobby sizes, avg game length, "
        "and death causes** need completed games (or `!exportstats` after more play)."
    )
    await safe_reply(ctx, msg)
    try:
        from bot_app.stats_board import refresh_guild_stats_board

        await refresh_guild_stats_board(bot=bot, guild_id=int(ctx.guild.id))
    except Exception:
        logging.debug("Stats board refresh after import skipped guild_id=%s", ctx.guild.id)

@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def reveal(ctx: commands.Context) -> None:
    from game import night_resolve_in_progress

    game = get_game_by_player_id(ctx.author.id)
    if not game or game.phase != "day" or game.player_roles.get(ctx.author.id) != "Mayor":
        return await deny_player_command(
            ctx, "🛑 You must be the living Mayor during the day to reveal."
        )
    guild = bot.get_guild(game.guild_id)
    if not guild:
        return await deny_player_command(
            ctx, "🛑 You must be the living Mayor during the day to reveal."
        )
    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if ctx.author.id not in living_ids:
        return await deny_player_command(
            ctx, "🛑 You must be the living Mayor during the day to reveal."
        )
    if night_resolve_in_progress(game):
        return await deny_player_command(ctx, "🛑 Please wait — night resolution is in progress.")
    if game.role_states.get(ctx.author.id, {}).get("is_revealed"):
        return await safe_reply(ctx,"You have already revealed yourself!")
        
    game.role_states.setdefault(ctx.author.id, {})["is_revealed"] = True
    await game.persist_flush()
    if not await post_game_channel(
        game,
        guild,
        f"👑 **{ctx.author.mention} has revealed as the Mayor! Their vote now counts as two.**",
    ):
        await safe_reply(ctx, "⚠️ Could not post your reveal to the game channel.")

@bot.command()
async def haunt(ctx: commands.Context, target_number: Optional[int] = None) -> None:
    if not isinstance(ctx.channel, discord.DMChannel):
        return await deny_player_command(ctx, "🛑 !haunt must be used in DMs.", dm_only=True)

    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.role_states.get(ctx.author.id, {}).get("can_haunt"):
        return await deny_player_command(ctx, "🛑 You cannot haunt anyone right now.")
    
    stored_voters: List[int] = game.role_states[ctx.author.id].get("guilty_voters", [])
    game_chan = bot.get_channel(game.game_channel_id)
    if not game_chan:
        return await deny_player_command(
            ctx, "🛑 Cannot haunt right now — the game channel is unavailable."
        )
    guild = game_chan.guild

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)

    # Live eligible list: filter out voters who are now dead/left.
    eligible_voters = [vid for vid in stored_voters if vid in living_ids]
    if not eligible_voters:
        return await safe_reply(ctx,"There are no eligible living voters left to haunt.")

    # Allow `!haunt` with no number to show the up-to-date list.
    if target_number is None:
        lines: List[str] = []
        for i, vid in enumerate(eligible_voters, start=1):
            m = await game.get_member_safe(guild, vid)
            lines.append(f"{i}: {m.display_name if m else str(vid)}")
        return await safe_reply(ctx,"Eligible voters you can haunt:\n" + "\n".join(lines) + "\nUse `!haunt <number>`.")

    if not (1 <= target_number <= len(eligible_voters)):
        return await safe_reply(ctx,"Invalid number. Use `!haunt` to see the current eligible list.")
    
    target_id = eligible_voters[target_number - 1]
    target = await game.get_member_safe(guild, target_id)
    
    if not target or target.id not in living_ids:
        return await safe_reply(ctx,"That player is already dead. Use `!haunt` to see the current eligible list.")

    game.role_states.setdefault(ctx.author.id, {})["haunt_target"] = target_id
    game.role_states[ctx.author.id]["can_haunt"] = False
    await game.persist_flush()
    await safe_reply(ctx,f"You have chosen to haunt **{target.display_name}**. Your soul may now rest.")


@bot.command(name="whisper", aliases=["w"])
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def whisper(ctx: commands.Context, target_number: int, *, message: str) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    guard_err, living_ids = await _whisper_guards_ok(ctx, game)
    if guard_err:
        return await safe_reply(ctx,guard_err)
    if not await _private_or_dm_action_surface_ok(ctx, game):
        return
    assert living_ids is not None
    if game.player_roles.get(ctx.author.id) == "Mayor" and game.role_states.get(ctx.author.id, {}).get(
        "is_revealed"
    ):
        return await safe_reply(ctx,tos_msg.whisper_mayor_revealed())
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    if target.id == ctx.author.id:
        return await safe_reply(ctx,tos_msg.whisper_self())
    if target.id not in living_ids:
        return await safe_reply(ctx,tos_msg.whisper_not_day())
    if game.player_roles.get(target.id) == "Mayor" and game.role_states.get(target.id, {}).get("is_revealed"):
        return await safe_reply(ctx,tos_msg.whisper_to_revealed_mayor())
    ignored = _whisper_ignore_list(game, ctx.author.id)
    if target.id in ignored or ctx.author.id in _whisper_ignore_list(game, target.id):
        return await safe_reply(ctx,tos_msg.whisper_ignoring())
    msg = discord.utils.escape_mentions((message or "").strip())[:1000]
    if not msg:
        return await deny_player_command(ctx, "🛑 Whisper message cannot be empty.")
    sname = tos_msg.format_player(game, ctx.author.id)
    tname = tos_msg.format_player(game, target.id)
    out_ok = await send_to_player_private_channel(
        ctx.guild, ctx.author.id, tos_msg.whisper_to_sender(tname, msg), log_context="whisper out"
    )
    in_ok = await send_to_player_private_channel(
        ctx.guild, target.id, tos_msg.whisper_from_recipient(sname, msg), log_context="whisper in"
    )
    if not (out_ok and in_ok):
        return await safe_reply(ctx,tos_msg.whisper_private_channel_failed())
    if not await post_game_channel(game, ctx.guild, tos_msg.whisper_public_meta(sname, tname)):
        return await safe_reply(ctx,tos_msg.whisper_public_delivery_failed())


@bot.command(name="ignore")
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def ignore_player(ctx: commands.Context, target_number: int) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    guard_err, _living = await _whisper_guards_ok(ctx, game)
    if guard_err:
        return await safe_reply(ctx,guard_err)
    target = await game.get_target_from_input(ctx, target_number)
    if not target or target.id == ctx.author.id:
        return await deny_player_command(ctx, "🛑 Pick a valid living player (not yourself).")
    st = game.role_states.setdefault(ctx.author.id, {})
    ignored = _whisper_ignore_list(game, ctx.author.id)
    if target.id not in ignored:
        ignored.append(target.id)
    st["whisper_ignored"] = ignored
    await game.persist_flush()
    await safe_reply(ctx,f"You will ignore whispers from **{target.display_name}**.")


@bot.command(name="unignore")
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def unignore_player(ctx: commands.Context, target_number: int) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    guard_err, _living = await _whisper_guards_ok(ctx, game)
    if guard_err:
        return await safe_reply(ctx,guard_err)
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return await deny_player_command(ctx, "🛑 Pick a valid living player (not yourself).")
    st = game.role_states.setdefault(ctx.author.id, {})
    ignored = [x for x in _whisper_ignore_list(game, ctx.author.id) if x != target.id]
    st["whisper_ignored"] = ignored
    await game.persist_flush()
    await safe_reply(ctx,f"You will no longer ignore **{target.display_name}**.")


@bot.hybrid_command(name="clear", aliases=["c"])
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def clear_action(ctx: commands.Context) -> None:
    game = ctx.game
    await game.clear_night_action(ctx)

