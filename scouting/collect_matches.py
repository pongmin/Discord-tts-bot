"""
매치 수집 CLI.

사용법:
    python -m scouting.collect_matches "Hide on bush#KR1"
    python -m scouting.collect_matches "Hide on bush#KR1" --max-count 200
    python -m scouting.collect_matches "Hide on bush#KR1" --queues 420,400,490

Riot ID로 계정을 찾고, 그 puuid로 --queues에 지정한 큐들의 매치를 각각
최대 max-count개까지 받아서 data/raw_matches/에 저장하고 data/scouting.db에
색인함. 큐별로 진행 상황이 독립적으로 추적되므로, 중간에 실패해도 다시
실행하면 이미 캐시된 매치는 건너뛰고 큐별로 이어서 진행함.
"""

import argparse
import asyncio

from dotenv import load_dotenv

from riot.riot_api import (
    parse_riot_id,
    get_account_by_riot_id,
    RiotApiError,
    RANKED_SOLO_QUEUE_ID,
    RANKED_FLEX_QUEUE_ID,
    ALLOWED_SCOUTING_QUEUE_IDS,
)
from scouting.match_collector import collect_player_matches
from http_session import close_session


def _parse_queues(value: str) -> list[int]:
    queue_ids = []

    for token in value.split(","):
        token = token.strip()

        if not token:
            continue

        try:
            queue_id = int(token)
        except ValueError:
            raise ValueError(f"큐 ID는 숫자여야 함: {token!r}")

        if queue_id == RANKED_FLEX_QUEUE_ID:
            raise ValueError("랭크 자유(440)는 수집 대상에서 제외됨")

        if queue_id not in ALLOWED_SCOUTING_QUEUE_IDS:
            allowed = ", ".join(str(q) for q in ALLOWED_SCOUTING_QUEUE_IDS)
            raise ValueError(f"지원하지 않는 큐 ID: {queue_id} (허용: {allowed})")

        if queue_id not in queue_ids:
            queue_ids.append(queue_id)

    if not queue_ids:
        raise ValueError("--queues에 유효한 큐 ID가 없음")

    return queue_ids


async def _run(riot_id: str, queue_ids: list[int], max_count: int, request_delay: float) -> None:
    game_name, tag_line = parse_riot_id(riot_id)

    try:
        account = await get_account_by_riot_id(game_name, tag_line)
    except RiotApiError as e:
        print(f"계정을 찾지 못함: {e}")
        return

    print(f"수집 시작: {account.game_name}#{account.tag_line} (puuid={account.puuid})")

    for queue_id in queue_ids:
        queue_name = ALLOWED_SCOUTING_QUEUE_IDS[queue_id]
        print(f"--- 큐 {queue_id} ({queue_name}) ---")

        result = await collect_player_matches(
            puuid=account.puuid,
            game_name=account.game_name,
            tag_line=account.tag_line,
            queue_id=queue_id,
            max_count=max_count,
            request_delay=request_delay,
        )

        print(
            f"요청 {result['requested']}개 / 신규 {result['newly_fetched']}개 / "
            f"캐시에서 재사용 {result['skipped_cached']}개 / "
            f"영구 제외 {result['permanently_skipped']}개 / 실패 {result['failed']}개"
        )

        if result["aborted"]:
            print("API 키 문제로 중단됨. RIOT_API_KEY를 확인하고 다시 실행해줘.")
            # 키 문제면 다른 큐를 시도해도 전부 똑같이 실패하므로 여기서 멈춤
            break
        elif result["is_complete"]:
            print("이 큐에 대한 수집 완료.")
        else:
            print("일부만 수집됨(일시적 실패). 다시 실행하면 이 큐는 남은 부분부터 이어서 받음.")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="매치 수집기")
    parser.add_argument("riot_id", help="예: Hide on bush#KR1")
    parser.add_argument(
        "--queues",
        default=str(RANKED_SOLO_QUEUE_ID),
        help=(
            f"쉼표로 구분한 큐 ID 목록 (기본 {RANKED_SOLO_QUEUE_ID}=솔로랭크). "
            f"허용값: {', '.join(f'{qid}={name}' for qid, name in ALLOWED_SCOUTING_QUEUE_IDS.items())}"
        ),
    )
    parser.add_argument("--max-count", type=int, default=200, help="큐별로 가져올 최대 매치 수 (기본 200)")
    parser.add_argument("--request-delay", type=float, default=1.2, help="매치 상세 요청 사이 대기 시간(초)")

    args = parser.parse_args()

    try:
        queue_ids = _parse_queues(args.queues)
    except ValueError as e:
        print(f"--queues 값이 잘못됨: {e}")
        return

    async def _main():
        try:
            await _run(args.riot_id, queue_ids, args.max_count, args.request_delay)
        finally:
            await close_session()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
