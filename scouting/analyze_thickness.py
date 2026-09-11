"""
데이터 두께 / 노벨 픽 분석 CLI.

Riot API를 호출하지 않고 data/scouting.db에 이미 수집된 데이터만 사용함.
(먼저 `python -m scouting.collect_matches`로 매치를 수집해 둬야 함)

사용법:
    python -m scouting.analyze_thickness "Hide on bush#KR1" --role MIDDLE
    python -m scouting.analyze_thickness "P1#KR1" "P2#KR1" "P3#KR1" --role JUNGLE
    python -m scouting.analyze_thickness "Hide on bush#KR1" --role MIDDLE --cutoff 2025-01-01T00:00:00
    python -m scouting.analyze_thickness "Hide on bush#KR1" --role MIDDLE --queues 420,400,490

--queues에 솔로랭크(420) 외의 큐를 섞으면, 같은 선수에 대해 "ranked only"와
"ranked+normals" 두 조건의 novel pick rate를 나란히 보여줌 - 일반 게임을
데이터에 섞는 게 실제로 의미가 있는지 확인하기 위한 비교임.
"""

import argparse
import json
from datetime import datetime, timezone

from scouting.match_position import VALID_POSITIONS
from riot.riot_api import (
    RANKED_SOLO_QUEUE_ID,
    RANKED_FLEX_QUEUE_ID,
    CLASH_QUEUE_ID,
    ALLOWED_SCOUTING_QUEUE_IDS,
)
from scouting.scouting_repo import ScoutingRepo
from scouting.scouting_analysis import (
    compute_player_thickness,
    summarize_group,
    compute_clash_coverage,
    CLASH_SMALL_SAMPLE_THRESHOLD,
)

RANKED_ONLY_QUEUES = (RANKED_SOLO_QUEUE_ID,)

# --clash-coverage의 "ranked+normals" 풀은 항상 랭크 자유/클래시를 제외한
# ALLOWED_SCOUTING_QUEUE_IDS 전체로 계산함. 새 일반 큐가 나중에 추가돼도
# 여기서 자동으로 따라가고, 클래시는 항상 제외됨.
RANKED_PLUS_NORMALS_QUEUES = tuple(q for q in ALLOWED_SCOUTING_QUEUE_IDS if q != CLASH_QUEUE_ID)


def _parse_queues(value: str) -> tuple[int, ...]:
    queue_ids = []

    for token in value.split(","):
        token = token.strip()

        if not token:
            continue

        queue_id = int(token)

        if queue_id == RANKED_FLEX_QUEUE_ID:
            raise ValueError("랭크 자유(440)는 분석 대상에서 제외됨")

        if queue_id == CLASH_QUEUE_ID:
            raise ValueError(
                "클래시(700)는 --queues로 풀 계산에 섞을 수 없음 - "
                "검증용 그라운드 트루스라서 feature/training 풀과 분리해야 함. "
                "--clash-coverage를 대신 쓸 것"
            )

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


def _fmt_date_ms(ms: int | None) -> str:
    if ms is None:
        return "N/A"

    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _format_clash_coverage(result: dict) -> str:
    n = result["clash_game_count"]
    lines = [
        f"  Clash games found: {result['total_clash_games_found']} "
        f"(사용 n={n}, game_end 없어서 제외={result['excluded_missing_game_end']})"
    ]

    if n == 0:
        lines.append("  Clash 경기가 없어서 coverage를 계산할 수 없음")
        return "\n".join(lines)

    if result["is_small_sample"]:
        lines.append(
            f"  ⚠ 표본이 작음(n={n} < {CLASH_SMALL_SAMPLE_THRESHOLD}) - 아래 비율은 참고용일 뿐, "
            f"신뢰 가능한 지표로 취급하면 안 됨"
        )

    lines.append(f"  ranked-only coverage    : {_fmt_pct(result['ranked_coverage_rate'])} (n={n})")
    lines.append(f"  ranked+normals coverage : {_fmt_pct(result['normals_coverage_rate'])} (n={n})")
    lines.append("  게임별 상세:")

    for game in result["games"]:
        date = _fmt_date_ms(game["game_end_ms"])
        ranked_mark = "YES" if game["in_ranked_pool"] else "no"
        normals_mark = "YES" if game["in_normals_pool"] else "no"

        lines.append(
            f"    {date}  {game['role']:<8} {game['champion_name']:<16} "
            f"ranked-pool(n={game['ranked_pool_size']:>3})={ranked_mark:<3}  "
            f"normals-pool(n={game['normals_pool_size']:>3})={normals_mark}"
        )

    return "\n".join(lines)


def _format_stats(label: str, stats: dict) -> str:
    if stats["n"] == 0:
        return f"  {label}: N/A (표본 없음)"

    return f"  {label}: median={stats['median']:.3g}, p25={stats['p25']:.3g}, p75={stats['p75']:.3g} (n={stats['n']})"


def _run_clash_coverage(args: argparse.Namespace) -> None:
    """
    선수별로 Clash 경기 전체(모든 role 섞임 - role은 경기마다 개별로 씀)를 훑어서
    각 경기 시점 기준 ranked-only / ranked+normals 풀 커버리지를 계산함.
    """
    cutoff_ms = _parse_cutoff(args.cutoff)
    repo = ScoutingRepo(cutoff_time=cutoff_ms)

    try:
        results = []

        for identifier in args.identifiers:
            player_id = _resolve_player_id(repo, identifier)

            if player_id is None:
                print(f"DB에서 찾을 수 없음: {identifier} (먼저 `python -m scouting.collect_matches --queues 700`으로 클래시를 수집해줘)")
                continue

            result = compute_clash_coverage(
                repo, player_id, RANKED_ONLY_QUEUES, RANKED_PLUS_NORMALS_QUEUES
            )
            result["identifier"] = identifier
            results.append(result)

        if args.json:
            print(json.dumps({"players": results}, ensure_ascii=False, indent=2))
            return

        for result in results:
            print(f"=== {result['identifier']} Clash coverage ===")
            print(_format_clash_coverage(result))
            print()

    finally:
        repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="데이터 두께 / 노벨 픽 분석")
    parser.add_argument("identifiers", nargs="+", help="Riot ID(이름#태그) 또는 puuid, 여러 개 가능")
    parser.add_argument(
        "--role", default=None, choices=sorted(VALID_POSITIONS),
        help="분석할 포지션 (--clash-coverage에서는 무시됨 - 경기별로 실제 뛴 role을 씀)"
    )
    parser.add_argument(
        "--queues",
        default=str(RANKED_SOLO_QUEUE_ID),
        help=(
            f"쉼표로 구분한 큐 ID 목록 (기본 {RANKED_SOLO_QUEUE_ID}=솔로랭크만). "
            f"허용값: {', '.join(f'{qid}={name}' for qid, name in ALLOWED_SCOUTING_QUEUE_IDS.items())} "
            f"(클래시는 여기 못 씀 - --clash-coverage 참고)"
        ),
    )
    parser.add_argument("--cutoff", default=None, help="ISO 날짜/시각. 이 시점 이후 매치는 제외함 (백테스트용)")
    parser.add_argument(
        "--clash-coverage", action="store_true",
        help=(
            "Clash에서 고른 챔피언이 그 경기 시점 이전의 ranked-only / ranked+normals "
            "챔피언 풀 안에 있었는지 검증 리포트를 출력함 (모델링 아님, 측정만)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력")

    args = parser.parse_args()

    if args.clash_coverage:
        _run_clash_coverage(args)
        return

    if args.role is None:
        parser.error("--role은 --clash-coverage를 쓰지 않는 한 필수임")

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
                print(f"DB에서 찾을 수 없음: {identifier} (먼저 `python -m scouting.collect_matches`로 수집해줘)")
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
