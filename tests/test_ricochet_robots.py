import json
import os

import pytest
from PIL import Image, ImageDraw

import ricochet_robots as rr

_APP_DIR = os.path.join(os.path.dirname(__file__), "..", "apps", "ricochet-robots")


def _empty(n):
    return [[0] * n for _ in range(n)]


def _wall_count(cells, x, y):
    return bin(cells[y][x]).count("1")


# --------------------------------------------------------------- constants ---
def test_constants_present():
    assert rr.WALL_BITS == [1, 2, 4, 8]
    assert rr.DIRS == [(0, -1), (1, 0), (0, 1), (-1, 0)]
    assert rr.OPP == [2, 3, 0, 1]
    assert rr.SHAPES == ["circle", "square", "triangle", "diamond"]
    assert rr.SIZES == [8, 12, 16]
    assert rr.WILDCARD == -1
    assert set(rr.BANDS) == {8, 12, 16}
    assert rr.K_BY_SIZE == {8: 6, 12: 12, 16: 17}
    # bands are ordered and non-overlapping-ish per size
    for n in rr.SIZES:
        e, m, h = (rr.BANDS[n]["easy"], rr.BANDS[n]["medium"], rr.BANDS[n]["hard"])
        assert e[0] <= e[1] < m[1] < h[1]


# ------------------------------------------------------------ wall helpers ---
def test_set_wall_mirrors_on_neighbour():
    cells = _empty(4)
    rr.set_wall(cells, 1, 1, 1)  # East wall of (1,1)
    assert rr.has_wall(cells, 1, 1, 1) is True
    assert rr.has_wall(cells, 2, 1, 3) is True  # West wall of the east neighbour
    assert rr.has_wall(cells, 1, 1, 0) is False


def test_set_wall_on_border_does_not_crash():
    cells = _empty(4)
    rr.set_wall(cells, 3, 0, 1)  # East wall at right edge: no neighbour to mirror
    assert rr.has_wall(cells, 3, 0, 1) is True


def test_center_cells():
    assert rr.center_cells(8) == {(3, 3), (4, 3), (3, 4), (4, 4)}
    assert rr.center_cells(12) == {(5, 5), (6, 5), (5, 6), (6, 6)}


# ------------------------------------------------------------------- slide ---
def test_slide_stops_at_border():
    cells = _empty(5)
    assert rr.slide(cells, [(2, 2)], 0, 1, set()) == (4, 2)   # East to edge
    assert rr.slide(cells, [(2, 2)], 0, 0, set()) == (2, 0)   # North to edge


def test_slide_stops_before_wall():
    cells = _empty(5)
    rr.set_wall(cells, 3, 2, 1)  # East wall of (3,2)
    assert rr.slide(cells, [(0, 2)], 0, 1, set()) == (3, 2)


def test_slide_stops_adjacent_to_robot():
    cells = _empty(5)
    assert rr.slide(cells, [(0, 2), (3, 2)], 0, 1, set()) == (2, 2)


def test_slide_stops_before_center_block():
    cells = _empty(8)
    blocked = rr.center_cells(8)  # {(3,3),(4,3),(3,4),(4,4)}
    assert rr.slide(cells, [(0, 3)], 0, 1, blocked) == (2, 3)


def test_slide_noop_when_immediately_blocked():
    cells = _empty(5)
    rr.set_wall(cells, 2, 2, 1)
    assert rr.slide(cells, [(2, 2)], 0, 1, set()) == (2, 2)


def test_fast_slide_matches_slide_over_random_boards():
    import random
    rng = random.Random(0)
    mism = 0
    for _ in range(200):
        n = rng.choice([8, 12, 16])
        cells, blocked, _ = rr._generate_walls(n, rr.K_BY_SIZE[n], rng)
        stops = rr._wall_stops(cells, blocked)
        free = [(x, y) for y in range(n) for x in range(n) if (x, y) not in blocked]
        robots = rng.sample(free, 4)
        for idx in range(4):
            for d in range(4):
                if rr.slide(cells, robots, idx, d, blocked) != rr._fast_slide(stops, robots, idx, d):
                    mism += 1
    assert mism == 0


# ------------------------------------------------------------------ solver ---
def test_solve_already_on_target():
    assert rr.solve(_empty(5), [(2, 2)], (0, 2, 2), set(), 1000, 20) == 0


def test_solve_single_slide():
    assert rr.solve(_empty(5), [(0, 2)], (0, 4, 2), set(), 1000, 20) == 1


def test_solve_needs_a_blocker():
    cells = _empty(5)
    # (2,2) is interior with no walls: a lone robot can never stop there.
    assert rr.solve(cells, [(0, 2)], (0, 2, 2), set(), 5000, 20) is None
    # Park a second robot at (3,2); robot 0 slides east and stops on (2,2).
    assert rr.solve(cells, [(0, 2), (3, 2)], (0, 2, 2), set(), 5000, 20) == 1


def test_solve_wildcard_any_robot():
    assert rr.solve(_empty(5), [(0, 0), (0, 2)], (rr.WILDCARD, 4, 2), set(), 5000, 20) == 1


def test_solve_returns_none_when_capped():
    assert rr.solve(_empty(8), [(0, 0)], (0, 4, 4), set(), 5, 20) is None


def test_solve_path_matches_solve_length():
    path = rr.solve_path(_empty(5), [(0, 2), (3, 2)], (0, 2, 2), set(), 5000, 20)
    assert path == [(0, 1)]  # robot 0 moves East (dir index 1)


def test_stop_reason_wall_vs_robot_vs_border():
    cells = _empty(5)
    rr.set_wall(cells, 3, 2, 1)
    assert rr._stop_reason(cells, [(0, 2)], 0, 1, set()) == "wall"
    assert rr._stop_reason(cells, [(0, 0)], 0, 1, set()) == "border"
    assert rr._stop_reason(cells, [(0, 2), (3, 2)], 0, 1, set()) == "robot"


def test_hard_quality_rejects_straight_shot():
    cells = _empty(8)
    caps = rr.CAPS[8]
    assert rr._hard_quality(cells, [(0, 4), (7, 7), (0, 0), (7, 0)],
                            (0, 7, 4), rr.center_cells(8), caps) is False


# -------------------------------------------------------------- generation ---
def test_generate_walls_invariants():
    import random
    for seed in range(20):
        n = 8
        cells, blocked, crooks = rr._generate_walls(n, rr.K_BY_SIZE[n], random.Random(seed))
        assert blocked == rr.center_cells(8)
        for y in range(n):
            for x in range(n):
                assert _wall_count(cells, x, y) <= 2  # no traps
                for d in range(4):
                    if rr.has_wall(cells, x, y, d):  # walls mirrored
                        nx, ny = x + rr.DIRS[d][0], y + rr.DIRS[d][1]
                        if 0 <= nx < n and 0 <= ny < n:
                            assert rr.has_wall(cells, nx, ny, rr.OPP[d])
        assert len(crooks) == rr.K_BY_SIZE[8]
        for cx, cy, _v, _h in crooks:
            assert 0 < cx < n - 1 and 0 < cy < n - 1
            assert (cx, cy) not in blocked


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
def test_generate_board_solvable_and_in_band(difficulty):
    lo, hi = rr.BANDS[8][difficulty]
    caps = rr.CAPS[8]
    for seed in range(6):
        b = rr.generate_board(8, difficulty, seed)
        blocked = {tuple(c) for c in b["blocked"]}
        m = rr.solve(b["cells"], b["robots"], tuple(b["target"]),
                     blocked, caps["max_states"], caps["max_depth"])
        assert m is not None                  # always solvable
        assert m == b["optimal"]              # stored optimum == true minimum
        assert lo - 1 <= m <= hi + 1          # in band (fallback may widen by 1)
        assert len(b["robots"]) == len(rr.SHAPES)
        assert tuple(b["target"][1:]) not in {tuple(r) for r in b["robots"]}


def test_generate_board_is_deterministic():
    a = rr.generate_board(8, "medium", 12345)
    b = rr.generate_board(8, "medium", 12345)
    assert a["cells"] == b["cells"]
    assert a["robots"] == b["robots"]
    assert a["target"] == b["target"]
    assert a["optimal"] == b["optimal"]


def test_seed_for_is_stable_and_difficulty_sensitive():
    assert rr._seed_for(8, "medium", 700000) == rr._seed_for(8, "medium", 700000)
    assert rr._seed_for(8, "medium", 700000) != rr._seed_for(8, "hard", 700000)


# ------------------------------------------------------------------- class ---
def _fresh_game(monkeypatch, tmp_path):
    monkeypatch.setattr(rr, "STATE_PATH", str(tmp_path / "s.json"))
    return rr.RicochetRobots()


def test_class_boots_with_a_default_board(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    assert g._size in rr.SIZES
    assert len(g._robots) == len(rr.SHAPES)
    st = g.published_state()
    for key in ("mode", "seed", "size", "difficulty", "target_shape",
                "selected", "moves", "optimal", "best", "status"):
        assert key in st
    assert st["moves"] == 0


def test_class_round_trips_through_persisted_state(monkeypatch, tmp_path):
    path = str(tmp_path / "s.json")
    monkeypatch.setattr(rr, "STATE_PATH", path)
    g1 = rr.RicochetRobots()
    g1._selected = 2
    g1._persist()
    g2 = rr.RicochetRobots()
    assert g2._size == g1._size
    assert g2._seed == g1._seed
    assert g2._cells == g1._cells
    assert g2._selected == 2


def test_select_sets_active_robot(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    assert g.on_data({"action": "select", "robot": "3"}) is True
    assert g._selected == 3
    assert g.on_data({"action": "select", "robot": "99"}) is True  # clamped
    assert g._selected == len(rr.SHAPES) - 1


def test_move_wins_and_is_ignored_after_completion(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    g._size = 8
    g._cells = _empty(8)
    g._blocked = set()
    g._robots = [(0, 0), (0, 7), (7, 7), (3, 7)]
    g._robot_start = list(g._robots)
    g._target = (0, 7, 0)
    g._selected = 0
    g._moves = 0
    g._history = []
    g._completed = False
    assert g.on_data({"action": "move", "dir": "right"}) is True
    assert g._robots[0] == (7, 0) and g._moves == 1   # shape 0 lands on (7,0)
    assert g._completed is True
    assert g.on_data({"action": "move", "dir": "down"}) is True  # ignored
    assert g._moves == 1


def test_undo_then_restart(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    g._size = 8
    g._cells = _empty(8)
    g._blocked = set()
    g._robots = [(0, 3), (0, 7), (7, 7), (7, 0)]
    g._robot_start = list(g._robots)
    g._target = (1, 4, 4)
    g._selected = 0
    g._moves = 0
    g._history = []
    g._completed = False
    g.on_data({"action": "move", "dir": "right"})  # robot 0 -> (7,3)
    assert g._robots[0] == (7, 3) and g._moves == 1
    g.on_data({"action": "undo"})
    assert g._robots[0] == (0, 3) and g._moves == 0
    g.on_data({"action": "move", "dir": "right"})
    g.on_data({"action": "restart"})
    assert g._robots == [(0, 3), (0, 7), (7, 7), (7, 0)] and g._moves == 0


def _materialize(g):
    """Drive the two deferred-render passes (announce, then generate)."""
    img = Image.new("1", (234, 122), 1)
    g.render(ImageDraw.Draw(img), 234, 122)  # announce
    g.render(ImageDraw.Draw(img), 234, 122)  # generate + paint


def test_move_moves_the_selected_robot(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    g._size = 8
    g._cells = _empty(8)
    g._blocked = set()
    g._robots = [(0, 0), (0, 4), (7, 7), (3, 7)]
    g._robot_start = list(g._robots)
    g._target = (2, 1, 1)
    g._moves = 0
    g._history = []
    g._completed = False
    g.on_data({"action": "select", "robot": "1"})
    assert g._selected == 1
    g.on_data({"action": "move", "dir": "right"})
    assert g._robots[1] == (7, 4)   # the SELECTED robot slid east
    assert g._robots[0] == (0, 0)   # robot 0 untouched (the reported bug)


def test_new_board_is_deferred_then_generated(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    before_seed = g._seed
    assert g.on_data({"action": "new_random", "size": "12", "difficulty": "medium"}) is True
    # on_data only queued the work — nothing generated yet.
    assert g._pending == ("random", 12, "medium")
    assert g._gen_phase == "announce"
    assert g.interval_seconds == 0.05
    assert g._seed == before_seed
    img = Image.new("1", (234, 122), 1)
    g.render(ImageDraw.Draw(img), 234, 122)            # announce frame
    assert g._gen_phase == "generate" and g._pending is not None
    assert img.getextrema()[0] == 0                    # "Generating..." drawn
    g.render(ImageDraw.Draw(img), 234, 122)            # generate
    assert g._pending is None
    assert g.interval_seconds is None
    assert g._size == 12 and g._difficulty == "medium" and g._moves == 0


def test_new_daily_is_reproducible(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    g.on_data({"action": "new_daily", "size": "8", "difficulty": "easy"})
    _materialize(g)
    seed1, cells1 = g._seed, [row[:] for row in g._cells]
    g.on_data({"action": "new_random", "size": "8", "difficulty": "hard"})
    _materialize(g)
    g.on_data({"action": "new_daily", "size": "8", "difficulty": "easy"})
    _materialize(g)
    assert g._seed == seed1
    assert g._cells == cells1


def test_unknown_action_returns_false(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    assert g.on_data({"action": "nope"}) is False
    assert g.on_data({}) is False


# ---------------------------------------------------------------- rendering ---
def test_render_draws_ink_without_error(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    img = Image.new("1", (234, 122), 1)
    g.render(ImageDraw.Draw(img), 234, 122)
    assert img.getextrema()[0] == 0  # some black pixels drawn


def test_render_victory_screen_runs(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    g._completed = True
    img = Image.new("1", (234, 122), 1)
    g.render(ImageDraw.Draw(img), 234, 122)
    assert img.getextrema()[0] == 0


def test_render_all_sizes(monkeypatch, tmp_path):
    g = _fresh_game(monkeypatch, tmp_path)
    for size in rr.SIZES:
        g.on_data({"action": "new_random", "size": str(size), "difficulty": "easy"})
        _materialize(g)  # announce + generate
        assert g._size == size
        img = Image.new("1", (234, 122), 1)
        g.render(ImageDraw.Draw(img), 234, 122)
        assert img.getextrema()[0] == 0


# --------------------------------------------------------- manifest + ui ---
def test_manifest_shape():
    with open(os.path.join(_APP_DIR, "ricochet_robots.manifest.json")) as f:
        m = json.load(f)
    assert m["name"] == "ricochet-robots"
    assert m["icon"] == "RR"
    assert m["version"] == rr.RicochetRobots.version
    assert m["category"] == "fun"
    for key in ("author", "description", "long_description", "schema_version"):
        assert key in m


def test_ui_is_valid_json_with_actions():
    with open(os.path.join(_APP_DIR, "ricochet_robots.ui.json")) as f:
        ui = json.load(f)
    assert ui["type"] == "column"
    blob = json.dumps(ui)
    for action in ("select", "move", "undo", "restart", "new_random", "new_daily"):
        assert action in blob
