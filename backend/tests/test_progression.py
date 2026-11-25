"""Tests for the 70% progression rule, the AI engine, SQLite and the HTTP API.

Run from the project folder:  python -m unittest discover -s backend/tests -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import ai_engine  # noqa: E402
import app  # noqa: E402
from database import (DatabaseError, GameDatabase, InvalidName, LevelLocked, SCHEMA, SCHEMA_VERSION,  # noqa: E402
                      PlayerNotFound)
from levels import MAX_LEVEL, PASS_THRESHOLD  # noqa: E402


def make_stats(total=48, perfect=0, good=0, miss=None, max_combo=None, score=0, lanes=4, **extra):
    """Build a consistent stats payload like the browser sends."""
    if miss is None:
        miss = total - perfect - good
    hits = perfect + good
    if max_combo is None:
        max_combo = hits
    stats = {
        "total_notes": total, "perfect": perfect, "good": good, "miss": miss,
        "max_combo": max_combo, "combo": max_combo, "score": score, "lanes": lanes,
        "kinds": {}, "miss_lanes": [0, 0, 0, 0],
        "early": 0, "late": 0, "offset_sum_ms": 0, "offset_count": 0,
    }
    stats.update(extra)
    return ai_engine.sanitize_stats(stats)


def run_with_hits(hits, total=48, perfect_share=1.0):
    """A run where the first `hits` notes are hit and the rest are missed."""
    perfect = int(hits * perfect_share)
    return make_stats(total=total, perfect=perfect, good=hits - perfect, score=hits * 100)


class ThresholdTests(unittest.TestCase):
    def test_classification_boundaries(self):
        self.assertEqual(ai_engine.classify(0), "FAILED")
        self.assertEqual(ai_engine.classify(69.9), "FAILED")
        self.assertEqual(ai_engine.classify(69.99), "FAILED")
        self.assertEqual(ai_engine.classify(70.0), "PASSED")
        self.assertEqual(ai_engine.classify(70.1), "PASSED")
        self.assertEqual(ai_engine.classify(99.9), "PASSED")
        self.assertEqual(ai_engine.classify(100.0), "PERFECT")

    def test_completion_is_floored_never_rounded_up(self):
        self.assertEqual(ai_engine._floor1(69.96), 69.9)
        self.assertEqual(ai_engine._floor1(70.0), 70.0)
        self.assertEqual(ai_engine._floor1(99.99), 99.9)

    def test_exactly_70_passes_and_just_below_fails(self):
        # accuracy 60 + challenges (falls back to accuracy) 10 -> 70.0 with all-good hits and no streak credit
        at_70 = ai_engine.evaluate_run(make_stats(total=100, good=100, max_combo=0))
        self.assertEqual(at_70["completion"], 70.0)
        self.assertEqual(at_70["status"], "PASSED")
        self.assertTrue(at_70["unlocks_next"])
        below = ai_engine.evaluate_run(make_stats(total=100, good=99, miss=1, max_combo=0))
        self.assertLess(below["completion"], 70.0)
        self.assertEqual(below["status"], "FAILED")
        self.assertFalse(below["unlocks_next"])

    def test_status_flips_exactly_at_the_pass_mark(self):
        """Walk accuracy from 0 to 100% and make sure the flip matches completion >= 70."""
        previous = -1
        flipped_at = None
        for hits in range(0, 49):
            ev = ai_engine.evaluate_run(run_with_hits(hits, perfect_share=0.5))
            self.assertGreaterEqual(ev["completion"], previous)  # monotonic
            previous = ev["completion"]
            self.assertEqual(ev["unlocks_next"], ev["completion"] >= PASS_THRESHOLD)
            self.assertEqual(ev["status"] == "FAILED", ev["completion"] < PASS_THRESHOLD)
            if ev["passed"] and flipped_at is None:
                flipped_at = hits
        self.assertIsNotNone(flipped_at)
        self.assertGreater(flipped_at, 0)

    def test_failed_run(self):
        ev = ai_engine.evaluate_run(run_with_hits(20))
        self.assertLess(ev["completion"], 70)
        self.assertEqual(ev["status"], "FAILED")
        self.assertFalse(ev["unlocks_next"])

    def test_passed_run_between_70_and_99(self):
        ev = ai_engine.evaluate_run(run_with_hits(44, perfect_share=0.7))
        self.assertGreaterEqual(ev["completion"], 70)
        self.assertLess(ev["completion"], 100)
        self.assertEqual(ev["status"], "PASSED")
        self.assertFalse(ev["perfect"])

    def test_perfect_run_is_exactly_100(self):
        ev = ai_engine.evaluate_run(make_stats(total=48, perfect=48))
        self.assertEqual(ev["completion"], 100.0)
        self.assertEqual(ev["status"], "PERFECT")
        self.assertTrue(ev["perfect"])
        self.assertTrue(ev["unlocks_next"])

    def test_all_good_hits_is_a_pass_not_perfect(self):
        ev = ai_engine.evaluate_run(make_stats(total=48, good=48))
        self.assertEqual(ev["status"], "PASSED")

    def test_breakdown_adds_up(self):
        ev = ai_engine.evaluate_run(run_with_hits(40, perfect_share=0.6))
        self.assertAlmostEqual(sum(i["max"] for i in ev["breakdown"]), 100.0)
        self.assertLessEqual(sum(i["points"] for i in ev["breakdown"]), ev["completion"] + 0.5)


class EngineTests(unittest.TestCase):
    def test_rejects_bad_stats(self):
        for bad in (None, [], {}, {"total_notes": 0}, {"total_notes": 10, "perfect": 9, "good": 9},
                    {"total_notes": "x"}, {"total_notes": 10, "miss_lanes": [1, 2]}):
            with self.assertRaises(ai_engine.StatsError):
                ai_engine.sanitize_stats(bad)

    def test_live_percentage_climbs_and_tracks_pass_possibility(self):
        start = ai_engine.evaluate_live(make_stats(total=48, perfect=0, good=0, miss=0))
        self.assertEqual(start["completion"], 0.0)
        self.assertTrue(start["can_still_pass"])
        mid = ai_engine.evaluate_live(make_stats(total=48, perfect=20, good=0, miss=0))
        self.assertGreater(mid["completion"], start["completion"])
        doomed = ai_engine.evaluate_live(make_stats(total=48, perfect=0, good=0, miss=30))
        self.assertFalse(doomed["can_still_pass"])
        self.assertIn("can't reach", doomed["tip"]["title"])

    def test_live_hits_needed(self):
        ev = ai_engine.evaluate_live(make_stats(total=48, perfect=10, good=0, miss=0))
        self.assertGreater(ev["perfect_hits_needed"], 0)
        passed = ai_engine.evaluate_live(make_stats(total=48, perfect=40, good=0, miss=0))
        self.assertEqual(passed["perfect_hits_needed"], 0)

    def test_feedback_targets_the_players_mistakes(self):
        stats = make_stats(
            total=48, perfect=10, good=14, miss=24, max_combo=6,
            kinds={"hold": {"total": 6, "done": 6, "missed": 5}, "tap": {"total": 30, "done": 30, "missed": 5}},
            miss_lanes=[0, 0, 4, 0], early=2, late=18, offset_sum_ms=900, offset_count=24,
        )
        ev = ai_engine.evaluate_run(stats)
        fb = ai_engine.build_feedback(stats, ev, level=1)
        text = json.dumps(fb)
        self.assertIn("HOLD", text)             # worst note type
        self.assertIn("late", text)             # timing habit
        self.assertEqual(ev["status"], "FAILED")
        self.assertIn("locked", fb["summary"])  # tells the player the next level stays locked

    def test_feedback_mentions_unlock_and_history(self):
        stats = run_with_hits(44, perfect_share=0.7)
        ev = ai_engine.evaluate_run(stats)
        fb = ai_engine.build_feedback(
            stats, ev, level=1, next_level={"level": 2, "name": "Groove"},
            history={"previous": [{"completion": 50.0, "score": 100}], "attempts": 1, "best_completion": 50.0})
        self.assertIn("Level 2", fb["summary"])
        self.assertIn("improving", json.dumps(fb))

    def test_perfect_feedback(self):
        stats = make_stats(total=48, perfect=48)
        fb = ai_engine.build_feedback(stats, ai_engine.evaluate_run(stats), level=3)
        self.assertEqual(fb["headline"], "Perfect completion!")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "nested", "game.db")
        self.db = GameDatabase(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, player_id, level, stats, mode="level", lanes=4):
        ev = ai_engine.evaluate_run(stats)
        return self.db.record_attempt(player_id=player_id, level=level, mode=mode,
                                      difficulty_lanes=lanes, stats=stats, evaluation=ev)

    def test_database_and_folder_created_automatically(self):
        self.assertTrue(os.path.exists(self.path))
        conn = sqlite3.connect(self.path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertTrue({"players", "level_progress", "song_progress", "attempts"} <= tables)

    def test_existing_database_gets_song_title_column(self):
        legacy_schema = SCHEMA.replace(
            "    song_title       TEXT    NOT NULL DEFAULT 'Unknown track',\n", "")
        conn = sqlite3.connect(self.path)
        conn.executescript(legacy_schema)
        conn.close()
        reopened = GameDatabase(self.path)
        conn = sqlite3.connect(self.path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(attempts)")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertIn("song_title", columns)
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertTrue(reopened.is_healthy())

    def test_players_are_unique_case_insensitively(self):
        a = self.db.get_or_create_player("Gian")
        b = self.db.get_or_create_player("  gian ")
        self.assertEqual(a["player_id"], b["player_id"])
        self.assertTrue(a["created"])
        self.assertFalse(b["created"])
        with self.assertRaises(InvalidName):
            self.db.get_or_create_player("   ")
        with self.assertRaises(InvalidName):
            self.db.get_or_create_player("x" * 25)

    def test_new_player_starts_at_level_1(self):
        p = self.db.get_or_create_player("Ana")
        self.assertEqual((p["current_level"], p["levels_completed"], p["highest_score"], p["total_attempts"]),
                         (1, 0, 0, 0))

    def test_failed_attempt_keeps_next_level_locked(self):
        p = self.db.get_or_create_player("Ana")
        res = self.save(p["player_id"], 1, run_with_hits(20))
        self.assertEqual(res["attempt"]["status"], "FAILED")
        self.assertFalse(res["attempt"]["passed"])
        self.assertEqual(res["player"]["current_level"], 1)
        self.assertIsNone(res["unlocked_level"])
        with self.assertRaises(LevelLocked):
            self.save(p["player_id"], 2, run_with_hits(48))

    def test_passing_unlocks_next_level(self):
        p = self.db.get_or_create_player("Ana")
        res = self.save(p["player_id"], 1, run_with_hits(44, perfect_share=0.7))
        self.assertEqual(res["attempt"]["status"], "PASSED")
        self.assertEqual(res["player"]["current_level"], 2)
        self.assertEqual(res["unlocked_level"], 2)
        self.assertTrue(res["first_clear"])
        self.assertEqual(res["player"]["levels_completed"], 1)

    def test_perfect_unlocks_and_is_recorded(self):
        p = self.db.get_or_create_player("Ana")
        res = self.save(p["player_id"], 1, make_stats(total=16, perfect=16, score=5000))
        self.assertEqual(res["attempt"]["status"], "PERFECT")
        self.assertTrue(res["attempt"]["perfect"])
        lvl = res["player"]["levels"][0]
        self.assertTrue(lvl["perfect"])
        self.assertEqual(res["player"]["current_level"], 2)

    def test_highest_score_only_goes_up(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        first = self.save(pid, 1, make_stats(total=16, good=10, miss=6, score=1000))
        self.assertTrue(first["new_high_score"])
        lower = self.save(pid, 1, make_stats(total=16, good=8, miss=8, score=400))
        self.assertFalse(lower["new_high_score"])
        self.assertEqual(lower["player"]["highest_score"], 1000)
        higher = self.save(pid, 1, make_stats(total=16, good=12, miss=4, score=2500))
        self.assertTrue(higher["new_high_score"])
        self.assertEqual(higher["player"]["highest_score"], 2500)

    def test_attempt_counts_and_history(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        for hits in (10, 20, 46):
            self.save(pid, 1, run_with_hits(hits, total=48))
        profile = self.db.get_player(pid)
        self.assertEqual(profile["total_attempts"], 3)
        self.assertEqual(profile["levels"][0]["attempts"], 3)
        history = self.db.get_attempts(pid)
        self.assertEqual(len(history), 3)
        self.assertEqual([h["attempt_number"] for h in history], [3, 2, 1])  # newest first
        self.assertTrue(all(h["created_at"] for h in history))
        summary = self.db.history_summary(pid, 1)
        self.assertEqual(summary["attempts"], 3)

    def test_free_play_is_recorded_but_does_not_unlock(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        res = self.save(pid, 0, make_stats(total=100, perfect=100, score=9999), mode="free")
        self.assertEqual(res["player"]["current_level"], 1)
        self.assertEqual(res["player"]["highest_score"], 0)
        self.assertEqual(res["player"]["total_attempts"], 1)
        self.assertEqual(self.db.get_attempts(pid)[0]["mode"], "free")

    def test_uploaded_song_progress_advances_and_persists_by_song(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        key = "song.mp3 :: song.chart.json :: My Song"
        first = self.db.record_attempt(
            player_id=pid, level=0, mode="free", difficulty_lanes=2,
            stats=make_stats(total=16, perfect=16, score=1200),
            evaluation=ai_engine.evaluate_run(make_stats(total=16, perfect=16, score=1200)),
            song_title="My Song", song_key=key, song_level=1)
        self.assertEqual(first["song_progress"]["current_level"], 2)
        self.assertEqual(first["next_song_level"], 2)
        self.assertEqual(self.db.get_song_progress(pid, key)["current_level"], 2)

        failed = self.db.record_attempt(
            player_id=pid, level=0, mode="free", difficulty_lanes=2,
            stats=run_with_hits(4, total=16),
            evaluation=ai_engine.evaluate_run(run_with_hits(4, total=16)),
            song_title="My Song", song_key=key, song_level=2)
        self.assertIsNone(failed["next_song_level"])
        self.assertEqual(failed["song_progress"]["current_level"], 2)

        second = self.db.record_attempt(
            player_id=pid, level=0, mode="free", difficulty_lanes=2,
            stats=make_stats(total=16, perfect=16, score=2400),
            evaluation=ai_engine.evaluate_run(make_stats(total=16, perfect=16, score=2400)),
            song_title="My Song", song_key=key, song_level=2)
        self.assertEqual(second["song_progress"]["current_level"], 3)
        self.assertEqual(second["attempt"]["song_level"], 2)

    def test_leaderboard_includes_each_run_and_song_in_score_order(self):
        first = self.db.get_or_create_player("Ana")["player_id"]
        second = self.db.get_or_create_player("Bo")["player_id"]
        self.db.record_attempt(player_id=first, level=0, mode="free", difficulty_lanes=4,
                               stats=make_stats(total=16, perfect=16, score=1200),
                               evaluation=ai_engine.evaluate_run(make_stats(total=16, perfect=16, score=1200)),
                               song_title="First Song")
        self.db.record_attempt(player_id=second, level=0, mode="free", difficulty_lanes=4,
                               stats=make_stats(total=16, perfect=16, score=2400),
                               evaluation=ai_engine.evaluate_run(make_stats(total=16, perfect=16, score=2400)),
                               song_title="Second Song")
        self.db.record_attempt(player_id=first, level=0, mode="free", difficulty_lanes=4,
                               stats=make_stats(total=16, perfect=16, score=1800),
                               evaluation=ai_engine.evaluate_run(make_stats(total=16, perfect=16, score=1800)),
                               song_title="Third Song")
        board = self.db.get_leaderboard()
        self.assertEqual([entry["score"] for entry in board], [2400, 1800, 1200])
        self.assertEqual([entry["song_title"] for entry in board],
                         ["Second Song", "Third Song", "First Song"])
        self.assertEqual([entry["player_name"] for entry in board], ["Bo", "Ana", "Ana"])

    def test_read_only_table_viewer_uses_allow_list(self):
        pid = self.db.get_or_create_player("Table Viewer")["player_id"]
        self.save(pid, 1, make_stats(total=16, perfect=16, score=1600))
        tables = self.db.list_tables()
        self.assertEqual([table["name"] for table in tables],
                         ["players", "level_progress", "song_progress", "attempts"])
        players = self.db.get_table_rows("players")
        self.assertIn("name", players["columns"])
        self.assertEqual(players["rows"][0]["name"], "Table Viewer")
        with self.assertRaises(ValueError):
            self.db.get_table_rows("sqlite_master")

    def test_final_level_does_not_overflow(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        for level in range(1, MAX_LEVEL + 1):
            self.save(pid, level, make_stats(total=16, perfect=16, score=100 * level))
        profile = self.db.get_player(pid)
        self.assertEqual(profile["current_level"], MAX_LEVEL)
        self.assertEqual(profile["levels_completed"], MAX_LEVEL)

    def test_progress_survives_reopening_the_database(self):
        pid = self.db.get_or_create_player("Ana")["player_id"]
        self.save(pid, 1, run_with_hits(46, total=48, perfect_share=0.8))
        reopened = GameDatabase(self.path)
        profile = reopened.get_player(pid)
        self.assertEqual(profile["current_level"], 2)
        self.assertEqual(len(reopened.get_attempts(pid)), 1)
        self.assertEqual(reopened.list_players()[0]["name"], "Ana")

    def test_unknown_player(self):
        with self.assertRaises(PlayerNotFound):
            self.db.get_player(999)
        with self.assertRaises(PlayerNotFound):
            self.save(999, 1, run_with_hits(10))

    def test_corrupt_database_is_quarantined_and_recreated(self):
        bad = os.path.join(self.tmp.name, "bad.db")
        with open(bad, "wb") as handle:
            handle.write(b"this is definitely not a sqlite database" * 20)
        db = GameDatabase(bad)
        self.assertTrue(db.is_healthy())
        self.assertTrue(any(name.startswith("bad.db.corrupt-") for name in os.listdir(self.tmp.name)))

    def test_unusable_path_raises_database_error_not_a_crash(self):
        blocker = os.path.join(self.tmp.name, "file.txt")
        open(blocker, "w").close()
        db = GameDatabase(os.path.join(blocker, "sub", "x.db"))  # parent is a file -> cannot be created
        self.assertFalse(db.is_healthy())
        with self.assertRaises(DatabaseError):
            db.list_players()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = GameDatabase(os.path.join(cls.tmp.name, "api.db"))
        cls.server = app.create_server("127.0.0.1", 0, cls.db)
        cls.base = "http://127.0.0.1:%d/api" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, base=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request((base or self.base) + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def payload(self, pid, level, stats, **extra):
        body = {"player_id": pid, "level": level, "mode": "level", "difficulty_lanes": 4, "stats": stats}
        body.update(extra)
        return body

    def test_health_levels_and_unknown_routes(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["db_ok"])
        status, body = self.call("GET", "/levels")
        self.assertEqual(len(body["levels"]), MAX_LEVEL)
        self.assertEqual(body["pass_threshold"], 70)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("POST", "/levels", {})[0], 405)

    def test_full_progression_flow_over_http(self):
        status, body = self.call("POST", "/players", {"name": "Tester"})
        self.assertEqual(status, 200)
        pid = body["player"]["player_id"]

        # live evaluation does not save anything
        status, live = self.call("POST", "/evaluate", run_with_hits(10))
        self.assertEqual(status, 200)
        self.assertIn("completion", live["evaluation"])
        self.assertEqual(self.call("GET", "/players/%d/attempts" % pid)[1]["attempts"], [])

        # 20/48 -> FAILED, level 2 locked
        status, res = self.call("POST", "/attempts", self.payload(
            pid, 1, run_with_hits(20), song_title="Warm-Up Theme"))
        self.assertEqual(status, 200)
        self.assertTrue(res["saved"])
        self.assertEqual(res["attempt"]["song_title"], "Warm-Up Theme")
        self.assertEqual(res["evaluation"]["status"], "FAILED")
        self.assertFalse(res["evaluation"]["unlocks_next"])
        self.assertIsNone(res["next_level"])
        self.assertEqual(res["player"]["current_level"], 1)
        self.assertTrue(res["feedback"]["tips"])

        # trying to save a run on the locked level is refused
        status, err = self.call("POST", "/attempts", self.payload(pid, 2, run_with_hits(48)))
        self.assertEqual((status, err["error"]), (403, "level_locked"))

        # 44/48 -> PASSED, level 2 unlocked
        status, res = self.call("POST", "/attempts", self.payload(pid, 1, run_with_hits(44, perfect_share=0.7)))
        self.assertEqual(res["evaluation"]["status"], "PASSED")
        self.assertEqual(res["next_level"], 2)
        self.assertEqual(res["unlocked_level"], 2)
        self.assertEqual(res["player"]["current_level"], 2)

        # 100% -> PERFECT on level 2
        status, res = self.call("POST", "/attempts", self.payload(pid, 2, make_stats(total=24, perfect=24, score=3000)))
        self.assertEqual(res["evaluation"]["status"], "PERFECT")
        self.assertEqual(res["player"]["current_level"], 3)

        status, hist = self.call("GET", "/players/%d/attempts" % pid)
        self.assertEqual(len(hist["attempts"]), 3)
        status, filtered = self.call("GET", "/players/%d/attempts?level=1" % pid)
        self.assertEqual(len(filtered["attempts"]), 2)
        status, listing = self.call("GET", "/players")
        self.assertIn("Tester", [p["name"] for p in listing["players"]])
        status, leaderboard = self.call("GET", "/leaderboard")
        self.assertEqual(status, 200)
        self.assertEqual(len(leaderboard["leaderboard"]), 3)
        self.assertIn("Warm-Up Theme", [entry["song_title"] for entry in leaderboard["leaderboard"]])

        status, tables = self.call("GET", "/database/tables")
        self.assertEqual(status, 200)
        self.assertEqual([table["name"] for table in tables["tables"]],
                         ["players", "level_progress", "song_progress", "attempts"])
        status, attempts_table = self.call("GET", "/database/tables/attempts?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(attempts_table["table"]["name"], "attempts")
        self.assertEqual(len(attempts_table["table"]["rows"]), 2)
        self.assertEqual(self.call("GET", "/database/tables/sqlite_master")[0], 404)

    def test_uploaded_song_progress_flow_over_http(self):
        status, body = self.call("POST", "/players", {"name": "SongTester"})
        self.assertEqual(status, 200)
        pid = body["player"]["player_id"]
        key = "song.mp3 :: chart.json :: Song Tester"
        request = self.payload(
            pid, 0, make_stats(total=16, perfect=16, score=1800), mode="free",
            song_title="Song Tester", song_key=key, song_level=1, difficulty_lanes=2)
        status, result = self.call("POST", "/attempts", request)
        self.assertEqual(status, 200)
        self.assertEqual(result["song_progress"]["current_level"], 2)
        self.assertEqual(result["next_song_level"], 2)
        status, progress = self.call(
            "GET", "/players/%d/song-progress?key=%s" % (pid, urllib.parse.quote(key)))
        self.assertEqual(status, 200)
        self.assertEqual(progress["progress"]["current_level"], 2)

    def test_song_title_must_be_text_and_within_limit(self):
        pid = self.call("POST", "/players", {"name": "SongTester"})[1]["player"]["player_id"]
        status, bad_type = self.call("POST", "/attempts", self.payload(
            pid, 1, run_with_hits(20), song_title=42))
        self.assertEqual((status, bad_type["error"]), (400, "bad_request"))
        status, too_long = self.call("POST", "/attempts", self.payload(
            pid, 1, run_with_hits(20), song_title="x" * 121))
        self.assertEqual((status, too_long["error"]), (400, "bad_request"))

    def test_bad_requests_get_clean_errors(self):
        self.assertEqual(self.call("POST", "/players", {"name": ""})[1]["error"], "invalid_name")
        self.assertEqual(self.call("POST", "/evaluate", {"total_notes": "abc"})[1]["error"], "invalid_stats")
        self.assertEqual(self.call("GET", "/players/12345")[1]["error"], "player_not_found")
        req = urllib.request.Request(self.base + "/evaluate", data=b"{not json", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_database_failure_does_not_lose_the_evaluation(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        open(blocker, "w").close()
        broken = GameDatabase(os.path.join(blocker, "x", "broken.db"))
        server = app.create_server("127.0.0.1", 0, broken)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d/api" % server.server_address[1]
        try:
            status, body = self.call("POST", "/attempts", self.payload(1, 1, run_with_hits(46)), base=base)
            self.assertEqual(status, 200)                 # the game still gets an answer
            self.assertFalse(body["saved"])
            self.assertIn("save_error", body)
            self.assertEqual(body["evaluation"]["status"], "PASSED")
            self.assertEqual(body["next_level"], 2)       # progression still works this session
            self.assertEqual(self.call("GET", "/players", base=base)[0], 503)
            self.assertEqual(self.call("GET", "/health", base=base)[1]["db_ok"], False)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
