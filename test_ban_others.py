"""Others mass conservation, neutral redistribution, and presentation regressions."""

from dataclasses import replace
import math
import unittest
from unittest.mock import Mock, patch

from ban_algorithm import (
    LAMBDA, OTHERS_EPSILON, ChampionModel, PlayerModel, build_player_model,
    geometric_mean, harmonic_mean, recommend_bans,
)
from ban_report_ui import render_summary_page, render_player_page, PlayerPresentation


class OthersTests(unittest.TestCase):
    def build(self, meta_ids):
        repo = Mock(cutoff_time=2_000_000_000_000)
        repo.get_player.return_value = {"game_name": "Fixture", "tag_line": "TEST"}
        repo.get_latest_rank_snapshot.return_value = None
        repo.get_role_matches.return_value = [
            dict(champion_id=cid, champion_name=f"Champion{cid}", win=cid % 2,
                 kills=cid, deaths=1, assists=2, queue_id=420,
                 game_start=repo.cutoff_time - 1000)
            for cid in (1, 1, 2, 3)
        ]
        repo.get_other_participant_role_matches.return_value = [
            {"champion_id": cid} for cid in meta_ids
        ]
        return build_player_model(repo, 1, "TOP")

    def assert_distribution(self, model):
        self.assertAlmostEqual(math.fsum(c.p_final for c in model.champions.values()) + model.p_others, 1)
        self.assertGreater(model.p_others, 0)
        self.assertEqual(model.strength(model.champions), 1)
        self.assertGreater(model.residual_ratio(model.champions), 0)
        self.assertTrue(math.isfinite(model.residual_ratio(model.champions)))

    def test_off_support_meta_is_retained_without_seen_renormalization(self):
        model = self.build([1, 2, 99, 100])
        self.assertAlmostEqual(model.p_others, LAMBDA * 0.5)
        for champion in model.champions.values():
            self.assertEqual(champion.p_final, (1 - LAMBDA) * champion.p_personal + LAMBDA * champion.p_meta)
        self.assert_distribution(model)
        self.assertEqual(set(model.champions), {1, 2, 3})
        self.assertEqual(set(model.candidate_champions()), {1, 2, 3})

    def test_entire_meta_outside_seen_pool_still_contributes(self):
        model = self.build([99, 100])
        self.assertEqual(model.p_others, LAMBDA)
        self.assertFalse(any("no meta observations" in w for w in model.warnings))
        self.assert_distribution(model)

    def test_no_meta_and_unusable_meta_have_finite_safety_mass(self):
        for meta in ([], [None, None], [1, 2, 3]):
            with self.subTest(meta=meta):
                model = self.build(meta)
                self.assert_distribution(model)
                self.assertLess(model.p_others, 2 * OTHERS_EPSILON)

    def test_three_bans_exhaust_observed_pool_without_zero_strength(self):
        model = self.build([1, 2, 99, 100])
        bans = {1, 2, 3}
        self.assertTrue(model.observed_pool_exhausted(bans))
        self.assertFalse(model.observed_pool_exhausted({1, 2}))
        self.assertEqual(model.strength(bans), 1)
        # residual_ratio now folds in the Dependency penalty (r = r_perf * D);
        # exhausting the pool bans 100% of this player's personal mass, so D
        # here is exp(-ETA), not 1.
        self.assertAlmostEqual(model.residual_ratio(bans), (1 / model.strength()) * model.dependency(bans))
        for aggregate in (geometric_mean, harmonic_mean):
            value = aggregate((model.residual_ratio(bans), 1, 1, 1, 1), (0.2,) * 5)
            self.assertGreater(value, 0)
            self.assertTrue(math.isfinite(value))

    def test_partial_ban_redistributes_over_seen_and_others(self):
        champions = {
            cid: ChampionModel(cid, f"Champion{cid}", 1, 1, probability, 0, probability, 0.5, threat)
            for cid, probability, threat in ((1, .60, 2), (2, .25, 1.5), (3, .06, .5))
        }
        model = PlayerModel(1, "Fixture#TEST", "TOP", champions, .5, 1, p_others=.09)
        self.assertAlmostEqual(model.strength(), .6 * 2 + .25 * 1.5 + .06 * .5 + .09)
        self.assertAlmostEqual(model.strength({1}), (.25 * 1.5 + .06 * .5 + .09) / .4)

    def test_search_diagnostics_and_ui_never_treat_others_as_champion(self):
        # All three high-threat observed picks must be removed to reach neutral
        # strength. Other players have neutral, disjoint pools.
        first = self.build([99])
        first = replace(first, champions={cid: replace(c, threat=2) for cid, c in first.champions.items()})
        players = [first]
        for pid, role in enumerate(("JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"), 2):
            # p_personal=0: these filler players aren't the subject under
            # test, so they must stay Dependency-neutral (D=1 regardless of
            # bans) - otherwise the new Dependency term makes banning their
            # single, 100%-personal-reliance champion look artificially
            # attractive to the search, which isn't what this test is about.
            champion = ChampionModel(10 + pid, f"Champion{10 + pid}", 1, 1, 0, 0, .9, .5, 1)
            players.append(PlayerModel(pid, f"Player{pid}#TEST", role, {champion.champion_id: champion}, .5, 1, p_others=.1))
        with patch("ban_algorithm.build_player_model", side_effect=players):
            result = recommend_bans(Mock(), [(p.player_id, p.role) for p in players])
        self.assertEqual(set(result.best_by_rho[0].bans), {1, 2, 3})
        for search in result.best_by_rho.values():
            self.assertGreater(search.value, 0)
            self.assertIn(1, search.exhausted_player_ids)
            self.assertGreater(search.exhausted_combinations, 0)
        self.assertTrue(result.player_diagnostics[0].observed_pool_exhausted)
        self.assertEqual(result.player_diagnostics[0].strength, 1)
        observed = {cid for p in players for cid in p.champions}
        self.assertEqual(set(result.candidates), observed)
        self.assertTrue(all(b.champion_id in observed for b in (*result.recommended, *result.also_consider)))
        summary = render_summary_page(result).to_dict()
        detail = render_player_page(result, first, PlayerPresentation(), 0).to_dict()
        for page in (summary, detail):
            text = str(page)
            self.assertNotIn("Others", text)
            self.assertIn("미관측 챔피언 추정값", text)


if __name__ == "__main__":
    unittest.main()
