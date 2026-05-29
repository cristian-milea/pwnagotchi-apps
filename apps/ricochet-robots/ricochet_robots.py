# Ricochet Robots — faithful single-player port for the Ink Cartridge e-ink.
#
# Robots slide until they hit a wall, the arena edge, the 2x2 center block,
# or another robot — never stopping mid-track. Land the matching SHAPE on its
# target cell in the fewest moves. Colours (no colour on 1-bit) become four
# primitive-drawn shapes: circle / square / triangle / diamond.
#
# Boards are generated on-device: authentic L-shaped walls (Phase A), then a
# target + robots placed and verified by BFS (Phase B), rejecting anything not
# in the chosen difficulty band. seed fully determines a board given
# (size, difficulty), so daily mode is shareable per (size, difficulty).
#
# Push schema (phone -> device):
#   {"action": "select", "robot": "0".."3"}
#   {"action": "move", "dir": "up|down|left|right"}   (moves the selected robot)
#   {"action": "undo"} / {"action": "restart"}
#   {"action": "new_random", "size": "8|12|16", "difficulty": "easy|medium|hard"}
#   {"action": "new_daily",  "size": "8|12|16", "difficulty": "easy|medium|hard"}

import json
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import date

from PIL import ImageFont

STATE_PATH = "/etc/pwnagotchi/ricochet_robots.state.json"

# Wall bit per side; DIRS indexed 0=N 1=E 2=S 3=W; y grows downward.
WALL_N, WALL_E, WALL_S, WALL_W = 1, 2, 4, 8
WALL_BITS = [WALL_N, WALL_E, WALL_S, WALL_W]
DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]
OPP = [2, 3, 0, 1]
DIR_FROM_NAME = {"up": 0, "right": 1, "down": 2, "left": 3}

SHAPES = ["circle", "square", "triangle", "diamond"]
SIZES = [8, 12, 16]
WILDCARD = -1

# Optimal-move bands per board size (inclusive). Calibrated empirically against
# the generator (see PLAN.md Task 7); achievable ranges, not aspirations.
BANDS = {
    8:  {"easy": (3, 4), "medium": (5, 6), "hard": (7, 9)},
    12: {"easy": (4, 5), "medium": (6, 7), "hard": (8, 10)},
    16: {"easy": (4, 6), "medium": (7, 8), "hard": (9, 11)},
}
# Number of L-shaped wall pairs placed per board (~ area / 11; anchored to the
# real game's 17 targets on 16x16).
K_BY_SIZE = {8: 6, 12: 12, 16: 17}
# Anti-runaway caps for BFS + the generate-and-reject budget.
CAPS = {
    8:  {"max_states": 40000,  "max_depth": 12, "placement_tries": 16, "layout_rerolls": 3},
    12: {"max_states": 70000,  "max_depth": 14, "placement_tries": 16, "layout_rerolls": 3},
    16: {"max_states": 100000, "max_depth": 14, "placement_tries": 12, "layout_rerolls": 2},
}
MIN_HUD_W = 104


# ---------------------------------------------------------------- geometry ---
def has_wall(cells, x, y, d):
    return bool(cells[y][x] & WALL_BITS[d])


def set_wall(cells, x, y, d):
    """Set the wall on side d of (x,y) and the mirrored side of its neighbour."""
    n = len(cells)
    cells[y][x] |= WALL_BITS[d]
    nx, ny = x + DIRS[d][0], y + DIRS[d][1]
    if 0 <= nx < n and 0 <= ny < n:
        cells[ny][nx] |= WALL_BITS[OPP[d]]


def center_cells(n):
    c = n // 2 - 1
    return {(c, c), (c + 1, c), (c, c + 1), (c + 1, c + 1)}


def slide(cells, positions, idx, d, blocked):
    """Slide robot `idx` in direction d until it stops. positions: list of (x,y).
    Returns the final (x,y) (== start if the robot is immediately blocked)."""
    n = len(cells)
    x, y = positions[idx]
    dx, dy = DIRS[d]
    others = set(positions)
    others.discard((x, y))
    while True:
        if has_wall(cells, x, y, d):
            break
        nx, ny = x + dx, y + dy
        if not (0 <= nx < n and 0 <= ny < n):
            break
        if (nx, ny) in blocked or (nx, ny) in others:
            break
        x, y = nx, ny
    return (x, y)


# ------------------------------------------------------------------ solver ---
def _wall_stops(cells, blocked):
    """Precompute, for every cell and direction, where a robot stops sliding
    with NO other robots in the way (walls + border + center block only).
    Lets the BFS skip the per-step corridor walk: O(1) wall-stop lookup + an
    O(#robots) clip for blockers."""
    n = len(cells)
    stops = [[[None] * n for _ in range(n)] for _ in range(4)]
    for d in range(4):
        dx, dy = DIRS[d]
        for y in range(n):
            for x in range(n):
                cx, cy = x, y
                while True:
                    if has_wall(cells, cx, cy, d):
                        break
                    nx, ny = cx + dx, cy + dy
                    if not (0 <= nx < n and 0 <= ny < n):
                        break
                    if (nx, ny) in blocked:
                        break
                    cx, cy = nx, ny
                stops[d][y][x] = (cx, cy)
    return stops


def _fast_slide(stops, positions, idx, d):
    """slide() equivalent using the precomputed wall-stop table. `positions`
    is a sequence of (x,y); returns the final (x,y) (== start if blocked)."""
    x, y = positions[idx]
    sx, sy = stops[d][y][x]
    dx, dy = DIRS[d]
    if dx:  # horizontal move
        if dx > 0:
            nearest = min((px for (px, py) in positions if py == y and x < px <= sx),
                          default=None)
            if nearest is not None:
                sx = nearest - 1
        else:
            nearest = max((px for (px, py) in positions if py == y and sx <= px < x),
                          default=None)
            if nearest is not None:
                sx = nearest + 1
        return (sx, y)
    # vertical move
    if dy > 0:
        nearest = min((py for (px, py) in positions if px == x and y < py <= sy),
                      default=None)
        if nearest is not None:
            sy = nearest - 1
    else:
        nearest = max((py for (px, py) in positions if px == x and sy <= py < y),
                      default=None)
        if nearest is not None:
            sy = nearest + 1
    return (x, sy)


def _is_goal(state, target):
    shape, tx, ty = target
    if shape == WILDCARD:
        return any(p == (tx, ty) for p in state)
    return state[shape] == (tx, ty)


def solve(cells, start_positions, target, blocked, max_states, max_depth):
    """BFS for the minimum number of moves. target = (shape_idx, tx, ty);
    shape_idx == WILDCARD means any robot. Returns min moves, or None if
    unsolvable within the (max_states, max_depth) caps."""
    stops = _wall_stops(cells, blocked)
    start = tuple(tuple(p) for p in start_positions)
    if _is_goal(start, target):
        return 0
    seen = {start}
    frontier = deque([(start, 0)])
    expanded = 0
    while frontier:
        state, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        expanded += 1
        if expanded > max_states:
            return None
        positions = list(state)
        for ri in range(len(positions)):
            for d in range(4):
                np = _fast_slide(stops, positions, ri, d)
                if np == positions[ri]:
                    continue
                nxt = positions[:]
                nxt[ri] = np
                ns = tuple(nxt)
                if ns in seen:
                    continue
                if _is_goal(ns, target):
                    return depth + 1
                seen.add(ns)
                frontier.append((ns, depth + 1))
    return None


def solve_path(cells, start_positions, target, blocked, max_states, max_depth):
    """Like solve(), but returns the optimal move list [(robot_idx, dir), ...]
    or None. Used only for the Hard-difficulty quality gate (once per board)."""
    stops = _wall_stops(cells, blocked)
    start = tuple(tuple(p) for p in start_positions)
    if _is_goal(start, target):
        return []
    seen = {start}
    came = {start: None}
    frontier = deque([(start, 0)])
    expanded = 0
    goal = None
    while frontier and goal is None:
        state, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        expanded += 1
        if expanded > max_states:
            return None
        positions = list(state)
        for ri in range(len(positions)):
            for d in range(4):
                np = _fast_slide(stops, positions, ri, d)
                if np == positions[ri]:
                    continue
                nxt = positions[:]
                nxt[ri] = np
                ns = tuple(nxt)
                if ns in seen:
                    continue
                seen.add(ns)
                came[ns] = (state, (ri, d))
                if _is_goal(ns, target):
                    goal = ns
                    break
                frontier.append((ns, depth + 1))
            if goal is not None:
                break
    if goal is None:
        return None
    path = []
    cur = goal
    while came[cur] is not None:
        prev, move = came[cur]
        path.append(move)
        cur = prev
    path.reverse()
    return path


def _stop_reason(cells, positions, idx, d, blocked):
    """Why robot idx stops sliding in d: 'wall' | 'block' | 'robot' | 'border'."""
    n = len(cells)
    x, y = positions[idx]
    others = set(positions)
    others.discard((x, y))
    dx, dy = DIRS[d]
    while True:
        if has_wall(cells, x, y, d):
            return "wall"
        nx, ny = x + dx, y + dy
        if not (0 <= nx < n and 0 <= ny < n):
            return "border"
        if (nx, ny) in blocked:
            return "block"
        if (nx, ny) in others:
            return "robot"
        x, y = nx, ny


def _hard_quality(cells, robots, target, blocked, caps):
    """A Hard board's optimal solution must ricochet (stop on a wall/center)
    AND involve a robot other than the target's (a real blocker or a second
    mover) — that is what separates Hard from a long straight corridor."""
    path = solve_path(cells, robots, target, blocked,
                      caps["max_states"], caps["max_depth"])
    if not path:
        return False
    positions = [tuple(r) for r in robots]
    shape = target[0]
    ricochet = False
    blocker = False
    movers = set()
    for ri, d in path:
        reason = _stop_reason(cells, positions, ri, d, blocked)
        if reason in ("wall", "block"):
            ricochet = True
        if reason == "robot":
            blocker = True
        movers.add(ri)
        positions[ri] = slide(cells, positions, ri, d, blocked)
    if shape == WILDCARD:
        used_other = len(movers) >= 2
    else:
        used_other = any(r != shape for r in movers)
    return ricochet and (blocker or used_other)


# -------------------------------------------------------------- generation ---
def _generate_walls(n, k, rng):
    """Phase A: place K L-shaped wall pairs, balanced across the four quadrants,
    keeping every cell at <=2 walls (no traps). Returns (cells, blocked, crooks)
    where crooks = [(x, y, vertical_dir, horizontal_dir), ...] candidate targets."""
    cells = [[0] * n for _ in range(n)]
    blocked = center_cells(n)

    def ok_cell(x, y):
        if x <= 0 or y <= 0 or x >= n - 1 or y >= n - 1:
            return False
        if (x, y) in blocked:
            return False
        for d in range(4):
            if (x + DIRS[d][0], y + DIRS[d][1]) in blocked:
                return False
        return True

    quads = {(qx, qy): [] for qx in (0, 1) for qy in (0, 1)}
    for y in range(n):
        for x in range(n):
            if ok_cell(x, y):
                quads[(0 if x < n // 2 else 1, 0 if y < n // 2 else 1)].append((x, y))
    pools = list(quads.values())
    for p in pools:
        rng.shuffle(p)
    order = []
    total = sum(len(p) for p in quads.values())
    i = 0
    while any(pools) and len(order) < total:
        p = pools[i % 4]
        if p:
            order.append(p.pop())
        i += 1

    def wall_count(x, y):
        return bin(cells[y][x]).count("1")

    crooks = []
    for (x, y) in order:
        if len(crooks) >= k:
            break
        if cells[y][x] != 0:
            continue
        vd = rng.choice([0, 2])  # N or S
        hd = rng.choice([1, 3])  # E or W
        touched = [(x, y)]
        for d in (vd, hd):
            nx, ny = x + DIRS[d][0], y + DIRS[d][1]
            if 0 <= nx < n and 0 <= ny < n:
                touched.append((nx, ny))

        def predicted(cx, cy):
            add = 2 if (cx, cy) == (x, y) else 1
            return wall_count(cx, cy) + add

        if any((cx, cy) in blocked for cx, cy in touched):
            continue
        if any(predicted(cx, cy) > 2 for cx, cy in touched):
            continue
        set_wall(cells, x, y, vd)
        set_wall(cells, x, y, hd)
        crooks.append((x, y, vd, hd))

    return cells, blocked, crooks


def _daily_seed():
    return date.today().toordinal()


def _seed_for(size, difficulty, day_ordinal):
    """Deterministic seed so everyone choosing the same day+size+difficulty
    gets the same daily board."""
    diff_idx = ["easy", "medium", "hard"].index(difficulty)
    return (day_ordinal * 9973 + size * 131 + diff_idx * 17) & 0x7FFFFFFF


def _target_optima(stops, robots, crook_cells, max_states, max_depth):
    """ONE BFS from `robots`. Returns, per crook cell, the minimum moves for each
    specific robot (per_shape) and for any robot (any_robot) to reach it — so a
    single search prices every candidate target on this placement at once."""
    start = tuple(tuple(r) for r in robots)
    per_shape = {}   # (shape_idx, x, y) -> min moves
    any_robot = {}   # (x, y) -> min moves (any robot)

    def record(state, depth):
        for ri, p in enumerate(state):
            if p in crook_cells:
                per_shape.setdefault((ri, p[0], p[1]), depth)
                any_robot.setdefault(p, depth)

    record(start, 0)
    seen = {start}
    frontier = deque([(start, 0)])
    expanded = 0
    while frontier:
        state, depth = frontier.popleft()
        if depth >= max_depth:
            continue
        expanded += 1
        if expanded > max_states:
            break
        positions = list(state)
        for ri in range(len(positions)):
            for d in range(4):
                np = _fast_slide(stops, positions, ri, d)
                if np == positions[ri]:
                    continue
                nxt = positions[:]
                nxt[ri] = np
                ns = tuple(nxt)
                if ns in seen:
                    continue
                seen.add(ns)
                record(ns, depth + 1)
                frontier.append((ns, depth + 1))
    return per_shape, any_robot


def _board(n, difficulty, seed, cells, blocked, target, robots, m):
    return {
        "size": n, "difficulty": difficulty, "seed": seed,
        "cells": cells, "blocked": sorted(blocked),
        "target": list(target), "robots": [list(r) for r in robots],
        "optimal": m,
    }


def generate_board(n, difficulty, seed):
    """Phase A walls + Phase B placement search.

    One full-explore BFS per robot placement prices every (shape, crook) target
    at once (`_target_optima`). We then pick the hardest target that lands in the
    chosen difficulty band. The depth cap = hi + 1 keeps each BFS cheap (we never
    explore past the band), and the placement budget is bounded. Always returns a
    solvable board; on budget exhaustion returns the closest-to-band board found
    (optimum may be a little outside the band)."""
    rng = random.Random(seed)
    lo, hi = BANDS[n][difficulty]
    caps = CAPS[n]
    search_depth = min(caps["max_depth"], hi + 1)
    best = None  # (distance_to_band, board_dict)

    for _ in range(caps["layout_rerolls"]):
        cells, blocked, crooks = _generate_walls(n, K_BY_SIZE[n], rng)
        if not crooks:
            continue
        stops = _wall_stops(cells, blocked)
        crook_cells = {(cx, cy) for (cx, cy, _v, _h) in crooks}
        free = [(x, y) for y in range(n) for x in range(n) if (x, y) not in blocked]
        for _ in range(caps["placement_tries"]):
            robots = rng.sample(free, len(SHAPES))
            rset = set(robots)
            per_shape, any_robot = _target_optima(
                stops, robots, crook_cells, caps["max_states"], search_depth)

            cand = {(s, x, y): m for (s, x, y), m in per_shape.items()
                    if (x, y) not in rset and m > 0}
            if rng.random() < 1 / 17:
                for (x, y), m in any_robot.items():
                    if (x, y) not in rset and m > 0:
                        cand[(WILDCARD, x, y)] = m
            if not cand:
                continue

            for target, m in cand.items():
                dist = 0 if lo <= m <= hi else (lo - m if m < lo else m - hi)
                if best is None or dist < best[0]:
                    best = (dist, _board(n, difficulty, seed, cells, blocked,
                                         target, robots, m))

            in_band = [(t, m) for t, m in cand.items()
                       if lo <= m <= hi and not (difficulty != "easy" and m == 1)]
            in_band.sort(key=lambda tm: (-tm[1], tm[0]))  # hardest first, deterministic
            for target, m in in_band:
                if difficulty == "hard" and not _hard_quality(
                        cells, robots, target, blocked, caps):
                    continue
                return _board(n, difficulty, seed, cells, blocked, target, robots, m)

    if best is not None:
        return best[1]
    raise RuntimeError("ricochet_robots: board generation failed")


# ------------------------------------------------------------- persistence ---
def _load_state(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(path, state):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ------------------------------------------------------------------- class ---
class RicochetRobots:
    name = "ricochet-robots"
    icon = "RR"
    version = "1.0.2"
    interval_seconds = None

    def __init__(self):
        self._lock = threading.RLock()
        self._fonts = {}
        # Deferred-generation state machine (see render/on_data). on_data only
        # queues a request; the render thread flashes "Generating..." then builds
        # the board on the next pass.
        self._pending = None      # ("random"|"daily", size, difficulty) | None
        self._gen_phase = None    # None | "announce" | "generate"
        s = _load_state(STATE_PATH)
        self._best_random = s.get("best_random") or {}
        self._best_daily = s.get("best_daily") or {}
        if s.get("cells"):
            self._mode = s.get("mode", "random")
            self._size = int(s["size"])
            self._difficulty = s["difficulty"]
            self._seed = int(s["seed"])
            self._cells = s["cells"]
            self._blocked = {tuple(c) for c in s["blocked"]}
            self._target = tuple(s["target"])
            self._robot_start = [tuple(r) for r in s["robot_start"]]
            self._robots = [tuple(r) for r in s["robots"]]
            self._optimal = int(s["optimal"])
            self._moves = int(s.get("moves", 0))
            self._history = [tuple(h) for h in s.get("history", [])]
            self._completed = bool(s.get("completed", False))
            self._selected = int(s.get("selected", 0))
        else:
            self._mode = "random"
            self._new_board("random", 8, "medium")

    def _resolve(self, mode, size, difficulty):
        size = size if size in SIZES else 8
        difficulty = difficulty if difficulty in ("easy", "medium", "hard") else "medium"
        if mode == "daily":
            seed = _seed_for(size, difficulty, _daily_seed())
        else:
            seed = int(time.time() * 1000) & 0x7FFFFFFF
        return size, difficulty, seed

    def _apply_board(self, mode, b):
        self._mode = mode
        self._size = b["size"]
        self._difficulty = b["difficulty"]
        self._seed = b["seed"]
        self._cells = b["cells"]
        self._blocked = {tuple(c) for c in b["blocked"]}
        self._target = tuple(b["target"])
        self._robots = [tuple(r) for r in b["robots"]]
        self._robot_start = [tuple(r) for r in b["robots"]]
        self._optimal = b["optimal"]
        self._moves = 0
        self._history = []
        self._completed = False
        self._selected = self._target[0] if self._target[0] != WILDCARD else 0
        self._persist()

    def _new_board(self, mode, size, difficulty):
        """Synchronous generate — used only at first launch, where there is no
        UI yet to flash a 'Generating...' frame to. Interactive new-board taps
        go through the deferred path in on_data/render instead."""
        sz, df, seed = self._resolve(mode, size, difficulty)
        self._apply_board(mode, generate_board(sz, df, seed))

    def _persist(self):
        _save_state(STATE_PATH, {
            "mode": self._mode, "size": self._size, "difficulty": self._difficulty,
            "seed": self._seed, "cells": self._cells,
            "blocked": [list(c) for c in self._blocked],
            "target": list(self._target),
            "robot_start": [list(r) for r in self._robot_start],
            "robots": [list(r) for r in self._robots],
            "optimal": self._optimal, "moves": self._moves,
            "history": [list(h) for h in self._history],
            "completed": self._completed, "selected": self._selected,
            "best_random": self._best_random, "best_daily": self._best_daily,
        })

    def published_state(self):
        ts = self._target[0]
        target_label = "any" if ts == WILDCARD else SHAPES[ts].capitalize()
        if self._mode == "daily":
            best = self._best_daily.get(str(self._seed))
        else:
            best = self._best_random.get(f"{self._size}:{self._difficulty}")
        return {
            "mode": self._mode,
            "seed": str(self._seed),
            "size": self._size,
            "difficulty": self._difficulty,
            "target_shape": target_label,
            "selected": SHAPES[self._selected].capitalize(),
            "moves": self._moves,
            "optimal": self._optimal,
            "best": best if best is not None else "—",
            "completed": self._completed,
            "status": "Solved!" if self._completed else "Solving…",
        }

    # ---- game actions ----
    def _check_win(self):
        ts, tx, ty = self._target
        won = (any(p == (tx, ty) for p in self._robots)
               if ts == WILDCARD else self._robots[ts] == (tx, ty))
        if not won:
            return
        self._completed = True
        if self._mode == "daily":
            key = str(self._seed)
            prev = self._best_daily.get(key)
            if prev is None or self._moves < prev:
                self._best_daily[key] = self._moves
        else:
            key = f"{self._size}:{self._difficulty}"
            prev = self._best_random.get(key)
            if prev is None or self._moves < prev:
                self._best_random[key] = self._moves

    def _move(self, d):
        if self._completed:
            return
        cur = self._robots[self._selected]
        np = slide(self._cells, self._robots, self._selected, d, self._blocked)
        if np == cur:
            return  # no-op: immediately blocked
        self._history.append((self._selected, cur[0], cur[1]))
        self._robots[self._selected] = np
        self._moves += 1
        self._check_win()
        self._persist()

    def _undo(self):
        if self._completed or not self._history:
            return
        ri, px, py = self._history.pop()
        self._robots[ri] = (px, py)
        self._moves = max(0, self._moves - 1)
        self._persist()

    def _restart(self):
        self._robots = list(self._robot_start)
        self._moves = 0
        self._history = []
        self._completed = False
        self._persist()

    # ---- host hooks ----
    def on_data(self, payload):
        payload = payload or {}
        action = payload.get("action")
        with self._lock:
            if action == "select":
                try:
                    self._selected = max(0, min(len(SHAPES) - 1, int(payload.get("robot", 0))))
                except (TypeError, ValueError):
                    return False
                self._persist()
                return True
            if action == "move":
                d = DIR_FROM_NAME.get(payload.get("dir"))
                if d is None:
                    return False
                self._move(d)
                return True
            if action == "undo":
                self._undo()
                return True
            if action == "restart":
                self._restart()
                return True
            if action in ("new_random", "new_daily"):
                try:
                    size = int(payload.get("size", 8))
                except (TypeError, ValueError):
                    size = 8
                difficulty = payload.get("difficulty", "medium")
                # Queue only — the render thread generates (see render). This
                # returns instantly so the push response isn't blocked, and rapid
                # repeat taps just overwrite the request instead of generating a
                # board per tap.
                self._pending = ("daily" if action == "new_daily" else "random",
                                 size, difficulty)
                self._gen_phase = "announce"
                self.interval_seconds = 0.05  # nudge the render loop to re-fire
                return True
        return False

    # ---- rendering ----
    def _font(self, size):
        f = self._fonts.get(size)
        if f is None:
            try:
                f = ImageFont.truetype("DejaVuSans.ttf", size)
            except Exception:
                f = ImageFont.load_default()
            self._fonts[size] = f
        return f

    def _draw_shape(self, draw, x0, y0, cell, shape, fill):
        p = max(1, cell // 6)
        a, b = x0 + p, y0 + p
        c, d = x0 + cell - p, y0 + cell - p
        if shape == WILDCARD:  # wildcard target: an asterisk/cross outline
            mx, my = (a + c) // 2, (b + d) // 2
            draw.line((a, my, c, my), fill=0)
            draw.line((mx, b, mx, d), fill=0)
            draw.line((a, b, c, d), fill=0)
            draw.line((a, d, c, b), fill=0)
            return
        kw = {"fill": 0} if fill else {"outline": 0}
        if shape == 0:
            draw.ellipse((a, b, c, d), **kw)
        elif shape == 1:
            # The square fills its bounding box to the corners, so it reads
            # bigger than the other shapes and swallows the selection ring.
            # Draw it at 70% (centered) so the border stays visible.
            side = (c - a) * 7 // 10
            off = ((c - a) - side) // 2
            draw.rectangle((a + off, b + off, a + off + side, b + off + side), **kw)
        elif shape == 2:
            draw.polygon([((a + c) // 2, b), (a, d), (c, d)], **kw)
        elif shape == 3:
            draw.polygon([((a + c) // 2, b), (c, (b + d) // 2),
                          ((a + c) // 2, d), (a, (b + d) // 2)], **kw)

    def render(self, draw, w, h):
        # Deferred board generation. A new-board tap only sets self._pending +
        # phase "announce". The first render after that paints "Generating..."
        # and flips to "generate"; the *next* render actually builds the board —
        # so the wait frame reaches the e-ink before the (possibly multi-second)
        # search begins. Generation runs WITHOUT the lock so on_data never blocks.
        with self._lock:
            pending, phase = self._pending, self._gen_phase
        if pending is not None and phase == "announce":
            with self._lock:
                self._gen_phase = "generate"
            try:
                self._paint_generating(draw, w, h, pending)
            except Exception:
                logging.exception("ricochet_robots: generating frame failed")
            return
        if pending is not None and phase == "generate":
            mode, size, difficulty = pending
            board = None
            try:
                sz, df, seed = self._resolve(mode, size, difficulty)
                board = generate_board(sz, df, seed)
            except Exception:
                logging.exception("ricochet_robots: generation failed")
            with self._lock:
                if board is not None:
                    self._apply_board(mode, board)
                if self._pending == pending:  # no newer tap arrived during generation
                    self._pending = None
                    self._gen_phase = None
                    self.interval_seconds = None
        with self._lock:
            try:
                self._paint(draw, w, h)
            except Exception:
                logging.exception("ricochet_robots: render failed")
                draw.text((4, 4), "RICOCHET — render error", font=self._font(10), fill=0)

    def _paint_generating(self, draw, w, h, pending):
        big = self._font(14)
        fs = self._font(9)
        msg = "Generating..."
        mw = int(draw.textlength(msg, font=big))
        draw.text(((w - mw) // 2, h // 2 - 14), msg, font=big, fill=0)
        sub = f"{pending[1]}x{pending[1]}  {pending[2]}"
        sw = int(draw.textlength(sub, font=fs))
        draw.text(((w - sw) // 2, h // 2 + 6), sub, font=fs, fill=0)

    def _paint(self, draw, w, h):
        n = self._size
        margin = 2
        board_side = min(h - 2 * margin, w - MIN_HUD_W - 4)
        cell = max(4, board_side // n)
        board_side = cell * n
        bx, by = margin, (h - board_side) // 2
        draw.rectangle((bx, by, bx + board_side, by + board_side), outline=0)
        # walls
        for y in range(n):
            for x in range(n):
                x0, y0 = bx + x * cell, by + y * cell
                mask = self._cells[y][x]
                if mask & WALL_N:
                    draw.line((x0, y0, x0 + cell, y0), fill=0, width=2)
                if mask & WALL_W:
                    draw.line((x0, y0, x0, y0 + cell), fill=0, width=2)
                if mask & WALL_S:
                    draw.line((x0, y0 + cell, x0 + cell, y0 + cell), fill=0, width=2)
                if mask & WALL_E:
                    draw.line((x0 + cell, y0, x0 + cell, y0 + cell), fill=0, width=2)
        # center block
        for (cxb, cyb) in self._blocked:
            x0, y0 = bx + cxb * cell, by + cyb * cell
            draw.rectangle((x0, y0, x0 + cell, y0 + cell), fill=0)
        # target (outline of the matching shape)
        ts, tx, ty = self._target
        self._draw_shape(draw, bx + tx * cell, by + ty * cell, cell, ts, fill=False)
        # robots (filled), with a selection ring on the active one
        for i, (rx, ry) in enumerate(self._robots):
            self._draw_shape(draw, bx + rx * cell, by + ry * cell, cell, i, fill=True)
            if i == self._selected:
                x0, y0 = bx + rx * cell, by + ry * cell
                draw.rectangle((x0 + 1, y0 + 1, x0 + cell - 1, y0 + cell - 1), outline=0)
        # HUD column
        self._paint_hud(draw, bx + board_side + 6, by, w - 2)
        if self._completed:
            self._paint_victory(draw, w, h)

    def _paint_hud(self, draw, x0, y0, x1):
        f = self._font(11)
        fs = self._font(9)
        st = self.published_state()
        y = y0
        draw.text((x0, y), "RICOCHET", font=f, fill=0); y += 13
        draw.text((x0, y), f"{st['mode'].upper()} #{st['seed'][-4:]}", font=fs, fill=0); y += 11
        draw.text((x0, y), f"{st['size']}x{st['size']} {st['difficulty'][:1].upper()}", font=fs, fill=0); y += 13
        draw.text((x0, y), "Target:", font=fs, fill=0)
        self._draw_shape(draw, x1 - 13, y - 2, 13, self._target[0], fill=False); y += 14
        draw.text((x0, y), f"Move: {st['selected']}", font=fs, fill=0); y += 11
        draw.text((x0, y), f"Moves: {st['moves']}", font=fs, fill=0); y += 11
        draw.text((x0, y), f"Optimum: {st['optimal']}", font=fs, fill=0); y += 11
        draw.text((x0, y), f"Best: {st['best']}", font=fs, fill=0)

    def _paint_victory(self, draw, w, h):
        big = self._font(16)
        fs = self._font(9)
        msg = "SOLVED"
        mw = int(draw.textlength(msg, font=big))
        draw.rectangle((w // 2 - mw // 2 - 8, h // 2 - 22,
                        w // 2 + mw // 2 + 8, h // 2 + 14), fill=1, outline=0)
        draw.text((w // 2 - mw // 2, h // 2 - 18), msg, font=big, fill=0)
        line = f"{self._moves} moves / opt {self._optimal}"
        lw = int(draw.textlength(line, font=fs))
        draw.text((w // 2 - lw // 2, h // 2 + 2), line, font=fs, fill=0)
