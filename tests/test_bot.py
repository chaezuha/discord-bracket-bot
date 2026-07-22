from types import SimpleNamespace
from unittest.mock import AsyncMock

import bot as bot_module


class FakeTree:
    def __init__(self):
        self.calls = []

    def add_command(self, command):
        self.calls.append(("add", command.name))

    async def sync(self, *, guild=None):
        self.calls.append(("sync", None if guild is None else guild.id))

    def copy_global_to(self, *, guild):
        self.calls.append(("copy", guild.id))


async def test_dev_setup_syncs_global_commands_before_guild_copy(monkeypatch):
    tree = FakeTree()
    fake_bot = SimpleNamespace(
        config=SimpleNamespace(db_path=":memory:", dev_guild_id=1234),
        tree=tree,
        db=None,
        add_dynamic_items=lambda *items: tree.calls.append(("dynamic", len(items))),
        add_cog=AsyncMock(),
    )
    monkeypatch.setattr(bot_module.db, "connect", AsyncMock(return_value="DB"))

    await bot_module.BracketBot.setup_hook(fake_bot)

    assert ("sync", None) in tree.calls
    assert tree.calls.index(("sync", None)) < tree.calls.index(("copy", 1234))
    assert tree.calls[-1] == ("sync", 1234)
