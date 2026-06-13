"""Night action commands — factory-registered + role-specific handlers."""

from __future__ import annotations

import asyncio
import random
from typing import Dict, List, Optional, Set

import discord
from discord.ext import commands

from config import ALL_MAFIA_ROLES, DUEL_DURATION
from faction_taxonomy import deputy_gun_evil_neutral_roles
from engine import combat as combat_tiers
from game import Game, active_games, get_game_by_player_id
import messages.tos as tos_msg
from messages.delivery import post_game_channel
from bot_app.instance import bot, only_during_night_gameplay
from bot_app.players import _private_or_dm_action_surface_ok
from bot_app.shared import (  # noqa: F401
    _GUILD_UNAVAILABLE_MSG,
    _autocomplete_living_slot,
    _extra_mole_investigate_ok,
    _extra_vig_shoot_ok,
    _sync_living_ids_for_action,
    safe_reply,
)
from bot_app.night_factory import register_standard_night_commands, reject_wrong_night_role


def _deputy_gun_sees_evil(game: Game, pid: int) -> bool:
    from engine.night import tamper_subject_for_submitted_slot

    subject = tamper_subject_for_submitted_slot(game, pid)
    if game.player_roles.get(subject) in ALL_MAFIA_ROLES:
        return True
    if game.player_roles.get(subject) in deputy_gun_evil_neutral_roles():
        return True
    if game.role_states.get(subject, {}).get("is_framed"):
        return True
    try:
        return int(subject) in game.doused_players
    except (TypeError, ValueError):
        return False


def _deputy_shot_blocked_by_defense(game: Game, pid: int) -> bool:
    """True when the Deputy's unstoppable daytime shot fails to pierce passive/vest defense."""
    return not combat_tiers.deputy_shot_would_kill_target(game, pid)


# Back-compat alias for monte_carlo bridge imports.
_deputy_target_basic_defense = _deputy_shot_blocked_by_defense


# Repetitive commands (kill, roleblock, watch, transport, vest, …).
register_standard_night_commands()


class _ChanCtx:
    """Minimal channel wrapper for Game.process_death (Deputy day shot)."""

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel

# ==========================================
# PLAYER NIGHT ACTION COMMANDS (custom)
# ==========================================


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def stab(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Serial Killer",)):
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    game.role_states.setdefault(ctx.author.id, {})["sk_target_id"] = target.id
    await game.set_night_action(ctx, {"type": "sk_kill", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx,f"You prepare to attack **{target.display_name}** tonight.")


@stab.autocomplete("target_number")
async def stab_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Serial Killer",))


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def ward(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Guardian Angel",)):
        return
    bind_raw = game.role_states.get(ctx.author.id, {}).get("ga_target_id")
    try:
        bind_id = int(bind_raw) if bind_raw is not None else None
    except (TypeError, ValueError):
        bind_id = None
    if bind_id is None:
        return await safe_reply(ctx,"You have no bind to ward.")
    if bool(game.role_states.get(ctx.author.id, {}).get("ga_defeated")):
        return await safe_reply(ctx, tos_msg.ga_ward_defeated())
    if int(game.role_states.get(ctx.author.id, {}).get("ga_ward_charges", 0)) <= 0:
        return await safe_reply(ctx,"You have no ward charge remaining.")
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    if target.id != bind_id:
        return await safe_reply(ctx,"You may only ward your bound player.")
    await game.set_night_action(ctx, {"type": "ward", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx,f"You ward **{target.display_name}** tonight.")


@ward.autocomplete("target_number")
async def ward_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Guardian Angel",))


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def cautious(ctx: commands.Context) -> None:
    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.in_progress:
        await safe_reply(ctx, "No active game found for you.")
        return
    if not await _private_or_dm_action_surface_ok(ctx, game):
        return
    if await reject_wrong_night_role(ctx, game, ("Serial Killer",)):
        return
    st = game.role_states.setdefault(ctx.author.id, {})
    st["sk_cautious"] = not bool(st.get("sk_cautious"))
    if st["sk_cautious"]:
        await safe_reply(ctx,"You are now **Cautious** — you will not counter-attack roleblockers.")
    else:
        await safe_reply(ctx,"You are now **Aggressive** — roleblockers who visit you may be counter-attacked.")


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def gaze(ctx: commands.Context, target_number: int, second_target: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Seer",)):
        return
    t1 = await game.get_target_from_input(ctx, target_number)
    t2 = await game.get_target_from_input(ctx, second_target)
    if not t1 or not t2:
        return
    if t1.id == t2.id:
        return await safe_reply(ctx,"You must choose two different players.")
    await game.set_night_action(
        ctx,
        {"type": "gaze", "targets": [t1.id, t2.id], "actor": ctx.author.id},
    )
    await safe_reply(ctx,f"You gaze upon **{t1.display_name}** and **{t2.display_name}** tonight.")


@gaze.autocomplete("target_number")
async def gaze_first_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Seer",))


@gaze.autocomplete("second_target")
async def gaze_second_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Seer",))


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def heal(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Doctor",)):
        return
    target = await game.get_target_from_input(ctx, target_number, allow_self=True)
    if not target:
        return
    if game.role_states.get(target.id, {}).get("is_revealed"):
        return await safe_reply(ctx,"Cannot heal a revealed Mayor!")
    if target.id == ctx.author.id and game.role_states.get(ctx.author.id, {}).get("self_heals_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have already used your self-heal!")

    await game.set_night_action(ctx, {"type": "heal", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx,f"Healing **{target.display_name}**.")


@heal.autocomplete("target_number")
async def heal_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Doctor",))


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def investigate(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Sheriff", "Investigator", "Mole")):
        return
    role = game.player_roles.get(ctx.author.id)
    if role == "Mole" and game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have no investigations left.")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(
        ctx, {"type": "investigate", "target": target.id, "role": role, "actor": ctx.author.id}
    )
    await safe_reply(ctx,f"Investigating **{target.display_name}**.")


@investigate.autocomplete("target_number")
async def investigate_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(
        interaction,
        current,
        allowed_roles=("Sheriff", "Investigator", "Mole"),
        extra_ok=_extra_mole_investigate_ok,
    )


@only_during_night_gameplay()
async def _vig_shoot_night(ctx: commands.Context, target_number: int) -> None:
    """Vigilante night branch (decorator supplies ``ctx.game`` and living sync)."""
    game = ctx.game
    if game.player_roles.get(ctx.author.id) != "Vigilante":
        return await safe_reply(ctx, "🛑 Only the **Vigilante** uses `!shoot` at night.")
    if game.role_states.get(ctx.author.id, {}).get("shots_remaining", 0) <= 0:
        return await safe_reply(ctx, "No bullets left!")
    if game.role_states.get(ctx.author.id, {}).get("will_die_of_guilt"):
        return await safe_reply(ctx, "You are overcome with guilt.")
    if game.role_states.get(ctx.author.id, {}).get("guilty_tomorrow"):
        return await safe_reply(ctx, "You are overcome with guilt.")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    await game.set_night_action(ctx, {"type": "shoot", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx, f"Aimed at **{target.display_name}**.")


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
async def shoot(ctx: commands.Context, target_number: int) -> None:
    """**Night:** Vigilante `!shoot <slot>`. **Day:** Deputy revolver (Day 2+, once per day, DM/private)."""
    game = get_game_by_player_id(ctx.author.id)
    if not game or not game.in_progress:
        await safe_reply(ctx, "No active game found for you.")
        return

    if not await _private_or_dm_action_surface_ok(ctx, game):
        return

    living_ids = await _sync_living_ids_for_action(ctx, game)
    if living_ids is None:
        await safe_reply(ctx, _GUILD_UNAVAILABLE_MSG)
        return

    role = game.player_roles.get(ctx.author.id)

    if game.phase == "day":
        if role != "Deputy":
            return await safe_reply(ctx,"🛑 **Shoot** during the day is only for the Deputy.")
        if getattr(game, "vote_in_progress", False):
            return await safe_reply(ctx,"🛑 You cannot fire during an active tribunal.")
        from game import night_resolve_in_progress

        if night_resolve_in_progress(game):
            return await safe_reply(ctx,"🛑 Please wait — the game is resolving.")
        if ctx.author.id not in living_ids:
            return await safe_reply(ctx,"🛑 You must be alive to fire.")
        if int(game.day_number) < 2:
            return await safe_reply(ctx,"🛑 You cannot fire before **Day 2**.")
        if game.deputy_fired_today(ctx.author.id):
            return await safe_reply(ctx,"🛑 You have already fired your revolver today.")
        st = game.role_states.setdefault(ctx.author.id, {})
        if int(st.get("deputy_shots_remaining", 0)) <= 0:
            return await safe_reply(ctx,"🛑 You have no ammunition left.")

        target = await game.get_target_from_input(ctx, target_number)
        if not target:
            return
        if target.id == ctx.author.id:
            return await safe_reply(ctx,"🛑 You cannot shoot yourself.")

        gc = bot.get_channel(game.game_channel_id) if game.game_channel_id else None
        if not isinstance(gc, discord.TextChannel):
            return await safe_reply(ctx,"🛑 Game channel is not available; contact a GM.")

        slot = game.player_slots.get(target.id, "?")
        evil = _deputy_gun_sees_evil(game, target.id)
        armored = _deputy_shot_blocked_by_defense(game, target.id)

        tname = tos_msg.format_player(game, target.id)

        async def _consume_deputy_shot() -> None:
            game.mark_deputy_shot_today(ctx.author.id)
            await game.persist_flush()

        if evil and armored:
            if not await post_game_channel(game, gc.guild, tos_msg.deputy_public_defense(tname)):
                return await safe_reply(ctx,"🛑 Could not post to the game channel; shot not fired.")
            await _consume_deputy_shot()
            await safe_reply(ctx, tos_msg.deputy_shot_absorbed())
            return

        if evil and not armored:
            if not await post_game_channel(game, gc.guild, tos_msg.deputy_public_kill(tname)):
                return await safe_reply(ctx,"🛑 Could not post to the game channel; shot not fired.")
            await _consume_deputy_shot()
            await game.process_death(_ChanCtx(gc), target, "deputy_shoot")
            await game.check_win_conditions()
            await safe_reply(ctx, tos_msg.deputy_shot_mark())
            return

        if not await post_game_channel(game, gc.guild, tos_msg.deputy_public_mistake(tname)):
            return await safe_reply(ctx,"🛑 Could not post to the game channel; shot not fired.")
        await _consume_deputy_shot()
        await game.process_death(_ChanCtx(gc), target, "deputy_friendly_fire")
        dep = await game.get_member_safe(gc.guild, ctx.author.id)
        if dep:
            await game.process_death(_ChanCtx(gc), dep, "deputy_friendly_fire_self")
        else:
            await game.process_death_by_id(
                _ChanCtx(gc), gc.guild, ctx.author.id, "deputy_friendly_fire_self"
            )
        await game.check_win_conditions()
        await safe_reply(ctx, tos_msg.deputy_shot_mistake())
        return

    if game.phase != "night":
        return await safe_reply(ctx, "🛑 **Shoot** is only used at night (Vigilante) or during the day (Deputy).")
    if role != "Vigilante":
        return await safe_reply(ctx, "🛑 Only the **Vigilante** uses `!shoot` at night.")
    return await _vig_shoot_night(ctx, target_number)


@shoot.autocomplete("target_number")
async def shoot_target_autocomplete(interaction: discord.Interaction, current: str):
    game = get_game_by_player_id(interaction.user.id)
    if not game or not game.in_progress:
        return []
    role = game.player_roles.get(interaction.user.id)
    if game.phase == "day" and role == "Deputy":
        return await _autocomplete_living_slot(interaction, current, allowed_roles=("Deputy",))
    return await _autocomplete_living_slot(
        interaction, current, allowed_roles=("Vigilante",), extra_ok=_extra_vig_shoot_ok
    )


@bot.hybrid_command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def protect(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Bodyguard",)):
        return
    target = await game.get_target_from_input(ctx, target_number, allow_self=True)
    if not target:
        return

    from night_action_eligibility import (
        bodyguard_off_self_protect_eligible,
        bodyguard_self_vest_eligible,
    )

    if target.id == ctx.author.id:
        if not bodyguard_self_vest_eligible(game, ctx.author.id):
            return await safe_reply(ctx,"You have already used your self-protection!")
        await game.set_night_action(ctx, {"type": "bg_vest", "target": target.id, "actor": ctx.author.id})
        return await safe_reply(ctx,"Using a bulletproof vest tonight. 🦺")
    if not bodyguard_off_self_protect_eligible(game, ctx.author.id):
        return await safe_reply(ctx,"You have already used your protection on someone else!")

    await game.set_night_action(ctx, {"type": "protect", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx,f"Protecting **{target.display_name}** tonight.")


@protect.autocomplete("target_number")
async def protect_target_autocomplete(interaction: discord.Interaction, current: str):
    return await _autocomplete_living_slot(interaction, current, allowed_roles=("Bodyguard",))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def doused(ctx: commands.Context) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Arsonist",)):
        return
    if not game.doused_players:
        return await safe_reply(ctx, "Nobody is doused yet.")
    guild = ctx.bot.get_guild(game.guild_id)
    living_ids: Set[int] = set()
    if guild is not None:
        living_ids = {int(x) for x in await game.get_living_ids(guild)}
    lines: List[str] = []
    for pid in sorted(game.doused_players, key=lambda x: int(game.player_slots.get(x, 999))):
        label = await tos_msg.format_player_async(game, guild, int(pid))
        if int(pid) not in living_ids:
            label = f"{label} (dead)"
        lines.append(label)
    await safe_reply(ctx, "**Doused players:**\n" + "\n".join(lines))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def corpses(ctx: commands.Context) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Retributionist",)):
        return
    state = game.role_states.get(ctx.author.id, {})
    if state.get("uses_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have no uses remaining.")
    from reanimate_expand import list_usable_retributionist_corpses

    usable = list_usable_retributionist_corpses(game, retri_player_id=ctx.author.id)
    if not usable:
        return await safe_reply(ctx,"No usable Town corpses are available yet.")
    lines = []
    for i, e in enumerate(usable, start=1):
        pid = e.get("player_id")
        member = (
            await game.get_member_safe(ctx.bot.get_guild(game.guild_id), pid)
            if ctx.bot.get_guild(game.guild_id)
            else None
        )
        name = member.display_name if member else str(pid)
        pid_label = e.get("player_id")
        lines.append(
            f"{i}: {name} ({e.get('real_role')}) — id {pid_label}, died N{e.get('died_day', '?')}"
        )
    await safe_reply(ctx,"**Usable corpses:**\n" + "\n".join(lines))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def reanimate(ctx: commands.Context, corpse_number: int, target1_num: int, target2_num: Optional[int] = None) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Retributionist",)):
        return
    state = game.role_states.get(ctx.author.id, {})
    if state.get("uses_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have no uses remaining.")
    if target2_num is not None:
        return await safe_reply(ctx,"Only one living target is required: `!reanimate <corpse> <slot>`.")

    from reanimate_expand import list_usable_retributionist_corpses

    usable = list_usable_retributionist_corpses(game, retri_player_id=ctx.author.id)

    if corpse_number < 1 or corpse_number > len(usable):
        return await safe_reply(ctx,"Invalid corpse number. Use `!corpses` first.")
    corpse = usable[corpse_number - 1]
    corpse_role = corpse["real_role"]

    t1 = await game.get_target_from_input(ctx, target1_num, allow_self=True)
    if not t1:
        return
    if corpse_role == "Doctor":
        if game.role_states.get(t1.id, {}).get("is_revealed") and game.player_roles.get(t1.id) == "Mayor":
            return await safe_reply(ctx,"Cannot heal a revealed Mayor!")

    action: Dict = {
        "type": "reanimate",
        "actor": ctx.author.id,
        "corpse_player_id": corpse["player_id"],
        "corpse_role": corpse_role,
        "target": t1.id,
    }
    await game.set_night_action(ctx, action)
    await safe_reply(ctx,"You begin your ritual over the graveyard...")


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def hypnotize(ctx: commands.Context, target_number: int, message_type: str) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Hypnotist",)):
        return

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    message_type = message_type.lower()
    valid_types = ["healed", "roleblocked", "transported", "controlled", "attacked"]
    if message_type not in valid_types:
        return await safe_reply(ctx,f"❌ Invalid message type. Use: {', '.join(valid_types)}")

    await game.set_night_action(
        ctx,
        {"type": "hypnotize", "target": target.id, "msg_type": message_type, "actor": ctx.author.id},
    )
    await safe_reply(ctx,tos_msg.hypnotize_ack(message_type, target.display_name))


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def tailor(ctx: commands.Context, target_number: int, *, fake_role: str) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Tailor",)):
        return
    if game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have no uses remaining!")

    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    fake_role = discord.utils.escape_mentions(discord.utils.escape_markdown(fake_role[:20]))
    await game.set_night_action(
        ctx, {"type": "tailor", "target": target.id, "fake_role": fake_role, "actor": ctx.author.id}
    )
    await safe_reply(ctx,f"Altering **{target.display_name}**'s role to '{fake_role}'.")


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def plunder(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Pirate",)):
        return
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return

    duel_token = random.randint(1, 2_000_000_000)
    await game.set_night_action(
        ctx,
        {
            "type": "plunder",
            "target": target.id,
            "actor": ctx.author.id,
            "duel_won": False,
            "duel_finished": False,
            "duel_token": duel_token,
        },
    )
    CHOICES = {"🪨": "rock", "📄": "paper", "✂️": "scissors"}

    async def _plunder_persist_if_live() -> None:
        if (
            game.in_progress
            and not getattr(game, "ending", False)
            and active_games.get(game.guild_id) is game
        ):
            await game.persist_flush()

    async def finish_duel_if_current(*, duel_won: bool) -> None:
        act = game.night_actions.get(ctx.author.id)
        if act and act.get("type") == "plunder" and act.get("duel_token") == duel_token:
            act["duel_won"] = bool(duel_won)
            act["duel_finished"] = True
            act["duel_outcome_ready"] = True
            await _plunder_persist_if_live()

    async def get_choice(player: discord.Member, prompt: str, is_target: bool = False) -> str:
        try:
            msg = await player.send(prompt)
            for emoji in CHOICES:
                await msg.add_reaction(emoji)
            check = lambda r, u: u.id == player.id and str(r.emoji) in CHOICES and r.message.id == msg.id
            reaction, _ = await bot.wait_for("reaction_add", timeout=DUEL_DURATION, check=check)
            return CHOICES[str(reaction.emoji)]
        except (asyncio.TimeoutError, discord.Forbidden):
            return "TIMEOUT_TARGET" if is_target else "TIMEOUT_PIRATE"

    try:
        results = await asyncio.gather(
            get_choice(ctx.author, "⚔️ Choose your weapon for the plunder! (30s)"),
            get_choice(target, "⚔️ You are being plundered! Choose your weapon. (30s)", is_target=True),
            return_exceptions=True,
        )
    except Exception:
        await finish_duel_if_current(duel_won=False)
        raise

    if not game.in_progress:
        await finish_duel_if_current(duel_won=False)
        return

    game_chan = bot.get_channel(game.game_channel_id)
    if not game_chan:
        await finish_duel_if_current(duel_won=False)
        return

    await game.sync_living_players(game_chan.guild)
    living_ids = await game.get_living_ids(game_chan.guild)
    if target.id not in living_ids:
        await finish_duel_if_current(duel_won=False)
        return await safe_reply(ctx,"The duel was cancelled — your target died or left during the night.")

    pirate_choice = results[0] if not isinstance(results[0], Exception) else "TIMEOUT_PIRATE"
    target_choice = results[1] if not isinstance(results[1], Exception) else "TIMEOUT_TARGET"

    if pirate_choice == "TIMEOUT_PIRATE":
        pirate_choice = random.choice(list(CHOICES.values()))
    if target_choice == "TIMEOUT_TARGET":
        target_choice = random.choice(list(CHOICES.values()))

    winner = None
    if (pirate_choice, target_choice) in [("rock", "scissors"), ("scissors", "paper"), ("paper", "rock")]:
        winner = ctx.author
    elif pirate_choice != target_choice:
        winner = target

    result_msg = f"Pirate chose **{pirate_choice}**, Target chose **{target_choice}**. "
    if winner == ctx.author:
        result_msg += "The Pirate wins the duel! 🏴‍☠️"
    elif winner == target:
        result_msg += "The Target wins the duel!"
    else:
        result_msg += "It's a draw!"

    await finish_duel_if_current(duel_won=(winner == ctx.author))

    for p in [ctx.author, target]:
        try:
            await p.send(result_msg)
        except discord.Forbidden:
            pass


@bot.command()
@commands.cooldown(1, 2, commands.BucketType.user)
@only_during_night_gameplay()
async def guard(ctx: commands.Context, target_number: int) -> None:
    game = ctx.game
    if await reject_wrong_night_role(ctx, game, ("Gatekeeper",)):
        return
    if game.role_states.get(ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return await safe_reply(ctx,"You have no guard uses remaining!")
    target = await game.get_target_from_input(ctx, target_number)
    if not target:
        return
    if target.id == ctx.author.id:
        return await safe_reply(ctx,"🛑 You cannot guard yourself.")
    if game.player_roles.get(target.id) in ALL_MAFIA_ROLES:
        return await safe_reply(ctx,"🛑 You cannot guard another Mafia member.")
    from engine.night import gatekeeper_back_to_back_rejects

    if gatekeeper_back_to_back_rejects(game, int(ctx.author.id), int(target.id)):
        return await safe_reply(ctx,"🛑 You cannot guard the same player two nights in a row.")
    await game.set_night_action(ctx, {"type": "guard", "target": target.id, "actor": ctx.author.id})
    await safe_reply(ctx,f"Guarding **{target.display_name}**'s location tonight.")
