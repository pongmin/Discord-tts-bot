"""Discord adapter for /assignroles: which of these five plays where.

Deliberately thin. Everything expensive is already built: account resolution,
match collection, the depth caps and the 420/400 cache all come from
ban_commands.prepare_players (the same code path /banrecommend uses), and the
model itself is scouting.role_assignment. This module only validates input,
registers a background job, and renders the result.

Collection happens once per player, not once per player/role - all 25 cells of
the matrix are read back out of that single collected dataset. The whole matrix
is always computed; `details` only decides whether it is rendered.

/banrecommend is untouched: recommend_bans is never called from here.
"""

import asyncio
import logging

import discord
from discord import app_commands

from scouting.ban_commands import (
    BanCommandError, COMMAND_TIMEOUT_SECONDS, DEPTH_CAPS, DEPTH_DESCRIPTION,
    BanProgressStatus, _error_text, depth_choices, parse_inputs, prepare_players,
)
from scouting import scouting_db as db
from scouting.role_assignment import assign_roles
from scouting.team_commands import TEAM_NAME_MAX, resolve_team_or_inputs
from scouting.role_assignment_ui import render_assignment_embed, render_matrix_embed
from scouting.scouting_job_manager import ScoutingJob, scouting_db_lock, scouting_jobs
from scouting.scouting_repo import ScoutingRepo
from riot.riot_api import RiotApiError


logger = logging.getLogger(__name__)
PLAYER_SLOTS = ("player1", "player2", "player3", "player4", "player5")
PLAYER_LABELS = {slot: f"선수 {index}" for index, slot in enumerate(PLAYER_SLOTS, start=1)}


def _assignment_key(parsed: list[tuple[str, str, str]]) -> tuple:
    """Dedupe key for /assignroles: the five accounts, order-independent.

    Unlike /banrecommend's key, the slot a player was typed into carries no
    meaning here - the whole point is that roles are an output - so the same
    five people entered in any order are one job. The leading marker keeps this
    from ever colliding with ban_commands._team_key, which is a different
    analysis over the same accounts.
    """
    return ("assignroles", *sorted((name.casefold(), tag.casefold()) for _, name, tag in parsed))


def _build_assignment(player_ids: list[int], cutoff_time: int):
    """Compute the assignment on a worker thread, with its own connection.

    The explicit cutoff is the one taken after collection finished, so every
    cell of the matrix sees exactly the same dataset.
    """
    with ScoutingRepo(cutoff_time=cutoff_time) as repo:
        return assign_roles(repo, player_ids)


async def _run_assignroles_job(
    job: ScoutingJob, inputs: dict[str, str], depth: str, channel,
    details: bool = False,
) -> None:
    """Background job body: collection + assignment, then a channel message.

    Mirrors the /banrecommend job's lifetime and failure handling - independent
    of the originating interaction, every failure reported as a bot-sent channel
    message naming the stage it stopped at - and re-raises so the job manager
    records the failure too.
    """
    mentions = discord.AllowedMentions.none()
    status = BanProgressStatus(
        channel, inputs, slots=PLAYER_SLOTS, labels=PLAYER_LABELS,
        title="🧭 역할 배치 분석 중", calculation="배치 계산",
    )
    await status.start()

    async def progress(message: str, role: str | None = None, done: bool = False):
        job.set_stage(message)
        if role is None:
            return
        if done:
            await status.mark_done(role)
        else:
            await status.set_current(role)

    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
            await progress("계정 확인 중")
            # Same serialization as /banrecommend: one team's whole scouting.db
            # access (collection writes here, then the read-only worker thread)
            # ordered against every other team's job.
            async with scouting_db_lock:
                collected, collection_warnings = await prepare_players(
                    inputs, progress, depth, slots=PLAYER_SLOTS,
                    labels=PLAYER_LABELS, role_required=False,
                )
                await progress("배치 계산 중")
                await status.start_calculation()
                # Taken after collection, for the same reason /banrecommend
                # does: a just-collected match's game_end can land at or after
                # an earlier cutoff and vanish from the model.
                cutoff_time = db.now_ms()
                player_ids = [player_id for _, player_id in collected]
                result = await asyncio.to_thread(_build_assignment, player_ids, cutoff_time)
    except TimeoutError:
        await status.fail(job.stage)
        await channel.send(
            "❌ 처리 시간이 초과되어 배치 계산을 중단했습니다. "
            "저장된 경기는 유지됩니다. 다시 실행하면 캐시를 재사용합니다.",
            allowed_mentions=mentions,
        )
        raise
    except (BanCommandError, RiotApiError, ValueError) as exc:
        await status.fail(job.stage)
        await channel.send(f"❌ {job.stage}: {_error_text(exc)}"[:1900], allowed_mentions=mentions)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("ASSIGNROLES background job failed at %s", job.stage)
        await status.fail(job.stage)
        await channel.send(
            f"❌ {job.stage}: 오류가 발생해 배치 계산을 중단했습니다. 봇 로그를 확인해주세요.",
            allowed_mentions=mentions,
        )
        raise

    await progress("완료")
    await status.complete()
    for warning in collection_warnings:
        await channel.send(warning[:1900], allowed_mentions=mentions)
    # The answer by default; the 5x5 evidence appended only on request. Either
    # way it is one message with no navigation state to persist and nothing to
    # revive after a restart.
    embeds = [render_assignment_embed(result)]
    if details:
        embeds.append(render_matrix_embed(result))
    await channel.send(embeds=embeds, allowed_mentions=mentions)


def setup_role_commands(bot):
    @bot.tree.command(
        name="assignroles",
        description="아군 5명의 Riot ID로 포지션 배치를 추천합니다.",
    )
    @app_commands.describe(
        team=f"저장된 팀 이름 (선수 5명 대신 · /teamregister, {TEAM_NAME_MAX}자 이하)",
        player1="선수 1 (이름#태그)", player2="선수 2 (이름#태그)",
        player3="선수 3 (이름#태그)", player4="선수 4 (이름#태그)",
        player5="선수 5 (이름#태그)",
        depth=DEPTH_DESCRIPTION,
        details="켜면 선수×포지션 5×5 적합도 진단표를 함께 보여줍니다.",
    )
    @app_commands.choices(depth=depth_choices())
    async def assignroles(
        interaction: discord.Interaction, team: str | None = None,
        player1: str | None = None, player2: str | None = None,
        player3: str | None = None, player4: str | None = None,
        player5: str | None = None,
        depth: str = "normal", details: bool = False,
    ):
        provided = dict(zip(PLAYER_SLOTS, (player1, player2, player3, player4, player5)))

        # A saved team is only an alias: it is resolved to the same five Riot ID
        # strings a user would have typed, and everything downstream is
        # unchanged. The saved order carries no role meaning here.
        try:
            inputs = await resolve_team_or_inputs(
                interaction.guild_id, team, provided,
                slots=PLAYER_SLOTS, labels=PLAYER_LABELS,
            )
            parsed = parse_inputs(inputs, slots=PLAYER_SLOTS, labels=PLAYER_LABELS)
        except BanCommandError as exc:
            await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
            return

        if depth not in DEPTH_CAPS:
            await interaction.response.send_message(
                "❌ 수집 깊이는 빠르게, 기본, 깊게 중에서 선택해주세요.", ephemeral=True
            )
            return

        channel = interaction.channel

        async def runner(job: ScoutingJob) -> None:
            await _run_assignroles_job(job, inputs, depth, channel, details)

        # start() is synchronous end-to-end, so dedupe and registration happen
        # atomically for two near-simultaneous requests for the same five.
        job, created = scouting_jobs.start(
            key=_assignment_key(parsed), kind="assignroles", user_id=interaction.user.id,
            guild_id=interaction.guild_id, channel_id=channel.id,
            players=tuple(parsed), runner=runner,
        )

        if not created:
            await interaction.response.send_message("이미 같은 조합을 분석 중입니다.", ephemeral=True)
            return

        await interaction.response.send_message(
            "🧭 역할 배치 분석을 시작했습니다. 완료되면 이 채널에 결과를 보내드릴게요."
        )
