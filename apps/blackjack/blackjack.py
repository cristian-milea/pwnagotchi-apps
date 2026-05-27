# Blackjack 21 — single-deck, dealer stands on all 17s, no split/insurance.
#
# Modes:
#   - Casual (default): just Deal/Hit/Stand, lifetime W/L/P counters.
#   - Chips:            adds bank + bet + Double; bank persisted.
#
# Push schema:
#   {"action": "deal",         "bet": 25, "bets_on": true}
#   {"action": "hit"}
#   {"action": "stand"}
#   {"action": "double"}        (legal only on first move, bets on)
#   {"action": "reset_bank"}
#   {"action": "set_bets_on",  "value": true}
#
# State on disk: /etc/pwnagotchi/blackjack.state.json
#   {"bank": 100, "wins": 0, "losses": 0, "pushes": 0,
#    "hands": 0, "bets_on": false}
# Mid-hand state is RAM-only — power loss aborts the hand, bank survives.

import json
import logging
import os
import random
import threading

from PIL import ImageFont


STATE_PATH = "/etc/pwnagotchi/blackjack.state.json"
START_BANK = 100

SUITS = ["S", "H", "D", "C"]  # internal — render maps to glyphs
SUIT_GLYPH = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]


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


def _card_value(rank):
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def _hand_total(cards):
    # Returns best total <=21, downgrading aces from 11 to 1 as needed.
    total = sum(_card_value(r) for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def _is_soft(cards):
    total = sum(_card_value(r) for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
        if aces == 0:
            return False
    return aces > 0 and total <= 21


def _fresh_deck():
    deck = [(r, s) for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


class Blackjack:
    name = "blackjack"
    icon = "BJ"
    version = "1.1.1"

    # Phase strings: "idle", "player", "dealer", "done"
    #
    # interval_seconds: host re-renders every N seconds while set. We use this
    # to animate the dealer drawing one card per tick — the rest of the game
    # is push-driven (interval=None).
    interval_seconds = None
    DEALER_TICK = 1.5

    def __init__(self):
        s = _load_state(STATE_PATH)
        self._bank = int(s.get("bank", START_BANK))
        self._wins = int(s.get("wins", 0))
        self._losses = int(s.get("losses", 0))
        self._pushes = int(s.get("pushes", 0))
        self._hands = int(s.get("hands", 0))
        self._bets_on = bool(s.get("bets_on", False))

        self._deck = []
        self._dealer = []
        self._player = []
        self._bet = 0
        self._phase = "idle"
        self._status = "Tap Deal"
        self._last_result = ""

        # on_data runs on a request thread; render + _dealer_step run on the
        # ink-cartridge render thread. Lock anything that touches game state so a
        # mid-render mutation can't hand a half-built list to PIL.
        self._lock = threading.RLock()

        # Font cache — re-creating ImageFont.truetype on every paint adds up
        # fast during the 1Hz dealer animation and was implicated in the
        # silent-crash report on device.
        self._fonts = {}

    # ---- persistence ----
    def _persist(self):
        _save_state(STATE_PATH, {
            "bank": self._bank,
            "wins": self._wins,
            "losses": self._losses,
            "pushes": self._pushes,
            "hands": self._hands,
            "bets_on": self._bets_on,
        })

    # ---- game flow ----
    def _deal_card(self, hand):
        if not self._deck:
            self._deck = _fresh_deck()
        hand.append(self._deck.pop())

    def _start_hand(self, bet, bets_on):
        self._bets_on = bool(bets_on)
        self._deck = _fresh_deck()
        self._dealer = []
        self._player = []
        if self._bets_on:
            try:
                bet = int(bet or 0)
            except (ValueError, TypeError):
                bet = 0
            if bet <= 0:
                self._status = "Pick a bet"
                return
            if bet > self._bank:
                self._status = "Insufficient chips"
                return
            self._bet = bet
            self._bank -= bet  # escrow the wager
        else:
            self._bet = 0

        self._deal_card(self._player)
        self._deal_card(self._dealer)
        self._deal_card(self._player)
        self._deal_card(self._dealer)
        self._hands += 1
        self._phase = "player"

        p_total = _hand_total(self._player)
        d_total = _hand_total(self._dealer)
        if p_total == 21 or d_total == 21:
            # Naturals resolve immediately.
            self._phase = "done"
            if p_total == 21 and d_total == 21:
                self._settle("push")
            elif p_total == 21:
                self._settle("blackjack")
            else:
                self._settle("dealer_blackjack")
        else:
            self._status = "Your turn"
        self._persist()

    def _player_hit(self):
        if self._phase != "player":
            return
        self._deal_card(self._player)
        total = _hand_total(self._player)
        if total > 21:
            self._phase = "done"
            self._settle("bust")
        elif total == 21:
            self._stand()
        else:
            self._status = "Your turn"

    def _player_double(self):
        if self._phase != "player":
            return
        if not self._bets_on:
            return
        if len(self._player) != 2:
            return
        if self._bet > self._bank:
            self._status = "Not enough chips to double"
            return
        self._bank -= self._bet
        self._bet *= 2
        self._deal_card(self._player)
        if _hand_total(self._player) > 21:
            self._phase = "done"
            self._settle("bust")
        else:
            self._stand()

    def _stand(self):
        # Enter dealer phase — actual draws happen one-per-render-tick in
        # _dealer_step so the user sees the dealer flip and draw on the e-ink.
        self._phase = "dealer"
        self._status = "Dealer plays"
        self.interval_seconds = self.DEALER_TICK

    def _dealer_step(self):
        # Called from render() while phase=="dealer". Draws one card per tick;
        # when the dealer is done (>=17 or busted), settles and clears the
        # tick so the host goes back to push-driven painting.
        if self._phase != "dealer":
            return
        total = _hand_total(self._dealer)
        if total < 17:
            self._deal_card(self._dealer)
            return
        # Done drawing — resolve.
        self._phase = "done"
        self.interval_seconds = None
        d_total = _hand_total(self._dealer)
        p_total = _hand_total(self._player)
        if d_total > 21 or p_total > d_total:
            self._settle("win")
        elif p_total < d_total:
            self._settle("lose")
        else:
            self._settle("push")

    def _settle(self, kind):
        if kind == "blackjack":
            payout = self._bet + int(self._bet * 1.5)  # 3:2
            self._bank += payout
            self._wins += 1
            self._status = "Blackjack!"
            self._last_result = f"Won ${payout - self._bet}" if self._bets_on else "Blackjack"
        elif kind == "win":
            payout = self._bet * 2
            self._bank += payout
            self._wins += 1
            self._status = "You win"
            self._last_result = f"Won ${self._bet}" if self._bets_on else "Win"
        elif kind == "push":
            self._bank += self._bet
            self._pushes += 1
            self._status = "Push"
            self._last_result = "Push"
        elif kind == "bust":
            self._losses += 1
            self._status = "Bust"
            self._last_result = f"Lost ${self._bet}" if self._bets_on else "Bust"
        elif kind == "lose":
            self._losses += 1
            self._status = "Dealer wins"
            self._last_result = f"Lost ${self._bet}" if self._bets_on else "Loss"
        elif kind == "dealer_blackjack":
            self._losses += 1
            self._status = "Dealer blackjack"
            self._last_result = f"Lost ${self._bet}" if self._bets_on else "Loss"
        self._persist()

    # ---- host hooks ----
    def on_data(self, payload):
        action = (payload or {}).get("action")
        with self._lock:
            if action == "deal":
                if self._phase in ("idle", "done"):
                    # bets_on comes from persisted state (set by the switch's
                    # set_bets_on action). Don't read it from the deal payload
                    # — the phone serializes template values as strings, and
                    # bool("false") is True, so a payload-driven flag would
                    # ignore the toggle.
                    self._start_hand(payload.get("bet", 0), self._bets_on)
                return True
            if action == "hit":
                self._player_hit()
                return True
            if action == "stand":
                if self._phase == "player":
                    self._stand()
                return True
            if action == "double":
                self._player_double()
                return True
            if action == "reset_bank":
                self._bank = START_BANK
                self._persist()
                return True
            if action == "set_bets_on":
                self._bets_on = bool(payload.get("value", False))
                self._persist()
                return True
        return False

    def published_state(self):
        return {
            "status": self._status,
            "bank": self._bank,
            "bet": self._bet,
            "bets_on": self._bets_on,
            "last_result": self._last_result,
            "wins": self._wins,
            "losses": self._losses,
            "pushes": self._pushes,
            "hands": self._hands,
        }

    def _font(self, size):
        f = self._fonts.get(size)
        if f is None:
            f = ImageFont.truetype("DejaVuSansMono-Bold", size)
            self._fonts[size] = f
        return f

    # ---- e-ink rendering ----
    def render(self, draw, w, h):
        # Advance dealer animation, then snapshot everything we need to draw
        # under the lock. After the snapshot we touch only locals, so an
        # on_data call from another thread can't mutate state mid-paint.
        with self._lock:
            if self._phase == "dealer":
                try:
                    self._dealer_step()
                except Exception:
                    # A render-time bug must not wedge the dealer phase
                    # forever. Force back to "done", clear the tick, log, and
                    # paint the current state.
                    logging.exception("blackjack: dealer step failed")
                    self._phase = "done"
                    self._status = "Error — tap Deal"
                    self.interval_seconds = None
            phase = self._phase
            status = self._status
            bets_on = self._bets_on
            bank = self._bank
            bet = self._bet
            last_result = self._last_result
            wins, losses, pushes, hands = (
                self._wins, self._losses, self._pushes, self._hands
            )
            dealer = list(self._dealer)
            player = list(self._player)

        title = self._font(10)
        small = self._font(8)
        name_font = self._font(10)
        total_font = self._font(16)

        # Title bar — name left, status right-aligned. Bank moves to the
        # footer so the top bar stays uncluttered.
        draw.text((2, 1), "BLACKJACK", font=title, fill=0)
        status_text = status or ""
        if status_text:
            sw = draw.textlength(status_text, font=small)
            draw.text((w - int(sw) - 2, 3), status_text, font=small, fill=0)
        draw.line((2, 12, w - 2, 12), fill=0)

        # Footer band (bet · bank when betting, else last result / stats; #hands)
        footer_top = h - 10
        draw.line((2, footer_top - 1, w - 2, footer_top - 1), fill=0)
        if bets_on:
            if phase in ("player", "dealer") and bet:
                left = f"Bet ${bet}  Bank ${bank}"
            else:
                left = f"Bank ${bank}"
                if last_result:
                    left = f"{last_result}  Bank ${bank}"
        else:
            left = last_result or f"W{wins} L{losses} P{pushes}"
        draw.text((2, footer_top), left, font=small, fill=0)
        right = f"#{hands}"
        rw = draw.textlength(right, font=small)
        draw.text((w - int(rw) - 2, footer_top), right, font=small, fill=0)

        # Card area split in two halves between title underline and footer line.
        area_top = 14
        area_bot = footer_top - 2
        half = (area_bot - area_top) // 2
        dealer_y = area_top
        player_y = area_top + half

        # Hide dealer hole card (and total) while the player is still acting.
        hide_hole = (phase == "player")
        d_total = _hand_total(dealer) if dealer else 0
        p_total = _hand_total(player) if player else 0

        # Winner gets their total inverted (black box, white digit) once the
        # hand is resolved. Push leaves both un-inverted.
        d_win = p_win = False
        if phase == "done" and dealer and player:
            if p_total > 21:
                d_win = True
            elif d_total > 21 or p_total > d_total:
                p_win = True
            elif d_total > p_total:
                d_win = True

        self._draw_row(draw, "DLR", d_total, dealer,
                       dealer_y, half, w, name_font, total_font,
                       hide_index=(1 if hide_hole else None),
                       hide_total=hide_hole, invert_total=d_win)
        self._draw_row(draw, "YOU", p_total, player,
                       player_y, half, w, name_font, total_font,
                       hide_index=None, hide_total=False, invert_total=p_win)

    def _draw_row(self, draw, who, total, cards, y, h, w, name_font,
                  total_font, hide_index, hide_total, invert_total):
        # Stacked label column on the left: short name on top, big total below.
        # Total is hidden while the dealer's hole card is still down; if the
        # hand resolved in this side's favour we invert the digit so the
        # winner is immediately readable from across the room.
        draw.text((2, y + 1), who, font=name_font, fill=0)
        show_total = cards and not hide_total
        if show_total:
            t_text = str(total)
            t_x, t_y = 2, y + 14
            tw = int(draw.textlength(t_text, font=total_font))
            ascent, descent = total_font.getmetrics()
            th = ascent + descent
            if invert_total:
                draw.rectangle(
                    (t_x - 2, t_y - 1, t_x + tw + 1, t_y + th - 1),
                    fill=0,
                )
                draw.text((t_x, t_y), t_text, font=total_font, fill=1)
            else:
                draw.text((t_x, t_y), t_text, font=total_font, fill=0)

        if not cards:
            return

        # Card layout. If every card fits at full width, lay them out with a
        # small gap. Otherwise overlap: the last card stays full-width and
        # the earlier ones each show only a thin left strip (rank + suit
        # stacked top-left), as if peeking out from under the next card.
        card_w, card_h = 26, 34
        gap = 3
        cards_left = 30  # 3-char label fits in ~22 px
        right_pad = 2
        available = w - cards_left - right_pad
        n = len(cards)

        full_width_needed = n * card_w + max(0, n - 1) * gap
        if full_width_needed <= available or n <= 1:
            step = card_w + gap
        else:
            # Compact mode: distribute the slack across the n-1 covered cards.
            # min_step keeps the rank + suit corner readable even when crowded.
            min_step = 8
            step = max(min_step, (available - card_w) // (n - 1))

        # Failsafe: if even the compact overlap can't squeeze every card into
        # the available width, fall back to a plain text list ("K♠ A♥ 5♦ …").
        # In a single deck this is unreachable in practice (max ~12 cards in a
        # blackjack hand) but the renderer should never draw past the panel.
        if (n - 1) * step + card_w > available:
            self._draw_card_list(draw, cards_left, y, h, available,
                                 cards, hide_index)
            return

        cy = y + (h - card_h) // 2
        for i, (rank, suit) in enumerate(cards):
            cx = cards_left + i * step
            is_covered = (i < n - 1) and (step < card_w + gap)
            if hide_index is not None and i == hide_index:
                self._draw_card_back(draw, cx, cy, card_w, card_h)
            elif is_covered:
                self._draw_card_compact(draw, cx, cy, card_w, card_h,
                                        rank, suit)
            else:
                self._draw_card(draw, cx, cy, card_w, card_h, rank, suit)

    def _draw_card_list(self, draw, x, y, h, available, cards, hide_index):
        # Compact text fallback for absurd hand sizes. Renders cards as
        # "K♠ A♥ 5♦ …"; the hole card (when hidden) becomes "??". If even
        # the text overflows, truncates and appends "+N".
        font = self._font(10)
        parts = []
        for i, (rank, suit) in enumerate(cards):
            if hide_index is not None and i == hide_index:
                parts.append("??")
            else:
                parts.append(f"{rank}{SUIT_GLYPH.get(suit, suit)}")

        text = " ".join(parts)
        if int(draw.textlength(text, font=font)) <= available:
            text_y = y + (h - 12) // 2
            draw.text((x, text_y), text, font=font, fill=0)
            return

        # Truncate from the head, keeping the most recent cards visible, until
        # the prefix + " +N" suffix fits.
        kept = list(parts)
        dropped = 0
        while kept:
            suffix = f"+{dropped}" if dropped else ""
            candidate = (suffix + " " if suffix else "") + " ".join(kept)
            if int(draw.textlength(candidate, font=font)) <= available:
                text_y = y + (h - 12) // 2
                draw.text((x, text_y), candidate, font=font, fill=0)
                return
            kept.pop(0)
            dropped += 1
        # Worst case (no card fits at all) — show just the count.
        text_y = y + (h - 12) // 2
        draw.text((x, text_y), f"{len(cards)} cards",
                  font=font, fill=0)

    def _draw_card(self, draw, x, y, cw, ch, rank, suit):
        draw.rectangle((x, y, x + cw - 1, y + ch - 1), outline=0, fill=1)
        rank_font = self._font(12)
        suit_font = self._font(14)
        draw.text((x + 2, y + 1), rank, font=rank_font, fill=0)
        glyph = SUIT_GLYPH.get(suit, suit)
        gw = draw.textlength(glyph, font=suit_font)
        draw.text((x + (cw - int(gw)) // 2, y + ch - 17), glyph,
                  font=suit_font, fill=0)

    def _draw_card_compact(self, draw, x, y, cw, ch, rank, suit):
        # Drawn full-size; the next card painted to the right of us will
        # overwrite our right portion (its fill=1 background covers us). All
        # we need to render is what shows in the visible left strip: rank on
        # top, suit underneath.
        draw.rectangle((x, y, x + cw - 1, y + ch - 1), outline=0, fill=1)
        rank_font = self._font(10)
        suit_font = self._font(10)
        draw.text((x + 1, y + 1), rank, font=rank_font, fill=0)
        glyph = SUIT_GLYPH.get(suit, suit)
        draw.text((x + 1, y + 12), glyph, font=suit_font, fill=0)

    def _draw_card_back(self, draw, x, y, cw, ch):
        draw.rectangle((x, y, x + cw - 1, y + ch - 1), outline=0, fill=1)
        # Diagonal hatch
        step = 3
        for off in range(-ch, cw, step):
            x0 = x + off
            y0 = y
            x1 = x + off + ch
            y1 = y + ch
            # Clip to card box
            draw.line((max(x0, x), y0 + max(0, x - x0),
                       min(x1, x + cw - 1), y1 - max(0, x1 - (x + cw - 1))),
                      fill=0)
