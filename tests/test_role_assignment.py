"""Role-fit and assignment regressions against the real cutoff-filtered repo."""

import math
from itertools import permutations
import sqlite3
import unittest
from unittest.mock import patch

from riot.riot_api import CLASH_QUEUE_ID, RANKED_SOLO_QUEUE_TYPE
from scouting import ban_algorithm
from scouting.ban_algorithm import (
    DEFAULT_NEUTRAL_RANK_SCORE, build_player_model, solo_rank_score,
)
from scouting import scouting_db as db
from scouting.role_assignment import (
    BREADTH_REFERENCE_POOL, DEFAULT_ROLE_PROBABILITY, MIN_ROLE_FIT,
    NEUTRAL_WINRATE, ROLE_BREADTH_EXPONENT, ROLE_ORDER, ROLE_SHARE_EXPONENT,
    ROLE_WR_PRIOR_GAMES, ROLE_WR_SENSITIVITY, assign_roles, breadth_score,
    build_role_candidate_model, effective_pool_size, global_baseline_winrate,
    player_role_fits, player_strengths,
)
from scouting.scouting_repo import ScoutingRepo


DAY_MS = 86_400_000
NOW = 2_000_000_000_000


class RoleFixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.sequence = 0

    def player(self, player_id: int, name: str = "Role") -> int:
        self.conn.execute(
            "INSERT INTO players (id, puuid, game_name, tag_line) VALUES (?,?,?,?)",
            (player_id, f"puuid-{player_id}", f"{name}{player_id}", "TEST"),
        )
        return player_id

    def add_games(self, player_id, role, games, wins, *, queue=420, champion_id=None,
                  days_ago=1):
        """`wins` of `games` in `role`, spread over two champions by default."""
        for index in range(games):
            self.sequence += 1
            match_id = f"RA_{self.sequence}"
            start = NOW - days_ago * DAY_MS
            self.conn.execute(
                "INSERT INTO matches (match_id,game_start,game_end,queue_id,raw_file_path,fetched_at)"
                " VALUES (?,?,?,?,?,?)",
                (match_id, start, start + 1000, queue, "", NOW),
            )
            champion = champion_id if champion_id is not None else 100 + index % 2
            self.conn.execute(
                "INSERT INTO player_matches (player_id,match_id,champion_id,champion_name,"
                "canonical_role,win,kills,deaths,assists) VALUES (?,?,?,?,?,?,?,?,?)",
                (player_id, match_id, champion, f"Champion{champion}", role,
                 1 if index < wins else 0, 5, 4, 6),
            )
        self.conn.commit()

    def repo(self) -> ScoutingRepo:
        repo = ScoutingRepo(cutoff_time=NOW, conn=self.conn)
        self.addCleanup(repo.close)
        return repo


class RoleCandidateModelTests(RoleFixture):
    def test_a_played_role_is_the_strict_model_verbatim(self):
        self.player(1)
        self.add_games(1, "MIDDLE", 20, 12)
        repo = self.repo()

        strict = build_player_model(repo, 1, "MIDDLE")
        candidate = build_role_candidate_model(repo, 1, "MIDDLE")

        # Same Others/P_final/Threat/KDA numbers, not a re-derivation of them.
        self.assertTrue(candidate.observed)
        self.assertEqual(candidate.model, strict)
        self.assertEqual(candidate.strength(frozenset()), strict.strength(frozenset()))
        self.assertEqual(candidate.games_in_role, 20)
        self.assertEqual(candidate.wins_in_role, 12)

    def test_an_unplayed_role_is_neutral_instead_of_raising(self):
        self.player(1)
        self.add_games(1, "MIDDLE", 20, 12)
        repo = self.repo()

        # The strict model still refuses it; only the tolerant one does not.
        with self.assertRaises(ValueError):
            build_player_model(repo, 1, "UTILITY")

        candidate = build_role_candidate_model(repo, 1, "UTILITY")
        self.assertFalse(candidate.observed)
        self.assertEqual((candidate.games_in_role, candidate.wins_in_role), (0, 0))
        self.assertEqual(candidate.model.champions, {})
        # Others-only: the neutral unseen-champion prior, never zero strength.
        self.assertEqual(candidate.strength(frozenset()), 1.0)
        self.assertEqual(candidate.model.baseline_winrate, global_baseline_winrate(repo, 1))

    def test_unknown_player_and_role_still_raise(self):
        self.player(1)
        self.add_games(1, "TOP", 3, 2)
        repo = self.repo()
        with self.assertRaises(ValueError):
            build_role_candidate_model(repo, 999, "TOP")
        with self.assertRaises(ValueError):
            build_role_candidate_model(repo, 1, "FILL")
        with ScoutingRepo(conn=self.conn) as uncapped:
            with self.assertRaises(ValueError):
                build_role_candidate_model(uncapped, 1, "TOP")


class GlobalBaselineTests(RoleFixture):
    def test_all_roles_and_both_queues_count_once(self):
        self.player(1)
        self.add_games(1, "TOP", 10, 8)
        self.add_games(1, "UTILITY", 10, 2, queue=400)
        # Clash is validation ground truth, never part of the model's pool.
        self.add_games(1, "MIDDLE", 10, 10, queue=CLASH_QUEUE_ID)
        repo = self.repo()

        self.assertAlmostEqual(global_baseline_winrate(repo, 1), 0.5)

    def test_no_collected_games_falls_back_to_neutral(self):
        self.player(1)
        self.assertEqual(global_baseline_winrate(self.repo(), 1), NEUTRAL_WINRATE)


class RoleFitTests(RoleFixture):
    def test_factors_match_the_specified_formulas(self):
        self.player(1)
        self.add_games(1, "TOP", 40, 28)
        self.add_games(1, "JUNGLE", 10, 3)
        repo = self.repo()
        baseline = global_baseline_winrate(repo, 1)
        fits = player_role_fits(repo, 1)

        self.assertAlmostEqual(fits["TOP"].role_probability, 40 / 50)
        self.assertAlmostEqual(fits["JUNGLE"].role_probability, 10 / 50)
        # F is normalized by the player's own main role, so it peaks at 1.
        self.assertAlmostEqual(fits["TOP"].share, 1.0)
        self.assertAlmostEqual(fits["JUNGLE"].share, 0.25)

        for role, games, wins in (("TOP", 40, 28), ("JUNGLE", 10, 3)):
            with self.subTest(role=role):
                fit = fits[role]
                expected_wr = (
                    (wins + ROLE_WR_PRIOR_GAMES * baseline) / (games + ROLE_WR_PRIOR_GAMES)
                )
                self.assertAlmostEqual(fit.winrate_adjusted, expected_wr)
                self.assertAlmostEqual(
                    fit.winrate_ratio,
                    math.exp(ROLE_WR_SENSITIVITY * (expected_wr - baseline)),
                )
                self.assertAlmostEqual(
                    fit.strength, build_player_model(repo, 1, role).strength(frozenset())
                )
                self.assertAlmostEqual(
                    fit.fit,
                    (fit.share ** ROLE_SHARE_EXPONENT) * fit.winrate_ratio * fit.strength
                    * (fit.breadth ** ROLE_BREADTH_EXPONENT),
                )

        # Above-baseline role lifts R, below-baseline role cuts it.
        self.assertGreater(fits["TOP"].winrate_ratio, 1.0)
        self.assertLess(fits["JUNGLE"].winrate_ratio, 1.0)

        # F enters E linearly, as the strongest term.
        self.assertEqual(ROLE_SHARE_EXPONENT, 1.0)
        self.assertAlmostEqual(fits["JUNGLE"].share ** ROLE_SHARE_EXPONENT, fits["JUNGLE"].share)


class BreadthTests(RoleFixture):
    def test_effective_pool_is_the_exponential_of_entropy(self):
        self.player(1)
        # Four champions, evenly played, all on the same day so the date weights
        # cannot skew the personal distribution.
        for champion in (101, 102, 103, 104):
            self.add_games(1, "TOP", 8, 4, champion_id=champion)
        repo = self.repo()

        model = build_role_candidate_model(repo, 1, "TOP").model
        self.assertAlmostEqual(effective_pool_size(model), 4.0, places=6)
        self.assertAlmostEqual(
            effective_pool_size(model),
            math.exp(-math.fsum(
                champion.p_personal * math.log(champion.p_personal)
                for champion in model.champions.values()
            )),
        )

    def test_a_one_trick_has_an_effective_pool_of_one(self):
        self.player(1)
        self.add_games(1, "MIDDLE", 30, 15, champion_id=99)
        model = build_role_candidate_model(self.repo(), 1, "MIDDLE").model

        self.assertEqual(len(model.champions), 1)
        self.assertAlmostEqual(effective_pool_size(model), 1.0)

    def test_others_is_not_counted_as_breadth(self):
        self.player(1)
        self.add_games(1, "BOTTOM", 20, 10, champion_id=42)
        model = build_role_candidate_model(self.repo(), 1, "BOTTOM").model

        # The unseen-champion prior carries real mass, and breadth ignores it:
        # a one-trick stays a one-trick.
        self.assertGreater(model.p_others, 0)
        self.assertAlmostEqual(effective_pool_size(model), 1.0)
        # An unplayed role is all Others and therefore has no breadth at all.
        unplayed = build_role_candidate_model(self.repo(), 1, "TOP").model
        self.assertEqual(unplayed.p_others, 1.0)
        self.assertEqual(effective_pool_size(unplayed), 0.0)

    def test_breadth_is_an_absolute_saturating_score(self):
        reference = math.log1p(BREADTH_REFERENCE_POOL)
        for pool in (0.0, 1.0, 3.0, 5.0, 9.0):
            with self.subTest(effective_pool=pool):
                self.assertAlmostEqual(
                    breadth_score(pool), min(1.0, math.log1p(pool) / reference)
                )
        # The shape the constants are meant to produce: a one-trick clearly
        # below full credit, ~3 champions moderate, N_REF and above saturated.
        self.assertLess(breadth_score(1.0), 0.5)
        self.assertTrue(0.7 < breadth_score(3.0) < 0.85)
        self.assertEqual(breadth_score(BREADTH_REFERENCE_POOL), 1.0)
        self.assertEqual(breadth_score(50.0), 1.0)
        self.assertEqual(breadth_score(0.0), 0.0)
        # Monotone in between, and never above the cap.
        scores = [breadth_score(pool) for pool in (0.5, 1, 2, 3, 4, 5, 6, 20)]
        self.assertEqual(scores, sorted(scores))
        self.assertLessEqual(max(scores), 1.0)

    def test_a_role_is_scored_on_its_own_pool_not_the_players_best_role(self):
        self.player(1)
        for champion in (101, 102, 103, 104):
            self.add_games(1, "TOP", 10, 5, champion_id=champion)
        for champion in (201, 202):
            self.add_games(1, "JUNGLE", 10, 5, champion_id=champion)
        fits = player_role_fits(self.repo(), 1)

        self.assertAlmostEqual(fits["TOP"].effective_pool, 4.0, places=6)
        self.assertAlmostEqual(fits["JUNGLE"].effective_pool, 2.0, places=6)
        # The broadest role no longer normalizes to exactly 1.0 - four effective
        # champions is genuinely short of the N_REF = 5 reference.
        self.assertAlmostEqual(fits["TOP"].breadth, breadth_score(4.0))
        self.assertLess(fits["TOP"].breadth, 1.0)
        self.assertAlmostEqual(fits["JUNGLE"].breadth, breadth_score(2.0))
        self.assertEqual(fits["MIDDLE"].breadth, 0.0)

    def test_a_broad_pool_outscores_an_identical_narrow_one(self):
        """Same role, same games, same win rate - only the pool differs.

        This is what the absolute scale buys: both players are TOP-only, so a
        per-player normalization would have tied them at B = 1.0.
        """
        self.player(1, name="Broad")
        self.player(2, name="Narrow")
        for champion in (101, 102, 103, 104):
            self.add_games(1, "TOP", 10, 5, champion_id=champion)
        self.add_games(2, "TOP", 40, 20, champion_id=101)
        repo = self.repo()

        broad = player_role_fits(repo, 1)["TOP"]
        narrow = player_role_fits(repo, 2)["TOP"]

        self.assertEqual((broad.games, broad.wins), (narrow.games, narrow.wins))
        self.assertAlmostEqual(broad.share, narrow.share)
        self.assertAlmostEqual(broad.winrate_ratio, narrow.winrate_ratio)
        self.assertAlmostEqual(narrow.effective_pool, 1.0)
        self.assertAlmostEqual(broad.effective_pool, 4.0, places=6)
        self.assertGreater(broad.breadth, narrow.breadth)
        self.assertGreater(broad.fit, narrow.fit)
        # Secondary, not dominant: breadth alone must not swing E by more than
        # the role share it is correcting.
        self.assertLess(broad.fit / narrow.fit, 2.0)

    def test_no_observed_champions_anywhere_scores_zero_breadth(self):
        self.player(1)
        fits = player_role_fits(self.repo(), 1)

        for role in ROLE_ORDER:
            with self.subTest(role=role):
                self.assertEqual(fits[role].effective_pool, 0.0)
                self.assertEqual(fits[role].breadth, 0.0)
                # Equally unsupported everywhere, so this player contributes the
                # same constant to all 120 permutations and cannot skew them.
                self.assertEqual(fits[role].fit, MIN_ROLE_FIT)

    def test_unplayed_roles_are_floored_not_zero(self):
        self.player(1)
        self.add_games(1, "TOP", 10, 5)
        fits = player_role_fits(self.repo(), 1)

        for role in ("JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"):
            with self.subTest(role=role):
                self.assertEqual(fits[role].share, 0.0)
                self.assertEqual(fits[role].fit, MIN_ROLE_FIT)
                self.assertFalse(fits[role].observed)
                # Still finite under log, which is what the assignment needs.
                self.assertTrue(math.isfinite(math.log(fits[role].fit)))

    def test_a_player_with_no_games_is_equally_unsupported_everywhere(self):
        self.player(1)
        fits = player_role_fits(self.repo(), 1)

        for role in ROLE_ORDER:
            with self.subTest(role=role):
                self.assertEqual(fits[role].role_probability, DEFAULT_ROLE_PROBABILITY)
                self.assertAlmostEqual(fits[role].share, 1.0)
                # No observed champions either, so breadth floors E. Still the
                # same value for every role, which is what "equally" means here.
                self.assertEqual(fits[role].fit, MIN_ROLE_FIT)

    def test_rank_does_not_move_role_fit(self):
        """solo_rank_score is constant across roles, so it must not be in E."""
        self.player(1)
        self.add_games(1, "TOP", 20, 12)
        self.add_games(1, "MIDDLE", 8, 4)
        before = {role: fit.fit for role, fit in player_role_fits(self.repo(), 1).items()}

        db.insert_rank_snapshot(
            self.conn, 1, RANKED_SOLO_QUEUE_TYPE, "CHALLENGER", "I", 1200, 300, 100, NOW - 1000
        )
        self.conn.commit()
        after = {role: fit.fit for role, fit in player_role_fits(self.repo(), 1).items()}

        self.assertEqual(before, after)


class AssignmentTests(RoleFixture):
    def build_team(self, off_role_games=6):
        """Five one-role mains, each with a little history in the next role."""
        for index, role in enumerate(ROLE_ORDER, start=1):
            self.player(index)
            self.add_games(index, role, 40 + index, 22 + index)
            self.add_games(index, ROLE_ORDER[index % len(ROLE_ORDER)], off_role_games, 2)
        return list(range(1, 6))

    def test_best_assignment_gives_everyone_their_main(self):
        players = self.build_team()
        result = assign_roles(self.repo(), players)

        self.assertEqual(
            [(fit.role, fit.player_id) for fit in result.best.fits],
            list(zip(ROLE_ORDER, players)),
        )
        self.assertEqual(len(result.matrix), len(players) * len(ROLE_ORDER))
        self.assertGreater(result.margin, 0)

    def test_score_is_the_weighted_sum_and_the_best_of_all_120(self):
        players = self.build_team()
        result = assign_roles(self.repo(), players)

        for assignment in (result.best, result.runner_up):
            with self.subTest(score=assignment.score):
                self.assertAlmostEqual(
                    assignment.score,
                    math.fsum(
                        result.strengths[fit.player_id] * fit.fit
                        for fit in assignment.fits
                    ),
                )
        every = sorted(
            (
                math.fsum(
                    result.strengths[player_id] * result.matrix[(player_id, role)].fit
                    for player_id, role in zip(order, ROLE_ORDER)
                )
                for order in permutations(players)
            ),
            reverse=True,
        )
        self.assertEqual(len(every), 120)
        self.assertAlmostEqual(result.best.score, every[0])
        self.assertAlmostEqual(result.runner_up.score, every[1])
        self.assertGreaterEqual(result.best.score, result.runner_up.score)
        self.assertAlmostEqual(result.margin, result.best.score - result.runner_up.score)

    def rank(self, player_id: int, tier: str, lp: int = 0):
        db.insert_rank_snapshot(
            self.conn, player_id, RANKED_SOLO_QUEUE_TYPE, tier, "I", lp,
            300, 100, NOW - 1000,
        )
        self.conn.commit()

    def test_strength_is_the_ban_models_own_rank_score(self):
        players = self.build_team()
        self.rank(1, "CHALLENGER", 500)
        self.rank(2, "IRON")

        strengths = player_strengths(self.repo(), players)

        self.assertAlmostEqual(strengths[1], solo_rank_score("CHALLENGER", 500))
        self.assertAlmostEqual(strengths[2], solo_rank_score("IRON", 0))
        # Unranked is unknown, not weak: the mean of this group's ranked
        # players, exactly the fallback recommend_bans applies.
        expected = (strengths[1] + strengths[2]) / 2
        for player_id in (3, 4, 5):
            with self.subTest(player=player_id):
                self.assertAlmostEqual(strengths[player_id], expected)

    def test_no_ranked_player_anywhere_falls_back_to_the_fixed_midpoint(self):
        players = self.build_team()
        strengths = player_strengths(self.repo(), players)

        self.assertEqual(
            set(strengths.values()), {DEFAULT_NEUTRAL_RANK_SCORE}
        )

    def test_strength_moves_the_assignment_without_touching_role_fit(self):
        """The whole point of W: a contested role goes to the stronger player."""
        # Two players want MIDDLE, one a little better suited to it; the other
        # three are unambiguous mains, so only the MIDDLE/TOP pair is in play.
        for index, role in enumerate(("JUNGLE", "BOTTOM", "UTILITY"), start=3):
            self.player(index)
            self.add_games(index, role, 60, 33)
        self.player(1)
        self.add_games(1, "MIDDLE", 40, 22)
        self.add_games(1, "TOP", 30, 16)
        self.player(2)
        self.add_games(2, "MIDDLE", 34, 19)
        self.add_games(2, "TOP", 32, 17)
        players = [1, 2, 3, 4, 5]

        before = {fit.player_id: fit.role for fit in assign_roles(self.repo(), players).best.fits}
        fits_before = {
            key: fit.fit for key, fit in assign_roles(self.repo(), players).matrix.items()
        }
        self.assertEqual(before[1], "MIDDLE")

        # Player 2 is now far stronger in absolute terms; their slightly worse
        # MIDDLE fit is now worth more to the team than player 1's.
        self.rank(2, "CHALLENGER", 800)
        self.rank(1, "IRON")
        result = assign_roles(self.repo(), players)
        after = {fit.player_id: fit.role for fit in result.best.fits}

        self.assertEqual(after[2], "MIDDLE")
        self.assertEqual(after[1], "TOP")
        # E itself never moved - only what the search does with it.
        self.assertEqual({key: fit.fit for key, fit in result.matrix.items()}, fits_before)

    def test_runner_up_is_a_different_assignment(self):
        players = self.build_team()
        result = assign_roles(self.repo(), players)

        self.assertNotEqual(
            [fit.player_id for fit in result.best.fits],
            [fit.player_id for fit in result.runner_up.fits],
        )
        for assignment in (result.best, result.runner_up):
            with self.subTest(assignment=assignment.score):
                self.assertEqual(
                    sorted(fit.player_id for fit in assignment.fits), sorted(players)
                )
                self.assertEqual([fit.role for fit in assignment.fits], list(ROLE_ORDER))

    def test_no_ban_recommendation_is_computed(self):
        players = self.build_team()
        with patch.object(ban_algorithm, "recommend_bans") as recommend:
            assign_roles(self.repo(), players)
        recommend.assert_not_called()

    def test_five_distinct_players_are_required(self):
        players = self.build_team()
        repo = self.repo()
        with self.assertRaises(ValueError):
            assign_roles(repo, players[:4])
        with self.assertRaises(ValueError):
            assign_roles(repo, [players[0]] + players[:4])

    def test_a_player_who_never_played_a_role_can_still_be_assigned_to_it(self):
        # Four mains plus one player who has only ever played TOP: somebody has
        # to take the empty role, and the model must produce it, not crash.
        for index, role in enumerate(ROLE_ORDER[:4], start=1):
            self.player(index)
            self.add_games(index, role, 30, 17)
        self.player(5)
        self.add_games(5, "TOP", 25, 13)
        result = assign_roles(self.repo(), [1, 2, 3, 4, 5])

        assigned = {fit.player_id: fit.role for fit in result.best.fits}
        self.assertEqual(assigned[5], "UTILITY")
        self.assertFalse(result.fit(5, "UTILITY").observed)
        self.assertTrue(math.isfinite(result.best.score))


if __name__ == "__main__":
    unittest.main()
