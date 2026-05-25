# Tide & Sun clock.
#
# Phone fetches the URL declared in manifest.json's `data_source` and POSTs:
#   {"location": {"lat":.., "lon":.., "label":..} | null,
#    "fetched":  <raw external API response> | null}
# This app expects the Open-Meteo forecast response under `fetched`. Moon
# phase is computed locally; tides are not provided by Open-Meteo so the
# list stays empty (the app renders "—" under NEXT TIDE).

import math
import time
from datetime import datetime, timezone

from PIL import ImageFont

from pwn_apps_host import draw_wrapped


MOON_PHASES = [
    "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
    "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
]
SYNODIC_MONTH = 29.530588853  # days
KNOWN_NEW_MOON_EPOCH = 1737374400  # 2025-01-20 12:00 UTC (a known new moon)


def _parse_iso(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _fmt_local(dt):
    if dt is None:
        return "--:--"
    return dt.astimezone().strftime("%H:%M")


def _next(events, now):
    future = [e for e in events if _parse_iso(e.get("at")) and _parse_iso(e["at"]) > now]
    future.sort(key=lambda e: _parse_iso(e["at"]))
    return future[0] if future else None


def _moon_phase(now_epoch):
    """Return (phase_name, illumination_fraction 0..1) using a Conway
    approximation against a known new-moon epoch. Accurate enough for a
    label on an e-ink clock."""
    days_since = (now_epoch - KNOWN_NEW_MOON_EPOCH) / 86400.0
    age = days_since % SYNODIC_MONTH
    if age < 0:
        age += SYNODIC_MONTH
    # Illumination: 0 at new, 1 at full, back to 0. cos curve.
    illum = (1 - math.cos(2 * math.pi * age / SYNODIC_MONTH)) / 2
    # Bucket the age into 8 phases.
    bucket = int((age / SYNODIC_MONTH) * 8 + 0.5) % 8
    return MOON_PHASES[bucket], illum


def _normalise_iso(raw, utc_offset_seconds):
    if not raw:
        return raw
    if raw.endswith("Z") or "+" in raw or len(raw) >= 6 and raw[-6] == "-" and raw[-3] == ":":
        return raw
    offset = utc_offset_seconds or 0
    sign = "+" if offset >= 0 else "-"
    a = abs(offset)
    return "%s%s%02d:%02d" % (raw, sign, a // 3600, (a % 3600) // 60)


def _transform(envelope, now_epoch):
    if not isinstance(envelope, dict):
        return {"location": "", "tides": [], "moon": {}}
    raw_loc = envelope.get("location")
    loc = raw_loc if isinstance(raw_loc, dict) else {}
    fetched = envelope.get("fetched")
    out = {"location": loc.get("label") or "", "tides": []}

    phase, illum = _moon_phase(now_epoch)
    out["moon"] = {"phase": phase, "illum": illum}

    if not isinstance(fetched, dict):
        return out

    offset = fetched.get("utc_offset_seconds")
    daily = fetched.get("daily") or {}
    sunrise_list = daily.get("sunrise") or []
    sunset_list = daily.get("sunset") or []
    if sunrise_list:
        out["sunrise"] = _normalise_iso(sunrise_list[0], offset)
    if sunset_list:
        out["sunset"] = _normalise_iso(sunset_list[0], offset)
    return out


class TideSun:
    name = "tide-sun"
    icon = "TS"
    version = "1.3.0"
    interval_seconds = 60

    def __init__(self):
        self._data = None

    def published_state(self):
        d = self._data or {}
        return {
            "location":   d.get("location") or "",
            "sunrise":    d.get("sunrise") or "",
            "sunset":     d.get("sunset") or "",
            "moon_phase": (d.get("moon") or {}).get("phase") or "",
            "tide_count": len(d.get("tides") or []),
        }

    def on_data(self, payload):
        if not isinstance(payload, dict):
            self._data = None
            return
        self._data = _transform(payload, int(time.time()))

    def render(self, draw, w, h):
        title_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        label_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        big = ImageFont.truetype("DejaVuSansMono-Bold", 14)
        small = ImageFont.truetype("DejaVuSansMono-Bold", 9)

        d = self._data or {}
        loc = d.get("location") or "—"
        header_end = draw_wrapped(draw, (4, 2), f"TIDE & SUN  {loc}",
                                  title_font, w - 8, line_spacing=0)
        underline_y = header_end + 1
        draw.line((4, underline_y, w - 4, underline_y), fill=0)
        content_top = underline_y + 4

        if not d:
            draw_wrapped(draw, (4, content_top),
                         "no data yet — waiting for phone to push location",
                         label_font, w - 8)
            return

        now = datetime.now(tz=timezone.utc)

        sunrise = _parse_iso(d.get("sunrise"))
        sunset = _parse_iso(d.get("sunset"))
        col1_x = 4
        col2_x = w // 2 + 4

        draw.text((col1_x, content_top), "RISE", font=label_font, fill=0)
        draw.text((col1_x, content_top + 12), _fmt_local(sunrise),
                  font=big, fill=0)

        draw.text((col2_x, content_top), "SET", font=label_font, fill=0)
        draw.text((col2_x, content_top + 12), _fmt_local(sunset),
                  font=big, fill=0)

        tides = d.get("tides") or []
        nxt = _next(tides, now)
        tide_top = content_top + 32
        draw.text((col1_x, tide_top), "NEXT TIDE", font=label_font, fill=0)
        if nxt:
            ttype = (nxt.get("type") or "?").upper()
            tat = _parse_iso(nxt.get("at"))
            tline = f"{ttype[:4]} {_fmt_local(tat)}"
            height = nxt.get("height_m")
            if height is not None:
                tline += f"  {height:.1f}m"
            draw.text((col1_x, tide_top + 12), tline, font=big, fill=0)
        else:
            draw.text((col1_x, tide_top + 12), "—", font=big, fill=0)

        moon = d.get("moon") or {}
        phase = moon.get("phase") or ""
        illum = moon.get("illum")
        moon_line = phase
        if isinstance(illum, (int, float)):
            moon_line = f"{phase} {int(illum * 100)}%"
        moon_top = h - 22
        draw.text((col1_x, moon_top), "MOON", font=label_font, fill=0)
        draw_wrapped(draw, (col1_x, moon_top + 11), moon_line,
                     small, w - 8, line_spacing=0)
