"""Offline checks for metadata identity, cutoff, and missing cosmetic caches."""

import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote
import zlib

import ban_report_assets as assets
import scouting_db as db
from scouting_repo import ScoutingRepo


class BanReportAssetsTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.player_id = db.get_or_create_player(
            self.conn, "own-puuid", "한 글 / Player", "T#1", seen_at=1,
        )
        self.result = SimpleNamespace(players=(SimpleNamespace(player_id=self.player_id),))
        self.repo = ScoutingRepo(cutoff_time=200, conn=self.conn)
        self.meta = patch.object(assets, "_ddragon_version", return_value="16.18.1")
        self.meta.start()
        self.addCleanup(self.meta.stop)

    def add_match(self, match_id, end, queue=420):
        self.conn.execute(
            "INSERT INTO matches (match_id,game_start,game_end,queue_id,raw_file_path,fetched_at) "
            "VALUES (?,?,?,?,?,?)", (match_id, end - 10, end, queue, "", end),
        )
        self.conn.execute(
            "INSERT INTO player_matches (player_id,match_id,champion_id,canonical_role,win) "
            "VALUES (?,?,?,?,?)", (self.player_id, match_id, 1, "TOP", 1),
        )

    def test_own_profile_and_rank_respect_cutoff_and_url_encoding(self):
        self.add_match("older", 100)
        self.add_match("latest", 200, queue=400)
        self.add_match("future", 201)
        self.add_match("other-queue", 200, queue=700)
        db.insert_rank_snapshot(self.conn, self.player_id, "RANKED_SOLO_5x5", "GOLD", "II", 50, 4, 2, 200)
        db.insert_rank_snapshot(self.conn, self.player_id, "RANKED_SOLO_5x5", "DIAMOND", "I", 90, 4, 2, 201)
        with patch.object(assets, "load_raw_match", return_value={"info": {"participants": [
            {"puuid": "somebody-else", "profileIcon": 999},
            {"puuid": "own-puuid", "profileIcon": 123},
        ]}}) as load:
            presentation = assets.load_player_presentations(self.repo, self.result)[self.player_id]
        load.assert_called_once_with("latest")
        self.assertEqual((presentation.tier, presentation.division, presentation.lp), ("GOLD", "II", 50))
        self.assertEqual(presentation.profile_icon_url, "https://ddragon.leagueoflegends.com/cdn/16.18.1/img/profileicon/123.png")
        self.assertEqual(presentation.opgg_url, f"https://op.gg/lol/summoners/kr/{quote('한 글 / Player', safe='')}-T%231")

    def test_missing_raw_and_rank_do_not_fail_report(self):
        self.add_match("unavailable", 200)
        for failure in (FileNotFoundError, zlib.error):
            with self.subTest(failure=failure), patch.object(assets, "load_raw_match", side_effect=failure):
                presentation = assets.load_player_presentations(self.repo, self.result)[self.player_id]
            self.assertIsNone(presentation.profile_icon_url)
            self.assertIsNone(presentation.tier)
            self.assertIsNotNone(presentation.opgg_url)

    def test_missing_latest_raw_falls_back_to_earlier_own_icon(self):
        self.add_match("older", 100)
        self.add_match("latest", 200)
        raw = {"info": {"participants": [{"puuid": "own-puuid", "profileIcon": 0}]}}
        with patch.object(assets, "load_raw_match", side_effect=[EOFError(), raw]) as load:
            presentation = assets.load_player_presentations(self.repo, self.result)[self.player_id]
        self.assertEqual([call.args[0] for call in load.call_args_list], ["latest", "older"])
        self.assertTrue(presentation.profile_icon_url.endswith("/0.png"))

    def test_existing_profile_icon_id_skips_raw_lookup(self):
        player = dict(self.repo.get_player(self.player_id)) | {"profileIconId": 44}
        with patch.object(self.repo, "get_player", return_value=player), patch.object(assets, "load_raw_match") as load:
            presentation = assets.load_player_presentations(self.repo, self.result)[self.player_id]
        load.assert_not_called()
        self.assertTrue(presentation.profile_icon_url.endswith("/44.png"))

    def test_invalid_icon_or_other_participant_is_never_used(self):
        self.add_match("latest", 200)
        for raw in (None, [], {"info": None}, {"info": {"participants": None}},
                    {"info": {"participants": [{"puuid": "somebody-else", "profileIcon": 99}]}},
                    {"info": {"participants": [{"puuid": "own-puuid", "profileIcon": True}]}}):
            with self.subTest(raw=raw), patch.object(assets, "load_raw_match", return_value=raw):
                presentation = assets.load_player_presentations(self.repo, self.result)[self.player_id]
                self.assertIsNone(presentation.profile_icon_url)

    def test_local_ddragon_version_or_verified_fallback(self):
        self.meta.stop()
        with patch.object(assets.Path, "read_text", return_value='{"version": "16.17.1"}'):
            self.assertEqual(assets._ddragon_version(), "16.17.1")
        for content in ("broken-json", "[]", '{"version": "../../bad"}', '{"version": 17}'):
            with self.subTest(content=content), patch.object(assets.Path, "read_text", return_value=content):
                self.assertEqual(assets._ddragon_version(), assets.FALLBACK_DDRAGON_VERSION)
        with patch.object(assets.Path, "read_text", side_effect=FileNotFoundError):
            self.assertEqual(assets._ddragon_version(), assets.FALLBACK_DDRAGON_VERSION)


if __name__ == "__main__":
    unittest.main()
