"""GUITAR-BEAT performance engine.

Turns the raw numbers the game collects during a run into:

* a **completion percentage** (0-100) built from four weighted objectives,
* a **status** (FAILED / PASSED / PERFECT) using the 70% pass mark,
* **live** evaluation while the song is still playing (can the player still
  reach 70%? how many more clean hits are needed?),
* **feedback** - a summary, strengths and tips tailored to the player's actual
  mistakes (which note types and lanes they missed, whether they hit early or
  late) and to their history on the level.
* **adaptive progression** - a passing uploaded-song run unlocks the next
  song level, while a failed run keeps the current challenge.

It is a transparent, rule-based analysis engine: no internet, no API keys and no
third-party packages. The stats it consumes are also stored in the database,
so a scikit-learn model can be trained on them later without changing the game.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from levels import PASS_THRESHOLD, PERFECT_THRESHOLD

# --- How completion is calculated -------------------------------------------
# Objective                       weight   what it measures
# accuracy                         60      notes hit / total notes
# timing                           15      PERFECT hits / total notes
# combo (streak)                   15      longest streak / total notes
# challenges (special notes)       10      x2, x3, HOLD and chord notes completed
# A player who hits every note perfectly, never breaks the streak and clears
# every special note scores exactly 100%.
WEIGHTS: Dict[str, float] = {"accuracy": 60.0, "timing": 15.0, "combo": 15.0, "challenges": 10.0}
LABELS: Dict[str, str] = {
    "accuracy": "Accuracy",
    "timing": "Perfect timing",
    "combo": "Streak",
    "challenges": "Special notes",
}
NOTE_KINDS = ("tap", "double", "triple", "hold", "chord")
SPECIAL_KINDS = ("double", "triple", "hold", "chord")
KIND_NAMES = {
    "tap": "single notes",
    "double": "x2 double-tap notes",
    "triple": "x3 triple-tap notes",
    "hold": "HOLD notes",
    "chord": "chords",
}
KIND_TIPS = {
    "tap": "Keep your eyes on the red strike line and press the instant a gem touches it.",
    "double": "A x2 note needs two evenly spaced taps (about 0.2 s apart). Count \"tap-tap\" and do not rush the second press.",
    "triple": "A x3 note needs three even taps. Settle into a steady \"tap-tap-tap\" instead of mashing the key.",
    "hold": "Press when the head reaches the line, then keep the key down until the whole tail has passed. Letting go early counts as a miss.",
    "chord": "Press every lane of the chord together (within about 0.16 s). Rest your fingers on the keys and drop them as one.",
}
LANE_KEYS = ("D", "F", "J", "K")


class StatsError(ValueError):
    """Raised when the stats sent by the game are malformed."""


# --- Input validation --------------------------------------------------------
def _num(raw: Dict[str, Any], key: str, default: float = 0, lo: float = 0,
         hi: float = 10 ** 9, integer: bool = True) -> Any:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StatsError("'%s' must be a number" % key)
    if math.isnan(value) or math.isinf(value):
        raise StatsError("'%s' must be a finite number" % key)
    value = int(round(value)) if integer else float(value)
    return max(lo, min(hi, value))


def sanitize_stats(raw: Any) -> Dict[str, Any]:
    """Validate and normalise the stats payload sent by the browser."""
    if not isinstance(raw, dict):
        raise StatsError("stats must be an object")
    total = _num(raw, "total_notes", lo=0, hi=5000)
    if total < 1:
        raise StatsError("total_notes must be at least 1")
    perfect = _num(raw, "perfect", hi=total)
    good = _num(raw, "good", hi=total)
    miss = _num(raw, "miss", hi=total)
    if perfect + good > total or perfect + good + miss > total:
        raise StatsError("hit and miss counts exceed total_notes")
    max_combo = _num(raw, "max_combo", hi=total)
    combo = min(_num(raw, "combo", hi=total), max_combo)

    kinds: Dict[str, Dict[str, int]] = {}
    raw_kinds = raw.get("kinds") or {}
    if not isinstance(raw_kinds, dict):
        raise StatsError("kinds must be an object")
    for kind in NOTE_KINDS:
        entry = raw_kinds.get(kind)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise StatsError("kinds.%s must be an object" % kind)
        k_total = _num(entry, "total", hi=total)
        k_done = _num(entry, "done", hi=k_total)
        k_missed = _num(entry, "missed", hi=k_done)
        kinds[kind] = {"total": k_total, "done": k_done, "missed": k_missed}

    raw_lanes = raw.get("miss_lanes") or [0, 0, 0, 0]
    if not isinstance(raw_lanes, list) or len(raw_lanes) != 4:
        raise StatsError("miss_lanes must be a list of 4 numbers")
    miss_lanes = [_num({"v": v}, "v", hi=total) for v in raw_lanes]

    return {
        "total_notes": total,
        "perfect": perfect,
        "good": good,
        "miss": miss,
        "max_combo": max_combo,
        "combo": combo,
        "score": _num(raw, "score", hi=10 ** 9),
        "lanes": _num(raw, "lanes", default=4, lo=1, hi=4),
        "kinds": kinds,
        "miss_lanes": miss_lanes,
        "early": _num(raw, "early", hi=total * 4),
        "late": _num(raw, "late", hi=total * 4),
        "offset_sum_ms": _num(raw, "offset_sum_ms", lo=-1e9, hi=1e9, integer=False),
        "offset_count": _num(raw, "offset_count", hi=total * 4),
    }


# --- Completion maths --------------------------------------------------------
def _floor1(value: float) -> float:
    """Floor to one decimal so a displayed value never overstates the real one
    (69.96% must not be shown, or treated, as 70%)."""
    return math.floor(value * 10 + 1e-9) / 10


def classify(completion: float) -> str:
    """Map a completion percentage to FAILED / PASSED / PERFECT."""
    if completion >= PERFECT_THRESHOLD:
        return "PERFECT"
    if completion >= PASS_THRESHOLD:
        return "PASSED"
    return "FAILED"


def _special_counts(stats: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Return (total, successful, done, missed) over the special note kinds."""
    total = ok = done = missed = 0
    for kind in SPECIAL_KINDS:
        entry = stats["kinds"].get(kind)
        if entry:
            total += entry["total"]
            done += entry["done"]
            missed += entry["missed"]
            ok += max(0, entry["done"] - entry["missed"])
    return total, ok, done, missed


def _ratios(total: int, perfect: int, good: int, max_combo: int,
            spec_ok: float, spec_total: int) -> Dict[str, float]:
    accuracy = (perfect + good) / total
    ratios = {
        "accuracy": accuracy,
        "timing": perfect / total,
        "combo": min(max_combo, total) / total,
        # With no special notes in the run, fall back to overall accuracy.
        "challenges": (spec_ok / spec_total) if spec_total else accuracy,
    }
    return {key: max(0.0, min(1.0, value)) for key, value in ratios.items()}


def _completion(ratios: Dict[str, float]) -> float:
    raw = sum(WEIGHTS[key] * ratios[key] for key in WEIGHTS)
    return _floor1(max(0.0, min(100.0, raw)))


def grade_for(completion: float) -> str:
    if completion >= 100:
        return "S"
    if completion >= 90:
        return "A"
    if completion >= 80:
        return "B"
    if completion >= PASS_THRESHOLD:
        return "C"
    if completion >= 50:
        return "D"
    return "F"


def evaluate_run(stats: Dict[str, Any]) -> Dict[str, Any]:
    """Score a (sanitised) run: completion %, status and per-objective points."""
    total = stats["total_notes"]
    spec_total, spec_ok, _, _ = _special_counts(stats)
    ratios = _ratios(total, stats["perfect"], stats["good"], stats["max_combo"], spec_ok, spec_total)
    completion = _completion(ratios)
    status = classify(completion)
    breakdown = [
        {
            "key": key,
            "label": LABELS[key],
            "ratio": round(ratios[key], 4),
            "points": round(WEIGHTS[key] * ratios[key], 1),
            "max": WEIGHTS[key],
        }
        for key in WEIGHTS
    ]
    return {
        "completion": completion,
        "status": status,
        "passed": status in ("PASSED", "PERFECT"),
        "perfect": status == "PERFECT",
        "unlocks_next": completion >= PASS_THRESHOLD,
        "threshold": PASS_THRESHOLD,
        "grade": grade_for(completion),
        "accuracy_pct": round(ratios["accuracy"] * 100, 1),
        "breakdown": breakdown,
    }


# --- Insight helpers ---------------------------------------------------------
def timing_bias(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Detect a consistent early/late habit from the player's hit offsets."""
    count = stats["offset_count"]
    samples = stats["early"] + stats["late"]
    if count < 6 or samples < 5:
        return None
    average = stats["offset_sum_ms"] / count
    direction = "late" if average > 0 else "early"
    share = max(stats["early"], stats["late"]) / samples
    if abs(average) >= 25 and share >= 0.6:
        return {"direction": direction, "ms": abs(average)}
    return None


def worst_kind(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The note type the player misses most (needs at least 2 misses)."""
    worst = None
    for kind, entry in stats["kinds"].items():
        if entry["missed"] < 2 or entry["total"] == 0:
            continue
        rate = entry["missed"] / entry["total"]
        if rate < 0.25:
            continue
        candidate = {"kind": kind, "missed": entry["missed"], "total": entry["total"], "rate": rate}
        if worst is None or (candidate["rate"], candidate["missed"]) > (worst["rate"], worst["missed"]):
            worst = candidate
    return worst


def worst_lane(stats: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The lane that causes most single-note misses (needs >= 3 misses)."""
    lanes = stats["miss_lanes"][: stats["lanes"]]
    total_misses = sum(lanes)
    if total_misses < 3:
        return None
    lane = max(range(len(lanes)), key=lambda i: lanes[i])
    share = lanes[lane] / total_misses
    if share < 0.45:
        return None
    return {"lane": lane, "key": LANE_KEYS[lane], "count": lanes[lane], "share": share}


def _tip(title: str, text: str) -> Dict[str, str]:
    return {"title": title, "text": text}


# --- Live evaluation ---------------------------------------------------------
def evaluate_live(stats: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate a run that is still in progress.

    Notes that have not been played yet count as zero, so the percentage climbs
    as the player performs. Also reports the best result still reachable and
    how many more perfect hits are needed to reach the pass mark.
    """
    total = stats["total_notes"]
    perfect, good, miss = stats["perfect"], stats["good"], stats["miss"]
    done = perfect + good + miss
    remaining = max(0, total - done)

    ev = evaluate_run(stats)
    spec_total, spec_ok, spec_done, spec_missed = _special_counts(stats)

    # Best case: every remaining note is hit perfectly in one unbroken streak.
    best_ratios = _ratios(
        total, perfect + remaining, good,
        max(stats["max_combo"], stats["combo"] + remaining),
        spec_total - spec_missed, spec_total,
    )
    best_possible = _completion(best_ratios)
    can_still_pass = best_possible >= PASS_THRESHOLD

    # Fewest additional perfect hits (rest missed) that would reach the pass mark.
    needed: Optional[int] = None
    if ev["completion"] >= PASS_THRESHOLD:
        needed = 0
    elif can_still_pass:
        spec_remaining = max(0, spec_total - spec_done)
        share = (spec_remaining / remaining) if remaining else 0.0
        for k in range(1, remaining + 1):
            ratios = _ratios(
                total, perfect + k, good,
                max(stats["max_combo"], stats["combo"] + k),
                spec_ok + k * share, spec_total,
            )
            if _completion(ratios) >= PASS_THRESHOLD:
                needed = k
                break

    tip = _live_tip(stats, ev, done, remaining, can_still_pass, needed)
    return {
        "completion": ev["completion"],
        "status": ev["status"],
        "threshold": PASS_THRESHOLD,
        "grade": ev["grade"],
        "breakdown": ev["breakdown"],
        "notes_done": done,
        "notes_remaining": remaining,
        "best_possible": best_possible,
        "can_still_pass": can_still_pass,
        "perfect_hits_needed": needed,
        "tip": tip,
    }


def _live_tip(stats: Dict[str, Any], ev: Dict[str, Any], done: int, remaining: int,
              can_still_pass: bool, needed: Optional[int]) -> Dict[str, str]:
    if done == 0:
        return _tip("Get set", "Watch the strike line and press each key as its gem touches it. "
                               "You need %d%% completion to unlock the next level." % PASS_THRESHOLD)
    if not can_still_pass and remaining > 0:
        return _tip("This run can't reach %d%%" % PASS_THRESHOLD,
                    "Even perfect play from here tops out at %.1f%%. Finish it as practice, "
                    "or press Restart for a clean attempt." % _floor1(evaluate_live_best(stats)))
    bias = timing_bias(stats)
    if bias and stats["miss"] + stats["good"] >= 2:
        if bias["direction"] == "late":
            return _tip("You're hitting late",
                        "On average you press about %d ms after the beat. Try pressing a touch earlier." % round(bias["ms"]))
        return _tip("You're hitting early",
                    "On average you press about %d ms before the beat. Wait for the gem to reach the line." % round(bias["ms"]))
    kind = worst_kind(stats)
    if kind:
        return _tip("Watch your %s" % KIND_NAMES[kind["kind"]],
                    "You've missed %d of %d. %s" % (kind["missed"], kind["total"], KIND_TIPS[kind["kind"]]))
    lane = worst_lane(stats)
    if lane:
        return _tip("Lane %s is slipping" % lane["key"],
                    "Most of your misses are in the %s lane. Keep that finger ready above the key." % lane["key"])
    if needed == 0:
        return _tip("Above the pass line", "You're over %d%%. Keep your streak alive to lock it in." % PASS_THRESHOLD)
    if needed:
        return _tip("Stay clean", "About %d more perfect hit%s would put you over %d%%."
                    % (needed, "" if needed == 1 else "s", PASS_THRESHOLD))
    return _tip("Keep going", "Stay relaxed and keep matching the beat.")


def evaluate_live_best(stats: Dict[str, Any]) -> float:
    total = stats["total_notes"]
    done = stats["perfect"] + stats["good"] + stats["miss"]
    remaining = max(0, total - done)
    spec_total, _, _, spec_missed = _special_counts(stats)
    ratios = _ratios(total, stats["perfect"] + remaining, stats["good"],
                     max(stats["max_combo"], stats["combo"] + remaining),
                     spec_total - spec_missed, spec_total)
    return _completion(ratios)


# --- Final feedback ----------------------------------------------------------
def build_feedback(stats: Dict[str, Any], ev: Dict[str, Any], *,
                   level: int = 0,
                   history: Optional[Dict[str, Any]] = None,
                   next_level: Optional[Dict[str, Any]] = None,
                   is_final_level: bool = False,
                   song_level: int = 0,
                   next_song_level: Optional[int] = None) -> Dict[str, Any]:
    """Create the post-run feedback shown in the AI coach panel.

    ``history`` (optional) describes earlier attempts on this level::

        {"previous": [{"completion": 61.2, "score": 3400}, ...],  # newest first
         "attempts": 3, "best_completion": 64.0}
    """
    completion = ev["completion"]
    status = ev["status"]
    total = stats["total_notes"]
    song_run = song_level > 0
    free_play = level == 0 and not song_run

    # The objective that cost the most points is the one to improve.
    weakest = max(ev["breakdown"], key=lambda item: item["max"] - item["points"])
    lost = weakest["max"] - weakest["points"]

    # ---- headline & summary
    if status == "PERFECT":
        headline = "Perfect completion!"
        summary = "Flawless: every note landed on the beat with an unbroken streak."
    elif status == "PASSED":
        headline = "Song level passed" if song_run else "Level passed" if not free_play else "Nice run"
        summary = "You completed %.1f%% (pass mark %d%%)." % (completion, PASS_THRESHOLD)
    else:
        headline = "Not quite - try again"
        summary = ("You reached %.1f%%, which is %.1f points short of the %d%% pass mark."
                   % (completion, PASS_THRESHOLD - completion, PASS_THRESHOLD))
    if status != "PERFECT" and lost >= 3:
        summary += " The biggest room to grow is %s (%.1f of %d points)." % (
            weakest["label"].lower(), weakest["points"], weakest["max"])
    if song_run:
        if next_song_level:
            summary += " Song Level %d is now unlocked - the AI will raise the challenge." % next_song_level
        elif is_final_level:
            summary += " That clears the final adaptive song level!"
        else:
            summary += " Replay this song level to improve before moving on."
    elif free_play:
        summary += " Free play is recorded in your history but does not unlock levels."
    elif status != "FAILED":
        if next_level:
            summary += " Level %d (%s) is now unlocked." % (next_level["level"], next_level["name"])
        elif is_final_level:
            summary += " That clears the final level - every level is complete!"
    else:
        summary += " The next level stays locked until you reach %d%%." % PASS_THRESHOLD

    # ---- strengths
    ratios = {item["key"]: item["ratio"] for item in ev["breakdown"]}
    strengths: List[str] = []
    hits = stats["perfect"] + stats["good"]
    if ratios["accuracy"] >= 0.9:
        strengths.append("Excellent accuracy: %d of %d notes hit." % (hits, total))
    elif ratios["accuracy"] >= 0.75:
        strengths.append("Solid accuracy: %d of %d notes hit." % (hits, total))
    if ratios["timing"] >= 0.7:
        strengths.append("Sharp timing: %d perfect hits." % stats["perfect"])
    if ratios["combo"] >= 0.6:
        strengths.append("Strong streak: %d notes in a row." % stats["max_combo"])
    if ratios["challenges"] >= 0.9 and any(k in stats["kinds"] for k in SPECIAL_KINDS):
        strengths.append("Special notes (x2, x3, holds, chords) handled cleanly.")
    strengths = strengths[:2]

    # ---- tips, most relevant first
    tips: List[Dict[str, str]] = []

    kind = worst_kind(stats)
    if kind:
        tips.append(_tip("Practice your %s" % KIND_NAMES[kind["kind"]],
                         "You missed %d of %d. %s" % (kind["missed"], kind["total"], KIND_TIPS[kind["kind"]])))

    bias = timing_bias(stats)
    if bias:
        if bias["direction"] == "late":
            tips.append(_tip("You tend to hit late",
                             "Your presses land about %d ms after the beat on average. Press slightly earlier - "
                             "aim for the moment the gem touches the line, not after it." % round(bias["ms"])))
        else:
            tips.append(_tip("You tend to hit early",
                             "Your presses land about %d ms before the beat on average. Hold back a fraction "
                             "and let the gem reach the line first." % round(bias["ms"])))

    lane = worst_lane(stats)
    if lane:
        tips.append(_tip("Lane %s needs work" % lane["key"],
                         "%d of your single-note misses (%d%%) were in the %s lane. Keep that finger "
                         "hovering over the key between notes." % (lane["count"], round(lane["share"] * 100), lane["key"])))

    if ratios["accuracy"] >= 0.8 and ratios["timing"] < 0.5 and hits:
        tips.append(_tip("Turn good hits into perfect hits",
                         "You hit most notes but only %d%% of your hits were Perfect (within 50 ms). "
                         "Perfect hits are worth double the points and fill the timing bar." % round(stats["perfect"] / hits * 100)))
    elif ratios["accuracy"] < 0.7:
        easier = " Try Easy mode (2 lanes) to rebuild your accuracy first." if stats["lanes"] > 2 else ""
        tips.append(_tip("Accuracy first",
                         "You hit %d of %d notes. Focus on hitting every note before worrying about speed or "
                         "style.%s" % (hits, total, easier)))

    if ratios["combo"] < 0.4 and stats["miss"] > 0:
        tips.append(_tip("Protect your streak",
                         "Your longest streak was %d. The score multiplier grows by 1x every 10 hits in a row "
                         "(up to 4x), and a single miss resets it." % stats["max_combo"]))

    if history and history.get("previous"):
        last = history["previous"][0]["completion"]
        diff = completion - last
        if diff >= 1:
            tips.append(_tip("You're improving",
                             "Up %.1f points from your last attempt (%.1f%%). Keep that momentum." % (diff, last)))
        elif diff <= -1:
            best = history.get("best_completion")
            extra = " Your best on this level is %.1f%%." % best if best is not None else ""
            tips.append(_tip("Slightly off your pace",
                             "Down %.1f points from your last attempt (%.1f%%).%s" % (-diff, last, extra)))

    attempts_before = (history or {}).get("attempts", 0)
    if status == "FAILED" and attempts_before >= 2:
        tips.append(_tip("Stuck on this level?",
                         "That was attempt %d. Take a short break, then replay%s." % (
                             attempts_before + 1,
                             " on Easy mode to learn the patterns" if stats["lanes"] > 2 else " and focus on one weak spot at a time")))

    if status == "FAILED" and completion >= PASS_THRESHOLD - 10 and not tips:
        tips.append(_tip("So close", "You're within 10 points of the pass mark - one cleaner run should do it."))

    if status == "PERFECT":
        tips = [_tip("Raise the challenge",
                     "Go for a harder difficulty." if stats["lanes"] < 4 else "Move on and try for a perfect on the next level.")]
    elif not tips:
        tips.append(_tip("Stay on the line", "Keep your eyes on the strike line and press as the gem touches it."))

    return {
        "headline": headline,
        "summary": summary,
        "strengths": strengths,
        "tips": tips[:3],
        "focus": weakest["label"],
    }
