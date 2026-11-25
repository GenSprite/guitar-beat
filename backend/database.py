"""SQLite persistence for GUITAR-BEAT.

Design
------
players         one row per player: name, the level to continue from
                (``current_level``), levels completed, highest score, attempts.
level_progress  one row per (player, level): attempts, best completion/score,
                completed / perfect flags.
song_progress   one row per (player, uploaded song): adaptive song level and
                best score for that chart.
attempts        one row per finished run (the full history, never overwritten):
                completion %, score, status, difficulty and a timestamp.

The database file is created automatically (``data/guitar_beat.db`` by default,
override with the ``GUITAR_BEAT_DB`` environment variable). Every request opens
its own short-lived connection, writes happen inside one transaction, and any
SQLite problem is reported as :class:`DatabaseError` so the caller can keep the
game running.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from levels import MAX_LEVEL

SCHEMA_VERSION = 3

READ_ONLY_TABLES = ("players", "level_progress", "song_progress", "attempts")
TABLE_ORDER_BY = {
    "players": "player_id ASC",
    "level_progress": "player_id ASC, level ASC",
    "song_progress": "last_played_at DESC, player_id ASC",
    "attempts": "created_at DESC, attempt_id DESC",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    current_level    INTEGER NOT NULL DEFAULT 1 CHECK (current_level >= 1),
    levels_completed INTEGER NOT NULL DEFAULT 0,
    highest_score    INTEGER NOT NULL DEFAULT 0,
    total_attempts   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    last_played_at   TEXT
);

CREATE TABLE IF NOT EXISTS level_progress (
    player_id       INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    level           INTEGER NOT NULL CHECK (level >= 1),
    attempts        INTEGER NOT NULL DEFAULT 0,
    best_completion REAL    NOT NULL DEFAULT 0,
    best_score      INTEGER NOT NULL DEFAULT 0,
    completed       INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
    perfect         INTEGER NOT NULL DEFAULT 0 CHECK (perfect IN (0, 1)),
    first_passed_at TEXT,
    PRIMARY KEY (player_id, level)
);

CREATE TABLE IF NOT EXISTS song_progress (
    player_id        INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    song_key         TEXT    NOT NULL,
    song_title       TEXT    NOT NULL DEFAULT 'Custom song',
    current_level    INTEGER NOT NULL DEFAULT 1 CHECK (current_level >= 1),
    levels_completed INTEGER NOT NULL DEFAULT 0,
    highest_score    INTEGER NOT NULL DEFAULT 0,
    total_attempts   INTEGER NOT NULL DEFAULT 0,
    last_played_at   TEXT,
    PRIMARY KEY (player_id, song_key)
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id        INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    level            INTEGER NOT NULL DEFAULT 0 CHECK (level >= 0),  -- 0 = free play
    mode             TEXT    NOT NULL DEFAULT 'level' CHECK (mode IN ('level', 'free')),
    attempt_number   INTEGER NOT NULL,
    difficulty_lanes INTEGER NOT NULL DEFAULT 4,
    song_title       TEXT    NOT NULL DEFAULT 'Unknown track',
    song_key         TEXT    NOT NULL DEFAULT '',
    song_level       INTEGER NOT NULL DEFAULT 0 CHECK (song_level >= 0),
    completion_pct   REAL    NOT NULL CHECK (completion_pct BETWEEN 0 AND 100),
    score            INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL CHECK (status IN ('FAILED', 'PASSED', 'PERFECT')),
    passed           INTEGER NOT NULL CHECK (passed IN (0, 1)),
    perfect          INTEGER NOT NULL DEFAULT 0 CHECK (perfect IN (0, 1)),
    accuracy_pct     REAL,
    total_notes      INTEGER,
    perfect_hits     INTEGER,
    good_hits        INTEGER,
    misses           INTEGER,
    max_combo        INTEGER,
    details          TEXT,                       -- JSON of the raw run stats
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attempts_player_time  ON attempts (player_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_player_level ON attempts (player_id, level);
CREATE INDEX IF NOT EXISTS idx_attempts_score ON attempts (score DESC, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_song_progress_player ON song_progress (player_id, last_played_at DESC);
"""


class DatabaseError(Exception):
    """The database could not be opened, read or written."""


class PlayerNotFound(Exception):
    pass


class LevelLocked(Exception):
    pass


class UnknownTable(ValueError):
    pass


class InvalidName(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(raw: Any) -> str:
    """Validate a player name: 1-24 characters, whitespace collapsed."""
    if not isinstance(raw, str):
        raise InvalidName("Enter a player name.")
    name = " ".join(raw.split())
    if not name:
        raise InvalidName("Enter a player name.")
    if len(name) > 24:
        raise InvalidName("Player names can be at most 24 characters.")
    if any(ord(ch) < 32 for ch in name):
        raise InvalidName("That name contains invalid characters.")
    return name


def clean_song_key(raw: Any) -> str:
    """Validate the stable identifier used for one player's uploaded song."""
    if not isinstance(raw, str):
        raise InvalidName("The uploaded song needs a valid chart identifier.")
    key = " ".join(raw.split())
    if not key:
        raise InvalidName("The uploaded song needs a valid chart identifier.")
    if len(key) > 240:
        raise InvalidName("The uploaded song identifier is too long.")
    if any(ord(ch) < 32 for ch in key):
        raise InvalidName("The uploaded song identifier contains invalid characters.")
    return key


class GameDatabase:
    def __init__(self, path: str):
        self.path = path
        self._ready = False
        self._lock = threading.Lock()
        try:
            self.ensure_ready()
        except DatabaseError:
            pass  # stay usable: every call retries initialisation

    # -- connection handling -------------------------------------------------
    def ensure_ready(self) -> None:
        """Create the database file and tables if they do not exist yet."""
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            try:
                folder = os.path.dirname(os.path.abspath(self.path))
                os.makedirs(folder, exist_ok=True)
                try:
                    self._create_schema()
                except sqlite3.DatabaseError as exc:
                    if not self._looks_corrupt(exc):
                        raise
                    self._quarantine()
                    self._create_schema()
            except (sqlite3.Error, OSError) as exc:
                raise DatabaseError("Could not open the database: %s" % exc) from exc
            self._ready = True

    @staticmethod
    def _looks_corrupt(exc: Exception) -> bool:
        text = str(exc).lower()
        return "malformed" in text or "not a database" in text or "corrupt" in text

    def _quarantine(self) -> None:
        """Move an unreadable database aside so a fresh one can be created."""
        if os.path.exists(self.path):
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            os.replace(self.path, "%s.corrupt-%s" % (self.path, stamp))

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)  # autocommit; we use BEGIN
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _create_schema(self) -> None:
        conn = self._open()
        try:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
            if "song_title" not in columns:
                conn.execute(
                    "ALTER TABLE attempts ADD COLUMN song_title TEXT NOT NULL DEFAULT 'Unknown track'"
                )
            if "song_key" not in columns:
                conn.execute("ALTER TABLE attempts ADD COLUMN song_key TEXT NOT NULL DEFAULT ''")
            if "song_level" not in columns:
                conn.execute("ALTER TABLE attempts ADD COLUMN song_level INTEGER NOT NULL DEFAULT 0")
            conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        finally:
            conn.close()

    @contextmanager
    def _connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        self.ensure_ready()
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            raise DatabaseError("Could not open the database: %s" % exc) from exc
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except sqlite3.Error as exc:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise DatabaseError("Database error: %s" % exc) from exc
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def is_healthy(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM players LIMIT 1").fetchall()
            return True
        except DatabaseError:
            return False

    # -- read-only table viewer ----------------------------------------------
    def list_tables(self) -> List[Dict[str, Any]]:
        """Return safe metadata for the application's own tables."""
        with self._connect() as conn:
            result: List[Dict[str, Any]] = []
            for name in READ_ONLY_TABLES:
                columns = [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % name)]
                count = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
                result.append({"name": name, "columns": columns, "row_count": count})
        return result

    def get_table_rows(self, name: str, limit: int = 200) -> Dict[str, Any]:
        """Read rows from one allow-listed table; never accepts arbitrary SQL."""
        if name not in READ_ONLY_TABLES:
            raise UnknownTable("Unknown database table.")
        limit = max(1, min(int(limit), 500))
        order_by = TABLE_ORDER_BY[name]
        with self._connect() as conn:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % name)]
            total = conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM %s ORDER BY %s LIMIT ?" % (name, order_by), (limit,)
            ).fetchall()
        return {"name": name, "columns": columns, "total": total, "rows": [dict(row) for row in rows]}

    # -- players -------------------------------------------------------------
    def get_or_create_player(self, name: str) -> Dict[str, Any]:
        name = clean_name(name)
        with self._connect(write=True) as conn:
            row = conn.execute("SELECT player_id FROM players WHERE name = ?", (name,)).fetchone()
            created = row is None
            if created:
                conn.execute("INSERT INTO players (name, created_at) VALUES (?, ?)", (name, now_iso()))
                row = conn.execute("SELECT player_id FROM players WHERE name = ?", (name,)).fetchone()
            profile = self._profile(conn, row["player_id"])
        profile["created"] = created
        return profile

    def list_players(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT player_id, name, current_level, levels_completed, highest_score, total_attempts, "
                "created_at, last_played_at FROM players "
                "ORDER BY COALESCE(last_played_at, created_at) DESC, player_id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_player(self, player_id: int) -> Dict[str, Any]:
        with self._connect() as conn:
            return self._profile(conn, player_id)

    def get_song_progress(self, player_id: int, song_key: str, song_title: str = "Custom song") -> Dict[str, Any]:
        song_key = clean_song_key(song_key)
        with self._connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM players WHERE player_id = ?", (player_id,)).fetchone() is None:
                raise PlayerNotFound("Player %s does not exist." % player_id)
            row = conn.execute(
                "SELECT * FROM song_progress WHERE player_id = ? AND song_key = ?",
                (player_id, song_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO song_progress (player_id, song_key, song_title) VALUES (?, ?, ?)",
                    (player_id, song_key, song_title or "Custom song"),
                )
                row = conn.execute(
                    "SELECT * FROM song_progress WHERE player_id = ? AND song_key = ?",
                    (player_id, song_key),
                ).fetchone()
            return dict(row)

    @staticmethod
    def _profile(conn: sqlite3.Connection, player_id: int) -> Dict[str, Any]:
        row = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
        if row is None:
            raise PlayerNotFound("Player %s does not exist." % player_id)
        profile = dict(row)
        levels = conn.execute(
            "SELECT level, attempts, best_completion, best_score, completed, perfect, first_passed_at "
            "FROM level_progress WHERE player_id = ? ORDER BY level",
            (player_id,),
        ).fetchall()
        profile["levels"] = [dict(r, completed=bool(r["completed"]), perfect=bool(r["perfect"])) for r in levels]
        return profile

    # -- attempts ------------------------------------------------------------
    def get_attempts(self, player_id: int, limit: int = 100, level: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        query = ("SELECT attempt_id, player_id, level, mode, attempt_number, difficulty_lanes, song_title, song_key, song_level, completion_pct, "
                 "score, status, passed, perfect, accuracy_pct, total_notes, perfect_hits, good_hits, misses, "
                 "max_combo, created_at FROM attempts WHERE player_id = ?")
        params: List[Any] = [player_id]
        if level is not None:
            query += " AND level = ?"
            params.append(level)
        query += " ORDER BY created_at DESC, attempt_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM players WHERE player_id = ?", (player_id,)).fetchone() is None:
                raise PlayerNotFound("Player %s does not exist." % player_id)
            rows = conn.execute(query, params).fetchall()
        return [dict(r, passed=bool(r["passed"]), perfect=bool(r["perfect"])) for r in rows]

    def get_leaderboard(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT a.attempt_id, p.name AS player_name, a.song_title, a.score, a.status, "
                "a.completion_pct, a.mode, a.level, a.created_at "
                "FROM attempts a JOIN players p ON p.player_id = a.player_id "
                "ORDER BY a.score DESC, a.created_at ASC, a.attempt_id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def history_summary(self, player_id: int, level: int) -> Dict[str, Any]:
        """Earlier attempts on a level, used to personalise AI feedback."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT completion_pct, score FROM attempts WHERE player_id = ? AND level = ? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 5",
                (player_id, level),
            ).fetchall()
            count = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE player_id = ? AND level = ?", (player_id, level)
            ).fetchone()[0]
            best = conn.execute(
                "SELECT MAX(completion_pct) FROM attempts WHERE player_id = ? AND level = ?", (player_id, level)
            ).fetchone()[0]
        return {
            "previous": [{"completion": r["completion_pct"], "score": r["score"]} for r in rows],
            "attempts": count,
            "best_completion": best,
        }

    def history_summary_song(self, player_id: int, song_key: str, song_level: int) -> Dict[str, Any]:
        """Earlier attempts on one uploaded song level, used for AI feedback."""
        song_key = clean_song_key(song_key)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT completion_pct, score FROM attempts WHERE player_id = ? AND song_key = ? AND song_level = ? "
                "ORDER BY created_at DESC, attempt_id DESC LIMIT 5",
                (player_id, song_key, song_level),
            ).fetchall()
            count = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE player_id = ? AND song_key = ? AND song_level = ?",
                (player_id, song_key, song_level),
            ).fetchone()[0]
            best = conn.execute(
                "SELECT MAX(completion_pct) FROM attempts WHERE player_id = ? AND song_key = ? AND song_level = ?",
                (player_id, song_key, song_level),
            ).fetchone()[0]
        return {
            "previous": [{"completion": r["completion_pct"], "score": r["score"]} for r in rows],
            "attempts": count,
            "best_completion": best,
        }

    def record_attempt(self, *, player_id: int, level: int, mode: str, difficulty_lanes: int,
                       stats: Dict[str, Any], evaluation: Dict[str, Any],
                       song_title: str = "Built-in track", song_key: str = "", song_level: int = 0) -> Dict[str, Any]:
        """Save one finished run and update the player's progress atomically.

        Returns the stored attempt, the refreshed player profile and flags that
        describe what changed (new high score, newly unlocked level, ...).
        """
        timestamp = now_iso()
        passed = bool(evaluation["passed"])
        score = int(stats["score"])
        completion = float(evaluation["completion"])
        is_level_run = mode == "level"
        is_song_run = mode == "free" and bool(song_key)
        if is_song_run:
            song_key = clean_song_key(song_key)
            song_level = max(1, min(MAX_LEVEL, int(song_level or 1)))
        else:
            song_key = ""
            song_level = 0

        with self._connect(write=True) as conn:
            player = conn.execute("SELECT * FROM players WHERE player_id = ?", (player_id,)).fetchone()
            if player is None:
                raise PlayerNotFound("Player %s does not exist." % player_id)
            if is_level_run and level > player["current_level"]:
                raise LevelLocked("Level %d is still locked for this player." % level)
            if is_song_run:
                song_lock = conn.execute(
                    "SELECT current_level FROM song_progress WHERE player_id = ? AND song_key = ?",
                    (player_id, song_key),
                ).fetchone()
                if song_lock is not None and song_level > song_lock["current_level"]:
                    raise LevelLocked("Song Level %d is still locked for this player." % song_level)

            attempt_number = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE player_id = ? AND level = ?", (player_id, level)
            ).fetchone()[0] + 1
            cursor = conn.execute(
                "INSERT INTO attempts (player_id, level, mode, attempt_number, difficulty_lanes, song_title, song_key, song_level, completion_pct, "
                "score, status, passed, perfect, accuracy_pct, total_notes, perfect_hits, good_hits, misses, "
                "max_combo, details, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (player_id, level, mode, attempt_number, difficulty_lanes, song_title, song_key, song_level, completion, score,
                 evaluation["status"], int(passed), int(evaluation["perfect"]), evaluation["accuracy_pct"],
                 stats["total_notes"], stats["perfect"], stats["good"], stats["miss"], stats["max_combo"],
                 json.dumps(stats, separators=(",", ":")), timestamp),
            )
            attempt_id = cursor.lastrowid

            new_high_score = False
            unlocked_level: Optional[int] = None
            first_clear = False
            highest = player["highest_score"]
            current_level = player["current_level"]

            if is_level_run:
                progress = conn.execute(
                    "SELECT * FROM level_progress WHERE player_id = ? AND level = ?", (player_id, level)
                ).fetchone()
                if progress is None:
                    conn.execute("INSERT INTO level_progress (player_id, level) VALUES (?, ?)", (player_id, level))
                    progress = conn.execute(
                        "SELECT * FROM level_progress WHERE player_id = ? AND level = ?", (player_id, level)
                    ).fetchone()
                first_clear = passed and not progress["completed"]
                conn.execute(
                    "UPDATE level_progress SET attempts = attempts + 1, best_completion = ?, best_score = ?, "
                    "completed = ?, perfect = ?, first_passed_at = ? WHERE player_id = ? AND level = ?",
                    (max(progress["best_completion"], completion), max(progress["best_score"], score),
                     int(bool(progress["completed"]) or passed), int(bool(progress["perfect"]) or evaluation["perfect"]),
                     timestamp if first_clear else progress["first_passed_at"], player_id, level),
                )
                if score > highest:
                    new_high_score = score > 0
                    highest = score
                if passed and level >= current_level and level < MAX_LEVEL:
                    current_level = level + 1
                    unlocked_level = current_level

            song_progress = None
            next_song_level: Optional[int] = None
            if is_song_run:
                progress = conn.execute(
                    "SELECT * FROM song_progress WHERE player_id = ? AND song_key = ?",
                    (player_id, song_key),
                ).fetchone()
                if progress is None:
                    conn.execute(
                        "INSERT INTO song_progress (player_id, song_key, song_title) VALUES (?, ?, ?)",
                        (player_id, song_key, song_title or "Custom song"),
                    )
                    progress = conn.execute(
                        "SELECT * FROM song_progress WHERE player_id = ? AND song_key = ?",
                        (player_id, song_key),
                    ).fetchone()
                song_current = progress["current_level"]
                song_completed = progress["levels_completed"]
                if score > progress["highest_score"]:
                    new_high_score = score > 0
                first_clear = passed and song_level > song_completed
                if passed and song_level >= song_current and song_level < MAX_LEVEL:
                    song_current = song_level + 1
                    next_song_level = song_current
                if passed:
                    song_completed = max(song_completed, song_level)
                conn.execute(
                    "UPDATE song_progress SET song_title = ?, current_level = ?, levels_completed = ?, "
                    "highest_score = ?, total_attempts = total_attempts + 1, last_played_at = ? "
                    "WHERE player_id = ? AND song_key = ?",
                    (song_title or progress["song_title"], song_current, song_completed,
                     max(progress["highest_score"], score), timestamp, player_id, song_key),
                )
                song_progress = dict(conn.execute(
                    "SELECT * FROM song_progress WHERE player_id = ? AND song_key = ?",
                    (player_id, song_key),
                ).fetchone())

            completed_count = conn.execute(
                "SELECT COUNT(*) FROM level_progress WHERE player_id = ? AND completed = 1", (player_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE players SET current_level = ?, levels_completed = ?, highest_score = ?, "
                "total_attempts = total_attempts + 1, last_played_at = ? WHERE player_id = ?",
                (current_level, completed_count, highest, timestamp, player_id),
            )
            profile = self._profile(conn, player_id)

        attempt = {
            "attempt_id": attempt_id, "player_id": player_id, "level": level, "mode": mode,
            "attempt_number": attempt_number, "difficulty_lanes": difficulty_lanes,
            "song_title": song_title, "song_key": song_key, "song_level": song_level,
            "completion_pct": completion, "score": score, "status": evaluation["status"],
            "passed": passed, "perfect": bool(evaluation["perfect"]), "created_at": timestamp,
        }
        return {
            "attempt": attempt,
            "player": profile,
            "new_high_score": new_high_score,
            "unlocked_level": unlocked_level,
            "first_clear": first_clear,
            "song_progress": song_progress,
            "next_song_level": next_song_level,
        }
