"""Offline command integration checks; no Discord login or Riot requests."""

import asyncio
import hashlib
import itertools
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import discord
from discord.ext import commands

from scouting import ban_commands as command
from clash.clash_commands import setup_clash_commands
from riot.riot_api import RiotAccount, PlayerNotFoundError
from scouting import scouting_db as db
from scouting.scouting_job_manager import ScoutingJobManager


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
        # A fresh manager per test: the real one is a process-wide singleton,
        # and dedupe state (same team = same key) must not bleed across tests.
        self.jobs = ScoutingJobManager()
        self.patches = [
            patch.object(command, "get_account_by_riot_id", self.accounts),
            patch.object(command, "get_league_entries", self.ranks),
            patch.object(command, "collect_player_matches", self.collect),
            patch.object(command, "scouting_jobs", self.jobs),
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
        # Mirror scouting_db.get_connection()'s busy_timeout: concurrent-team
        # tests open multiple connections against the same shared in-memory DB.
        conn.execute("PRAGMA busy_timeout = 5000")
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
        before = hashlib.sha256(Path("scouting/ban_algorithm.py").read_bytes()).digest()
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
        self.assertNotIn("100.00%", report)
        self.assertIn("rho=-1", report)
        self.assertEqual(hashlib.sha256(Path("scouting/ban_algorithm.py").read_bytes()).digest(), before)

    async def test_bad_format_and_duplicate_input_before_api(self):
        for raw in ("missingtag", "#tag", "name#", "name#tag#extra"):
            with self.subTest(raw=raw), self.assertRaisesRegex(command.BanCommandError, "BOTTOM.*입력 오류"):
                await command.prepare_opponents(self.inputs | {"bottom": raw}, self.progress)
        self.accounts.assert_not_awaited()
        with self.assertRaisesRegex(command.BanCommandError, "중복.*TOP, BOTTOM"):
            await command.prepare_opponents(self.inputs | {"bottom": " player0#test "}, self.progress)

    async def test_duplicate_puuid_before_collection(self):
        self.accounts.side_effect = lambda name, tag: RiotAccount("same", name, tag)
        with self.assertRaisesRegex(command.BanCommandError, "중복.*TOP, JUNGLE"):
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
                with self.assertRaisesRegex(command.BanCommandError, "TOP 수집 실패.*Player0#TEST.*솔로 랭크"):
                    await command.prepare_opponents(self.inputs, self.progress)
                self.ranks.assert_not_awaited()

    async def test_missing_role_data_is_explicit(self):
        self.collect.side_effect = None
        self.collect.return_value = dict(player_id=1, failed=0, aborted=False, is_complete=True)
        with self.assertRaisesRegex(command.BanCommandError, "TOP 데이터 부족.*Player0#TEST"):
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

    def _bot_with_ban_commands(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.addAsyncCleanup(bot.close)
        setup_clash_commands(bot)
        clash = bot.tree.get_command("clashlookup")
        command.setup_ban_commands(bot)
        self.assertIs(bot.tree.get_command("clashlookup"), clash)
        return bot.tree.get_command("banrecommend")

    _next_message_id = itertools.count(10_000_000)

    @classmethod
    def _interaction(cls, user_id: int = 123, guild_id: int = 555):
        channel = SimpleNamespace(id=999, send=AsyncMock())
        # channel.send must return something with a real int .id: the
        # persistent-report code path stores it as a SQLite PK
        # (scouting_db.save_ban_report), which a bare MagicMock id can't bind.
        channel.send.side_effect = lambda *a, **kw: SimpleNamespace(id=next(cls._next_message_id))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=user_id), guild_id=guild_id, channel=channel,
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        return interaction, channel

    async def test_command_registration_shape(self):
        slash = self._bot_with_ban_commands()
        self.assertEqual([p.name for p in slash.parameters], list(command.ROLE_INPUTS) + ["depth"])
        self.assertTrue(all(p.required for p in slash.parameters[:5]))
        depth_param = slash.parameters[-1]
        self.assertFalse(depth_param.required)
        self.assertEqual(depth_param.default, "normal")
        self.assertEqual([c.value for c in depth_param.choices], ["quick", "normal", "deep"])

    async def test_bad_input_replies_immediately_without_starting_a_job(self):
        slash = self._bot_with_ban_commands()
        interaction, channel = self._interaction()
        await slash.callback(interaction, **(self.inputs | {"bottom": "badformat"}))
        interaction.response.send_message.assert_awaited_once()
        call = interaction.response.send_message.call_args
        self.assertIn("입력 오류", call.args[0])
        self.assertTrue(call.kwargs.get("ephemeral"))
        self.accounts.assert_not_awaited()
        await asyncio.sleep(0)
        channel.send.assert_not_awaited()

    async def test_command_returns_immediately_then_job_delivers_result_to_channel(self):
        slash = self._bot_with_ban_commands()
        interaction, channel = self._interaction()

        await slash.callback(interaction, **self.inputs)

        # The command itself must not block on collection: it acknowledges
        # right away, and nothing has been sent to the channel yet.
        interaction.response.send_message.assert_awaited_once()
        ack = interaction.response.send_message.call_args
        self.assertIn("분석을 시작했습니다", ack.args[0])
        self.assertFalse(ack.kwargs.get("ephemeral", False))
        channel.send.assert_not_awaited()
        self.accounts.assert_not_awaited()  # background job hasn't run a tick yet

        key = command._team_key(command.parse_inputs(self.inputs))
        job = self.jobs.get(key)
        self.assertIsNotNone(job)
        await job.task  # let the background job run to completion

        self.assertEqual(job.status.value, "completed")
        self.assertEqual(self.collect.await_count, 10)
        channel.send.assert_awaited_once()
        sent = channel.send.call_args.kwargs
        view = sent["view"]
        self.addCleanup(view.stop)
        self.assertEqual(view.page_index, 0)
        self.assertIn("TOP", sent["embed"].title)
        self.assertIsNotNone(view.message)

        # Enough is persisted (message_id -> opponents+cutoff, not the computed
        # Recommendation itself) that navigation would survive a bot restart.
        row = db.get_ban_report(self.anchor, view.message.id)
        self.assertIsNotNone(row)
        self.assertEqual(row["owner_id"], 123)
        self.assertEqual(row["page_index"], 0)
        self.assertEqual(len(db.ban_report_opponents(row)), 5)

    async def test_failure_is_reported_to_the_channel_with_the_failing_player_and_stage(self):
        slash = self._bot_with_ban_commands()
        interaction, channel = self._interaction()
        self.accounts.side_effect = PlayerNotFoundError("계정 없음")

        await slash.callback(interaction, **self.inputs)
        key = command._team_key(command.parse_inputs(self.inputs))
        job = self.jobs.get(key)
        await job.task  # ScoutingJobManager swallows the exception into job.error

        self.assertEqual(job.status.value, "failed")
        self.assertIn("계정 없음", job.error)
        self.collect.assert_not_awaited()
        channel.send.assert_awaited_once()
        message = channel.send.call_args.args[0]
        self.assertIn("TOP 계정 조회 (Player0#TEST)", message)
        self.assertIn("계정 없음", message)

    async def test_duplicate_team_job_is_rejected_but_different_teams_are_not(self):
        slash = self._bot_with_ban_commands()
        interaction1, channel = self._interaction(user_id=1)
        interaction2, _ = self._interaction(user_id=2)
        interaction2.channel = channel

        gate = asyncio.Event()

        async def blocked(*args, **kwargs):
            await gate.wait()
            return await self.collect_success(*args, **kwargs)

        self.collect.side_effect = blocked

        await slash.callback(interaction1, **self.inputs)
        await asyncio.sleep(0)  # let the background job start and reach the gate

        # Same team (even from a different user) while the first is still running.
        await slash.callback(interaction2, **self.inputs)
        interaction2.response.send_message.assert_awaited_once_with(
            "이미 같은 팀을 분석 중입니다.", ephemeral=True
        )

        # A different team must not be blocked by the first team's in-flight job.
        other_inputs = self.inputs | {"top": "Different0#TEST"}
        interaction3, _ = self._interaction(user_id=3)
        interaction3.channel = channel
        await slash.callback(interaction3, **other_inputs)
        interaction3.response.send_message.assert_awaited_once()
        self.assertIn("분석을 시작했습니다", interaction3.response.send_message.call_args.args[0])

        gate.set()
        key1 = command._team_key(command.parse_inputs(self.inputs))
        key3 = command._team_key(command.parse_inputs(other_inputs))
        await self.jobs.get(key1).task
        await self.jobs.get(key3).task
        self.assertEqual(self.jobs.get(key1).status.value, "completed")
        self.assertEqual(self.jobs.get(key3).status.value, "completed")

    async def test_job_reuses_existing_cache_on_a_second_run(self):
        slash = self._bot_with_ban_commands()
        interaction, channel = self._interaction()

        await slash.callback(interaction, **self.inputs)
        key = command._team_key(command.parse_inputs(self.inputs))
        await self.jobs.get(key).task
        self.assertEqual(self.collect.await_count, 10)

        # Same team again, after the first job completed: cached matches are
        # reused, so collection is not re-awaited.
        self.collect.reset_mock()
        self.accounts.reset_mock()
        await slash.callback(interaction, **self.inputs)
        await self.jobs.get(key).task
        self.collect.assert_not_awaited()
        self.assertEqual(channel.send.await_count, 2)

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
        from scouting.ban_algorithm import recommend_bans as real_recommend
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
        self.assertIn("unseen-champion Others estimate", report)
        self.assertEqual(report.count("only 2 role-filtered games available"), 5)

    async def test_invalid_depth_does_not_start_lookup(self):
        with self.assertRaisesRegex(command.BanCommandError, "수집 깊이"):
            await command.prepare_opponents(self.inputs, self.progress, "automatic")
        self.accounts.assert_not_awaited()

    def test_algorithm_errors_are_displayed_in_korean(self):
        message = command._error_text(ValueError("At least 3 distinct candidate champions are required for a 3-ban search."))
        self.assertIn("3종 미만", message)
        self.assertNotIn("candidate", message)

    async def test_persistent_router_is_registered_at_setup(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.addAsyncCleanup(bot.close)
        command.setup_ban_commands(bot)
        custom_ids = {
            child.custom_id
            for view in bot.persistent_views
            for child in view.children
            if getattr(child, "custom_id", None)
        }
        self.assertIn(command.ScoutingReportView.PREV_CUSTOM_ID, custom_ids)
        self.assertIn(command.ScoutingReportView.NEXT_CUSTOM_ID, custom_ids)

    @staticmethod
    def _find_button(view, label):
        return next(child for child in view.children if getattr(child, "label", None) == label)

    @staticmethod
    def _router_interaction(user_id: int, message_id: int):
        return SimpleNamespace(
            user=SimpleNamespace(id=user_id), message=SimpleNamespace(id=message_id),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def _send_one_report(self):
        """Runs a full successful job and returns (owner_id, message_id)."""
        slash = self._bot_with_ban_commands()
        interaction, channel = self._interaction()
        await slash.callback(interaction, **self.inputs)
        key = command._team_key(command.parse_inputs(self.inputs))
        await self.jobs.get(key).task
        view = channel.send.call_args.kwargs["view"]
        self.addCleanup(view.stop)
        return interaction.user.id, view.message.id

    async def test_persistent_router_revives_a_report_after_a_restart(self):
        owner_id, message_id = await self._send_one_report()

        # A brand-new router with zero in-memory state, as if the process had
        # just restarted and lost the original ScoutingReportView instance -
        # only scouting.db (ban_reports) still knows about this message.
        router = command.PersistentBanReportRouter()
        self.addCleanup(router.stop)
        interaction = self._router_interaction(owner_id, message_id)

        await self._find_button(router, "다음").callback(interaction)

        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once()
        revived = interaction.edit_original_response.call_args.kwargs["view"]
        self.addCleanup(revived.stop)
        self.assertEqual(revived.page_index, 1)
        self.assertIn("JUNGLE", interaction.edit_original_response.call_args.kwargs["embed"].title)

        row = db.get_ban_report(self.anchor, message_id)
        self.assertEqual(row["page_index"], 1)

    async def test_persistent_router_rejects_non_owner(self):
        owner_id, message_id = await self._send_one_report()
        router = command.PersistentBanReportRouter()
        self.addCleanup(router.stop)
        interaction = self._router_interaction(owner_id + 1, message_id)

        await self._find_button(router, "다음").callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        interaction.response.defer.assert_not_awaited()
        row = db.get_ban_report(self.anchor, message_id)
        self.assertEqual(row["page_index"], 0)

    async def test_persistent_router_handles_a_report_it_never_saved(self):
        router = command.PersistentBanReportRouter()
        self.addCleanup(router.stop)
        interaction = self._router_interaction(123, 999999999)

        await self._find_button(router, "이전").callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("다시 실행", interaction.response.send_message.call_args.args[0])
        interaction.response.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
