"""Observed-pool ban recommendations, v1.1. No API calls or parameter tuning.

Pick probabilities use dated, queue-weighted observations. Threat uses raw
role-filtered wins/games and a weak, shrunk KDA signal. Meta rates use all other
same-role participants across patches. Off-support meta mass is retained in a
separate, unbannable Others bucket with neutral threat, not a champion ID.
Pool exhaustion means reliance on this prior, never zero player strength.
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from itertools import combinations
import math

from riot_api import RANKED_SOLO_QUEUE_TYPE
from scouting_repo import ScoutingRepo


TAU = 30
GAMMA = 0.3
LAMBDA = 0.15
K = 8
BETA = 4
ALPHA = 0.4
KDA_EPSILON = 1e-6
OTHERS_EPSILON = 1e-6
T_OTHERS = 1.0
TOP_CANDIDATES = 8
BAN_COUNT = 3
DAY_MS = 24 * 60 * 60 * 1000
QUEUE_WEIGHTS = {420: 1.0, 400: GAMMA}
ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
TIERS = (
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
    "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
)


@dataclass(frozen=True)
class ChampionModel:
    champion_id: int
    name: str
    games: int
    wins: int
    p_personal: float
    p_meta: float
    p_final: float
    wr_adj: float
    threat: float
    kda_champ: float = 0.0
    kda_adj: float = 0.0

    @property
    def candidate_score(self) -> float:
        # Raw pick share keeps a one-trick's main eligible even when T ~= 1.
        return self.p_final * self.threat


@dataclass(frozen=True)
class PlayerModel:
    player_id: int
    label: str
    role: str
    champions: dict[int, ChampionModel]
    baseline_winrate: float
    rank_score: float
    warnings: tuple[str, ...] = ()
    kda_baseline: float = 0.0
    p_others: float = OTHERS_EPSILON

    def strength(self, bans: Collection[int] = frozenset()) -> float:
        remaining = [c for cid, c in self.champions.items() if cid not in bans]
        mass = math.fsum([self.p_others, *(c.p_final for c in remaining)])
        # Sum remaining mass directly; 1 - banned_mass loses tiny rare picks.
        return math.fsum([
            self.p_others / mass * T_OTHERS,
            *((c.p_final / mass) * c.threat for c in remaining),
        ])

    def residual_ratio(self, bans: Collection[int] = frozenset()) -> float:
        return self.strength(bans) / self.strength()

    def observed_pool_exhausted(self, bans: Collection[int] = frozenset()) -> bool:
        """True means strength relies entirely on the unseen-champion prior."""
        return math.fsum(c.p_final for cid, c in self.champions.items() if cid not in bans) <= 0

    def candidate_champions(self) -> tuple[int, ...]:
        ordered = sorted(
            self.champions.values(),
            key=lambda c: (-c.candidate_score, c.champion_id),
        )
        return tuple(c.champion_id for c in ordered[:TOP_CANDIDATES])


@dataclass(frozen=True)
class SearchResult:
    bans: tuple[int, ...]
    value: float
    combinations_checked: int
    exhausted_combinations: int
    exhausted_player_ids: tuple[int, ...]


@dataclass(frozen=True)
class BanImpact:
    champion_id: int
    name: str
    affected: tuple[str, ...]
    marginal: float
    exhausted_player_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlayerDiagnostic:
    player_id: int
    label: str
    role: str
    strength: float
    residual_ratio: float
    observed_pool_exhausted: bool


@dataclass(frozen=True)
class Recommendation:
    players: tuple[PlayerModel, ...]
    weights: tuple[float, ...]
    candidates: tuple[int, ...]
    best_by_rho: dict[int, SearchResult]
    recommended: tuple[BanImpact, ...]
    also_consider: tuple[BanImpact, ...]
    player_diagnostics: tuple[PlayerDiagnostic, ...]
    warnings: tuple[str, ...]

    @property
    def threat_reduction(self) -> float:
        return 1 - self.best_by_rho[0].value


def solo_rank_score(tier: str | None, lp: int | None) -> float:
    # Placeholder strength scale, NOT calibrated: IRON=1 ... CHALLENGER=10
    # (including EMERALD). LP adds <0.01, so even elite LP cannot cross tiers.
    # Missing/unranked snapshots receive the same base weight as IRON, 1.
    tier = (tier or "").upper()
    if tier not in TIERS:
        return 1.0
    points = max(0, lp or 0)
    return TIERS.index(tier) + 1 + 0.01 * points / (points + 100)


def build_player_model(repo: ScoutingRepo, player_id: int, role: str) -> PlayerModel:
    """Build P and T using only queue 420/400 data at repo.cutoff_time (ms)."""
    if repo.cutoff_time is None:
        raise ValueError("Ban recommendations require ScoutingRepo(cutoff_time=now_ms).")
    role = role.upper()
    if role not in ROLES:
        raise ValueError(f"Unknown canonical role: {role}")
    player = repo.get_player(player_id)
    if player is None:
        raise ValueError(f"Unknown collected player: {player_id}")
    label = (
        f"{player['game_name']}#{player['tag_line']}"
        if player["game_name"] and player["tag_line"] else f"Player {player_id}"
    )
    rows = repo.get_role_matches(player_id, role, queue_ids=tuple(QUEUE_WEIGHTS))
    if not rows:
        raise ValueError(f"{label}/{role}: no collected queue 420/400 role games before cutoff.")
    if any(row["champion_id"] is None or row["win"] not in (0, 1) for row in rows):
        raise ValueError(f"{label}/{role}: incomplete champion or win data; reparse matches.")

    games = Counter(row["champion_id"] for row in rows)
    wins = Counter()
    takedowns = Counter()
    deaths = Counter()
    names = {}
    log_weights = []
    for row in rows:
        cid = row["champion_id"]
        wins[cid] += row["win"]
        # Raw role-filtered totals, including the champion itself in baseline.
        # Nullable legacy rows contribute zero for absent K/D/A counts.
        takedowns[cid] += (row["kills"] or 0) + (row["assists"] or 0)
        deaths[cid] += row["deaths"] or 0
        names[cid] = row["champion_name"] or str(cid)
        days_ago = max(0, repo.cutoff_time - row["game_start"]) / DAY_MS
        log_weights.append(math.log(QUEUE_WEIGHTS[row["queue_id"]]) - days_ago / TAU)

    # Scaling all weights by the same factor preserves exp(-days_ago/TAU)
    # after normalization and prevents all-old histories from underflowing.
    shift = max(log_weights)
    pick_weights = defaultdict(list)
    for row, log_weight in zip(rows, log_weights):
        pick_weights[row["champion_id"]].append(math.exp(log_weight - shift))
    counts = {cid: math.fsum(values) for cid, values in pick_weights.items()}
    total = math.fsum(counts.values())
    personal = {cid: counts[cid] / total for cid in games}

    meta_rows = repo.get_other_participant_role_matches(
        player_id, role, queue_ids=tuple(QUEUE_WEIGHTS)
    )
    # Empirical (unweighted) rates use ALL other same-role picks as denominator.
    # Preserve off-support mass separately; Others is never a champion/candidate.
    meta_counts = Counter(row["champion_id"] for row in meta_rows if row["champion_id"] is not None)
    meta_total = sum(meta_counts.values())
    notes = []
    meta = {cid: meta_counts[cid] / meta_total if meta_total else 0.0 for cid in games}
    if not meta_total:
        notes.append(f"{label}/{role}: no meta observations; using personal probabilities with safety Others mass.")
    mixed = {cid: (1 - LAMBDA) * personal[cid] + LAMBDA * meta[cid] for cid in games}
    off_support = LAMBDA * max(0.0, 1.0 - math.fsum(meta.values())) if meta_total else 0.0
    p_others = max(OTHERS_EPSILON, off_support)
    # Epsilon is numerical/model safety, not an estimated unseen pick rate.
    # The ordinary meta mixture already sums to one: leave seen P unchanged.
    # Only the safety fallback (including absent meta) needs normalization.
    if off_support < OTHERS_EPSILON:
        mixed_total = math.fsum([p_others, *mixed.values()])
        mixed = {cid: probability / mixed_total for cid, probability in mixed.items()}
        p_others /= mixed_total

    # Date/queue weights apply only to picks; WR and KDA performance is raw.
    baseline = sum(wins.values()) / len(rows)
    kda_baseline = sum(takedowns.values()) / max(1, sum(deaths.values()))
    champions = {}
    for cid in sorted(games):
        wr_adj = (wins[cid] + K * baseline) / (games[cid] + K)
        kda_champ = takedowns[cid] / max(1, deaths[cid])
        kda_adj = (games[cid] * kda_champ + K * kda_baseline) / (games[cid] + K)
        kda_log_ratio = math.log(
            max(kda_adj, KDA_EPSILON) / max(kda_baseline, KDA_EPSILON)
        )
        threat = math.exp(BETA * (wr_adj - baseline) + ALPHA * kda_log_ratio)
        champions[cid] = ChampionModel(
            cid, names[cid], games[cid], wins[cid], personal[cid], meta[cid],
            mixed[cid], wr_adj, threat, kda_champ, kda_adj,
        )

    rank = repo.get_latest_rank_snapshot(player_id, RANKED_SOLO_QUEUE_TYPE)
    if rank is None or (rank["tier"] or "").upper() not in TIERS:
        notes.append(f"{label}: no recognized solo-queue tier at cutoff; placeholder rank weight = 1.")
    score = solo_rank_score(rank["tier"], rank["lp"]) if rank else 1.0
    return PlayerModel(player_id, label, role, champions, baseline, score, tuple(notes), kda_baseline, p_others)


def arithmetic_mean(residuals: Sequence[float], weights: Sequence[float]) -> float:
    """V_1(B) = sum_i w_i * r_i(B), with normalized positive weights."""
    return math.fsum(w * r for w, r in zip(weights, residuals, strict=True))


def geometric_mean(residuals: Sequence[float], weights: Sequence[float]) -> float:
    """V_0(B) = product_i r_i(B) ** w_i."""
    pairs = tuple(zip(weights, residuals, strict=True))
    if any(r == 0 and w > 0 for w, r in pairs):
        return 0.0
    return math.prod(r ** w for w, r in pairs)


def harmonic_mean(residuals: Sequence[float], weights: Sequence[float]) -> float:
    """V_-1(B) = 1 / sum_i (w_i / r_i(B)); zero is the continuous limit."""
    pairs = tuple(zip(weights, residuals, strict=True))
    if any(r == 0 and w > 0 for w, r in pairs):
        return 0.0
    return 1 / math.fsum(w / r for w, r in pairs)


def _exhaustive_search(
    candidates: tuple[int, ...],
    residuals_for: Callable[[Collection[int]], tuple[float, ...]],
    weights: tuple[float, ...],
    aggregate: Callable[[Sequence[float], Sequence[float]], float],
    exhausted_for: Callable[[Collection[int]], tuple[int, ...]],
) -> SearchResult:
    best_bans = None
    best_value = math.inf
    checked = exhausted = 0
    best_exhausted = ()
    # A new iterator is constructed for EACH aggregation function. Never reuse
    # the geometric optimum as a substitute for either other full search.
    for bans in combinations(candidates, BAN_COUNT):
        checked += 1
        residuals = residuals_for(bans)
        exhausted_ids = exhausted_for(bans)
        exhausted += bool(exhausted_ids)
        value = aggregate(residuals, weights)
        # Sorted IDs and strict comparison give deterministic lexicographic ties.
        if value < best_value:
            best_bans, best_value = bans, value
            best_exhausted = exhausted_ids
    if best_bans is None:
        raise ValueError("At least 3 distinct candidate champions are required for a 3-ban search.")
    return SearchResult(best_bans, best_value, checked, exhausted, best_exhausted)


def recommend_bans(repo: ScoutingRepo, opponents: Sequence[tuple[int, str]]) -> Recommendation:
    """Recommend 3 bans for exactly five distinct collected players and roles."""
    if len(opponents) != 5 or len({player_id for player_id, _ in opponents}) != 5:
        raise ValueError("Provide exactly five distinct opposing players with their roles.")
    players = tuple(build_player_model(repo, player_id, role) for player_id, role in opponents)
    total_rank = math.fsum(p.rank_score for p in players)
    weights = tuple(p.rank_score / total_rank for p in players)
    candidates = tuple(sorted({cid for p in players for cid in p.candidate_champions()}))
    baselines = tuple(p.strength() for p in players)

    def residuals_for(bans: Collection[int]) -> tuple[float, ...]:
        return tuple(p.strength(bans) / baseline for p, baseline in zip(players, baselines))

    def exhausted_for(bans: Collection[int]) -> tuple[int, ...]:
        return tuple(p.player_id for p in players if p.observed_pool_exhausted(bans))

    best_by_rho = {
        rho: _exhaustive_search(
            candidates, residuals_for, weights, aggregate, exhausted_for
        )
        for rho, aggregate in ((1, arithmetic_mean), (0, geometric_mean), (-1, harmonic_mean))
    }
    best = best_by_rho[0]
    bans = frozenset(best.bans)
    notes = [note for p in players for note in p.warnings]
    if any(result.exhausted_player_ids for result in best_by_rho.values()):
        notes.append(
            "An optimum exhausts an observed role champion pool: strength now relies "
            "on the unseen-champion Others estimate (neutral threat = 1), not zero strength."
        )

    def impact(cid: int, marginal: float, resulting_bans: Collection[int]) -> BanImpact:
        affected = tuple(f"{p.label}/{p.role}" for p in players if cid in p.champions)
        champion = next(p.champions[cid] for p in players if cid in p.champions)
        exhausted_ids = tuple(p.player_id for p in players if p.observed_pool_exhausted(resulting_bans))
        return BanImpact(cid, champion.name, affected, marginal, exhausted_ids)

    recommended = []
    for cid in best.bans:
        marginal = geometric_mean(residuals_for(bans - {cid}), weights) - best.value
        recommended.append(impact(cid, marginal, bans))
        if marginal < 0:
            notes.append(
                f"Negative marginal contribution for {recommended[-1].name} ({marginal:+.6f}): "
                "Threat/redistribution may redirect picks onto more threatening champions."
            )
    recommended.sort(key=lambda b: (-b.marginal, b.champion_id))

    # Also-consider values each start from the SAME B*, not a greedy 4..8 chain.
    # Include all observed champions here, even those outside the top-8 shortlist.
    all_champions = {cid for p in players for cid in p.champions}
    alternatives = []
    for cid in sorted(all_champions - bans):
        value = geometric_mean(residuals_for(bans | {cid}), weights)
        alternatives.append(impact(cid, best.value - value, bans | {cid}))
    alternatives.sort(key=lambda b: (-b.marginal, b.champion_id))
    if any(b.exhausted_player_ids for b in alternatives[:5]):
        notes.append(
            "An also-consider set exhausts an observed pool; strength for that player "
            "relies on the unseen-champion Others estimate (neutral threat = 1)."
        )
    shared = set.intersection(*(set(result.bans) for result in best_by_rho.values()))
    if len(shared) < 2:
        notes.append("Recommendation is sensitive to concentration assumption — treat with caution.")
    diagnostics = tuple(
        PlayerDiagnostic(
            p.player_id, p.label, p.role, p.strength(bans),
            p.residual_ratio(bans), p.observed_pool_exhausted(bans),
        )
        for p in players
    )
    return Recommendation(
        players, weights, candidates, best_by_rho, tuple(recommended),
        tuple(alternatives[:5]), diagnostics, tuple(notes),
    )


def format_recommendation(result: Recommendation) -> str:
    """Human-readable manual report; marginal values are V_0 score differences."""
    lines = ["Recommended bans (B*, ordered by marginal contribution):"]

    def ban_line(index: int, ban: BanImpact) -> str:
        line = (
            f"  {index}. {ban.name} [{ban.champion_id}] — {', '.join(ban.affected)}"
            f" — marginal {ban.marginal:+.6f}"
        )
        if ban.exhausted_player_ids:
            line += f"; observed_pool_exhausted=True for player IDs {ban.exhausted_player_ids}"
        return line

    lines.extend(ban_line(i, ban) for i, ban in enumerate(result.recommended, 1))
    lines.extend([
        f"Estimated threat reduction: {result.threat_reduction:.2%}",
        "", "Also consider (each given B* already banned):",
    ])
    lines.extend(ban_line(i, ban) for i, ban in enumerate(result.also_consider, 4))
    if len(result.also_consider) < 5:
        lines.append(f"  Only {len(result.also_consider)} scoreable additional champions.")
    lines.extend(["", "Player residuals at B*:"])
    for diagnostic in result.player_diagnostics:
        lines.append(
            f"  {diagnostic.label}/{diagnostic.role}: S={diagnostic.strength:.6f}, "
            f"r={diagnostic.residual_ratio:.6f}, "
            f"observed_pool_exhausted={diagnostic.observed_pool_exhausted}"
        )
    lines.extend(["", "Sensitivity check (independent full searches):"])
    names = {cid: c.name for p in result.players for cid, c in p.champions.items()}
    for rho in (1, 0, -1):
        best = result.best_by_rho[rho]
        label = ", ".join(f"{names[cid]} [{cid}]" for cid in best.bans)
        lines.append(
            f"  rho={rho}: {label} — V={best.value:.6f}; "
            f"{best.combinations_checked} combinations checked, "
            f"{best.exhausted_combinations} exhaust an observed pool; "
            f"optimum exhausted player IDs={best.exhausted_player_ids}"
        )
    lines.extend(f"WARNING: {note}" for note in result.warnings)
    return "\n".join(lines)
