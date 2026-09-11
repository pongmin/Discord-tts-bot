"""
스냅샷(숙련도/랭크) 수집기.

Champion-Mastery-V4로 전체 챔피언 숙련도를, League-V4로 솔로랭크/자유랭크
큐 항목을 받아서 mastery_snapshots/rank_snapshots에 append-only로 저장함.
한 번의 수집 호출 안에서는 같은 snapshot_at(now_ms())을 모든 행에 씀.

실패 처리는 match_collector와 동일한 원칙을 따름:
- 429는 backoff 후 이번 실행에서는 넘어감(다시 실행하면 재시도됨).
- 401/403은 API 키 문제이므로 즉시 중단(aborted=True)하고 이후 요청을 하지 않음.
- 그 외 실패는 fetch_failures에 남기고 계속 진행함.
"""

import asyncio

from riot.riot_api import (
    get_champion_masteries,
    get_league_entries,
    RANKED_SOLO_QUEUE_TYPE,
    RANKED_FLEX_QUEUE_TYPE,
    InvalidApiKeyError,
    RateLimitedError,
    RiotServerError,
    RiotApiError,
)
from scouting import scouting_db as db


# 429를 받았을 때 Retry-After가 없으면 대신 쓰는 대기 시간(초)
DEFAULT_RATE_LIMIT_BACKOFF = 5

_ALLOWED_RANK_QUEUE_TYPES = {RANKED_SOLO_QUEUE_TYPE, RANKED_FLEX_QUEUE_TYPE}


async def _fetch_champion_masteries(conn, puuid: str):
    """
    (masteries, aborted) 튜플을 반환함. aborted면 masteries는 항상 None.
    """
    try:
        return await get_champion_masteries(puuid), False
    except InvalidApiKeyError as e:
        db.record_failure(conn, "champion_mastery", retryable=False, message=str(e))
        return None, True
    except RateLimitedError as e:
        db.record_failure(
            conn, "champion_mastery", retryable=True,
            status_code=e.status_code, message=str(e)
        )
        await asyncio.sleep(e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF)
        return None, False
    except RiotServerError as e:
        db.record_failure(
            conn, "champion_mastery", retryable=True,
            status_code=e.status_code, message=str(e)
        )
        return None, False
    except RiotApiError as e:
        db.record_failure(conn, "champion_mastery", retryable=False, message=str(e))
        return None, False


async def _fetch_league_entries(conn, puuid: str):
    """
    (entries, aborted) 튜플을 반환함. aborted면 entries는 항상 None.
    """
    try:
        return await get_league_entries(puuid), False
    except InvalidApiKeyError as e:
        db.record_failure(conn, "league_entries", retryable=False, message=str(e))
        return None, True
    except RateLimitedError as e:
        db.record_failure(
            conn, "league_entries", retryable=True,
            status_code=e.status_code, message=str(e)
        )
        await asyncio.sleep(e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF)
        return None, False
    except RiotServerError as e:
        db.record_failure(
            conn, "league_entries", retryable=True,
            status_code=e.status_code, message=str(e)
        )
        return None, False
    except RiotApiError as e:
        db.record_failure(conn, "league_entries", retryable=False, message=str(e))
        return None, False


async def collect_player_snapshots(
    puuid: str,
    game_name: str | None = None,
    tag_line: str | None = None,
    platform: str = "KR",
    conn=None,
) -> dict:
    owns_conn = conn is None
    conn = conn or db.get_connection()
    db.init_db(conn)

    try:
        player_id = db.get_or_create_player(conn, puuid, game_name, tag_line, platform)
        snapshot_at = db.now_ms()

        mastery_stored = 0
        rank_stored = 0

        masteries, aborted = await _fetch_champion_masteries(conn, puuid)

        if masteries:
            for m in masteries:
                db.insert_mastery_snapshot(
                    conn, player_id, m.champion_id, m.mastery_points,
                    m.mastery_level, m.last_play_time, snapshot_at
                )
                mastery_stored += 1
            conn.commit()

        if not aborted:
            entries, league_aborted = await _fetch_league_entries(conn, puuid)
            aborted = aborted or league_aborted

            if entries:
                for e in entries:
                    if e.queue_type not in _ALLOWED_RANK_QUEUE_TYPES:
                        continue

                    db.insert_rank_snapshot(
                        conn, player_id, e.queue_type, e.tier, e.division,
                        e.lp, e.wins, e.losses, snapshot_at
                    )
                    rank_stored += 1
                conn.commit()

        return {
            "player_id": player_id,
            "snapshot_at": snapshot_at,
            "mastery_stored": mastery_stored,
            "rank_stored": rank_stored,
            "aborted": aborted,
        }

    finally:
        if owns_conn:
            conn.close()
