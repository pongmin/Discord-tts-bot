"""
데이터 두께(data-thickness) / 노벨 픽 분석.

전부 캐시된 DB(scouting_db를 통해 쌓인 player_matches/matches)에서만 계산하며,
Riot API를 직접 호출하지 않음. ScoutingRepo를 통해 조회하므로 cutoff_time을
넘기면 그 이후 경기는 절대 섞이지 않음(백테스트 안전).

여기서는 데이터가 얼마나 쌓여있는지/픽이 얼마나 새로운지만 계산함.
밴 추천, 숙련도, Threat, 교체 로직 등은 여기서 다루지 않음.
"""

import math
from statistics import median

from scouting.scouting_repo import ScoutingRepo

# 노벨 픽 판단에 쓰는 "이전 N경기" 윈도우들
NOVEL_PICK_WINDOWS = (20, 50, 100, 200)

THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000

# 이보다 Clash 표본이 적으면 coverage 비율을 신뢰 가능한 지표로 취급하지 않고
# 경고와 함께 보여줌 (표본이 1~2개인데 100%/0%가 나오는 걸 그대로 믿으면 안 됨).
CLASH_SMALL_SAMPLE_THRESHOLD = 5


def _percentile(sorted_values: list[float], pct: float) -> float:
    """
    선형 보간 방식의 백분위수(0.0~1.0). sorted_values는 이미 오름차순이어야 함.
    """
    if not sorted_values:
        return None

    if len(sorted_values) == 1:
        return sorted_values[0]

    k = (len(sorted_values) - 1) * pct
    f = math.floor(k)
    c = math.ceil(k)

    if f == c:
        return sorted_values[int(k)]

    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def _effective_champion_pool(champion_counts: dict) -> float:
    """
    섀넌 엔트로피 H(p)의 exp(H(p)). 챔피언 하나만 팠으면 1에 가깝고,
    여러 챔피언을 균등하게 썼으면 실제 챔피언 수에 가까워짐.
    """
    total = sum(champion_counts.values())

    if total == 0:
        return 0.0

    entropy = 0.0

    for count in champion_counts.values():
        p = count / total

        if p > 0:
            entropy -= p * math.log(p)

    return math.exp(entropy)


def _top_n_concentration(sorted_counts: list[int], n: int, total: int) -> float | None:
    if total == 0:
        return None

    return sum(sorted_counts[:n]) / total


def _novel_pick_rate(chronological_champions: list[str], window: int) -> float | None:
    """
    picks[i]가 노벨인지는 picks[i-window:i](직전 window경기) 안에 같은 챔피언이
    있었는지로만 판단함. 그 window를 채울 만큼 이전 경기가 없으면(i < window)
    판단 대상에서 제외함. 표본이 window개보다 적으면 그 윈도우는 None(판단 불가).
    """
    n = len(chronological_champions)

    if n <= window:
        return None

    novel_count = 0
    eligible = 0

    for i in range(window, n):
        preceding = chronological_champions[i - window:i]

        eligible += 1

        if chronological_champions[i] not in set(preceding):
            novel_count += 1

    if eligible == 0:
        return None

    return novel_count / eligible


def compute_player_thickness(
    repo: ScoutingRepo,
    player_id: int,
    role: str,
    queue_ids: tuple[int, ...] | list[int] = (420,),
    as_of_ms: int | None = None,
) -> dict:
    """
    한 선수/한 포지션에 대한 데이터 두께 지표를 계산함.
    rows는 repo가 이미 cutoff_time 이하로만, game_start 오름차순으로 준 것이고
    player_id로 걸러져 있어서 같은 매치의 다른 9명 참가자는 절대 섞이지 않음.
    """
    rows = repo.get_role_matches(player_id, role, queue_ids=queue_ids)

    role_game_count = len(rows)

    champion_counts: dict[str, int] = {}
    for row in rows:
        champion_counts[row["champion_name"]] = champion_counts.get(row["champion_name"], 0) + 1

    unique_champion_count = len(champion_counts)
    sorted_counts = sorted(champion_counts.values(), reverse=True)

    top1 = _top_n_concentration(sorted_counts, 1, role_game_count)
    top3 = _top_n_concentration(sorted_counts, 3, role_game_count)
    top5 = _top_n_concentration(sorted_counts, 5, role_game_count)

    effective_pool = _effective_champion_pool(champion_counts)

    now_ms = as_of_ms if as_of_ms is not None else (rows[-1]["game_start"] if rows else 0)
    games_last_30_days = sum(
        1 for row in rows if now_ms - row["game_start"] <= THIRTY_DAYS_MS
    )

    latest_patch = rows[-1]["patch"] if rows else None
    pct_on_latest_patch = None

    if rows and latest_patch is not None:
        on_latest = sum(1 for row in rows if row["patch"] == latest_patch)
        pct_on_latest_patch = on_latest / role_game_count

    chronological_champions = [row["champion_name"] for row in rows]

    novel_pick_rate = {
        window: _novel_pick_rate(chronological_champions, window)
        for window in NOVEL_PICK_WINDOWS
    }

    earliest_match_ms = rows[0]["game_start"] if rows else None
    latest_match_ms = rows[-1]["game_start"] if rows else None
    span_days = (
        (latest_match_ms - earliest_match_ms) / (24 * 60 * 60 * 1000)
        if rows
        else None
    )

    return {
        "player_id": player_id,
        "role": role,
        "role_game_count": role_game_count,
        "unique_champion_count": unique_champion_count,
        "top1_concentration": top1,
        "top3_concentration": top3,
        "top5_concentration": top5,
        "effective_champion_pool": effective_pool,
        "games_last_30_days": games_last_30_days,
        "latest_patch": latest_patch,
        "pct_on_latest_patch": pct_on_latest_patch,
        "novel_pick_rate": novel_pick_rate,
        "earliest_match_ms": earliest_match_ms,
        "latest_match_ms": latest_match_ms,
        "span_days": span_days,
    }


def compute_clash_coverage(
    repo: ScoutingRepo,
    player_id: int,
    ranked_queue_ids: tuple[int, ...],
    ranked_plus_normals_queue_ids: tuple[int, ...],
) -> dict:
    """
    이 선수가 뛴 Clash 경기마다, 그 경기에서 고른 챔피언이 "그 경기 시점 이전"의
    챔피언 풀(같은 role) 안에 있었는지를 봄. 검증(ground-truth) 목적이며 모델/가중치는
    여기서 다루지 않음.

    각 Clash 경기의 풀은 반드시 그 경기의 game_end를 cutoff_time으로 한
    ScoutingRepo(cutoff_time=...)로 새로 계산함 - 선수의 챔피언 선호는 몇 달 사이에도
    크게 바뀌므로, 지금 시점의 전체 이력으로 계산한 풀과 비교하면 측정 자체가
    무의미해짐(3월 클래시 픽을 9월 풀과 비교하는 격). repo와 connection을 공유해서
    쓰므로 경기마다 새 sqlite connection을 열지는 않음.

    game_end가 없는(아주 오래된/불완전한) 경기는 cutoff를 걸 수 없어서
    안전하게 건너뛰고 excluded_missing_game_end에 집계함.
    """
    clash_rows = repo.get_clash_matches(player_id)

    per_game = []
    excluded_missing_game_end = 0
    ranked_hits = 0
    normals_hits = 0

    for row in clash_rows:
        if row["game_end"] is None:
            excluded_missing_game_end += 1
            continue

        role = row["canonical_role"]
        cutoff_repo = ScoutingRepo(cutoff_time=row["game_end"], conn=repo._conn)

        ranked_rows = cutoff_repo.get_role_matches(player_id, role, queue_ids=ranked_queue_ids)
        normals_rows = cutoff_repo.get_role_matches(player_id, role, queue_ids=ranked_plus_normals_queue_ids)

        ranked_pool = {r["champion_name"] for r in ranked_rows}
        normals_pool = {r["champion_name"] for r in normals_rows}

        in_ranked = row["champion_name"] in ranked_pool
        in_normals = row["champion_name"] in normals_pool

        ranked_hits += int(in_ranked)
        normals_hits += int(in_normals)

        per_game.append({
            "match_id": row["match_id"],
            "game_end_ms": row["game_end"],
            "role": role,
            "champion_name": row["champion_name"],
            "in_ranked_pool": in_ranked,
            "in_normals_pool": in_normals,
            "ranked_pool_size": len(ranked_pool),
            "normals_pool_size": len(normals_pool),
        })

    n = len(per_game)

    return {
        "total_clash_games_found": len(clash_rows),
        "excluded_missing_game_end": excluded_missing_game_end,
        "clash_game_count": n,
        "is_small_sample": n < CLASH_SMALL_SAMPLE_THRESHOLD,
        "ranked_coverage_rate": (ranked_hits / n) if n else None,
        "normals_coverage_rate": (normals_hits / n) if n else None,
        "games": per_game,
    }


def _distribution_stats(values: list[float | None]) -> dict:
    clean = sorted(v for v in values if v is not None)

    if not clean:
        return {"median": None, "p25": None, "p75": None, "n": 0}

    return {
        "median": median(clean),
        "p25": _percentile(clean, 0.25),
        "p75": _percentile(clean, 0.75),
        "n": len(clean),
    }


def summarize_group(results: list[dict]) -> dict:
    """
    여러 선수의 compute_player_thickness() 결과를 받아서
    role_game_count / effective_champion_pool / top3_concentration / novel_pick_rate(윈도우별)의
    median, p25, p75를 계산함.
    """
    summary = {
        "role_game_count": _distribution_stats([r["role_game_count"] for r in results]),
        "effective_champion_pool": _distribution_stats([r["effective_champion_pool"] for r in results]),
        "top3_concentration": _distribution_stats([r["top3_concentration"] for r in results]),
        "novel_pick_rate": {},
    }

    for window in NOVEL_PICK_WINDOWS:
        values = [r["novel_pick_rate"].get(window) for r in results]
        summary["novel_pick_rate"][window] = _distribution_stats(values)

    return summary
