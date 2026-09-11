"""UI-only PlayerThreat (player_threat_scores) regressions.

PlayerThreat is a display-only metric, separate from the optimizer's own
player weight (Recommendation.weights, the rank_score share ban optimization
actually uses). These tests check the formula itself and that computing it
never changes weights or recommended bans.
"""

import math
import sqlite3
import unittest

from ban_algorithm import (
    K, PLAYER_THREAT_FORM_EXPONENT, PLAYER_THREAT_POOL_EXPONENT,
    PLAYER_THREAT_RANK_EXPONENT, PLAYER_THREAT_WR_PRIOR, player_threat_scores,
    recommend_bans,
)
import scouting_db as db
from scouting_repo import ScoutingRepo


DAY_MS = 86_400_000
NOW = 2_000_000_000_000
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def _expected_player_threat_scores(players):
    mean_rank_score = math.fsum(p.rank_score for p in players) / len(players)
    raw = {}
    for p in players:
        role_games = sum(c.games for c in p.champions.values())
        role_wins = sum(c.wins for c in p.champions.values())
        wr_adj = (role_wins + K * PLAYER_THREAT_WR_PRIOR) / (role_games + K)
        form_factor = wr_adj / PLAYER_THREAT_WR_PRIOR
        rank_factor = p.rank_score / mean_rank_score
        raw[p.player_id] = (
            rank_factor ** PLAYER_THREAT_RANK_EXPONENT
            * form_factor ** PLAYER_THREAT_FORM_EXPONENT
            * p.strength() ** PLAYER_THREAT_POOL_EXPONENT
        )
    mean_raw = math.fsum(raw.values()) / len(raw)
    return {player_id: value / mean_raw for player_id, value in raw.items()}


class PlayerThreatTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.sequence = 0
        self.player_ids = {
            role: db.get_or_create_player(self.conn, f"puuid-{role}", f"Player{index}", "KR1", seen_at=NOW)
            for index, role in enumerate(ROLES)
        }

    def _add_game(self, role, champion_id, win, *, kills=5, deaths=2, assists=5):
        self.sequence += 1
        match_id = f"THREAT_{self.sequence}"
        start = NOW - DAY_MS
        self.conn.execute(
            "INSERT INTO matches (match_id,game_start,game_end,queue_id,raw_file_path,fetched_at) VALUES (?,?,?,?,?,?)",
            (match_id, start, start + 1000, 420, "", NOW),
        )
        self.conn.execute(
            "INSERT INTO player_matches (player_id,match_id,champion_id,champion_name,canonical_role,win,kills,deaths,assists) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self.player_ids[role], match_id, champion_id, f"Champion{champion_id}", role, win, kills, deaths, assists),
        )
        self.conn.commit()

    def _set_rank(self, role, tier, lp=50):
        db.insert_rank_snapshot(self.conn, self.player_ids[role], "RANKED_SOLO_5x5", tier, "II", lp, 40, 30, NOW)
        self.conn.commit()

    def _recommend(self):
        with ScoutingRepo(cutoff_time=NOW, conn=self.conn) as repo:
            return recommend_bans(repo, [(self.player_ids[role], role) for role in ROLES])

    def test_formula_matches_hand_computation_with_one_standout_player(self):
        for role in ROLES:
            if role != "UTILITY":
                self._set_rank(role, "GOLD")
            champion_id = ROLES.index(role) + 1
            for _ in range(9):
                self._add_game(role, champion_id, win=1)
            self._add_game(role, champion_id, win=0)
        # UTILITY stands out: much higher rank, better recent form, and a
        # more dangerous (higher-threat, via KDA) current pool.
        self._set_rank("UTILITY", "CHALLENGER")
        for _ in range(10):
            self._add_game("UTILITY", 100, win=1, kills=20, deaths=1, assists=20)

        result = self._recommend()
        expected = _expected_player_threat_scores(result.players)
        actual = player_threat_scores(result)
        self.assertEqual(actual.keys(), expected.keys())
        for player_id in actual:
            with self.subTest(player_id=player_id):
                self.assertAlmostEqual(actual[player_id], expected[player_id])
        self.assertGreater(
            actual[self.player_ids["UTILITY"]],
            max(value for pid, value in actual.items() if pid != self.player_ids["UTILITY"]),
        )

    def test_normalizes_to_a_team_mean_of_one(self):
        for role in ROLES:
            self._set_rank(role, "SILVER")
            self._add_game(role, ROLES.index(role) + 1, win=1)
        result = self._recommend()
        scores = player_threat_scores(result)
        self.assertAlmostEqual(math.fsum(scores.values()) / len(scores), 1.0)

    def test_does_not_change_optimizer_weights_or_recommended_bans(self):
        for role in ROLES:
            self._set_rank(role, "PLATINUM")
            self._add_game(role, ROLES.index(role) + 1, win=1)
        result = self._recommend()
        before_weights = result.weights
        before_recommended = result.recommended
        before_also_consider = result.also_consider
        player_threat_scores(result)
        player_threat_scores(result)
        self.assertEqual(result.weights, before_weights)
        self.assertEqual(result.recommended, before_recommended)
        self.assertEqual(result.also_consider, before_also_consider)

    def test_rank_dominates_over_a_much_stronger_form_and_pool(self):
        # Rank is the dominant factor by design (exponent 0.6 vs 0.2/0.2):
        # UTILITY has the lowest rank_score here but a far better recent-form
        # and current-pool profile than the other four, yet its PlayerThreat
        # must still stay below theirs - form/pool can nudge the ranking but
        # not overturn a real rank gap.
        for role in ROLES:
            if role == "UTILITY":
                continue
            self._set_rank(role, "BRONZE")
            champion_id = ROLES.index(role) + 1
            for _ in range(9):
                self._add_game(role, champion_id, win=1)
            self._add_game(role, champion_id, win=0)
        self._set_rank("UTILITY", "IRON")
        for _ in range(15):
            self._add_game("UTILITY", 100, win=1, kills=40, deaths=1, assists=40)
        for _ in range(5):
            self._add_game("UTILITY", 101, win=0, kills=0, deaths=8, assists=0)

        result = self._recommend()
        by_role_weight = {p.role: w for p, w in zip(result.players, result.weights)}
        self.assertEqual(min(by_role_weight, key=by_role_weight.get), "UTILITY")
        scores = player_threat_scores(result)
        by_role_threat = {p.role: scores[p.player_id] for p in result.players}
        self.assertLess(by_role_threat["UTILITY"], min(
            value for role, value in by_role_threat.items() if role != "UTILITY"
        ))


if __name__ == "__main__":
    unittest.main()
