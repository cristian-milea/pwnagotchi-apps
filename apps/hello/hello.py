# Hello — the minimal working app. Copy this file to start a new app.
#
# Demonstrates: required attributes, optional on_data, basic Pillow rendering,
# and the shared wrap_text helper from the host plugin.
#
# Try it:
#   POST /plugins/pwn-apps/push  {"app": "hello", "payload": {"text": "hi"}}

from PIL import ImageFont

from pwn_apps_host import draw_wrapped_centered


class Hello:
    name = "hello"
    icon = "Hi"
    version = "1.1.0"

    def __init__(self):
        self._text = None

    def on_data(self, payload):
        self._text = (payload or {}).get("text")

    def render(self, draw, w, h):
        title = ImageFont.truetype("DejaVuSansMono-Bold", 12)
        big = ImageFont.truetype("DejaVuSansMono-Bold", 22)

        draw.text((4, 2), "HELLO, WORLD", font=title, fill=0)
        draw.line((4, 16, w - 4, 16), fill=0)

        msg = self._text or "push some text to me"
        draw_wrapped_centered(draw, msg, big, area=(4, 18, w - 4, h - 2))
