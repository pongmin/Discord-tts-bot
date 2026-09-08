"""
솔로 랭크 매치 수집기.

Riot Match-V5에서 매치 ID -> 매치 상세를 받아와서
raw_match_store(원본 압축 저장)와 scouting_db(색인/파생 데이터)에 채워 넣음.

재시작해도 이어서 할 수 있어야 하므로:
- 이미 원본 파일 + matches 색인이 둘 다 있는 매치는 다시 내려받지 않음.
- 매치 ID 목록만 다시 받고, 실제로 없는 것만 상세 조회함.
- 실패는 fetch_failures에 남기고, 재시도 가능 여부(retryable)를 구분함.
"""

import asyncio

from riot_api import (
    get_solo_queue_match_ids,
    get_match_by_id,
    RANKED_SOLO_QUEUE_ID,
    RiotApiError,
    InvalidApiKeyError,
    RateLimitedError,
    RiotServerError,
    MatchNotFoundError,
)
from match_position import derive_canonical_role, is_role_mismatch
from raw_match_store import save_raw_match
import scouting_db as db


# Riot 개인 개발자 키 기준 요청 간 최소 간격(초). 너무 빠르게 쏘면 429가 남.
DEFAULT_REQUEST_DELAY = 1.2

# 429를 받았을 때 Retry-After가 없으면 대신 쓰는 대기 시간(초)
DEFAULT_RATE_LIMIT_BACKOFF = 5


def _derive_patch(game_version: str | None) -> str | None:
    if not game_version:
        return None

    parts = game_version.split(".")

    if len(parts) < 2:
        return game_version

    return f"{parts[0]}.{parts[1]}"


def _derive_game_end(info: dict) -> int | None:
    game_end = info.get("gameEndTimestamp")

    if game_end:
        return game_end

    game_start = info.get("gameStartTimestamp")
    duration = info.get("gameDuration")

    if game_start is None or duration is None:
        return None

    # 아주 오래된 매치는 gameDuration이 이미 ms 단위였음. 그 외에는 초 단위.
    duration_ms = duration if duration > 100_000 else duration * 1000

    return game_start + duration_ms


def _build_match_row(raw_data: dict) -> dict:
    info = raw_data["info"]

    return {
        "game_start": info.get("gameStartTimestamp"),
        "game_end": _derive_game_end(info),
        "game_version": info.get("gameVersion"),
        "patch": _derive_patch(info.get("gameVersion")),
        "queue_id": info.get("queueId"),
        "game_duration": info.get("gameDuration"),
    }


def _find_participant(raw_data: dict, puuid: str) -> dict | None:
    for participant in raw_data["info"]["participants"]:
        if participant.get("puuid") == puuid:
            return participant

    return None


def _build_participant_row(participant: dict) -> dict:
    return {
        "team_id": participant.get("teamId"),
        "participant_id": participant.get("participantId"),
        "champion_id": participant.get("championId"),
        "champion_name": participant.get("championName"),
        "team_position": participant.get("teamPosition") or "",
        "individual_position": participant.get("individualPosition") or "",
        "canonical_role": derive_canonical_role(participant),
        "role_mismatch": is_role_mismatch(participant),
        "win": bool(participant.get("win")),
        "kills": participant.get("kills"),
        "deaths": participant.get("deaths"),
        "assists": participant.get("assists"),
    }


def _store_match_for_player(conn, player_id: int, puuid: str, match_id: str, raw_data: dict) -> bool:
    """
    원본 저장 + matches 색인 + 이 플레이어의 player_matches 행 upsert.
    해당 puuid의 참가자 정보를 raw_data에서 못 찾으면(있어선 안 되는 상황) False를 반환함.
    """
    participant = _find_participant(raw_data, puuid)

    if participant is None:
        return False

    path = save_raw_match(match_id, raw_data)

    match_row = _build_match_row(raw_data)
    db.upsert_match(conn, match_id, match_row, str(path))

    participant_row = _build_participant_row(participant)
    db.upsert_player_match(conn, player_id, match_id, participant_row)

    conn.commit()
    return True


def _ensure_player_match_from_cache(conn, player_id: int, puuid: str, match_id: str) -> None:
    """
    matches/raw 파일은 이미 있는데(다른 선수 수집 때 받았을 수 있음)
    이 player_id의 player_matches 행이 아직 없으면, 다시 내려받지 않고
    캐시된 원본에서만 채워 넣음.
    """
    if db.has_player_match_row(conn, player_id, match_id):
        return

    from raw_match_store import load_raw_match

    raw_data = load_raw_match(match_id)
    participant = _find_participant(raw_data, puuid)

    if participant is None:
        return

    participant_row = _build_participant_row(participant)
    db.upsert_player_match(conn, player_id, match_id, participant_row)
    conn.commit()


async def collect_player_matches(
    puuid: str,
    game_name: str | None = None,
    tag_line: str | None = None,
    platform: str = "KR",
    queue_id: int = RANKED_SOLO_QUEUE_ID,
    max_count: int = 200,
    request_delay: float = DEFAULT_REQUEST_DELAY,
    conn=None,
) -> dict:
    owns_conn = conn is None
    conn = conn or db.get_connection()
    db.init_db(conn)

    try:
        player_id = db.get_or_create_player(conn, puuid, game_name, tag_line, platform)
        db.touch_fetch_attempt(conn, player_id, queue_id)

        match_ids = await get_solo_queue_match_ids(puuid, count=max_count)

        newly_fetched = 0
        skipped_cached = 0
        failed = 0
        aborted = False

        for match_id in match_ids:
            if db.has_cached_match(conn, match_id):
                _ensure_player_match_from_cache(conn, player_id, puuid, match_id)
                skipped_cached += 1
                continue

            try:
                match_data = await get_match_by_id(match_id)

            except InvalidApiKeyError as e:
                db.record_failure(conn, "match_by_id", retryable=False, match_id=match_id, message=str(e))
                # 키 문제는 이후 요청도 전부 실패하므로 이번 실행은 여기서 멈춤
                aborted = True
                break

            except RateLimitedError as e:
                db.record_failure(
                    conn, "match_by_id", retryable=True, match_id=match_id,
                    status_code=e.status_code, message=str(e)
                )
                failed += 1
                await asyncio.sleep(e.retry_after or DEFAULT_RATE_LIMIT_BACKOFF)
                continue

            except RiotServerError as e:
                db.record_failure(
                    conn, "match_by_id", retryable=True, match_id=match_id,
                    status_code=e.status_code, message=str(e)
                )
                failed += 1
                continue

            except MatchNotFoundError as e:
                # 매치가 삭제됐거나 접근 불가한 경우. 재시도해도 소용없음.
                db.record_failure(conn, "match_by_id", retryable=False, match_id=match_id, message=str(e))
                failed += 1
                continue

            except RiotApiError as e:
                db.record_failure(conn, "match_by_id", retryable=False, match_id=match_id, message=str(e))
                failed += 1
                continue

            stored = _store_match_for_player(conn, player_id, puuid, match_id, match_data)

            if stored:
                newly_fetched += 1
            else:
                db.record_failure(
                    conn, "match_by_id", retryable=False, match_id=match_id,
                    message="참가자 목록에서 이 puuid를 찾을 수 없음"
                )
                failed += 1

            await asyncio.sleep(request_delay)

        cached_count = sum(1 for match_id in match_ids if db.has_cached_match(conn, match_id))
        is_complete = (not aborted) and cached_count == len(match_ids)

        db.refresh_fetch_state(conn, player_id, queue_id, is_complete)

        return {
            "player_id": player_id,
            "requested": len(match_ids),
            "newly_fetched": newly_fetched,
            "skipped_cached": skipped_cached,
            "failed": failed,
            "aborted": aborted,
            "is_complete": is_complete,
        }

    finally:
        if owns_conn:
            conn.close()
