"""
솔로 랭크 매치 수집 CLI.

사용법:
    python collect_matches.py "Hide on bush#KR1"
    python collect_matches.py "Hide on bush#KR1" --max-count 200

Riot ID로 계정을 찾고, 그 puuid로 최신 솔로 랭크 매치를 최대 max-count개까지
받아서 data/raw_matches/에 저장하고 data/scouting.db에 색인함.
중간에 실패해도 다시 실행하면 이미 캐시된 매치는 건너뛰고 이어서 진행함.
"""

import argparse
import asyncio

from dotenv import load_dotenv

from riot_api import parse_riot_id, get_account_by_riot_id, RiotApiError
from match_collector import collect_player_matches
from http_session import close_session


async def _run(riot_id: str, max_count: int, request_delay: float) -> None:
    game_name, tag_line = parse_riot_id(riot_id)

    try:
        account = await get_account_by_riot_id(game_name, tag_line)
    except RiotApiError as e:
        print(f"계정을 찾지 못함: {e}")
        return

    print(f"수집 시작: {account.game_name}#{account.tag_line} (puuid={account.puuid})")

    result = await collect_player_matches(
        puuid=account.puuid,
        game_name=account.game_name,
        tag_line=account.tag_line,
        max_count=max_count,
        request_delay=request_delay,
    )

    print(
        f"요청 {result['requested']}개 / 신규 {result['newly_fetched']}개 / "
        f"캐시에서 재사용 {result['skipped_cached']}개 / 실패 {result['failed']}개"
    )

    if result["aborted"]:
        print("API 키 문제로 중단됨. RIOT_API_KEY를 확인하고 다시 실행해줘.")
    elif result["is_complete"]:
        print("이 큐에 대한 수집 완료.")
    else:
        print("일부만 수집됨. 다시 실행하면 남은 부분부터 이어서 받음.")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="솔로 랭크 매치 수집기")
    parser.add_argument("riot_id", help="예: Hide on bush#KR1")
    parser.add_argument("--max-count", type=int, default=200, help="가져올 최대 매치 수 (기본 200)")
    parser.add_argument("--request-delay", type=float, default=1.2, help="매치 상세 요청 사이 대기 시간(초)")

    args = parser.parse_args()

    async def _main():
        try:
            await _run(args.riot_id, args.max_count, args.request_delay)
        finally:
            await close_session()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
