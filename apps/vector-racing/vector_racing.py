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
# Lap detection: cumulative *winding angle* around the track centre. A full
# forward revolution = one lap; backward winding never decrements completed
# laps; oscillating at the line nets ~0. This is the robust realisation of the
# spec's "cross the start/finish line forward" rule — the literal line-cross
# rule is cheesable (step back over the line, then forward = a fake lap) and
# would poison the BFS optimum. Live game and BFS share this logic.
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

# ---- tunable constants (spec §12) ----
VMAX = 3            # per-component velocity clamp
LW, LH = 36, 20     # logical lattice size
TRACK_W = 3         # nominal band width (cells)
TRACK_W_MIN = 2     # minimum allowed band width (validation)
MIN_STRAIGHT = 6    # min length of a "straight" run; need >= 2 of them
RESEED_CAP = 64     # generation retries before falling back to a plain oval
DEFAULT_LAPS = 2    # starting lap selection

# Winding is discretised into S sectors around the centre. A move is short
# relative to the loop, so it crosses at most ~2 sector boundaries; the wrap
# below tolerates up to S/2. A full forward loop sums to exactly +S sectors.
S_SECTORS = 12

# Track-centre, ellipse base radii (derived from the lattice with a margin).
CX, CY = LW / 2.0, LH / 2.0
OUTER_A = LW / 2.0 - 3.0   # 15.0
OUTER_B = LH / 2.0 - 3.0   # 7.0

TRAIL_CAP = 256


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


# ---- ring geometry ----

def _ring_params(seed):
    """Perturbed-ring track. Outer boundary = ellipse radius times a small
    seeded low-frequency noise; inner boundary = outer shrunk by TRACK_W
    radially (guarantees a continuous band of controlled width)."""
    rng = random.Random(seed)
    harmonics = []
    # 2-3 low-frequency sinusoids; small amplitude so the band never pinches
    # off and the outer boundary stays inside the lattice.
    for _ in range(rng.randint(2, 3)):
        freq = rng.randint(2, 4)
        amp = rng.uniform(0.03, 0.06)
        phase = rng.uniform(0.0, 2.0 * math.pi)
        harmonics.append((amp, freq, phase))
    return {"a": OUTER_A, "b": OUTER_B, "harmonics": harmonics}


def _r_out(params, theta):
    a, b = params["a"], params["b"]
    ct, st = math.cos(theta), math.sin(theta)
    ell = (a * b) / math.sqrt((b * ct) ** 2 + (a * st) ** 2)
    noise = 0.0
    for amp, freq, phase in params["harmonics"]:
        noise += amp * math.sin(freq * theta + phase)
    return ell * (1.0 + noise)


def _r_in(params, theta):
    return _r_out(params, theta) - TRACK_W


def _on_track_pt(params, x, y):
    dx, dy = x - CX, y - CY
    rho = math.hypot(dx, dy)
    if rho < 1e-6:
        return False
    theta = math.atan2(dy, dx)
    return _r_in(params, theta) <= rho <= _r_out(params, theta)


def _build_grid(params):
    """Boolean on-track grid[y][x] for fast O(1) lookups in BFS / collision."""
    return [[_on_track_pt(params, x, y) for x in range(LW)] for y in range(LH)]


def _sector(x, y):
    theta = math.atan2(y - CY, x - CX)
    if theta < 0:
        theta += 2.0 * math.pi
    return int(theta / (2.0 * math.pi / S_SECTORS))


def _wrap_sector(d):
    """Fold a sector delta into (-S/2, S/2] so a short move reads as the small
    signed number of sectors it actually swept (CCW positive)."""
    half = S_SECTORS // 2
    while d > half:
        d -= S_SECTORS
    while d <= -half:
        d += S_SECTORS
    return d


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
    """Last on-track lattice point along the attempted segment (for Safe
    crashes). p is always on-track, so the result is at least p."""
    last = (x0, y0)
    for (x, y) in _supercover(x0, y0, x1, y1)[1:]:
        if 0 <= x < LW and 0 <= y < LH and grid[y][x]:
            last = (x, y)
        else:
            break
    return last


# ---- validation ----

def _validate(params, grid):
    on = [(x, y) for y in range(LH) for x in range(LW) if grid[y][x]]
    if len(on) < 2 * (LW + LH):  # sanity floor; a real ring is much larger
        return False

    # Single 8-connected component.
    seen = {on[0]}
    q = deque([on[0]])
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
    if len(seen) != len(on):
        return False

    # It's a loop: the centre is off-track and enclosed (off-track flood from
    # the border does not reach it).
    cxi, cyi = int(round(CX)), int(round(CY))
    if grid[cyi][cxi]:
        return False
    reached = set()
    q = deque()
    for x in range(LW):
        for y in (0, LH - 1):
            if not grid[y][x] and (x, y) not in reached:
                reached.add((x, y))
                q.append((x, y))
    for y in range(LH):
        for x in (0, LW - 1):
            if not grid[y][x] and (x, y) not in reached:
                reached.add((x, y))
                q.append((x, y))
    while q:
        x, y = q.popleft()
        for ddx, ddy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + ddx, y + ddy
            if (0 <= nx < LW and 0 <= ny < LH and not grid[ny][nx]
                    and (nx, ny) not in reached):
                reached.add((nx, ny))
                q.append((nx, ny))
    if (cxi, cyi) in reached:
        return False

    # Band width is TRACK_W by construction (inner = outer - TRACK_W radially);
    # the connectivity + enclosed-hole checks above already reject any noise
    # spike that would pinch the band below TRACK_W_MIN. Require >= 2 straights
    # so the speed dimension stays usable.
    runs = _straight_runs(params)
    return sum(1 for length, _theta in runs if length >= MIN_STRAIGHT) >= 2


_STRAIGHT_THRESH = math.radians(18.0)


def _straight_runs(params):
    """Segment the centreline into runs where the tangent direction stays
    within _STRAIGHT_THRESH of the run's *anchor* direction. A near-straight
    arc keeps one anchor and grows; a curve drifts and breaks into short runs.
    Returns [(arc_length, mid_theta), ...]. Scanning starts at the sharpest
    turn (deep in a curve) so a straight is never split across the seam."""
    N = 240
    pts = []
    for i in range(N):
        theta = 2.0 * math.pi * i / N
        rmid = (_r_out(params, theta) + _r_in(params, theta)) / 2.0
        pts.append((CX + rmid * math.cos(theta), CY + rmid * math.sin(theta)))

    dirs, lens = [], []
    for i in range(N):
        ax, ay = pts[i]
        bx, by = pts[(i + 1) % N]
        dirs.append(math.atan2(by - ay, bx - ax))
        lens.append(math.hypot(bx - ax, by - ay))

    def angdiff(a, b):
        return math.atan2(math.sin(a - b), math.cos(a - b))

    start = max(range(N), key=lambda i: abs(angdiff(dirs[i], dirs[i - 1])))
    order = [(start + k) % N for k in range(N)]

    runs = []
    anchor = None
    run_len = 0.0
    run_ps = 0
    for pos, i in enumerate(order):
        if anchor is None:
            anchor, run_len, run_ps = dirs[i], lens[i], pos
        elif abs(angdiff(dirs[i], anchor)) <= _STRAIGHT_THRESH:
            run_len += lens[i]
        else:
            runs.append((run_len, run_ps, pos - 1))
            anchor, run_len, run_ps = dirs[i], lens[i], pos
    runs.append((run_len, run_ps, len(order) - 1))

    result = []
    for length, ps, pe in runs:
        mid_i = order[(ps + pe) // 2]
        result.append((length, 2.0 * math.pi * mid_i / N))
    return result


def _place_start(params, grid):
    """Start sits on the longest straight, on-track. Forward is the
    increasing-theta (CCW) direction, so winding is +1 per forward sector.
    Returns (sx, sy, theta_s)."""
    runs = _straight_runs(params)
    _length, theta_s = max(runs, key=lambda r: r[0])
    rmid = (_r_out(params, theta_s) + _r_in(params, theta_s)) / 2.0
    mx = CX + rmid * math.cos(theta_s)
    my = CY + rmid * math.sin(theta_s)

    # Snap the centreline midpoint to the nearest on-track lattice point.
    best = None
    best_d = 1e9
    for ddx in range(-2, 3):
        for ddy in range(-2, 3):
            x = int(round(mx)) + ddx
            y = int(round(my)) + ddy
            if 0 <= x < LW and 0 <= y < LH and grid[y][x]:
                d = (x - mx) ** 2 + (y - my) ** 2
                if d < best_d:
                    best_d = d
                    best = (x, y)
    if best is None:
        best = (int(round(mx)), int(round(my)))
    return best[0], best[1], theta_s


def _generate_track(seed):
    """Returns (params, grid, start_xy, theta_s) or None if invalid."""
    params = _ring_params(seed)
    grid = _build_grid(params)
    if not _validate(params, grid):
        return None
    sx, sy, theta_s = _place_start(params, grid)
    if not grid[sy][sx]:
        return None
    return params, grid, (sx, sy), theta_s


def _generate_with_retry(base_seed):
    """Advance the seed deterministically until a valid track is found, so
    daily players on the same day converge on the same track. Falls back to a
    plain (un-perturbed) oval, which is always valid."""
    seed = base_seed
    for _ in range(RESEED_CAP):
        result = _generate_track(seed)
        if result is not None:
            return seed, result
        seed = (seed + 1) & 0x7fffffff
    params = {"a": OUTER_A, "b": OUTER_B, "harmonics": []}
    grid = _build_grid(params)
    sx, sy, theta_s = _place_start(params, grid)
    return base_seed, (params, grid, (sx, sy), theta_s)


def _compute_optimum(grid, start_xy, target_laps, should_stop):
    """BFS over (px, py, vx, vy, prog) for the fewest clean turns to complete
    target_laps. prog = forward sectors swept since the start, capped at the
    goal. Uniform turn cost, so the first time prog hits the goal is optimal.
    Returns the optimum, or None if unreachable / aborted / state cap hit."""
    sx, sy = start_xy
    goal = S_SECTORS * target_laps
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
                dprog = _wrap_sector(_sector(tx, ty) - _sector(px, py))
                nprog = _clamp(prog + dprog, 0, goal)
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
    version = "1.0.0"

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

        # Mid-race state is RAM-only — start every launch at the setup screen.
        self._status = "setup"
        self._reset_race_vars()

        # Background optimum computation.
        self._opt_token = 0
        self._opt_thread = None

    def _reset_race_vars(self):
        self._params = None
        self._grid = None
        self._seed = None
        self._start = (0, 0)
        self._theta_s = 0.0
        self._px = self._py = 0
        self._vx = self._vy = 0
        self._attempt_v = (0, 0)
        self._prog = 0
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

        seed, (params, grid, start_xy, theta_s) = _generate_with_retry(base_seed)

        self._reset_race_vars()
        self._params = params
        self._grid = grid
        self._seed = seed
        self._start = start_xy
        self._theta_s = theta_s
        self._px, self._py = start_xy
        self._trail = [start_xy]
        self._best_key = self._best_key_for(mode, laps)
        self._status = "racing"
        self._persist()

        # Optimum is expensive on Pi-class hardware — compute off the request
        # thread so the race starts instantly. A token guards against a stale
        # thread writing into a newer race.
        self._opt_token += 1
        token = self._opt_token
        grid_ref = grid
        start_ref = start_xy
        laps_ref = laps

        def worker():
            opt = _compute_optimum(grid_ref, start_ref, laps_ref,
                                   lambda: self._opt_token != token)
            with self._lock:
                if self._opt_token == token:
                    self._optimum = opt
        t = threading.Thread(target=worker, daemon=True)
        self._opt_thread = t
        t.start()

    def _abandon(self):
        self._opt_token += 1  # orphan any running optimum thread
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

    def _accel(self, dx, dy):
        if self._status != "racing" or self._grid is None:
            return
        self._crashed_flag = False
        nvx = _clamp(self._vx + dx, -VMAX, VMAX)
        nvy = _clamp(self._vy + dy, -VMAX, VMAX)
        tx, ty = self._px + nvx, self._py + nvy
        self._moves += 1

        if _seg_on_track(self._grid, self._px, self._py, tx, ty):
            dprog = _wrap_sector(_sector(tx, ty) - _sector(self._px, self._py))
            self._prog += dprog
            self._px, self._py = tx, ty
            self._vx, self._vy = nvx, nvy
            self._trail.append((tx, ty))
            if len(self._trail) > TRAIL_CAP:
                self._trail = self._trail[-TRAIL_CAP:]
            ld = int(math.floor(self._prog / float(S_SECTORS)))
            if ld > self._laps_done:
                self._laps_done = ld
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
            self._prog = self._laps_done * S_SECTORS
            self._trail.append(self._start)
        else:
            # Safe: stop at the last on-track point along the attempted move.
            avx, avy = self._attempt_v
            lx, ly = _last_on_track(self._grid, self._px, self._py,
                                    self._px + avx, self._py + avy)
            dprog = _wrap_sector(_sector(lx, ly) - _sector(self._px, self._py))
            self._prog += dprog
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
                # Stash the attempted clamped velocity for Safe-crash landing.
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
                "laps": self._laps,
                "lap_done": self._laps_done,
                "moves": self._moves,
                "optimum": self._optimum,
                "crashes": self._crashes,
                "last_result": self._last_result,
                "crashed": self._crashed_flag,
                "params": self._params,
                "grid": self._grid,
                "px": self._px, "py": self._py,
                "vx": self._vx, "vy": self._vy,
                "theta_s": self._theta_s,
                "trail": list(self._trail),
                "best_key": self._best_key,
            }
            best = self._bests.get(snap["best_key"]) if snap["best_key"] else None
            snap["best"] = best
        try:
            self._paint(draw, w, h, snap)
        except Exception:
            logging.exception("vector-racing: render failed")
            f = self._font(10)
            draw.text((4, 4), "VECTOR RACING — render error", font=f, fill=0)

    def _paint(self, draw, w, h, s):
        title_f = self._font(10)
        small_f = self._font(8)
        big_f = self._font(14)

        # ---- header strip ----
        mode = s["mode"]
        lap = "%d/%d" % (min(s["lap_done"] + 1, s["laps"]), s["laps"])
        header = "VEC · %s · lap %s · moves %d" % (mode, lap, s["moves"])
        draw.text((2, 1), header, font=small_f, fill=0)
        draw.line((2, 12, w - 2, 12), fill=0)

        body_top = 14
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
        draw.text(((w - hw) // 2, cy + 6), hint, font=small_f, fill=0)

    def _paint_victory(self, draw, w, top, bot, s, big_f, small_f):
        cy = (top + bot) // 2
        msg = "FINISHED"
        mw = int(draw.textlength(msg, font=big_f))
        draw.text(((w - mw) // 2, cy - 22), msg, font=big_f, fill=0)

        opt = s["optimum"]
        opt_str = str(opt) if opt is not None else "—"
        line = "%d moves · optimum %s" % (s["moves"], opt_str)
        lw = int(draw.textlength(line, font=small_f))
        draw.text(((w - lw) // 2, cy - 2), line, font=small_f, fill=0)

        best = s["best"]
        bline = "best %s" % (str(best) if best is not None else "—")
        bw = int(draw.textlength(bline, font=small_f))
        draw.text(((w - bw) // 2, cy + 10), bline, font=small_f, fill=0)

        cline = "crashes %d" % s["crashes"]
        cw = int(draw.textlength(cline, font=small_f))
        draw.text(((w - cw) // 2, cy + 22), cline, font=small_f, fill=0)

    def _paint_track(self, draw, w, top, bot, s, small_f):
        params = s["params"]
        pad = 2
        avail_w = w - 2 * pad
        avail_h = (bot - top) - 2 * pad
        cell = min(avail_w / float(LW), avail_h / float(LH))
        if cell < 1.0:
            cell = 1.0
        # Centre the lattice in the body.
        ox = pad + (avail_w - cell * LW) / 2.0
        oy = top + pad + (avail_h - cell * LH) / 2.0

        def sx(x):
            return ox + x * cell
        def sy(y):
            return oy + y * cell

        # ---- track boundaries (analytic polygons; cheaper + smooth) ----
        N = 120
        outer = []
        inner = []
        for i in range(N + 1):
            theta = 2.0 * math.pi * i / N
            ro = _r_out(params, theta)
            ri = _r_in(params, theta)
            outer.append((sx(CX + ro * math.cos(theta)),
                          sy(CY + ro * math.sin(theta))))
            inner.append((sx(CX + ri * math.cos(theta)),
                          sy(CY + ri * math.sin(theta))))
        draw.line(outer, fill=0)
        draw.line(inner, fill=0)

        # ---- start/finish: dashed radial segment across the band ----
        th = s["theta_s"]
        ro = _r_out(params, th)
        ri = _r_in(params, th)
        ax, ay = sx(CX + ri * math.cos(th)), sy(CY + ri * math.sin(th))
        bx, by = sx(CX + ro * math.cos(th)), sy(CY + ro * math.sin(th))
        self._dash_line(draw, ax, ay, bx, by, dash=2, gap=2)

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

        # ---- car: filled dot at p ----
        cr = max(1.5, cell * 0.4)
        draw.ellipse((sx(px) - cr, sy(py) - cr, sx(px) + cr, sy(py) + cr),
                     fill=0)

        # ---- status footer ----
        opt = s["optimum"]
        opt_str = str(opt) if opt is not None else "—"
        best = s["best"]
        best_str = str(best) if best is not None else "—"
        foot = "spd %d,%d  opt %s  best %s  cr %d" % (
            vx, vy, opt_str, best_str, s["crashes"])
        if s["crashed"]:
            foot = "CRASH   " + foot
        draw.text((2, bot - 8), foot, font=small_f, fill=0)

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
