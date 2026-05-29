# Vector Racing

Turn-based *Racetrack* (the pen-and-paper momentum game) on the e-ink. The
phone is a 9-way acceleration pad; the e-ink draws a top-down circuit, your car,
its trail, and where this turn's coast would land you. One tap = one move = one
redraw — no animation, no clock.

```
VEC · daily · lap 1/2 · moves 7
────────────────────────────────
        ╭───────────────╮
       ╭┘   ╭───────╮    └╮
       │    ╰───────╯  ⌂  │      ⌂ = car (heads where v points)
       ╰╮               ╭─╯      ○ = where you'll land if you coast
        ╰───────────────╯        ╎ = start/finish gate
 figure8  spd 1,0  opt 24  best — cr 0
```

## How to play

Each turn you nudge your **velocity** by `±1` in x and/or y (the nine pad
buttons; centre = *coast*, no change). Velocity then carries you: you move from
`p` to `p + v`. Momentum is the whole game — to take a corner you have to start
braking *before* it, or you'll sail straight into the wall.

- The straight line from `p` to `p + v` must lie **entirely** on the track. A
  fast move that clips a corner crashes even if the landing cell is on-track.
- The hollow marker shows your coast landing point; the nine buttons each shift
  it by one. Plan against it before you commit.
- Velocity is clamped to `±3` per axis.

Before the off you choose:

- **Mode** — *Daily* (everyone on every device gets the same track that day) or
  *Random* (a fresh track each race).
- **Crash rule** — *Safe* (stop dead at the last on-track point, keep racing) or
  *Hardcore* (back to the start line; completed laps are kept, the current lap
  is redone — the lost ground is the penalty).
- **Laps** — 1, 2, or 3.

## Tracks

Circuits are generated on the device from the seed, so a daily track is shared
by everyone. Nine shapes, picked from the seed:

`oval` · `triangle` · `square` · `pentagon` · `hexagon` · `zigzag` ·
`scalloped` (wavy) · `kidney` (a concave bean) · `figure8` (self-crossing).

The current shape is named in the e-ink footer.

## Scoring

- **Optimum** — the fewest clean turns (no wall touch, every lap counted) to
  finish, found by an exhaustive search the moment the race starts. It runs in
  the background, so the race begins instantly and `optimum` shows `—` until the
  search lands (a second or two on Pi-class hardware).
- **Moves** — turns you've taken.
- **Best** — your personal best, kept per `(mode, laps)` — and per day for
  Daily. Persists across restarts.
- **Crashes** — this race, informational; not part of par.

## How it works (for the curious / future-you)

- **Track = a centreline tube.** A closed centreline curve (one of the nine
  shape families, fitted to the lattice) is thickened into a band of width
  `TRACK_W` by marking every lattice cell within `HALF_W` of the path. This is
  what lets a **figure-8** exist — the self-crossing is simply on-track for both
  passes.
- **Collision** tests the *supercover* line of the move segment (every cell the
  segment touches, including corner-clips), not plain Bresenham.
- **Laps via ordered checkpoint gates.** Equally-spaced full-width gates sit
  along the centreline; a lap is completed by crossing the whole ring of them
  *in order*, forward. The path-position (which gate is next) is carried in
  state, which (a) disambiguates the two passes of a figure-8 and (b) kills the
  "step back over the line then forward = free lap" cheese that a naive
  line-crossing rule allows. Driving backward never decrements laps.
- **Optimum** is a BFS over `(px, py, vx, vy, gates-passed)` — uniform turn
  cost, so the first time the goal is reached is the optimum. Verified: the
  optimal path, replayed through the live game, finishes in exactly `optimum`
  moves (so the par is always achievable, including on the figure-8).
- Generated tracks are validated (single connected band, an enclosed interior,
  a drivable start) and reseeded on failure, with a plain oval as the guaranteed
  fallback. Mid-race state is RAM-only; only settings and best scores persist.

## Files

```
vector_racing.py             # plugin: class VectorRacing, name="vector-racing"
vector-racing.manifest.json  # metadata (no permissions, no secrets)
vector-racing.ui.json        # phone UI: setup selects + in-race 9-way pad
```

Per repo convention: underscore file stem, hyphen manifest `name`; bump the
version in **both** the `.py` class and the manifest on any change.

## Tunables (top of `vector_racing.py`)

| Const          | Default | Meaning                                  |
| -------------- | ------- | ---------------------------------------- |
| `VMAX`         | 3       | per-axis velocity clamp                  |
| `LW × LH`      | 36 × 20 | logical lattice                          |
| `TRACK_W`      | 4       | band width in cells (`HALF_W` = tube radius) |
| `RESEED_CAP`   | 64      | generation retries before the oval fallback |
| `DEFAULT_LAPS` | 2       | starting lap selection                   |
| `SHAPES`       | —       | the nine shape families                  |
