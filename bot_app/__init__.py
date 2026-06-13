"""Discord bot package — root `bot.py` loads this package."""

from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay

from bot_app import bootstrap as _bootstrap  # noqa: F401
from bot_app import shared as _shared  # noqa: F401
from bot_app import ui as _ui  # noqa: F401
from bot_app import gm as _gm  # noqa: F401
from bot_app import tribunal as _tribunal  # noqa: F401
from bot_app import players as _players  # noqa: F401
from bot_app import night_cmds as _night_cmds  # noqa: F401

__all__ = ["bot", "ALLOWED_GUILD_ID", "only_during_night_gameplay"]
