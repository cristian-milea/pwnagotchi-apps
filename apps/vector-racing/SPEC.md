# Vector Racing — cartridge spec (v1)

**Status:** design approved 2026-05-28. Implementation-ready.
**Stem:** `vector_racing` · **Manifest name:** `vector-racing` · **Icon:** `VR` · **Category:** `fun`

A turn-based racing cartridge based on the pen-and-paper game *Racetrack*. The
phone is a 9-way acceleration pad; the e-ink draws a top-down circuit, the car,
its trail, and the projected next move. No FPS, no animation — one tap = one
push = one redraw. Reuses maze's proven patterns: on-device procedural
generation, daily seed via `date.toordinal()`, BFS-computed optimum, persisted
best, `published_state()` for the phone.

---

## 1. Core model

State is integer lattice points. The car has position `p = (px, py)` and
velocity `v = (vx, vy)`.

Each turn the player picks an acceleration `a ∈ {-1,0,1}²` (9 choices — the
button pad). Resolution:

```
v ← clamp(v + a, ±VMAX)          # VMAX = 3, per-component
target ← p + v                   # where the car wants to land this turn
# the straight segment p → target must lie entirely on-track:
cells = supercover_line(p, target)   # every lattice cell the segment touches
if all cells on-track and target on-track:
    p ← target                       # clean move
else:
    crash(mode)                      # see §4
```

- `VMAX = 3` per component. Chosen so the usable speed range (1–3) stays
  meaningful on a small circuit without instantly overshooting the band.
- **Coast** = the center button = `a = (0,0)`: velocity unchanged.
- Collision test is on the **segment**, not just the endpoint: a fast move that
  skims across a corner clips the wall even if `target` itself is on-track.
  Use a supercover line (all cells the segment passes through), not plain
  Bresenham, so corner-cutting is caught.

---

## 2. Track: procedural closed circuit

The course is a **closed band** (a loop with width), generated on-device from a
seed. Generation must be robust — a malformed track is unplayable — so we do
**not** grow a random maze-style loop. Instead:

**Perturbed-ring generation.**
1. Define a center and a base loop (ellipse / rounded-rect) sized to the logical
   lattice (see §6 for sizing).
2. Sample the loop at N angular steps. Perturb each sample's radius by
   seeded smooth noise (e.g. sum of a few low-frequency sinusoids with
   seeded phases/amplitudes) to make the outer boundary distinct per track.
3. The inner boundary is the outer boundary shrunk by the **track width**
   (TRACK_W lattice cells, default 3; clamp 2–4). This guarantees a continuous
   band of controlled width — no pinch-offs.
4. Rasterize: a lattice cell is **on-track** iff it lies between the inner and
   outer boundaries.

**Validation (reseed on failure, retry cap ~64):**
- Band is everywhere ≥ `TRACK_W_MIN` (2) cells wide — no choke points narrower
  than the car can thread.
- On-track region is a single connected loop (one BFS component, has a hole).
- **At least 2 "straights"**: runs of the centerline where direction stays
  roughly constant for ≥ `MIN_STRAIGHT` (default 6) lattice steps. This is what
  makes speed 2–3 usable; without it the speed dimension is dead. If a seed
  fails this, reseed.

**Start/finish.** A fixed line segment crossing the band, placed on the longest
straight, perpendicular to the centerline direction there. The car starts on it
at `v = (0,0)`, facing along the forward centerline direction.

**Seeds.**
- *Daily* mode: `seed = date.today().toordinal()` — everyone, every device, same
  track that day (matches maze).
- *Random* mode: fresh seed each new race.

---

## 3. Laps & direction

- Player picks **laps ∈ {1, 2, 3}** before the race (default 2).
- A lap counts when the car's move segment crosses the start/finish line in the
  **forward** direction (positive dot product of the move against the line's
  stored forward normal). Backward crossings are ignored (not decremented) —
  cheap anti-cheese; driving backward only wastes moves.
- Completing `laps` forward crossings → race finished.

---

## 4. Crash rule (player-selected before Start)

The player chooses one of two modes from the phone before starting a race. The
mode does **not** affect the computed optimum (§5) — optimum is always the clean
ideal.

- **Safe** — on a wall hit: place the car at the last on-track lattice point
  along the attempted segment, set `v ← (0,0)`, increment `crashes`. The turn is
  consumed (counts as a move). Play continues.
- **Hardcore** — on a wall hit: reset the car to the start/finish line at
  `v ← (0,0)`. Completed laps are kept; the in-progress lap must be redone. The
  move counter keeps climbing — the lost progress *is* the penalty. `crashes`
  increments.

---

## 5. Scoring

- **Optimum** — at generation time, BFS/Dijkstra (uniform turn cost) over state
  `(px, py, vx, vy, laps_done)` finds the minimum number of turns to complete
  all laps **cleanly** (no wall hit; every move's segment fully on-track).
  Bounded and tractable because velocity components are clamped to `±VMAX`
  (≤ `(2·VMAX+1)² = 49` velocity states per cell) and it runs once per new
  track. Surfaced as `Optimum: N`.
- **Moves** — turns the player has taken this race.
- **Best** — persisted personal best (fewest moves to finish):
  - Daily: keyed by `("daily", ordinal, laps)`.
  - Random: a single overall best for `("random", laps)`.
- **Crashes** — count this race (informational; not part of the par).

---

## 6. Display sizing & rendering (device, 1-bit)

The host passes `(w, h)`; **do not hardcode screen pixels**. The logical lattice
is fixed and scaled to fit `(w, h)` minus the header.

- Logical lattice: **target ~36 × 20** cells (tune so cells render ≥ ~6px on the
  target panel). `cell_px = min((w-pad)/LW, (h-header-pad)/LH)`.
- **Header** (maze-style, top strip): `VEC · {mode} · lap {x}/{N} · moves {m}`,
  thin divider below.
- **Track:** draw the off-track region shaded/hatched (or draw inner+outer
  boundary lines and leave the band white) so the band reads clearly at small
  size. Pick whichever is more legible on the real panel during implementation;
  boundary-lines is the cheaper default.
- **Start/finish:** dashed segment across the band.
- **Trail:** dotted line through the car's past positions.
- **Car:** filled dot at `p`.
- **Projected move:** from `p`, draw the line to `p + v` (the coast target) and a
  **hollow** marker at the landing cell. This is mandatory for plannability —
  the player must see corner-clipping before committing. (The 9 pad buttons each
  shift this target by `a`; the device redraws the projection after each tap.)
- **Victory screen:** `FINISHED`, then `{moves} moves · optimum {N}`, `best {b}`,
  `crashes {c}`.
- **Crash:** no animation; status text shows `CRASH` for the next render and the
  car/trail reflect the new (reset or stopped) position.
- Wrap `render` in try/except with a plain-text fallback (maze does this) so a
  render bug never blanks the device.

---

## 7. Phone UI (`vector-racing.ui.json`)

Uses `when` to switch between pre-race setup and in-race pad, keyed on
`state.status`.

**Pre-race (`status` = `setup` or `finished`):**
- `select` **Mode**: `Daily` / `Random`.
- `select` **Crash rule**: `Safe` / `Hardcore`.
- `select` **Laps**: `1` / `2` / `3` (default `2`).
- `button` **Start race** (primary) → `push {action:"start", mode, crash, laps}`
  (templated from the selects).

**In-race (`status` = `racing`):** a 3×3 grid of buttons = the 9 accelerations.
Each pushes `{action:"accel", dx, dy}` with `dx,dy ∈ {-1,0,1}`. Center button
labeled `coast`. Layout:

```
↖ ↑ ↗        (-1,-1)(0,-1)(1,-1)
←  •  →   =  (-1, 0)(0, 0)(1, 0)
↙ ↓ ↘        (-1, 1)(0, 1)(1, 1)
```

(Screen Y grows downward; "up" buttons send `dy = -1`.)

- `button` **Abandon** (secondary) → `push {action:"abandon"}` → back to setup.

**state_text bindings (all modes):** `status`, `mode`, `crash_rule`,
`lap` (formatted `{}` already as `"x/N"`), `moves`, `optimum`, `best`, `speed`
(`|v|` as `vx,vy` or magnitude), `crashes`, `last_result`.

No `data_source` (no network). No permissions, no secrets.

---

## 8. `published_state()` keys

```python
{
  "status":     "setup" | "racing" | "finished",
  "mode":       "daily" | "random",
  "crash_rule": "safe" | "hardcore",
  "laps":       2,
  "lap":        "1/2",          # current/total, preformatted
  "moves":      0,
  "optimum":    0,              # 0/"—" until a track exists
  "best":       0,              # 0/"—" until first finish
  "speed":      "0,0",          # vx,vy
  "crashes":    0,
  "last_result":"",             # e.g. "Finished in 24 (optimum 19)"
}
```

---

## 9. Actions (device-side `_handle` / `on_data`)

| action     | payload fields            | effect                                              |
| ---------- | ------------------------- | --------------------------------------------------- |
| `start`    | `mode, crash, laps`       | generate track for mode+seed, compute optimum, reset car to start, `status=racing` |
| `accel`    | `dx, dy`                  | one turn (§1): clamp velocity, move or crash (§4), check lap/finish |
| `abandon`  | —                         | discard in-race state, `status=setup`               |

Unknown / malformed actions are ignored (no state change). Mid-race state is
**RAM-only**; only settings (last mode/crash/laps) and **best** scores persist
to disk (JSON file, same approach as maze's `_load_state`/`_save_state`).

---

## 10. Persistence

- File: device-side JSON (mirror maze: load on init, atomic save on change).
- Persisted: `bests` map (keys per §5), last-used `mode`/`crash`/`laps`.
- Not persisted: current track, car position, velocity, trail (regenerated /
  reset on next `start`).

---

## 11. Files & catalog

```
apps/vector-racing/
├── vector_racing.py            # plugin: class VectorRacing, name="vector-racing"
├── vector-racing.manifest.json # {name, version:"1.0.0", author, schema_version:1}
└── vector-racing.ui.json       # the UI tree above
```

Conventions (per repo CLAUDE.md): underscore file stem, hyphen manifest `name`;
bump version in **both** the `.py` class and the manifest on any change.

**index.json entry (added when we build, not now):**

```jsonc
{
  "name": "vector-racing",
  "icon": "VR",
  "version": "1.0.0",
  "author": "cristian-milea",
  "description": "Turn-based vector racing. 9-way pad on the phone, circuit on the e-ink.",
  "long_description": "Pen-and-paper Racetrack on the e-ink. Each turn you nudge your velocity by ±1 in x/y; momentum carries you, so brake before the corners or clip the wall. Procedurally generated closed circuits — a daily-seeded track everyone shares, or random. Pick Safe (stop on a crash) or Hardcore (back to the line) before the off, run 1–3 laps, and chase the computed optimum.",
  "category": "fun",
  "requires": { "permissions": [], "secrets": [] },
  "files": {
    "py":       "apps/vector-racing/vector_racing.py",
    "manifest": "apps/vector-racing/vector-racing.manifest.json",
    "ui":       "apps/vector-racing/vector-racing.ui.json"
  }
}
```

---

## 12. Tunable constants (single block in the `.py`)

| Const          | Default | Meaning                                            |
| -------------- | ------- | -------------------------------------------------- |
| `VMAX`         | 3       | per-component velocity clamp                        |
| `LW × LH`      | 36 × 20 | logical lattice size                                |
| `TRACK_W`      | 3       | nominal band width (cells)                          |
| `TRACK_W_MIN`  | 2       | minimum allowed band width (validation)             |
| `MIN_STRAIGHT` | 6       | min length of a "straight" run; need ≥ 2 of them    |
| `RESEED_CAP`   | 64      | generation retries before giving up                 |
| `DEFAULT_LAPS` | 2       | starting lap selection                              |

---

## 13. Out of scope (v1)

AI opponents, multiple cars, obstacles/oil/boost, animation, online
leaderboards, replay/ghost. Single-player, single-car, beat-your-best.
