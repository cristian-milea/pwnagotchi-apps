# Ricochet Robots

A single-player take on the board game *Ricochet Robots* (Alex Randolph / Rio
Grande Games) for the Ink Cartridge platform. The phone is the remote (robot
selector + D-pad + puzzle controls); the e-ink is the board.

**How to play:** pick a robot, then push a direction. The robot slides until it
crashes into a wall, the arena edge, the center block, or another robot — it
never stops mid-track. Land the **matching shape** on its target cell in as few
moves as possible. The on-device solver knows the optimum, so you always have a
par to beat.

- **Stem:** `ricochet_robots` (underscores) · **manifest `name`:** `ricochet-robots` (hyphen)
- **Render area:** 234 × 122 px, 1-bit mono. The host draws its own taskbar;
  this app only paints `(0,0)..(w,h)`.
- **Re-render:** push-driven (`on_data`). No render loop, no `interval_seconds`.
  One tap → one push → one redraw. Generation runs once per "New board" tap,
  never per render.

---

## 1. Rules

| Element | Official game | This port |
| --- | --- | --- |
| Board | 16×16, 4 random quadrant tiles, 2×2 walled center | N×N, **N configurable** (even only); procedurally generated walls; 2×2 walled center |
| Robots | 4–5, distinguished by **colour** | **4 robots, distinguished by drawn shape** (no colour on 1-bit) |
| Target | colour+symbol chip in a wall crook; matching-colour robot must reach it | **shape-specific** target cell in a wall crook; matching-shape robot must reach it |
| Wildcard | "vortex" chip, any robot | rare wildcard target (~1/17), any robot reaches it |
| Movement | slide until wall / border / robot; stop adjacent; must move ≥1 | identical, 4 directions, center block also stops robots |
| Goal | reach target in fewest moves (players bid) | reach target; **score = moves used vs. computed optimum** |

**Movement is the core mechanic and is exact:** a robot slides in the chosen
direction and continues cell-by-cell until the *next* step would (a) leave the
board, (b) cross a wall, (c) enter the 2×2 center block, or (d) enter a cell
occupied by another robot. It stops in the last legal cell. A robot must move at
least one cell — choosing a direction that is immediately blocked is a **no-op**
(the tap is ignored). Only the **target's matching robot** (or any robot, for a
wildcard) on the **exact** target cell wins.

### Robot shapes (drawn with PIL primitives, not font glyphs)

Filled shapes, ~cell-sized, font-independent so they stay crisp at 7–14 px:

| Index | Shape | Primitive | Selector label |
| --- | --- | --- | --- |
| 0 | ● circle | `ellipse` | Circle |
| 1 | ■ square | `rectangle` | Square |
| 2 | ▲ triangle | `polygon` | Triangle |
| 3 | ◆ diamond | `polygon` | Diamond |

- The **selected** robot gets a highlight ring/box drawn around its cell.
- The **target** is the matching shape drawn as an **outline only** on the
  target cell (a filled robot sitting on its target reads as "solved").
- Wildcard target: a small outlined asterisk/cross drawn with lines.

---

## 2. Data model

### Coordinates & walls
- Cells `(x, y)`, `0 ≤ x,y < N`, origin top-left.
- **Walls** stored as a per-cell 4-bit mask `N=1, E=2, S=4, W=8` (same shape as
  the maze cartridge's cell bits). Border walls are implicit. Shared edges are
  kept consistent: if cell A has its E bit set, cell B to its east has its W bit
  set — `set_wall(cells, x, y, side)` sets both halves.
- **Center block:** the 2×2 cells `{(c,c),(c+1,c),(c,c+1),(c+1,c+1)}` where
  `c = N/2 - 1`, held in a `blocked` set. Robots can neither enter nor stop
  there; it acts like a solid obstacle.

### Persisted state (JSON, via `_load_state`/`_save_state`)
```jsonc
{
  "mode": "random",            // "random" | "daily"
  "size": 8,                   // N
  "difficulty": "medium",      // "easy" | "medium" | "hard"  (chosen BEFORE generation)
  "seed": 123456,
  "cells": [[...wall masks...]],   // N×N
  "target": [1, 3, 5],             // [shape, x, y]; shape -1 = wildcard
  "blocked": [[3,3],[4,3],[3,4],[4,4]],
  "robot_start": [[x,y],[x,y],[x,y],[x,y]],   // index = shape index
  "robots":      [[x,y],[x,y],[x,y],[x,y]],   // current positions
  "selected": 0,               // index of robot the D-pad drives
  "optimal": 7,                // BFS minimum move count for this puzzle
  "moves": 0,                  // moves used this attempt
  "history": [[robot_idx, prev_x, prev_y], ...],  // for Undo
  "completed": false,
  "best_random": { "8:medium": 7, ... },   // best (lowest) moves, keyed "size:difficulty"
  "best_daily":  { "<seed>": 7, ... }      // best moves per daily board
}
```

### Published state (`published_state()` → HUD bindings)
`mode`, `seed`, `size`, `difficulty`, `target_shape` (label), `selected`
(label), `moves`, `optimal`, `best`, `completed`, `status` ("Solving…" /
"Solved!").

---

## 3. Movement & solver

### `slide(cells, positions, idx, dir, blocked) -> (x, y)`
Pure function. From `positions[idx]`, step repeatedly in `dir` while the next
cell is in bounds, not wall-separated, not a center-block cell, and not occupied
by another robot. Returns the final cell (== start if immediately blocked → the
caller treats it as a no-op).

### `solve(cells, robots, target, blocked, max_states, max_depth) -> int | None`
**Breadth-first search for the minimum number of moves.** Used to (1) prove a
board is solvable, (2) compute `optimal` for scoring, (3) verify a generated
board.

- **State** = tuple of all robot positions, ordered by shape index:
  `((x0,y0),(x1,y1),(x2,y2),(x3,y3))`.
- **Goal** = the target's matching robot is on the target cell (for wildcard:
  *any* robot on the target cell).
- **Successors** = for each robot × each of 4 directions, slide; skip no-ops.
  Branching ≤ 16.
- **Dedupe** with a visited set of states. Standard BFS layer counting gives the
  true minimum.
- **Caps (anti-runaway, important on Pi Zero):** return `None` ("unsolvable
  within budget") if `states_expanded > max_states` or `depth > max_depth`. See
  §5 for values.
- `solve_path(...)` returns the move sequence too; used only by the
  Hard-difficulty quality check (§4), at most once per accepted board.

State space is tame because robots stop only at walls/robots, so the reachable
set is far smaller than `N²` choose 4.

**Wall-stop table (`_wall_stops`) + `_fast_slide`:** the BFS calls slide 16× per
state expansion, so slide's per-step corridor walk dominated. We precompute
once per board, for every cell and direction, where a robot stops with *no other
robots* present (walls/border/center only). `_fast_slide` then does an O(1)
wall-stop lookup plus an O(#robots) clip for blockers — no per-step walk, no
per-call set construction. `_fast_slide` is verified identical to the reference
`slide` across thousands of random configs (unit test). The plain `slide` stays
the reference implementation used by `_move` and the tests.

---

## 4. Board generation

Goal: **always correct** (solvable, legal, no traps) and **good** (non-trivial,
within the requested difficulty, coherent walls). Two phases; all randomness
comes from a single seeded PRNG `rng = random.Random(seed)` so boards are
reproducible (see §6).

### Phase A — wall layout
1. Start all cells with mask `0` (only implicit border walls).
2. Flag the 2×2 center block as `blocked`.
3. Place **K L-shaped wall pairs** in the interior. An *L* = two perpendicular
   edge-walls meeting at one cell corner (e.g. set cell `(x,y)`'s N **and** E
   bits). This is exactly the structure that catches a sliding robot — the crook
   cell is where a robot coming along either arm stops.
4. Placement constraints (reject a candidate L and re-draw if violated):
   - not on the border ring, not on / adjacent to the center block;
   - no two L's share a cell;
   - **never produce a cell with walls on ≥3 sides** (prevents traps);
   - spread evenly across the four quadrants (mimics the real game's 4-tile
     construction; avoids lopsided boards).
5. Each **L-crook cell is a candidate target**. One becomes the active target in
   phase B; the rest remain as walls — faithful to the real board, where every
   chip's cell exists physically but only one is active per round.

`K` by size (≈ area/11, anchored to the real game's 17 targets on 16×16):

| N | K (L-shapes) |
| --- | --- |
| 8 | 6 |
| 12 | 12 |
| 16 | 17 |

### Phase B — multi-target placement search
For each robot placement we run **one BFS that prices every candidate target at
once**, rather than one BFS per target. This was the key performance decision
(see Calibration below): single-target rejection sampling for rare deep boards
was 5–14 s; multi-target with a reachable difficulty floor is sub-2 s.

1. Place the 4 robots on distinct random free cells (not the center block).
2. `_target_optima(...)` runs **one BFS from the robots**, recording, for every
   crook cell, the minimum moves for *each* specific robot to reach it (and for
   *any* robot — the wildcard case). One search → an optimum for all `4 × K`
   shape/crook targets. To keep it cheap the BFS depth is capped at
   `band_hi + 1` (never explore past the band) and uses the wall-stop table (§3).
3. Build the candidate set (excluding robots' own start cells); with probability
   ≈ 1/17 include **wildcard** targets.
4. Among candidates whose optimum lands **inside the chosen difficulty band**,
   pick the **hardest** (largest optimum); deterministic tiebreak.
   - **Hard only:** require `_hard_quality` — the optimal solution must ricochet
     (a robot stops on a wall/center, not just the border) **and** involve a
     robot other than the target's. If the hardest in-band target fails this,
     fall through to the next-hardest.
5. **No in-band target → retry** with budget (§5): re-roll the placement; after
   `placement_tries`, re-roll the wall layout.
6. **Exhaustion fallback (never ship a bad board):** track the closest-to-band
   solvable board across all attempts and return it if the budget is spent.
   Always solvable; worst case a little outside the band.

### Difficulty bands (optimal move count) — empirically calibrated

These are the **measured-achievable** bands, not aspirations. The generator's
move-count ceiling is ≈ 9–11 regardless of board size (bigger boards have more
open space, so robots slide far and reach targets in similar move counts).
Reaching genuine 12–15-move puzzles would require dense structured walls and
multi-second searches — out of scope for a Pi-Zero cartridge. **Board size is a
spatial/visual choice, not a move multiplier.** Floors are set reachable so a
placement hits its band on the first try or two; 0 band-misses observed over the
calibration sweep.

| Difficulty | 8×8 | 12×12 | 16×16 |
| --- | --- | --- | --- |
| Easy | 3–4 | 4–5 | 4–6 |
| Medium | 5–6 | 6–7 | 7–8 |
| Hard | 7–9 | 8–10 | 9–11 |

---

## 5. Performance budget (Pi Zero)

Generation is one-shot per "New board" tap. Render reads stored state only.

| Constant | 8×8 | 12×12 | 16×16 |
| --- | --- | --- | --- |
| `max_states` (BFS) | 40 000 | 70 000 | 100 000 |
| `max_depth` (BFS) | 12 | 14 | 14 |
| `placement_tries` (per wall layout) | 16 | 16 | 12 |
| `layout_rerolls` | 3 | 3 | 2 |

Search depth per board = `min(max_depth, band_hi + 1)`. Measured generation
latency on a dev laptop (Python 3.14): easy/medium ≤ 1 s for all sizes; hard
~0.6 s (8×8), ~1.2 s (12×12), ~2 s (16×16), worst single board ~2.9 s. A Pi Zero
is roughly 10× slower, so a 16×16 Hard board can take ~20–30 s worst case — run
generation synchronously in the `new_board` handler behind a **"Generating…"**
e-ink frame so it doesn't look like a hang.

---

## 6. Determinism & daily sharing

`seed` fully determines a board given `(size, difficulty)`:

- **Random mode:** `seed` from the wall-clock (stored, so the board survives
  restarts and can be replayed/restarted).
- **Daily mode:** `seed = _seed_for(size, difficulty, _daily_seed())`. Everyone
  who picks *today / Medium / 12×12* gets the **same** board. Daily sharing is
  therefore **per-(size, difficulty)** — a deliberate consequence of letting the
  player choose difficulty (a single global daily board can't honour a
  per-player difficulty choice). `_daily_seed()` is the maze cartridge's
  day-ordinal helper; size and difficulty are folded into the hash.

---

## 7. Render layout (e-ink, 234 × 122)

No top title strip (saves vertical space). **Square board on the left, HUD
column on the right.**

```
+----------------------------+------------------+
|                            | RICOCHET         |
|        BOARD               | DAILY #4821      |
|   (square, side =          | 8x8 H            |
|    min(118, ...))          | Target: ▲        |
|   walls, robots,           | Move:  ◆         |
|   target outline,          | Moves:  3        |
|   selection ring           | Optimum: 7       |
|                            | Best:   6        |
+----------------------------+------------------+
```

- `board_side = min(118, 234 - MIN_HUD_W - 4)` with `MIN_HUD_W = 104`,
  `cell = board_side // N`, board centered vertically with ≤2 px margins.
- Cell ≈ 14 px (8×8), 9 px (12×12), 7 px (16×16). At 7 px, robots are small
  filled shapes and the selection ring is a 1-px box — legible but tight; the
  documented trade-off for the 16×16 size.
- HUD: name, mode+seed, size+difficulty, target shape, selected-robot shape,
  moves, optimum, best.
- **Victory screen** (on `completed`): a boxed "SOLVED" + `moves` / `optimum`,
  mirroring the maze cartridge's victory layout.
- `render` wraps `_paint` in try/except and falls back to a plain text error
  frame so the device never goes blank (same defensive pattern as maze).

---

## 8. Phone UI (`ricochet_robots.ui.json`)

```
column
├─ text  "Ricochet Robots" (headline) + caption
├─ select  local=robot   → push {action:"select", robot:"{{local.robot}}"}
│          options: Circle / Square / Triangle / Diamond
├─ row (D-pad)
│   ←  ↑/↓  →   → push {action:"move", dir:"left|up|down|right"}   (moves selected robot)
├─ row:  [Undo] {action:"undo"}     [Restart] {action:"restart"}
├─ divider
├─ select  local=difficulty (Easy/Medium/Hard)   (no push; read at generate time)
├─ select  local=size       (8 / 12 / 16)         (no push; read at generate time)
├─ button "New random board" → push {action:"new_random", size, difficulty}
├─ button "Today's daily"    → push {action:"new_daily",  size, difficulty}
├─ divider
└─ state_text bindings: status, mode, target_shape, selected, moves, optimal, best
```

`on_data` actions: `select`, `move`, `undo`, `restart`, `new_random`,
`new_daily`. `move` applies `slide`, pushes onto `history`, increments `moves`,
checks win; `undo` pops `history`; `restart` resets `robots = robot_start`,
`moves = 0`, clears `history`, `completed = false`.

---

## 9. Files & tests

```
apps/ricochet-robots/
├── README.md                        # this document
├── ricochet_robots.py               # plugin: model, slide, solve, generate, render, on_data
├── ricochet_robots.manifest.json    # name "ricochet-robots", icon "RR", version, category "fun"
└── ricochet_robots.ui.json          # phone UI tree above
```

Manifest: `category: "fun"`, `requires: { permissions: [], secrets: [] }` (no
network, fully on-device). `icon` is canonical in the manifest (`"RR"`); the
Python class `.icon` matches it.

**Tests** live at the repo root in `tests/test_ricochet_robots.py` (a test file
*cannot* sit in the app folder — `build_index.py` requires exactly one `.py`
there). `tests/conftest.py` puts the app dir on `sys.path`. Run with:

```
python3 -m pytest tests/ -q
```

Coverage: `slide` stop conditions + no-op; `_fast_slide` ≡ `slide`; wall
consistency and no-traps invariants; `solve`/`solve_path` correctness incl.
wildcard and caps; per-(size,difficulty) generation is solvable, in-band, and
deterministic; class actions (select/move/undo/restart/new_daily reproducibility,
win + best tracking); render smoke tests across all sizes + victory; manifest
and UI shape.

## 10. Publishing

`index.json` is **generated** from the manifests — do not hand-edit it. After
changing the cartridge:

```
python3 build_index.py            # regenerate index.json from manifests
git -C ~/projects/ink-cartridges add -A
git -C ~/projects/ink-cartridges commit -m "..."
git -C ~/projects/ink-cartridges push
```

CI (`.github/workflows/index.yml`) runs `build_index.py --check` and fails if
the index wasn't regenerated.
