"""Entry point: load config from .env, connect the database, run the bot."""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import logging.handlers
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bracketbot import db
from bracketbot.cog import BracketCog, help_command
from bracketbot.config import Config, ConfigError, load_config
from bracketbot.views import VoteButton

log = logging.getLogger("bot")

# Held open for the life of the process: faulthandler writes to this fd on a
# hard crash, so it must never be garbage-collected and closed.
_faulthandler_file = None


def setup_logging(log_dir: str) -> None:
    """Log to stderr plus a rotating file under LOG_DIR."""
    discord.utils.setup_logging(root=True)

    global _faulthandler_file
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        # Same format discord.utils.setup_logging puts on stderr, so the two
        # outputs correlate line for line.
        file_handler.setFormatter(
            logging.Formatter(
                "[{asctime}] [{levelname:<8}] {name}: {message}",
                "%Y-%m-%d %H:%M:%S",
                style="{",
            )
        )
        logging.getLogger().addHandler(file_handler)
        _faulthandler_file = open(os.path.join(log_dir, "faulthandler.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except OSError as exc:
        log.warning(
            "Cannot write log files under %r (%s); continuing with console logging only",
            log_dir,
            exc,
        )
        faulthandler.enable()


class BracketBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.db = None

    async def setup_hook(self) -> None:
        self.db = await db.connect(self.config.db_path)
        # Vote buttons resolve by custom_id pattern, so every matchup message
        # keeps working across restarts.
        self.add_dynamic_items(VoteButton)
        await self.add_cog(BracketCog(self))
        self.tree.add_command(help_command)
        # DMs and group DMs only receive global commands. Keep those synced
        # even in development, then add the instant guild copy as well.
        await self.tree.sync()
        if self.config.dev_guild_id:
            guild = discord.Object(id=self.config.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            if self.db is not None:
                await self.db.close()


async def main() -> None:
    load_dotenv()
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    setup_logging(config.log_dir)
    async with BracketBot(config) as bot:
        await bot.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
