"""Collection resilience: retries, permanent skips, and what actually stops a run.

Fully offline - no Riot requests, no real raw-match directory, no sleeping.
"""

import sqlite3
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from riot import riot_api
from riot.riot_api import (
    InvalidApiKeyError, MatchNotFoundError, RateLimitedError,
    RiotNetworkError, RiotServerError,
)
from scouting import match_collector, scouting_db as db


def raw_match(match_id: str, puuid: str, queue_id: int = 420) -> dict:
    participants = [
        {"puuid": puuid if i == 0 else f"other-{match_id}-{i}", "teamId": 100 + 100 * (i // 5),
         "participantId": i + 1, "championId": 1, "championName": "Aatrox",
         "teamPosition": "TOP", "individualPosition": "TOP", "win": True,
         "kills": 1, "deaths": 1, "assists": 1}
        for i in range(10)
    ]
    return {
        "metadata": {"matchId": match_id},
        "info": {
            "gameStartTimestamp": 1_700_000_000_000, "gameDuration": 1800,
            "gameVersion": "14.18.1", "queueId": queue_id, "participants": participants,
        },
    }


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    """riot_api retries the same URL; only transient failures qualify."""

    async def asyncSetUp(self):
        self.slept = []
        sleep = patch.object(riot_api.asyncio, "sleep", AsyncMock(side_effect=self.slept.append))
        sleep.start()
        self.addCleanup(sleep.stop)

    async def test_rate_limit_retries_and_honors_retry_after(self):
        request = AsyncMock(side_effect=[RateLimitedError(7), RateLimitedError(3), {"ok": True}])
        with patch.object(riot_api, "_request_json", request):
            self.assertEqual(await riot_api._request_json_with_retry("url"), {"ok": True})
        self.assertEqual(request.await_count, 3)
        self.assertEqual(self.slept, [7, 3])  # Retry-After obeyed verbatim

    async def test_server_and_network_errors_retry_then_surface_the_last_one(self):
        for error in (RiotServerError("boom", status_code=503), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.slept.clear()
                request = AsyncMock(side_effect=error)
                with patch.object(riot_api, "_request_json", request):
                    with self.assertRaises(riot_api.RiotApiError):
                        await riot_api._request_json_with_retry("url")
                self.assertEqual(request.await_count, riot_api.MATCH_FETCH_MAX_ATTEMPTS)
                # Backs off between attempts, but never after the last one.
                self.assertEqual(len(self.slept), riot_api.MATCH_FETCH_MAX_ATTEMPTS - 1)

    async def test_key_and_not_found_answers_are_not_retried(self):
        request = AsyncMock(side_effect=InvalidApiKeyError("bad key"))
        with patch.object(riot_api, "_request_json", request):
            with self.assertRaises(InvalidApiKeyError):
                await riot_api._request_json_with_retry("url")
        self.assertEqual(request.await_count, 1)

        # 404 comes back as None from _request_json and must pass straight through.
        request = AsyncMock(return_value=None)
        with patch.object(riot_api, "_request_json", request):
            self.assertIsNone(await riot_api._request_json_with_retry("url"))
        self.assertEqual(request.await_count, 1)
        self.assertEqual(self.slept, [])


class CollectorToleranceTests(unittest.IsolatedAsyncioTestCase):
    PUUID = "puuid-under-test"

    async def asyncSetUp(self):
        uri = f"file:{uuid4().hex}?mode=memory&cache=shared"
        self.conn = sqlite3.connect(uri, uri=True)
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)
        self.addCleanup(self.conn.close)
        self.match_ids = [f"KR_{i}" for i in range(4)]
        self.saved = {}
        patches = [
            patch.object(match_collector, "get_match_ids_by_queue", AsyncMock(return_value=self.match_ids)),
            # Keep the real raw-match directory untouched; storage is not under test.
            patch.object(match_collector, "save_raw_match", side_effect=self.save),
            # has_cached_match checks the file too, so mirror what save recorded.
            patch("scouting.raw_match_store.raw_match_exists", side_effect=lambda mid: mid in self.saved),
            patch.object(match_collector.asyncio, "sleep", AsyncMock()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def save(self, match_id, data):
        self.saved[match_id] = data
        return match_id

    async def collect(self):
        return await match_collector.collect_player_matches(
            self.PUUID, "Name", "TAG", queue_id=420, max_count=4,
            request_delay=0, conn=self.conn,
        )

    def fetcher(self, outcomes):
        """Map match_id -> raw data to return, or an exception to raise for it."""
        async def fetch(match_id):
            outcome = outcomes[match_id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return AsyncMock(side_effect=fetch)

    def outcomes(self, **overrides):
        return {mid: raw_match(mid, self.PUUID) for mid in self.match_ids} | overrides

    async def test_transient_failure_is_counted_but_does_not_stop_the_rest(self):
        outcomes = self.outcomes(KR_1=RateLimitedError(1), KR_2=RiotNetworkError("connection reset"))
        with patch.object(match_collector, "get_match_by_id", self.fetcher(outcomes)) as fetch:
            result = await self.collect()
        self.assertEqual(fetch.await_count, 4)  # every match still attempted
        self.assertEqual(result["newly_fetched"], 2)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["permanently_skipped"], 0)
        # Transient losses stay incomplete so a rerun picks them back up.
        self.assertFalse(result["is_complete"])
        self.assertEqual(db.get_permanent_skip_match_ids(self.conn), set())

    async def test_permanent_skips_complete_the_run_and_are_not_refetched(self):
        outcomes = self.outcomes(
            KR_1=MatchNotFoundError("gone"),
            KR_2=raw_match("KR_2", self.PUUID, queue_id=1700),  # not the queue we asked for
            KR_3=raw_match("KR_3", "someone-else"),             # requester not in the match
        )
        with patch.object(match_collector, "get_match_by_id", self.fetcher(outcomes)):
            result = await self.collect()
        self.assertEqual(result["permanently_skipped"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(self.saved.keys(), {"KR_0"})  # nothing dubious was stored
        # Nothing retryable is left, so the queue counts as done - the whole
        # point: matches Riot will never hand over must not block this player
        # forever.
        self.assertTrue(result["is_complete"])
        self.assertEqual(db.get_permanent_skip_match_ids(self.conn), {"KR_1", "KR_2", "KR_3"})

        with patch.object(match_collector, "get_match_by_id", self.fetcher(outcomes)) as again:
            rerun = await self.collect()
        again.assert_not_awaited()  # skips are remembered across runs
        self.assertEqual(rerun["permanently_skipped"], 3)
        self.assertEqual(rerun["skipped_cached"], 1)
        self.assertTrue(rerun["is_complete"])
        # Re-marking the same matches must not pile up rows.
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM fetch_failures WHERE endpoint = ?", (db.PERMANENT_SKIP_ENDPOINT,)
        ).fetchone()[0]
        self.assertEqual(rows, 3)

    async def test_invalid_key_aborts_immediately(self):
        outcomes = self.outcomes(KR_0=InvalidApiKeyError("expired"))
        with patch.object(match_collector, "get_match_by_id", self.fetcher(outcomes)) as fetch:
            result = await self.collect()
        self.assertEqual(fetch.await_count, 1)  # no point asking again
        self.assertTrue(result["aborted"])
        self.assertFalse(result["is_complete"])
        self.assertEqual(db.get_permanent_skip_match_ids(self.conn), set())

    async def test_storage_failure_stays_fatal(self):
        outcomes = self.outcomes()
        with patch.object(match_collector, "get_match_by_id", self.fetcher(outcomes)), \
             patch.object(match_collector, "save_raw_match", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.collect()


if __name__ == "__main__":
    unittest.main()
