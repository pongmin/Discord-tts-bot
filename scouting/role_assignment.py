"""Role-fit assignment, v1. Reads collected queue 420/400 data only.

This module answers a different question from the ban recommender: given five
players and the five canonical roles, who should play where. It reuses the ban
model's per-role champion math verbatim - `build_player_model` is called
unchanged for every player/role cell that has games - and adds only the three
role-comparison factors the ban model has no need for:

    F  role share, how much of this player's history is actually in this role
    R  role win rate versus their own all-role baseline
    C  the ban model's own observed-pool strength in that role
    B  champion-pool breadth in that role, as an entropy-effective pool size

    E = max(F * R * C * B**0.4, MIN_ROLE_FIT)

F is deliberately the strongest term and enters linearly: how often someone
actually plays a role is the best evidence available about whether they can play
it. R, C and B are secondary corrections of roughly comparable influence - each
lands in a band around 0.7-1.2 for realistic inputs - and none of them can
overturn a large difference in F on its own.

N_REF and the breadth exponent are initial v1 constants, chosen to be eyeballed
against real diagnostic matrices (which is why both B and raw N_eff are in the
/assignroles details output). They are not fitted, and nothing here tunes them.

The ban recommender is untouched: nothing here calls `recommend_bans`, and no
ban candidate, pruning, rho aggregation, KDA or Others computation is redefined.

`solo_rank_score` is deliberately absent from E. It is constant across roles for
the same player, so it cannot change any assignment's ranking - it would only add
the same constant to all 120 permutations.

Where the ban model is strict (a role with no games is a hard error, because
recommending bans against a player you have never seen in that role is
meaningless), assignment must instead compare every player against every role,
including ones they have never played. `build_role_candidate_model` is that
tolerant variant: for zero role games it returns an Others-only neutral model
(strength 1.0, the same unseen-champion prior the ban model falls back to when a
pool is banned out) rather than raising, and the F factor is what actually
penalizes the unplayed role.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import permutations
import math
import sqlite3

from scouting.ban_algorithm import (
    K, QUEUE_WEIGHTS, ROLES, PlayerModel, build_player_model,
)
from scouting.scouting_repo import ScoutingRepo


ROLE_ORDER = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")
# Only reached when a player has zero queue 420/400 games in every role, which
# makes all five roles equally (un)supported rather than all-zero probabilities.
DEFAULT_ROLE_PROBABILITY = 1.0 / len(ROLE_ORDER)
# F = (P_role / max P_role), raised to this in E. 1.0 keeps role share the
# dominant, linear term. Placeholder v1 value, not tuned.
ROLE_SHARE_EXPONENT = 1.0
# B raised to this in E. Breadth should matter without dominating: a one-trick
# is worth less than a comparable player with options, but not half as much.
# Placeholder v1 value, not tuned.
ROLE_BREADTH_EXPONENT = 0.4
# N_REF: the effective pool size that earns full breadth credit. Roughly a v1
# reading of "a comfortable champion pool for one role" - one champion lands
# well below 1, about three is moderate, five or more saturates. Placeholder v1
# value, not tuned.
BREADTH_REFERENCE_POOL = 5.0
# Same shrinkage strength the ban model uses for per-champion win rate, applied
# here to per-role win rate against the player's own all-role baseline.
ROLE_WR_PRIOR_GAMES = K
ROLE_WR_SENSITIVITY = 1.5
MIN_ROLE_FIT = 1e-6
# A player with no usable queue 420/400 games at all has no baseline to shrink
# toward; R then collapses to exp(0) = 1 for every role, which is the intent.
NEUTRAL_WINRATE = 0.5


def _player_label(player: sqlite3.Row | None, player_id: int) -> str:
    if player is not None and player["game_name"] and player["tag_line"]:
        return f"{player['game_name']}#{player['tag_line']}"
    return f"Player {player_id}"


@dataclass(frozen=True)
class RoleCandidateModel:
    """One player/role cell: the ban model plus this role's raw totals.

    `observed` is False for the neutral stand-in built when the player has no
    games in this role; `model.champions` is then empty and all of its
    probability mass sits in Others, so `strength()` is exactly T_OTHERS.
    """

    model: PlayerModel
    games_in_role: int
    wins_in_role: int
    observed: bool

    @property
    def player_id(self) -> int:
        return self.model.player_id

    @property
    def label(self) -> str:
        return self.model.label

    @property
    def role(self) -> str:
        return self.model.role

    def strength(self, bans=frozenset()) -> float:
        """C: the unchanged PlayerModel.strength for this role."""
        return self.model.strength(bans)


def effective_pool_size(model: PlayerModel) -> float:
    """N_eff = exp(H) over the role's personal pick distribution.

    Breadth means champions this player has actually been observed picking, so
    this reads P_personal (the pre-meta-blend share the ban model already
    computes) and never the Others bucket - Others is a prior over champions
    nobody has seen them play, which is the opposite of demonstrated breadth.

    One champion gives exp(0) = 1, an even n-champion pool gives n, and an
    unplayed role gives 0.
    """
    shares = [champion.p_personal for champion in model.champions.values()
              if champion.p_personal > 0]
    if not shares:
        return 0.0
    entropy = -math.fsum(share * math.log(share) for share in shares)
    return math.exp(entropy)


def breadth_score(effective_pool: float) -> float:
    """B: saturating absolute breadth, comparable ACROSS players.

    B = min(1, log(1 + N_eff) / log(1 + N_REF)), so it answers "is this a real
    champion pool" on one fixed scale rather than "is this their broadest role".
    Two players competing for the same role are therefore separated by it, which
    a per-player normalization could not do - one-tricks would both normalize to
    1.0 on their own scale.

    Saturating rather than linear: the gap between one champion and three
    matters far more than the gap between six and eight. An unplayed role
    (N_eff = 0) scores 0, which is the floor, not a divide-by-zero.
    """
    if effective_pool <= 0:
        return 0.0
    return min(1.0, math.log1p(effective_pool) / math.log1p(BREADTH_REFERENCE_POOL))


def global_baseline_winrate(repo: ScoutingRepo, player_id: int) -> float:
    """This player's win rate over ALL collected queue 420/400 games.

    Deliberately role-blind: it is the reference R compares each role's win
    rate against, so it must not itself move from role to role.
    """
    rows = repo.get_all_matches(player_id, queue_ids=tuple(QUEUE_WEIGHTS))
    results = [row["win"] for row in rows if row["win"] in (0, 1)]
    if not results:
        return NEUTRAL_WINRATE
    return sum(results) / len(results)


def build_role_candidate_model(
    repo: ScoutingRepo, player_id: int, role: str
) -> RoleCandidateModel:
    """Tolerant `build_player_model`: never raises on an unplayed role.

    With games in the role this is the strict model verbatim, so P_final,
    Threat, KDA and Others are exactly what /banrecommend would compute. With
    zero games it is an Others-only neutral model instead of a ValueError.
    """
    if repo.cutoff_time is None:
        raise ValueError("Role assignment requires ScoutingRepo(cutoff_time=now_ms).")
    role = role.upper()
    if role not in ROLES:
        raise ValueError(f"Unknown canonical role: {role}")
    player = repo.get_player(player_id)
    if player is None:
        raise ValueError(f"Unknown collected player: {player_id}")

    rows = repo.get_role_matches(player_id, role, queue_ids=tuple(QUEUE_WEIGHTS))
    if rows:
        model = build_player_model(repo, player_id, role)
        games = sum(champion.games for champion in model.champions.values())
        wins = sum(champion.wins for champion in model.champions.values())
        return RoleCandidateModel(model, games, wins, True)

    label = _player_label(player, player_id)
    neutral = PlayerModel(
        player_id, label, role, {}, global_baseline_winrate(repo, player_id),
        rank_score=0.0,
        warnings=(f"{label}/{role}: no collected queue 420/400 role games; "
                  "using the neutral unseen-champion prior for role fit.",),
        kda_baseline=0.0,
        # All mass in Others: strength() is T_OTHERS, the same neutral value the
        # ban model uses for a fully banned-out pool. Never a zero strength.
        p_others=1.0,
        is_ranked=False,
    )
    return RoleCandidateModel(neutral, 0, 0, False)


@dataclass(frozen=True)
class RoleFit:
    """One cell of the 5x5 matrix, with every factor kept for diagnostics."""

    player_id: int
    label: str
    role: str
    games: int
    wins: int
    observed: bool
    role_probability: float  # P_role
    share: float             # F
    winrate_adjusted: float  # WR_role_adj
    winrate_ratio: float     # R
    strength: float          # C
    effective_pool: float    # N_eff
    breadth: float           # B
    fit: float               # E


def player_role_fits(repo: ScoutingRepo, player_id: int) -> dict[str, RoleFit]:
    """All five role cells for one player, from one collected dataset.

    Every cell comes from the same already-collected 420/400 history - there is
    no per-role collection, and no role's numbers depend on which roles the
    other four players end up taking.
    """
    baseline = global_baseline_winrate(repo, player_id)
    candidates = {
        role: build_role_candidate_model(repo, player_id, role) for role in ROLE_ORDER
    }
    total_games = sum(candidate.games_in_role for candidate in candidates.values())
    probabilities = {
        role: (candidate.games_in_role / total_games if total_games
               else DEFAULT_ROLE_PROBABILITY)
        for role, candidate in candidates.items()
    }
    # Normalizing by the player's own main role makes F a within-player
    # comparison: their best role always scores 1.0, whatever their total games.
    top_probability = max(probabilities.values())
    # B is NOT normalized within the player: unlike F, breadth is meant to be
    # comparable between the five players competing for the same role.
    pools = {role: effective_pool_size(candidate.model)
             for role, candidate in candidates.items()}

    fits = {}
    for role in ROLE_ORDER:
        candidate = candidates[role]
        games, wins = candidate.games_in_role, candidate.wins_in_role
        strength = candidate.strength(frozenset())
        probability = probabilities[role]
        share = probability / top_probability
        winrate_adjusted = (
            (wins + ROLE_WR_PRIOR_GAMES * baseline) / (games + ROLE_WR_PRIOR_GAMES)
        )
        ratio = math.exp(ROLE_WR_SENSITIVITY * (winrate_adjusted - baseline))
        pool = pools[role]
        breadth = breadth_score(pool)
        fit = (share ** ROLE_SHARE_EXPONENT) * ratio * strength * (
            breadth ** ROLE_BREADTH_EXPONENT
        )
        fits[role] = RoleFit(
            player_id, candidate.label, role, games, wins, candidate.observed,
            probability, share, winrate_adjusted, ratio, strength, pool, breadth,
            max(fit, MIN_ROLE_FIT),
        )
    return fits


@dataclass(frozen=True)
class Assignment:
    """One complete player->role assignment, in ROLE_ORDER order."""

    fits: tuple[RoleFit, ...]
    score: float

    @property
    def by_role(self) -> dict[str, RoleFit]:
        return {fit.role: fit for fit in self.fits}


@dataclass(frozen=True)
class RoleAssignmentResult:
    player_ids: tuple[int, ...]
    labels: dict[int, str]
    baselines: dict[int, float]
    matrix: dict[tuple[int, str], RoleFit]
    best: Assignment
    runner_up: Assignment

    @property
    def margin(self) -> float:
        """How much log-score the best assignment wins by. Never negative."""
        return self.best.score - self.runner_up.score

    def fit(self, player_id: int, role: str) -> RoleFit:
        return self.matrix[(player_id, role)]


def assign_roles(repo: ScoutingRepo, player_ids: Sequence[int]) -> RoleAssignmentResult:
    """Best and second-best of all 120 role permutations, by sum(log E).

    Scoring is a plain log-sum - no softmax, no probabilities over assignments.
    Ties keep permutation order, so the result is deterministic.
    """
    player_ids = tuple(player_ids)
    if len(player_ids) != len(ROLE_ORDER):
        raise ValueError(f"Role assignment needs exactly {len(ROLE_ORDER)} players.")
    if len(set(player_ids)) != len(player_ids):
        raise ValueError("Role assignment needs five distinct players.")

    matrix = {}
    labels = {}
    baselines = {}
    for player_id in player_ids:
        fits = player_role_fits(repo, player_id)
        baselines[player_id] = global_baseline_winrate(repo, player_id)
        for role, fit in fits.items():
            matrix[(player_id, role)] = fit
            labels[player_id] = fit.label

    ranked = []
    for order in permutations(player_ids):
        # order[i] takes ROLE_ORDER[i]; E is floored at MIN_ROLE_FIT, so log is
        # always finite.
        chosen = tuple(matrix[(player_id, role)] for player_id, role in zip(order, ROLE_ORDER))
        ranked.append(Assignment(chosen, math.fsum(math.log(fit.fit) for fit in chosen)))
    # Stable sort: equal scores keep itertools.permutations order.
    ranked.sort(key=lambda assignment: -assignment.score)
    return RoleAssignmentResult(
        player_ids, labels, baselines, matrix, ranked[0], ranked[1],
    )


def personal_fit_percent(result: RoleAssignmentResult, fit: RoleFit) -> float:
    """Display-only: this role's E as a percentage of the player's best role.

    Answers "how close is this to where they belong", which is the question a
    reader actually has about a recommended seat - raw E values sit in ranges
    nobody can calibrate by eye. The player's strongest role is always 100%.

    This normalization is for rendering ONLY. The assignment search keeps using
    raw E, so it must never be fed back into scoring: dividing by a per-player
    maximum would erase exactly the cross-player differences that decide who
    gets a contested role.
    """
    best = max(result.fit(fit.player_id, role).fit for role in ROLE_ORDER)
    if best <= 0:
        return 0.0
    return fit.fit / best * 100.0


def geometric_mean_fit(assignment: Assignment) -> float:
    """GM(A) = exp(Score(A) / 5): the log-sum read back as an average E.

    Score is sum(log E) over the five seats, so its exponential per seat is the
    geometric mean of the five role fits - the same ranking, on a scale that
    can be compared as a ratio.
    """
    return math.exp(assignment.score / len(ROLE_ORDER))


def team_fit_percent(result: RoleAssignmentResult, assignment: Assignment) -> float:
    """Display-only: GM(A) against GM(best), as a percentage.

    The best assignment is 100% by construction and the runner-up shows how
    little is given up by preferring it - a 96.4% second choice is a real
    alternative, a 40% one is not. Computed from the score difference directly
    rather than as a ratio of two exponentials, which keeps it finite for the
    very small E values MIN_ROLE_FIT allows.
    """
    return math.exp(
        (assignment.score - result.best.score) / len(ROLE_ORDER)
    ) * 100.0


def format_assignment(result: RoleAssignmentResult) -> str:
    """Plain-text dump for manual/debug use; Discord rendering lives elsewhere."""
    lines = [
        f"best={result.best.score:.4f} runner_up={result.runner_up.score:.4f} "
        f"margin={result.margin:.4f}",
        "",
    ]
    for fit in result.best.fits:
        lines.append(f"{fit.role:<8} {fit.label} E={fit.fit:.4f} games={fit.games}")
    lines.append("")
    lines.append("matrix (E / F / R / C / B / games):")
    for player_id in result.player_ids:
        lines.append(f"{result.labels[player_id]} baseline={result.baselines[player_id]:.3f}")
        for role in ROLE_ORDER:
            fit = result.matrix[(player_id, role)]
            lines.append(
                f"  {role:<8} E={fit.fit:.4f} F={fit.share:.4f} R={fit.winrate_ratio:.4f} "
                f"C={fit.strength:.4f} B={fit.breadth:.4f} N_eff={fit.effective_pool:.2f} "
                f"games={fit.games}"
            )
    return "\n".join(lines)


__all__ = [
    "ROLE_ORDER", "Assignment", "RoleAssignmentResult", "RoleCandidateModel", "RoleFit",
    "assign_roles", "breadth_score", "build_role_candidate_model",
    "effective_pool_size", "format_assignment", "geometric_mean_fit",
    "global_baseline_winrate", "personal_fit_percent", "player_role_fits",
    "team_fit_percent",
]
