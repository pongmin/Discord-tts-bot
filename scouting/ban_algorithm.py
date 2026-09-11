"""Observed-pool ban recommendations, v1.2. No API calls or parameter tuning.

Pick probabilities use dated, queue-weighted observations. Threat uses raw
role-filtered wins/games and a weak, shrunk KDA signal. Meta rates use all other
same-role participants across patches. Off-support meta mass is retained in a
separate, unbannable Others bucket with neutral threat, not a champion ID.
Pool exhaustion means reliance on this prior, never zero player strength.

Threat and Dependency are kept deliberately separate. Threat asks "how well do
they do on this champion" (performance); Dependency asks "how much of their
familiar pool do they lose if this champion is banned" (pick-share loss),
using the pre-meta-blend P_personal, not P_final - a low personal reliance
that gets inflated by meta backoff would otherwise be double-counted as
dependency it doesn't represent. A player's final residual is the performance
residual times the dependency penalty: r(p,B) = r_perf(p,B) * D(p,B).
"""

from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from itertools import combinations
import math

from riot.riot_api import RANKED_SOLO_QUEUE_TYPE
from scouting.scouting_repo import ScoutingRepo


TAU = 30
GAMMA = 0.3
LAMBDA = 0.15
K = 8
BETA = 4
ALPHA = 0.4
KDA_EPSILON = 1e-6
OTHERS_EPSILON = 1e-6
T_OTHERS = 1.0
ETA = 0.35
TOP_CANDIDATES = 8
# Per player, always keep this many of their own top-P_personal champions as
# ban candidates, in addition to the TOP_CANDIDATES chosen by performance
# (P_final * threat). Without this, a heavily-relied-on but low-threat main
# (exactly what Dependency exists to penalize) could be pruned out before the
# search ever runs, making the new term moot for the champion it targets.
DEPENDENCY_CANDIDATES = 3
BAN_COUNT = 3
DAY_MS = 24 * 60 * 60 * 1000
QUEUE_WEIGHTS = {420: 1.0, 400: GAMMA}
ROLES = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
TIERS = (
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD",
    "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER",
)
# Missing rank info is treated as "unknown", not "weak": a player with no
# recognized current solo-queue tier borrows the average raw rank_score of
# their ranked opponents (see recommend_bans). Only when all five opponents
# are unranked does this fixed midpoint of the [1, 10] tier scale apply.
DEFAULT_NEUTRAL_RANK_SCORE = 5.5


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
    # False means rank_score is not this player's own: build_player_model set
    # it to a temporary placeholder, and recommend_bans overwrites it with the
    # team-neutral fallback (opponent average, or DEFAULT_NEUTRAL_RANK_SCORE)
    # once all five opponents' models are known.
    is_ranked: bool = True

    def strength(self, bans: Collection[int] = frozenset()) -> float:
        remaining = [c for cid, c in self.champions.items() if cid not in bans]
        mass = math.fsum([self.p_others, *(c.p_final for c in remaining)])
        # Sum remaining mass directly; 1 - banned_mass loses tiny rare picks.
        return math.fsum([
            self.p_others / mass * T_OTHERS,
            *((c.p_final / mass) * c.threat for c in remaining),
        ])

    def dependency_mass(self, bans: Collection[int] = frozenset()) -> float:
        """q(p,B): personal (pre-meta-blend) pick share banned away, restricted
        to champions this player has actually played - Others is a
        performance prior, never part of anyone's pool, so it never
        contributes here regardless of how large bans gets.
        """
        q = math.fsum(c.p_personal for cid, c in self.champions.items() if cid in bans)
        # P_personal already sums to <= 1 over the observed pool, so this is
        # just float-safety, not a real clamp on realistic inputs.
        return min(1.0, max(0.0, q))

    def dependency(self, bans: Collection[int] = frozenset()) -> float:
        """D(p,B) = exp(-ETA * q(p,B)); 1.0 (no penalty) when B bans nothing
        the player has observed themselves picking.
        """
        return math.exp(-ETA * self.dependency_mass(bans))

    def residual_ratio(self, bans: Collection[int] = frozenset()) -> float:
        """r(p,B) = r_perf(p,B) * D(p,B); both factors are 1 for B=empty."""
        return (self.strength(bans) / self.strength()) * self.dependency(bans)

    def observed_pool_exhausted(self, bans: Collection[int] = frozenset()) -> bool:
        """True means strength relies entirely on the unseen-champion prior."""
        return math.fsum(c.p_final for cid, c in self.champions.items() if cid not in bans) <= 0

    def candidate_champions(self) -> tuple[int, ...]:
        by_score = sorted(
            self.champions.values(),
            key=lambda c: (-c.candidate_score, c.champion_id),
        )
        by_personal = sorted(
            self.champions.values(),
            key=lambda c: (-c.p_personal, c.champion_id),
        )
        selected = {c.champion_id for c in by_score[:TOP_CANDIDATES]}
        selected.update(c.champion_id for c in by_personal[:DEPENDENCY_CANDIDATES])
        return tuple(sorted(selected))


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


def solo_rank_score(tier: str, lp: int | None) -> float:
    # Placeholder strength scale, NOT calibrated: IRON=1 ... CHALLENGER=10
    # (including EMERALD). LP adds <0.01, so even elite LP cannot cross tiers.
    # Callers must pass a recognized tier (see TIERS); unranked/missing
    # snapshots are resolved by recommend_bans as a team-neutral fallback,
    # not routed through here as if they were IRON.
    points = max(0, lp or 0)
    return TIERS.index(tier.upper()) + 1 + 0.01 * points / (points + 100)


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
    tier = (rank["tier"] or "").upper() if rank else ""
    is_ranked = tier in TIERS
    if is_ranked:
        score = solo_rank_score(tier, rank["lp"])
    else:
        # Placeholder only: recommend_bans overwrites this once it knows the
        # other four opponents' rank_score, per is_ranked=False above.
        score = 0.0
        notes.append(
            f"{label}: no recognized solo-queue tier at cutoff; rank weight uses the "
            "team-neutral fallback (opponents' average rank_score, or "
            f"{DEFAULT_NEUTRAL_RANK_SCORE} if all five are unranked)."
        )
    return PlayerModel(
        player_id, label, role, champions, baseline, score, tuple(notes),
        kda_baseline, p_others, is_ranked,
    )


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
    # Rank absence means "unknown", not "weak": fill each unranked opponent's
    # rank_score with the average of this same team's ranked opponents (or
    # the fixed midpoint DEFAULT_NEUTRAL_RANK_SCORE if none of the five are
    # ranked) before any player-weight normalization runs.
    ranked_scores = [p.rank_score for p in players if p.is_ranked]
    neutral_score = (
        math.fsum(ranked_scores) / len(ranked_scores) if ranked_scores
        else DEFAULT_NEUTRAL_RANK_SCORE
    )
    players = tuple(
        p if p.is_ranked else replace(p, rank_score=neutral_score)
        for p in players
    )
    total_rank = math.fsum(p.rank_score for p in players)
    weights = tuple(p.rank_score / total_rank for p in players)
    candidates = tuple(sorted({cid for p in players for cid in p.candidate_champions()}))
    baselines = tuple(p.strength() for p in players)

    def residuals_for(bans: Collection[int]) -> tuple[float, ...]:
        # r(p,B) = r_perf(p,B) * D(p,B). baselines caches S(p,empty) so the
        # performance half isn't recomputed per combination; dependency is
        # cheap (a sum over at most BAN_COUNT champion lookups) either way.
        return tuple(
            (p.strength(bans) / baseline) * p.dependency(bans)
            for p, baseline in zip(players, baselines)
        )

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


# Shrinkage strength for role_wr_adj below. Reuses K's magnitude by design
# (same shrink-strength convention as champion-level wr_adj), but the prior
# it shrinks toward is fixed at PLAYER_THREAT_WR_PRIOR (0.5), not a player's
# own baseline_winrate, so this is an independent constant, not a shared one.
PLAYER_THREAT_WR_PRIOR = 0.5
# Weighted-geometric exponents for player_threat_scores below (sum to 1).
# Rank is deliberately the dominant factor; form and pool are secondary
# tiebreakers, not co-equal with rank.
PLAYER_THREAT_RANK_EXPONENT = 0.6
PLAYER_THREAT_FORM_EXPONENT = 0.2
PLAYER_THREAT_POOL_EXPONENT = 0.2


def player_threat_scores(result: Recommendation) -> dict[int, float]:
    """UI-only per-player threat, keyed by player_id: how much this specific
    opponent stands out (baseline rank, recent role form, current champion
    pool danger) relative to the other four - NOT champion Threat, NOT
    Dependency, and NOT the optimizer's player weight (Recommendation.weights,
    the rank_score share ban optimization actually uses). This is a display
    metric only: it reads `result` but never feeds back into weights, the
    exhaustive search, or any recommended ban - callers must not pass it into
    recommend_bans or treat it as ban-optimization input.

    player_threat_raw(p) = rank_factor(p)**0.6 * form_factor(p)**0.2 * pool_factor(p)**0.2
    (a weighted geometric mean, rank-dominant by design), where:
      rank_factor = p.rank_score / team_mean(rank_score)   -- baseline rank,
        already resolved for Unranked players (see recommend_bans).
      form_factor = role_wr_adj / PLAYER_THREAT_WR_PRIOR, where role_wr_adj is
        this player's whole-role win rate, K-shrunk toward the fixed 0.5
        prior (not their own baseline_winrate) -- recent role-level form,
        1.0 at a neutral (0.5) win rate.
      pool_factor = p.strength() -- the current champion pool's raw danger
        (Dependency deliberately excluded).
    The final value is player_threat_raw normalized to a team average of 1.0.
    """
    mean_rank_score = math.fsum(p.rank_score for p in result.players) / len(result.players)
    raw_scores = {}
    for p in result.players:
        rank_factor = p.rank_score / mean_rank_score
        role_games = sum(c.games for c in p.champions.values())
        role_wins = sum(c.wins for c in p.champions.values())
        role_wr_adj = (role_wins + K * PLAYER_THREAT_WR_PRIOR) / (role_games + K)
        form_factor = role_wr_adj / PLAYER_THREAT_WR_PRIOR
        pool_factor = p.strength()
        raw_scores[p.player_id] = (
            rank_factor ** PLAYER_THREAT_RANK_EXPONENT
            * form_factor ** PLAYER_THREAT_FORM_EXPONENT
            * pool_factor ** PLAYER_THREAT_POOL_EXPONENT
        )
    mean_raw = math.fsum(raw_scores.values()) / len(raw_scores)
    return {player_id: raw / mean_raw for player_id, raw in raw_scores.items()}


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
