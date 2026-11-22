# GUITAR-BEAT: 4-Button Controller Guide

## 1. Exact GPIO Pin Assignments

| Button | Lane | Game key | ESP32 pin | Other leg |
|---|---|---|---|---|
| Button 1 | 0 | D | GPIO 13 | GND |
| Button 2 | 1 | F | GPIO 12 | GND |
| Button 3 | 2 | J | GPIO 14 | GND |
| Button 4 | 3 | K | GPIO 27 | GND |

INPUT_PULLUP is correct here: no external resistor and no 3V3 wire.
Unpressed = HIGH, pressed = LOW.

## 2. Wiring Diagram

```
                ESP32 DevKit
             +-----------------+
             |   [ USB port ]  |
  Btn1 (D) --| GPIO13          |
  Btn2 (F) --| GPIO12          |
  Btn3 (J) --| GPIO14          |
  Btn4 (K) --| GPIO27          |
  GND rail --| GND             |
             +-----------------+

  One button (same for all four):

   GPIO pin -------o  [ button ]  o------- GND rail
        (pressed = GPIO connects to GND = reads LOW)

  Breadboard, one button (top view):

            col 10     col 12
   row E    [ A ]=====[ B ]       A: GPIO wire
            ------ center gap ------
   row F    [ C ]=====[ D ]       D: GND wire   (A and D are diagonal)
```

The 4 buttons are not connected to each other. Only the GND rail is shared.

## 3. Connecting Each Button

Unplug USB first.

1. Jumper from ESP32 GND pin to the breadboard GND rail.
2. Place the 4 buttons across the center gap, 1-2 empty columns apart.
3. Button 1 (D): one leg -> GPIO 13. Diagonal leg -> GND rail.
4. Button 2 (F): one leg -> GPIO 12. Diagonal leg -> GND rail.
5. Button 3 (J): one leg -> GPIO 14. Diagonal leg -> GND rail.
6. Button 4 (K): one leg -> GPIO 27. Diagonal leg -> GND rail.
7. Make sure no wire goes to 3V3 or 5V.

Notes:
- Two legs on the same side of a 4-pin button are already connected inside,
  so always use diagonal legs. If a button reads "always pressed", rotate it 90 degrees.
- GPIO 12 is a boot pin: do not hold any button while plugging in the USB.

## 4. Code

- button_test/button_test.ino : Serial Monitor test, no Wi-Fi. Use this FIRST.
- esp32_guitar_beat/esp32_guitar_beat.ino : the game sketch (Wi-Fi + WebSocket).
  Edit ssid, pass, host (PC IPv4 from ipconfig) before uploading.

Each sketch must stay inside a folder with the same name as the .ino file.

## 5. Function of Each Button

Each button is one lane of the fret highway.

| Button | Lane | Keyboard equivalent | Message sent to game |
|---|---|---|---|
| 1 | 0 (leftmost) | D | {"type":"down"/"up","lane":0} |
| 2 | 1 | F | {"type":"down"/"up","lane":1} |
| 3 | 2 | J | {"type":"down"/"up","lane":2} |
| 4 | 3 (rightmost) | K | {"type":"down"/"up","lane":3} |

In the game:
- Plain note: press that lane's button once.
- x2 / x3: tap that lane's button 2 or 3 times to the beat.
- HOLD: press and keep held until the bar ends (needs the down and up messages).
- ALL 4: press all four buttons together.
- Difficulty: Easy uses buttons 1-2, Medium uses 1-3, Hard uses all 4.

## 6. Testing Each Button with the Serial Monitor

1. Upload button_test/button_test.ino.
2. Tools -> Serial Monitor, 115200 baud, then press EN on the ESP32.
   You should see: === GUITAR-BEAT button test ===

One by one:

| Action | Expected output |
|---|---|
| Press Button 1 | Button 1 (D, GPIO 13) PRESSED  \| total presses: 1 |
| Release Button 1 | Button 1 (D) RELEASED \| held for ~230 ms |
| Press Button 2 | Button 2 (F, GPIO 12) PRESSED ... |
| Press Button 3 | Button 3 (J, GPIO 14) PRESSED ... |
| Press Button 4 | Button 4 (K, GPIO 27) PRESSED ... |

Debounce test: one press must give exactly one PRESSED line.
If you get doubles, check wiring or raise DEBOUNCE_MS to 30.

Hold test: hold a button for 2 seconds. Expect "held for ~2000 ms".

Simultaneous test (no interference):
1. Press Button 1 + 2 together: "Now held" shows D:1 F:1 J:0 K:0.
2. Add Button 3, then Button 4: D:1 F:1 J:1 K:1.
3. Release one at a time: each goes to 0 without affecting the others.

## 7. Why the Buttons Don't Interfere

- Each button has its own GPIO pin and its own state, timer, and counter
  (arrays indexed by lane), so one button never changes another's data.
- The loop checks all 4 pins every pass, with no delay() calls, so presses
  are never missed while another button is held.
- Only GND is shared, which does not cause interference.
- Debounce is per button (lastChange[i]), so bouncing on one button
  does not block the others.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| No output when pressed | Check that button's GPIO wire and GND wire |
| Always "PRESSED" | Same-side legs were used. Rotate the button 90 degrees |
| Two buttons trigger together | A jumper is in the wrong column, or two legs share a breadboard row. Re-check each button's column |
| Double PRESSED per press | Loose wire, or raise DEBOUNCE_MS to 30 |
| Garbage characters | Set Serial Monitor to 115200 baud |
| Boot loop / odd boot message | Unplug, release all buttons, replug |
| Upload "Connecting..." timeout | Hold BOOT while it connects, and use a data USB cable |
