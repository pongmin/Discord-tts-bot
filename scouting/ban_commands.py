"""Discord adapter for the unchanged v1 ban algorithm and scouting pipeline.

Collection (account lookup -> match collection -> DB write) and the
recommendation calculation run as a background job via scouting_job_manager,
fully decoupled from the /banrecommend interaction's lifetime. The slash
command only validates input, registers a job, and acknowledges immediately;
the job itself delivers its result (or failure) as a plain channel message
sent with the bot's own credentials, not an interaction followup.
"""

import asyncio
from contextlib import closing
from dataclasses import replace
import logging

import discord
from discord import app_commands

from scouting.ban_algorithm import Recommendation, recommend_bans, format_recommendation
from scouting.ban_report_assets import load_player_presentations
from scouting.ban_report_ui import ScoutingReportView
from scouting.match_collector import collect_player_matches
from riot.riot_api import (
    parse_riot_id, get_account_by_riot_id, get_league_entries,
    InvalidRiotIdError, RiotApiError, RANKED_SOLO_QUEUE_TYPE,
)
from scouting import scouting_db as db
from scouting.scouting_job_manager import ScoutingJob, scouting_db_lock, scouting_jobs
from scouting.scouting_repo import ScoutingRepo


logger = logging.getLogger(__name__)
ROLE_INPUTS = ("top", "jungle", "middle", "bottom", "utility")
ROLE_LABELS = {"top": "TOP", "jungle": "JUNGLE", "middle": "MID", "bottom": "BOTTOM", "utility": "SUPPORT"}
QUEUE_IDS = (420, 400)
QUEUE_LABELS = {420: "솔로 랭크", 400: "일반 드래프트"}
# Placeholder collection budgets, not tuned. Estimates are per cold-cache player:
# normal/deep follow the measured ~9 min/200-game rate; quick is a rough fraction.
DEPTH_CAPS = {"quick": 30, "normal": 100, "deep": 200}
LOW_ROLE_GAME_COUNT = 20  # Advisory placeholder only; never a collection target.
RANK_CACHE_MS = 24 * 60 * 60 * 1000
# No longer bounded by Discord's 15-minute interaction token (the job reports
# through a plain channel message instead of a followup) - this is now just a
# sanity cap against a stuck/runaway background job.
COMMAND_TIMEOUT_SECONDS = 40 * 60


class BanCommandError(ValueError):
    """An actionable input or incomplete-collection error."""


def parse_inputs(inputs: dict[str, str]) -> list[tuple[str, str, str]]:
    parsed = []
    seen = {}
    for role in ROLE_INPUTS:
        raw = inputs[role]
        try:
            game_name, tag_line = parse_riot_id(raw)
            # The shared parser splits once; reject extra separators here without
            # changing the existing /clashlookup parsing behavior.
            if raw.count("#") != 1:
                raise InvalidRiotIdError("이름#태그 형식으로 입력해주세요.")
        except InvalidRiotIdError as exc:
            raise BanCommandError(f"{ROLE_LABELS[role]} 입력 오류 ({raw}): {exc}") from exc
        key = (game_name.casefold(), tag_line.casefold())
        if key in seen:
            raise BanCommandError(f"동일 계정 중복 입력: {ROLE_LABELS[seen[key]]}, {ROLE_LABELS[role]} ({raw})")
        seen[key] = role
        parsed.append((role, game_name, tag_line))
    return parsed


def _fresh(timestamp: int | None, now: int, ttl: int) -> bool:
    return timestamp is not None and 0 <= now - timestamp < ttl


async def _ensure_solo_rank(conn, player_id: int, account) -> None:
    now = db.now_ms()
    with ScoutingRepo(cutoff_time=now, conn=conn) as repo:
        rank = repo.get_latest_rank_snapshot(player_id, RANKED_SOLO_QUEUE_TYPE)
    if rank is not None and _fresh(rank["snapshot_at"], now, RANK_CACHE_MS):
        return
    # Reuse League-V4 and the snapshot writer, without collecting unused mastery.
    entries = await get_league_entries(account.puuid)
    solo = next((e for e in entries if e.queue_type == RANKED_SOLO_QUEUE_TYPE), None)
    # A successful empty response records unranked, superseding any old tier.
    db.insert_rank_snapshot(
        conn, player_id, RANKED_SOLO_QUEUE_TYPE,
        solo.tier if solo else None, solo.division if solo else None,
        solo.lp if solo else None, solo.wins if solo else None,
        solo.losses if solo else None, db.now_ms(),
    )
    conn.commit()


async def prepare_opponents(
    inputs: dict[str, str], progress, depth: str = "normal",
) -> list[tuple[int, str]]:
    if depth not in DEPTH_CAPS:
        raise BanCommandError("수집 깊이는 빠르게, 기본, 깊게 중에서 선택해주세요.")
    cap = DEPTH_CAPS[depth]
    parsed = parse_inputs(inputs)
    resolved = []
    seen_puuids = {}
    # Resolve all five before writing/collecting; aliases can map to one PUUID.
    for role, game_name, tag_line in parsed:
        label = f"{game_name}#{tag_line}"
        await progress(f"{ROLE_LABELS[role]} 계정 조회 ({label})")
        account = await get_account_by_riot_id(game_name, tag_line)
        if account.puuid in seen_puuids:
            raise BanCommandError(
                f"동일 계정 중복: {ROLE_LABELS[seen_puuids[account.puuid]]}, {ROLE_LABELS[role]} ({label})"
            )
        seen_puuids[account.puuid] = role
        resolved.append((role, label, account))

    opponents = []
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        collection_cutoff = db.now_ms()
        for role, label, account in resolved:
            player_id = db.get_or_create_player(
                conn, account.puuid, account.game_name, account.tag_line
            )
            for queue_id in QUEUE_IDS:
                await progress(f"{ROLE_LABELS[role]} {QUEUE_LABELS[queue_id]} 경기 확보 ({label})")
                # Count actual player/queue rows at cutoff, regardless of role
                # or fetch-state age. Never limit the algorithm's read-side pool.
                with ScoutingRepo(cutoff_time=collection_cutoff, conn=conn) as repo:
                    cached_count = len(repo.get_all_matches(player_id, queue_ids=(queue_id,)))
                if cached_count >= cap:
                    continue
                # Keep incomplete attempts marked as such for other consumers.
                db.refresh_fetch_state(conn, player_id, queue_id, False)
                result = await collect_player_matches(
                    account.puuid, account.game_name, account.tag_line,
                    queue_id=queue_id, max_count=cap, conn=conn,
                )
                if result["failed"] or result["aborted"] or not result["is_complete"]:
                    db.refresh_fetch_state(conn, player_id, queue_id, False)
                    raise BanCommandError(
                        f"{ROLE_LABELS[role]} 수집 실패 ({label}, {QUEUE_LABELS[queue_id]}): "
                        f"경기 수집을 완료하지 못했습니다. 오류 {result['failed']}건. 추천을 중단합니다."
                    )
                if result["player_id"] != player_id:
                    raise BanCommandError(f"{ROLE_LABELS[role]} 수집 계정 불일치 ({label}). 추천을 중단합니다.")
            await progress(f"{ROLE_LABELS[role]} 솔로 랭크 확보 ({label})")
            await _ensure_solo_rank(conn, player_id, account)
            with ScoutingRepo(cutoff_time=db.now_ms(), conn=conn) as repo:
                if not repo.get_role_matches(player_id, role.upper(), queue_ids=QUEUE_IDS):
                    raise BanCommandError(
                        f"{ROLE_LABELS[role]} 데이터 부족 ({label}): 수집된 솔로 랭크·일반 드래프트에 "
                        f"{ROLE_LABELS[role]} 기록이 없어 추천을 중단합니다."
                    )
            opponents.append((player_id, role.upper()))
    return opponents


def _recommendation(repo: ScoutingRepo, opponents: list[tuple[int, str]]) -> Recommendation:
    result = recommend_bans(repo, opponents)
    warnings = []
    for player in result.players:
        count = sum(champion.games for champion in player.champions.values())
        if count < LOW_ROLE_GAME_COUNT:
            warnings.append(
                f"{player.label}/{player.role}: only {count} role-filtered games available "
                "— recommendation may be less reliable. Consider a deeper --depth "
                "if more history exists."
            )
    # Preserve exactly the pre-existing scoring and low-sample warning behavior.
    return replace(result, warnings=result.warnings + tuple(warnings))


def _recommend_report(opponents: list[tuple[int, str]]) -> str:
    """Retain the text formatter for manual/debug use only."""
    with ScoutingRepo(cutoff_time=db.now_ms()) as repo:
        return format_recommendation(_recommendation(repo, opponents))


def _build_discord_report(opponents: list[tuple[int, str]], cutoff_time: int):
    """
    cutoff_time을 명시적으로 받음(호출부가 db.now_ms()를 흘려 넣던 것에서 변경) -
    버튼으로 리포트를 다시 그릴 때(재시작 후 복구 포함) 처음 만들 때와 정확히
    같은 컷오프로 재계산해야, 그 사이 새로 수집된 경기 때문에 픽 풀/위험도가
    페이지 넘길 때마다 달라져 보이는 일이 없음.
    """
    # Create/use/close SQLite on the worker thread. Presentation-only metadata
    # reads reuse the same cutoff and never add API calls or change collection.
    with ScoutingRepo(cutoff_time=cutoff_time) as repo:
        result = _recommendation(repo, opponents)
        return result, load_player_presentations(repo, result)


def _save_ban_report_row(
    message_id: int, channel_id: int, guild_id: int | None, owner_id: int,
    cutoff_time: int, opponents: list[tuple[int, str]], page_index: int,
) -> None:
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        db.save_ban_report(conn, message_id, channel_id, guild_id, owner_id, cutoff_time, opponents, page_index)


def _load_ban_report_row(message_id: int):
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        return db.get_ban_report(conn, message_id)


def _update_ban_report_page_row(message_id: int, page_index: int) -> None:
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        db.update_ban_report_page(conn, message_id, page_index)


async def _persist_page_index(view: ScoutingReportView, new_index: int) -> None:
    """
    ScoutingReportView.on_page_change로 주입되는 훅. 어느 리포트든(새로 만든
    것이든, 재시작 후 되살린 것이든) 페이지를 넘길 때마다 DB에 현재 페이지를
    기록해서, 다음 재시작 때 마지막으로 보던 페이지부터 이어서 열리게 함.
    """
    if view.message is None:
        return

    async with scouting_db_lock:
        await asyncio.to_thread(_update_ban_report_page_row, view.message.id, new_index)


def _error_text(exc: Exception) -> str:
    """Translate calculation failures without leaking internal formula names."""
    message = str(exc)
    if isinstance(exc, (BanCommandError, RiotApiError)):
        return message
    if "At least 3 distinct candidate champions" in message:
        return "추천할 수 있는 챔피언이 3종 미만입니다. 수집된 경기 기록을 확인해주세요."
    if "no collected queue 420/400 role games" in message:
        return "선수의 해당 포지션 경기 기록이 없어 추천할 수 없습니다."
    if "incomplete champion or win data" in message:
        return "챔피언 또는 승패 기록이 불완전합니다. 수집된 경기 기록을 확인해주세요."
    return "수집된 자료로 추천을 계산하지 못했습니다. 봇 로그를 확인해주세요."


def _team_key(parsed: list[tuple[str, str, str]]) -> tuple:
    """Dedupe key for a team: role + case-folded Riot ID, in ROLE_INPUTS order.

    Identical regardless of which user requested it - two different users
    asking for the same five accounts still share one in-flight collection.
    """
    return tuple((role, game_name.casefold(), tag_line.casefold()) for role, game_name, tag_line in parsed)


async def _run_banrecommend_job(
    job: ScoutingJob, inputs: dict[str, str], depth: str, channel, owner_id: int,
    guild_id: int | None,
) -> None:
    """Background job body: collection + recommendation, then a channel message.

    Runs fully independent of the originating interaction. Failures are
    reported the same way successes are - a new message in `channel` sent
    with the bot's own credentials - naming the stage (and, via BanCommandError
    text, the specific player/role) where it stopped. Re-raises afterwards so
    ScoutingJobManager also records the failure on the job itself.
    """
    mentions = discord.AllowedMentions.none()

    async def progress(message: str):
        job.set_stage(message)

    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
            await progress("계정 확인 중")
            # Serialize this team's entire scouting.db access - collection
            # writes on this thread, then the recommendation's read-only
            # worker thread (asyncio.to_thread below) - against every other
            # team's job (see scouting_job_manager.scouting_db_lock). This
            # does not replace riot_api.py's own 429 backoff, which every
            # request still goes through as-is; it only orders teams so no
            # two ever touch scouting.db at the same time, whether that's two
            # write connections on this thread or a write here racing a
            # different team's read connection on its worker thread.
            async with scouting_db_lock:
                opponents = await prepare_opponents(inputs, progress, depth)
                await progress("밴 계산 중")
                # Captured AFTER collection finishes, not before: a freshly
                # collected match's game_end can land at/after a cutoff taken
                # earlier, which would make build_player_model see "no games
                # before cutoff" and drop a player who was just collected.
                cutoff_time = db.now_ms()
                result, presentations = await asyncio.to_thread(_build_discord_report, opponents, cutoff_time)
    except TimeoutError:
        await channel.send(
            "❌ 처리 시간이 초과되어 추천을 중단했습니다. "
            "저장된 경기는 유지됩니다. 다시 실행하면 캐시를 재사용합니다.",
            allowed_mentions=mentions,
        )
        raise
    except (BanCommandError, RiotApiError, ValueError) as exc:
        await channel.send(f"❌ {job.stage}: {_error_text(exc)}"[:1900], allowed_mentions=mentions)
        raise
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("BANRECOMMEND background job failed at %s", job.stage)
        await channel.send(
            f"❌ {job.stage}: 오류가 발생해 추천을 중단했습니다. 봇 로그를 확인해주세요.",
            allowed_mentions=mentions,
        )
        raise

    await progress("완료")
    view = ScoutingReportView(result, presentations, owner_id=owner_id, on_page_change=_persist_page_index)
    view.message = await channel.send(embed=view.current_embed, view=view, allowed_mentions=mentions)

    # Persist enough to rebuild this exact report later: opponents+cutoff_time
    # (not the computed Recommendation itself) so a restart can recompute it
    # faithfully via PersistentBanReportRouter, and page_index so navigation
    # resumes where the user left off.
    async with scouting_db_lock:
        await asyncio.to_thread(
            _save_ban_report_row, view.message.id, channel.id, guild_id,
            owner_id, cutoff_time, opponents, view.page_index,
        )


class PersistentBanReportRouter(discord.ui.View):
    """
    프로세스가 재시작된 뒤에도 예전에 보낸 리포트 메시지의 이전/다음 버튼이
    계속 동작하게 하는 전역 fallback view. bot.add_view()로 딱 한 번만
    등록해 두면 됨 - 이 view 자체는 특정 메시지에 묶여 있지 않고, 클릭된
    메시지가 어느 리포트인지는 매번 interaction.message.id로 ban_reports
    테이블을 조회해서 알아냄(Recommendation은 저장하지 않고 opponents+
    cutoff_time으로 그 자리에서 다시 계산함).

    같은 프로세스 안에서는, 이렇게 되살린 메시지를 한 번 edit_message로
    갱신하고 나면 discord.py가 그 순간부터 그 메시지에 새로 붙은
    ScoutingReportView 인스턴스를 직접 추적하므로(일반 메시지별 view 추적),
    다음 클릭부터는 이 라우터를 다시 거치지 않고 곧장 그 인스턴스가 처리함 -
    즉, 되살린 리포트당 재계산은 재시작 후 최초 클릭 한 번뿐임.
    """

    def __init__(self):
        super().__init__(timeout=None)

    async def _revive(self, interaction: discord.Interaction, offset: int) -> None:
        message = interaction.message

        if message is None:
            return

        row = await asyncio.to_thread(_load_ban_report_row, message.id)

        if row is None:
            await interaction.response.send_message(
                "이 리포트는 더 이상 탐색할 수 없습니다. /banrecommend를 다시 실행해주세요.",
                ephemeral=True,
            )
            return

        if interaction.user.id != row["owner_id"]:
            await interaction.response.send_message(
                "페이지 이동은 이 리포트를 요청한 사용자만 할 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer()
        opponents = db.ban_report_opponents(row)
        target_page = max(0, min(5, row["page_index"] + offset))

        try:
            async with scouting_db_lock:
                result, presentations = await asyncio.to_thread(
                    _build_discord_report, opponents, row["cutoff_time"]
                )
        except (BanCommandError, RiotApiError, ValueError) as exc:
            await interaction.edit_original_response(
                content=f"❌ 리포트를 다시 계산하지 못했습니다: {_error_text(exc)}"[:1900],
                embed=None, view=None,
            )
            return
        except Exception:
            logger.exception("Failed to revive ban report for message %s", message.id)
            await interaction.edit_original_response(
                content="❌ 리포트를 다시 계산하지 못했습니다. 봇 로그를 확인해주세요.",
                embed=None, view=None,
            )
            return

        view = ScoutingReportView(
            result, presentations, owner_id=row["owner_id"], page_index=target_page,
            on_page_change=_persist_page_index,
        )
        view.message = message
        await interaction.edit_original_response(
            content=None, embed=view.current_embed, view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await _persist_page_index(view, target_page)

    @discord.ui.button(label="이전", style=discord.ButtonStyle.secondary, row=0,
                        custom_id=ScoutingReportView.PREV_CUSTOM_ID)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._revive(interaction, -1)

    @discord.ui.button(label="다음", style=discord.ButtonStyle.primary, row=0,
                        custom_id=ScoutingReportView.NEXT_CUSTOM_ID)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._revive(interaction, 1)


def setup_ban_commands(bot):
    # 재시작 후에도 예전 리포트의 버튼이 동작하도록 전역으로 한 번만 등록함.
    # 로그인 전에 호출해도 되는 동기 등록이라 setup 시점에 바로 둠.
    bot.add_view(PersistentBanReportRouter())

    @bot.tree.command(name="banrecommend", description="상대 5명의 역할별 Riot ID로 밴 3개를 추천합니다.")
    @app_commands.describe(
        top="TOP 선수 (이름#태그)", jungle="JUNGLE 선수 (이름#태그)",
        middle="MID 선수 (이름#태그)", bottom="BOTTOM 선수 (이름#태그)",
        utility="SUPPORT 선수 (이름#태그)",
        depth="수집 깊이: 빠르게 30 / 기본 100 / 깊게 200 경기, 선수·큐별",
    )
    @app_commands.choices(depth=[
        app_commands.Choice(name="빠르게 · 30경기", value="quick"),
        app_commands.Choice(name="기본 · 100경기", value="normal"),
        app_commands.Choice(name="깊게 · 200경기", value="deep"),
    ])
    async def banrecommend(
        interaction: discord.Interaction, top: str, jungle: str,
        middle: str, bottom: str, utility: str,
        depth: str = "normal",
    ):
        inputs = dict(zip(ROLE_INPUTS, (top, jungle, middle, bottom, utility)))

        # Immediate, no-I/O validation only: Riot ID shape and duplicate slots.
        # Account lookups, collection, and recommendation all happen in the
        # background job below.
        try:
            parsed = parse_inputs(inputs)
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
            await _run_banrecommend_job(job, inputs, depth, channel, interaction.user.id, interaction.guild_id)

        # start() is synchronous end-to-end (no await inside), so the
        # dedupe check and job registration happen atomically - two near-
        # simultaneous requests for the same team can't both slip through.
        job, created = scouting_jobs.start(
            key=_team_key(parsed), kind="banrecommend", user_id=interaction.user.id,
            guild_id=interaction.guild_id, channel_id=channel.id,
            players=tuple(parsed), runner=runner,
        )

        if not created:
            await interaction.response.send_message("이미 같은 팀을 분석 중입니다.", ephemeral=True)
            return

        await interaction.response.send_message(
            "🔎 밴 추천 분석을 시작했습니다. 완료되면 이 채널에 결과를 보내드릴게요."
        )
