# Weather + rain forecast.
#
# Phone fetches the URL declared in manifest.json's `data_source` and POSTs:
#   {"location": {"lat":.., "lon":.., "label":..} | null,
#    "fetched":  <raw external API response> | null}
# This app expects the Open-Meteo forecast response under `fetched`. It does
# the transformation here so the Android app stays agnostic of weather APIs.

from datetime import datetime, timezone

from PIL import ImageFont

from pwn_apps_host import draw_wrapped


# WMO weather code → short human phrase. From the Open-Meteo docs.
WMO_CODES = {
    0: "Clear",
    1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains",
    80: "Rain showers", 81: "Rain showers", 82: "Violent showers",
    85: "Snow showers", 86: "Snow showers",
    95: "Thunderstorm", 96: "Thunderstorm w/ hail", 99: "Thunderstorm w/ hail",
}


def _parse_iso(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _normalise_iso(raw, utc_offset_seconds):
    # Open-Meteo returns times in the requested timezone without a UTC suffix
    # (e.g. "2026-05-24T09:30"). Append the offset so downstream parsers can
    # interpret it correctly.
    if not raw:
        return raw
    if raw.endswith("Z") or "+" in raw or len(raw) >= 6 and raw[-6] == "-" and raw[-3] == ":":
        return raw
    offset = utc_offset_seconds or 0
    sign = "+" if offset >= 0 else "-"
    a = abs(offset)
    return "%s%s%02d:%02d" % (raw, sign, a // 3600, (a % 3600) // 60)


def _transform(envelope):
    """Open-Meteo envelope → flat display dict."""
    if not isinstance(envelope, dict):
        return {"location": ""}
    raw_loc = envelope.get("location")
    loc = raw_loc if isinstance(raw_loc, dict) else {}
    fetched = envelope.get("fetched")
    if not isinstance(fetched, dict):
        return {"location": loc.get("label") or ""}

    offset = fetched.get("utc_offset_seconds")
    current = fetched.get("current") or {}
    hourly = fetched.get("hourly") or {}

    rain_hours = []
    times = hourly.get("time") or []
    precip = hourly.get("precipitation") or []
    for i, t in enumerate(times):
        if i >= len(precip):
            break
        mm = precip[i]
        if mm is None:
            continue
        rain_hours.append({"t": _normalise_iso(t, offset), "mm": mm})

    code = current.get("weather_code")
    return {
        "location":   loc.get("label") or "",
        "temp_c":     current.get("temperature_2m"),
        "feels_c":    current.get("apparent_temperature"),
        "condition":  WMO_CODES.get(code, "") if code is not None else "",
        "updated":    _normalise_iso(current.get("time"), offset),
        "rain_hours": rain_hours,
        "aqi":        None,  # Open-Meteo forecast endpoint doesn't include AQI.
    }


class Weather:
    name = "weather"
    icon = "W"
    version = "1.4.0"
    interval_seconds = 300

    def __init__(self):
        self._data = None

    def published_state(self):
        d = self._data or {}
        return {
            "location":  d.get("location") or "",
            "temp_c":    d.get("temp_c"),
            "feels_c":   d.get("feels_c"),
            "condition": d.get("condition") or "",
            "aqi":       d.get("aqi"),
            "updated":   d.get("updated") or "",
        }

    def on_data(self, payload):
        # Phone always sends the sync envelope:
        #   {"location": {"lat":.., "lon":.., "label":..} | null,
        #    "fetched":  <raw Open-Meteo response> | null}
        self._data = _transform(payload) if isinstance(payload, dict) else None

    def render(self, draw, w, h):
        title_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        label_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        huge = ImageFont.truetype("DejaVuSansMono-Bold", 28)
        small = ImageFont.truetype("DejaVuSansMono-Bold", 9)

        d = self._data or {}
        loc = d.get("location") or "—"
        header_end = draw_wrapped(draw, (4, 2), f"WEATHER  {loc}",
                                  title_font, w - 8, line_spacing=0)
        underline_y = header_end + 1
        draw.line((4, underline_y, w - 4, underline_y), fill=0)
        content_top = underline_y + 4

        if not d:
            draw_wrapped(draw, (4, content_top), "no data yet",
                         label_font, w - 8)
            return

        temp = d.get("temp_c")
        temp_str = f"{temp:.0f}°" if isinstance(temp, (int, float)) else "--"
        draw.text((4, content_top), temp_str, font=huge, fill=0)

        info_x = 80
        info_w = w - info_x - 4
        cond = d.get("condition") or ""
        cond_end = draw_wrapped(draw, (info_x, content_top + 2), cond,
                                label_font, info_w, line_spacing=0)
        feels = d.get("feels_c")
        if isinstance(feels, (int, float)):
            draw.text((info_x, cond_end + 2), f"feels {feels:.0f}°",
                      font=label_font, fill=0)
            cond_end += 12
        aqi = d.get("aqi")
        if isinstance(aqi, (int, float)):
            draw.text((info_x, cond_end + 2), f"AQI {int(aqi)}",
                      font=label_font, fill=0)

        rain = d.get("rain_hours") or []
        chart_y0 = h - 26
        chart_y1 = h - 12
        chart_x0 = 4
        chart_x1 = w - 4
        draw.text((chart_x0, chart_y0 - 11), "RAIN (next hrs)",
                  font=label_font, fill=0)
        draw.line((chart_x0, chart_y1, chart_x1, chart_y1), fill=0)

        if rain:
            n = min(len(rain), 24)
            band_w = max(2, (chart_x1 - chart_x0) // n)
            mm_max = max((r.get("mm") or 0) for r in rain[:n]) or 1.0
            mm_max = max(mm_max, 1.0)
            height = chart_y1 - chart_y0
            for i, hour in enumerate(rain[:n]):
                mm = hour.get("mm") or 0
                bh = int(height * min(1.0, mm / mm_max))
                bx0 = chart_x0 + i * band_w
                bx1 = bx0 + band_w - 1
                if bh > 0:
                    draw.rectangle((bx0, chart_y1 - bh, bx1, chart_y1), fill=0)

            # Hour labels at every other bar so they're spaced but legible.
            # Use the timestamp's own TZ (preserved by _normalise_iso) — do
            # NOT astimezone() to the host's TZ, which on a UTC-only pi
            # shifted London-local hours into UTC and made the graph look
            # 1-5h offset from the wall clock.
            label_y = h - 11
            for i, hour in enumerate(rain[:n]):
                if i % 2 != 0:
                    continue
                t = _parse_iso(hour.get("t"))
                if not t:
                    continue
                label = t.strftime("%H")
                lx = chart_x0 + i * band_w
                lw = int(draw.textlength(label, font=small))
                if lx + lw > chart_x1:
                    lx = chart_x1 - lw
                draw.text((lx, label_y), label, font=small, fill=0)
        else:
            draw.text((chart_x0, chart_y0), "no rain data", font=small, fill=0)
