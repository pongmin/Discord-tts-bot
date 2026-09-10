"""Offline tests for champion_emoji.py; no real Discord login or HTTP calls."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

import champion_data
import champion_emoji


def _emoji(name, id):
    # SimpleNamespace, not MagicMock(name=..., id=...): MagicMock's "name"
    # constructor kwarg sets the mock's own repr name, not a real .name
    # attribute, so it would silently break emoji.name lookups below.
    return SimpleNamespace(name=name, id=id)


class ChampionEmojiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.cache_path = Path(self._tmpdir.name) / "discord_emojis.json"
        self.cache_patch = patch.object(champion_emoji, "CACHE_PATH", self.cache_path)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        # Reset the in-memory cache between tests; module-level state persists
        # across tests in the same process otherwise.
        champion_emoji._cache = None
        self.addCleanup(setattr, champion_emoji, "_cache", None)

        self.known_ids_patch = patch.object(champion_data, "known_champion_ids", return_value=[1, 2, 3])
        self.image_key_patch = patch.object(
            champion_data, "champion_image_key", side_effect=lambda cid: {1: "Jax", 2: "Kayn", 3: "Zed"}.get(cid)
        )
        self.version_patch = patch.object(champion_data, "current_version", return_value="16.18.1")
        for p in (self.known_ids_patch, self.image_key_patch, self.version_patch):
            p.start()
            self.addCleanup(p.stop)

        self.fetch_image_patch = patch.object(champion_emoji, "_fetch_champion_image", AsyncMock(return_value=b"fake-png-bytes"))
        self.fetch_image_patch.start()
        self.addCleanup(self.fetch_image_patch.stop)

        self.sleep_patch = patch("asyncio.sleep", AsyncMock())
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def bot(self, existing_emojis=(), created_id_start=9000):
        bot = MagicMock()
        bot.fetch_application_emojis = AsyncMock(return_value=list(existing_emojis))
        counter = iter(range(created_id_start, created_id_start + 100))

        async def _create(*, name, image):
            return _emoji(name, next(counter))

        bot.create_application_emoji = AsyncMock(side_effect=_create)
        return bot

    async def test_emoji_markup_without_cache_raises(self):
        with self.assertRaises(champion_emoji.ChampionEmojiNotLoadedError):
            champion_emoji.emoji_markup(1)

    async def test_sync_creates_missing_emojis_and_persists_cache(self):
        bot = self.bot()
        summary = await champion_emoji.sync_champion_emojis(bot)

        self.assertEqual(summary, {"created": 3, "reused": 0, "total": 3})
        self.assertEqual(bot.create_application_emoji.await_count, 3)
        self.assertTrue(self.cache_path.exists())

        champion_emoji._cache = None  # force a fresh disk read
        self.assertEqual(champion_emoji.emoji_markup(1), "<:champ_1:9000>")
        self.assertEqual(champion_emoji.emoji_markup(2), "<:champ_2:9001>")
        self.assertEqual(champion_emoji.emoji_markup(3), "<:champ_3:9002>")

    async def test_sync_reuses_existing_remote_emoji_without_reupload(self):
        # champion 1 already exists on Discord under the expected name, even
        # though our local cache file doesn't know about it (e.g. lost cache).
        bot = self.bot(existing_emojis=[_emoji("champ_1", 555)])
        summary = await champion_emoji.sync_champion_emojis(bot)

        self.assertEqual(summary, {"created": 2, "reused": 1, "total": 3})
        created_names = {call.kwargs["name"] for call in bot.create_application_emoji.await_args_list}
        self.assertNotIn("champ_1", created_names)
        self.assertEqual(champion_emoji.emoji_markup(1), "<:champ_1:555>")

    async def test_sync_skips_champions_already_in_local_cache_without_checking_remote(self):
        self.cache_path.write_text(json.dumps({"1": {"name": "champ_1", "id": 111}}), encoding="utf-8")
        bot = self.bot()

        summary = await champion_emoji.sync_champion_emojis(bot)

        self.assertEqual(summary, {"created": 2, "reused": 1, "total": 3})
        created_names = {call.kwargs["name"] for call in bot.create_application_emoji.await_args_list}
        self.assertEqual(created_names, {"champ_2", "champ_3"})
        self.assertEqual(champion_emoji.emoji_markup(1), "<:champ_1:111>")

    async def test_force_reuploads_everything(self):
        self.cache_path.write_text(json.dumps({"1": {"name": "champ_1", "id": 111}}), encoding="utf-8")
        bot = self.bot(existing_emojis=[_emoji("champ_1", 555)])

        summary = await champion_emoji.sync_champion_emojis(bot, force=True)

        self.assertEqual(summary["created"], 3)
        self.assertEqual(bot.create_application_emoji.await_count, 3)

    async def test_upload_failure_for_one_champion_does_not_abort_the_rest(self):
        bot = self.bot()

        async def _create(*, name, image):
            if name == "champ_2":
                raise discord.HTTPException(MagicMock(status=400), "bad request")
            return _emoji(name, 42)

        bot.create_application_emoji = AsyncMock(side_effect=_create)
        summary = await champion_emoji.sync_champion_emojis(bot)

        self.assertEqual(summary, {"created": 2, "reused": 0, "total": 2})
        champion_emoji._cache = None  # force a fresh disk read
        self.assertIsNone(champion_emoji.emoji_markup(2))
        self.assertEqual(champion_emoji.emoji_markup(1), "<:champ_1:42>")


if __name__ == "__main__":
    unittest.main()
