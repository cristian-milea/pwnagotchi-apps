# Tide & Sun clock.
#
# Single data source: the WorldTides v3 API (declared in manifest.json's
# `data_source`). On Sync the phone GETs that URL and POSTs the envelope
#   {"location": {"lat":.., "lon":.., "label":..} | null,
#    "fetched":  <raw WorldTides response> | null}
# `fetched` carries `heights` (a height curve) and `extremes` (high/low
# markers). Sunrise/sunset are computed device-side from the location with
# the standard sunrise equation; moon phase via a Conway approximation.
# WorldTides needs a free API key, supplied as the `worldtides` secret.
#
# The phone can also push view controls (no re-fetch):
#   {"action": "shift", "delta": +6 | -6}   shift the 12h window by hours
#   {"action": "reset"}                      re-centre on "now"

import math
import time
from datetime import datetime, timezone

from PIL import ImageFont


MOON_PHASES = [
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
]
SYNODIC_MONTH = 29.530588853  # days
KNOWN_NEW_MOON_EPOCH = 1737374400  # 2025-01-20 12:00 UTC (a known new moon)

WINDOW_HALF_H = 6           # graph spans now±6h => a 12h window
MAX_OFFSET_H = 48           # how far the phone may scrub either way
TIDE_BACK_SECONDS = 86400   # how far before "now" we ask WorldTides to start
ROW1_FRAC = 0.35            # sun/moon strip; the graph gets the rest


def _fmt_clock(epoch):
    """Unix epoch -> local HH:MM."""
    if epoch is None:
        return "--:--"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%H:%M")


def _moon_phase(now_epoch):
    """Return (phase_name, illumination 0..1, waxing bool) using a Conway
    approximation against a known new-moon epoch. Accurate enough for a
    label on an e-ink clock."""
    age = ((now_epoch - KNOWN_NEW_MOON_EPOCH) / 86400.0) % SYNODIC_MONTH
    if age < 0:
        age += SYNODIC_MONTH
    illum = (1 - math.cos(2 * math.pi * age / SYNODIC_MONTH)) / 2
    bucket = int((age / SYNODIC_MONTH) * 8 + 0.5) % 8
    return MOON_PHASES[bucket], illum, age < SYNODIC_MONTH / 2


def _sun_times(lat, lon, year, month, day):
    """Standard sunrise equation (Wikipedia). Returns (sunrise_epoch,
    sunset_epoch) in unix UTC seconds, or (None, None) for polar day/night
    or bad input. lon east-positive."""
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
            return None, None  # sun never rises / never sets here today
        w = math.degrees(math.acos(cos_w)) / 360.0

        def jd_to_unix(jd):
            return (jd - 2440587.5) * 86400.0

        return jd_to_unix(jtransit - w), jd_to_unix(jtransit + w)
    except Exception:
        return None, None


def _tide_points(fetched):
    """Pull (dt, height) curve points and (dt, height, type) extremes out of
    a WorldTides response. Returns (heights, extremes), both sorted by dt."""
    heights, extremes = [], []
    if not isinstance(fetched, dict):
        return heights, extremes
    for h in fetched.get("heights") or []:
        dt, ht = h.get("dt"), h.get("height")
        if isinstance(dt, (int, float)) and isinstance(ht, (int, float)):
            heights.append((int(dt), float(ht)))
    for e in fetched.get("extremes") or []:
        dt, ht = e.get("dt"), e.get("height")
        typ = (e.get("type") or "").lower()
        if isinstance(dt, (int, float)) and isinstance(ht, (int, float)):
            extremes.append((int(dt), float(ht), "H" if typ.startswith("h") else "L"))
    heights.sort(key=lambda p: p[0])
    extremes.sort(key=lambda p: p[0])
    return heights, extremes


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
    version = "1.4.0"
    interval_seconds = 60

    def __init__(self):
        self._loc = {}          # last location dict
        self._heights = []       # [(dt, height)]
        self._extremes = []      # [(dt, height, "H"/"L")]
        self._offset_h = 0       # window shift in hours

    # ---- derived view helpers ----
    def _center(self):
        return time.time() + self._offset_h * 3600

    def _day_note(self):
        center_local = datetime.fromtimestamp(self._center(), tz=timezone.utc).astimezone()
        today = datetime.now().astimezone()
        ddays = (center_local.date() - today.date()).days
        if ddays == 0:
            return "today"
        return "%s %d %+dd" % (center_local.strftime("%a"), center_local.day, ddays)

    def _next_event(self):
        now = time.time()
        for dt, ht, typ in self._extremes:
            if dt >= now:
                label = "High" if typ == "H" else "Low"
                return "%s %s (%.1fm)" % (label, _fmt_clock(dt), ht)
        return "—"

    # ---- host hooks ----
    def on_data(self, payload):
        if not isinstance(payload, dict):
            return False
        action = payload.get("action")
        if action == "shift":
            try:
                delta = int(payload.get("delta", 0))
            except (TypeError, ValueError):
                delta = 0
            self._offset_h = max(-MAX_OFFSET_H, min(MAX_OFFSET_H, self._offset_h + delta))
            return True
        if action == "reset":
            self._offset_h = 0
            return True
        # Otherwise it's the sync envelope: {location, fetched}.
        raw_loc = payload.get("location")
        self._loc = raw_loc if isinstance(raw_loc, dict) else {}
        self._heights, self._extremes = _tide_points(payload.get("fetched"))
        self._offset_h = 0  # fresh data re-centres on now
        return True

    def published_state(self):
        now_local = datetime.now().astimezone()
        sr, ss = (None, None)
        if isinstance(self._loc.get("lat"), (int, float)) and \
           isinstance(self._loc.get("lon"), (int, float)):
            sr, ss = _sun_times(self._loc["lat"], self._loc["lon"],
                                now_local.year, now_local.month, now_local.day)
        phase, illum, _ = _moon_phase(int(time.time()))
        t0, t1 = self._center() - WINDOW_HALF_H * 3600, self._center() + WINDOW_HALF_H * 3600
        return {
            "location":     self._loc.get("label") or "",
            "sunrise":      _fmt_clock(sr),
            "sunset":       _fmt_clock(ss),
            "moon_phase":   "%s %d%%" % (phase, int(illum * 100)),
            "offset_label": "now" if self._offset_h == 0 else "now %+dh" % self._offset_h,
            "day_note":     self._day_note(),
            "window_label": "%s – %s" % (_fmt_clock(t0), _fmt_clock(t1)),
            "next_event":   self._next_event(),
            "tide_count":   len(self._extremes),
            "tide_start":   int(time.time() - TIDE_BACK_SECONDS),
        }

    # ---- render ----
    def render(self, draw, w, h):
        small = ImageFont.truetype("DejaVuSansMono-Bold", 9)
        tiny = ImageFont.truetype("DejaVuSansMono-Bold", 8)

        row1_h = int(h * ROW1_FRAC)
        self._render_sun_moon(draw, w, row1_h, small)
        draw.line((2, row1_h, w - 2, row1_h), fill=0)
        self._render_tides(draw, w, h, row1_h, small, tiny)

    def _render_sun_moon(self, draw, w, row1_h, font):
        s = self.published_state()
        line = "Sunrise %s   Sunset %s" % (s["sunrise"], s["sunset"])
        ty = max(1, (row1_h - 11) // 2)
        draw.text((3, ty), line, font=font, fill=0)

        # Moon disc + % on the right edge of the strip.
        _, illum, waxing = _moon_phase(int(time.time()))
        pct = "%d%%" % int(illum * 100)
        pct_w = int(draw.textlength(pct, font=font))
        r = 5
        cy = row1_h // 2
        cx = w - pct_w - 6 - r
        _draw_moon(draw, cx, cy, r, illum, waxing)
        draw.text((w - pct_w - 3, ty), pct, font=font, fill=0)

    def _render_tides(self, draw, w, h, top, font, tiny):
        center = self._center()
        t0, t1 = center - WINDOW_HALF_H * 3600, center + WINDOW_HALF_H * 3600

        # Header: left = view offset, right = day note.
        offset_label = "TIDE  now" if self._offset_h == 0 else "TIDE  now%+dh" % self._offset_h
        draw.text((3, top + 2), offset_label, font=tiny, fill=0)
        note = self._day_note()
        nw = int(draw.textlength(note, font=tiny))
        draw.text((w - nw - 3, top + 2), note, font=tiny, fill=0)

        px0, px1 = 4, w - 4
        py_top, py_bot = top + 13, h - 11

        def xpos(dt):
            return px0 + (dt - t0) / (t1 - t0) * (px1 - px0)

        vis = [(dt, ht) for dt, ht in self._heights if t0 <= dt <= t1]
        if len(vis) < 2:
            draw.text((px0, py_top + 8), "no tide data in range — Sync",
                      font=font, fill=0)
            return

        hmin = min(ht for _, ht in vis)
        hmax = max(ht for _, ht in vis)
        span = (hmax - hmin) or 1.0

        def ypos(ht):
            return py_bot - (ht - hmin) / span * (py_bot - py_top)

        # Baseline + the height curve.
        draw.line((px0, py_bot, px1, py_bot), fill=0)
        pts = [(xpos(dt), ypos(ht)) for dt, ht in vis]
        draw.line(pts, fill=0, width=1)

        # "now" marker (dashed vertical) when now is inside the window.
        now = time.time()
        if t0 <= now <= t1:
            nx = int(xpos(now))
            for yy in range(py_top, py_bot, 4):
                draw.line((nx, yy, nx, yy + 2), fill=0)

        # High/low markers within the window.
        for dt, ht, typ in self._extremes:
            if not (t0 <= dt <= t1):
                continue
            ex, ey = int(xpos(dt)), int(ypos(ht))
            draw.ellipse((ex - 2, ey - 2, ex + 2, ey + 2), fill=0)
            label = "%s %s" % (typ, _fmt_clock(dt))
            lw = int(draw.textlength(label, font=tiny))
            lx = max(px0, min(ex - lw // 2, px1 - lw))
            ly = ey - 11 if typ == "H" else ey + 4
            ly = max(py_top, min(ly, py_bot - 9))
            draw.text((lx, ly), label, font=tiny, fill=0)

        # X-axis clock labels: window start / centre / end.
        for frac, dt in ((0.0, t0), (0.5, center), (1.0, t1)):
            lbl = _fmt_clock(dt)
            lw = int(draw.textlength(lbl, font=tiny))
            lx = int(px0 + frac * (px1 - px0))
            lx = max(px0, min(lx - lw // 2, px1 - lw))
            draw.text((lx, py_bot + 1), lbl, font=tiny, fill=0)
