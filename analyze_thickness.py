"""
데이터 두께 / 노벨 픽 분석 CLI.

Riot API를 호출하지 않고 data/scouting.db에 이미 수집된 데이터만 사용함.
(먼저 collect_matches.py로 매치를 수집해 둬야 함)

사용법:
    python analyze_thickness.py "Hide on bush#KR1" --role MIDDLE
    python analyze_thickness.py "P1#KR1" "P2#KR1" "P3#KR1" --role JUNGLE
    python analyze_thickness.py "Hide on bush#KR1" --role MIDDLE --cutoff 2025-01-01T00:00:00
    python analyze_thickness.py "Hide on bush#KR1" --role MIDDLE --queues 420,400,490

--queues에 솔로랭크(420) 외의 큐를 섞으면, 같은 선수에 대해 "ranked only"와
"ranked+normals" 두 조건의 novel pick rate를 나란히 보여줌 - 일반 게임을
데이터에 섞는 게 실제로 의미가 있는지 확인하기 위한 비교임.
"""

import argparse
import json
from datetime import datetime, timezone

from match_position import VALID_POSITIONS
from riot_api import RANKED_SOLO_QUEUE_ID, RANKED_FLEX_QUEUE_ID, ALLOWED_SCOUTING_QUEUE_IDS
from scouting_repo import ScoutingRepo
from scouting_analysis import compute_player_thickness, summarize_group

RANKED_ONLY_QUEUES = (RANKED_SOLO_QUEUE_ID,)


def _parse_queues(value: str) -> tuple[int, ...]:
    queue_ids = []

    for token in value.split(","):
        token = token.strip()

        if not token:
            continue

        queue_id = int(token)

        if queue_id == RANKED_FLEX_QUEUE_ID:
            raise ValueError("랭크 자유(440)는 분석 대상에서 제외됨")

        if queue_id not in ALLOWED_SCOUTING_QUEUE_IDS:
            allowed = ", ".join(str(q) for q in ALLOWED_SCOUTING_QUEUE_IDS)
            raise ValueError(f"지원하지 않는 큐 ID: {queue_id} (허용: {allowed})")

        if queue_id not in queue_ids:
            queue_ids.append(queue_id)

    if not queue_ids:
        raise ValueError("--queues에 유효한 큐 ID가 없음")

    return tuple(queue_ids)


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


def _fmt_date(ms: int | None) -> str:
    if ms is None:
        return "N/A"

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "N/A"


def _format_novel_pick_rate(novel_pick_rate: dict) -> str:
    return ", ".join(
        f"{window}경기={rate:.1%}" if rate is not None else f"{window}경기=N/A"
        for window, rate in novel_pick_rate.items()
    )


def _format_result(result: dict) -> str:
    latest_patch = result["latest_patch"] or "N/A"
    pct_latest = f"{result['pct_on_latest_patch']:.1%}" if result["pct_on_latest_patch"] is not None else "N/A"
    span_days = f"{result['span_days']:.1f}일" if result["span_days"] is not None else "N/A"

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
        f"  date range         : {_fmt_date(result['earliest_match_ms'])} ~ "
        f"{_fmt_date(result['latest_match_ms'])} ({span_days})\n"
        f"  novel pick rate    : {_format_novel_pick_rate(result['novel_pick_rate'])}"
    )


def _format_novel_pick_comparison(ranked_only: dict, ranked_plus_normals: dict, queue_label: str) -> str:
    lines = ["  novel pick rate - ranked only vs ranked+normals:"]

    for window in ranked_only["novel_pick_rate"]:
        ro_rate = ranked_only["novel_pick_rate"][window]
        rn_rate = ranked_plus_normals["novel_pick_rate"][window]

        ro_str = f"{ro_rate:.1%}" if ro_rate is not None else "N/A"
        rn_str = f"{rn_rate:.1%}" if rn_rate is not None else "N/A"

        lines.append(
            f"    {window:>3}경기: ranked only={ro_str:>6}  |  ranked+normals({queue_label})={rn_str:>6}"
        )

    lines.append(
        f"    (표본 수: ranked only n={ranked_only['role_game_count']}, "
        f"ranked+normals n={ranked_plus_normals['role_game_count']})"
    )

    return "\n".join(lines)


def _format_stats(label: str, stats: dict) -> str:
    if stats["n"] == 0:
        return f"  {label}: N/A (표본 없음)"

    return f"  {label}: median={stats['median']:.3g}, p25={stats['p25']:.3g}, p75={stats['p75']:.3g} (n={stats['n']})"


def main() -> None:
    parser = argparse.ArgumentParser(description="데이터 두께 / 노벨 픽 분석")
    parser.add_argument("identifiers", nargs="+", help="Riot ID(이름#태그) 또는 puuid, 여러 개 가능")
    parser.add_argument("--role", required=True, choices=sorted(VALID_POSITIONS), help="분석할 포지션")
    parser.add_argument(
        "--queues",
        default=str(RANKED_SOLO_QUEUE_ID),
        help=(
            f"쉼표로 구분한 큐 ID 목록 (기본 {RANKED_SOLO_QUEUE_ID}=솔로랭크만). "
            f"허용값: {', '.join(f'{qid}={name}' for qid, name in ALLOWED_SCOUTING_QUEUE_IDS.items())}"
        ),
    )
    parser.add_argument("--cutoff", default=None, help="ISO 날짜/시각. 이 시점 이후 매치는 제외함 (백테스트용)")
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")

    args = parser.parse_args()

    try:
        queue_ids = _parse_queues(args.queues)
    except ValueError as e:
        print(f"--queues 값이 잘못됨: {e}")
        return

    queue_label = "+".join(str(q) for q in queue_ids)
    # ranked-only 비교는 --queues와 무관하게 항상 솔로랭크(420) 단독 기준으로 고정함
    show_comparison = queue_ids != RANKED_ONLY_QUEUES

    cutoff_ms = _parse_cutoff(args.cutoff)
    repo = ScoutingRepo(cutoff_time=cutoff_ms)

    try:
        results = []

        for identifier in args.identifiers:
            player_id = _resolve_player_id(repo, identifier)

            if player_id is None:
                print(f"DB에서 찾을 수 없음: {identifier} (먼저 collect_matches.py로 수집해줘)")
                continue

            result = compute_player_thickness(repo, player_id, args.role, queue_ids=queue_ids)
            result["identifier"] = identifier

            if show_comparison:
                result["ranked_only_comparison"] = compute_player_thickness(
                    repo, player_id, args.role, queue_ids=RANKED_ONLY_QUEUES
                )

            results.append(result)

        if args.json:
            output = {"players": results}

            if len(results) > 1:
                output["group_summary"] = summarize_group(results)

            print(json.dumps(output, ensure_ascii=False, indent=2))
            return

        for result in results:
            print(f"=== {result['identifier']} ({result['role']}) [queues={queue_label}] ===")
            print(_format_result(result))

            if show_comparison:
                print()
                print(_format_novel_pick_comparison(result["ranked_only_comparison"], result, queue_label))

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
