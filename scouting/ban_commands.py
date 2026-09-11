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

from clash.clash_commands import fetch_clash_roster
from scouting.ban_algorithm import Recommendation, recommend_bans, format_recommendation
from scouting.ban_report_assets import load_player_presentations
from scouting.ban_report_ui import COLLECTION_WARNING_PREFIX, LAST_PAGE_INDEX, ScoutingReportView
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
# CLASH-V1 position -> this module's role input key. Anything else (UNSELECTED,
# FILL, an unknown value) has no role to analyze and stops /clashban.
CLASH_POSITION_ROLES = {
    "TOP": "top", "JUNGLE": "jungle", "MIDDLE": "middle",
    "BOTTOM": "bottom", "UTILITY": "utility",
}
QUEUE_IDS = (420, 400)
QUEUE_LABELS = {420: "솔로 랭크", 400: "일반 드래프트"}
# Placeholder collection budgets, not tuned. Estimates are per cold-cache player:
# normal/deep follow the measured ~9 min/200-game rate; quick is a rough fraction.
DEPTH_CAPS = {"quick": 30, "normal": 100, "deep": 200}
LOW_ROLE_GAME_COUNT = 20  # Advisory placeholder only; never a collection target.
# Collection tolerance: a handful of unreachable matches must not sink a whole
# team's recommendation. A queue is usable once this share of the matches Riot
# listed is actually stored; the rest is reported as a warning, not an error.
# Below it, too much of the player's history is missing to rank them honestly.
MIN_COLLECTION_RATIO = 0.8
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


async def resolve_clash_inputs(riot_id: str) -> dict[str, str]:
    """One opponent's Riot ID -> the /banrecommend inputs for their Clash team.

    Reuses /clashlookup's roster lookup as-is (including its "#MOCK" routing)
    and only maps it onto the five role slots; collection, scoring and the
    report are the unchanged /banrecommend pipeline. Anything that cannot be
    mapped to exactly one player per role stops here with a Korean error
    rather than analyzing a partial team.
    """
    roster = await fetch_clash_roster(riot_id)
    if len(roster) != 5:
        raise BanCommandError(
            f"격전 팀 인원이 {len(roster)}명입니다. 5명이 모두 등록된 팀만 분석할 수 있습니다."
        )
    inputs: dict[str, str] = {}
    for player, resolved in roster:
        position = (player.position or "").upper()
        role = CLASH_POSITION_ROLES.get(position)
        if role is None:
            raise BanCommandError(
                "포지션이 정해지지 않은 팀원이 있습니다. 격전 팀에서 5개 포지션이 "
                "모두 지정된 뒤 다시 시도해주세요."
            )
        if role in inputs:
            raise BanCommandError(
                f"{ROLE_LABELS[role]} 포지션이 중복된 팀입니다. 격전 팀 포지션을 확인해주세요."
            )
        if isinstance(resolved, Exception):
            # /clashlookup shows these as "알 수 없음"; a recommendation cannot
            # collect for an account it could not resolve, so stop instead.
            raise BanCommandError(
                f"{ROLE_LABELS[role]} 팀원의 Riot ID를 확인하지 못했습니다. 잠시 후 다시 시도해주세요."
            )
        inputs[role] = f"{resolved.game_name}#{resolved.tag_line}"
    # Five members, five distinct mapped roles: every slot is filled.
    return inputs


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


def _collection_summary(result: dict) -> tuple[int, int, int, int, str]:
    """(requested, secured, failed, skipped, user-facing summary) for one queue.

    Every listed match ends up in exactly one bucket - stored (freshly fetched
    or already cached), transiently failed, or permanently skipped - so what is
    actually secured is simply what is left after the other two.
    """
    requested = result.get("requested", 0)
    failed = result.get("failed", 0)
    skipped = result.get("permanently_skipped", 0)
    secured = max(requested - failed - skipped, 0)
    parts = [f"{requested}경기 중 {secured}경기 확보"]
    if failed:
        parts.append(f"{failed}경기 조회 실패")
    if skipped:
        parts.append(f"{skipped}경기 조회 불가(영구 제외)")
    return requested, secured, failed, skipped, " · ".join(parts)


async def prepare_opponents(
    inputs: dict[str, str], progress, depth: str = "normal",
) -> tuple[list[tuple[int, str]], list[str]]:
    """Resolve, collect and rank all five opponents.

    Returns the opponents and any collection warnings, which are surfaced with
    the report rather than aborting it: collection is tolerant of a few matches
    Riot will not hand over, and only stops when too little of a player's
    history could be secured (or when the failure is fatal for every request).

    `progress(message, role=..., done=...)` reports both the stage text (used
    verbatim in failure messages) and, where a stage belongs to one player,
    which role it is and whether that player is now fully collected.
    """
    if depth not in DEPTH_CAPS:
        raise BanCommandError("수집 깊이는 빠르게, 기본, 깊게 중에서 선택해주세요.")
    cap = DEPTH_CAPS[depth]
    parsed = parse_inputs(inputs)
    resolved = []
    seen_puuids = {}
    # Resolve all five before writing/collecting; aliases can map to one PUUID.
    for role, game_name, tag_line in parsed:
        label = f"{game_name}#{tag_line}"
        await progress(f"{ROLE_LABELS[role]} 계정 조회 ({label})", role=role)
        account = await get_account_by_riot_id(game_name, tag_line)
        if account.puuid in seen_puuids:
            raise BanCommandError(
                f"동일 계정 중복: {ROLE_LABELS[seen_puuids[account.puuid]]}, {ROLE_LABELS[role]} ({label})"
            )
        seen_puuids[account.puuid] = role
        resolved.append((role, label, account))

    opponents = []
    warnings: list[str] = []
    with closing(db.get_connection()) as conn:
        db.init_db(conn)
        collection_cutoff = db.now_ms()
        for role, label, account in resolved:
            player_id = db.get_or_create_player(
                conn, account.puuid, account.game_name, account.tag_line
            )
            for queue_id in QUEUE_IDS:
                await progress(
                    f"{ROLE_LABELS[role]} {QUEUE_LABELS[queue_id]} 경기 확보 ({label})", role=role
                )
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
                requested, secured, failed, skipped, summary = _collection_summary(result)
                where = f"{ROLE_LABELS[role]} {QUEUE_LABELS[queue_id]} ({label})"
                if result["aborted"]:
                    db.refresh_fetch_state(conn, player_id, queue_id, False)
                    raise BanCommandError(
                        f"{where} 수집 중단: API 키가 유효하지 않아 이후 요청도 모두 실패합니다. "
                        f"{summary}. 추천을 중단합니다."
                    )
                # A single unreachable match no longer stops the pipeline; only
                # losing most of the queue does.
                if requested and secured / requested < MIN_COLLECTION_RATIO:
                    db.refresh_fetch_state(conn, player_id, queue_id, False)
                    raise BanCommandError(
                        f"{where} 수집 실패: {summary}. 확보한 경기가 너무 적어 추천을 중단합니다."
                    )
                if failed or skipped:
                    warnings.append(f"{COLLECTION_WARNING_PREFIX}{where}: {summary}")
                if result["player_id"] != player_id:
                    raise BanCommandError(f"{ROLE_LABELS[role]} 수집 계정 불일치 ({label}). 추천을 중단합니다.")
            await progress(f"{ROLE_LABELS[role]} 솔로 랭크 확보 ({label})", role=role)
            await _ensure_solo_rank(conn, player_id, account)
            with ScoutingRepo(cutoff_time=db.now_ms(), conn=conn) as repo:
                if not repo.get_role_matches(player_id, role.upper(), queue_ids=QUEUE_IDS):
                    raise BanCommandError(
                        f"{ROLE_LABELS[role]} 데이터 부족 ({label}): 수집된 솔로 랭크·일반 드래프트에 "
                        f"{ROLE_LABELS[role]} 기록이 없어 추천을 중단합니다."
                    )
            # Only now is this player fully collected: account, both queues'
            # matches, rank, and a non-empty role history.
            await progress(f"{ROLE_LABELS[role]} 수집 완료 ({label})", role=role, done=True)
            opponents.append((player_id, role.upper()))
    return opponents, warnings


def _recommendation(
    repo: ScoutingRepo, opponents: list[tuple[int, str]],
    collection_warnings: tuple[str, ...] = (),
) -> Recommendation:
    result = recommend_bans(repo, opponents)
    warnings = list(collection_warnings)
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


def _build_discord_report(
    opponents: list[tuple[int, str]], cutoff_time: int,
    collection_warnings: tuple[str, ...] = (),
):
    """
    cutoff_time을 명시적으로 받음(호출부가 db.now_ms()를 흘려 넣던 것에서 변경) -
    버튼으로 리포트를 다시 그릴 때(재시작 후 복구 포함) 처음 만들 때와 정확히
    같은 컷오프로 재계산해야, 그 사이 새로 수집된 경기 때문에 픽 풀/위험도가
    페이지 넘길 때마다 달라져 보이는 일이 없음.
    """
    # Create/use/close SQLite on the worker thread. Presentation-only metadata
    # reads reuse the same cutoff and never add API calls or change collection.
    with ScoutingRepo(cutoff_time=cutoff_time) as repo:
        result = _recommendation(repo, opponents, collection_warnings)
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


class BanProgressStatus:
    """Per-player collection progress in one bot-owned channel message.

    Lives on a plain channel message the job sends itself, not on the
    interaction reply: the job outlives the interaction token, and both
    /banrecommend and /clashban run through the same job body, so both get
    this for free.

    Every Discord call here is best-effort - a failed send or edit is logged
    and swallowed, never allowed to fail the analysis it is reporting on. If
    the initial send fails there is no message to edit and every later update
    is skipped silently.
    """

    def __init__(self, channel, inputs: dict[str, str]):
        self.channel = channel
        self.inputs = inputs
        self.done: set[str] = set()
        self.current: str | None = None
        self.footer = f"0 / {len(ROLE_INPUTS)} 완료"
        self.message = None
        self.rendered = None

    def render(self) -> str:
        lines = ["🔎 밴 추천 분석 중", ""]
        for role in ROLE_INPUTS:
            if role in self.done:
                mark = "✅"
            elif role == self.current:
                mark = "⏳"
            else:
                mark = "⬜"
            lines.append(f"{mark} {ROLE_LABELS[role]} — {self.inputs[role]}")
        lines.extend(["", self.footer])
        return "\n".join(lines)[:1900]

    async def start(self) -> None:
        self.rendered = self.render()
        try:
            self.message = await self.channel.send(
                self.rendered, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            logger.warning("Ban progress status message could not be sent", exc_info=True)

    async def _refresh(self) -> None:
        content = self.render()
        # Several collection stages per player render identically (both queues
        # while that player is the ⏳ one); skipping those keeps this well
        # clear of Discord's per-message edit rate limit.
        if self.message is None or content == self.rendered:
            return
        self.rendered = content
        try:
            await self.message.edit(
                content=content, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            logger.warning("Ban progress status message could not be edited", exc_info=True)

    async def set_current(self, role: str) -> None:
        """Mark `role` as the player being worked on right now."""
        if role in self.done:
            return
        self.current = role
        await self._refresh()

    async def mark_done(self, role: str) -> None:
        """Mark `role` finished - account, matches and rank all collected."""
        self.done.add(role)
        if self.current == role:
            self.current = None
        self.footer = f"{len(self.done)} / {len(ROLE_INPUTS)} 완료"
        await self._refresh()

    async def start_calculation(self) -> None:
        self.current = None
        self.footer = f"✅ {len(self.done)} / {len(ROLE_INPUTS)} 수집 완료 · 밴 계산 중..."
        await self._refresh()

    async def complete(self) -> None:
        self.current = None
        self.footer = f"✅ {len(self.done)} / {len(ROLE_INPUTS)} 수집 완료 · 분석 완료"
        await self._refresh()

    async def fail(self, stage: str) -> None:
        """Leave the message on the stage that stopped it, not a stale ⏳.

        The actionable error text is still its own message; this only keeps
        the status message from looking like it is still running forever.
        """
        self.current = None
        self.footer = f"❌ {stage}에서 중단됨"
        await self._refresh()


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
    status = BanProgressStatus(channel, inputs)
    await status.start()

    async def progress(message: str, role: str | None = None, done: bool = False):
        # job.stage stays the source of truth for failure messages; the status
        # message is a purely cosmetic, best-effort mirror of per-player state.
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
                opponents, collection_warnings = await prepare_opponents(inputs, progress, depth)
                await progress("밴 계산 중")
                await status.start_calculation()
                # Captured AFTER collection finishes, not before: a freshly
                # collected match's game_end can land at/after a cutoff taken
                # earlier, which would make build_player_model see "no games
                # before cutoff" and drop a player who was just collected.
                cutoff_time = db.now_ms()
                result, presentations = await asyncio.to_thread(
                    _build_discord_report, opponents, cutoff_time, tuple(collection_warnings)
                )
    except TimeoutError:
        await status.fail(job.stage)
        await channel.send(
            "❌ 처리 시간이 초과되어 추천을 중단했습니다. "
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
        logger.exception("BANRECOMMEND background job failed at %s", job.stage)
        await status.fail(job.stage)
        await channel.send(
            f"❌ {job.stage}: 오류가 발생해 추천을 중단했습니다. 봇 로그를 확인해주세요.",
            allowed_mentions=mentions,
        )
        raise

    await progress("완료")
    await status.complete()
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
        target_page = max(0, min(LAST_PAGE_INDEX, row["page_index"] + offset))

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

    depth_choices = [
        app_commands.Choice(name="빠르게 · 30경기", value="quick"),
        app_commands.Choice(name="기본 · 100경기", value="normal"),
        app_commands.Choice(name="깊게 · 200경기", value="deep"),
    ]

    @bot.tree.command(name="banrecommend", description="상대 5명의 역할별 Riot ID로 밴 3개를 추천합니다.")
    @app_commands.describe(
        top="TOP 선수 (이름#태그)", jungle="JUNGLE 선수 (이름#태그)",
        middle="MID 선수 (이름#태그)", bottom="BOTTOM 선수 (이름#태그)",
        utility="SUPPORT 선수 (이름#태그)",
        depth="수집 깊이: 빠르게 30 / 기본 100 / 깊게 200 경기, 선수·큐별",
    )
    @app_commands.choices(depth=depth_choices)
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

    @bot.tree.command(
        name="clashban",
        description="상대 한 명의 Riot ID로 격전 팀을 찾아 밴 3개를 추천합니다.",
    )
    @app_commands.describe(
        riot_id="상대 팀원 한 명 (이름#태그)",
        depth="수집 깊이: 빠르게 30 / 기본 100 / 깊게 200 경기, 선수·큐별",
    )
    @app_commands.choices(depth=depth_choices)
    async def clashban(interaction: discord.Interaction, riot_id: str, depth: str = "normal"):
        """/banrecommend와 같은 파이프라인을, 역할별 Riot ID 5개 대신 격전 팀
        조회로 채워 실행함. 밴 로직·수집·리포트는 전부 기존 것을 그대로 씀.

        로스터 조회만 커맨드 안에서(백그라운드 job 전에) 처리함 - 그래야 팀을
        못 찾거나 로스터가 5명/5포지션이 아닐 때 job을 만들지 않고 바로 알려줄
        수 있고, 확정된 5명으로 만든 dedupe key가 /banrecommend의 것과 정확히
        같아져서 같은 팀을 두 커맨드로 동시에 분석하는 일도 막힘.
        """
        if depth not in DEPTH_CAPS:
            await interaction.response.send_message(
                "❌ 수집 깊이는 빠르게, 기본, 깊게 중에서 선택해주세요.", ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            inputs = await resolve_clash_inputs(riot_id)
            parsed = parse_inputs(inputs)
        except (BanCommandError, RiotApiError) as exc:
            await interaction.followup.send(f"❌ {exc}"[:1900])
            return
        except Exception:
            logger.exception("CLASHBAN roster lookup failed for %r", riot_id)
            await interaction.followup.send("❌ 격전 팀 정보를 불러오지 못했습니다. 봇 로그를 확인해주세요.")
            return

        channel = interaction.channel

        async def runner(job: ScoutingJob) -> None:
            await _run_banrecommend_job(job, inputs, depth, channel, interaction.user.id, interaction.guild_id)

        # Same key shape as /banrecommend: one in-flight collection per team,
        # whichever command asked for it.
        job, created = scouting_jobs.start(
            key=_team_key(parsed), kind="clashban", user_id=interaction.user.id,
            guild_id=interaction.guild_id, channel_id=channel.id,
            players=tuple(parsed), runner=runner,
        )

        if not created:
            await interaction.followup.send("이미 같은 팀을 분석 중입니다.")
            return

        roster = " · ".join(f"{ROLE_LABELS[role]} {inputs[role]}" for role in ROLE_INPUTS)
        await interaction.followup.send(
            (f"🔎 격전 팀을 찾았습니다 ({roster}).\n"
             "밴 추천 분석을 시작했습니다. 완료되면 이 채널에 결과를 보내드릴게요.")[:1900],
            allowed_mentions=discord.AllowedMentions.none(),
        )
