"""Offline command integration checks; no Discord login or Riot requests."""

import asyncio
import hashlib
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord
from discord.ext import commands

import ban_commands as command
from clash_commands import setup_clash_commands
from riot_api import RiotAccount, PlayerNotFoundError
import scouting_db as db


class BanCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.uri = f"file:{uuid4().hex}?mode=memory&cache=shared"
        self.anchor = self.connection()
        db.init_db(self.anchor)
        self.connection_patch = patch.object(db, "get_connection", self.connection)
        self.connection_patch.start()
        self.inputs = {role: f"Player{i}#TEST" for i, role in enumerate(command.ROLE_INPUTS)}
        self.progress = AsyncMock()
        self.accounts = AsyncMock(side_effect=lambda name, tag: RiotAccount(name, name, tag))
        self.ranks = AsyncMock(return_value=[])
        self.collect = AsyncMock(side_effect=self.collect_success)
        self.games_per_fetch = None
        self.patches = [
            patch.object(command, "get_account_by_riot_id", self.accounts),
            patch.object(command, "get_league_entries", self.ranks),
            patch.object(command, "collect_player_matches", self.collect),
        ]
        for mock_patch in self.patches:
            mock_patch.start()

    async def asyncTearDown(self):
        for mock_patch in reversed(self.patches):
            mock_patch.stop()
        self.connection_patch.stop()
        self.anchor.close()

    def connection(self):
        conn = sqlite3.connect(self.uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    async def collect_success(self, puuid, name, tag, *, queue_id, max_count, conn):
        self.assertIn(max_count, (30, 100, 200))
        player_id = db.get_or_create_player(conn, puuid, name, tag)
        role = command.ROLE_INPUTS[int(name[-1])].upper()
        now = db.now_ms()
        for index in range(max_count if self.games_per_fetch is None else self.games_per_fetch):
            match_id = f"{puuid}-{queue_id}-{index}"
            conn.execute(
                "INSERT OR IGNORE INTO matches "
                "(match_id,game_start,game_end,queue_id,raw_file_path,fetched_at) VALUES (?,?,?,?,?,?)",
                (match_id, now - 1000, now, queue_id, "", now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO player_matches "
                "(player_id,match_id,champion_id,champion_name,canonical_role,win) VALUES (?,?,?,?,?,?)",
                (player_id, match_id, int(name[-1]) + 1, f"Champion{name[-1]}", role, 1),
            )
        db.touch_fetch_attempt(conn, player_id, queue_id)
        db.refresh_fetch_state(conn, player_id, queue_id, True)
        return dict(player_id=player_id, failed=0, aborted=False, is_complete=True)

    async def test_success_roles_cache_and_real_algorithm(self):
        before = hashlib.sha256(Path("ban_algorithm.py").read_bytes()).digest()
        opponents = await command.prepare_opponents(self.inputs, self.progress)
        self.assertEqual([role for _, role in opponents], [r.upper() for r in command.ROLE_INPUTS])
        self.assertEqual(self.collect.await_count, 10)
        self.assertEqual(self.ranks.await_count, 5)
        self.assertEqual({c.kwargs["queue_id"] for c in self.collect.await_args_list}, {420, 400})
        self.collect.reset_mock()
        self.ranks.reset_mock()
        self.assertEqual(await command.prepare_opponents(self.inputs, self.progress), opponents)
        self.collect.assert_not_awaited()
        self.ranks.assert_not_awaited()
        # Exercise the real unchanged algorithm and real repo in its worker thread.
        report = await asyncio.to_thread(command._recommend_report, opponents)
        self.assertIn("observed_pool_exhausted=True", report)
        self.assertIn("WARNING:", report)
        self.assertIn("100.00%", report)
        self.assertIn("rho=-1", report)
        self.assertEqual(hashlib.sha256(Path("ban_algorithm.py").read_bytes()).digest(), before)

    async def test_bad_format_and_duplicate_input_before_api(self):
        for raw in ("missingtag", "#tag", "name#", "name#tag#extra"):
            with self.subTest(raw=raw), self.assertRaisesRegex(command.BanCommandError, "bottom.*입력 오류"):
                await command.prepare_opponents(self.inputs | {"bottom": raw}, self.progress)
        self.accounts.assert_not_awaited()
        with self.assertRaisesRegex(command.BanCommandError, "중복.*top, bottom"):
            await command.prepare_opponents(self.inputs | {"bottom": " player0#test "}, self.progress)

    async def test_duplicate_puuid_before_collection(self):
        self.accounts.side_effect = lambda name, tag: RiotAccount("same", name, tag)
        with self.assertRaisesRegex(command.BanCommandError, "중복.*top, jungle"):
            await command.prepare_opponents(self.inputs, self.progress)
        self.collect.assert_not_awaited()

    async def test_all_partial_failure_signals_stop(self):
        for flags in (
            dict(failed=1, aborted=False, is_complete=True),
            dict(failed=0, aborted=True, is_complete=False),
            dict(failed=0, aborted=False, is_complete=False),
        ):
            with self.subTest(flags=flags):
                self.collect.side_effect = None
                self.collect.return_value = dict(player_id=1, **flags)
                with self.assertRaisesRegex(command.BanCommandError, "top 수집 실패.*Player0#TEST.*420"):
                    await command.prepare_opponents(self.inputs, self.progress)
                self.ranks.assert_not_awaited()

    async def test_missing_role_data_is_explicit(self):
        self.collect.side_effect = None
        self.collect.return_value = dict(player_id=1, failed=0, aborted=False, is_complete=True)
        with self.assertRaisesRegex(command.BanCommandError, "top 데이터 부족.*Player0#TEST"):
            await command.prepare_opponents(self.inputs, self.progress)

    async def test_cancelled_refresh_cannot_become_fresh_cache(self):
        await command.prepare_opponents(self.inputs, self.progress, "quick")
        self.anchor.execute("UPDATE player_fetch_state SET last_attempt_at=0")
        self.anchor.commit()

        async def interrupted(puuid, name, tag, *, queue_id, max_count, conn):
            pid = db.get_player_by_puuid(conn, puuid)["id"]
            db.touch_fetch_attempt(conn, pid, queue_id)
            raise asyncio.CancelledError()

        self.collect.side_effect = interrupted
        with self.assertRaises(asyncio.CancelledError):
            await command.prepare_opponents(self.inputs, self.progress)
        state = self.anchor.execute("SELECT * FROM player_fetch_state WHERE player_id=1 AND queue_id=420").fetchone()
        self.assertFalse(state["is_complete"])
        self.assertGreater(state["last_attempt_at"], 0)

    async def test_command_registration_lookup_failure_and_timeout(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.addAsyncCleanup(bot.close)
        setup_clash_commands(bot)
        clash = bot.tree.get_command("clashlookup")
        command.setup_ban_commands(bot)
        self.assertIs(bot.tree.get_command("clashlookup"), clash)
        slash = bot.tree.get_command("banrecommend")
        self.assertEqual([p.name for p in slash.parameters], list(command.ROLE_INPUTS) + ["depth"])
        self.assertTrue(all(p.required for p in slash.parameters[:5]))
        depth_param = slash.parameters[-1]
        self.assertFalse(depth_param.required)
        self.assertEqual(depth_param.default, "normal")
        self.assertEqual([c.value for c in depth_param.choices], ["quick", "normal", "deep"])
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        )
        self.accounts.side_effect = PlayerNotFoundError("계정 없음")
        await slash.callback(interaction, **self.inputs)
        self.assertIn("top 계정 조회 (Player0#TEST)", interaction.edit_original_response.call_args.kwargs["content"])
        self.assertIn("계정 없음", interaction.edit_original_response.call_args.kwargs["content"])
        self.collect.assert_not_awaited()
        initial = interaction.edit_original_response.call_args_list[0].kwargs["content"]
        self.assertIn("depth=normal", initial)
        self.assertIn("100 games/queue per player", initial)
        self.assertIn("min/player", initial)

        async def slow(*args):
            await asyncio.sleep(60)

        with patch.object(command, "prepare_opponents", side_effect=slow), patch.object(command, "COMMAND_TIMEOUT_SECONDS", 0.001):
            await slash.callback(interaction, **self.inputs)
        self.assertIn("처리 시간이 초과", interaction.edit_original_response.call_args.kwargs["content"])
        # The lock was released on failure; a successful run still sends ALL text.
        report = "Recommendation\n" * 200 + "observed_pool_exhausted=True\nWARNING: sensitivity"
        with patch.object(command, "prepare_opponents", return_value=[]), patch.object(command, "_recommend_report", return_value=report):
            await slash.callback(interaction, **self.inputs)
        output = [interaction.edit_original_response.call_args.kwargs["content"]]
        output.extend(call.args[0] for call in interaction.followup.send.call_args_list)
        self.assertEqual("".join(chunk[4:-4] for chunk in output), report)

    async def test_depth_caps_upgrade_and_skip_independent_of_fetch_state(self):
        for depth, cap in (("quick", 30), ("normal", 100), ("deep", 200)):
            with self.subTest(depth=depth):
                self.collect.reset_mock()
                opponents = await command.prepare_opponents(self.inputs, self.progress, depth)
                self.assertEqual(self.collect.await_count, 10)
                self.assertEqual({c.kwargs["max_count"] for c in self.collect.await_args_list}, {cap})
                # Even stale/incomplete fetch metadata must not override enough
                # actual cutoff-filtered data in each player's queue.
                self.anchor.execute("UPDATE player_fetch_state SET is_complete=0, last_attempt_at=0")
                self.anchor.commit()
                self.collect.reset_mock()
                await command.prepare_opponents(self.inputs, self.progress, depth)
                self.collect.assert_not_awaited()
        # Downgrading depth preserves and analyzes every cached observation.
        await command.prepare_opponents(self.inputs, self.progress, "quick")
        self.collect.assert_not_awaited()
        from ban_algorithm import recommend_bans as real_recommend
        models = []

        def capture(repo, opponents):
            result = real_recommend(repo, opponents)
            models.extend(result.players)
            return result

        with patch.object(command, "recommend_bans", side_effect=capture):
            await asyncio.to_thread(command._recommend_report, opponents)
        self.assertEqual([sum(c.games for c in p.champions.values()) for p in models], [400] * 5)

    async def test_queue_counts_use_cutoff_and_all_roles(self):
        await command.prepare_opponents(self.inputs, self.progress, "quick")
        # Off-role observations count towards collection depth, not analysis.
        self.anchor.execute("UPDATE player_matches SET canonical_role='UTILITY' WHERE player_id=1")
        self.anchor.execute("UPDATE player_matches SET canonical_role='TOP' WHERE player_id=1 AND match_id='Player0-420-0'")
        # Exactly one queue is short at cutoff; future and unknown ends don't count.
        self.anchor.execute("UPDATE matches SET game_end=? WHERE match_id='Player1-400-0'", (db.now_ms() + 86400000,))
        self.anchor.execute("UPDATE matches SET game_end=NULL WHERE match_id='Player1-400-1'")
        self.anchor.commit()
        self.collect.reset_mock()
        await command.prepare_opponents(self.inputs, self.progress, "quick")
        self.assertEqual(self.collect.await_count, 1)
        call = self.collect.await_args
        self.assertEqual(call.args[0], "Player1")
        self.assertEqual(call.kwargs["queue_id"], 400)
        self.assertEqual(call.kwargs["max_count"], 30)

    async def test_short_history_warns_without_failing_or_forcing_deeper_fetch(self):
        self.games_per_fetch = 1
        opponents = await command.prepare_opponents(self.inputs, self.progress, "deep")
        self.assertEqual(self.collect.await_count, 10)
        report = await asyncio.to_thread(command._recommend_report, opponents)
        self.assertIn("Player0#TEST/TOP: only 2 role-filtered games available", report)
        self.assertIn("Consider a deeper --depth if more history exists.", report)
        self.assertIn("observed_pool_exhausted=True", report)
        self.assertIn("optimistic", report)
        self.assertEqual(report.count("only 2 role-filtered games available"), 5)

    async def test_invalid_depth_does_not_start_lookup(self):
        with self.assertRaisesRegex(command.BanCommandError, "depth"):
            await command.prepare_opponents(self.inputs, self.progress, "automatic")
        self.accounts.assert_not_awaited()

    def test_chunk_limits_and_all_warnings_preserved(self):
        report = "😀" * 2200 + "\n" + "a\n" * 1500 + "WARNING: sensitivity\nobserved_pool_exhausted=True"
        chunks = command.report_chunks(report)
        self.assertEqual("".join(chunk[4:-4] for chunk in chunks), report)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 2000 for chunk in chunks))
        self.assertTrue(all(chunk.count("```") == 2 for chunk in command.report_chunks("a```b")))


if __name__ == "__main__":
    unittest.main()
