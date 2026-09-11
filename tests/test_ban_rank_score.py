"""Unranked SoloQ rank_score handling: missing rank means "unknown", not
"weak". Regressions for the team-neutral fallback in recommend_bans, as
opposed to the old behavior of silently scoring Unranked the same as IRON.
"""

import math
import sqlite3
import unittest

from scouting.ban_algorithm import (
    DEFAULT_NEUTRAL_RANK_SCORE, TIERS, build_player_model, recommend_bans,
    solo_rank_score,
)
from scouting import scouting_db as db
from scouting.scouting_repo import ScoutingRepo


DAY_MS = 86_400_000
NOW = 2_000_000_000_000
ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


class RankScoreFallbackTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.sequence = 0
        self.player_ids = {}
        for index, role in enumerate(ROLES):
            player_id = db.get_or_create_player(
                self.conn, f"puuid-{role}", f"Player{index}", "KR1", seen_at=NOW,
            )
            self.player_ids[role] = player_id
            self._add_role_game(player_id, role)

    def _add_role_game(self, player_id, role):
        # Distinct champion per role: a 3-ban exhaustive search needs at
        # least three distinct candidate champions across the whole team.
        champion_id = ROLES.index(role) + 1
        self.sequence += 1
        match_id = f"RANK_{self.sequence}"
        start = NOW - DAY_MS
        self.conn.execute(
            "INSERT INTO matches (match_id,game_start,game_end,queue_id,raw_file_path,fetched_at) VALUES (?,?,?,?,?,?)",
            (match_id, start, start + 1000, 420, "", NOW),
        )
        self.conn.execute(
            "INSERT INTO player_matches (player_id,match_id,champion_id,champion_name,canonical_role,win,kills,deaths,assists) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (player_id, match_id, champion_id, f"Champion{champion_id}", role, 1, 5, 2, 5),
        )
        self.conn.commit()

    def _set_rank(self, role, tier, lp=50):
        db.insert_rank_snapshot(
            self.conn, self.player_ids[role], "RANKED_SOLO_5x5", tier, "II", lp, 40, 30, NOW,
        )
        self.conn.commit()

    def _opponents(self):
        return [(self.player_ids[role], role) for role in ROLES]

    def _recommend(self):
        with ScoutingRepo(cutoff_time=NOW, conn=self.conn) as repo:
            return recommend_bans(repo, self._opponents())

    def _model(self, role):
        with ScoutingRepo(cutoff_time=NOW, conn=self.conn) as repo:
            return build_player_model(repo, self.player_ids[role], role)

    def test_four_ranked_one_unranked_uses_ranked_average(self):
        tiers = {"TOP": "GOLD", "JUNGLE": "PLATINUM", "MIDDLE": "DIAMOND", "BOTTOM": "SILVER"}
        for role, tier in tiers.items():
            self._set_rank(role, tier)
        # UTILITY is left with no rank snapshot at all: Unranked.
        result = self._recommend()
        by_role = {p.role: p for p in result.players}
        ranked_scores = [solo_rank_score(tier, 50) for tier in tiers.values()]
        expected = math.fsum(ranked_scores) / len(ranked_scores)
        self.assertFalse(by_role["UTILITY"].is_ranked)
        self.assertAlmostEqual(by_role["UTILITY"].rank_score, expected)
        for role, tier in tiers.items():
            self.assertTrue(by_role[role].is_ranked)
            self.assertAlmostEqual(by_role[role].rank_score, solo_rank_score(tier, 50))

    def test_three_ranked_two_unranked_share_the_same_average(self):
        tiers = {"TOP": "EMERALD", "JUNGLE": "IRON", "MIDDLE": "CHALLENGER"}
        for role, tier in tiers.items():
            self._set_rank(role, tier)
        # BOTTOM and UTILITY stay Unranked.
        result = self._recommend()
        by_role = {p.role: p for p in result.players}
        ranked_scores = [solo_rank_score(tier, 50) for tier in tiers.values()]
        expected = math.fsum(ranked_scores) / len(ranked_scores)
        for role in ("BOTTOM", "UTILITY"):
            self.assertFalse(by_role[role].is_ranked)
            self.assertAlmostEqual(by_role[role].rank_score, expected)
        self.assertAlmostEqual(by_role["BOTTOM"].rank_score, by_role["UTILITY"].rank_score)

    def test_all_five_unranked_use_the_fixed_neutral_constant(self):
        # No _set_rank calls at all: every opponent is Unranked.
        result = self._recommend()
        for player in result.players:
            self.assertFalse(player.is_ranked)
            self.assertAlmostEqual(player.rank_score, DEFAULT_NEUTRAL_RANK_SCORE)
        # Sanity: the fixed fallback sits mid-scale, not at IRON's floor.
        self.assertAlmostEqual(DEFAULT_NEUTRAL_RANK_SCORE, (TIERS.index("IRON") + 1 + TIERS.index("CHALLENGER") + 1) / 2)

    def test_unranked_is_no_longer_scored_like_iron(self):
        # All four opponents are high-tier; if Unranked still silently fell
        # back to IRON's raw score of 1.0, this would assert a false negative.
        for role in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM"):
            self._set_rank(role, "DIAMOND")
        result = self._recommend()
        unranked = next(p for p in result.players if p.role == "UTILITY")
        self.assertFalse(unranked.is_ranked)
        iron_score = solo_rank_score("IRON", 0)
        self.assertNotAlmostEqual(unranked.rank_score, iron_score)
        self.assertAlmostEqual(unranked.rank_score, solo_rank_score("DIAMOND", 50))

    def test_player_weight_normalization_still_sums_to_one_and_is_proportional(self):
        tiers = {"TOP": "IRON", "JUNGLE": "GOLD", "MIDDLE": "DIAMOND", "BOTTOM": "CHALLENGER"}
        for role, tier in tiers.items():
            self._set_rank(role, tier)
        result = self._recommend()
        self.assertAlmostEqual(math.fsum(result.weights), 1.0)
        by_role = {p.role: (p, w) for p, w in zip(result.players, result.weights)}
        total_rank = math.fsum(p.rank_score for p in result.players)
        for role in ROLES:
            player, weight = by_role[role]
            self.assertAlmostEqual(weight, player.rank_score / total_rank)
        # Higher raw rank_score must still translate into higher relative weight.
        self.assertLess(by_role["TOP"][1], by_role["JUNGLE"][1])
        self.assertLess(by_role["JUNGLE"][1], by_role["BOTTOM"][1])

    def test_build_player_model_alone_leaves_unranked_score_as_placeholder(self):
        # build_player_model has no view of the other four opponents, so it
        # cannot itself resolve the team-neutral value; is_ranked=False is
        # the signal recommend_bans uses to fill it in afterward.
        model = self._model("UTILITY")
        self.assertFalse(model.is_ranked)
        self.assertTrue(any("no recognized solo-queue tier" in note for note in model.warnings))


if __name__ == "__main__":
    unittest.main()
