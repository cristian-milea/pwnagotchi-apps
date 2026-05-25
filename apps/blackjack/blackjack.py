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
import os
import random

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
    version = "1.0.1"

    # Phase strings: "idle", "player", "dealer", "done"
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
        self._phase = "dealer"
        self._status = "Dealer plays"
        # Dealer hits to 17 (stands on all 17s including soft).
        while True:
            total = _hand_total(self._dealer)
            if total >= 17:
                break
            self._deal_card(self._dealer)
        self._phase = "done"
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
        if action == "deal":
            if self._phase in ("idle", "done"):
                # bets_on comes from persisted state (set by the switch's
                # set_bets_on action). Don't read it from the deal payload —
                # the phone serializes template values as strings, and
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

    # ---- e-ink rendering ----
    def render(self, draw, w, h):
        title = ImageFont.truetype("DejaVuSansMono-Bold", 10)
        small = ImageFont.truetype("DejaVuSansMono-Bold", 8)
        label = ImageFont.truetype("DejaVuSansMono-Bold", 9)

        # Title bar
        draw.text((2, 1), "BLACKJACK", font=title, fill=0)
        status = self._status or ""
        sw = draw.textlength(status, font=small)
        draw.text(((w - int(sw)) // 2, 3), status, font=small, fill=0)
        if self._bets_on:
            bank = f"${self._bank}"
            bw = draw.textlength(bank, font=small)
            draw.text((w - int(bw) - 2, 3), bank, font=small, fill=0)
        draw.line((2, 12, w - 2, 12), fill=0)

        # Footer band (bet/result + hand count)
        footer_top = h - 10
        draw.line((2, footer_top - 1, w - 2, footer_top - 1), fill=0)
        if self._bets_on and self._phase != "idle":
            left = f"Bet ${self._bet}" if self._bet else self._last_result
        else:
            left = self._last_result or f"W{self._wins} L{self._losses} P{self._pushes}"
        draw.text((2, footer_top), left, font=small, fill=0)
        right = f"#{self._hands}"
        rw = draw.textlength(right, font=small)
        draw.text((w - int(rw) - 2, footer_top), right, font=small, fill=0)

        # Card area split in two halves between title underline and footer line.
        area_top = 14
        area_bot = footer_top - 2
        half = (area_bot - area_top) // 2
        dealer_y = area_top
        player_y = area_top + half

        # Hide dealer hole card (and total) while the player is still acting.
        hide_hole = (self._phase == "player")
        d_total = _hand_total(self._dealer) if self._dealer else 0
        p_total = _hand_total(self._player) if self._player else 0

        self._draw_row(draw, "Dealer", d_total, self._dealer,
                       dealer_y, half, w, label,
                       hide_index=(1 if hide_hole else None),
                       hide_total=hide_hole)
        self._draw_row(draw, "You", p_total, self._player,
                       player_y, half, w, label,
                       hide_index=None, hide_total=False)

    def _draw_row(self, draw, who, total, cards, y, h, w, font,
                  hide_index, hide_total):
        # Label line ("Dealer: 17"), then a row of card glyphs underneath.
        # While the hole card is hidden we don't show a number — the visible
        # up-card alone isn't the hand total, and showing it would mislead.
        if not cards:
            label_text = who
        elif hide_total:
            label_text = who
        else:
            label_text = f"{who}: {total}"
        draw.text((2, y), label_text, font=font, fill=0)

        if not cards:
            return

        # Card size and layout
        card_w, card_h = 20, 26
        gap = 3
        cards_left = 64
        max_cards = max(1, (w - cards_left - 2) // (card_w + gap))
        visible = cards[:max_cards]
        for i, (rank, suit) in enumerate(visible):
            cx = cards_left + i * (card_w + gap)
            cy = y + (h - card_h) // 2
            if hide_index is not None and i == hide_index:
                self._draw_card_back(draw, cx, cy, card_w, card_h)
            else:
                self._draw_card(draw, cx, cy, card_w, card_h, rank, suit)
        # Overflow indicator
        if len(cards) > max_cards:
            more = f"+{len(cards) - max_cards}"
            mx = cards_left + max_cards * (card_w + gap)
            draw.text((mx, y + h // 2 - 4), more, font=font, fill=0)

    def _draw_card(self, draw, x, y, cw, ch, rank, suit):
        # Outline
        draw.rectangle((x, y, x + cw - 1, y + ch - 1), outline=0, fill=1)
        rank_font = ImageFont.truetype("DejaVuSansMono-Bold", 9)
        suit_font = ImageFont.truetype("DejaVuSansMono-Bold", 11)
        # Rank top-left (use "10" or first char)
        r_text = rank if rank != "10" else "10"
        draw.text((x + 2, y + 1), r_text, font=rank_font, fill=0)
        # Suit centered
        glyph = SUIT_GLYPH.get(suit, suit)
        gw = draw.textlength(glyph, font=suit_font)
        draw.text((x + (cw - int(gw)) // 2, y + ch - 14), glyph,
                  font=suit_font, fill=0)

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
