# Jitter Click

A self-contained autoclicker and input-automation tool for Windows. It records and replays clicks,
key presses and color-finding steps. Mouse movement and timing are made to look natural: the cursor
glides along curved paths, every delay has random jitter added, and the cursor can idle in chosen
areas while it waits.

It's one Python file with no dependencies beyond the standard library (`tkinter` + `ctypes`).

## Requirements and running

- Windows 10 or 11
- Python 3.8+ (with tkinter, which the standard python.org installer includes)

```bash
python autoclicker.py
```

The window stays on top of other windows. Rest the mouse on any setting or button to see a tooltip
explaining it.

## Hotkeys

The hotkeys are global. The key press goes only to the clicker, not to whatever app has focus, so
F6 won't also jump to a browser's address bar and F10 won't open a menu.

| Key | Action |
|-----|--------|
| **F6** | Start / stop |
| **F7** | Start / stop recording into the open sequence |
| **F8** | Set the anchor to the mouse position (on the **Idle areas** tab: add an idle area) |
| **F9** | Add a color step using the pixel under the mouse |
| **F10** | Pause / resume |

If another program has already claimed one of these keys, the clicker still responds to it, and a
note at startup says so. That key then also reaches the focused app.

## Play modes

Choose one in the bar at the bottom of the window:

- **Main sequence** plays the **Main** sequence in a loop. Sub-sequences play wherever Main runs
  them.
- **Anchor clicks** clicks over and over around the anchor point (set on the Settings tab or with
  F8), within the click radius.

Starting again after a stop begins from the top of Main.

## Sequences and steps (Sequences tab)

### Step types

| Step | How to add it | What it does |
|------|---------------|--------------|
| **click** | Record, then left-click on another window | Glides to the point and clicks within the click radius |
| **shift-click** | Record, then click while holding Shift | Same, holding Shift: it goes down just before the click and comes up just after |
| **key / type** | Record, then press keys on another window | A single key, a combo (e.g. `Ctrl+S`), or a run of typed keys. Presses within 2 s of each other join one step |
| **find color** | F9 over a pixel | Takes a screenshot, finds the largest patch of that color, glides to it and clicks on it |
| **wait** | The *Wait* row, then **Add wait** | Waits a set time (+ jitter). Replaces the normal delay at that point |
| **run** | The *Run* row, then **Add run** | Plays another sequence (a sub-sequence) |

Recording ignores clicks and typing in the clicker's own window. It also ignores the program's
hotkeys. A modifier tapped on its own (for example a lone Shift) is recorded as a key press.

### Sub-sequences and repeats

- **Main** is the sequence that plays. **New…** creates named sub-sequences.
- **Rename…** also updates every Run step that uses the sub-sequence.
- **Delete** asks first, and also removes the Run steps that use the sub-sequence.
- **Duplicate** copies the selected sequence as a new sub-sequence.
- Select a sequence on the left to open its steps for editing. Recording, F9 and the add buttons
  all work on the open sequence.
- Any step can repeat: select steps, enter a count in **Repeat selected ×**, then click
  **Set repeats**. The list shows repeats as `×N`.
- Sub-sequences can run other sub-sequences. The Run dropdown only offers sequences that can't end
  up running the open one, so loops can't be built. A loop that arrives in a hand-edited file is
  skipped during playback.

Example: a sub-sequence `Loot` (click, press 1, click elsewhere), with Main set to
`run Loot ×5`, `find color #FF8C00 ×2`, `run Loot ×8`.

### Editing steps

- **Reorder:** drag a step, use **▲ / ▼**, or press **Ctrl+↑ / Ctrl+↓**. Several selected steps can
  move together.
- **Insert:** new waits and runs go after the selected step, or at the end if nothing is selected.
- **Remove** deletes the selected steps; **Clear** empties the open sequence.
- While playing, **▶** marks the step in progress and the sequences that are playing. The status
  line shows where playback is, for example `Typing (Main 1 [2/5] › Loot 2)`.

### Record my timing

Tick **Record my timing** next to Record to save how long you took before each click, key step and
color step (shown as `⏱ 601 ms`). Within a typed run, your spacing between keys is saved too.

On playback, your recorded time is the base delay before that step, and jitter only adds to it.
Typed runs keep your rhythm. To go back to the normal delays without losing the recorded times,
untick **Use recorded timing** on the Settings tab.

## Settings (Settings tab)

Each timing has a **minimum** and a **jitter**. The jitter adds between 0 and that amount on top,
and results never go below the minimum. Most values land near the minimum; longer ones are rarer.

| Group | Settings |
|-------|----------|
| **Clicking** | Anchor X/Y, click radius, min delay between steps, delay jitter |
| **Keys** | Min key hold, key hold jitter, min delay between keys, key delay jitter |
| **Movement** | Min move speed, speed jitter (only ever faster), max path curve |
| **Color steps** | Tolerance (± per R/G/B channel), search radius (0 = all screens), wait timeout |
| **Breaks** | Take random breaks; break chance, min break length, break jitter. By default the chance is rolled after every step; tick **Only at the end of Main** to roll it once per loop, just before Main starts over (anchor clicks still roll per click) |
| **Control** | Stop if I move the mouse, use recorded timing, set anchor |

### How input looks natural

- **Mouse movement:** the cursor glides along a curved path that bows to a random side, reaches
  peak speed early and slows into the target. It has slight hand tremor along the way. Longer moves
  take longer, but not proportionally (Fitts's law).
- **Overshoot:** on longer moves it sometimes overshoots and corrects. This always fits inside the
  planned time, so clicks land on schedule.
- **Click position:** clicks cluster toward the middle of the click radius and never land outside
  it.
- **Mouse buttons:** each click holds the button down for a natural, varying time.
- **Keys:** each key is held for a varying time. In fast typing, the next key goes down before the
  previous one comes up, but never in a way that changes what gets typed. Combos press their
  modifiers in order and release them in reverse.
- **Real input events:** mouse and keyboard events carry virtual-key and scan codes, like physical
  devices send. Move targets are kept on a real monitor, so aiming past a screen edge never leaves
  the cursor somewhere unexpected.

## Idle areas (Idle areas tab)

While playback is waiting for the next step, the cursor can wander to an idle area and drift around
it slowly, instead of sitting still.

- **Adding an area:** hover where you want it and press **F8** with this tab open. **Hover radius**
  sets how far the cursor drifts from that spot.
- **Click before idling:** with this ticked, adding an area takes two F8 presses: first over a spot
  to click, then over the spot to idle around. On playback the cursor clicks the first spot, then
  goes and drifts around the second.
- **Changing areas:** select areas to see their settings. **Apply to selected** changes their hover
  radius, and unticking the click box removes their click spot.
- **When it idles:** choose any of delays between steps, wait steps and breaks. Breaks are off by
  default, so breaks keep the mouse still.
- **Hovering settings:** min hover time, drift speed, and min pause between drifts (+ jitter).
- **Never delays a step:** idling only starts if there's time to reach an area, hover for the
  minimum time, and still get back for the next step on schedule. The return trip is budgeted at
  its worst case. Before a key step, the cursor returns to where the last click left it.
- While idling, the area in use is highlighted and the status line says so.

## Pause, stop and safety

- **Pause (F10)** resumes on the exact step it paused on, even inside nested sub-sequences.
  - A paused move restarts from wherever the mouse is.
  - A paused typed run continues from the next key.
  - A paused wait keeps its remaining time.
  - The mouse is yours while paused.
- **Stop if I move the mouse** stops playback if the cursor is more than 3 px from where the
  clicker left it.
- Keys and Shift are always released on stop or pause, never left held down.

## Saving and loading

**Save…** and **Load…** (bottom bar) use a `.json` pattern file holding all sequences, idle areas
and settings. Older pattern files still load: a file from before sub-sequences existed becomes the
Main sequence, and old idle areas keep their click behaviour.

## Notes

- Windows only, since it uses the Win32 API through `ctypes`.
- Clicks made by "click before idling" areas are real clicks and count in the status line total.
- Use it only with software where automation is permitted.
