"""Level definitions and progression rules for GUITAR-BEAT.

Everything that decides *what a level is* and *what counts as passing* lives
here, so the AI engine, the database layer and the API all agree.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# --- Progression rules -------------------------------------------------------
PASS_THRESHOLD = 70.0      # minimum completion % that unlocks the next level
PERFECT_THRESHOLD = 100.0  # completion % that counts as a perfect completion

# --- Levels ------------------------------------------------------------------
# Each level replays the built-in track (the same note patterns the game has
# always used) at a different tempo and length. `notes` is how many notes of the
# built-in 48-note pattern are used, `bpm` sets the spacing between notes.
LEVELS: List[Dict[str, Any]] = [
    {"level": 1, "name": "Warm-Up",      "bpm": 60,  "notes": 16,
     "description": "A short, slow run to learn the patterns."},
    {"level": 2, "name": "Groove",       "bpm": 70,  "notes": 24,
     "description": "A little faster and longer."},
    {"level": 3, "name": "Rhythm Run",   "bpm": 80,  "notes": 32,
     "description": "Steady tempo with every pattern in play."},
    {"level": 4, "name": "Fast Fingers", "bpm": 90,  "notes": 40,
     "description": "Quicker spacing with tighter timing."},
    {"level": 5, "name": "Full Shred",   "bpm": 100, "notes": 48,
     "description": "The full track at full speed."},
]

MAX_LEVEL = len(LEVELS)


def get_level(level: int) -> Optional[Dict[str, Any]]:
    for entry in LEVELS:
        if entry["level"] == level:
            return entry
    return None
