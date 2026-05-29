# Tide & Sun clock.
#
# Single data source: the WorldTides v3 API (declared in manifest.json's
# `data_source`). On Sync the phone GETs that URL and POSTs the envelope
#   {"location": {"lat":.., "lon":.., "label":..} | null,
#    "fetched":  <raw WorldTides response> | null}
#
# Credit budget (free tier = 100/month): one call asks for 7 days of `heights`
# at 30-min steps, which WorldTides bills as a single credit. We DON'T ask for
# `extremes` — the high/low turning points are computed device-side from the
# height curve. The 7-day curve is cached on disk and survives restarts, so the
# phone only needs to sync about once a week (the manifest declares the app as
# manual-sync / weekly via data_source.auto_sync + min_sync_seconds).
#
# Times are shown in the *location's* local time, derived from longitude
# (round(lon/15)h). This is solar-zone time: exact for non-DST zones (the
# device clock is irrelevant) but can be ~1h off where DST / political borders
# apply.
#
# Sunrise/sunset are computed device-side (sunrise equation); moon phase via a
# Conway approximation. The phone can also push view controls (no re-fetch):
#   {"action": "next" | "prev"}   jump the window to the next/prev tide
#   {"action": "reset"}            re-centre on "now"

import json
import math
import os
import time
from datetime import datetime, timezone

from PIL import ImageFont


STATE_PATH = "/etc/pwnagotchi/tide_sun.state.json"

MOON_PHASES = [
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
]
SYNODIC_MONTH = 29.530588853  # days
KNOWN_NEW_MOON_EPOCH = 1737374400  # 2025-01-20 12:00 UTC (a known new moon)

SHOWN_EXTREMES = 3        # how many high/low markers the graph fits
WINDOW_PAD = 2700         # 45 min of breathing room each side of the markers
ROW1_FRAC = 0.35          # sun/moon strip; the graph gets the rest


def _load_state():
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _tz_offset(loc):
    """Location's UTC offset in seconds, from longitude (solar zone)."""
    lon = (loc or {}).get("lon")
    if not isinstance(lon, (int, float)):
        return 0
    return int(round(lon / 15.0)) * 3600


def _shift(epoch, off):
    return datetime.fromtimestamp(epoch + off, tz=timezone.utc)


def _fmt_clock(epoch, off):
    if epoch is None:
        return "--:--"
    return _shift(epoch, off).strftime("%H:%M")


def _moon_phase(now_epoch):
    """Return (phase_name, illumination 0..1, waxing bool) using a Conway
    approximation against a known new-moon epoch."""
    age = ((now_epoch - KNOWN_NEW_MOON_EPOCH) / 86400.0) % SYNODIC_MONTH
    if age < 0:
        age += SYNODIC_MONTH
    illum = (1 - math.cos(2 * math.pi * age / SYNODIC_MONTH)) / 2
    bucket = int((age / SYNODIC_MONTH) * 8 + 0.5) % 8
    return MOON_PHASES[bucket], illum, age < SYNODIC_MONTH / 2


def _sun_times(lat, lon, year, month, day):
    """Standard sunrise equation. Returns (sunrise_epoch, sunset_epoch) in unix
    UTC seconds, or (None, None) for polar day/night or bad input."""
    try:
        a = (14 - month) // 12
        y = year + 4800 - a
        m = month + 12 * a - 3
        jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
        n = jdn - 2451545 + 0.0008
        jstar = n - lon / 360.0
        M = math.radians((357.5291 + 0.98560028 * jstar) % 360)
        C = 1.9148 * math.sin(M) + 0.0200 * math.sin(2 * M) + 0.0003 * math.sin(3 * M)
        lam = math.radians((math.degrees(M) + C + 282.9372) % 360)
        jtransit = 2451545.0 + jstar + 0.0053 * math.sin(M) - 0.0069 * math.sin(2 * lam)
        sin_dec = math.sin(lam) * math.sin(math.radians(23.44))
        cos_dec = math.cos(math.asin(sin_dec))
        latr = math.radians(lat)
        cos_w = (math.sin(math.radians(-0.83)) - math.sin(latr) * sin_dec) / (math.cos(latr) * cos_dec)
        if cos_w > 1 or cos_w < -1:
            return None, None
        w = math.degrees(math.acos(cos_w)) / 360.0

        def jd_to_unix(jd):
            return (jd - 2440587.5) * 86400.0

        return jd_to_unix(jtransit - w), jd_to_unix(jtransit + w)
    except Exception:
        return None, None


def _parse_heights(fetched):
    """Pull a sorted (dt, height) curve out of a WorldTides response."""
    pts = []
    if isinstance(fetched, dict):
        for h in fetched.get("heights") or []:
            dt, ht = h.get("dt"), h.get("height")
            if isinstance(dt, (int, float)) and isinstance(ht, (int, float)):
                pts.append((int(dt), float(ht)))
    pts.sort(key=lambda p: p[0])
    return pts


def _extremes_from_heights(heights):
    """Find high/low turning points in the curve, refining each to sub-sample
    accuracy with a parabolic vertex estimate. Returns sorted
    [(dt, height, "H"/"L")]."""
    n = len(heights)
    out = []
    for i in range(1, n - 1):
        dt0, y0 = heights[i - 1]
        dt1, y1 = heights[i]
        _, y2 = heights[i + 1]
        d_prev, d_next = y1 - y0, y2 - y1
        is_max = d_prev >= 0 and d_next <= 0 and (d_prev > 0 or d_next < 0)
        is_min = d_prev <= 0 and d_next >= 0 and (d_prev < 0 or d_next > 0)
        if not (is_max or is_min):
            continue
        denom = y0 - 2 * y1 + y2
        p = max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom)) if denom else 0.0
        rdt = int(round(dt1 + p * (dt1 - dt0)))
        rht = y1 - 0.25 * (y0 - y2) * p
        out.append((rdt, rht, "H" if is_max else "L"))
    # Collapse a flat crest sampled as two equal points into one marker.
    ded = []
    for e in out:
        if ded and ded[-1][2] == e[2] and abs(e[0] - ded[-1][0]) < 3600:
            better = (e[2] == "H" and e[1] > ded[-1][1]) or (e[2] == "L" and e[1] < ded[-1][1])
            if better:
                ded[-1] = e
        else:
            ded.append(e)
    return ded


def _draw_moon(draw, cx, cy, r, illum, waxing):
    """Tiny moon-phase disc: outline + lit portion (right when waxing)."""
    bbox = (cx - r, cy - r, cx + r, cy + r)
    draw.ellipse(bbox, outline=0, fill=255)
    if illum <= 0.04:
        return
    if illum >= 0.96:
        draw.ellipse(bbox, fill=0)
        return
    draw.chord(bbox, -90, 90, fill=0) if waxing else draw.chord(bbox, 90, 270, fill=0)
    tw = int(round(r * abs(1 - 2 * illum)))
    if tw > 0:
        draw.ellipse((cx - tw, cy - r, cx + tw, cy + r),
                     fill=(0 if illum > 0.5 else 255))


class TideSun:
    name = "tide-sun"
    icon = "TS"
    version = "1.5.0"
    interval_seconds = 60

    def __init__(self):
        s = _load_state()
        self._loc = s.get("loc") or {}
        self._heights = [tuple(p) for p in s.get("heights") or []]
        self._extremes = [tuple(e) for e in s.get("extremes") or []]
        self._fetched_at = s.get("fetched_at") or 0
        self._anchor = 0
        self._reset_anchor()

    # ---- view state ----
    def _reset_anchor(self):
        """Anchor the window on the last tide at/just before now, leaving room
        for SHOWN_EXTREMES markers."""
        now = time.time()
        idx = 0
        for i, (dt, _, _) in enumerate(self._extremes):
            if dt <= now:
                idx = i
        self._anchor = max(0, min(idx, max(0, len(self._extremes) - SHOWN_EXTREMES)))

    def _shown(self):
        return self._extremes[self._anchor:self._anchor + SHOWN_EXTREMES]

    def _window(self):
        shown = self._shown()
        if not shown:
            now = time.time()
            return now, now + 12 * 3600, shown
        t0 = shown[0][0] - WINDOW_PAD
        t1 = shown[-1][0] + WINDOW_PAD
        return t0, (t1 if t1 > t0 else t0 + 3600), shown

    def _persist(self):
        _save_state({
            "loc": self._loc,
            "heights": [list(p) for p in self._heights],
            "extremes": [list(e) for e in self._extremes],
            "fetched_at": self._fetched_at,
        })

    # ---- host hooks ----
    def on_data(self, payload):
        if not isinstance(payload, dict):
            return False
        action = payload.get("action")
        if action in ("next", "prev", "reset"):
            top = max(0, len(self._extremes) - SHOWN_EXTREMES)
            if action == "next":
                self._anchor = min(self._anchor + 1, top)
            elif action == "prev":
                self._anchor = max(self._anchor - 1, 0)
            else:
                self._reset_anchor()
            return True

        # Otherwise it's the sync envelope: {location, fetched}.
        heights = _parse_heights(payload.get("fetched"))
        if not heights:
            return False  # failed/empty fetch — keep the cached curve
        raw_loc = payload.get("location")
        self._loc = raw_loc if isinstance(raw_loc, dict) else {}
        self._heights = heights
        self._extremes = _extremes_from_heights(heights)
        self._fetched_at = int(time.time())
        self._reset_anchor()
        self._persist()
        return True

    def _day_note(self, ref_epoch, off, now):
        ddays = (_shift(ref_epoch, off).date() - _shift(now, off).date()).days
        if ddays == 0:
            return "today"
        return "%s %d %+dd" % (_shift(ref_epoch, off).strftime("%a"),
                               _shift(ref_epoch, off).day, ddays)

    def published_state(self):
        now = time.time()
        off = _tz_offset(self._loc)
        lat, lon = self._loc.get("lat"), self._loc.get("lon")
        sr = ss = None
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            ld = _shift(now, off)
            sr, ss = _sun_times(lat, lon, ld.year, ld.month, ld.day)
        phase, illum, _ = _moon_phase(int(now))

        t0, t1, shown = self._window()
        nxt = next(((dt, ht, ty) for dt, ht, ty in self._extremes if dt >= now), None)
        days_left = round((self._heights[-1][0] - now) / 86400.0, 1) if self._heights else 0.0
        if not self._heights:
            cache_status = "No tide data — tap Sync"
        elif days_left <= 0.5:
            cache_status = "Tide data spent — Sync soon"
        else:
            cache_status = "Tide data cached: %.1f days left" % days_left

        return {
            "location":     self._loc.get("label") or "",
            "sunrise":      _fmt_clock(sr, off),
            "sunset":       _fmt_clock(ss, off),
            "moon_phase":   "%s %d%%" % (phase, int(illum * 100)),
            "window_label": "%s → %s" % (_fmt_clock(t0, off), _fmt_clock(t1, off)),
            "day_note":     self._day_note((t0 + t1) // 2, off, now),
            "next_event":   ("%s %s (%.1fm)" % ("High" if nxt[2] == "H" else "Low",
                                                _fmt_clock(nxt[0], off), nxt[1])) if nxt else "—",
            "cache_status": cache_status,
            "tide_count":   len(self._extremes),
            "tide_start":   int(now - 43200),  # WorldTides start: now − 12h
        }

    # ---- render ----
    def render(self, draw, w, h):
        small = ImageFont.truetype("DejaVuSansMono-Bold", 9)
        tiny = ImageFont.truetype("DejaVuSansMono-Bold", 8)
        off = _tz_offset(self._loc)

        row1_h = int(h * ROW1_FRAC)
        self._render_sun_moon(draw, w, row1_h, small, off)
        draw.line((2, row1_h, w - 2, row1_h), fill=0)
        self._render_tides(draw, w, h, row1_h, small, tiny, off)

    def _render_sun_moon(self, draw, w, row1_h, font, off):
        now = int(time.time())
        lat, lon = self._loc.get("lat"), self._loc.get("lon")
        sr = ss = None
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            ld = _shift(now, off)
            sr, ss = _sun_times(lat, lon, ld.year, ld.month, ld.day)
        ty = max(1, (row1_h - 11) // 2)
        draw.text((3, ty), "Sunrise %s  Sunset %s  Moon" %
                  (_fmt_clock(sr, off), _fmt_clock(ss, off)), font=font, fill=0)

        # Moon disc + illum% hard against the right edge.
        _, illum, waxing = _moon_phase(now)
        pct = "%d%%" % int(illum * 100)
        pct_w = int(draw.textlength(pct, font=font))
        r = 5
        cy = row1_h // 2
        _draw_moon(draw, w - pct_w - 7 - r, cy, r, illum, waxing)
        draw.text((w - pct_w - 3, ty), pct, font=font, fill=0)

    def _render_tides(self, draw, w, h, top, font, tiny, off):
        now = time.time()
        t0, t1, shown = self._window()

        # Header: left = which tides, right = day note.
        draw.text((3, top + 2), "TIDE", font=tiny, fill=0)
        note = self._day_note((t0 + t1) // 2, off, now)
        nw = int(draw.textlength(note, font=tiny))
        draw.text((w - nw - 3, top + 2), note, font=tiny, fill=0)

        px0, px1 = 4, w - 4
        py_top, py_bot = top + 24, h - 11   # extra top room so H labels clear the header

        if not shown:
            draw.text((px0, py_top), "no tide data — tap Sync", font=font, fill=0)
            return

        def xpos(dt):
            return px0 + (dt - t0) / (t1 - t0) * (px1 - px0)

        vis = [(dt, ht) for dt, ht in self._heights if t0 <= dt <= t1]
        if len(vis) < 2:
            draw.text((px0, py_top), "no tide data — tap Sync", font=font, fill=0)
            return

        hmin = min(ht for _, ht in vis)
        hmax = max(ht for _, ht in vis)
        span = (hmax - hmin) or 1.0

        def ypos(ht):
            return py_bot - (ht - hmin) / span * (py_bot - py_top)

        draw.line((px0, py_bot, px1, py_bot), fill=0)
        draw.line([(xpos(dt), ypos(ht)) for dt, ht in vis], fill=0, width=1)

        if t0 <= now <= t1:
            nx = int(xpos(now))
            for yy in range(py_top, py_bot, 4):
                draw.line((nx, yy, nx, yy + 2), fill=0)

        # The SHOWN_EXTREMES markers, with a clear gap between dot and label.
        for dt, ht, typ in shown:
            ex, ey = int(xpos(dt)), int(ypos(ht))
            draw.ellipse((ex - 2, ey - 2, ex + 2, ey + 2), fill=0)
            label = "%s %s" % (typ, _fmt_clock(dt, off))
            lw = int(draw.textlength(label, font=tiny))
            lx = max(px0, min(ex - lw // 2, px1 - lw))
            # Always label above the dot — lows sit on the baseline, so a label
            # below them would collide with the x-axis clock row.
            ly = max(top + 11, ey - 13)
            draw.text((lx, ly), label, font=tiny, fill=0)

        # X-axis clock labels: window start / centre / end.
        for frac, dt in ((0.0, t0), (0.5, (t0 + t1) / 2), (1.0, t1)):
            lbl = _fmt_clock(dt, off)
            lw = int(draw.textlength(lbl, font=tiny))
            lx = int(px0 + frac * (px1 - px0))
            lx = max(px0, min(lx - lw // 2, px1 - lw))
            draw.text((lx, py_bot + 1), lbl, font=tiny, fill=0)
