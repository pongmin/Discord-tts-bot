"""
숙련도/랭크 스냅샷 수집 CLI.

사용법:
    python collect_snapshots.py "Hide on bush#KR1"
    python collect_snapshots.py "Hide on bush#KR1" "Faker#KR1"

각 Riot ID로 계정을 찾고, 그 puuid로 전체 챔피언 숙련도(Champion-Mastery-V4)와
솔로랭크/자유랭크 큐 항목(League-V4)을 받아서 mastery_snapshots/rank_snapshots에
새 스냅샷으로 저장함. 기존 스냅샷은 절대 덮어쓰지 않음.
"""

import argparse
import asyncio

from dotenv import load_dotenv

from riot_api import parse_riot_id, get_account_by_riot_id, RiotApiError
from snapshot_collector import collect_player_snapshots
from http_session import close_session


async def _run(riot_ids: list[str]) -> None:
    for riot_id in riot_ids:
        game_name, tag_line = parse_riot_id(riot_id)

        try:
            account = await get_account_by_riot_id(game_name, tag_line)
        except RiotApiError as e:
            print(f"계정을 찾지 못함 ({riot_id}): {e}")
            continue

        print(f"수집 시작: {account.game_name}#{account.tag_line} (puuid={account.puuid})")

        result = await collect_player_snapshots(
            puuid=account.puuid,
            game_name=account.game_name,
            tag_line=account.tag_line,
        )

        print(
            f"숙련도 {result['mastery_stored']}개 / 랭크 {result['rank_stored']}개 저장 "
            f"(snapshot_at={result['snapshot_at']})"
        )

        if result["aborted"]:
            print("API 키 문제로 중단됨. RIOT_API_KEY를 확인하고 다시 실행해줘.")
            break


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="숙련도/랭크 스냅샷 수집기")
    parser.add_argument("riot_ids", nargs="+", help="예: Hide on bush#KR1 (여러 개 가능)")

    args = parser.parse_args()

    async def _main():
        try:
            await _run(args.riot_ids)
        finally:
            await close_session()

    asyncio.run(_main())


if __name__ == "__main__":
    main()
