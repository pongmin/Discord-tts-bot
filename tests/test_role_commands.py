"""Offline /assignroles integration checks; no Discord login or Riot requests."""

from unittest.mock import patch

import discord
from discord.ext import commands

from scouting import ban_commands
from scouting import role_commands as command
from scouting.role_assignment import ROLE_ORDER
from scouting.scouting_job_manager import JobStatus
from tests.test_ban_commands import ScoutingCommandFixture


class AssignRolesFixture(ScoutingCommandFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # The five slots carry no role meaning here, but the shared fixture's
        # collector derives each fake player's role from the trailing digit, so
        # PlayerN still ends up a main in ROLE_INPUTS[N].
        self.inputs = {slot: f"Player{index}#TEST"
                       for index, slot in enumerate(command.PLAYER_SLOTS)}
        # role_commands imports the job manager into its own namespace; the
        # base fixture only patches ban_commands'.
        jobs_patch = patch.object(command, "scouting_jobs", self.jobs)
        jobs_patch.start()
        self.addCleanup(jobs_patch.stop)

    def _assignroles_command(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        self.addAsyncCleanup(bot.close)
        ban_commands.setup_ban_commands(bot)
        command.setup_role_commands(bot)
        # Registering the new command must not disturb the existing ones.
        self.assertIsNotNone(bot.tree.get_command("banrecommend"))
        self.assertIsNotNone(bot.tree.get_command("clashban"))
        return bot.tree.get_command("assignroles")

    async def _run_one(self, **extra):
        slash = self._assignroles_command()
        interaction, channel = self._interaction()
        await slash.callback(interaction, **self.inputs, **extra)
        key = command._assignment_key(
            ban_commands.parse_inputs(
                self.inputs, slots=command.PLAYER_SLOTS, labels=command.PLAYER_LABELS
            )
        )
        await self.jobs.get(key).task
        return interaction, channel


class AssignRolesCommandTests(AssignRolesFixture):
    async def test_registration_takes_five_players_depth_and_details(self):
        slash = self._assignroles_command()
        self.assertEqual(
            [parameter.name for parameter in slash.parameters],
            ["team", *command.PLAYER_SLOTS, "depth", "details"],
        )
        self.assertEqual(
            [choice.value for choice in slash._params["depth"].choices],
            list(ban_commands.DEPTH_CAPS),
        )
        details = slash._params["details"]
        self.assertFalse(details.required)
        self.assertIs(details.default, False)

    async def test_one_collection_per_player_feeds_all_five_role_cells(self):
        interaction, channel = await self._run_one()

        # Two queues per player, once each - never once per player/role.
        self.assertEqual(self.collect.await_count, 2 * len(command.PLAYER_SLOTS))
        self.assertEqual({c.kwargs["queue_id"] for c in self.collect.await_args_list}, {420, 400})
        self.assertEqual({c.kwargs["max_count"] for c in self.collect.await_args_list}, {100})
        interaction.response.send_message.assert_awaited_once()

        embeds = channel.send.await_args.kwargs["embeds"]
        # Default response: the answer only, no 5x5 grid.
        self.assertEqual(len(embeds), 1)
        assignment = embeds[0]
        # Each synthetic player mains exactly one role, so the recommendation
        # is the identity mapping.
        recommended = assignment.fields[0].value
        # Line 0 is the assignment's team fit; the five seats follow it.
        for index, role in enumerate(ROLE_ORDER):
            with self.subTest(role=role):
                self.assertIn(f"Player{index}#TEST", recommended.splitlines()[index + 1])
        self.assertLessEqual(len(assignment), 6000)

    async def test_the_concise_response_carries_both_fits_and_the_gap(self):
        _, channel = await self._run_one()
        embed = channel.send.await_args.kwargs["embeds"][0]

        names = [field.name for field in embed.fields]
        self.assertEqual(names[:3], ["추천 배치", "적합도", "차선 배치"])
        fields = dict(zip(names, (field.value for field in embed.fields)))
        scores = fields["적합도"]
        self.assertIn("최적 배치 팀 적합도 **100.0%**", scores)
        self.assertIn("차선 배치 팀 적합도", scores)
        self.assertIn("적합도 차이", scores)
        # Percentages only out here: raw E and the log scores stay in details.
        for field in embed.fields:
            with self.subTest(field=field.name):
                self.assertNotIn("E ", field.value)
                self.assertNotIn("sum(log E)", field.value)
        # Every seat is shown as a share of that player's own best role, and
        # each synthetic player mains the role they are given.
        self.assertEqual(fields["추천 배치"].count("개인 적합도 100%"), len(ROLE_ORDER))
        # The runner-up still marks the roles that moved.
        self.assertIn("↔", fields["차선 배치"])

    async def test_details_appends_every_role_as_a_personal_fit_percentage(self):
        _, plain_channel = await self._run_one()
        plain = plain_channel.send.await_args.kwargs["embeds"]

        _, channel = await self._run_one(details=True)
        embeds = channel.send.await_args.kwargs["embeds"]

        self.assertEqual(len(embeds), 2)
        assignment, matrix = embeds
        # The concise half is byte-for-byte what the default response shows.
        self.assertEqual(assignment.to_dict(), plain[0].to_dict())
        self.assertEqual(len(matrix.fields), len(command.PLAYER_SLOTS))
        for index, field in enumerate(matrix.fields):
            with self.subTest(player=index):
                self.assertIn(f"Player{index}#TEST", field.name)
                lines = [line for line in field.value.splitlines() if line.startswith(("✅", "  "))]
                # All five roles, each as a percentage and nothing else - no
                # raw E, no factors, no game counts anywhere in the embed.
                self.assertEqual(len(lines), len(ROLE_ORDER))
                for line in lines:
                    self.assertRegex(line, r"^(✅|  ) [A-Z]+ +(\d+%|<1%)$")
                self.assertEqual(sum(line.startswith("✅") for line in lines), 1)
                # Their own main is 100%, the role they are given here.
                self.assertRegex(
                    next(line for line in lines if line.startswith("✅")), r"100%$"
                )
        # The line shape above already excludes raw E and its factors; these are
        # the developer numbers the embed used to carry and must no longer.
        for raw in ("경기", "sum(log", "N_eff"):
            self.assertNotIn(raw, str(matrix.to_dict()))
        for embed in embeds:
            with self.subTest(title=embed.title):
                self.assertLessEqual(len(embed), 6000)

    async def test_details_changes_nothing_about_collection_or_scoring(self):
        _, plain_channel = await self._run_one()
        plain = plain_channel.send.await_args.kwargs["embeds"][0]
        collected = self.collect.await_count

        _, channel = await self._run_one(details=True)

        # Cached, so no new collection, and the recommendation is identical.
        self.assertEqual(self.collect.await_count, collected)
        self.assertEqual(
            channel.send.await_args.kwargs["embeds"][0].to_dict(), plain.to_dict()
        )

    async def test_depth_controls_only_how_much_history_is_collected(self):
        slash = self._assignroles_command()
        interaction, _ = self._interaction()
        await slash.callback(interaction, **self.inputs, depth="deep")
        key = command._assignment_key(
            ban_commands.parse_inputs(
                self.inputs, slots=command.PLAYER_SLOTS, labels=command.PLAYER_LABELS
            )
        )
        await self.jobs.get(key).task

        self.assertEqual({c.kwargs["max_count"] for c in self.collect.await_args_list}, {200})

    async def test_cached_history_is_reused_on_a_second_run(self):
        await self._run_one()
        self.collect.reset_mock()
        self.ranks.reset_mock()

        await self._run_one()

        self.collect.assert_not_awaited()
        self.ranks.assert_not_awaited()

    async def test_bad_input_is_rejected_before_any_api_call(self):
        slash = self._assignroles_command()
        interaction, channel = self._interaction()
        await slash.callback(interaction, **{**self.inputs, "player3": "no-tag"})

        self.accounts.assert_not_awaited()
        self.collect.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
        self.assertIn("선수 3", interaction.response.send_message.call_args.args[0])

    async def test_the_same_five_in_any_order_is_one_job(self):
        parsed = ban_commands.parse_inputs(
            self.inputs, slots=command.PLAYER_SLOTS, labels=command.PLAYER_LABELS
        )
        shuffled_inputs = dict(zip(
            command.PLAYER_SLOTS,
            [self.inputs[slot] for slot in reversed(command.PLAYER_SLOTS)],
        ))
        shuffled = ban_commands.parse_inputs(
            shuffled_inputs, slots=command.PLAYER_SLOTS, labels=command.PLAYER_LABELS
        )

        self.assertEqual(command._assignment_key(parsed), command._assignment_key(shuffled))
        # ...and never the same job as a /banrecommend for the same accounts.
        self.assertNotEqual(
            command._assignment_key(parsed),
            ban_commands._team_key(ban_commands.parse_inputs(
                dict(zip(ban_commands.ROLE_INPUTS, self.inputs.values()))
            )),
        )

    async def test_a_failed_collection_reports_the_stage_and_no_embed(self):
        self.collect.side_effect = None
        self.collect.return_value = dict(
            player_id=1, requested=100, newly_fetched=0, skipped_cached=0,
            permanently_skipped=0, failed=100, aborted=False, is_complete=False,
        )
        slash = self._assignroles_command()
        interaction, channel = self._interaction()
        await slash.callback(interaction, **self.inputs)
        key = command._assignment_key(
            ban_commands.parse_inputs(
                self.inputs, slots=command.PLAYER_SLOTS, labels=command.PLAYER_LABELS
            )
        )
        job = self.jobs.get(key)
        await job.task

        # The job manager records the failure; the channel still gets told.
        self.assertEqual(job.status, JobStatus.FAILED)
        sent = [c.args[0] for c in channel.send.await_args_list if c.args]
        self.assertTrue(any(text.startswith("❌") for text in sent), sent)
        self.assertFalse(any("embeds" in c.kwargs for c in channel.send.await_args_list))


class BanRecommendStillWorksTests(ScoutingCommandFixture):
    async def test_role_slots_still_drive_banrecommend(self):
        """The shared collection refactor must not move /banrecommend's roles."""
        opponents, warnings = await ban_commands.prepare_opponents(self.inputs, self.progress)

        self.assertEqual(warnings, [])
        self.assertEqual(
            [role for _, role in opponents],
            [role.upper() for role in ban_commands.ROLE_INPUTS],
        )
