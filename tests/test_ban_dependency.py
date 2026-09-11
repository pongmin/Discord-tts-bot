"""Champion Dependency: separates "banned champion loses familiar pool" (D)
from "banned champion was performing well" (r_perf). r(p,B) = r_perf(p,B) * D(p,B).
"""

import math
import unittest
from unittest.mock import Mock, patch

from scouting.ban_algorithm import (
    ETA, ChampionModel, PlayerModel, recommend_bans,
)


def champion(cid, p_personal, p_final, threat, *, p_meta=0.0, name=None, games=10, wins=6):
    return ChampionModel(cid, name or f"Champion{cid}", games, wins, p_personal, p_meta, p_final, 0.5, threat)


def player(champions, *, player_id=1, label="Fixture#TEST", role="BOTTOM", p_others=0.05):
    return PlayerModel(player_id, label, role, champions, 0.5, 1, p_others=p_others)


class DependencyTests(unittest.TestCase):
    # 1. B={} => every player's final residual is exactly 1.
    def test_empty_ban_gives_exact_residual_one(self):
        champions = {
            1: champion(1, .6, .55, 0.6),
            2: champion(2, .3, .3, 1.8),
            3: champion(3, .1, .1, 2.5),
        }
        model = player(champions)
        self.assertEqual(model.dependency_mass(), 0.0)
        self.assertEqual(model.dependency(), 1.0)
        self.assertEqual(model.residual_ratio(), 1.0)

    # 2. Same performance, higher P_personal => larger dependency penalty.
    def test_higher_p_personal_ban_incurs_larger_dependency_penalty(self):
        champions = {
            1: champion(1, .7, .65, 1.0),   # dominant main, mediocre performance
            2: champion(2, .1, .12, 1.0),   # rarely played, identical threat
            3: champion(3, .1, .1, 1.0),
        }
        model = player(champions, p_others=.1)

        # With identical threat, the performance-only residual is the same for
        # both bans - isolating the gap between them as 100% Dependency.
        perf_main = model.strength({1}) / model.strength()
        perf_minor = model.strength({2}) / model.strength()
        self.assertAlmostEqual(perf_main, perf_minor)

        self.assertLess(model.dependency({1}), model.dependency({2}))
        self.assertLess(model.residual_ratio({1}), model.residual_ratio({2}))

    # 3. Banning several mains accumulates the dependency penalty.
    def test_dependency_accumulates_across_multiple_banned_mains(self):
        champions = {
            1: champion(1, .5, .5, 1.0),
            2: champion(2, .3, .3, 1.0),
            3: champion(3, .1, .1, 1.0),
        }
        model = player(champions, p_others=.1)
        self.assertAlmostEqual(model.dependency_mass({1}), .5)
        self.assertAlmostEqual(model.dependency_mass({1, 2}), .8)
        self.assertAlmostEqual(model.dependency({1, 2}), math.exp(-ETA * .8))
        self.assertLess(model.dependency({1, 2}), model.dependency({1}))
        self.assertLess(model.residual_ratio({1, 2}), model.residual_ratio({1}))

    # 4. Exhausting the observed pool never zeroes the residual.
    def test_full_pool_exhaustion_keeps_residual_positive(self):
        champions = {
            1: champion(1, .6, .55, 2.0),
            2: champion(2, .25, .25, 1.8),
            3: champion(3, .15, .15, 1.5),
        }
        model = player(champions)
        bans = {1, 2, 3}
        self.assertTrue(model.observed_pool_exhausted(bans))
        self.assertAlmostEqual(model.dependency_mass(bans), 1.0)
        self.assertAlmostEqual(model.dependency(bans), math.exp(-ETA))
        self.assertGreater(model.residual_ratio(bans), 0)

    # 5. Others is never part of the dependency mass, however large p_others is.
    def test_others_never_contributes_to_dependency_mass(self):
        champions = {1: champion(1, .3, .28, 1.2)}
        model = player(champions, p_others=.7)
        self.assertAlmostEqual(model.dependency_mass({1}), .3)
        self.assertAlmostEqual(model.dependency({1}), math.exp(-ETA * .3))
        # Banning a champion outside the pool (Others has no ID to ban) can't
        # move dependency at all.
        self.assertEqual(model.dependency_mass({999}), 0.0)

    # 6. The exhaustive search / rho aggregation / marginal contribution all
    # consume the new (performance * dependency) residual, not r_perf alone.
    def test_search_marginal_and_diagnostics_use_the_dependency_adjusted_residual(self):
        main_player = player(
            {
                1: champion(1, .8, .75, .8),   # dominant main, mediocre performance
                2: champion(2, .1, .12, 1.0),
                3: champion(3, .1, .13, 1.0),
            },
            player_id=1, label="Main#TEST", role="TOP", p_others=.05,
        )
        fillers = [
            player({10 + pid: champion(10 + pid, 0.0, .9, 1.0)},
                   player_id=pid, label=f"Player{pid}#TEST", role=role, p_others=.1)
            for pid, role in enumerate(("JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"), 2)
        ]
        players = [main_player, *fillers]
        with patch("scouting.ban_algorithm.build_player_model", side_effect=players):
            result = recommend_bans(Mock(), [(p.player_id, p.role) for p in players])

        best_bans = frozenset(result.best_by_rho[0].bans)
        self.assertIn(1, best_bans)  # the mediocre-but-relied-on main gets banned

        diag = next(d for d in result.player_diagnostics if d.player_id == 1)
        self.assertAlmostEqual(diag.residual_ratio, main_player.residual_ratio(best_bans))
        # Sanity: that figure is strictly below the performance-only residual,
        # i.e. Dependency actually moved the number the search/report use.
        perf_only = main_player.strength(best_bans) / main_player.strength()
        self.assertLess(diag.residual_ratio, perf_only)

        recommended_impact = next(b for b in result.recommended if b.champion_id == 1)
        self.assertTrue(math.isfinite(recommended_impact.marginal))

    # 7. No NaN/inf/division-by-zero across degenerate inputs.
    def test_no_nan_or_inf_across_extreme_inputs(self):
        for p_personal, p_final, threat, p_others in (
            (1.0, 1.0, 0.0, 1e-9),
            (0.0, 1e-9, 100.0, 1.0),
            (0.5, 0.5, 1e-10, 0.5),
        ):
            with self.subTest(p_personal=p_personal, threat=threat, p_others=p_others):
                model = player({1: champion(1, p_personal, max(p_final, 1e-9), threat)}, p_others=p_others)
                for bans in (frozenset(), {1}, {999}):
                    for value in (
                        model.dependency_mass(bans), model.dependency(bans), model.residual_ratio(bans),
                    ):
                        self.assertTrue(math.isfinite(value), value)


class SamilhaComparisonTests(unittest.TestCase):
    """Reconstructed from the reported 샤밀하#KR1/BOTTOM stats (Miss Fortune
    ~77% pick share at threat 0.75; Lucian ~9% at threat 2.17) - real DB rows
    for that account aren't available in this environment, so P_personal is
    assumed equal to P_final for the two named champions (meta backoff is
    LAMBDA=0.15 and typically minor). The remaining ~9% is split over two
    small, mediocre-performance picks (threat below baseline, like the named
    champions' own occasional off-meta attempts would realistically be for a
    one-trick) so the pool sums to 1 - p_others; making the leftovers
    *better*-performing than Lucian would understate the effect, and isn't
    the realistic case this fix targets.
    """

    MISS_FORTUNE = 21
    LUCIAN = 236

    @classmethod
    def build_samilha(cls):
        champions = {
            cls.MISS_FORTUNE: champion(cls.MISS_FORTUNE, .77, .77, .75, name="Miss Fortune"),
            cls.LUCIAN: champion(cls.LUCIAN, .09, .09, 2.17, name="Lucian"),
            103: champion(103, .05, .05, .8, name="Ashe"),
            22: champion(22, .04, .04, .7, name="Kaisa"),
        }
        return player(champions, player_id=1, label="샤밀하#KR1", role="BOTTOM", p_others=.05)

    def build_team(self):
        samilha = self.build_samilha()
        # 9000s: guaranteed not to collide with Samilha's own champion IDs
        # (21, 236, 103, 22) above - a collision would let a filler player's
        # ban accidentally double as a ban for Samilha's pool too.
        fillers = [
            player({9000 + pid: champion(9000 + pid, .4, .4, 1.0)},
                   player_id=pid, label=f"Player{pid}#TEST", role=role, p_others=.1)
            for pid, role in enumerate(("TOP", "JUNGLE", "MIDDLE", "UTILITY"), 2)
        ]
        return [samilha, *fillers]

    def test_dependency_moves_miss_fortune_into_the_recommended_bans(self):
        samilha = self.build_samilha()

        r_mf_only = samilha.residual_ratio({self.MISS_FORTUNE})
        r_lucian_only = samilha.residual_ratio({self.LUCIAN})
        r_both = samilha.residual_ratio({self.MISS_FORTUNE, self.LUCIAN})

        # Performance alone says Lucian is the scarier ban (threat 2.17 vs
        # 0.75); Dependency must still make banning Miss Fortune cost more
        # overall once her 77% pick-share loss is priced in.
        perf_mf_only = samilha.strength({self.MISS_FORTUNE}) / samilha.strength()
        perf_lucian_only = samilha.strength({self.LUCIAN}) / samilha.strength()
        self.assertGreater(perf_mf_only, perf_lucian_only)  # performance alone: MF looks "safe to leave"
        self.assertLess(r_mf_only, perf_mf_only)             # Dependency pulls her residual down
        self.assertAlmostEqual(samilha.dependency({self.MISS_FORTUNE}), math.exp(-ETA * .77))
        self.assertAlmostEqual(samilha.dependency({self.LUCIAN}), math.exp(-ETA * .09))
        # Dependency mass (and so the penalty) accumulates when banning both,
        # even though the combined *residual* also depends on how much
        # threat remains in the pool once both are gone.
        self.assertLess(
            samilha.dependency({self.MISS_FORTUNE, self.LUCIAN}),
            min(samilha.dependency({self.MISS_FORTUNE}), samilha.dependency({self.LUCIAN})),
        )
        self.assertGreater(r_both, 0)

        # Full 5-player search, compared with Dependency off (ETA=0, i.e. the
        # pre-this-change model) vs. on (ETA=0.35, the default) - everything
        # else (pick probabilities, threat, Others, redistribution, search)
        # is identical between the two runs.
        players = self.build_team()
        with patch("scouting.ban_algorithm.build_player_model", side_effect=players):
            with patch("scouting.ban_algorithm.ETA", 0.0):
                before = recommend_bans(Mock(), [(p.player_id, p.role) for p in players])
        with patch("scouting.ban_algorithm.build_player_model", side_effect=players):
            after = recommend_bans(Mock(), [(p.player_id, p.role) for p in players])

        def rank_of(result, champion_id):
            return next((i for i, b in enumerate(result.recommended, 1) if b.champion_id == champion_id), None)

        # Before: performance alone prefers banning Lucian + her tiny Ashe
        # pick over ever touching her 77%-share main - Miss Fortune doesn't
        # even make B*. After: Dependency's pick-share cost outweighs her
        # mediocre performance, and she displaces Ashe in B* (rank 2).
        self.assertIsNone(rank_of(before, self.MISS_FORTUNE))
        self.assertEqual(rank_of(after, self.MISS_FORTUNE), 2)
        self.assertEqual(set(before.best_by_rho[0].bans) & {self.MISS_FORTUNE, self.LUCIAN}, {self.LUCIAN})
        self.assertLessEqual({self.MISS_FORTUNE, self.LUCIAN}, set(after.best_by_rho[0].bans))
        # Lucian - the actually-scary pick - is never displaced by the change.
        self.assertEqual(rank_of(before, self.LUCIAN), 1)
        self.assertEqual(rank_of(after, self.LUCIAN), 1)


if __name__ == "__main__":
    unittest.main()
