"""
데이터 두께 / 노벨 픽 분석 CLI.

Riot API를 호출하지 않고 data/scouting.db에 이미 수집된 데이터만 사용함.
(먼저 collect_matches.py로 매치를 수집해 둬야 함)

사용법:
    python analyze_thickness.py "Hide on bush#KR1" --role MIDDLE
    python analyze_thickness.py "P1#KR1" "P2#KR1" "P3#KR1" --role JUNGLE
    python analyze_thickness.py "Hide on bush#KR1" --role MIDDLE --cutoff 2025-01-01T00:00:00
"""

import argparse
import json
from datetime import datetime, timezone

from match_position import VALID_POSITIONS
from scouting_repo import ScoutingRepo
from scouting_analysis import compute_player_thickness, summarize_group


def _resolve_player_id(repo: ScoutingRepo, identifier: str) -> int | None:
    if "#" in identifier:
        game_name, tag_line = identifier.split("#", 1)
        return repo.get_player_id_by_riot_id(game_name.strip(), tag_line.strip())

    # '#'이 없으면 puuid로 취급함
    return repo.get_player_id_by_puuid(identifier)


def _parse_cutoff(value: str | None) -> int | None:
    if value is None:
        return None

    dt = datetime.fromisoformat(value)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return int(dt.timestamp() * 1000)


def _format_result(result: dict) -> str:
    novel_str = ", ".join(
        f"{window}경기={rate:.1%}" if rate is not None else f"{window}경기=N/A"
        for window, rate in result["novel_pick_rate"].items()
    )

    latest_patch = result["latest_patch"] or "N/A"
    pct_latest = f"{result['pct_on_latest_patch']:.1%}" if result["pct_on_latest_patch"] is not None else "N/A"

    return (
        f"  role-matched games : {result['role_game_count']}\n"
        f"  unique champions   : {result['unique_champion_count']}\n"
        f"  top1 / top3 / top5 : "
        f"{_fmt_pct(result['top1_concentration'])} / "
        f"{_fmt_pct(result['top3_concentration'])} / "
        f"{_fmt_pct(result['top5_concentration'])}\n"
        f"  effective pool     : {result['effective_champion_pool']:.2f}\n"
        f"  games last 30 days : {result['games_last_30_days']}\n"
        f"  latest patch       : {latest_patch} ({pct_latest} of games)\n"
        f"  novel pick rate    : {novel_str}"
    )


def _fmt_pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "N/A"


def _format_stats(label: str, stats: dict) -> str:
    if stats["n"] == 0:
        return f"  {label}: N/A (표본 없음)"

    return f"  {label}: median={stats['median']:.3g}, p25={stats['p25']:.3g}, p75={stats['p75']:.3g} (n={stats['n']})"


def main() -> None:
    parser = argparse.ArgumentParser(description="데이터 두께 / 노벨 픽 분석")
    parser.add_argument("identifiers", nargs="+", help="Riot ID(이름#태그) 또는 puuid, 여러 개 가능")
    parser.add_argument("--role", required=True, choices=sorted(VALID_POSITIONS), help="분석할 포지션")
    parser.add_argument("--queue-id", type=int, default=420, help="큐 ID (기본 420 = 솔로랭크)")
    parser.add_argument("--cutoff", default=None, help="ISO 날짜/시각. 이 시점 이후 매치는 제외함 (백테스트용)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")

    args = parser.parse_args()

    cutoff_ms = _parse_cutoff(args.cutoff)
    repo = ScoutingRepo(cutoff_time=cutoff_ms)

    try:
        results = []

        for identifier in args.identifiers:
            player_id = _resolve_player_id(repo, identifier)

            if player_id is None:
                print(f"DB에서 찾을 수 없음: {identifier} (먼저 collect_matches.py로 수집해줘)")
                continue

            result = compute_player_thickness(repo, player_id, args.role, queue_id=args.queue_id)
            result["identifier"] = identifier
            results.append(result)

        if args.json:
            output = {"players": results}

            if len(results) > 1:
                output["group_summary"] = summarize_group(results)

            print(json.dumps(output, ensure_ascii=False, indent=2))
            return

        for result in results:
            print(f"=== {result['identifier']} ({result['role']}) ===")
            print(_format_result(result))
            print()

        if len(results) > 1:
            summary = summarize_group(results)

            print(f"=== 그룹 요약 (n={len(results)}) ===")
            print(_format_stats("role-game count", summary["role_game_count"]))
            print(_format_stats("effective pool", summary["effective_champion_pool"]))
            print(_format_stats("top3 concentration", summary["top3_concentration"]))

            for window, stats in summary["novel_pick_rate"].items():
                print(_format_stats(f"novel pick rate ({window}경기)", stats))

    finally:
        repo.close()


if __name__ == "__main__":
    main()
