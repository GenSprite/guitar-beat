"""GUITAR-BEAT Python backend (standard library only - nothing to pip install).

Run it with ``python backend/app.py``. ``npm start`` launches it automatically
and the Node server forwards ``/api/game/*`` requests to it.

Endpoints (all JSON)::

    GET  /api/health
    GET  /api/levels
    GET  /api/players
    POST /api/players                    {"name": "Gian"}
    GET  /api/players/<id>
    GET  /api/players/<id>/attempts?limit=100&level=2
    GET  /api/players/<id>/song-progress?key=...&title=...
    GET  /api/leaderboard?limit=100
    GET  /api/database/tables
    GET  /api/database/tables/<name>?limit=200
    POST /api/evaluate                   live stats  -> completion %, AI tip
    POST /api/attempts                   finished run -> AI feedback + saved record
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

import ai_engine  # noqa: E402
from database import (DatabaseError, GameDatabase, InvalidName, LevelLocked,  # noqa: E402
                      PlayerNotFound, UnknownTable)
from levels import LEVELS, MAX_LEVEL, PASS_THRESHOLD, get_level  # noqa: E402

DEFAULT_PORT = 5001
MAX_BODY_BYTES = 64 * 1024
BUILT_IN_SONG_TITLE = "Electric riff / Track 01"


def default_db_path() -> str:
    return os.environ.get("GUITAR_BEAT_DB") or os.path.join(os.path.dirname(BACKEND_DIR), "data", "guitar_beat.db")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- Route handlers ----------------------------------------------------------
# Each handler receives (db, match, query, body) and returns a JSON-able dict.
def health(db: GameDatabase, *_: Any) -> Dict[str, Any]:
    return {"ok": True, "service": "guitar-beat-backend", "db_ok": db.is_healthy(),
            "pass_threshold": PASS_THRESHOLD}


def levels(db: GameDatabase, *_: Any) -> Dict[str, Any]:
    return {"ok": True, "pass_threshold": PASS_THRESHOLD, "levels": LEVELS}


def players_list(db: GameDatabase, *_: Any) -> Dict[str, Any]:
    return {"ok": True, "players": db.list_players()}


def players_create(db: GameDatabase, match: Any, query: Any, body: Any) -> Dict[str, Any]:
    name = (body or {}).get("name") if isinstance(body, dict) else None
    profile = db.get_or_create_player(name)  # InvalidName -> 400 via dispatcher
    return {"ok": True, "player": profile, "created": profile.pop("created")}


def player_get(db: GameDatabase, match: Any, *_: Any) -> Dict[str, Any]:
    return {"ok": True, "player": db.get_player(int(match.group(1)))}


def player_attempts(db: GameDatabase, match: Any, query: Dict[str, List[str]], *_: Any) -> Dict[str, Any]:
    try:
        limit = int(query.get("limit", ["100"])[0])
        level = int(query["level"][0]) if "level" in query else None
    except ValueError:
        raise ApiError(400, "bad_request", "limit and level must be integers.")
    return {"ok": True, "attempts": db.get_attempts(int(match.group(1)), limit, level)}


def player_song_progress(db: GameDatabase, match: Any, query: Dict[str, List[str]], *_: Any) -> Dict[str, Any]:
    song_key = query.get("key", [""])[0]
    song_title = query.get("title", ["Custom song"])[0]
    return {"ok": True, "progress": db.get_song_progress(int(match.group(1)), song_key, song_title)}


def leaderboard(db: GameDatabase, match: Any, query: Dict[str, List[str]], *_: Any) -> Dict[str, Any]:
    try:
        limit = int(query.get("limit", ["100"])[0])
    except ValueError:
        raise ApiError(400, "bad_request", "limit must be an integer.")
    return {"ok": True, "leaderboard": db.get_leaderboard(limit)}


def database_tables(db: GameDatabase, *_: Any) -> Dict[str, Any]:
    return {"ok": True, "tables": db.list_tables()}


def database_table(db: GameDatabase, match: Any, query: Dict[str, List[str]], *_: Any) -> Dict[str, Any]:
    try:
        limit = int(query.get("limit", ["200"])[0])
    except ValueError:
        raise ApiError(400, "bad_request", "limit must be an integer.")
    try:
        return {"ok": True, "table": db.get_table_rows(match.group(1), limit)}
    except UnknownTable as exc:
        raise ApiError(404, "table_not_found", str(exc))


def _clean_stats(raw: Any) -> Dict[str, Any]:
    try:
        return ai_engine.sanitize_stats(raw)
    except ai_engine.StatsError as exc:
        raise ApiError(400, "invalid_stats", str(exc))


def evaluate_live(db: GameDatabase, match: Any, query: Any, body: Any) -> Dict[str, Any]:
    """Real-time completion % and coaching tip for a run in progress (no saving)."""
    return {"ok": True, "evaluation": ai_engine.evaluate_live(_clean_stats(body))}


def save_attempt(db: GameDatabase, match: Any, query: Any, body: Any) -> Dict[str, Any]:
    """Evaluate a finished run, generate AI feedback and save it.

    A database failure never loses the evaluation: the response then has
    ``saved: false`` and the game keeps working (and unlocks levels for the
    current session).
    """
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "Expected a JSON object.")
    stats = _clean_stats(body.get("stats"))
    mode = body.get("mode", "level")
    if mode not in ("level", "free"):
        raise ApiError(400, "bad_request", "mode must be 'level' or 'free'.")
    try:
        player_id = int(body.get("player_id"))
        level = int(body.get("level", 0))
        lanes = int(body.get("difficulty_lanes", stats["lanes"]))
        song_key = body.get("song_key", "")
        song_level = int(body.get("song_level", 0)) if song_key else 0
    except (TypeError, ValueError):
        raise ApiError(400, "bad_request", "player_id, level and difficulty_lanes must be integers.")
    if mode == "level" and not 1 <= level <= MAX_LEVEL:
        raise ApiError(400, "bad_request", "level must be between 1 and %d." % MAX_LEVEL)
    if mode == "free":
        level = 0
        if song_key and not 1 <= song_level <= MAX_LEVEL:
            raise ApiError(400, "bad_request", "song_level must be between 1 and %d." % MAX_LEVEL)
    else:
        song_key = ""
        song_level = 0
    lanes = max(1, min(4, lanes))
    song_title = body.get("song_title")
    if song_title is None:
        song_title = BUILT_IN_SONG_TITLE if mode == "level" else "Custom song"
    if not isinstance(song_title, str):
        raise ApiError(400, "bad_request", "song_title must be text.")
    song_title = " ".join(song_title.split())
    if not song_title:
        song_title = BUILT_IN_SONG_TITLE if mode == "level" else "Custom song"
    if len(song_title) > 120:
        raise ApiError(400, "bad_request", "song_title can be at most 120 characters.")

    evaluation = ai_engine.evaluate_run(stats)
    unlocks = mode == "level" and evaluation["unlocks_next"]
    next_info = get_level(level + 1) if unlocks else None
    next_song_level = song_level + 1 if song_key and evaluation["unlocks_next"] and song_level < MAX_LEVEL else None

    history: Optional[Dict[str, Any]] = None
    try:
        history = (db.history_summary_song(player_id, song_key, song_level)
                   if song_key else db.history_summary(player_id, level))
    except (DatabaseError, PlayerNotFound):
        history = None  # feedback just becomes less personalised

    feedback = ai_engine.build_feedback(
        stats, evaluation, level=level, history=history, next_level=next_info,
        is_final_level=((mode == "level" and level == MAX_LEVEL and unlocks) or
                        (bool(song_key) and song_level == MAX_LEVEL and evaluation["unlocks_next"])),
        song_level=song_level, next_song_level=next_song_level,
    )

    response: Dict[str, Any] = {
        "ok": True,
        "saved": False,
        "mode": mode,
        "level": level,
        "evaluation": evaluation,
        "feedback": feedback,
        "next_level": next_info["level"] if next_info else None,
        "is_final_level": mode == "level" and level == MAX_LEVEL and unlocks,
        "new_high_score": False,
        "unlocked_level": None,
        "first_clear": False,
        "song_level": song_level,
        "song_progress": None,
        "next_song_level": next_song_level,
        "player": None,
        "attempt": None,
    }
    try:
        saved = db.record_attempt(player_id=player_id, level=level, mode=mode,
                                  difficulty_lanes=lanes, stats=stats, evaluation=evaluation,
                                  song_title=song_title, song_key=song_key, song_level=song_level)
        response.update(saved)
        response["saved"] = True
    except DatabaseError as exc:
        log("could not save attempt: %s" % exc)
        response["save_error"] = "The result could not be saved to the database."
    return response


ROUTES: List[Tuple[str, "re.Pattern[str]", Callable[..., Dict[str, Any]]]] = [
    ("GET", re.compile(r"^/api/health$"), health),
    ("GET", re.compile(r"^/api/levels$"), levels),
    ("GET", re.compile(r"^/api/players$"), players_list),
    ("POST", re.compile(r"^/api/players$"), players_create),
    ("GET", re.compile(r"^/api/leaderboard$"), leaderboard),
    ("GET", re.compile(r"^/api/database/tables$"), database_tables),
    ("GET", re.compile(r"^/api/database/tables/([a-z_]+)$"), database_table),
    ("GET", re.compile(r"^/api/players/(\d+)$"), player_get),
    ("GET", re.compile(r"^/api/players/(\d+)/attempts$"), player_attempts),
    ("GET", re.compile(r"^/api/players/(\d+)/song-progress$"), player_song_progress),
    ("POST", re.compile(r"^/api/evaluate$"), evaluate_live),
    ("POST", re.compile(r"^/api/attempts$"), save_attempt),
]


def log(message: str) -> None:
    sys.stderr.write("[backend] %s\n" % message)
    sys.stderr.flush()


# --- HTTP plumbing -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "GuitarBeatBackend/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
        if os.environ.get("GUITAR_BEAT_VERBOSE"):
            log(fmt % args)

    def _send(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "payload_too_large", "Request body is too large.")
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "bad_json", "Request body is not valid JSON.")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            path_matched = False
            for route_method, pattern, handler in ROUTES:
                match = pattern.match(parsed.path)
                if not match:
                    continue
                path_matched = True
                if route_method != method:
                    continue
                body = self._read_body() if method == "POST" else None
                result = handler(self.server.db, match, parse_qs(parsed.query), body)  # type: ignore[attr-defined]
                self._send(200, result)
                return
            if path_matched:
                raise ApiError(405, "method_not_allowed", "Method not allowed.")
            raise ApiError(404, "not_found", "Unknown endpoint.")
        except ApiError as exc:
            self._send(exc.status, {"ok": False, "error": exc.code, "message": exc.message})
        except InvalidName as exc:
            self._send(400, {"ok": False, "error": "invalid_name", "message": str(exc)})
        except PlayerNotFound as exc:
            self._send(404, {"ok": False, "error": "player_not_found", "message": str(exc)})
        except LevelLocked as exc:
            self._send(403, {"ok": False, "error": "level_locked", "message": str(exc)})
        except DatabaseError as exc:
            log("database error: %s" % exc)
            self._send(503, {"ok": False, "error": "database_unavailable",
                             "message": "The player database is unavailable right now."})
        except Exception:  # never let one bad request kill the server
            traceback.print_exc()
            self._send(500, {"ok": False, "error": "internal_error", "message": "Unexpected server error."})

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def create_server(host: str, port: int, db: GameDatabase) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.db = db  # type: ignore[attr-defined]
    return server


def main() -> int:
    port = int(os.environ.get("GUITAR_BEAT_PY_PORT", DEFAULT_PORT))
    db_path = default_db_path()
    db = GameDatabase(db_path)
    try:
        server = create_server("127.0.0.1", port, db)
    except OSError as exc:
        log("port %d is not available (%s) - assuming a backend is already running." % (port, exc))
        return 0
    state = "ready" if db.is_healthy() else "UNAVAILABLE (will keep retrying)"
    log("listening on http://127.0.0.1:%d  |  database: %s [%s]" % (port, db_path, state))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
