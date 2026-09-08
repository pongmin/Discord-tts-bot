"""
스카우팅용 SQLite 데이터베이스.

- players: 내부 surrogate id를 기본키로 쓰고, puuid는 별도 unique 컬럼으로 둠
  (라이엇이 puuid 체계를 바꾸거나, 한 선수가 여러 puuid를 가진 과거 데이터를 다룰 때
  외부 키가 아니라 내부 id로 참조를 고정하기 위함).
- matches: Match-V5 원본 파일(raw_matches/<id>.json.gz)에 대한 색인.
- player_matches: 선수별 파생 통계(챔피언, 포지션, 승패 등). 원본에서 언제든 재생성 가능.
- player_fetch_state: 선수+큐별 수집 진행 상황. 수집기가 재시작해도 이어서 할 수 있게 함.
- fetch_failures: 실패 로그. retryable로 재시도 가능한 실패와 영구 실패를 구분함.

이 모듈은 쓰기 쪽(수집기)이 쓰는 저수준 접근 계층이고, cutoff 적용이 필요한
읽기 전용 분석 쪽은 scouting_repo.ScoutingRepo를 통해서 함.
"""

import sqlite3
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
DB_PATH = DATA_DIR / "scouting.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    puuid TEXT NOT NULL UNIQUE,
    game_name TEXT,
    tag_line TEXT,
    platform TEXT,
    last_seen_at INTEGER
);

CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    game_start INTEGER NOT NULL,
    game_end INTEGER,
    game_version TEXT,
    patch TEXT,
    queue_id INTEGER NOT NULL,
    game_duration INTEGER,
    raw_file_path TEXT NOT NULL,
    fetched_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matches_game_start ON matches(game_start);

CREATE TABLE IF NOT EXISTS player_matches (
    player_id INTEGER NOT NULL,
    match_id TEXT NOT NULL,
    team_id INTEGER,
    participant_id INTEGER,
    champion_id INTEGER,
    champion_name TEXT,
    team_position TEXT,
    individual_position TEXT,
    canonical_role TEXT,
    role_mismatch INTEGER NOT NULL DEFAULT 0,
    win INTEGER,
    kills INTEGER,
    deaths INTEGER,
    assists INTEGER,
    PRIMARY KEY (player_id, match_id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

CREATE INDEX IF NOT EXISTS idx_player_matches_player_role
    ON player_matches(player_id, canonical_role);

CREATE TABLE IF NOT EXISTS player_fetch_state (
    player_id INTEGER NOT NULL,
    queue_id INTEGER NOT NULL,
    newest_fetched_at INTEGER,
    oldest_fetched_at INTEGER,
    match_count_fetched INTEGER NOT NULL DEFAULT 0,
    is_complete INTEGER NOT NULL DEFAULT 0,
    last_attempt_at INTEGER,
    PRIMARY KEY (player_id, queue_id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS fetch_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL,
    match_id TEXT,
    status_code INTEGER,
    attempted_at INTEGER NOT NULL,
    retryable INTEGER NOT NULL,
    message TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_failures_match ON fetch_failures(match_id);
"""


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    conn = conn or get_connection()

    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def now_ms() -> int:
    return int(time.time() * 1000)


# =========================
# players
# =========================

def get_player_by_puuid(conn: sqlite3.Connection, puuid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM players WHERE puuid = ?", (puuid,)
    ).fetchone()


def get_player_by_riot_id(conn: sqlite3.Connection, game_name: str, tag_line: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM players WHERE LOWER(game_name) = LOWER(?) AND LOWER(tag_line) = LOWER(?)",
        (game_name, tag_line)
    ).fetchone()


def get_or_create_player(
    conn: sqlite3.Connection,
    puuid: str,
    game_name: str | None = None,
    tag_line: str | None = None,
    platform: str = "KR",
    seen_at: int | None = None,
) -> int:
    seen_at = seen_at if seen_at is not None else now_ms()

    row = get_player_by_puuid(conn, puuid)

    if row is None:
        cursor = conn.execute(
            """
            INSERT INTO players (puuid, game_name, tag_line, platform, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (puuid, game_name, tag_line, platform, seen_at)
        )
        conn.commit()
        return cursor.lastrowid

    # Riot ID는 바뀔 수 있으므로 새 값이 왔을 때만 갱신하고, 없으면 기존 값을 유지함
    conn.execute(
        """
        UPDATE players
        SET
            game_name = COALESCE(?, game_name),
            tag_line = COALESCE(?, tag_line),
            platform = COALESCE(?, platform),
            last_seen_at = ?
        WHERE id = ?
        """,
        (game_name, tag_line, platform, seen_at, row["id"])
    )
    conn.commit()

    return row["id"]


# =========================
# matches / player_matches
# =========================

def has_cached_match(conn: sqlite3.Connection, match_id: str) -> bool:
    """
    DB에 색인이 있고 원본 파일도 실제로 있을 때만 "캐시됨"으로 취급함.
    (수집 도중 죽으면 둘 중 하나만 있는 상태가 생길 수 있어서 방어적으로 둘 다 확인)
    """
    from raw_match_store import raw_match_exists

    row = conn.execute(
        "SELECT 1 FROM matches WHERE match_id = ?", (match_id,)
    ).fetchone()

    return row is not None and raw_match_exists(match_id)


def upsert_match(conn: sqlite3.Connection, match_id: str, match_row: dict, raw_file_path: str) -> None:
    conn.execute(
        """
        INSERT INTO matches (
            match_id, game_start, game_end, game_version, patch,
            queue_id, game_duration, raw_file_path, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(match_id) DO UPDATE SET
            game_start = excluded.game_start,
            game_end = excluded.game_end,
            game_version = excluded.game_version,
            patch = excluded.patch,
            queue_id = excluded.queue_id,
            game_duration = excluded.game_duration,
            raw_file_path = excluded.raw_file_path
        """,
        (
            match_id,
            match_row["game_start"],
            match_row["game_end"],
            match_row["game_version"],
            match_row["patch"],
            match_row["queue_id"],
            match_row["game_duration"],
            raw_file_path,
            now_ms(),
        )
    )


def upsert_player_match(conn: sqlite3.Connection, player_id: int, match_id: str, participant_row: dict) -> None:
    conn.execute(
        """
        INSERT INTO player_matches (
            player_id, match_id, team_id, participant_id, champion_id, champion_name,
            team_position, individual_position, canonical_role, role_mismatch,
            win, kills, deaths, assists
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_id, match_id) DO UPDATE SET
            team_id = excluded.team_id,
            participant_id = excluded.participant_id,
            champion_id = excluded.champion_id,
            champion_name = excluded.champion_name,
            team_position = excluded.team_position,
            individual_position = excluded.individual_position,
            canonical_role = excluded.canonical_role,
            role_mismatch = excluded.role_mismatch,
            win = excluded.win,
            kills = excluded.kills,
            deaths = excluded.deaths,
            assists = excluded.assists
        """,
        (
            player_id,
            match_id,
            participant_row["team_id"],
            participant_row["participant_id"],
            participant_row["champion_id"],
            participant_row["champion_name"],
            participant_row["team_position"],
            participant_row["individual_position"],
            participant_row["canonical_role"],
            int(participant_row["role_mismatch"]),
            int(participant_row["win"]),
            participant_row["kills"],
            participant_row["deaths"],
            participant_row["assists"],
        )
    )


def has_player_match_row(conn: sqlite3.Connection, player_id: int, match_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM player_matches WHERE player_id = ? AND match_id = ?",
        (player_id, match_id)
    ).fetchone()

    return row is not None


# =========================
# player_fetch_state
# =========================

def touch_fetch_attempt(conn: sqlite3.Connection, player_id: int, queue_id: int, attempted_at: int | None = None) -> None:
    attempted_at = attempted_at if attempted_at is not None else now_ms()

    conn.execute(
        """
        INSERT INTO player_fetch_state (player_id, queue_id, last_attempt_at)
        VALUES (?, ?, ?)
        ON CONFLICT(player_id, queue_id) DO UPDATE SET
            last_attempt_at = excluded.last_attempt_at
        """,
        (player_id, queue_id, attempted_at)
    )
    conn.commit()


def refresh_fetch_state(conn: sqlite3.Connection, player_id: int, queue_id: int, is_complete: bool) -> None:
    row = conn.execute(
        """
        SELECT MIN(m.game_start) AS oldest, MAX(m.game_start) AS newest, COUNT(*) AS count
        FROM player_matches pm
        JOIN matches m ON m.match_id = pm.match_id
        WHERE pm.player_id = ? AND m.queue_id = ?
        """,
        (player_id, queue_id)
    ).fetchone()

    conn.execute(
        """
        UPDATE player_fetch_state
        SET
            oldest_fetched_at = ?,
            newest_fetched_at = ?,
            match_count_fetched = ?,
            is_complete = ?
        WHERE player_id = ? AND queue_id = ?
        """,
        (row["oldest"], row["newest"], row["count"], int(is_complete), player_id, queue_id)
    )
    conn.commit()


def get_fetch_state(conn: sqlite3.Connection, player_id: int, queue_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM player_fetch_state WHERE player_id = ? AND queue_id = ?",
        (player_id, queue_id)
    ).fetchone()


# =========================
# fetch_failures
# =========================

def record_failure(
    conn: sqlite3.Connection,
    endpoint: str,
    retryable: bool,
    match_id: str | None = None,
    status_code: int | None = None,
    message: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_failures (endpoint, match_id, status_code, attempted_at, retryable, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (endpoint, match_id, status_code, now_ms(), int(retryable), message)
    )
    conn.commit()
