# GUITAR-BEAT

ESP32 (4 buttons) -> Wi-Fi WebSocket -> Node.js server -> browser game.

## Wiring
Buttons: GPIO 13, 12, 14, 27 -> GND (INPUT_PULLUP, no resistor needed)
Optional LEDs: GPIO 26, 25, 33, 32 (220 ohm each) | Buzzer: GPIO 15

## Run
1. `npm install`
2. `npm start`  -> open http://localhost:3000 (test with keys D F J K)
3. Arduino IDE: install "esp32 by Espressif" board + "WebSockets" (Markus Sattler) library.
4. Open esp32_guitar_beat/esp32_guitar_beat.ino, edit ssid / pass / host (PC IP), upload.

PC and ESP32 must be on the same Wi-Fi. Allow Node.js through Windows Firewall (private network).

## Gameplay
Play the four-lane fret highway by matching each note to its lane as it reaches the strike line. The four buttons act as fret-and-strum controls, so press the matching key/button on the beat:
- Plain lane notes: press the matching key once.
- `HOLD`: press and keep the lane button down until the bar finishes.
- `ALL 4`: press D, F, J, and K together.

Build a streak to increase the score multiplier, up to 4x. The keyboard supports tap, hold and chord patterns. For ESP32 holds, upload the current sketch so it sends both button-down and button-up events.

At startup, choose a difficulty: **Easy** uses 2 lanes (D/F), **Medium** uses 3 lanes (D/F/J), and **Hard** uses all 4 lanes (D/F/J/K). Higher-lane notes are removed or simplified for easier modes while preserving the song timing.

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

`lane` values map to D/F/J/K as 0/1/2/3. Use `lanes` for a chord, `duration` for a hold, and `gap` for the spacing between repeated taps.

## Rounds, pause, restart and AI coach

- **Pause** (button, `P` or `Esc`): freezes the song, notes, timer and score and ignores all hits (keyboard, touch and ESP32). **Resume** counts down 3-2-1 and continues exactly where it stopped. **Restart** (also in the pause menu) resets score, streak, accuracy, misses, inputs, chart and song to the start.
- **Rounds 1-10**: clear a round to unlock the next one. Default requirements (they scale with note count and round): accuracy 80%, score 5,000 (for a 48-note chart), and at least 70% of notes hit. Misses are still tracked in the results and AI coach but do not block progression. Progress and best scores are saved in `localStorage` (key `guitarBeat.progress.v1`); use *Reset saved progress* in the side panel to start over.
- **Per-round difficulty**: faster notes (up to 1.6x), tighter Good/Perfect windows, and for the built-in practice chart more notes plus more HOLD, DOUBLE, TRIPLE and CHORD patterns. For a loaded song chart the note timestamps are never changed (exact sync), so rounds there use speed and timing windows, and the AI can remove some plain taps for beginners.
- **AI coach**: after every round a local model scores accuracy, average reaction error, Perfect/Good ratio, streak consistency and misses, rates the player Beginner / Intermediate / Advanced, writes feedback, and tunes the next round (-12% speed and -20% notes, normal, or +10% speed with trickier patterns). It runs in the browser; `POST /api/results` also receives `round`, `passed` and `aiTier` for the future scikit-learn model.

## Next
- POST /api/results already receives end-of-song stats -> save to MySQL
- Train scikit-learn model, predict skill level, adjust SPEED / note density
