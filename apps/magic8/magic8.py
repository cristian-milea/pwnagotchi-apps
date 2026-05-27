# Magic 8-Ball.
#
# Phone pushes {"action": "shake"} (no question text). App picks a random
# answer, wraps it into the centre of the screen, and shows:
#   - the time of the last shake in the corner
#   - a lifetime shake counter, bottom-left
#   - a small toggle glyph (filled vs hollow square) that flips on every
#     shake, so the screen visibly changes even when the same answer is
#     drawn twice in a row
#
# Push schema:
#   POST /plugins/ink-cartridge/push
#   {"app": "magic8", "payload": {"action": "shake"}}
#
# Persistence: the lifetime counter is kept in /etc/pwnagotchi/magic8.state.json
# so it survives restarts. Written atomically (tmp + rename).

import json
import os
import random
from datetime import datetime

from PIL import ImageFont

from ink_cartridge_host import wrap_text


STATE_PATH = "/etc/pwnagotchi/magic8.state.json"

ANSWERS = [
    "It is certain.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy.",
    "Ask again later.",
    "Cannot predict now.",
    "Concentrate and ask.",
    "Don't count on it.",
    "My reply is no.",
    "Very doubtful.",
    "Outlook not so good.",
]


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


class Magic8:
    name = "magic8"
    icon = "8"
    version = "1.3.0"

    def __init__(self):
        self._answer = None
        self._at = None
        state = _load_state(STATE_PATH)
        self._count = int(state.get("count") or 0)
        self._toggle = bool(state.get("toggle") or False)

    def on_data(self, payload):
        action = (payload or {}).get("action")
        if action != "shake":
            return False
        self._answer = random.choice(ANSWERS)
        self._at = datetime.now().strftime("%H:%M")
        self._count += 1
        self._toggle = not self._toggle
        _save_state(STATE_PATH, {"count": self._count, "toggle": self._toggle})
        return True

    def published_state(self):
        return {
            "last_answer":   self._answer or "",
            "shake_count":   self._count,
            "last_shake_at": self._at or "",
            "toggle":        self._toggle,
        }

    def render(self, draw, w, h):
        title_font = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        ans_font = ImageFont.truetype("DejaVuSansMono-Bold", 18)
        small = ImageFont.truetype("DejaVuSansMono-Bold", 9)

        draw.text((4, 2), "MAGIC 8-BALL", font=title_font, fill=0)
        draw.line((4, 14, w - 4, 14), fill=0)

        # Reserve a footer band for counter + toggle + last-shake time.
        footer_top = h - 12
        msg = self._answer or "shake the phone to ask"
        # Vertically centre the wrapped answer between the title underline
        # and the footer.
        body_top = 16
        body_area = (4, body_top, w - 4, footer_top - 2)
        lines = wrap_text(msg, w - 8, draw, ans_font)
        if not lines:
            lines = [""]
        ascent, descent = ans_font.getmetrics()
        line_h = ascent + descent + 1
        total_h = line_h * len(lines)
        y = body_top + max(0, ((body_area[3] - body_area[1]) - total_h) // 2)
        for line in lines:
            lw = draw.textlength(line, font=ans_font)
            draw.text(((w - int(lw)) // 2, y), line, font=ans_font, fill=0)
            y += line_h

        # Footer: lifetime counter (left), toggle glyph (centre-left), time
        # of last shake (right). The toggle's filled-vs-hollow box gives a
        # one-pixel visual change every shake even if the answer repeats.
        counter = f"#{self._count}"
        draw.text((4, footer_top + 1), counter, font=small, fill=0)
        cw = draw.textlength(counter, font=small)
        glyph_x = 4 + int(cw) + 6
        # 8x8 box, filled when toggle is on, hollow when off.
        box = (glyph_x, footer_top + 2, glyph_x + 7, footer_top + 9)
        if self._toggle:
            draw.rectangle(box, fill=0)
        else:
            draw.rectangle(box, outline=0)

        if self._at:
            tw = draw.textlength(self._at, font=small)
            draw.text((w - int(tw) - 4, footer_top + 1), self._at,
                      font=small, fill=0)
