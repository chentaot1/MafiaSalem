"""Day-phase vote and tribunal flow."""

from __future__ import annotations

from bot_app.imports import *  # noqa: F403
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay
from bot_app.bootstrap import enforce_allowed_guild_check
from bot_app.shared import safe_reply, safe_send


"""
Tribunal flow (persisted deadlines + on_ready resume in bootstrap)

```mermaid
stateDiagram-v2
  [*] --> Nomination: !vote
  Nomination --> Defense: defendant on stand
  Defense --> Judgment: defense timer
  Judgment --> LastWords: guilty
  Judgment --> Cleared: innocent
  LastWords --> Lynch
  Cleared --> [*]: teardown full_clear
  Lynch --> [*]: teardown full_clear
```

Tribunal teardown (``teardown_tribunal``):

| Mode | When | Clears ``vote_in_progress`` |
|------|------|-----------------------------|
| ``live_vote_finally`` | ``!vote`` ``finally`` | yes, if still set |
| ``full_clear`` | abort, guilty early exit, bailout | no — caller uses ``_release_tribunal_trial_lock`` |
| ``last_words_only`` | end of last-words phase | no |
"""

from typing import Literal

TribunalTeardownMode = Literal["live_vote_finally", "full_clear", "last_words_only"]

# ==========================================
# VOTE / TRIBUNAL
# ==========================================
def _parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        raw = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0)
    except Exception:
        return None


class _ChanCtx:
    """Minimal ctx for ``Game.start_night`` when only ``channel`` / ``guild`` are required."""

    __slots__ = ("channel", "guild", "bot")

    def __init__(self, channel: discord.TextChannel):
        self.channel = channel
        self.guild = channel.guild
        self.bot = bot

    async def send(self, *args, **kwargs):
        return await self.channel.send(*args, **kwargs)


def _tribunal_resolved_judgments_int(game: Game) -> Dict[int, str]:
    raw = getattr(game, "tribunal_resolved_judgments", None) or {}
    out: Dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def _persist_tribunal_judgment_snapshot(
    game: Game,
    resolved_judgments: Dict[int, Optional[str]],
    guilty_votes: int,
    innocent_votes: int,
    mayor_voted: bool,
) -> None:
    snap: Dict[str, str] = {}
    for uid, vote_type in resolved_judgments.items():
        if vote_type == "✅":
            snap[str(uid)] = "guilty"
        elif vote_type == "❌":
            snap[str(uid)] = "innocent"
        elif vote_type is None:
            snap[str(uid)] = "abstain"
    game.tribunal_resolved_judgments = snap
    game.tribunal_guilty_vote_count = guilty_votes
    game.tribunal_innocent_vote_count = innocent_votes
    game.tribunal_mayor_voted = mayor_voted


async def _build_eligible_haunt_voters(
    game: Game,
    guild: discord.Guild,
    defendant_id: int,
    living_ids: List[int],
    resolved: Dict[int, str],
) -> List[discord.Member]:
    voters: List[discord.Member] = []
    for uid in living_ids:
        if uid == defendant_id:
            continue
        vote = resolved.get(uid)
        if vote == "innocent":
            continue
        if vote in ("guilty", "abstain"):
            m = await game.get_member_safe(guild, uid)
            if m:
                voters.append(m)
    return voters


async def _release_tribunal_trial_lock(game: Game) -> None:
    """End the day-phase trial lock (live vote() finally, or terminal tribunal paths)."""
    game.vote_in_progress = False
    try:
        await game.persist_flush()
    except Exception:
        logging.exception("persist_flush failed after releasing tribunal trial lock")


async def teardown_tribunal(
    game: Game,
    guild: Optional[discord.Guild] = None,
    *,
    mode: TribunalTeardownMode,
    defendant: Optional[discord.Member] = None,
    stand_role: Optional[discord.Role] = None,
    day_vc: Optional[discord.abc.GuildChannel] = None,
    alive_role: Optional[discord.Role] = None,
    current_day: Optional[int] = None,
    restore_town_speak: bool = True,
) -> None:
    """Unified tribunal teardown — one implementation, explicit modes.

    - ``live_vote_finally``: ``!vote`` ``finally`` — stand role, partial field clear, may release trial lock.
    - ``full_clear``: terminal abort / guilty early exit — last-words VC cleanup + full persisted reset.
    - ``last_words_only``: end of last-words timer — defendant muted, optional town speak restore.
    """
    if mode == "last_words_only":
        if guild is None:
            raise ValueError("teardown_tribunal(last_words_only) requires guild")
        if day_vc and defendant:
            try:
                await day_vc.set_permissions(defendant, speak=False)
            except discord.HTTPException:
                pass
        game.tribunal_last_words_deadline_utc = None
        if restore_town_speak and day_vc and alive_role and getattr(game, "tribunal_muted", False):
            try:
                await day_vc.set_permissions(alive_role, speak=True)
            except discord.HTTPException:
                pass
            game.tribunal_muted = False
        await game.persist_flush()
        return

    if mode == "full_clear":
        if guild is None:
            raise ValueError("teardown_tribunal(full_clear) requires guild")
        await teardown_tribunal(
            game,
            guild,
            mode="last_words_only",
            defendant=defendant,
            day_vc=day_vc,
            alive_role=alive_role,
            restore_town_speak=True,
        )
        game.tribunal_state.clear_persisted(keep_vote_in_progress=True)
        await game.persist_flush()
        return

    if mode == "live_vote_finally":
        if defendant is not None and stand_role is not None:
            try:
                if stand_role in getattr(defendant, "roles", []):
                    await defendant.remove_roles(stand_role)
            except discord.HTTPException:
                pass
        if (
            current_day is not None
            and game.is_active("day")
            and game.day_number == current_day
            and day_vc
            and alive_role
            and getattr(game, "tribunal_muted", False)
        ):
            try:
                await day_vc.set_permissions(alive_role, speak=True)
            except discord.HTTPException:
                pass
        game.tribunal_state.clear_persisted(keep_vote_in_progress=True)
        if game.vote_in_progress:
            await _release_tribunal_trial_lock(game)
        return

    raise ValueError(f"unknown tribunal teardown mode: {mode!r}")


async def _cleanup_live_vote_finally(
    game: Game,
    *,
    defendant: Optional[discord.Member],
    stand_role: Optional[discord.Role],
    day_vc: Optional[discord.abc.GuildChannel],
    alive_role: Optional[discord.Role],
    current_day: int,
) -> None:
    """Single teardown for live ``!vote`` (early exit, cancel, or after judgment)."""
    await teardown_tribunal(
        game,
        mode="live_vote_finally",
        defendant=defendant,
        stand_role=stand_role,
        day_vc=day_vc,
        alive_role=alive_role,
        current_day=current_day,
    )


async def _clear_tribunal_vote_state(
    game: Game,
    guild: discord.Guild,
    *,
    defendant: Optional[discord.Member] = None,
    day_vc: Optional[discord.abc.GuildChannel] = None,
    alive_role: Optional[discord.Role] = None,
) -> None:
    await teardown_tribunal(
        game,
        guild,
        mode="full_clear",
        defendant=defendant,
        day_vc=day_vc,
        alive_role=alive_role,
    )


async def _tribunal_last_words_cleanup(
    game: Game,
    guild: discord.Guild,
    defendant: Optional[discord.Member],
    day_vc: Optional[discord.abc.GuildChannel],
    alive_role: Optional[discord.Role],
    *,
    restore_town_speak: bool = True,
) -> None:
    await teardown_tribunal(
        game,
        guild,
        mode="last_words_only",
        defendant=defendant,
        day_vc=day_vc,
        alive_role=alive_role,
        restore_town_speak=restore_town_speak,
    )


async def _finish_guilty_tribunal(
    channel: discord.TextChannel,
    game: Game,
    defendant: discord.Member,
    current_day: int,
    *,
    stand_role: Optional[discord.Role] = None,
) -> None:
    """Shared guilty verdict path: lynch death, EXE line, night transition (live + resume)."""
    guild = channel.guild
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None

    if not game.is_active("day") or game.day_number != current_day:
        await _clear_tribunal_vote_state(
            game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
        )
        await _release_tribunal_trial_lock(game)
        return

    if getattr(game, "tribunal_lynch_finisher_done", False):
        await _release_tribunal_trial_lock(game)
        return

    if stand_role is None and game.stand_role_id:
        stand_role = guild.get_role(game.stand_role_id)
    if stand_role and stand_role in defendant.roles:
        try:
            await defendant.remove_roles(stand_role)
        except discord.HTTPException:
            pass

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    resolved = _tribunal_resolved_judgments_int(game)
    eligible_haunt_voters = await _build_eligible_haunt_voters(
        game, guild, defendant.id, living_ids, resolved
    )
    if defendant.id in await game.get_living_ids(guild):
        await game.process_death(channel, defendant, "lynch", eligible_haunt_voters)

    from night_guilt import apply_guilt_and_haunt_deaths

    await apply_guilt_and_haunt_deaths(game, guild, channel, night_kill_deaths=set())

    if await game.check_win_conditions():
        game.tribunal_lynch_finisher_done = True
        await _clear_tribunal_vote_state(
            game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
        )
        await _release_tribunal_trial_lock(game)
        return

    game.tribunal_lynch_finisher_done = True
    await _clear_tribunal_vote_state(
        game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
    )
    await _release_tribunal_trial_lock(game)

    if not await post_game_channel(game, guild, tos_msg.execution_concluded()):
        logging.warning("execution_concluded post failed guild_id=%s", guild.id)
    await game.start_night(_ChanCtx(channel))


async def _tribunal_last_words_phase(
    channel: discord.TextChannel,
    game: Game,
    defendant: discord.Member,
    *,
    day_vc: Optional[discord.abc.GuildChannel],
    alive_role: Optional[discord.Role],
    current_day: int,
    skip_open: bool = False,
) -> None:
    """Last Words before lynch death (Spec 2); deadline-based wait for resume parity."""
    guild = channel.guild
    game.tribunal_subphase = "last_words"
    if not game.tribunal_last_words_deadline_utc:
        lw_end = (datetime.now(timezone.utc) + timedelta(seconds=20)).replace(microsecond=0)
        game.tribunal_last_words_deadline_utc = lw_end.isoformat()
    if not skip_open and not getattr(game, "tribunal_last_words_open_posted", False):
        if await post_game_channel(
            game,
            guild,
            tos_msg.last_words_open(tos_msg.format_player(game, defendant.id)),
        ):
            game.tribunal_last_words_open_posted = True
    await game.persist_flush()

    if day_vc:
        try:
            await day_vc.set_permissions(defendant, speak=True)
        except discord.HTTPException:
            pass

    deadline = _parse_iso_utc(game.tribunal_last_words_deadline_utc)
    if deadline:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    else:
        remaining = 20.0
    await asyncio.sleep(max(0.0, remaining))

    if not game.is_active("day") or game.day_number != current_day:
        await _clear_tribunal_vote_state(
            game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
        )
        await _release_tribunal_trial_lock(game)
        return

    await _tribunal_last_words_cleanup(
        game, guild, defendant, day_vc, alive_role, restore_town_speak=False
    )


def _refund_tribunal_daily_if_consumed(game: Game) -> None:
    """Decrement ``votes_today`` when a tribunal aborts after someone reached the stand.

    ``votes_today`` is incremented when a defendant is chosen (before defense/judgment).
    If judgment never completes (deleted message, fetch error, forced phase change, or
    restart abort), refund so the town does not lose a trial day for no verdict.
    """
    try:
        cur = int(getattr(game, "votes_today", 0))
    except (TypeError, ValueError):
        cur = 0
    game.votes_today = max(0, cur - 1)


async def _abort_tribunal_during_judgment(
    game: Game,
    guild: discord.Guild,
    defendant: discord.Member,
    *,
    refund: bool = True,
) -> None:
    """Refund (optional) and fully clear tribunal state after judgment-phase failure."""
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    if refund:
        _refund_tribunal_daily_if_consumed(game)
    await _clear_tribunal_vote_state(
        game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
    )
    await _release_tribunal_trial_lock(game)


async def _tribunal_run_judgment_deadline_and_tally(
    channel: discord.TextChannel,
    game: Game,
    defendant: discord.Member,
    current_day: int,
) -> None:
    """After judgment window: fetch reactions, announce verdict, lynch or spare (shared live + resume)."""
    guild = channel.guild

    if await _abort_tribunal_if_game_channel_unavailable(
        game, guild, defendant, notice_channel=channel
    ):
        return

    if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
        await _abort_tribunal_during_judgment(game, guild, defendant)
        return

    jmid = getattr(game, "tribunal_judgment_message_id", None)
    if jmid is None:
        await _abort_tribunal_during_judgment(game, guild, defendant)
        return

    tally_ch = game_text_channel(game, guild) or channel
    try:
        judgment_msg = await tally_ch.fetch_message(int(jmid))
    except discord.NotFound:
        await post_game_channel(
            game, guild, "The judgment message was deleted — cancelling the trial."
        )
        await _abort_tribunal_during_judgment(game, guild, defendant)
        return
    except discord.HTTPException as e:
        logging.warning(
            "Tribunal judgment fetch_message failed (channel_id=%s msg_id=%s): %r",
            getattr(tally_ch, "id", None),
            jmid,
            e,
        )
        await post_game_channel(
            game, guild, "⚠️ Trial cancelled — couldn't retrieve the judgment message."
        )
        await _abort_tribunal_during_judgment(game, guild, defendant)
        return

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if defendant.id not in living_ids:
        await post_game_channel(
            game, guild, "The trial has been cancelled — the defendant is no longer alive."
        )
        await _abort_tribunal_during_judgment(game, guild, defendant)
        return

    guilty_votes, innocent_votes = 0, 0
    user_reacts: Dict[int, Set[str]] = {}
    mayor_voted = False

    for reaction in judgment_msg.reactions:
        if str(reaction.emoji) not in {"✅", "❌"}:
            continue
        async for user in reaction.users():
            if user.id != bot.user.id and user.id in living_ids and user.id != defendant.id:
                user_reacts.setdefault(user.id, set()).add(str(reaction.emoji))

    resolved_judgments: Dict[int, Optional[str]] = {}
    for uid, reacts in user_reacts.items():
        if "✅" in reacts and "❌" in reacts:
            resolved_judgments[uid] = None
        elif "✅" in reacts:
            resolved_judgments[uid] = "✅"
        elif "❌" in reacts:
            resolved_judgments[uid] = "❌"
        else:
            resolved_judgments[uid] = None

    for uid, vote_type in resolved_judgments.items():
        if not vote_type:
            continue
        is_mayor = game.player_roles.get(uid) == "Mayor" and bool(game.role_states.get(uid, {}).get("is_revealed"))
        if is_mayor:
            mayor_voted = True
        weight = 2 if is_mayor else 1

        if vote_type == "✅":
            guilty_votes += weight
        elif vote_type == "❌":
            innocent_votes += weight

    game.tribunal_judgment_deadline_utc = None
    game.tribunal_judgment_message_id = None

    recap_lines: List[str] = []
    for uid in resolved_judgments:
        if uid == defendant.id:
            continue
        vote_type = resolved_judgments.get(uid)
        voter = await game.get_member_safe(guild, uid)
        if not voter:
            continue
        vname = tos_msg.format_player(game, uid)
        if vote_type == "✅":
            recap_lines.append(tos_msg.judgment_voted_guilty(vname))
        elif vote_type == "❌":
            recap_lines.append(tos_msg.judgment_voted_innocent(vname))
        else:
            recap_lines.append(tos_msg.judgment_abstained(vname))

    if recap_lines:
        for i in range(0, len(recap_lines), 12):
            await post_game_channel(game, guild, *recap_lines[i : i + 12])

    def_name = tos_msg.format_player(game, defendant.id)
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None

    if guilty_votes > innocent_votes:
        _persist_tribunal_judgment_snapshot(
            game, resolved_judgments, guilty_votes, innocent_votes, mayor_voted
        )
        await post_game_channel(
            game, guild, tos_msg.verdict_lynch(def_name, guilty_votes, innocent_votes)
        )
        if mayor_voted:
            await post_game_channel(game, guild, tos_msg.mayor_double_vote_note())
        game.tribunal_subphase = "last_words"
        lw_end = (datetime.now(timezone.utc) + timedelta(seconds=20)).replace(microsecond=0)
        game.tribunal_last_words_deadline_utc = lw_end.isoformat()
        game.tribunal_verdict_committed = True
        await game.persist_flush()

        await _tribunal_last_words_phase(
            channel,
            game,
            defendant,
            day_vc=day_vc,
            alive_role=alive_role,
            current_day=current_day,
        )

        if not game.is_active("day") or game.day_number != current_day:
            await _clear_tribunal_vote_state(
                game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
            )
            await _release_tribunal_trial_lock(game)
            return
        if defendant.id not in await game.get_living_ids(guild):
            await _clear_tribunal_vote_state(
                game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
            )
            await _release_tribunal_trial_lock(game)
            return

        stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
        await _finish_guilty_tribunal(
            channel, game, defendant, current_day, stand_role=stand_role
        )
    else:
        game.tribunal_verdict_committed = True
        await game.persist_flush()
        await post_game_channel(
            game, guild, tos_msg.verdict_pardon(def_name, innocent_votes, guilty_votes)
        )
        if mayor_voted:
            await post_game_channel(game, guild, tos_msg.mayor_double_vote_note())

        if await game.check_win_conditions():
            if day_vc and alive_role and getattr(game, "tribunal_muted", False):
                try:
                    await day_vc.set_permissions(alive_role, speak=True)
                except discord.HTTPException:
                    pass
            game.tribunal_state.clear_persisted(
                keep_verdict_committed=True,
                keep_vote_in_progress=True,
            )
            await _release_tribunal_trial_lock(game)
            return

        stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
        if stand_role:
            try:
                await defendant.remove_roles(stand_role)
            except discord.HTTPException:
                pass
        if day_vc and alive_role:
            try:
                await day_vc.set_permissions(alive_role, speak=True)
            except discord.HTTPException:
                pass
        game.tribunal_state.clear_persisted(
            keep_verdict_committed=True,
            keep_vote_in_progress=True,
        )
        await _release_tribunal_trial_lock(game)


async def _complete_tribunal_after_defense(
    channel: discord.TextChannel,
    game: Game,
    defendant: discord.Member,
    current_day: int,
    alive_role: Optional[discord.Role],
    stand_role: Optional[discord.Role],
    day_vc: Optional[discord.abc.GuildChannel],
) -> None:
    """Post-defense → judgment → verdict (B4); shared by live ``!vote`` and restart resume."""
    guild = channel.guild
    if getattr(game, "tribunal_verdict_committed", False):
        return

    if await _abort_tribunal_if_game_channel_unavailable(
        game, guild, defendant, notice_channel=channel
    ):
        return

    await game.sync_living_players(guild)
    living_ids = await game.get_living_ids(guild)
    if defendant.id not in living_ids:
        _refund_tribunal_daily_if_consumed(game)
        await game.persist_flush()
        await post_game_channel(
            game,
            guild,
            "The trial has been cancelled — the defendant is no longer alive.",
        )
        return

    if stand_role:
        try:
            await defendant.remove_roles(stand_role)
        except discord.HTTPException:
            pass

    # CR15 — keep town muted in VC until verdict cleanup releases speak.

    game.tribunal_defense_deadline_utc = None
    j_end = (datetime.now(timezone.utc) + timedelta(seconds=30)).replace(microsecond=0)
    game.tribunal_judgment_deadline_utc = j_end.isoformat()
    game.tribunal_subphase = "judgment"
    await game.persist_flush()

    j_embed = discord.Embed(
        title=f"⚖️ JUDGMENT: {defendant.display_name.upper()} ⚖️",
        description="✅ — Guilty\n❌ — Innocent\n*(30 seconds)*",
        color=discord.Color.dark_red(),
    )
    _ok, judgment_msg = await post_game_channel_embed(game, guild, j_embed)
    if not _ok or judgment_msg is None:
        _refund_tribunal_daily_if_consumed(game)
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=channel,
            notice="⚠️ **Trial cancelled:** could not post judgment (game channel missing or deleted).",
        )
        return
    try:
        await judgment_msg.add_reaction("✅")
        await judgment_msg.add_reaction("❌")
    except (discord.Forbidden, discord.HTTPException):
        pass

    game.tribunal_judgment_message_id = judgment_msg.id
    await game.persist_flush()

    await asyncio.sleep(30)
    judgment_ch = game_text_channel(game, guild) or channel
    await _tribunal_run_judgment_deadline_and_tally(judgment_ch, game, defendant, current_day)


async def _bailout_tribunal_resume(
    game: Game,
    guild: discord.Guild,
    *,
    channel: Optional[discord.TextChannel] = None,
    refund: bool = True,
    notice: str = "⚠️ **Trial cancelled:** could not resume after restart.",
) -> None:
    """Release trial lock when a resume path cannot continue."""
    if refund:
        _refund_tribunal_daily_if_consumed(game)
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    did = getattr(game, "tribunal_defendant_id", None)
    defendant: Optional[discord.Member] = None
    if did is not None:
        defendant = await game.get_member_safe(guild, int(did))
    await _clear_tribunal_vote_state(
        game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
    )
    await _release_tribunal_trial_lock(game)
    if notice:
        await post_game_channel(game, guild, notice)


async def _abort_tribunal_if_game_channel_unavailable(
    game: Game,
    guild: discord.Guild,
    defendant: discord.Member,
    *,
    notice_channel: Optional[discord.TextChannel] = None,
    refund: bool = True,
) -> bool:
    """CR10 — configured game channel missing/deleted: full tribunal teardown."""
    ch_id = getattr(game, "game_channel_id", None)
    if not ch_id:
        return False
    if game_text_channel(game, guild) is not None:
        return False
    game.game_channel_id = None
    try:
        await game.persist_flush()
    except Exception:
        logging.exception("persist_flush failed clearing stale game_channel_id")
    await _bailout_tribunal_resume(
        game,
        guild,
        channel=notice_channel,
        refund=refund,
        notice="⚠️ **Trial cancelled:** the game channel was deleted or is unreachable.",
    )
    return True


async def _resume_tribunal_last_words_after_restart(guild: discord.Guild, remaining: float) -> None:
    """Resume Last Words timer after bot restart."""
    game = active_games.get(ALLOWED_GUILD_ID)
    if not game or not game.vote_in_progress:
        return
    if getattr(game, "tribunal_verdict_committed", False) and int(
        getattr(game, "tribunal_guilty_vote_count", 0) or 0
    ) <= int(getattr(game, "tribunal_innocent_vote_count", 0) or 0):
        return
    if getattr(game, "tribunal_subphase", None) != "last_words":
        game.tribunal_subphase = "last_words"
        try:
            await game.persist_flush()
        except Exception:
            pass
    ch = bot.get_channel(game.game_channel_id)
    if not isinstance(ch, discord.TextChannel):
        await _bailout_tribunal_resume(
            game,
            guild,
            notice="⚠️ **Trial cancelled:** game channel missing after restart (last words).",
        )
        return
    did = getattr(game, "tribunal_defendant_id", None)
    if not did:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant missing after restart (last words).",
        )
        return
    defendant = await game.get_member_safe(guild, int(did))
    if not defendant:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant could not be loaded after restart (last words).",
        )
        return
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    current_day = game.day_number

    deadline = _parse_iso_utc(game.tribunal_last_words_deadline_utc)
    if deadline:
        remaining = max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
    else:
        remaining = max(0.0, remaining)

    await _tribunal_last_words_phase(
        ch,
        game,
        defendant,
        day_vc=day_vc,
        alive_role=alive_role,
        current_day=current_day,
        skip_open=getattr(game, "tribunal_last_words_open_posted", False),
    )

    if not game.is_active("day") or game.day_number != current_day:
        await _release_tribunal_trial_lock(game)
        return
    if defendant.id not in await game.get_living_ids(guild):
        await _clear_tribunal_vote_state(
            game, guild, defendant=defendant, day_vc=day_vc, alive_role=alive_role
        )
        await _release_tribunal_trial_lock(game)
        return
    stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
    await _finish_guilty_tribunal(
        ch, game, defendant, current_day, stand_role=stand_role
    )


async def _resume_tribunal_judgment_after_restart(guild: discord.Guild, remaining: float) -> None:
    """Sleep until judgment deadline then tally (B4 resume; mirrors defense resume)."""
    await asyncio.sleep(max(0.0, remaining))
    game = active_games.get(ALLOWED_GUILD_ID)
    if not game or not game.vote_in_progress:
        return
    if getattr(game, "tribunal_subphase", None) != "judgment":
        return
    if getattr(game, "tribunal_verdict_committed", False):
        return
    ch = bot.get_channel(game.game_channel_id)
    if not isinstance(ch, discord.TextChannel):
        await _bailout_tribunal_resume(
            game,
            guild,
            notice="⚠️ **Trial cancelled:** game channel missing after restart (judgment).",
        )
        return
    did = getattr(game, "tribunal_defendant_id", None)
    if not did:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant missing after restart (judgment).",
        )
        return
    defendant = await game.get_member_safe(guild, int(did))
    if not defendant:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant could not be loaded after restart (judgment phase).",
        )
        return

    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    current_day = game.day_number
    try:
        await ch.send("⚖️ **Trial resumed** after bot restart — tallying judgment votes.")
    except discord.HTTPException:
        pass
    await _tribunal_run_judgment_deadline_and_tally(ch, game, defendant, current_day)


async def _resume_tribunal_defense_after_restart(guild: discord.Guild, remaining: float) -> None:
    """Sleep remaining defense time then continue tribunal (B4 resume path).

    Belt-and-braces: short-circuit if a verdict was already committed by a
    previous instance (or if the defense subphase has already advanced),
    so a duplicate restart spawn cannot double-fire judgment messages.
    """
    await asyncio.sleep(max(0.0, remaining))
    game = active_games.get(ALLOWED_GUILD_ID)
    if not game or not game.vote_in_progress:
        return
    if getattr(game, "tribunal_subphase", None) != "defense":
        return
    if getattr(game, "tribunal_verdict_committed", False):
        return
    ch = bot.get_channel(game.game_channel_id)
    if not isinstance(ch, discord.TextChannel):
        await _bailout_tribunal_resume(
            game,
            guild,
            notice="⚠️ **Trial cancelled:** game channel missing after restart (defense).",
        )
        return
    did = getattr(game, "tribunal_defendant_id", None)
    if not did:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant missing after restart (defense).",
        )
        return
    defendant = await game.get_member_safe(guild, int(did))
    if not defendant:
        await _bailout_tribunal_resume(
            game,
            guild,
            channel=ch,
            notice="⚠️ **Trial cancelled:** defendant could not be loaded after restart.",
        )
        return
    alive_role = guild.get_role(game.alive_role_id) if game.alive_role_id else None
    stand_role = guild.get_role(game.stand_role_id) if game.stand_role_id else None
    day_vc = guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    current_day = game.day_number
    try:
        await ch.send("⚖️ **Trial resumed** after bot restart — continuing where the defense phase left off.")
    except discord.HTTPException:
        pass
    await _complete_tribunal_after_defense(ch, game, defendant, current_day, alive_role, stand_role, day_vc)


@bot.command()
@commands.guild_only()
@commands.check(enforce_allowed_guild_check)
async def vote(ctx: commands.Context, target_number: Optional[int] = None) -> None:
    game = get_game_for_guild(ctx.guild.id, allowed_guild_id=ALLOWED_GUILD_ID)
    if not game.in_progress:
        return await safe_reply(ctx, "🛑 No game is in progress.")
    from game import night_resolve_in_progress

    if night_resolve_in_progress(game):
        return await safe_reply(ctx,
            "🛑 **Night resolution in progress.** Wait for `!resolve` to finish."
        )
    if game.phase != "day":
        return await safe_reply(ctx, "🛑 Tribunal can only be used during the **day** phase.")

    is_gm = any(r.id == GAME_OVERSEER_ROLE_ID for r in ctx.author.roles)
    if target_number:
        # Non-GMs get a simple hint; don't do any lookups.
        if not is_gm:
            return await safe_reply(ctx,"*(Use the Game Overseer's `!vote` command to run the Tribunal.)*")
        target = await game.get_target_from_input(ctx, target_number)
        if target:
            await safe_reply(ctx, "*(Use the Tribunal UI to cast votes.)*")
        return

    if not is_gm:
        return await safe_reply(ctx,"Only the Game Overseer can initiate the Tribunal!")

    if game.votes_today >= VOTE_LIMIT_PER_DAY:
        return await safe_reply(ctx,"🛑 **The town is exhausted.** No more trials today.")
    alive_role = ctx.guild.get_role(game.alive_role_id) if game.alive_role_id else None
    stand_role = ctx.guild.get_role(game.stand_role_id) if game.stand_role_id else None
    day_vc = ctx.guild.get_channel(game.day_vc_id) if game.day_vc_id else None
    
    current_day = game.day_number

    async with game._tribunal_start_lock:
        if game.vote_in_progress:
            return await safe_reply(ctx,"A vote is already underway!")
        game.vote_in_progress = True

    defendant: Optional[discord.Member] = None
    try:
            await game.sync_living_players(ctx.guild)
            living_ids = await game.get_living_ids(ctx.guild)

            def _ga_bind_nomination_locked(game: Game, player: discord.Member) -> bool:
                st = game.role_states.get(player.id, {}) or {}
                try:
                    lock_day = st.get("ga_trial_lock_day")
                    if lock_day is None:
                        return False
                    return int(lock_day) == int(game.day_number)
                except (TypeError, ValueError):
                    return False

            emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟","🇦","🇧","🇨","🇩","🇪"]
            ordered_living = [p for p in game.ordered_living_players() if not _ga_bind_nomination_locked(game, p)]
            nominees = {emojis[i]: p for i, p in enumerate(ordered_living) if i < len(emojis)}

            embed = discord.Embed(
                title="⚖️ NOMINATION PHASE ⚖️",
                description=f"Vote to put someone on trial. ({VOTE_DURATION} seconds)",
                color=discord.Color.dark_gold()
            )
            embed.add_field(
                name="Living Players",
                value="\n".join(
                    [
                        f"{e} — #{game.player_slots.get(p.id, '?')} {p.mention} ({p.display_name})"
                        for e, p in nominees.items()
                    ]
                ),
                inline=False
            )

            poll_ok, poll = await post_game_channel_embed(
                game,
                ctx.guild,
                embed,
                allowed_mentions=discord.AllowedMentions(users=list(nominees.values())),
            )
            if not poll_ok or poll is None:
                return await safe_reply(
                    ctx,
                    "🛑 Could not post the nomination poll to the game channel. "
                    "Check `game_channel_id` and bot permissions (Send Messages / Add Reactions).",
                )
            game_channel = game_text_channel(game, ctx.guild)
            if game_channel is None:
                return await safe_reply(ctx, "🛑 Game channel is not configured — cannot run tribunal.")
            try:
                for e in nominees:
                    await poll.add_reaction(e)
            except (discord.Forbidden, discord.HTTPException):
                return await safe_reply(
                    ctx,
                    "🛑 I couldn't add reactions on the game channel. Check Add Reactions permission.",
                )

            await asyncio.sleep(VOTE_DURATION)

            if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
                await safe_reply(
                    ctx,
                    "⚖️ **Nomination cancelled** — the day phase changed or the trial was interrupted before nominations closed.",
                )
                return

            try:
                poll = await game_channel.fetch_message(poll.id)
            except discord.NotFound:
                await safe_reply(
                    ctx,
                    "⚖️ **Nomination cancelled** — the nomination poll was deleted before nominations closed.",
                )
                return

            # Refresh living list before tally (admins may slay, users may leave).
            await game.sync_living_players(ctx.guild)
            living_ids = await game.get_living_ids(ctx.guild)

            ordered_living2 = [p for p in game.ordered_living_players() if not _ga_bind_nomination_locked(game, p)]
            votes = {p: 0 for p in ordered_living2}
            user_nomination_votes: Dict[int, Optional[discord.Member]] = {}
            user_nomination_multi: Set[int] = set()
            voters_map = {p: [] for p in ordered_living2}

            for reaction in poll.reactions:
                if reaction.emoji not in nominees:
                    continue
                target = nominees[reaction.emoji]
                async for user in reaction.users():
                    if user.id != bot.user.id and user.id in living_ids and user.id != target.id:
                        if user.id in user_nomination_multi:
                            continue
                        if user.id not in user_nomination_votes:
                            user_nomination_votes[user.id] = target
                        else:
                            # Reacted to multiple nominees -> invalid/abstain for nomination.
                            user_nomination_multi.add(user.id)
                            user_nomination_votes[user.id] = None

            for uid, target in user_nomination_votes.items():
                if target and target.id in living_ids and target in votes:
                    weight = (
                        2
                        if game.player_roles.get(uid) == "Mayor" and game.role_states.get(uid, {}).get("is_revealed")
                        else 1
                    )
                    votes[target] += weight
                    m = await game.get_member_safe(ctx.guild, uid)
                    if m:
                        voters_map[target].append(m)

            max_votes = max(votes.values()) if votes else 0
            if max_votes == 0:
                return await safe_reply(ctx,"The town remains silent. No one is put on trial.")

            top = [p for p, v in votes.items() if v == max_votes]
            if len(top) > 1:
                return await safe_reply(ctx,"The nomination resulted in a tie! No one takes the stand.")

            defendant = top[0]
            game.votes_today += 1
            await game.persist_flush()

            if await _abort_tribunal_if_game_channel_unavailable(
                game, ctx.guild, defendant, notice_channel=ctx.channel
            ):
                return

            nom_voters = voters_map.get(defendant) or []
            if nom_voters:
                voter_parts: List[str] = []
                for v in nom_voters:
                    voter_parts.append(await tos_msg.format_player_async(game, ctx.guild, v.id))
                voter_names = ", ".join(voter_parts)
                defendant_line = await tos_msg.format_player_async(game, ctx.guild, defendant.id)
                await post_game_channel(
                    game,
                    ctx.guild,
                    tos_msg.nomination_recap(voter_names, defendant_line),
                )

            await post_game_channel(
                game,
                ctx.guild,
                tos_msg.tribunal_stand_open(defendant.mention),
                allowed_mentions=discord.AllowedMentions(users=[defendant]),
            )
            game.tribunal_defendant_id = defendant.id
            if day_vc and alive_role:
                try:
                    await day_vc.set_permissions(alive_role, speak=False)
                except discord.HTTPException:
                    pass
                game.tribunal_muted = True
                await game.persist_flush()

            if stand_role:
                try:
                    await defendant.add_roles(stand_role)
                except discord.HTTPException:
                    pass

            defense_end = (datetime.now(timezone.utc) + timedelta(seconds=45)).replace(microsecond=0)
            game.tribunal_defense_deadline_utc = defense_end.isoformat()
            game.tribunal_subphase = "defense"
            game.tribunal_verdict_committed = False
            game.tribunal_lynch_finisher_done = False
            game.tribunal_last_words_open_posted = False
            await game.persist_flush()

            await asyncio.sleep(45)
            if not game.is_active("day") or not game.vote_in_progress or game.day_number != current_day:
                _refund_tribunal_daily_if_consumed(game)
                return

            await _complete_tribunal_after_defense(ctx.channel, game, defendant, current_day, alive_role, stand_role, day_vc)

    finally:
        await _cleanup_live_vote_finally(
            game,
            defendant=defendant,
            stand_role=stand_role,
            day_vc=day_vc,
            alive_role=alive_role,
            current_day=current_day,
        )


