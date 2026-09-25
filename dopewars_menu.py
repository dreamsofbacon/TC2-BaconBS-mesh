"""Numbered menus over the trading door, in Candy Wars or Dope Wars words.

The engine (dopewars.py) is played by typed commands -- "B weed 2" -- and
every move redrew the whole market, two Meshtastic packets a turn. This layer
turns it into numbered screens that each fit one packet, and translates a
choice into the engine command it stands for. The engine is untouched: its
rules, its saves and Materva's tests carry on exactly as they were.

Shortcuts cost nothing extra. A reply is read one word at a time, each word
answering the screen the previous one opened, so "1 2 5" from the main
screen is Buy -> item 2 -> five of them, and "1 2 M" buys as many as you can.
A newcomer can take it one screen at a time; nobody else has to.

Every line a player reads is written here from the saved state and the
theme -- never the engine's reply text -- so the engine's own words
("Police stop!", "Unknown item: weed...") cannot reach a Candy Wars player.
"""

import dopewars as game
import dopewars_door as door
from dopewars_theme import theme

EXIT_WORDS = {'x', '!x', 'q', 'quit', 'exit'}

# One Meshtastic packet of text. A screen over this spends a second
# packet of airtime on every turn that shows it.
MAX_SCREEN_BYTES = 200

# The market row is "$price/stock", plus "+n" for what is already in
# the bag, and the footer says so: unlabelled, the number after the
# slash was read as the bag, against a bag the status line had just
# called full.
_FOOTER = "$/stock +bag [0]Back"
_FOOTER_MORE = "$/stock +bag [M]ore [0]Back"

# Ten goods do not fit one packet, so the market pages. A page is as
# many rows as the packet holds rather than a fixed count -- the rows
# vary by a factor of two in width, and a fixed count would either
# waste half a screen or overrun it.
_PAGE_BUDGET = MAX_SCREEN_BYTES

# Mirrors the engine's own gear table (dopewars.command, 'equipment'). The
# menu checks these before asking, so a refusal is said in theme rather than
# in the engine's words; a test pins them to what the engine charges.
GEAR = (('bag', 900), ('vest', 1200), ('weapon', 1600), ('medkit', 250))


class _Stop(Exception):
    """A reply the menu cannot act on; carries the themed reason."""


# ── Rendering ───────────────────────────────────────────────────────────────

def _status(state, t) -> str:
    place = t['places'][state['place']]
    # "d30" rather than "by day 30": this line is on the most-seen screen,
    # and with six goods, an event line and a five-figure debt the long form
    # pushed it past one packet.
    owed = (f", {t['owed']} ${state['debt']} d{state['loan_due']}"
            if state['debt'] else "")
    carried = sum(state['inventory'].values())
    return (f"Day {state['day']}/{state['days']} {place}: ${state['cash']}{owed}, "
            f"{t['hp']} {state['hp']}, bag {carried}/{state['capacity']}")


def _max_buy(state, item) -> int:
    offer = state['market'][item]
    room = state['capacity'] - sum(state['inventory'].values())
    return max(0, min(offer['stock'], state['cash'] // offer['price'], room))


def _market_rows(state, t):
    """One row per good worth a keypress: on the shelf, or in the bag.

    Numbers are the good's place in the catalogue, not its place on the
    screen, so [4] is the same thing in every town and on every page --
    including on a page it is not currently printed on.
    """
    rows = []
    for index, item in enumerate(game.GOODS, start=1):
        offer, held = state['market'][item], state['inventory'][item]
        if not offer['stock'] and not held:
            continue
        icon = t.get('icons', {}).get(item, '')
        # A space after the icon: several emoji are drawn double-width
        # and a few carry a variation selector, and without it the
        # glyph lands on top of the first letter of the name.
        row = (f"[{index}]{icon} {t['goods'][item]} ${offer['price']}"
               f"/{offer['stock'] or 'out'}")
        if held:
            row += f" +{held}"
        rows.append(row)
    return rows


def _market_page(state, nav, t, budget=_PAGE_BUDGET):
    """(lines, next page's first row) for the market at nav['start']."""
    carried = sum(state['inventory'].values())
    head = f"Market: ${state['cash']} bag {carried}/{state['capacity']}"
    rows = _market_rows(state, t)
    if not rows:
        return [head, "Nothing on the shelf, nothing in your bag.",
                "[0]Back"], 0
    start = nav.get('start', 0)
    if not 0 <= start < len(rows):
        start = 0
    used = len(head.encode('utf-8'))
    reserve = len(_FOOTER_MORE.encode('utf-8')) + 1
    lines = [head]
    for row in rows[start:]:
        cost = len(row.encode('utf-8')) + 1
        # Always place one row, even a freakishly wide one: a page that
        # showed nothing could never be paged past.
        if len(lines) > 1 and used + cost + reserve > budget:
            break
        used += cost
        lines.append(row)
    shown = len(lines) - 1
    nxt = start + shown
    if nxt >= len(rows):
        nxt = 0
    lines.append(_FOOTER if nxt == 0 and start == 0 else _FOOTER_MORE)
    return lines, nxt


def _max_borrow(state) -> int:
    return max(0, min(game.LOAN_LIMIT - state['debt'], game.MAX_CASH - state['cash']))


def _others(state):
    return [p for p in game.PLACES if p != state['place']]


def render(state, nav, t, note='') -> str:
    lines = [note] if note else []
    phase = state['phase']

    if phase == 'ended':
        outcome = t['outcomes'].get(state['outcome'], state['outcome'])
        lines += [f"{t['title']} is over: {outcome}. Score {game.score(state)}.",
                  "[1]New 30-day run [2]New 365-day run [0]Exit"]
        return "\n".join(lines)

    if phase == 'police':
        lines += [f"{t.get('encounter_icon', '')} {t['encounter']} {t['hp']} {state['hp']}.",
                  f"[1]{t['fight']} [2]{t['run']} [3]{t['surrender']} [0]Exit"]
        return "\n".join(lines)

    menu = nav.get('menu', 'main')
    place = t['places'][state['place']]

    if menu == 'market':
        # One screen for both sides of the trade. Buy and Sell were two
        # lists of the same six goods, each showing one number, and you
        # had to guess from the main screen which one you wanted.
        lines += _market_page(state, nav, t,
                              MAX_SCREEN_BYTES - _note_cost(note))[0]
    elif menu == 'item':
        item = nav['item']
        offer, held = state['market'][item], state['inventory'][item]
        icon = t.get('icons', {}).get(item, '')
        room = state['capacity'] - sum(state['inventory'].values())
        lines.append(f"{icon} {t['goods'][item]} ${offer['price']}")
        lines.append(f"Shelf {offer['stock']}, bag {held}, room {room}.")
        lines.append("[1]Buy [2]Sell [0]Back")
    elif menu in ('buy_qty', 'sell_qty'):
        item = nav['item']
        most = (_max_buy(state, item) if menu == 'buy_qty'
                else state['inventory'][item])
        lines.append(f"How many {t['goods'][item]}? 1-{most}, M for max. [0]Back")
    elif menu == 'move':
        cost = f"{t['owed']} +5%" if state['debt'] else "no interest"
        lines.append(f"Go from {place} (a day passes, {cost}):")
        lines.append(" ".join(f"[{i}]{t['places'][p]}"
                              for i, p in enumerate(_others(state), start=1))
                     + " [0]Back")
    elif menu == 'gear':
        lines.append(f"Gear (${state['cash']}):")
        for index, (key, price) in enumerate(GEAR, start=1):
            name, blurb = t['gear'][key]
            lines.append(f"[{index}]{name} ${price} {blurb}")
        lines.append("[0]Back")
    elif menu == 'bag':
        goods = ", ".join(f"{t['goods'][k]} x{q}"
                          for k, q in state['inventory'].items() if q) or "nothing"
        owned = [t['gear'][k][0] for k in ('vest', 'weapon') if state[
            'armor' if k == 'vest' else 'weapon']]
        lines.append(f"Bag {sum(state['inventory'].values())}/{state['capacity']}: {goods}.")
        if owned:
            lines.append("Gear: " + ", ".join(owned) + ".")
        lines.append("[0]Back")
    elif menu == 'loan':
        owed = (f"{t['owed']} ${state['debt']} by day {state['loan_due']}"
                if state['debt'] else "nothing owed")
        lines.append(f"{t['loan']}: {owed}. Up to ${game.LOAN_LIMIT} in all.")
        lines.append("[1]Borrow [2]Pay back [0]Back. Or 1 500.")
    elif menu == 'loan_amt':
        if nav['op'] == 'borrow':
            most, verb = _max_borrow(state), 'borrow'
        else:
            most, verb = min(state['debt'], state['cash']), 'pay back'
        lines.append(f"How much to {verb}? 1-{most}, M for max. [0]Back")
    elif menu == 'confirm_end':
        lines.append("End the run now? What you carry sells at today's prices. "
                     "[Y]es [0]No")
    else:
        if not note:
            lines.append(t['title'])
        lines.append(_status(state, t))
        if state.get('event'):
            kind, item = state['event'].split(':', 1)
            icon = t.get(f'{kind}_icon', '')
            lines.append(f"{icon} " + t[kind].format(item=t['goods'][item]))
        lines.append(f"[1]Market [2]Travel [3]Bag [4]Gear [5]{t['loan']} "
                     "[6]End [0]Exit")
        if state['moves'] == 0 and state['days'] == 30:
            lines.append("[7]Make it a 365-day run")

    # One packet beats a few words, but only just: each screen gives up
    # its least useful text rather than spend a second packet of airtime.
    # On the trade screens that is the slash legend, on the main screen
    # the title. Neither fires on a screen reachable today; they are here
    # so a line that grows later costs a word, not an extra packet.
    screen = "\n".join(lines)
    if len(screen.encode('utf-8')) > MAX_SCREEN_BYTES:
        if lines[-1] == _FOOTER:
            lines = lines[:-1] + ["[0]Back"]
        elif lines[-1] == _FOOTER_MORE:
            # [M]ore stays: without it the later pages are unreachable.
            lines = lines[:-1] + ["[M]ore [0]Back"]
        elif not note and lines and lines[0] == t['title']:
            lines = lines[1:]
        screen = "\n".join(lines)
    return screen


def _note_cost(note):
    """Bytes a note above a screen takes, itself plus its newline."""
    return len(note.encode('utf-8')) + 1 if note else 0


# ── One word at a time ──────────────────────────────────────────────────────

class _Turn:
    """One reply's worth of moves against one saved run."""

    def __init__(self, user_id, short_name, t, state):
        self.user_id, self.short_name, self.t = user_id, short_name, t
        self.state = state
        self.notes = []
        self.last_reply = ''

    def act(self, engine_text):
        """Send one command to the engine and return (before, after)."""
        before, after, reply, _leave, _result = door.play_state(
            self.user_id, engine_text, self.short_name)
        self.state = after
        self.last_reply = reply
        return before, after

    def note(self, text):
        self.notes.append(text)


def _number(word, low, high) -> int:
    if not word.isdigit() or not low <= int(word) <= high:
        # Terse on purpose: this sits above a screen that is already at
        # the edge of one packet, and it matches the voice of the rows
        # it is explaining ("$/stock", "1-N, M for max").
        raise _Stop(f"Pick {low}-{high}." if high >= low
                    else "There is nothing to pick.")
    return int(word)


def _amount(word, most) -> int:
    if most <= 0:
        raise _Stop("There is none to use.")
    if word == 'm':
        return most
    return _number(word, 1, most)


def _step(turn, word, nav) -> dict:
    """Apply one word to the current screen; return the next screen."""
    state, t = turn.state, turn.t
    phase = state['phase']
    menu = nav.get('menu', 'main')

    if phase == 'ended':
        choice = _number(word, 1, 2)
        turn.act(f"new {30 if choice == 1 else 365}")
        turn.note(f"New {turn.state['days']}-day run.")
        return {'menu': 'main'}

    if phase == 'police':
        choice = _number(word, 1, 3)
        before, after = turn.act(('fight', 'run', 'surrender')[choice - 1])
        if after['phase'] == 'ended' and after['outcome'] == 'Defeated':
            turn.note(t['defeated'])
        elif choice == 3:
            turn.note(t['surrendered'].format(fine=before['cash'] // 4))
        elif after['phase'] != 'police':
            turn.note(t['fought_off'] if choice == 1 else t['escaped'])
        else:
            turn.note(t['hit'].format(n=before['hp'] - after['hp']))
        if after['phase'] == 'ended' and after['outcome'] != 'Defeated':
            turn.note("That was the last day.")
        return {'menu': 'main'}

    if menu == 'main':
        top = 7 if state['moves'] == 0 and state['days'] == 30 else 6
        # Letters for the two people reach for every turn, spelling what
        # the buttons say.
        if word in ('t', 'travel'):
            return {'menu': 'move'}
        if word in ('m', 'market'):
            return {'menu': 'market', 'start': 0}
        choice = _number(word, 1, top)
        if choice == 7:
            turn.act("new 365")
            turn.note("Now a 365-day run.")
            return {'menu': 'main'}
        return {'menu': {1: 'market', 2: 'move', 3: 'bag',
                         4: 'gear', 5: 'loan', 6: 'confirm_end'}[choice]}

    if word == '0':
        # One level up from every sub-screen.
        up = {'buy_qty': 'item', 'sell_qty': 'item', 'item': 'market',
              'loan_amt': 'loan'}
        back = up.get(menu, 'main')
        if back == 'item':
            return {'menu': 'item', 'item': nav['item']}
        return {'menu': back}

    if menu == 'market':
        # Travel from here too. Arriving lands on this screen, so
        # without T the way on would be 0 then T, every single day.
        if word in ('t', 'travel'):
            return {'menu': 'move'}
        if word in ('m', 'more'):
            return {'menu': 'market',
                    'start': _market_page(state, nav, t)[1]}
        item = list(game.GOODS)[_number(word, 1, len(game.GOODS)) - 1]
        return {'menu': 'item', 'item': item}

    if menu == 'item':
        item = nav['item']
        if _number(word, 1, 2) == 1:
            if not _max_buy(state, item):
                # Say which limit it is -- "can't buy any" left the player
                # to guess between an empty shelf, an empty wallet and a
                # full bag.
                if state['capacity'] <= sum(state['inventory'].values()):
                    raise _Stop("Your bag is full.")
                if not state['market'][item]['stock']:
                    raise _Stop(f"{t['goods'][item]} is sold out.")
                raise _Stop("Not enough money.")
            return {'menu': 'buy_qty', 'item': item}
        if not state['inventory'][item]:
            raise _Stop(f"You have no {t['goods'][item]}.")
        return {'menu': 'sell_qty', 'item': item}

    if menu in ('buy_qty', 'sell_qty'):
        item = nav['item']
        buying = menu == 'buy_qty'
        most = _max_buy(state, item) if buying else state['inventory'][item]
        qty = _amount(word, most)
        price = state['market'][item]['price']
        before, after = turn.act(f"{'buy' if buying else 'sell'} {item} {qty}")
        if after['moves'] == before['moves']:
            raise _Stop("That didn't work.")
        turn.note(f"{'Bought' if buying else 'Sold'} {qty} {t['goods'][item]} "
                  f"for ${qty * price}.")
        # Back to the shelf, not the front door: one trade is rarely the
        # whole errand, and the market header carries cash and bag.
        return {'menu': 'market', 'start': 0}

    if menu == 'move':
        others = _others(state)
        place = others[_number(word, 1, len(others)) - 1]
        before, after = turn.act(f"travel {place}")
        if after['phase'] == 'ended' and after['day'] == before['day']:
            turn.note(t['deadline'])
        elif after['phase'] == 'ended':
            turn.note(f"Day {after['day']}: {t['places'][place]}. That was the last day.")
        else:
            turn.note(f"Day {after['day']}: {t['places'][place]}.")
        if after['phase'] != 'ended':
            cash_found = after['cash'] - before['cash']
            if cash_found > 0:
                turn.note(t['loot_cash'].format(amount=cash_found))
            else:
                for item in game.GOODS:
                    qty = after['inventory'][item] - before['inventory'][item]
                    if qty > 0:
                        turn.note(t['loot_goods'].format(
                            qty=qty, item=t['goods'][item]))
                        break
                else:
                    if 'Loot full.' in turn.last_reply:
                        turn.note(t['loot_full'])
            return {'menu': 'market', 'start': 0}
        return {'menu': 'main'}

    if menu == 'gear':
        key, price = GEAR[_number(word, 1, len(GEAR)) - 1]
        name = t['gear'][key][0]
        have = {'bag': state['capacity'] >= 70, 'vest': state['armor'] >= 1,
                'weapon': state['weapon'] >= 1, 'medkit': state['hp'] >= 100}[key]
        if have:
            raise _Stop(f"You already have full {t['hp']}." if key == 'medkit'
                        else f"You already have the {name}.")
        if state['cash'] < price:
            raise _Stop(f"The {name} costs ${price}; you have ${state['cash']}.")
        before, after = turn.act(f"equipment {key}")
        if after['moves'] == before['moves']:
            raise _Stop("That didn't work.")
        turn.note(f"Got the {name}.")
        return {'menu': 'main'}

    if menu == 'bag':
        raise _Stop("Reply 0 to go back.")

    if menu == 'loan':
        choice = _number(word, 1, 2)
        return {'menu': 'loan_amt', 'op': 'borrow' if choice == 1 else 'repay'}

    if menu == 'loan_amt':
        borrowing = nav['op'] == 'borrow'
        most = _max_borrow(state) if borrowing else min(state['debt'], state['cash'])
        amount = _amount(word, most)
        before, after = turn.act(f"loan {nav['op']} {amount}")
        if after['moves'] == before['moves']:
            raise _Stop("That didn't work.")
        turn.note(f"Borrowed ${amount} from {t['loan_long']}." if borrowing
                  else f"Paid back ${amount}.")
        return {'menu': 'main'}

    if menu == 'confirm_end':
        if word not in ('y', 'yes'):
            return {'menu': 'main'}
        turn.act("finish")
        turn.note("Everything you carried is sold.")
        return {'menu': 'main'}

    return {'menu': 'main'}


def handle(user_id, text, short_name, pg13, nav=None):
    """One reply in, one screen out: (reply, leave, nav).

    *nav* is the screen the player is on, kept in the BBS session state by
    the caller -- not in the save, which the engine's validate() rejects any
    unknown key from, by design.
    """
    t = theme(pg13)
    nav = dict(nav or {'menu': 'main'})
    words = (text or '').strip().lower().split()

    if words and words[0] in EXIT_WORDS:
        return f"{t['title']} saved. Carry on from Games.", True, {'menu': 'main'}

    _before, state, _reply, _leave, _result = door.play_state(user_id, None, short_name)
    if text is None:
        nav = {'menu': 'main'}

    turn = _Turn(user_id, short_name, t, state)
    for word in words:
        if word == '?':
            continue
        # [0] on the main screen, and on the encounter and end screens, is
        # save-and-exit; everywhere else it goes back a level.
        if word == '0' and (turn.state['phase'] in ('police', 'ended')
                            or nav.get('menu', 'main') == 'main'):
            prefix = (" ".join(turn.notes) + "\n") if turn.notes else ""
            return (f"{prefix}{t['title']} saved. Carry on from Games.",
                    True, {'menu': 'main'})
        try:
            nav = _step(turn, word, nav)
        except _Stop as stop:
            turn.note(str(stop))
            break
    return render(turn.state, nav, t, " ".join(turn.notes)), False, nav
