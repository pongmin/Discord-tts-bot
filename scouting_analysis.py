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

from scouting_repo import ScoutingRepo

# 노벨 픽 판단에 쓰는 "이전 N경기" 윈도우들
NOVEL_PICK_WINDOWS = (20, 50, 100, 200)

THIRTY_DAYS_MS = 30 * 24 * 60 * 60 * 1000


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
    queue_id: int = 420,
    as_of_ms: int | None = None,
) -> dict:
    """
    한 선수/한 포지션에 대한 데이터 두께 지표를 계산함.
    rows는 repo가 이미 cutoff_time 이하로만, game_start 오름차순으로 준 것.
    """
    rows = repo.get_role_matches(player_id, role, queue_id=queue_id)

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
