"""Factory for repetitive night-action Discord commands (phase 4 refactor)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Collection, Optional

import discord
from discord.ext import commands

from bot_app.imports import *  # noqa: F403
from bot_app.instance import bot, only_during_night_gameplay
from bot_app.shared import (
    _autocomplete_living_slot,
    _autocomplete_living_slot_excluding,
    _extra_framer_nights_ok,
    _extra_gravedigger_uses_ok,
    safe_reply,
)

# Populated at import for smoke contract checks (see tests/smoke/checks_engine.py).
AUTOCOMPLETE_MARKERS: list[str] = []
REGISTERED_ACTION_TYPES: dict[str, str] = {}


@dataclass(frozen=True)
class SingleTargetNightCmd:
    name: str
    roles: tuple[str, ...]
    action_type: str
    hybrid: bool = True
    allow_self: bool = False
    ack: Optional[str] = None
    extra_ok: Optional[Callable[[Any, int], bool]] = None


@dataclass(frozen=True)
class DualTargetNightCmd:
    name: str
    roles: tuple[str, ...]
    action_type: str
    param1: str = "target1_num"
    param2: str = "target2_num"
    hybrid: bool = True
    allow_self: bool = False
    ack: Optional[str] = None
    different_message: str = "You must choose two different people."


@dataclass(frozen=True)
class SelfNightCmd:
    name: str
    roles: tuple[str, ...]
    action_type: str
    target_self: bool = True
    ack: Optional[str] = None


ValidateFn = Callable[[commands.Context, Any], Awaitable[Optional[str]]]
PostTargetFn = Callable[[commands.Context, Any, Any], Awaitable[Optional[str]]]
BuildActionFn = Callable[[commands.Context, Any, Any], dict]


def _role_ok(game: Any, author_id: int, roles: Collection[str]) -> bool:
    return game.player_roles.get(author_id) in roles


async def reject_wrong_night_role(
    ctx: commands.Context, game: Any, roles: Collection[str]
) -> bool:
    """Return True when the author cannot use this night command (reply already sent)."""
    if _role_ok(game, ctx.author.id, roles):
        return False
    role = game.player_roles.get(ctx.author.id) or "Unknown"
    cmd = ctx.command
    cmd_label = (
        getattr(cmd, "qualified_name", None) or getattr(cmd, "name", None) or "this command"
    )
    await safe_reply(ctx, f"🛑 Your role (**{role}**) cannot use `!{cmd_label}` at night.")
    return True


def _record_autocomplete(name: str, param: str) -> None:
    AUTOCOMPLETE_MARKERS.append(f'@{name}.autocomplete("{param}")')


def register_single_target_night_command(
    spec: SingleTargetNightCmd,
    *,
    validate: Optional[ValidateFn] = None,
    post_target: Optional[PostTargetFn] = None,
    build_action: Optional[BuildActionFn] = None,
) -> None:
    roles = spec.roles

    async def cmd(ctx: commands.Context, target_number: int) -> None:
        game = ctx.game
        if await reject_wrong_night_role(ctx, game, roles):
            return
        if validate is not None:
            err = await validate(ctx, game)
            if err:
                return await safe_reply(ctx,err)
        target = await game.get_target_from_input(ctx, target_number, allow_self=spec.allow_self)
        if not target:
            return
        if post_target is not None:
            err = await post_target(ctx, game, target)
            if err:
                return await safe_reply(ctx,err)
        if build_action is not None:
            action = build_action(ctx, game, target)
        else:
            action = {"type": spec.action_type, "target": target.id, "actor": ctx.author.id}
        await game.set_night_action(ctx, action)
        if spec.ack:
            await safe_reply(ctx,spec.ack.format(name=target.display_name))

    cmd.__name__ = spec.name
    decorated = commands.cooldown(1, 2, commands.BucketType.user)(cmd)
    decorated = only_during_night_gameplay()(decorated)
    if spec.hybrid:
        command = bot.hybrid_command()(decorated)
    else:
        command = bot.command()(decorated)

    async def ac(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[int]]:
        return await _autocomplete_living_slot(
            interaction, current, allowed_roles=roles, extra_ok=spec.extra_ok
        )

    command.autocomplete("target_number")(ac)
    _record_autocomplete(spec.name, "target_number")
    REGISTERED_ACTION_TYPES[spec.name] = spec.action_type


def register_dual_target_night_command(
    spec: DualTargetNightCmd,
    *,
    validate: Optional[ValidateFn] = None,
) -> None:
    roles = spec.roles
    p1, p2 = spec.param1, spec.param2

    async def cmd(ctx: commands.Context, target1_num: int, target2_num: int) -> None:
        game = ctx.game
        if await reject_wrong_night_role(ctx, game, roles):
            return
        if validate is not None:
            err = await validate(ctx, game)
            if err:
                return await safe_reply(ctx,err)
        t1 = await game.get_target_from_input(ctx, target1_num, allow_self=spec.allow_self)
        t2 = await game.get_target_from_input(ctx, target2_num, allow_self=spec.allow_self)
        if not t1 or not t2:
            return
        if t1.id == t2.id:
            return await safe_reply(ctx,spec.different_message)
        action = {"type": spec.action_type, "targets": [t1.id, t2.id], "actor": ctx.author.id}
        await game.set_night_action(ctx, action)
        if spec.ack:
            await safe_reply(ctx,spec.ack.format(name1=t1.display_name, name2=t2.display_name))

    cmd.__name__ = spec.name
    decorated = commands.cooldown(1, 2, commands.BucketType.user)(cmd)
    decorated = only_during_night_gameplay()(decorated)
    if spec.hybrid:
        command = bot.hybrid_command()(decorated)
    else:
        command = bot.command()(decorated)

    async def ac1(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[int]]:
        return await _autocomplete_living_slot(interaction, current, allowed_roles=roles)

    async def ac2(interaction: discord.Interaction, current: str) -> list[discord.app_commands.Choice[int]]:
        return await _autocomplete_living_slot_excluding(
            interaction, current, exclude_param=p1, allowed_roles=roles
        )

    if spec.hybrid:
        command.autocomplete(p1)(ac1)
        command.autocomplete(p2)(ac2)
        _record_autocomplete(spec.name, p1)
        _record_autocomplete(spec.name, p2)
    REGISTERED_ACTION_TYPES[spec.name] = spec.action_type


def register_self_night_command(
    spec: SelfNightCmd,
    *,
    validate: Optional[ValidateFn] = None,
) -> None:
    roles = spec.roles

    async def cmd(ctx: commands.Context) -> None:
        game = ctx.game
        if await reject_wrong_night_role(ctx, game, roles):
            return
        if validate is not None:
            err = await validate(ctx, game)
            if err:
                return await safe_reply(ctx, err)
        action: dict[str, Any] = {"type": spec.action_type, "actor": ctx.author.id}
        if spec.target_self:
            action["target"] = ctx.author.id
        await game.set_night_action(ctx, action)
        if spec.ack:
            await safe_reply(ctx,spec.ack)

    cmd.__name__ = spec.name
    decorated = commands.cooldown(1, 2, commands.BucketType.user)(cmd)
    decorated = only_during_night_gameplay()(decorated)
    bot.command()(decorated)
    REGISTERED_ACTION_TYPES[spec.name] = spec.action_type


async def _validate_frame(_ctx: commands.Context, game: Any) -> Optional[str]:
    from night_action_eligibility import framer_frame_eligible

    if not framer_frame_eligible(game):
        return "You can only frame on Nights 1 and 2."
    return None


async def _validate_hide(_ctx: commands.Context, game: Any) -> Optional[str]:
    from night_action_eligibility import gravedigger_hide_eligible

    if not gravedigger_hide_eligible(game, _ctx.author.id):
        return "You have no uses remaining!"
    return None


async def _validate_vest(_ctx: commands.Context, game: Any) -> Optional[str]:
    from night_action_eligibility import survivor_vest_eligible

    if not survivor_vest_eligible(game, _ctx.author.id):
        return "You have no vests remaining!"
    return None


async def _validate_alert(_ctx: commands.Context, game: Any) -> Optional[str]:
    from night_action_eligibility import scary_grandma_alert_eligible

    if not scary_grandma_alert_eligible(game, _ctx.author.id):
        return "You have no alerts remaining!"
    return None


async def _validate_chaos(_ctx: commands.Context, game: Any) -> Optional[str]:
    if game.role_states.get(_ctx.author.id, {}).get("uses_remaining", 0) <= 0:
        return "You have no uses remaining."
    return None


def register_standard_night_commands() -> None:
    """Register repetitive night commands (custom commands stay in night_cmds.py)."""
    register_single_target_night_command(SingleTargetNightCmd("kill", ("Mobster",), "kill"))
    register_single_target_night_command(
        SingleTargetNightCmd("roleblock", ("Escort", "Consort"), "roleblock")
    )
    register_single_target_night_command(
        SingleTargetNightCmd("watch", ("Lookout",), "watch", ack="Watching **{name}** tonight.")
    )
    register_single_target_night_command(
        SingleTargetNightCmd("track", ("Tracker",), "track", ack="Tracking **{name}** tonight.")
    )
    register_single_target_night_command(
        SingleTargetNightCmd("douse", ("Arsonist",), "douse", ack="Dousing **{name}**.")
    )
    register_single_target_night_command(
        SingleTargetNightCmd(
            "frame",
            ("Framer",),
            "frame",
            ack="Framing **{name}**.",
            extra_ok=_extra_framer_nights_ok,
        ),
        validate=_validate_frame,
    )
    register_single_target_night_command(
        SingleTargetNightCmd(
            "hide",
            ("Gravedigger",),
            "hide",
            ack="Concealing **{name}**.",
            extra_ok=_extra_gravedigger_uses_ok,
        ),
        validate=_validate_hide,
    )
    register_dual_target_night_command(
        DualTargetNightCmd(
            "transport",
            ("Transporter",),
            "transport",
            allow_self=True,
            ack="Swapping **{name1}** and **{name2}**.",
        )
    )
    register_dual_target_night_command(
        DualTargetNightCmd(
            "control",
            ("Witch",),
            "control",
            ack="Attempting to force **{name1}** to target **{name2}**.",
        )
    )
    register_dual_target_night_command(
        DualTargetNightCmd(
            "chaos",
            ("Chaos",),
            "chaos",
            hybrid=False,
            ack="Reality bends around your choices...",
        ),
        validate=_validate_chaos,
    )
    register_self_night_command(
        SelfNightCmd("vest", ("Survivor",), "vest", ack="Using a protective vest tonight. 🦺"),
        validate=_validate_vest,
    )
    register_self_night_command(
        SelfNightCmd(
            "alert",
            ("Scary Grandma",),
            "alert",
            target_self=False,
            ack="You are on alert tonight. Any visitors will be attacked. 🔫",
        ),
        validate=_validate_alert,
    )
    register_self_night_command(
        SelfNightCmd(
            "ignite",
            ("Arsonist",),
            "ignite",
            target_self=False,
            ack="Igniting all doused players tonight. 🔥",
        )
    )
    register_self_night_command(
        SelfNightCmd(
            "clean",
            ("Arsonist",),
            "clean",
            target_self=False,
            ack="Cleaning gasoline off yourself tonight. 🧼",
        )
    )
