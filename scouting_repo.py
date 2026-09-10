"""
분석/백테스트용 읽기 전용 데이터 접근 계층.

ScoutingRepo(cutoff_time=T)로 만들면, 이 repo를 통한 모든 조회는
game_end가 T 이후인 매치를 절대 포함하지 않음. 나중 시점 정보가
과거 시점 분석에 새는(leakage) 걸 막기 위한 용도.

cutoff_time은 Riot 타임스탬프와 같은 단위(epoch ms)를 씀.
cutoff_time=None이면 컷오프 없이 최신까지 전부 조회함(백테스트가 아닌
실시간 조회용도로만 사용해야 함).
"""

import sqlite3

import scouting_db as db


class ScoutingRepo:
    def __init__(self, cutoff_time: int | None = None, conn: sqlite3.Connection | None = None):
        self.cutoff_time = cutoff_time
        self._conn = conn or db.get_connection()
        self._owns_conn = conn is None

    def _cutoff_clause(self, alias: str = "m") -> tuple[str, tuple]:
        if self.cutoff_time is None:
            return "", ()

        return f" AND {alias}.game_end <= ?", (self.cutoff_time,)

    def get_player_id_by_puuid(self, puuid: str) -> int | None:
        row = db.get_player_by_puuid(self._conn, puuid)
        return row["id"] if row else None

    def get_player_id_by_riot_id(self, game_name: str, tag_line: str) -> int | None:
        row = db.get_player_by_riot_id(self._conn, game_name, tag_line)
        return row["id"] if row else None

    def get_player(self, player_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM players WHERE id = ?", (player_id,)
        ).fetchone()

    @staticmethod
    def _queue_in_clause(queue_ids: tuple[int, ...] | list[int]) -> tuple[str, tuple]:
        queue_ids = tuple(queue_ids)

        if not queue_ids:
            raise ValueError("queue_ids는 비어 있을 수 없음")

        placeholders = ",".join("?" for _ in queue_ids)
        return f"m.queue_id IN ({placeholders})", queue_ids

    def get_role_matches(
        self,
        player_id: int,
        role: str,
        queue_ids: tuple[int, ...] | list[int] = (420,),
    ) -> list[sqlite3.Row]:
        """
        해당 플레이어가 canonical_role == role로 뛴 경기를(queue_ids에 속한 큐만),
        game_start 오름차순(과거 -> 최근)으로 반환함.
        cutoff_time 이후 경기는 절대 섞이지 않음.
        pm.player_id로 걸러내므로, 매치당 참가자 10명이 전부 저장돼 있어도
        여기 반환되는 행은 항상 이 player_id 한 명 것뿐임.
        """
        cutoff_clause, cutoff_params = self._cutoff_clause("m")
        queue_clause, queue_params = self._queue_in_clause(queue_ids)

        query = f"""
            SELECT
                pm.player_id, pm.match_id, pm.champion_id, pm.champion_name,
                pm.team_position, pm.individual_position, pm.canonical_role,
                pm.role_mismatch, pm.win, pm.kills, pm.deaths, pm.assists,
                m.game_start, m.game_end, m.game_version, m.patch, m.queue_id
            FROM player_matches pm
            JOIN matches m ON m.match_id = pm.match_id
            WHERE pm.player_id = ?
              AND {queue_clause}
              AND pm.canonical_role = ?
              {cutoff_clause}
            ORDER BY m.game_start ASC
        """

        params = (player_id, *queue_params, role, *cutoff_params)
        return self._conn.execute(query, params).fetchall()

    def get_all_matches(
        self,
        player_id: int,
        queue_ids: tuple[int, ...] | list[int] = (420,),
    ) -> list[sqlite3.Row]:
        """
        pm.player_id로 걸러내므로, 매치당 참가자 10명이 전부 저장돼 있어도
        여기 반환되는 행은 항상 이 player_id 한 명 것뿐임.
        """
        cutoff_clause, cutoff_params = self._cutoff_clause("m")
        queue_clause, queue_params = self._queue_in_clause(queue_ids)

        query = f"""
            SELECT
                pm.player_id, pm.match_id, pm.champion_id, pm.champion_name,
                pm.team_position, pm.individual_position, pm.canonical_role,
                pm.role_mismatch, pm.win, pm.kills, pm.deaths, pm.assists,
                m.game_start, m.game_end, m.game_version, m.patch, m.queue_id
            FROM player_matches pm
            JOIN matches m ON m.match_id = pm.match_id
            WHERE pm.player_id = ?
              AND {queue_clause}
              {cutoff_clause}
            ORDER BY m.game_start ASC
        """

        params = (player_id, *queue_params, *cutoff_params)
        return self._conn.execute(query, params).fetchall()

    def get_fetch_state(self, player_id: int, queue_id: int = 420) -> sqlite3.Row | None:
        return db.get_fetch_state(self._conn, player_id, queue_id)

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    def __enter__(self) -> "ScoutingRepo":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
