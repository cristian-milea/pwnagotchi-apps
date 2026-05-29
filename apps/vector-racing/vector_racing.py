# Vector Racing — turn-based "Racetrack" on the e-ink.
#
# The phone is a 9-way acceleration pad; the e-ink draws a top-down closed
# circuit, the car, its trail, and the projected next move. One tap = one push
# = one redraw. No FPS, no animation.
#
# Mirrors maze's proven patterns: on-device procedural generation, daily seed
# via date.toordinal(), BFS-computed optimum, persisted best, published_state()
# for the phone, render() wrapped in try/except with a text fallback.
#
# Core model (integer lattice). Car position p=(px,py), velocity v=(vx,vy).
# Each turn the player picks a ∈ {-1,0,1}^2:
#   v ← clamp(v + a, ±VMAX);  target ← p + v
#   the straight segment p→target must lie entirely on-track (supercover line,
#   so corner-cutting is caught). Clean → p ← target; else → crash(mode).
#
# Track = a CENTRELINE (closed polyline, possibly self-crossing for a
# figure-8) thickened into a tube of width TRACK_W via distance-to-path. Shapes
# are drawn from a seeded library (oval / triangle / square / pentagon /
# hexagon / zigzag / scalloped / figure-8 / kidney) so daily players share one.
#
# Laps are detected by ORDERED CHECKPOINT GATES along the centreline, not by
# winding angle: each gate is a full-width slice; a forward crossing of the
# next expected gate advances progress; completing a full ring of gates = a
# lap. Carrying the gate index in BFS/game state is what makes a self-crossing
# figure-8 work (the two passes hit different gates, in order) and kills the
# oscillate-at-the-line cheese that a literal line-cross rule allows.
#
# Push schema (phone → device):
#   {"action": "start",   "mode": "daily"|"random",
#                         "crash": "safe"|"hardcore", "laps": 1|2|3}
#   {"action": "accel",   "dx": -1|0|1, "dy": -1|0|1}
#   {"action": "abandon"}

import json
import logging
import math
import os
import random
import threading
import time
from collections import deque
from datetime import date

from PIL import ImageFont


STATE_PATH = "/etc/pwnagotchi/vector_racing.state.json"

# ---- tunable constants ----
VMAX = 3              # per-component velocity clamp
LW, LH = 36, 20       # logical lattice size
TRACK_W = 4           # nominal band width (cells); HALF_W is the tube radius
HALF_W = TRACK_W / 2.0
RESEED_CAP = 64       # generation retries before falling back to a plain oval
DEFAULT_LAPS = 2
MARGIN = 3            # lattice cells kept clear around the track
TRAIL_CAP = 256

CX, CY = LW / 2.0, LH / 2.0

SHAPES = ["ellipse", "triangle", "square", "pentagon", "hexagon",
          "zigzag", "wavy", "figure8", "kidney"]


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


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


def _daily_seed():
    return date.today().toordinal()


# ---- geometry helpers ----

def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _pt_seg_dist2(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    cx, cy = ax + t * dx, ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


def _ccw(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _segments_cross(ax, ay, bx, by, cx, cy, dx, dy):
    """Proper crossing of segment AB and segment CD (collinear/touch → False)."""
    d1 = _ccw(cx, cy, dx, dy, ax, ay)
    d2 = _ccw(cx, cy, dx, dy, bx, by)
    d3 = _ccw(ax, ay, bx, by, cx, cy)
    d4 = _ccw(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _supercover(x0, y0, x1, y1):
    """Every lattice cell the segment passes through, endpoints included.
    On an exact-diagonal corner both orthogonal neighbours are added, so a
    move that skims across a wall corner is caught (not plain Bresenham)."""
    pts = [(x0, y0)]
    dx, dy = x1 - x0, y1 - y0
    nx, ny = abs(dx), abs(dy)
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    px, py = x0, y0
    ix = iy = 0
    while ix < nx or iy < ny:
        t = (1 + 2 * ix) * ny - (1 + 2 * iy) * nx
        if t == 0:
            pts.append((px + sx, py))
            pts.append((px, py + sy))
            px += sx
            py += sy
            ix += 1
            iy += 1
        elif t < 0:
            px += sx
            ix += 1
        else:
            py += sy
            iy += 1
        pts.append((px, py))
    return pts


def _seg_on_track(grid, x0, y0, x1, y1):
    for (x, y) in _supercover(x0, y0, x1, y1):
        if not (0 <= x < LW and 0 <= y < LH) or not grid[y][x]:
            return False
    return True


def _last_on_track(grid, x0, y0, x1, y1):
    last = (x0, y0)
    for (x, y) in _supercover(x0, y0, x1, y1)[1:]:
        if 0 <= x < LW and 0 <= y < LH and grid[y][x]:
            last = (x, y)
        else:
            break
    return last


# ---- centreline generation ----

def _chaikin(pts, iters):
    for _ in range(iters):
        out = []
        m = len(pts)
        for i in range(m):
            p, q = pts[i], pts[(i + 1) % m]
            out.append((0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]))
            out.append((0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]))
        pts = out
    return pts


def _shape_points(rng, shape):
    T = 180
    if shape == "ellipse":
        base = [(math.cos(2 * math.pi * i / T), math.sin(2 * math.pi * i / T))
                for i in range(T)]
    elif shape in ("triangle", "square", "pentagon", "hexagon"):
        k = {"triangle": 3, "square": 4, "pentagon": 5, "hexagon": 6}[shape]
        rot = rng.uniform(0, 2 * math.pi)
        verts = [(math.cos(2 * math.pi * i / k + rot),
                  math.sin(2 * math.pi * i / k + rot)) for i in range(k)]
        base = _chaikin(verts, 2)
    elif shape == "zigzag":
        k = rng.choice([4, 5, 6])
        verts = []
        for i in range(2 * k):
            r = 1.0 if i % 2 == 0 else 0.58
            a = 2 * math.pi * i / (2 * k)
            verts.append((r * math.cos(a), r * math.sin(a)))
        base = _chaikin(verts, 1)
    elif shape == "wavy":
        k = rng.choice([6, 7, 8])
        base = []
        for i in range(T):
            a = 2 * math.pi * i / T
            r = 1.0 + 0.14 * math.sin(k * a)
            base.append((r * math.cos(a), r * math.sin(a)))
    elif shape == "figure8":
        # Gerono lemniscate: crosses itself at the origin.
        base = [(math.cos(2 * math.pi * i / T),
                 math.sin(2 * math.pi * i / T) * math.cos(2 * math.pi * i / T))
                for i in range(T)]
    elif shape == "kidney":
        base = []
        for i in range(T):
            a = 2 * math.pi * i / T
            r = 0.68 + 0.5 * math.cos(a)
            base.append((r * math.cos(a), r * math.sin(a)))
    else:
        base = [(math.cos(2 * math.pi * i / T), math.sin(2 * math.pi * i / T))
                for i in range(T)]

    rot = rng.uniform(0, 2 * math.pi)
    ca, sa = math.cos(rot), math.sin(rot)
    return [(x * ca - y * sa, x * sa + y * ca) for (x, y) in base]


def _fit_to_lattice(pts):
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    spanx = (maxx - minx) or 1.0
    spany = (maxy - miny) or 1.0
    sx = (LW - 2 * MARGIN) / spanx
    sy = (LH - 2 * MARGIN) / spany
    # Cap anisotropy so a square doesn't squash into the same wide rectangle as
    # an oval; naturally-wide shapes (oval, figure-8) still fill the panel.
    MAXR = 1.5
    if sx > sy * MAXR:
        sx = sy * MAXR
    if sy > sx * MAXR:
        sy = sx * MAXR
    offx = (LW - spanx * sx) / 2.0
    offy = (LH - spany * sy) / 2.0
    return [(offx + (x - minx) * sx, offy + (y - miny) * sy) for (x, y) in pts]


def _resample_closed(pts, n):
    """Resample a closed polyline to n points spaced equally by arc length."""
    m = len(pts)
    seglen = [_dist(pts[i], pts[(i + 1) % m]) for i in range(m)]
    total = sum(seglen)
    if total <= 0:
        return list(pts[:n])
    step = total / n
    out = []
    i = 0
    acc = 0.0
    for k in range(n):
        target = k * step
        while acc + seglen[i] < target and i < m - 1:
            acc += seglen[i]
            i += 1
        a, b = pts[i], pts[(i + 1) % m]
        rem = target - acc
        f = rem / seglen[i] if seglen[i] > 0 else 0.0
        out.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f))
    return out


def _centreline(seed, shape):
    rng = random.Random(seed)
    pts = _fit_to_lattice(_shape_points(rng, shape))
    return _resample_closed(pts, 240)


def _build_grid(center):
    """Tube of radius HALF_W around the centreline polyline. The centreline is
    dense (~0.3 cells/step), so every-other point is sub-pixel identical and
    halves the per-cell distance scan."""
    pts = center[::2] or center
    m = len(pts)
    segs = [(pts[i][0], pts[i][1], pts[(i + 1) % m][0], pts[(i + 1) % m][1])
            for i in range(m)]
    r2 = HALF_W * HALF_W
    grid = [[False] * LW for _ in range(LH)]
    for y in range(LH):
        for x in range(LW):
            best = 1e18
            for (ax, ay, bx, by) in segs:
                d2 = _pt_seg_dist2(x, y, ax, ay, bx, by)
                if d2 < best:
                    best = d2
                    if best <= r2:
                        break
            grid[y][x] = best <= r2
    return grid


def _curvatures(center):
    """Per-point turning magnitude; low = a straight/gentle stretch."""
    n = len(center)
    out = []
    for i in range(n):
        a = center[(i - 1) % n]
        b = center[i]
        c = center[(i + 1) % n]
        a1 = math.atan2(b[1] - a[1], b[0] - a[0])
        a2 = math.atan2(c[1] - b[1], c[0] - b[0])
        out.append(abs(math.atan2(math.sin(a2 - a1), math.cos(a2 - a1))))
    return out


def _checkpoints(center, k):
    """k gates equally spaced by arc length, ordered along the centreline.
    Gate 0 is the start, chosen at the gentlest (lowest-curvature) stretch so
    the car has room to accelerate off the line. Each gate is a full-width
    slice perpendicular to the path; its forward normal is the path tangent."""
    curv = _curvatures(center)
    # Smooth curvature over a small window, then pick the calmest index — but
    # exclude points near a crossing/pinch (a non-adjacent bit of centreline
    # within ~TRACK_W), so a figure-8 doesn't start the car inside its X.
    n = len(center)
    win = max(1, n // 24)
    far = max(2, n // 6)          # "non-adjacent" in arc-index terms
    near2 = (TRACK_W * 1.6) ** 2
    smooth = []
    for i in range(n):
        s = sum(curv[(i + j) % n] for j in range(-win, win + 1))
        crossing = False
        for j in range(far, n - far):
            o = center[(i + j) % n]
            if (center[i][0] - o[0]) ** 2 + (center[i][1] - o[1]) ** 2 < near2:
                crossing = True
                break
        smooth.append(s + (100.0 if crossing else 0.0))
    start_i = min(range(n), key=lambda i: smooth[i])

    gates = []
    centres = []
    for j in range(k):
        idx = (start_i + round(j * n / k)) % n
        cx, cy = center[idx]
        a = center[(idx - 1) % n]
        b = center[(idx + 1) % n]
        tx, ty = b[0] - a[0], b[1] - a[1]
        tl = math.hypot(tx, ty) or 1.0
        tx, ty = tx / tl, ty / tl
        px, py = -ty, tx          # perpendicular across the band
        hl = HALF_W * 1.4         # extend a touch past the tube edges
        gates.append((cx - px * hl, cy - py * hl,
                      cx + px * hl, cy + py * hl, tx, ty))
        centres.append((cx, cy, tx, ty))
    return gates, centres


def _cross_gate(p, target, gate):
    """Forward crossing of a gate slice by the move segment."""
    ax, ay, bx, by, nx, ny = gate
    if not _segments_cross(p[0], p[1], target[0], target[1], ax, ay, bx, by):
        return False
    return (target[0] - p[0]) * nx + (target[1] - p[1]) * ny > 0


def _advance_progress(p, target, cp_count, gates):
    """Number of forward gate crossings on this move, advancing the ordered
    counter. Loops so a fast move that clears more than one gate is credited
    fully (and never gets stuck behind a skipped gate)."""
    k = len(gates)
    for _ in range(k):
        ng = (cp_count + 1) % k
        if _cross_gate(p, target, gates[ng]):
            cp_count += 1
        else:
            break
    return cp_count


def _snap_on_track(grid, fx, fy):
    """Nearest on-track lattice point to a float location."""
    best = None
    best_d = 1e18
    for ddx in range(-3, 4):
        for ddy in range(-3, 4):
            x = int(round(fx)) + ddx
            y = int(round(fy)) + ddy
            if 0 <= x < LW and 0 <= y < LH and grid[y][x]:
                d = (x - fx) ** 2 + (y - fy) ** 2
                if d < best_d:
                    best_d = d
                    best = (x, y)
    return best


def _has_enclosed_hole(grid):
    """True if some off-track cell is unreachable from the border (i.e. the
    band encloses interior space → it's a real loop, not a blob)."""
    reached = [[False] * LW for _ in range(LH)]
    q = deque()
    for x in range(LW):
        for y in (0, LH - 1):
            if not grid[y][x] and not reached[y][x]:
                reached[y][x] = True
                q.append((x, y))
    for y in range(LH):
        for x in (0, LW - 1):
            if not grid[y][x] and not reached[y][x]:
                reached[y][x] = True
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + ddx, y + ddy
            if (0 <= nx < LW and 0 <= ny < LH and not grid[ny][nx]
                    and not reached[ny][nx]):
                reached[ny][nx] = True
                q.append((nx, ny))
    for y in range(LH):
        for x in range(LW):
            if not grid[y][x] and not reached[y][x]:
                return True
    return False


def _single_component(grid):
    start = None
    total = 0
    for y in range(LH):
        for x in range(LW):
            if grid[y][x]:
                total += 1
                if start is None:
                    start = (x, y)
    if start is None:
        return False, 0
    seen = {start}
    q = deque([start])
    while q:
        x, y = q.popleft()
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                if ddx == 0 and ddy == 0:
                    continue
                nx, ny = x + ddx, y + ddy
                if (0 <= nx < LW and 0 <= ny < LH and grid[ny][nx]
                        and (nx, ny) not in seen):
                    seen.add((nx, ny))
                    q.append((nx, ny))
    return len(seen) == total, total


def _generate_track(seed, shape):
    """Returns (center, grid, start_xy, start_tan, gates, centres, k) or None."""
    center = _centreline(seed, shape)
    grid = _build_grid(center)

    ok, total = _single_component(grid)
    if not ok or total < 2 * (LW + LH):
        return None
    if not _has_enclosed_hole(grid):
        return None

    length = sum(_dist(center[i], center[(i + 1) % len(center)])
                 for i in range(len(center)))
    k = int(_clamp(round(length / 7.0), 6, 12))
    gates, centres = _checkpoints(center, k)

    start_xy = _snap_on_track(grid, centres[0][0], centres[0][1])
    if start_xy is None:
        return None
    # Every gate centre must sit on the band.
    for (cx, cy, _tx, _ty) in centres:
        if _snap_on_track(grid, cx, cy) is None:
            return None
    # Not boxed in: at least one accel from rest stays on-track.
    sx, sy = start_xy
    drivable = any(_seg_on_track(grid, sx, sy, sx + ax, sy + ay)
                   for ax in (-1, 0, 1) for ay in (-1, 0, 1)
                   if (ax, ay) != (0, 0))
    if not drivable:
        return None

    start_tan = (centres[0][2], centres[0][3])
    return center, grid, start_xy, start_tan, gates, centres, k


def _generate_with_retry(base_seed):
    """Advance the seed deterministically until a valid track is found, so
    daily players on the same day converge on the same track. The shape is
    derived from the seed too. Falls back to a plain oval, always valid."""
    seed = base_seed
    for _ in range(RESEED_CAP):
        shape = SHAPES[seed % len(SHAPES)]
        result = _generate_track(seed, shape)
        if result is not None:
            return seed, shape, result
        seed = (seed + 1) & 0x7fffffff
    result = _generate_track(base_seed, "ellipse")
    if result is None:  # pragma: no cover — ellipse always validates
        result = _generate_track(base_seed + 1, "ellipse")
    return base_seed, "ellipse", result


def _compute_optimum(grid, start_xy, start_tan, gates, k, target_laps,
                     should_stop):
    """BFS over (px, py, vx, vy, cp_count) for the fewest clean turns to
    complete target_laps. cp_count = ordered forward gate crossings, capped at
    the goal. Uniform turn cost → first time the goal is hit is optimal.
    Returns the optimum, or None if unreachable / aborted / state cap hit."""
    sx, sy = start_xy
    goal = k * target_laps
    start = (sx, sy, 0, 0, 0)
    dist = {start: 0}
    q = deque([start])
    cap = 2_000_000
    while q:
        if should_stop():
            return None
        px, py, vx, vy, prog = q.popleft()
        d = dist[(px, py, vx, vy, prog)]
        for ax in (-1, 0, 1):
            for ay in (-1, 0, 1):
                nvx = _clamp(vx + ax, -VMAX, VMAX)
                nvy = _clamp(vy + ay, -VMAX, VMAX)
                tx, ty = px + nvx, py + nvy
                if not _seg_on_track(grid, px, py, tx, ty):
                    continue
                nprog = _advance_progress((px, py), (tx, ty), prog, gates)
                if nprog > goal:
                    nprog = goal
                if nprog >= goal:
                    return d + 1
                ns = (tx, ty, nvx, nvy, nprog)
                if ns not in dist:
                    dist[ns] = d + 1
                    q.append(ns)
        if len(dist) > cap:
            return None
    return None


class VectorRacing:
    name = "vector-racing"
    icon = "VR"
    version = "1.1.0"

    interval_seconds = None

    def __init__(self):
        self._lock = threading.RLock()
        self._fonts = {}

        s = _load_state(STATE_PATH)
        self._bests = dict(s.get("bests") or {})
        self._mode = s.get("mode") if s.get("mode") in ("daily", "random") else "daily"
        self._crash_rule = (s.get("crash_rule")
                            if s.get("crash_rule") in ("safe", "hardcore")
                            else "safe")
        try:
            self._laps = int(s.get("laps", DEFAULT_LAPS))
        except (TypeError, ValueError):
            self._laps = DEFAULT_LAPS
        self._laps = _clamp(self._laps, 1, 3)

        self._status = "setup"
        self._reset_race_vars()

        self._opt_token = 0
        self._opt_thread = None

    def _reset_race_vars(self):
        self._center = None
        self._grid = None
        self._gates = None
        self._centres = None
        self._k = 0
        self._seed = None
        self._shape = None
        self._start = (0, 0)
        self._start_tan = (1.0, 0.0)
        self._px = self._py = 0
        self._vx = self._vy = 0
        self._attempt_v = (0, 0)
        self._cp_count = 0
        self._laps_done = 0
        self._moves = 0
        self._crashes = 0
        self._trail = []
        self._optimum = None
        self._best_key = None
        self._last_result = ""
        self._crashed_flag = False

    # ---- persistence (settings + bests only) ----
    def _persist(self):
        _save_state(STATE_PATH, {
            "bests": self._bests,
            "mode": self._mode,
            "crash_rule": self._crash_rule,
            "laps": self._laps,
        })

    def _best_key_for(self, mode, laps):
        if mode == "daily":
            return "daily:%d:%d" % (_daily_seed(), laps)
        return "random:%d" % laps

    # ---- game flow ----
    def _start_race(self, mode, crash_rule, laps):
        self._mode = mode
        self._crash_rule = crash_rule
        self._laps = laps

        if mode == "daily":
            base_seed = _daily_seed()
        else:
            base_seed = int(time.time() * 1000) & 0x7fffffff

        seed, shape, track = _generate_with_retry(base_seed)
        center, grid, start_xy, start_tan, gates, centres, k = track

        self._reset_race_vars()
        self._center = center
        self._grid = grid
        self._gates = gates
        self._centres = centres
        self._k = k
        self._seed = seed
        self._shape = shape
        self._start = start_xy
        self._start_tan = start_tan
        self._px, self._py = start_xy
        self._trail = [start_xy]
        self._best_key = self._best_key_for(mode, laps)
        self._status = "racing"
        self._persist()

        # Optimum is expensive on Pi-class hardware — compute off the request
        # thread so the race starts instantly. A token guards a stale thread.
        self._opt_token += 1
        token = self._opt_token

        def worker():
            opt = _compute_optimum(grid, start_xy, start_tan, gates, k, laps,
                                   lambda: self._opt_token != token)
            with self._lock:
                if self._opt_token == token:
                    self._optimum = opt
        t = threading.Thread(target=worker, daemon=True)
        self._opt_thread = t
        t.start()

    def _abandon(self):
        self._opt_token += 1
        self._reset_race_vars()
        self._status = "setup"

    def _finish(self):
        self._status = "finished"
        best = self._bests.get(self._best_key)
        if best is None or self._moves < best:
            self._bests[self._best_key] = self._moves
        if self._optimum is not None:
            self._last_result = "Finished in %d (optimum %d)" % (
                self._moves, self._optimum)
        else:
            self._last_result = "Finished in %d" % self._moves
        self._persist()

    def _apply_progress(self, frm, to):
        self._cp_count = _advance_progress(frm, to, self._cp_count, self._gates)
        ld = self._cp_count // self._k
        if ld > self._laps_done:
            self._laps_done = ld

    def _accel(self, dx, dy):
        if self._status != "racing" or self._grid is None:
            return
        self._crashed_flag = False
        nvx = _clamp(self._vx + dx, -VMAX, VMAX)
        nvy = _clamp(self._vy + dy, -VMAX, VMAX)
        tx, ty = self._px + nvx, self._py + nvy
        self._moves += 1

        if _seg_on_track(self._grid, self._px, self._py, tx, ty):
            self._apply_progress((self._px, self._py), (tx, ty))
            self._px, self._py = tx, ty
            self._vx, self._vy = nvx, nvy
            self._trail.append((tx, ty))
            if len(self._trail) > TRAIL_CAP:
                self._trail = self._trail[-TRAIL_CAP:]
            self._last_result = ""
            if self._laps_done >= self._laps:
                self._finish()
        else:
            self._crash()

    def _crash(self):
        self._crashes += 1
        self._crashed_flag = True
        self._last_result = "CRASH"
        if self._crash_rule == "hardcore":
            # Back to the line; completed laps kept, in-progress lap redone.
            self._px, self._py = self._start
            self._vx = self._vy = 0
            self._cp_count = self._laps_done * self._k
            self._trail.append(self._start)
        else:
            # Safe: stop at the last on-track point along the attempted move.
            avx, avy = self._attempt_v
            lx, ly = _last_on_track(self._grid, self._px, self._py,
                                    self._px + avx, self._py + avy)
            self._apply_progress((self._px, self._py), (lx, ly))
            self._px, self._py = lx, ly
            self._vx = self._vy = 0
            self._trail.append((lx, ly))
        if len(self._trail) > TRAIL_CAP:
            self._trail = self._trail[-TRAIL_CAP:]

    # ---- host hooks ----
    def on_data(self, payload):
        payload = payload or {}
        action = payload.get("action")
        with self._lock:
            if action == "start":
                mode = payload.get("mode")
                if mode not in ("daily", "random"):
                    return False
                crash_rule = payload.get("crash")
                if crash_rule not in ("safe", "hardcore"):
                    crash_rule = "safe"
                try:
                    laps = int(payload.get("laps", DEFAULT_LAPS))
                except (TypeError, ValueError):
                    laps = DEFAULT_LAPS
                laps = _clamp(laps, 1, 3)
                self._start_race(mode, crash_rule, laps)
                return True
            if action == "accel":
                if self._status != "racing":
                    return False
                try:
                    dx = int(payload.get("dx", 0))
                    dy = int(payload.get("dy", 0))
                except (TypeError, ValueError):
                    return False
                if dx not in (-1, 0, 1) or dy not in (-1, 0, 1):
                    return False
                self._attempt_v = (_clamp(self._vx + dx, -VMAX, VMAX),
                                   _clamp(self._vy + dy, -VMAX, VMAX))
                self._accel(dx, dy)
                return True
            if action == "abandon":
                self._abandon()
                return True
        return False

    def published_state(self):
        with self._lock:
            laps = self._laps
            if self._status == "finished":
                lap = "%d/%d" % (laps, laps)
            elif self._status == "racing":
                lap = "%d/%d" % (min(self._laps_done + 1, laps), laps)
            else:
                lap = "0/%d" % laps
            optimum = self._optimum if self._optimum is not None else "—"
            key = (self._best_key if self._best_key
                   else self._best_key_for(self._mode, laps))
            best = self._bests.get(key)
            return {
                "status": self._status,
                "mode": self._mode,
                "crash_rule": self._crash_rule,
                "laps": laps,
                "lap": lap,
                "moves": self._moves,
                "optimum": optimum,
                "best": best if best is not None else "—",
                "speed": "%d,%d" % (self._vx, self._vy),
                "crashes": self._crashes,
                "shape": self._shape or "—",
                "last_result": self._last_result,
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
            snap = {
                "status": self._status,
                "mode": self._mode,
                "shape": self._shape,
                "laps": self._laps,
                "lap_done": self._laps_done,
                "moves": self._moves,
                "optimum": self._optimum,
                "crashes": self._crashes,
                "last_result": self._last_result,
                "crashed": self._crashed_flag,
                "grid": self._grid,
                "gates": self._gates,
                "px": self._px, "py": self._py,
                "vx": self._vx, "vy": self._vy,
                "start_tan": self._start_tan,
                "next_gate": ((self._cp_count + 1) % self._k) if self._k else 0,
                "trail": list(self._trail),
                "best_key": self._best_key,
            }
            best = self._bests.get(snap["best_key"]) if snap["best_key"] else None
            snap["best"] = best
        try:
            self._paint(draw, w, h, snap)
        except Exception:
            logging.exception("vector-racing: render failed")
            try:
                f = self._font(11)
            except Exception:
                f = ImageFont.load_default()
            draw.text((4, 4), "VECTOR RACING — render error", font=f, fill=0)

    def _paint(self, draw, w, h, s):
        title_f = self._font(11)
        small_f = self._font(9)
        big_f = self._font(15)

        mode = s["mode"]
        lap = "%d/%d" % (min(s["lap_done"] + 1, s["laps"]), s["laps"])
        header = "VEC · %s · lap %s · moves %d" % (mode, lap, s["moves"])
        draw.text((2, 1), header, font=small_f, fill=0)
        draw.line((2, 13, w - 2, 13), fill=0)

        body_top = 15
        body_bot = h - 2

        if s["status"] == "setup" or s["grid"] is None:
            self._paint_setup(draw, w, body_top, body_bot, big_f, small_f)
            return
        if s["status"] == "finished":
            self._paint_victory(draw, w, body_top, body_bot, s, big_f, small_f)
            return
        self._paint_track(draw, w, body_top, body_bot, s, small_f)

    def _paint_setup(self, draw, w, top, bot, big_f, small_f):
        msg = "VECTOR RACING"
        mw = int(draw.textlength(msg, font=big_f))
        cy = (top + bot) // 2
        draw.text(((w - mw) // 2, cy - 14), msg, font=big_f, fill=0)
        hint = "set up a race on the phone"
        hw = int(draw.textlength(hint, font=small_f))
        draw.text(((w - hw) // 2, cy + 8), hint, font=small_f, fill=0)

    def _paint_victory(self, draw, w, top, bot, s, big_f, small_f):
        cy = (top + bot) // 2
        msg = "FINISHED"
        mw = int(draw.textlength(msg, font=big_f))
        draw.text(((w - mw) // 2, cy - 24), msg, font=big_f, fill=0)

        opt = s["optimum"]
        opt_str = str(opt) if opt is not None else "—"
        line = "%d moves · optimum %s" % (s["moves"], opt_str)
        lw = int(draw.textlength(line, font=small_f))
        draw.text(((w - lw) // 2, cy - 2), line, font=small_f, fill=0)

        best = s["best"]
        bline = "best %s" % (str(best) if best is not None else "—")
        bw = int(draw.textlength(bline, font=small_f))
        draw.text(((w - bw) // 2, cy + 11), bline, font=small_f, fill=0)

        cline = "crashes %d" % s["crashes"]
        cw = int(draw.textlength(cline, font=small_f))
        draw.text(((w - cw) // 2, cy + 23), cline, font=small_f, fill=0)

    def _paint_track(self, draw, w, top, bot, s, small_f):
        grid = s["grid"]
        pad = 3
        avail_w = w - 2 * pad
        avail_h = (bot - top) - 2 * pad
        cell = min(avail_w / float(LW), avail_h / float(LH))
        if cell < 1.0:
            cell = 1.0
        ox = pad + (avail_w - cell * LW) / 2.0
        oy = top + pad + (avail_h - cell * LH) / 2.0

        def sx(x):
            return ox + x * cell
        def sy(y):
            return oy + y * cell

        # ---- band outline: edges between on-track and off-track cells ----
        for y in range(LH):
            for x in range(LW):
                on = grid[y][x]
                on_r = grid[y][x + 1] if x + 1 < LW else False
                if on != on_r:
                    X = sx(x + 0.5)
                    draw.line((X, sy(y - 0.5), X, sy(y + 0.5)), fill=0)
                on_d = grid[y + 1][x] if y + 1 < LH else False
                if on != on_d:
                    Y = sy(y + 0.5)
                    draw.line((sx(x - 0.5), Y, sx(x + 0.5), Y), fill=0)

        # ---- start/finish gate: dashed slice across the band ----
        g0 = s["gates"][0]
        self._dash_line(draw, sx(g0[0]), sy(g0[1]), sx(g0[2]), sy(g0[3]),
                        dash=2, gap=2)

        # ---- next checkpoint: hollow guide marker (helps on figure-8) ----
        gn = s["gates"][s["next_gate"]]
        gcx = (gn[0] + gn[2]) / 2.0
        gcy = (gn[1] + gn[3]) / 2.0
        gr = max(1.5, cell * 0.35)
        draw.ellipse((sx(gcx) - gr, sy(gcy) - gr, sx(gcx) + gr, sy(gcy) + gr),
                     outline=0)

        # ---- trail: dotted line through past positions ----
        trail = s["trail"]
        for i in range(1, len(trail)):
            x0, y0 = trail[i - 1]
            x1, y1 = trail[i]
            self._dot_line(draw, sx(x0), sy(y0), sx(x1), sy(y1), step=3)

        # ---- projected move: line p -> p+v, hollow marker at the target ----
        px, py, vx, vy = s["px"], s["py"], s["vx"], s["vy"]
        tx, ty = px + vx, py + vy
        if vx != 0 or vy != 0:
            self._dot_line(draw, sx(px), sy(py), sx(tx), sy(ty), step=2)
        mr = max(2.0, cell * 0.45)
        draw.ellipse((sx(tx) - mr, sy(ty) - mr, sx(tx) + mr, sy(ty) + mr),
                     outline=0)

        # ---- car: little rectangle with wheels, pointing along velocity ----
        if vx != 0 or vy != 0:
            ang = math.atan2(vy, vx)
        else:
            ang = math.atan2(s["start_tan"][1], s["start_tan"][0])
        self._draw_car(draw, sx(px), sy(py), ang, cell)

        # ---- status footer ----
        opt = s["optimum"]
        opt_str = str(opt) if opt is not None else "—"
        best = s["best"]
        best_str = str(best) if best is not None else "—"
        foot = "%s  spd %d,%d  opt %s  best %s  cr %d" % (
            s["shape"], vx, vy, opt_str, best_str, s["crashes"])
        if s["crashed"]:
            foot = "CRASH  " + foot
        draw.text((2, bot - 9), foot, font=small_f, fill=0)

    def _draw_car(self, draw, cx, cy, ang, cell):
        ca, sa = math.cos(ang), math.sin(ang)

        def tf(lx, ly):
            return (cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

        if cell < 4:
            r = max(1.0, cell * 0.4)
            draw.rectangle((cx - r, cy - r, cx + r, cy + r), fill=0)
            return

        bl = cell * 0.7    # half body length (along travel)
        bw = cell * 0.42   # half body width
        # Wheels first (black), so the white body sits over their inner edge.
        wl = cell * 0.28
        ww = cell * 0.16
        for sxl, syl in ((bl * 0.6, bw), (bl * 0.6, -bw),
                         (-bl * 0.6, bw), (-bl * 0.6, -bw)):
            wpts = [tf(sxl - wl, syl - ww), tf(sxl + wl, syl - ww),
                    tf(sxl + wl, syl + ww), tf(sxl - wl, syl + ww)]
            draw.polygon(wpts, fill=0)
        body = [tf(bl, 0), tf(bl * 0.5, -bw), tf(-bl, -bw),
                tf(-bl, bw), tf(bl * 0.5, bw)]
        draw.polygon(body, outline=0, fill=1)

    def _dash_line(self, draw, x0, y0, x1, y1, dash=2, gap=2):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            return
        ux, uy = (x1 - x0) / length, (y1 - y0) / length
        d = 0.0
        on = True
        while d < length:
            seg = dash if on else gap
            e = min(d + seg, length)
            if on:
                draw.line((x0 + ux * d, y0 + uy * d,
                           x0 + ux * e, y0 + uy * e), fill=0)
            d = e
            on = not on

    def _dot_line(self, draw, x0, y0, x1, y1, step=2):
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            draw.point((x0, y0), fill=0)
            return
        n = int(length / step) + 1
        for i in range(n + 1):
            t = i / float(n)
            draw.point((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t), fill=0)
