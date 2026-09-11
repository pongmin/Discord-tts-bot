"""Hand-computed KDA threat regressions using the real cutoff-filtered repo."""

import math
import sqlite3
import unittest

from scouting.ban_algorithm import (
    ALPHA, BETA, K, KDA_EPSILON, build_player_model,
    arithmetic_mean, geometric_mean, harmonic_mean,
)
from scouting import scouting_db as db
from scouting.scouting_repo import ScoutingRepo


DAY_MS = 86_400_000
NOW = 2_000_000_000_000


class KdaThreatTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO players (id, puuid, game_name, tag_line) VALUES (1, 'fixture', 'Kda', 'TEST')")
        self.conn.execute("INSERT INTO players (id, puuid) VALUES (2, 'other')")
        self.sequence = 0

    def add_game(self, champion_id, kills=0, deaths=0, assists=0, win=1,
                 *, days_ago=1, queue=420, role="BOTTOM", player_id=1):
        self.sequence += 1
        match_id = f"KDA_{self.sequence}"
        start = NOW - days_ago * DAY_MS
        self.conn.execute(
            "INSERT INTO matches (match_id,game_start,game_end,queue_id,raw_file_path,fetched_at) VALUES (?,?,?,?,?,?)",
            (match_id, start, start + 1000, queue, "", NOW),
        )
        self.conn.execute(
            "INSERT INTO player_matches (player_id,match_id,champion_id,champion_name,canonical_role,win,kills,deaths,assists) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (player_id, match_id, champion_id, f"Champion{champion_id}", role, win, kills, deaths, assists),
        )
        self.conn.commit()

    def model(self):
        with ScoutingRepo(cutoff_time=NOW, conn=self.conn) as repo:
            return build_player_model(repo, 1, "BOTTOM")

    def assert_finite_model(self, model):
        self.assertTrue(math.isfinite(model.kda_baseline))
        self.assertEqual(model.residual_ratio(), 1.0)
        for cid, champion in model.champions.items():
            with self.subTest(champion=cid):
                for value in (champion.kda_champ, champion.kda_adj, champion.threat,
                              model.strength({cid}), model.residual_ratio({cid})):
                    self.assertTrue(math.isfinite(value), value)
                self.assertGreater(champion.threat, 0)
                remaining = model.residual_ratio({cid})
                for aggregate in (arithmetic_mean, geometric_mean, harmonic_mean):
                    self.assertTrue(math.isfinite(aggregate((remaining, 1, 1, 1, 1), (0.2,) * 5)))

    def test_raw_totals_and_single_game_shrinkage(self):
        self.assertEqual((ALPHA, BETA, K, KDA_EPSILON), (0.4, 4, 8, 1e-6))
        self.add_game(1, 10, 2, 4, 1, days_ago=1)
        self.add_game(1, 0, 10, 1, 0, days_ago=60)
        self.add_game(2, 4, 0, 6, 1, days_ago=90, queue=400)
        model = self.model()
        self.assertAlmostEqual(model.baseline_winrate, 2 / 3)
        self.assertAlmostEqual(model.kda_baseline, 25 / 12)
        for cid, games, raw, adjusted, adjusted_wr in (
            (1, 2, 15 / 12, 23 / 12, (1 + 8 * 2 / 3) / 10),
            (2, 1, 10, 80 / 27, (1 + 8 * 2 / 3) / 9),
        ):
            champion = model.champions[cid]
            self.assertEqual(champion.games, games)
            self.assertAlmostEqual(champion.kda_champ, raw)
            self.assertAlmostEqual(champion.kda_adj, adjusted)
            self.assertAlmostEqual(champion.wr_adj, adjusted_wr)
            expected = math.exp(4 * (adjusted_wr - 2 / 3) + 0.4 * math.log(adjusted / (25 / 12)))
            self.assertAlmostEqual(champion.threat, expected)
        # KDA is a ratio of totals, not a mean of each game's KDA.
        self.assertNotAlmostEqual(model.champions[1].kda_champ, (7 + 0.1) / 2)
        weighted_a = math.exp(-1 / 30) + math.exp(-60 / 30)
        weighted_b = 0.3 * math.exp(-90 / 30)
        self.assertAlmostEqual(model.champions[1].p_final, (1 - model.p_others) * weighted_a / (weighted_a + weighted_b))
        self.assert_finite_model(model)

    def test_date_and_queue_weights_change_only_picks(self):
        self.add_game(1, 12, 3, 3, 1, days_ago=1)
        self.add_game(2, 0, 8, 2, 0, days_ago=150, queue=400)
        before = self.model()
        self.conn.execute("UPDATE matches SET queue_id=420, game_start=?, game_end=?", (NOW - DAY_MS, NOW - DAY_MS + 1000))
        after = self.model()
        self.assertNotAlmostEqual(before.champions[1].p_final, after.champions[1].p_final)
        self.assertEqual(before.kda_baseline, after.kda_baseline)
        self.assertEqual(before.baseline_winrate, after.baseline_winrate)
        for cid in before.champions:
            for attr in ("games", "wins", "wr_adj", "kda_champ", "kda_adj", "threat"):
                self.assertEqual(getattr(before.champions[cid], attr), getattr(after.champions[cid], attr))

    def test_role_queue_cutoff_and_player_filtering(self):
        self.add_game(1, 2, 2, 4)
        before = self.model()
        for options in ({"role": "TOP"}, {"queue": 490}, {"queue": 700},
                        {"days_ago": -1}, {"player_id": 2}):
            self.add_game(99, 100, 100, 100, **options)
        self.assertEqual(self.model(), before)

    def test_zero_kda_preserves_wr_threat(self):
        self.add_game(1, 0, 3, 0, 1)
        self.add_game(2, 0, 0, 0, 0)
        model = self.model()
        self.assertEqual(model.kda_baseline, 0)
        for champion in model.champions.values():
            self.assertEqual((champion.kda_champ, champion.kda_adj), (0, 0))
            self.assertEqual(champion.threat, math.exp(4 * (champion.wr_adj - model.baseline_winrate)))
        self.assert_finite_model(model)

    def test_zero_deaths_uses_total_takedowns_over_one(self):
        self.add_game(1, 4, 0, 2)
        self.add_game(2, 1, 0, 2)
        model = self.model()
        self.assertEqual(model.kda_baseline, 9)
        self.assertEqual(model.champions[1].kda_champ, 6)
        self.assertAlmostEqual(model.champions[1].kda_adj, (6 + 8 * 9) / 9)
        self.assert_finite_model(model)

    def test_tiny_baseline_and_tiny_adjusted_kda_are_both_clamped(self):
        self.add_game(1, 1, 10**15, 0, 1)
        self.add_game(2, 0, 10**15, 0, 0)
        model = self.model()
        self.assertAlmostEqual(model.kda_baseline, 5e-16, delta=1e-30)
        for champion in model.champions.values():
            self.assertLess(champion.kda_adj, KDA_EPSILON)
            self.assertEqual(champion.threat, math.exp(4 * (champion.wr_adj - model.baseline_winrate)))
        self.assert_finite_model(model)

    def test_tiny_baseline_with_positive_champion_kda_is_finite(self):
        self.add_game(1, 1, 0, 0)
        self.add_game(2, 0, 10**15, 0)
        model = self.model()
        self.assertLess(model.kda_baseline, KDA_EPSILON)
        champion = model.champions[1]
        self.assertGreater(champion.kda_adj, KDA_EPSILON)
        self.assertAlmostEqual(champion.threat, math.exp(0.4 * math.log(champion.kda_adj / 1e-6)))
        self.assert_finite_model(model)

    def test_self_inclusive_one_champion_baseline_is_neutral(self):
        self.add_game(1, 5, 0, 10)
        model = self.model()
        champion = model.champions[1]
        self.assertEqual((model.kda_baseline, champion.kda_champ, champion.kda_adj), (15, 15, 15))
        self.assertEqual(champion.threat, 1.0)
        self.assert_finite_model(model)

    def test_nullable_legacy_counts_are_zero_without_nan(self):
        self.add_game(1, None, None, None)
        model = self.model()
        self.assertEqual(model.kda_baseline, 0)
        self.assertEqual(model.champions[1].threat, 1.0)
        self.assert_finite_model(model)


if __name__ == "__main__":
    unittest.main()
