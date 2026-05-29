# Maze — first-person crawler with random + daily-seeded generation.
#
# Layout (landscape e-ink, typically 250x122):
#   +------------------------------------------------+
#   | MAZE      RANDOM #1234           42 moves      |
#   +--------------------------+---------------------+
#   |                          |   minimap           |
#   |     first-person view    |                     |
#   |     (line-art walls,     |                     |
#   |      vanishing point)    |   face N  best 23   |
#   |                          |   (3,2)             |
#   +--------------------------+---------------------+
#
# Maze generation is on-device (recursive backtracker, ~30 lines). Seed makes
# it shareable: daily-mode uses date.toordinal() so every device gets the
# same maze each day. State lives in /etc/pwnagotchi/maze.state.json — only
# the seed + player pose is persisted; the wall grid is reconstructed.
#
# Push schema (phone → device):
#   {"action": "forward"}
#   {"action": "back"}
#   {"action": "turn_left"}
#   {"action": "turn_right"}
#   {"action": "new_random"}
#   {"action": "new_daily"}

import json
import logging
import os
import random
import threading
import time
from collections import deque
from datetime import date

from PIL import ImageFont


STATE_PATH = "/etc/pwnagotchi/maze.state.json"

MAZE_W = 10
MAZE_H = 7

# Wall bit per direction. Direction enum: 0=N 1=E 2=S 3=W. y grows downward.
WALL_BITS = [1, 2, 4, 8]
DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]
DIR_NAMES = ["N", "E", "S", "W"]


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


def _generate_maze(width, height, seed):
    """Recursive backtracker. Returns cells[y][x] = wall bitmask (1=N 2=E 4=S 8=W)."""
    rng = random.Random(seed)
    cells = [[0b1111 for _ in range(width)] for _ in range(height)]
    visited = [[False] * width for _ in range(height)]
    stack = [(0, 0)]
    visited[0][0] = True
    while stack:
        x, y = stack[-1]
        nbrs = []
        for d, (dx, dy) in enumerate(DIRS):
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and not visited[ny][nx]:
                nbrs.append((nx, ny, d))
        if not nbrs:
            stack.pop()
            continue
        nx, ny, d = rng.choice(nbrs)
        cells[y][x] &= ~WALL_BITS[d]
        cells[ny][nx] &= ~WALL_BITS[(d + 2) % 4]
        visited[ny][nx] = True
        stack.append((nx, ny))
    return cells


def _bfs_distances(cells, src):
    """BFS from src across open corridors. Returns dist[y][x], -1 = unreachable."""
    width = len(cells[0])
    height = len(cells)
    dist = [[-1] * width for _ in range(height)]
    sx, sy = src
    dist[sy][sx] = 0
    q = deque([src])
    while q:
        x, y = q.popleft()
        for d, (dx, dy) in enumerate(DIRS):
            if cells[y][x] & WALL_BITS[d]:
                continue
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and dist[ny][nx] == -1:
                dist[ny][nx] = dist[y][x] + 1
                q.append((nx, ny))
    return dist


def _bfs_farthest(cells, start):
    """Returns ((x, y), distance) for the farthest reachable cell from start."""
    dist = _bfs_distances(cells, start)
    width = len(cells[0])
    height = len(cells)
    far = start
    for y in range(height):
        for x in range(width):
            if dist[y][x] > dist[far[1]][far[0]]:
                far = (x, y)
    return far, dist[far[1]][far[0]]


TORCH_MIN_OPTIMUM = 31
TORCH_DIST_MIN = 20
TORCH_DIST_MAX = 30
TORCH_RETRY_CAP = 64
TORCH_DURATION = 40
FPV_MAX_DEPTH = 4


def _torch_placement(cells, exit_xy, rng):
    """Pick a cell whose BFS distance from the exit is in [TORCH_DIST_MIN,
    TORCH_DIST_MAX]. If the maze is too small for any such cell, fall back
    to the cell farthest from the exit so a torch always exists."""
    dist = _bfs_distances(cells, exit_xy)
    target = rng.randint(TORCH_DIST_MIN, TORCH_DIST_MAX)
    candidates = [(x, y) for y, row in enumerate(dist)
                  for x, d in enumerate(row) if d == target]
    if candidates:
        return rng.choice(candidates)
    for d in range(TORCH_DIST_MAX, TORCH_DIST_MIN - 1, -1):
        picks = [(x, y) for y, row in enumerate(dist)
                 for x, dd in enumerate(row) if dd == d]
        if picks:
            return rng.choice(picks)
    # Maze is shorter than the preferred range — use the deepest reachable cell.
    far_x, far_y = exit_xy
    for y, row in enumerate(dist):
        for x, d in enumerate(row):
            if d > dist[far_y][far_x]:
                far_x, far_y = x, y
    return (far_x, far_y) if (far_x, far_y) != exit_xy else None


def _daily_seed():
    return date.today().toordinal()


def _initial_facing(cells):
    """Face the first open direction from (0,0), preferring E then S then N then W."""
    wall = cells[0][0]
    for d in (1, 2, 0, 3):
        if not (wall & WALL_BITS[d]):
            return d
    return 1


class Maze:
    name = "maze"
    icon = "MZ"
    version = "1.6.1"

    interval_seconds = None

    def __init__(self):
        self._lock = threading.RLock()
        self._fonts = {}

        s = _load_state(STATE_PATH)
        self._wins = int(s.get("wins", 0))
        self._best_random = s.get("best_random")
        self._best_daily = s.get("best_daily") or {}

        self._maze_w = int(s.get("maze_w") or MAZE_W)
        self._maze_h = int(s.get("maze_h") or MAZE_H)

        seed = s.get("seed")
        mode = s.get("mode", "random")
        if seed is None:
            # First launch — give the user a maze to play with.
            mode = "random"
            seed = int(time.time() * 1000) & 0x7fffffff
        self._mode = mode
        self._seed = int(seed)
        self._cells = _generate_maze(self._maze_w, self._maze_h, self._seed)

        ex_default, opt_default = _bfs_farthest(self._cells, (0, 0))
        self._exit_x = int(s.get("exit_x", ex_default[0]))
        self._exit_y = int(s.get("exit_y", ex_default[1]))
        self._optimal = int(s.get("optimal", opt_default))

        self._player_x = int(s.get("player_x", 0))
        self._player_y = int(s.get("player_y", 0))
        self._facing = int(s.get("facing", _initial_facing(self._cells)))
        self._moves = int(s.get("moves", 0))
        self._completed = bool(s.get("completed", False))
        self._started_at = float(s.get("started_at") or time.time())

        # Fog of war: only cells the player has stood on count as discovered.
        # Older state files don't carry this; seed from the current pose so
        # in-progress games don't suddenly reveal the whole map.
        raw_visited = s.get("visited")
        if isinstance(raw_visited, list) and raw_visited:
            self._visited = {(int(p[0]), int(p[1])) for p in raw_visited
                             if isinstance(p, (list, tuple)) and len(p) >= 2}
        else:
            self._visited = {(self._player_x, self._player_y)}

        # Torch mode: when armed, the next New Maze places a torch on the
        # critical path. Until the player walks onto it, the minimap is
        # fully fogged; pickup reveals the full layout.
        self._torch_mode_enabled = bool(s.get("torch_mode_enabled", False))
        tx, ty = s.get("torch_x"), s.get("torch_y")
        self._torch_x = int(tx) if isinstance(tx, int) else None
        self._torch_y = int(ty) if isinstance(ty, int) else None
        self._torch_found = bool(s.get("torch_found", False))
        tml = s.get("torch_moves_left")
        self._torch_moves_left = int(tml) if isinstance(tml, int) else None

    # ---- persistence ----
    def _persist(self):
        _save_state(STATE_PATH, {
            "mode": self._mode,
            "seed": self._seed,
            "maze_w": self._maze_w,
            "maze_h": self._maze_h,
            "exit_x": self._exit_x,
            "exit_y": self._exit_y,
            "optimal": self._optimal,
            "player_x": self._player_x,
            "player_y": self._player_y,
            "facing": self._facing,
            "moves": self._moves,
            "completed": self._completed,
            "started_at": self._started_at,
            "wins": self._wins,
            "best_random": self._best_random,
            "best_daily": self._best_daily,
            "visited": [list(p) for p in self._visited],
            "torch_mode_enabled": self._torch_mode_enabled,
            "torch_x": self._torch_x,
            "torch_y": self._torch_y,
            "torch_found": self._torch_found,
            "torch_moves_left": self._torch_moves_left,
        })

    # ---- game flow ----
    def _new_game(self, mode):
        # Always pull the latest module-level dimensions so version bumps that
        # change maze size take effect on the next New-Maze press.
        self._maze_w = MAZE_W
        self._maze_h = MAZE_H

        if mode == "daily":
            base_seed = _daily_seed()
        else:
            base_seed = int(time.time() * 1000) & 0x7fffffff

        torch_armed = self._torch_mode_enabled
        # Torch mode requires a maze long enough to make finding the torch
        # interesting. Advance the seed deterministically until the optimum
        # crosses the floor so every device armed on day D converges on the
        # same maze.
        seed = base_seed
        cells = None
        exit_xy = None
        optimal = 0
        for _attempt in range(TORCH_RETRY_CAP):
            cells = _generate_maze(self._maze_w, self._maze_h, seed)
            exit_xy, optimal = _bfs_farthest(cells, (0, 0))
            if not torch_armed or optimal >= TORCH_MIN_OPTIMUM:
                break
            seed = (seed + 1) & 0x7fffffff
        else:
            # Couldn't satisfy the floor — play without a torch rather than
            # refuse to start.
            cells = _generate_maze(self._maze_w, self._maze_h, base_seed)
            exit_xy, optimal = _bfs_farthest(cells, (0, 0))
            torch_armed = False
            seed = base_seed

        self._mode = mode
        self._seed = seed
        self._cells = cells
        self._exit_x, self._exit_y = exit_xy
        self._optimal = optimal

        if torch_armed:
            # Deterministic torch placement: derive an RNG from the (final)
            # seed so daily players see the torch in the same cell.
            place_rng = random.Random(seed ^ 0x70_1C_4E)
            torch = _torch_placement(self._cells, exit_xy, place_rng)
            self._torch_x = torch[0] if torch else None
            self._torch_y = torch[1] if torch else None
        else:
            self._torch_x = None
            self._torch_y = None

        self._player_x = 0
        self._player_y = 0
        self._facing = _initial_facing(self._cells)
        self._moves = 0
        self._completed = False
        self._started_at = time.time()
        self._visited = {(0, 0)}
        self._torch_found = False
        self._torch_moves_left = None
        self._persist()

    def _step(self, direction):
        x, y = self._player_x, self._player_y
        if self._cells[y][x] & WALL_BITS[direction]:
            return
        dx, dy = DIRS[direction]
        nx, ny = x + dx, y + dy
        if not (0 <= nx < self._maze_w and 0 <= ny < self._maze_h):
            return
        # Tick the torch *before* applying the new position, so the step that
        # picks up the torch doesn't immediately count against its duration.
        if (self._torch_found
                and self._torch_moves_left is not None
                and self._torch_moves_left > 0):
            self._torch_moves_left -= 1
        self._player_x = nx
        self._player_y = ny
        self._moves += 1
        self._visited.add((nx, ny))
        if (self._torch_x is not None and not self._torch_found
                and (nx, ny) == (self._torch_x, self._torch_y)):
            self._torch_found = True
            self._torch_moves_left = TORCH_DURATION

    def _handle(self, action):
        if self._completed:
            return
        if action == "turn_left":
            self._facing = (self._facing - 1) % 4
        elif action == "turn_right":
            self._facing = (self._facing + 1) % 4
        elif action == "forward":
            self._step(self._facing)
        elif action == "back":
            self._step((self._facing + 2) % 4)
        self._check_win()
        self._persist()

    def _check_win(self):
        if self._completed:
            return
        if (self._player_x, self._player_y) == (self._exit_x, self._exit_y):
            self._completed = True
            self._wins += 1
            if self._mode == "daily":
                key = date.today().isoformat()
                prev = self._best_daily.get(key)
                if prev is None or self._moves < prev:
                    self._best_daily[key] = self._moves
            else:
                if self._best_random is None or self._moves < self._best_random:
                    self._best_random = self._moves

    # ---- host hooks ----
    def on_data(self, payload):
        payload = payload or {}
        action = payload.get("action")
        with self._lock:
            if action == "new_random":
                self._new_game("random")
                return True
            if action == "new_daily":
                self._new_game("daily")
                return True
            if action == "set_torch_mode":
                # Arm/disarm — affects the *next* New Maze, not the current one.
                self._torch_mode_enabled = bool(payload.get("enabled", False))
                self._persist()
                return True
            if action in ("forward", "back", "turn_left", "turn_right"):
                self._handle(action)
                return True
        return False

    def _visible_cells(self):
        """Returns the set of cells whose walls are *currently* drawn on the
        minimap. The memory trail (visited cells, persistently un-fogged) is
        applied separately at paint time via the `explored` set.

        Torch mode rules:
          - torch armed, not found  → only the player's cell (true darkness)
          - torch armed, found      → 3x3 box around the player (through walls)
                                      plus corridor sight in the facing
                                      direction until a wall blocks; this set
                                      moves with the player, it is not banked
          - torch disarmed          → visited cells + corridor sight from each
        """
        px, py = self._player_x, self._player_y

        if self._torch_x is not None:
            torch_lit = (self._torch_found
                         and self._torch_moves_left is not None
                         and self._torch_moves_left > 0)
            if not torch_lit:
                # Pre-pickup darkness OR the torch has burned out.
                return {(px, py)}
            lit = set()
            # 1-step "see through walls" halo: 3x3 box around the player.
            for ddy in (-1, 0, 1):
                for ddx in (-1, 0, 1):
                    nx, ny = px + ddx, py + ddy
                    if 0 <= nx < self._maze_w and 0 <= ny < self._maze_h:
                        lit.add((nx, ny))
            # Corridor sight forward, blocked by the first wall in the way.
            d = self._facing
            dx, dy = DIRS[d]
            x, y = px, py
            while not (self._cells[y][x] & WALL_BITS[d]):
                x += dx
                y += dy
                if not (0 <= x < self._maze_w and 0 <= y < self._maze_h):
                    break
                lit.add((x, y))
            return lit

        seen = set(self._visited)
        for vx, vy in list(self._visited):
            for d, (dx, dy) in enumerate(DIRS):
                x, y = vx, vy
                while not (self._cells[y][x] & WALL_BITS[d]):
                    x += dx
                    y += dy
                    if not (0 <= x < self._maze_w and 0 <= y < self._maze_h):
                        break
                    seen.add((x, y))
        return seen

    def published_state(self):
        if self._mode == "daily":
            best = self._best_daily.get(date.today().isoformat())
        else:
            best = self._best_random
        total_cells = self._maze_w * self._maze_h
        if self._torch_x is None:
            torch_status = "off"
        elif not self._torch_found:
            torch_status = "armed"
        elif (self._torch_moves_left is not None
              and self._torch_moves_left > 0):
            torch_status = f"lit • {self._torch_moves_left} steps"
        else:
            torch_status = "burned out"
        return {
            "mode": "daily" if self._mode == "daily" else "random",
            "moves": self._moves,
            "facing": DIR_NAMES[self._facing],
            "completed": self._completed,
            "wins": self._wins,
            "optimal": self._optimal,
            "best": best if best is not None else "—",
            "seed": str(self._seed),
            "seen": f"{len(self._visited)}/{total_cells}",
            "torch_mode": "on" if self._torch_mode_enabled else "off",
            "torch_status": torch_status,
        }

    # ---- rendering ----
    def _font(self, size):
        f = self._fonts.get(size)
        if f is None:
            f = ImageFont.truetype("DejaVuSansMono-Bold", size)
            self._fonts[size] = f
        return f

    def render(self, draw, w, h):
        with self._lock:
            mode = self._mode
            seed = self._seed
            moves = self._moves
            optimal = self._optimal
            completed = self._completed
            facing = self._facing
            px, py = self._player_x, self._player_y
            ex, ey = self._exit_x, self._exit_y
            cells = [row[:] for row in self._cells]
            best_random = self._best_random
            best_daily = dict(self._best_daily)
            visible = self._visible_cells()
            explored = set(self._visited)
            torch_lit_now = (
                self._torch_x is not None
                and self._torch_found
                and self._torch_moves_left is not None
                and self._torch_moves_left > 0
            )
            torch_render = {
                "x": self._torch_x,
                "y": self._torch_y,
                "found": self._torch_found,
                "lit_now": torch_lit_now,
            }

        try:
            self._paint(draw, w, h, mode, seed, moves, optimal, completed,
                        facing, px, py, ex, ey, cells, best_random,
                        best_daily, visible, explored, torch_render)
        except Exception:
            logging.exception("maze: render failed")
            # Fall back to a plain text screen so the device isn't blank.
            f = self._font(10)
            draw.text((4, 4), "MAZE — render error", font=f, fill=0)

    def _paint(self, draw, w, h, mode, seed, moves, optimal, completed,
               facing, px, py, ex, ey, cells, best_random, best_daily,
               visible, explored, torch_render):
        title_f = self._font(10)
        small_f = self._font(8)
        big_f = self._font(14)

        # ---- title bar ----
        draw.text((2, 1), "MAZE", font=title_f, fill=0)
        mode_label = "DAILY" if mode == "daily" else "RANDOM"
        seed_short = str(seed)[-4:]
        mid = f"{mode_label} #{seed_short}"
        mw = int(draw.textlength(mid, font=small_f))
        draw.text(((w - mw) // 2, 3), mid, font=small_f, fill=0)
        right = f"{moves} mv"
        rw = int(draw.textlength(right, font=small_f))
        draw.text((w - rw - 2, 3), right, font=small_f, fill=0)
        draw.line((2, 12, w - 2, 12), fill=0)

        body_top = 14
        body_bot = h - 2

        if completed:
            self._paint_victory(draw, w, body_top, body_bot, moves, optimal,
                                mode, best_random, best_daily, big_f, small_f)
            return

        # ---- split into FPV (left) and info (right) ----
        gap = 4
        fpv_w = max(60, int((w - 6) * 0.62))
        fpv_box = (2, body_top, 2 + fpv_w, body_bot)
        info_box = (2 + fpv_w + gap, body_top, w - 2, body_bot)

        self._paint_fpv(draw, cells, px, py, facing, ex, ey, fpv_box,
                        torch_render)
        self._paint_info(draw, info_box, cells, px, py, facing, ex, ey,
                         mode, best_random, best_daily, optimal, small_f,
                         visible, explored, torch_render)

    # ---- victory screen ----
    def _paint_victory(self, draw, w, top, bot, moves, optimal, mode,
                       best_random, best_daily, big_f, small_f):
        msg = "EXIT FOUND!"
        mw = int(draw.textlength(msg, font=big_f))
        cy = (top + bot) // 2
        draw.text(((w - mw) // 2, cy - 20), msg, font=big_f, fill=0)

        line = f"{moves} moves   optimum {optimal}"
        lw = int(draw.textlength(line, font=small_f))
        draw.text(((w - lw) // 2, cy), line, font=small_f, fill=0)

        if mode == "daily":
            best = best_daily.get(date.today().isoformat())
        else:
            best = best_random
        if best is not None:
            bl = f"best {best}"
            bw = int(draw.textlength(bl, font=small_f))
            draw.text(((w - bw) // 2, cy + 12), bl, font=small_f, fill=0)

        hint = "tap New Maze on the phone"
        hw = int(draw.textlength(hint, font=small_f))
        draw.text(((w - hw) // 2, bot - 10), hint, font=small_f, fill=0)

    # ---- first-person view ----
    def _paint_fpv(self, draw, cells, px, py, facing, ex, ey, box,
                   torch_render):
        x0, y0, x1, y1 = box
        # White background + black border framing the view.
        draw.rectangle((x0, y0, x1, y1), outline=0, fill=1)

        MAX_DEPTH = FPV_MAX_DEPTH
        SHRINK = 0.55
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        # frames[d] is the screen-space rectangle of the boundary between
        # cell d-1 (nearer) and cell d (farther); frames[0] is the screen
        # edge, frames[MAX_DEPTH] is the far vanishing rectangle.
        frames = []
        for d in range(MAX_DEPTH + 1):
            s = SHRINK ** d
            frames.append((
                cx - (cx - x0) * s,
                cy - (cy - y0) * s,
                cx + (x1 - cx) * s,
                cy + (y1 - cy) * s,
            ))

        fdx, fdy = DIRS[facing]
        left_dir = (facing - 1) % 4
        right_dir = (facing + 1) % 4

        prev_left = True   # screen edge already provides a "wall" at d=0
        prev_right = True
        closed = False
        # Markers are recorded here and painted *after* the wall loop so a
        # closing wall filling the same frame can't erase them.
        exit_far = None
        torch_far = None
        torch_x = torch_render.get("x")
        torch_y = torch_render.get("y")
        torch_visible = (torch_x is not None and not torch_render.get("found"))

        for d in range(MAX_DEPTH):
            tx, ty = px + fdx * d, py + fdy * d
            if not (0 <= tx < self._maze_w and 0 <= ty < self._maze_h):
                self._closing_wall(draw, frames[d])
                closed = True
                break

            wall = cells[ty][tx]
            has_left = bool(wall & WALL_BITS[left_dir])
            has_right = bool(wall & WALL_BITS[right_dir])
            has_front = bool(wall & WALL_BITS[facing])
            near = frames[d]
            far = frames[d + 1]

            if has_left:
                self._left_wall(draw, near, far,
                                near_post=(d > 0 and not prev_left))
            if has_right:
                self._right_wall(draw, near, far,
                                 near_post=(d > 0 and not prev_right))

            # Exit marker — only meaningful in cells the player can see
            # ahead (d>=1). Recorded at the far edge so it appears to recede.
            if d >= 1 and (tx, ty) == (ex, ey) and exit_far is None:
                exit_far = far

            # Torch — same rule: only when directly down the visible corridor.
            if (d >= 1 and torch_visible and torch_far is None
                    and (tx, ty) == (torch_x, torch_y)):
                torch_far = far

            if has_front:
                self._closing_wall(draw, far)
                closed = True
                break

            prev_left = has_left
            prev_right = has_right

        if not closed:
            self._closing_wall(draw, frames[MAX_DEPTH])

        # Paint markers last so the closing wall of their own cell (or the far
        # vanishing wall) doesn't paint over them.
        if exit_far is not None:
            self._exit_marker(draw, exit_far)
        if torch_far is not None:
            self._torch_marker(draw, torch_far)

        # Tiny compass letter in the top-left corner of the view.
        cf = self._font(8)
        draw.rectangle((x0 + 1, y0 + 1, x0 + 11, y0 + 10), fill=1)
        draw.text((x0 + 3, y0 + 1), DIR_NAMES[facing], font=cf, fill=0)

    def _closing_wall(self, draw, frame):
        fx0, fy0, fx1, fy1 = frame
        draw.rectangle((fx0, fy0, fx1, fy1), outline=0, fill=1)
        # Sparse dot stipple so the wall reads as a surface, not just a box.
        sx, sy = int(fx0) + 3, int(fy0) + 3
        ex, ey = int(fx1) - 1, int(fy1) - 1
        step = 4
        for yy in range(sy, ey, step):
            for xx in range(sx, ex, step):
                draw.point((xx, yy), fill=0)

    def _left_wall(self, draw, near, far, near_post=False):
        nx0, ny0, _nx1, ny1 = near
        fx0, fy0, _fx1, fy1 = far
        draw.line((nx0, ny0, fx0, fy0), fill=0)  # top edge receding
        draw.line((nx0, ny1, fx0, fy1), fill=0)  # bottom edge receding
        draw.line((fx0, fy0, fx0, fy1), fill=0)  # far vertical (wall end)
        if near_post:
            draw.line((nx0, ny0, nx0, ny1), fill=0)

    def _right_wall(self, draw, near, far, near_post=False):
        _nx0, ny0, nx1, ny1 = near
        _fx0, fy0, fx1, fy1 = far
        draw.line((nx1, ny0, fx1, fy0), fill=0)
        draw.line((nx1, ny1, fx1, fy1), fill=0)
        draw.line((fx1, fy0, fx1, fy1), fill=0)
        if near_post:
            draw.line((nx1, ny0, nx1, ny1), fill=0)

    def _exit_marker(self, draw, far_frame):
        fx0, fy0, fx1, fy1 = far_frame
        cxp = (fx0 + fx1) / 2
        cyp = (fy0 + fy1) / 2
        size = max(2, min(fx1 - fx0, fy1 - fy0) / 3)
        draw.line((cxp - size, cyp - size, cxp + size, cyp + size), fill=0)
        draw.line((cxp + size, cyp - size, cxp - size, cyp + size), fill=0)

    def _dotted_line(self, draw, x0, y0, x1, y1, step=2):
        """Dot an axis-aligned segment (single pixels every `step`)."""
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        if y0 == y1:
            for x in range(x0, x1 + 1, step):
                draw.point((x, y0), fill=0)
        else:
            for y in range(y0, y1 + 1, step):
                draw.point((x0, y), fill=0)

    def _torch_halo_outline(self, draw, lit, ox, oy, cs):
        """Dot the perimeter of the lit region: for each lit cell, dot only the
        edges whose neighbour is unlit (or off-grid). Shared interior edges
        stay blank, so the illuminated area reads as one outlined blob."""
        for (lx, ly) in lit:
            cx0 = ox + lx * cs
            cy0 = oy + ly * cs
            cx1 = cx0 + cs
            cy1 = cy0 + cs
            if (lx, ly - 1) not in lit:
                self._dotted_line(draw, cx0, cy0, cx1, cy0)
            if (lx, ly + 1) not in lit:
                self._dotted_line(draw, cx0, cy1, cx1, cy1)
            if (lx - 1, ly) not in lit:
                self._dotted_line(draw, cx0, cy0, cx0, cy1)
            if (lx + 1, ly) not in lit:
                self._dotted_line(draw, cx1, cy0, cx1, cy1)

    def _torch_marker(self, draw, far_frame):
        # A "T" shape — flame on top, handle below — clearly distinct from
        # the exit's X. Centered in the far frame so it appears to recede.
        fx0, fy0, fx1, fy1 = far_frame
        cxp = (fx0 + fx1) / 2
        cyp = (fy0 + fy1) / 2
        size = max(2, min(fx1 - fx0, fy1 - fy0) / 3)
        draw.line((cxp - size, cyp - size, cxp + size, cyp - size), fill=0)
        draw.line((cxp, cyp - size, cxp, cyp + size), fill=0)

    # ---- info panel: minimap + stats ----
    def _paint_info(self, draw, box, cells, px, py, facing, ex, ey,
                    mode, best_random, best_daily, optimal, small_f,
                    visible, explored, torch_render):
        x0, y0, x1, y1 = box
        info_text_h = 20
        map_box = (x0, y0, x1, y1 - info_text_h)
        text_top = y1 - info_text_h + 1

        mw_cells = self._maze_w
        mh_cells = self._maze_h
        aw = map_box[2] - map_box[0]
        ah = map_box[3] - map_box[1]
        if aw > 8 and ah > 8:
            cs = max(3, min(aw // mw_cells, ah // mh_cells, 9))
            tmw = cs * mw_cells
            tmh = cs * mh_cells
            ox = map_box[0] + (aw - tmw) // 2
            # Top-align so the map sits flush under the title bar; vertical
            # slack ends up below the stats lines instead of around the map.
            oy = map_box[1]

            # Outer perimeter: always drawn so the player can read the maze's
            # overall size from move 1, regardless of mode.
            draw.rectangle((ox, oy, ox + tmw, oy + tmh), outline=0)

            # Wall memory = cells the player physically stood on; their walls
            # stay drawn forever. Torch halo / corridor sight reveals walls
            # transiently in `visible` cells but those decay when the player
            # leaves, unless they were stepped on.
            wall_cells = visible | explored

            # Fog texture: three dots per cell, on cells outside wall memory
            # and outside the current lit set. Conveys maze extent without
            # revealing layout.
            fog_offsets = (
                (cs // 4, cs // 4),
                (cs // 2, cs // 2),
                (3 * cs // 4, 3 * cs // 4),
            )
            for y in range(mh_cells):
                for x in range(mw_cells):
                    if (x, y) in wall_cells:
                        continue
                    cxp = ox + x * cs
                    cyp = oy + y * cs
                    for fx, fy in fog_offsets:
                        draw.point((cxp + fx, cyp + fy), fill=0)

            # Walls — draw on every cell the player has ever revealed.
            for y in range(mh_cells):
                for x in range(mw_cells):
                    if (x, y) not in wall_cells:
                        continue
                    wall = cells[y][x]
                    cxp = ox + x * cs
                    cyp = oy + y * cs
                    if wall & WALL_BITS[0]:  # N
                        draw.line((cxp, cyp, cxp + cs, cyp), fill=0)
                    if wall & WALL_BITS[1]:  # E
                        draw.line((cxp + cs, cyp, cxp + cs, cyp + cs), fill=0)
                    if wall & WALL_BITS[2]:  # S
                        draw.line((cxp, cyp + cs, cxp + cs, cyp + cs), fill=0)
                    if wall & WALL_BITS[3]:  # W
                        draw.line((cxp, cyp, cxp, cyp + cs), fill=0)

            # Exit — revealed once seen (lit) or once visited.
            if (ex, ey) in wall_cells:
                exp = ox + ex * cs
                eyp = oy + ey * cs
                if cs >= 5:
                    draw.rectangle((exp + 2, eyp + 2,
                                    exp + cs - 2, eyp + cs - 2), fill=0)
                else:
                    draw.point((exp + cs // 2, eyp + cs // 2), fill=0)

            # Torch — shown on the minimap when it lies directly down the
            # current forward corridor (same line of sight that puts it in
            # the FPV). Walls stay hidden either way; this is just the
            # "I see something glittering ahead" hint the player needs.
            torch_x = torch_render.get("x")
            torch_y = torch_render.get("y")
            if torch_x is not None and not torch_render.get("found"):
                ddx, ddy = DIRS[facing]
                sx, sy = px, py
                # Match the FPV's reachable depth: it can only draw the torch
                # at distances 1..FPV_MAX_DEPTH-1, so don't hint a torch the
                # 3D view won't show.
                for _ in range(FPV_MAX_DEPTH - 1):
                    if cells[sy][sx] & WALL_BITS[facing]:
                        break
                    sx += ddx
                    sy += ddy
                    if not (0 <= sx < mw_cells and 0 <= sy < mh_cells):
                        break
                    if (sx, sy) == (torch_x, torch_y):
                        txp = ox + torch_x * cs
                        typ = oy + torch_y * cs
                        if cs >= 5:
                            cxp = txp + cs // 2
                            cyp = typ + cs // 2
                            s = max(1, cs // 3)
                            draw.line((cxp - s, cyp - s,
                                       cxp + s, cyp - s), fill=0)
                            draw.line((cxp, cyp - s, cxp, cyp + s), fill=0)
                        else:
                            draw.point((txp + cs // 2, typ + cs // 2), fill=0)
                        break

            # Torch-active overlay — only while the player carries a burning
            # torch. A single dotted line traces the perimeter of the lit
            # region (3x3 halo plus the forward corridor sight), telegraphing
            # the torch's reach without cluttering every cell.
            if torch_render.get("lit_now"):
                self._torch_halo_outline(draw, visible, ox, oy, cs)

            # Player — triangular arrow showing facing.
            pxp = ox + px * cs
            pyp = oy + py * cs
            if cs >= 5:
                # Clear the cell interior first so the arrow is unambiguous.
                draw.rectangle((pxp + 1, pyp + 1, pxp + cs - 1, pyp + cs - 1),
                               fill=1)
                a, b = pxp, pyp
                e = cs
                if facing == 0:    # N
                    pts = [(a + e // 2, b + 1),
                           (a + e - 1, b + e - 1),
                           (a + 1, b + e - 1)]
                elif facing == 1:  # E
                    pts = [(a + e - 1, b + e // 2),
                           (a + 1, b + 1),
                           (a + 1, b + e - 1)]
                elif facing == 2:  # S
                    pts = [(a + e // 2, b + e - 1),
                           (a + e - 1, b + 1),
                           (a + 1, b + 1)]
                else:              # W
                    pts = [(a + 1, b + e // 2),
                           (a + e - 1, b + 1),
                           (a + e - 1, b + e - 1)]
                draw.polygon(pts, fill=0)
            else:
                draw.point((pxp + cs // 2, pyp + cs // 2), fill=0)

        # Stats — two short lines under the minimap.
        if mode == "daily":
            best = best_daily.get(date.today().isoformat())
        else:
            best = best_random
        best_str = str(best) if best is not None else "—"
        line1 = f"opt {optimal}  best {best_str}"
        draw.text((x0, text_top), line1, font=small_f, fill=0)
        line2 = f"@({px},{py}) {DIR_NAMES[facing]}"
        draw.text((x0, text_top + 9), line2, font=small_f, fill=0)
