"""Discord adapter for the unchanged v1 ban algorithm and scouting pipeline."""

import asyncio
from contextlib import closing
from dataclasses import replace
import logging
from typing import Literal

import discord
from discord import app_commands

from ban_algorithm import recommend_bans, format_recommendation
from match_collector import collect_player_matches
from riot_api import (
    parse_riot_id, get_account_by_riot_id, get_league_entries,
    InvalidRiotIdError, RiotApiError, RANKED_SOLO_QUEUE_TYPE,
)
import scouting_db as db
from scouting_repo import ScoutingRepo


logger = logging.getLogger(__name__)
ROLE_INPUTS = ("top", "jungle", "middle", "bottom", "utility")
QUEUE_IDS = (420, 400)
# Placeholder collection budgets, not tuned. Estimates are per cold-cache player:
# normal/deep follow the measured ~9 min/200-game rate; quick is a rough fraction.
DEPTH_CAPS = {"quick": 30, "normal": 100, "deep": 200}
DEPTH_ESTIMATES = {"quick": "1–2", "normal": "4–5", "deep": "9"}
LOW_ROLE_GAME_COUNT = 20  # Advisory placeholder only; never a collection target.
RANK_CACHE_MS = 24 * 60 * 60 * 1000
# Leave time to report failure before Discord's 15-minute interaction expiry.
COMMAND_TIMEOUT_SECONDS = 12 * 60


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
                raise InvalidRiotIdError("GameName#TagLine 형식으로 입력해주세요.")
        except InvalidRiotIdError as exc:
            raise BanCommandError(f"{role} 입력 오류 ({raw}): {exc}") from exc
        key = (game_name.casefold(), tag_line.casefold())
        if key in seen:
            raise BanCommandError(f"동일 계정 중복 입력: {seen[key]}, {role} ({raw})")
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
        raise BanCommandError("depth must be quick, normal, or deep.")
    cap = DEPTH_CAPS[depth]
    parsed = parse_inputs(inputs)
    resolved = []
    seen_puuids = {}
    # Resolve all five before writing/collecting; aliases can map to one PUUID.
    for role, game_name, tag_line in parsed:
        label = f"{game_name}#{tag_line}"
        await progress(f"{role} 계정 조회 ({label})")
        account = await get_account_by_riot_id(game_name, tag_line)
        if account.puuid in seen_puuids:
            raise BanCommandError(
                f"동일 계정 중복: {seen_puuids[account.puuid]}, {role} ({label})"
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
                await progress(f"{role} 경기 확보 ({label}, 큐 {queue_id})")
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
                        f"{role} 수집 실패 ({label}, 큐 {queue_id}): "
                        f"실패 {result['failed']}건, 중단={result['aborted']}, "
                        f"완료={result['is_complete']}. 추천을 중단합니다."
                    )
                if result["player_id"] != player_id:
                    raise BanCommandError(f"{role} 수집 계정 불일치 ({label}). 추천을 중단합니다.")
            await progress(f"{role} 솔로 랭크 확보 ({label})")
            await _ensure_solo_rank(conn, player_id, account)
            with ScoutingRepo(cutoff_time=db.now_ms(), conn=conn) as repo:
                if not repo.get_role_matches(player_id, role.upper(), queue_ids=QUEUE_IDS):
                    raise BanCommandError(
                        f"{role} 데이터 부족 ({label}): 수집된 SoloQ/Normal Draft에 "
                        f"{role.upper()} 기록이 없어 추천을 중단합니다."
                    )
            opponents.append((player_id, role.upper()))
    return opponents


def _recommend_report(opponents: list[tuple[int, str]]) -> str:
    # Create/use/close SQLite on the worker thread; don't block Discord's loop
    # while the algorithm runs its three exhaustive searches.
    with ScoutingRepo(cutoff_time=db.now_ms()) as repo:
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
        # Recommendation is frozen. Extend its warnings in the adapter, keeping
        # algorithm warnings, scoring, and its full-history DB reads unchanged.
        result = replace(result, warnings=result.warnings + tuple(warnings))
        return format_recommendation(result)


def report_chunks(report: str) -> list[str]:
    """Preserve every line, including diagnostics/warnings, within message limits."""
    # Prevent user-controlled Riot names from closing the report's code fences.
    text = report.replace("```", "`\u200b``")
    chunks = []
    while text:
        # 950 Unicode code points are <=1900 UTF-16 units, even with emoji.
        end = min(len(text), 950)
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        chunks.append("```\n" + text[:end] + "\n```")
        text = text[end:]
    return chunks


def setup_ban_commands(bot):
    # Serialize this expensive command, without delaying unrelated commands.
    running = asyncio.Lock()

    @bot.tree.command(name="banrecommend", description="상대 5명의 역할별 Riot ID로 밴 3개를 추천합니다.")
    @app_commands.describe(
        top="TOP: GameName#TagLine", jungle="JUNGLE: GameName#TagLine",
        middle="MIDDLE: GameName#TagLine", bottom="BOTTOM: GameName#TagLine",
        utility="UTILITY: GameName#TagLine",
        depth="수집 깊이: quick 30 / normal 100 (기본) / deep 200 경기, 선수·큐별",
    )
    async def banrecommend(
        interaction: discord.Interaction, top: str, jungle: str,
        middle: str, bottom: str, utility: str,
        depth: Literal["quick", "normal", "deep"] = "normal",
    ):
        await interaction.response.defer(thinking=True)
        mentions = discord.AllowedMentions.none()
        if running.locked():
            await interaction.followup.send(
                "다른 밴 추천을 처리 중입니다. 완료 후 다시 시도해주세요.", allowed_mentions=mentions
            )
            return

        stage = "입력 확인"

        async def progress(message: str):
            nonlocal stage
            stage = message
            await interaction.edit_original_response(
                content=(
                    f"Collecting with depth={depth} (up to {DEPTH_CAPS[depth]} games/queue per player). "
                    f"Cold cache: roughly {DEPTH_ESTIMATES[depth]} min/player; "
                    "five players may take longer. Cached matches are reused.\n"
                    f"밴 추천 준비 중: {message}"
                ),
                allowed_mentions=mentions,
            )

        async with running:
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                    await progress("입력 확인")
                    opponents = await prepare_opponents(
                        dict(zip(ROLE_INPUTS, (top, jungle, middle, bottom, utility))), progress, depth
                    )
                    await progress("추천 계산")
                    report = await asyncio.to_thread(_recommend_report, opponents)
            except TimeoutError:
                await interaction.edit_original_response(
                    content=f"❌ {stage}: 처리 시간이 초과되어 추천을 중단했습니다. "
                    "저장된 경기는 유지됩니다. 다시 실행하면 캐시를 재사용합니다.",
                    allowed_mentions=mentions,
                )
                return
            except (BanCommandError, RiotApiError, ValueError) as exc:
                await interaction.edit_original_response(
                    content=(f"❌ {stage}: {exc}")[:950], allowed_mentions=mentions,
                )
                return
            except Exception:
                logger.exception("BANRECOMMEND failed at %s", stage)
                await interaction.edit_original_response(
                    content=f"❌ {stage}: 오류가 발생해 추천을 중단했습니다. 봇 로그를 확인해주세요.",
                    allowed_mentions=mentions,
                )
                return

            chunks = report_chunks(report)
            await interaction.edit_original_response(content=chunks[0], allowed_mentions=mentions)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, allowed_mentions=mentions)
