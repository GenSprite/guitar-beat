# GUITAR-BEAT

ESP32 (4 buttons) -> Wi-Fi WebSocket -> Node.js server -> browser game.

## Wiring
Buttons: GPIO 13, 12, 14, 27 -> GND (INPUT_PULLUP, no resistor needed)
Optional LEDs: GPIO 26, 25, 33, 32 (220 ohm each) | Buzzer: GPIO 15

## Run
1. Install Node.js and Python 3.8+ (Python needs **no extra packages** - it only uses the standard library).
2. `npm install`
3. `npm start`  -> open http://localhost:3000 (test with keys D F J K). This also starts the Python backend automatically.
4. Arduino IDE: install "esp32 by Espressif" board + "WebSockets" (Markus Sattler) library.
5. Open esp32_guitar_beat/esp32_guitar_beat.ino, edit ssid / pass / host (PC IP), upload.

PC and ESP32 must be on the same Wi-Fi. Allow Node.js through Windows Firewall (private network).

## Gameplay
Play the four-lane fret highway by matching each note to its lane as it reaches the strike line. The four buttons act as fret-and-strum controls, so press the matching key/button on the beat:
- Plain lane notes: press the matching key once.
- `HOLD`: press and keep the lane button down until the bar finishes.
- `ALL 4`: press D, F, J, and K together.

Build a streak to increase the score multiplier, up to 4x. The keyboard supports tap, hold and chord patterns. For ESP32 holds, upload the current sketch so it sends both button-down and button-up events.

After loading the song and matching chart, choose a starting difficulty: **Easy** starts with 2 lanes (D/F), **Medium** starts with 3 lanes (D/F/J), and **Hard** starts with all 4 lanes (D/F/J/K). Each successful song level adds another active lane up to 4; speed and timing windows continue getting stricter through the later levels.

## Song mode and exact guitar charts

For exact guitar-part synchronization, choose the song audio and a matching guitar chart JSON or MIDI file, then press **Play song**. The audio is the master clock; each chart note uses its exact timestamp in seconds from the start of the audio. MIDI tempo is imported automatically; the BPM field is not used once a chart is loaded.

MIDI files are converted into four lanes by pitch range. That is useful for a quick playable conversion, but a MIDI made from a full mixed song is not guaranteed to contain only the guitar part. A guitar-specific MIDI track or authored chart is required for true guitar-only accuracy.

Chart format:

```json
{
  "title": "My Song Guitar",
  "bpm": 128,
  "notes": [
    { "time": 2.315, "kind": "tap", "lane": 0 },
    { "time": 3.020, "kind": "hold", "lane": 2, "duration": 0.850 },
    { "time": 4.125, "kind": "chord", "lanes": [0, 2] }
  ]
}
```

`lane` values map to D/F/J/K as 0/1/2/3. Use `lanes` for a chord and `duration` for a hold.

## AI progression + SQLite records (new)

```
browser (index.html) --/api/game/*--> Node server (proxy) --> Python backend (backend/app.py) --> data/guitar_beat.db
```

- **Levels:** An uploaded song + matching chart starts a 5-level adaptive progression for that song. Easy progresses 2 → 3 → 4 lanes, Medium progresses 3 → 4 lanes, and Hard begins at 4 lanes. The uploaded song keeps its exact chart timestamps; the AI increases note speed and timing pressure at every later level. Failed runs repeat the current song level.
- **Completion %** (computed by `backend/ai_engine.py`): Accuracy 60 + Perfect timing 15 + Streak 15 + Special notes (HOLD/chord) 10. It updates live while you play.
- **Rules:** 0-69.9% Failed (next level locked, retry) · 70-99.9% Passed (next level unlocks) · 100% Perfect (unlocks + perfect score recorded).
- **AI coach:** live tips during the run and personalised feedback afterwards (weak note types, weak lanes, early/late habit, progress vs your last attempt). It is a rule-based analysis engine - no internet or API key needed.
- **Database:** `data/guitar_beat.db` is created automatically. Tables: `players`, `level_progress`, `song_progress`, and `attempts` (full score history with song title, song level and date/time). Existing databases are upgraded automatically. The in-game **Leaderboard** ranks saved runs from all players by score; **Player records** show the current player's history; **Database tables** opens a read-only viewer for the raw local SQLite tables.
- **Data use:** Before creating or selecting a player, the profile dialog requires agreement that the name and gameplay data are used only for scoring, progression, AI feedback and this school project. Records stay in the local SQLite database.
- **Resilience:** if Python/SQLite is unavailable an uploaded song can still be played without saving; if a save fails the result is still shown and the song remains playable.

Useful commands:
- `npm run backend` - run the Python backend on its own (then start Node with `GUITAR_BEAT_NO_PYTHON=1`)
- `npm test` - run the backend tests (70% rule, AI engine, SQLite, HTTP API)
- Environment: `PYTHON` (interpreter), `GUITAR_BEAT_PY_PORT` (default 5001), `GUITAR_BEAT_DB` (database path)

## Next
- Train a scikit-learn model on the `details` JSON stored with every attempt to predict skill level and adjust SPEED / note density
