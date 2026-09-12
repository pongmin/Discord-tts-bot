"""Offline saved-team checks: storage, the three commands, and reuse by
/assignroles and /banrecommend. No Discord login or Riot requests."""

import sqlite3
import unittest
from unittest.mock import patch
import discord
from discord.ext import commands

from scouting import ban_commands
from scouting import role_commands
from scouting import scouting_db as db
from scouting import team_commands as command
from tests.test_ban_commands import ScoutingCommandFixture


ROSTER = [f"Player{index}#TEST" for index in range(5)]


class TeamStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)

    def test_save_read_and_replace(self):
        self.assertTrue(db.save_team(self.conn, 1, "ECHO", ROSTER))
        row = db.get_team(self.conn, 1, "ECHO")
        self.assertEqual(db.team_players(row), ROSTER)
        self.assertEqual(row["created_at"], row["updated_at"])

        replacement = [f"New{index}#TEST" for index in range(5)]
        self.assertFalse(db.save_team(self.conn, 1, "ECHO", replacement))
        row = db.get_team(self.conn, 1, "ECHO")
        self.assertEqual(db.team_players(row), replacement)
        # Replacing keeps one row, and keeps when it was first created.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM saved_teams").fetchone()[0], 1
        )
        self.assertLessEqual(row["created_at"], row["updated_at"])

    def test_the_name_is_scoped_to_one_guild(self):
        db.save_team(self.conn, 1, "ECHO", ROSTER)
        other = [f"Other{index}#TEST" for index in range(5)]
        db.save_team(self.conn, 2, "ECHO", other)

        self.assertEqual(db.team_players(db.get_team(self.conn, 1, "ECHO")), ROSTER)
        self.assertEqual(db.team_players(db.get_team(self.conn, 2, "ECHO")), other)
        self.assertIsNone(db.get_team(self.conn, 3, "ECHO"))

    def test_lookup_ignores_case_and_surrounding_space(self):
        db.save_team(self.conn, 1, "  Echo  ", ROSTER)
        row = db.get_team(self.conn, 1, "ECHO")

        self.assertIsNotNone(row)
        # The typed name is preserved for display, trimmed but not case-folded.
        self.assertEqual(row["team_name"], "Echo")
        self.assertIsNotNone(db.get_team(self.conn, 1, "echo"))

    def test_delete_reports_whether_anything_went(self):
        db.save_team(self.conn, 1, "ECHO", ROSTER)
        self.assertTrue(db.delete_team(self.conn, 1, "echo"))
        self.assertFalse(db.delete_team(self.conn, 1, "ECHO"))
        self.assertIsNone(db.get_team(self.conn, 1, "ECHO"))

    def test_a_team_is_always_exactly_five(self):
        with self.assertRaises(ValueError):
            db.save_team(self.conn, 1, "SHORT", ROSTER[:4])

    def test_saving_a_team_stores_no_scouting_data(self):
        db.save_team(self.conn, 1, "ECHO", ROSTER)

        # An alias only: no player rows, no matches, nothing collected.
        for table in ("players", "matches", "player_matches"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )


class TeamCommandFixture(ScoutingCommandFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # role_commands imports the job manager into its own namespace; the
        # base fixture only patches ban_commands'.
        jobs_patch = patch.object(role_commands, "scouting_jobs", self.jobs)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)

    def _bot(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.addAsyncCleanup(bot.close)
        ban_commands.setup_ban_commands(bot)
        role_commands.setup_role_commands(bot)
        command.setup_team_commands(bot)
        return bot

    def _team_interaction(self, guild_id: int | None = 555):
        # The shared fixture's interaction, which gives channel.send a real
        # int message id - the /banrecommend path stores it as a SQLite key.
        interaction, _ = self._interaction(guild_id=guild_id)
        return interaction

    @staticmethod
    def _reply(interaction) -> str:
        return interaction.response.send_message.call_args.args[0]

    async def _register(self, bot, name="ECHO", guild_id=555, players=None):
        players = players or ROSTER
        interaction = self._team_interaction(guild_id)
        await bot.tree.get_command("teamregister").callback(
            interaction, name, *players
        )
        return interaction


class TeamCommandTests(TeamCommandFixture):
    async def test_register_show_and_delete(self):
        bot = self._bot()
        registered = await self._register(bot)
        self.assertIn("저장했습니다", self._reply(registered))

        shown = self._team_interaction()
        await bot.tree.get_command("teamshow").callback(shown, "echo")
        reply = self._reply(shown)
        for player in ROSTER:
            with self.subTest(player=player):
                self.assertIn(player, reply)

        deleted = self._team_interaction()
        await bot.tree.get_command("teamdelete").callback(deleted, "ECHO")
        self.assertIn("삭제", self._reply(deleted))

        gone = self._team_interaction()
        await bot.tree.get_command("teamshow").callback(gone, "ECHO")
        self.assertIn("찾을 수 없습니다", self._reply(gone))

    async def test_registering_the_same_name_replaces_it(self):
        bot = self._bot()
        await self._register(bot)
        replacement = [f"New{index}#TEST" for index in range(5)]
        again = await self._register(bot, players=replacement)

        self.assertIn("덮어썼습니다", self._reply(again))
        shown = self._team_interaction()
        await bot.tree.get_command("teamshow").callback(shown, "ECHO")
        self.assertIn(replacement[0], self._reply(shown))
        self.assertNotIn(ROSTER[0], self._reply(shown))

    async def test_invalid_players_are_refused_at_registration(self):
        bot = self._bot()
        broken = [*ROSTER[:2], "no-tag", *ROSTER[3:]]
        interaction = await self._register(bot, players=broken)

        self.assertIn("입력 오류", self._reply(interaction))
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertIsNone(db.get_team(self.anchor, 555, "ECHO"))

    async def test_duplicate_players_are_refused_at_registration(self):
        bot = self._bot()
        interaction = await self._register(bot, players=[ROSTER[0], *ROSTER[:4]])

        self.assertIn("중복", self._reply(interaction))
        self.assertIsNone(db.get_team(self.anchor, 555, "ECHO"))

    async def test_an_empty_or_overlong_name_is_refused(self):
        bot = self._bot()
        for name in ("   ", "N" * (command.TEAM_NAME_MAX + 1)):
            with self.subTest(name=len(name)):
                interaction = await self._register(bot, name=name)
                self.assertIn("❌", self._reply(interaction))

    async def test_teams_are_guild_scoped_end_to_end(self):
        bot = self._bot()
        await self._register(bot, guild_id=555)

        elsewhere = self._team_interaction(guild_id=777)
        await bot.tree.get_command("teamshow").callback(elsewhere, "ECHO")
        self.assertIn("찾을 수 없습니다", self._reply(elsewhere))

    async def test_team_commands_refuse_a_direct_message(self):
        bot = self._bot()
        registered = await self._register(bot, guild_id=None)
        self.assertIn("서버", self._reply(registered))

        shown = self._team_interaction(guild_id=None)
        await bot.tree.get_command("teamshow").callback(shown, "ECHO")
        self.assertIn("서버", self._reply(shown))


class TeamReuseTests(TeamCommandFixture):
    """A saved team must be a pure alias: same pipeline, same everything."""

    def _assign_key(self):
        return role_commands._assignment_key(ban_commands.parse_inputs(
            dict(zip(role_commands.PLAYER_SLOTS, ROSTER)),
            slots=role_commands.PLAYER_SLOTS, labels=role_commands.PLAYER_LABELS,
        ))

    async def test_assignroles_by_team_matches_typing_the_five(self):
        bot = self._bot()
        await self._register(bot)
        slash = bot.tree.get_command("assignroles")

        typed = self._team_interaction()
        await slash.callback(typed, **dict(zip(role_commands.PLAYER_SLOTS, ROSTER)))
        await self.jobs.get(self._assign_key()).task
        typed_embed = typed.channel.send.await_args.kwargs["embeds"][0]
        collected = self.collect.await_count

        by_team = self._team_interaction()
        await slash.callback(by_team, team="echo")
        await self.jobs.get(self._assign_key()).task

        # A saved team resolves to exactly the same five accounts, so it lands
        # on the same dedupe key and the same cached history - no new
        # collection, and an identical recommendation.
        self.assertIn("시작했습니다", self._reply(by_team))
        self.assertEqual(self.collect.await_count, collected)
        embeds = by_team.channel.send.await_args.kwargs["embeds"]
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].to_dict(), typed_embed.to_dict())

    async def test_banrecommend_by_team_reads_the_saved_order_as_roles(self):
        bot = self._bot()
        await self._register(bot)
        interaction = self._team_interaction()

        await bot.tree.get_command("banrecommend").callback(interaction, team="ECHO")
        key = ban_commands._team_key(
            ban_commands.parse_inputs(dict(zip(ban_commands.ROLE_INPUTS, ROSTER)))
        )
        job = self.jobs.get(key)
        await job.task

        # Slot 1..5 become TOP..UTILITY: the saved roster reaches the unchanged
        # /banrecommend pipeline exactly as if the five had been typed in.
        self.assertIn("시작했습니다", self._reply(interaction))
        self.assertEqual(job.kind, "banrecommend")
        self.assertEqual(
            [(slot, name) for slot, name, _ in job.players],
            list(zip(ban_commands.ROLE_INPUTS, [player.split("#")[0] for player in ROSTER])),
        )
        self.assertEqual(self.collect.await_count, 10)

    async def test_an_unknown_team_is_an_actionable_error(self):
        bot = self._bot()
        interaction = self._team_interaction()

        await bot.tree.get_command("assignroles").callback(interaction, team="NOPE")

        self.assertIn("찾을 수 없습니다", self._reply(interaction))
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.accounts.assert_not_awaited()
        self.collect.assert_not_awaited()

    async def test_a_stored_team_with_a_broken_player_is_refused(self):
        bot = self._bot()
        # Written straight to the table, bypassing /teamregister's validation.
        db.save_team(self.anchor, 555, "BROKEN", [*ROSTER[:4], "no-tag"])
        interaction = self._team_interaction()

        await bot.tree.get_command("assignroles").callback(interaction, team="BROKEN")

        reply = self._reply(interaction)
        self.assertIn("올바르지 않습니다", reply)
        self.assertIn("teamregister", reply)
        self.collect.assert_not_awaited()

    async def test_team_and_players_together_are_refused(self):
        bot = self._bot()
        await self._register(bot)
        interaction = self._team_interaction()

        await bot.tree.get_command("assignroles").callback(
            interaction, team="ECHO", **dict(zip(role_commands.PLAYER_SLOTS, ROSTER))
        )

        self.assertIn("동시에", self._reply(interaction))
        self.collect.assert_not_awaited()

    async def test_neither_team_nor_players_is_refused(self):
        bot = self._bot()
        interaction = self._team_interaction()

        await bot.tree.get_command("assignroles").callback(interaction)

        self.assertIn("입력해주세요", self._reply(interaction))
        self.collect.assert_not_awaited()

    async def test_a_partial_roster_names_the_missing_slots(self):
        bot = self._bot()
        interaction = self._team_interaction()

        await bot.tree.get_command("assignroles").callback(
            interaction, player1=ROSTER[0], player2=ROSTER[1]
        )

        reply = self._reply(interaction)
        self.assertIn("빠진 자리", reply)
        self.assertIn("선수 5", reply)
        self.collect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
