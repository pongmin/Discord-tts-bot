"""Why this ban? — deterministic marginal-contribution attribution.

This is an EXPLANATION layer only. It reads a finished Recommendation and
re-evaluates the same model functions the optimizer already used; it never
changes B*, the search, the ordering, or any score the report displays.

For a recommended ban c the comparison is always B* against B* - {c}, the
exact pair whose V_0 difference the optimizer reports as `marginal`. Per
player i, in log space (so the geometric aggregate decomposes additively):

    total_delta_i = w_i * (log r_i(B* - {c}) - log r_i(B*))

and, because r_i = r_perf_i * D_i, that total splits exactly into

    perf_delta_i  = w_i * (log r_perf_i(B* - {c}) - log r_perf_i(B*))
    dep_delta_i   = w_i * (log D_i(B* - {c})      - log D_i(B*))

Summed over the five players, total_delta is log V_0(B* - {c}) - log V_0(B*):
the same quantity as BanImpact.marginal, in logs instead of raw score units.

Two further comparisons separate "strong on its own" from "strong here":

    solo_delta         = sum_i w_i * (-log r_i({c}))       -- c banned alone
    partner_gain(c, d) = sum_i w_i * (log r_i({d}) - log r_i({c, d})) - solo_delta

Reason codes are chosen from these computed values. P_personal, threat and
pick share are only phrasing material (which words the report uses), never
the reason on their own.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
import math

from scouting.ban_algorithm import PlayerModel, Recommendation


# Residuals are strictly positive in the model (p_others > 0 keeps strength
# above zero, dependency is an exp), so this floor is float safety only.
MIN_RESIDUAL = 1e-12
# A single axis owns the explanation only above this share of the ban's total
# log contribution; between the two shares the ban is genuinely mixed.
DOMINANT_SHARE = 0.65
# A player counts as co-affected at this share of the positive contribution.
CONTRIBUTOR_SHARE = 0.20
# Above this, the effect really is one player's, not the team's.
CONCENTRATION_SHARE = 0.80
# Banning c alongside its best partner must beat banning c alone by this much
# of the ban's own contribution before the pairing is called synergy.
SYNERGY_SHARE = 0.20
# Solo effect at most this share of the in-combination contribution means the
# ban earns its slot from the combination, not from itself.
WEAK_SOLO_SHARE = 0.50
# Phrasing material only (see module docstring): never a reason by itself.
ONE_TRICK_P_PERSONAL = 0.70
MAIN_PICK_P_PERSONAL = 0.50
LOW_PICK_P_FINAL = 0.15
HIGH_THREAT = 1.10
FLAT_THREAT = 1.00
# The last recommended ban adds this little next to the strongest one.
SMALL_TAIL_SHARE = 0.30
MAX_REASONS = 2


@dataclass(frozen=True)
class PlayerAttribution:
    """One opponent's share of a single ban's marginal contribution."""

    player_id: int
    label: str
    role: str
    weight: float
    total_delta: float
    perf_delta: float
    dep_delta: float
    # Phrasing material, straight from this player's own champion model.
    p_personal: float
    p_final: float
    threat: float
    pool_exhausted_by_ban: bool

    @property
    def perf_share(self) -> float:
        return _share(self.perf_delta, self.total_delta)

    @property
    def dep_share(self) -> float:
        return _share(self.dep_delta, self.total_delta)


@dataclass(frozen=True)
class BanAttribution:
    """Decomposition of one recommended ban, largest contributor first."""

    champion_id: int
    name: str
    per_player: tuple[PlayerAttribution, ...]
    total_delta: float
    perf_delta: float
    dep_delta: float
    solo_delta: float
    partner_id: int | None
    partner_name: str | None
    partner_gain: float
    share_of_best: float
    reasons: tuple[str, ...]

    @property
    def perf_share(self) -> float:
        return _share(self.perf_delta, self.total_delta)

    @property
    def dep_share(self) -> float:
        return _share(self.dep_delta, self.total_delta)

    @property
    def contributors(self) -> tuple[PlayerAttribution, ...]:
        """Players carrying a real part of the positive contribution."""
        positive = math.fsum(p.total_delta for p in self.per_player if p.total_delta > 0)
        if positive <= 0:
            return ()
        return tuple(p for p in self.per_player
                     if p.total_delta > 0 and p.total_delta / positive >= CONTRIBUTOR_SHARE)

    @property
    def top_share(self) -> float:
        positive = math.fsum(p.total_delta for p in self.per_player if p.total_delta > 0)
        if positive <= 0 or not self.per_player:
            return 0.0
        return max(0.0, self.per_player[0].total_delta) / positive

    @property
    def exhausted(self) -> tuple[PlayerAttribution, ...]:
        return tuple(p for p in self.per_player if p.pool_exhausted_by_ban)


def _share(part: float, total: float) -> float:
    """Signed part over a positive total; 0 for a non-positive contribution."""
    return part / total if total > 0 else 0.0


def _log_residual(player: PlayerModel, bans: Collection[int]) -> tuple[float, float]:
    """(log r_perf, log D) for this player at `bans`."""
    perf = player.strength(bans) / player.strength()
    return (math.log(max(perf, MIN_RESIDUAL)),
            math.log(max(player.dependency(bans), MIN_RESIDUAL)))


def _weighted_log_value(
    players: Sequence[PlayerModel], weights: Sequence[float], bans: Collection[int]
) -> float:
    """log V_0(B) = sum_i w_i * log r_i(B)."""
    return math.fsum(
        w * math.fsum(_log_residual(p, bans))
        for p, w in zip(players, weights, strict=True)
    )


def attribute_ban(result: Recommendation, champion_id: int) -> BanAttribution:
    """Decompose one ban of B* into per-player performance/dependency deltas."""
    full = frozenset(result.best_by_rho[0].bans)
    if champion_id not in full:
        raise ValueError(f"{champion_id} is not part of the recommended ban set.")
    players, weights = result.players, result.weights
    without = full - {champion_id}

    per_player = []
    for player, weight in zip(players, weights, strict=True):
        perf_full, dep_full = _log_residual(player, full)
        perf_without, dep_without = _log_residual(player, without)
        perf_delta = weight * (perf_without - perf_full)
        dep_delta = weight * (dep_without - dep_full)
        champion = player.champions.get(champion_id)
        per_player.append(PlayerAttribution(
            player.player_id, player.label, player.role, weight,
            perf_delta + dep_delta, perf_delta, dep_delta,
            champion.p_personal if champion else 0.0,
            champion.p_final if champion else 0.0,
            champion.threat if champion else 0.0,
            player.observed_pool_exhausted(full) and not player.observed_pool_exhausted(without),
        ))
    # Deterministic order: biggest contributor first, player_id breaks ties.
    per_player.sort(key=lambda p: (-p.total_delta, p.player_id))

    solo_delta = -_weighted_log_value(players, weights, {champion_id})
    partner_id: int | None = None
    partner_gain = 0.0
    for other in sorted(without):
        gain = (
            _weighted_log_value(players, weights, {other})
            - _weighted_log_value(players, weights, {champion_id, other})
        ) - solo_delta
        if partner_id is None or gain > partner_gain:
            partner_id, partner_gain = other, gain

    names = {cid: c.name for p in players for cid, c in p.champions.items()}
    total = math.fsum(p.total_delta for p in per_player)
    # Same log-space marginal for every ban in B*, so "small tail" is measured
    # against the strongest recommended ban rather than an absolute cutoff.
    best_total = max(
        (_weighted_log_value(players, weights, full - {cid})
         - _weighted_log_value(players, weights, full))
        for cid in full
    )
    attribution = BanAttribution(
        champion_id, names.get(champion_id, str(champion_id)), tuple(per_player),
        total,
        math.fsum(p.perf_delta for p in per_player),
        math.fsum(p.dep_delta for p in per_player),
        solo_delta, partner_id,
        names.get(partner_id) if partner_id is not None else None, partner_gain,
        total / best_total if best_total > 0 else 0.0,
        (),
    )
    return replace(attribution, reasons=ban_reasons(attribution))


def attribute_bans(result: Recommendation) -> dict[int, BanAttribution]:
    """Attribution for every champion in B*, keyed by champion_id."""
    return {cid: attribute_ban(result, cid) for cid in result.best_by_rho[0].bans}


def ban_reasons(attribution: BanAttribution) -> tuple[str, ...]:
    """Up to MAX_REASONS reason codes, most important first.

    Slot 1 is the performance/dependency axis the computed deltas actually put
    this contribution on; slot 2 is the structural reason (combination, spread,
    pool pressure) when one applies.
    """
    if attribution.total_delta <= 0:
        return ("redistribution",)

    contributors = attribution.contributors
    perf_lead = [p for p in contributors if p.perf_share >= DOMINANT_SHARE]
    dep_lead = [p for p in contributors if p.dep_share >= DOMINANT_SHARE]
    if len(contributors) >= 2 and perf_lead and dep_lead:
        axis = "split_perf_dep"
    elif attribution.perf_share >= DOMINANT_SHARE:
        axis = "performance"
    elif attribution.dep_share >= DOMINANT_SHARE:
        axis = "dependency"
    else:
        axis = "mixed"

    synergy = (
        attribution.partner_id is not None
        and attribution.partner_gain >= SYNERGY_SHARE * attribution.total_delta
    )
    weak_solo = attribution.solo_delta <= WEAK_SOLO_SHARE * attribution.total_delta
    top = attribution.per_player[0] if attribution.per_player else None
    relied_on = max((p.p_personal for p in attribution.per_player), default=0.0)
    # "Their main, but not a champion they actually outperform on."
    flat_threat = all(p.threat <= FLAT_THREAT
                      for p in attribution.per_player if p.p_personal > 0)

    structural = None
    if attribution.exhausted:
        structural = "pool_exhaustion"
    elif relied_on >= MAIN_PICK_P_PERSONAL and flat_threat and (weak_solo or synergy):
        structural = "main_pick_only_in_combination"
    elif weak_solo and synergy:
        structural = "weak_solo_strong_combination"
    elif synergy:
        structural = "combination_synergy"
    elif (top is not None and 0 < top.p_final <= LOW_PICK_P_FINAL
          and top.threat >= HIGH_THREAT and axis == "performance"):
        structural = "low_pick_high_threat"
    elif len(contributors) >= 2:
        structural = "multi_player"
    elif attribution.share_of_best <= SMALL_TAIL_SHARE:
        structural = "small_but_best_available"
    elif attribution.top_share >= CONCENTRATION_SHARE:
        structural = "single_player"

    return tuple(code for code in (axis, structural) if code)[:MAX_REASONS]
