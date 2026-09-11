"""Alternative 3-ban sets — a second look at the same search space.

This is an EXPLANATION layer only, like ban_attribution. It re-walks the
combinations the optimizer already scored and re-evaluates the same model
functions; it never changes B*, the search, the recommended order, or any
score the report displays.

Two alternatives are reported, both measured against B* = best_by_rho[0]:

    alt 1: the best set that differs from B* in exactly one ban
    alt 2: the best set that differs from B* in two or more bans

Picking them by distance rather than by rank is the whole point: the runner-up
combinations by score are usually B* with one champion nudged sideways, so a
plain 2nd/3rd place list says nothing new. A set is described by what actually
separates it from B* - which player it piles onto, whose one-trick it deletes,
which high-threat pick it closes - all read off the same P_personal / threat /
marginal values the report already shows elsewhere, so the same input always
produces the same explanation.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from itertools import combinations

from scouting.ban_algorithm import (
    BAN_COUNT, PlayerModel, Recommendation, geometric_mean,
)
from scouting.ban_attribution import HIGH_THREAT, ONE_TRICK_P_PERSONAL


# Two bans on one player is the point at which a set reads as "focused on that
# role" rather than as a spread; with BAN_COUNT == 3 it is also the most a set
# can spend on one player while still differing from B*.
ROLE_FOCUS_BANS = 2


@dataclass(frozen=True)
class AlternativeBanSet:
    """One alternative to B*, with the computed values that explain it."""

    bans: tuple[int, ...]
    names: tuple[str, ...]
    value: float
    threat_reduction: float
    # Reduction given up against B*, as a ratio (0.015 -> "-1.5%p").
    loss: float
    differing: int
    added: tuple[int, ...]
    # B*'s bans this set gives up, strongest marginal contribution first.
    dropped: tuple[int, ...]
    reason: str
    focus_role: str | None = None
    focus_champion_id: int | None = None
    focus_bans: int = 0
    # Model names for the ids above, so the report can name a champion the
    # Data Dragon cache does not have without looking it up again.
    added_names: tuple[str, ...] = ()
    dropped_names: tuple[str, ...] = ()
    focus_name: str | None = None


def _owners(players: Sequence[PlayerModel], champion_id: int) -> tuple[PlayerModel, ...]:
    return tuple(p for p in players if champion_id in p.champions)


def _peak_owner(
    players: Sequence[PlayerModel], champion_id: int, field: str
) -> PlayerModel | None:
    """Whoever leads on p_personal / threat for a champion, ties by player_id."""
    owners = _owners(players, champion_id)
    if not owners:
        return None
    return min(owners, key=lambda p: (-getattr(p.champions[champion_id], field), p.player_id))


def _peak(players: Sequence[PlayerModel], champion_id: int, field: str) -> float:
    """Highest p_personal / threat for a champion across everyone who plays it."""
    owner = _peak_owner(players, champion_id, field)
    return getattr(owner.champions[champion_id], field) if owner else 0.0


def _classify(
    result: Recommendation, bans: tuple[int, ...], best_bans: frozenset[int],
    added: tuple[int, ...], dropped: tuple[int, ...],
) -> tuple[str, str | None, int | None, int]:
    """(reason, focus_role, focus_champion_id, focus_bans) for one set.

    Checked in a fixed order, so a set that qualifies on several counts always
    gets the same - and the most structural - explanation.
    """
    players = result.players

    # 1. Two bans aimed at one player, where B* did not aim that many there.
    focused = sorted(
        (
            (sum(1 for cid in bans if cid in p.champions), p.rank_score, p)
            for p in players
        ),
        key=lambda item: (-item[0], -item[1], item[2].player_id),
    )
    count, _, player = focused[0]
    in_best = sum(1 for cid in best_bans if cid in player.champions)
    if count >= ROLE_FOCUS_BANS and count > in_best:
        return "role_focus", player.role, None, count

    # 2. A newly added ban that deletes somebody's one-trick.
    one_tricks = sorted(
        (cid for cid in added if _peak(players, cid, "p_personal") >= ONE_TRICK_P_PERSONAL),
        key=lambda cid: (-_peak(players, cid, "p_personal"), cid),
    )
    if one_tricks:
        owner = _peak_owner(players, one_tricks[0], "p_personal")
        return "one_trick", owner.role, one_tricks[0], 0

    # 3. A high-threat pick B* leaves open, closed here at the expense of a
    #    tamer champion - only when the swap really does raise the threat bar.
    dropped_threat = max((_peak(players, cid, "threat") for cid in dropped), default=0.0)
    blocked = sorted(
        (cid for cid in added
         if _peak(players, cid, "threat") >= HIGH_THREAT
         and _peak(players, cid, "threat") > dropped_threat),
        key=lambda cid: (-_peak(players, cid, "threat"), cid),
    )
    if blocked:
        owner = _peak_owner(players, blocked[0], "threat")
        return "high_threat_block", owner.role, blocked[0], 0

    return "swap", None, None, 0


def alternative_ban_sets(result: Recommendation) -> tuple[AlternativeBanSet, ...]:
    """Best 1-ban-different and best 2+-ban-different alternatives to B*.

    Same candidate pool, same aggregate (rho = 0) and same residuals as the
    optimizer's own search, so every value here is directly comparable to
    result.threat_reduction. Returns fewer than two entries only when the
    candidate pool is too small to contain such a set.
    """
    players, weights = result.players, result.weights
    baselines = tuple(p.strength() for p in players)
    best = result.best_by_rho[0]
    best_bans = frozenset(best.bans)

    def value(bans: Collection[int]) -> float:
        return geometric_mean(
            tuple((p.strength(bans) / baseline) * p.dependency(bans)
                  for p, baseline in zip(players, baselines, strict=True)),
            weights,
        )

    # One pass over the same combinations the optimizer walked, keeping the
    # best set at each distance. Ties break lexicographically on the sorted
    # champion ids, exactly like _exhaustive_search.
    by_distance: dict[str, tuple[float, tuple[int, ...]] | None] = {"one": None, "many": None}
    for bans in combinations(result.candidates, BAN_COUNT):
        differing = BAN_COUNT - len(best_bans.intersection(bans))
        if differing == 0:
            continue
        slot = "one" if differing == 1 else "many"
        current = by_distance[slot]
        candidate = (value(bans), bans)
        if current is None or candidate < current:
            by_distance[slot] = candidate

    names = {cid: c.name for p in players for cid, c in p.champions.items()}
    # B*'s own bans, strongest marginal first: what an alternative gives up is
    # read off the same ordering the report's 1~3위 list already shows.
    marginals = {impact.champion_id: impact.marginal for impact in result.recommended}

    alternatives = []
    for slot in ("one", "many"):
        entry = by_distance[slot]
        if entry is None:
            continue
        value_, bans = entry
        added = tuple(cid for cid in bans if cid not in best_bans)
        dropped = tuple(sorted(best_bans - set(bans),
                               key=lambda cid: (-marginals.get(cid, 0.0), cid)))
        reason, role, focus_id, focus_bans = _classify(result, bans, best_bans, added, dropped)
        alternatives.append(AlternativeBanSet(
            bans=bans,
            names=tuple(names.get(cid, str(cid)) for cid in bans),
            value=value_,
            threat_reduction=1 - value_,
            # Both reductions are 1 - value, so the difference is just the
            # value gap; never negative, since B* is the search optimum.
            loss=max(0.0, value_ - best.value),
            differing=len(added),
            added=added,
            dropped=dropped,
            reason=reason,
            focus_role=role,
            focus_champion_id=focus_id,
            focus_bans=focus_bans,
            added_names=tuple(names.get(cid, str(cid)) for cid in added),
            dropped_names=tuple(names.get(cid, str(cid)) for cid in dropped),
            focus_name=names.get(focus_id) if focus_id is not None else None,
        ))
    return tuple(alternatives)
