"""
스카우팅용 SQLite 데이터베이스.

- players: 내부 surrogate id를 기본키로 쓰고, puuid는 별도 unique 컬럼으로 둠
  (라이엇이 puuid 체계를 바꾸거나, 한 선수가 여러 puuid를 가진 과거 데이터를 다룰 때
  외부 키가 아니라 내부 id로 참조를 고정하기 위함).
- matches: Match-V5 원본 파일(raw_matches/<id>.json.gz)에 대한 색인.
- player_matches: 매치당 10명 참가자 전원의 파생 통계(챔피언, 포지션, 승패 등).
  원본에서 언제든 재생성 가능(reparse_matches.py). 수집을 직접 요청한 선수 외에는
  game_name/tag_line을 모르는 채로(puuid만 알고) 들어올 수 있음 - 추가 API 호출 없이
  puuid만으로 players에 등록되기 때문.
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
CREATE INDEX IF NOT EXISTS idx_matches_queue_id ON matches(queue_id);

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
    patch TEXT,
    PRIMARY KEY (player_id, match_id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (match_id) REFERENCES matches(match_id)
);

-- PRIMARY KEY(player_id, match_id)가 이미 (player_id, match_id) 조회에 쓰이는
-- 고유 인덱스라 별도 인덱스는 필요 없음.
CREATE INDEX IF NOT EXISTS idx_player_matches_player_role
    ON player_matches(player_id, canonical_role);

-- 챔피언별/포지션별/패치별 메타 픽률 집계용 (자체 데이터로 티어 메타를 낼 때 씀).
-- patch는 matches.patch를 참가자 행에 복제해 둔 값이라, matches 조인 없이도
-- player_matches 단독으로 이 집계를 낼 수 있음.
CREATE INDEX IF NOT EXISTS idx_player_matches_champion_role_patch
    ON player_matches(champion_id, canonical_role, patch);

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

-- mastery_snapshots / rank_snapshots: append-only. 기존 행을 절대 UPDATE하지
-- 않고, 수집할 때마다 새 snapshot_at으로 새 행을 INSERT함(시계열로 축적).
CREATE TABLE IF NOT EXISTS mastery_snapshots (
    player_id INTEGER NOT NULL,
    champion_id INTEGER NOT NULL,
    mastery_points INTEGER NOT NULL,
    mastery_level INTEGER NOT NULL,
    last_play_time INTEGER,
    snapshot_at INTEGER NOT NULL,
    PRIMARY KEY (player_id, champion_id, snapshot_at),
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS rank_snapshots (
    player_id INTEGER NOT NULL,
    queue_type TEXT NOT NULL,
    tier TEXT,
    division TEXT,
    lp INTEGER,
    wins INTEGER,
    losses INTEGER,
    snapshot_at INTEGER NOT NULL,
    PRIMARY KEY (player_id, queue_type, snapshot_at),
    FOREIGN KEY (player_id) REFERENCES players(id)
);
"""


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """
    기존 DB를 지우지 않고 새 컬럼을 맞춰줌. SCHEMA의 CREATE TABLE은 새 DB에만
    적용되므로(테이블이 이미 있으면 무시됨), 이미 만들어진 DB에 나중에 추가된
    컬럼은 여기서 ALTER TABLE로 채워야 함.

    반드시 executescript(SCHEMA)보다 먼저 실행해야 함: SCHEMA 안의
    CREATE INDEX(...patch)가 patch 컬럼이 없는 옛 player_matches 테이블을
    보고 바로 실패하기 때문 - 컬럼을 먼저 만들어 둬야 그 인덱스 생성이 통과함.
    player_matches 테이블 자체가 아직 없는 새 DB에서는 CREATE TABLE이 이미
    patch를 포함해서 만들 것이므로 여기선 손댈 게 없음.
    """
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='player_matches'"
    ).fetchone() is not None

    if not table_exists:
        return

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(player_matches)").fetchall()
    }

    if "patch" not in columns:
        conn.execute("ALTER TABLE player_matches ADD COLUMN patch TEXT")

    conn.commit()


def _backfill_patch(conn: sqlite3.Connection) -> None:
    """
    patch가 비어 있는 player_matches 행을 matches.patch로 채움.
    이미 채워진 행은 건드리지 않으니 몇 번을 다시 실행해도 안전함.
    """
    conn.execute(
        """
        UPDATE player_matches
        SET patch = (
            SELECT m.patch FROM matches m WHERE m.match_id = player_matches.match_id
        )
        WHERE patch IS NULL
        """
    )
    conn.commit()


def init_db(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    conn = conn or get_connection()

    try:
        _migrate(conn)
        conn.executescript(SCHEMA)
        conn.commit()
        _backfill_patch(conn)
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


def upsert_player_match(
    conn: sqlite3.Connection,
    player_id: int,
    match_id: str,
    participant_row: dict,
    patch: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO player_matches (
            player_id, match_id, team_id, participant_id, champion_id, champion_name,
            team_position, individual_position, canonical_role, role_mismatch,
            win, kills, deaths, assists, patch
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            assists = excluded.assists,
            patch = excluded.patch
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
            patch,
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

# =========================
# mastery_snapshots / rank_snapshots (append-only)
# =========================

def insert_mastery_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    champion_id: int,
    mastery_points: int,
    mastery_level: int,
    last_play_time: int | None,
    snapshot_at: int,
) -> None:
    """
    항상 새 행을 INSERT함. 기존 스냅샷을 덮어쓰지 않음
    (같은 player_id/champion_id/snapshot_at 조합이 이미 있으면 PK 위반으로 실패함 -
    같은 수집 배치 안에서는 champion_id가 서로 달라서 충돌하지 않음).
    """
    conn.execute(
        """
        INSERT INTO mastery_snapshots (
            player_id, champion_id, mastery_points, mastery_level, last_play_time, snapshot_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (player_id, champion_id, mastery_points, mastery_level, last_play_time, snapshot_at)
    )


def insert_rank_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    queue_type: str,
    tier: str | None,
    division: str | None,
    lp: int | None,
    wins: int | None,
    losses: int | None,
    snapshot_at: int,
) -> None:
    """
    항상 새 행을 INSERT함. 기존 스냅샷을 덮어쓰지 않음.
    """
    conn.execute(
        """
        INSERT INTO rank_snapshots (
            player_id, queue_type, tier, division, lp, wins, losses, snapshot_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (player_id, queue_type, tier, division, lp, wins, losses, snapshot_at)
    )


def get_latest_mastery_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    champion_id: int,
    cutoff_time: int | None = None,
) -> sqlite3.Row | None:
    """
    cutoff_time이 주어지면 snapshot_at <= cutoff_time인 것 중 가장 최근 스냅샷을 반환함.
    cutoff_time이 None이면 전체에서 가장 최근 스냅샷을 반환함.
    """
    query = "SELECT * FROM mastery_snapshots WHERE player_id = ? AND champion_id = ?"
    params: tuple = (player_id, champion_id)

    if cutoff_time is not None:
        query += " AND snapshot_at <= ?"
        params += (cutoff_time,)

    query += " ORDER BY snapshot_at DESC LIMIT 1"

    return conn.execute(query, params).fetchone()


def get_latest_mastery_snapshots(
    conn: sqlite3.Connection,
    player_id: int,
    cutoff_time: int | None = None,
) -> list[sqlite3.Row]:
    """
    챔피언별로 cutoff_time 이전(또는 전체)에서 가장 최근 스냅샷 하나씩만 반환함.
    """
    query = """
        SELECT ms.*
        FROM mastery_snapshots ms
        JOIN (
            SELECT champion_id, MAX(snapshot_at) AS max_snapshot_at
            FROM mastery_snapshots
            WHERE player_id = ?
    """
    params: tuple = (player_id,)

    if cutoff_time is not None:
        query += " AND snapshot_at <= ?"
        params += (cutoff_time,)

    query += """
            GROUP BY champion_id
        ) latest
        ON latest.champion_id = ms.champion_id AND latest.max_snapshot_at = ms.snapshot_at
        WHERE ms.player_id = ?
        ORDER BY ms.champion_id
    """
    params += (player_id,)

    return conn.execute(query, params).fetchall()


def get_latest_rank_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    queue_type: str,
    cutoff_time: int | None = None,
) -> sqlite3.Row | None:
    """
    cutoff_time이 주어지면 snapshot_at <= cutoff_time인 것 중 가장 최근 스냅샷을 반환함.
    cutoff_time이 None이면 전체에서 가장 최근 스냅샷을 반환함.
    """
    query = "SELECT * FROM rank_snapshots WHERE player_id = ? AND queue_type = ?"
    params: tuple = (player_id, queue_type)

    if cutoff_time is not None:
        query += " AND snapshot_at <= ?"
        params += (cutoff_time,)

    query += " ORDER BY snapshot_at DESC LIMIT 1"

    return conn.execute(query, params).fetchone()


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
