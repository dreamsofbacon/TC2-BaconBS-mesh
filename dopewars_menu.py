"""Numbered menus over the trading door, in Candy Wars or Dope Wars words.

The engine (dopewars.py) is played by typed commands -- "B weed 2" -- and
every move redrew the whole market, two Meshtastic packets a turn. This layer
turns it into numbered screens that each fit one packet, and translates a
choice into the engine command it stands for. The engine remains the source of
truth for rules and saves, while this layer owns the airport confirmation flow.

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
_FOOTER_MORE = "$/stock +bag [M]ore {page}/{pages} [0]Back"
# What the fit rule trims either of them to, and the width the pager
# must keep free for the wider one.
_FOOTER_KEY = "$/stock +bag "
_FOOTER_WIDEST = _FOOTER_MORE.format(page=9, pages=9)

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
    carried = sum(state['inventory'].values())
    due = f" (due D{state['loan_due']})" if state['debt'] else ""
    gear = ("🔫" if state['weapon'] else "") + ("🛡️" if state['armor'] else "")
    gear = gear or "—"
    net = game.score(state)
    net_display = f"${net}" if net < 1_000_000_000 else f"${net / 1_000_000_000:g}B"
    return (f"⏱️ D{state['day']}/{state['days']} | 📍 {place}"
            f" | 💵 ${state['cash']} | 💳 ${state['debt']}{due}\n"
            f"❤️ {state['hp']} | 🎒 {carried}/{state['capacity']}"
            f" | Gear {gear} | Net {net_display}")


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


def _paginate(rows, head, budget):
    """Split the rows into windows, each fitting one packet.

    Whole pages rather than a rolling window: the player needs to know
    how many there are and that pressing [M] enough times comes back
    round, which a window computed only forwards cannot say.
    """
    reserve = len(_FOOTER_WIDEST.encode('utf-8')) + 1
    room = budget - len(head.encode('utf-8')) - reserve
    pages, index = [], 0
    while index < len(rows):
        first, used = index, 0
        while index < len(rows):
            cost = len(rows[index].encode('utf-8')) + 1
            # Always place one row, even a freakishly wide one: a page
            # showing nothing could never be paged past.
            if index > first and used + cost > room:
                break
            used += cost
            index += 1
        pages.append((first, index))
    return pages


def _market_pages(state, t):
    """Return the readable market as exactly two automatic messages."""
    carried = sum(state['inventory'].values())
    rows = _market_rows(state, t)
    midpoint = (len(rows) + 1) // 2
    pages = (rows[:midpoint], rows[midpoint:])
    output = []
    for number, page in enumerate(pages, start=1):
        lines = [f"Mkt {number}/2 ${state['cash']} b{carried}/{state['capacity']}"]
        for index in range(0, len(page), 2):
            left = page[index]
            right = page[index + 1] if index + 1 < len(page) else ""
            lines.append(f"{left} | {right}" if right else left)
        lines.append("0")
        output.append("\n".join(lines))
    return output


def _market_page(state, nav, t, budget=_PAGE_BUDGET):
    """Compatibility helper for callers that walk the old page cursor."""
    pages = _market_pages(state, t)
    start = nav.get('start', 0)
    index = 1 if start else 0
    return pages[index].split('\n'), 0


def _max_borrow(state) -> int:
    return max(0, min(game.LOAN_LIMIT - state['debt'], game.MAX_CASH - state['cash']))


def _at_the_bank(state) -> bool:
    return state['place'] == game.BANK_PLACE


def _at_the_airport(state) -> bool:
    return game.airport_city(state['place']) is not None


def _at_the_border(state) -> bool:
    return state['place'] in game.BORDER_PLACES


def _airport_destinations(state):
    origin = game.airport_city(state['place'])
    return [city for city in game.CITIES if city in game.AIRPORTS and city != origin]


# What each money move can move at most, and the engine command for it.
MONEY_OPS = {
    'borrow': (_max_borrow, 'loan borrow'),
    'repay': (lambda s: min(s['debt'], s['cash']), 'loan repay'),
    'deposit': (lambda s: min(s['cash'], game.MAX_CASH - s['bank']),
                'bank deposit'),
    'withdraw': (lambda s: min(s['bank'], game.MAX_CASH - s['cash']),
                 'bank withdraw'),
}


def _others(state):
    current_city = game.city(state['place'])
    return [p for p in game.CITY_PLACES[current_city] if p != state['place']]


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
        lines += "\n\n".join(_market_pages(state, t)).split("\n")
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
    elif menu == 'airport':
        origin = game.airport_city(state['place'])
        lines.append(f"Airport: {game.CITY_LABELS[origin]} (cash ${state['cash']})")
        for index, destination in enumerate(_airport_destinations(state), start=1):
            fare = game.flight_fare(state, destination)
            lines.append(f"[{index}]{game.CITY_LABELS[destination]} ${fare}")
        lines.append("[0]Back")
    elif menu == 'flight_confirm':
        destination = nav['destination']
        fare = game.flight_fare(state, destination)
        lines.append(f"Fly to {game.CITY_LABELS[destination]} for ${fare}?")
        lines.append("[Y]es, board now [0]Back")
    elif menu == 'border':
        other = game.CITY_LABELS[game.city(game.BORDER_PLACES[state['place']])]
        lines.append(f"Border terminal: {other}")
        lines.append("[1]Cross legally [2]Hop fence [0]Back")
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
        saved = f", ${state['bank']} in the {t['bank']}" if state['bank'] else ''
        lines.append(f"${state['cash']} on you{saved}.")
        owed = (f"{t['owed']} ${state['debt']} d{state['loan_due']}"
                if state['debt'] else "nothing owed")
        lines.append(f"{t['loan']}: {owed}, up to ${game.LOAN_LIMIT}.")
        if _at_the_bank(state):
            lines.append("[1]Borrow [2]Pay back [3]Deposit [4]Withdraw "
                         "[0]Back")
        else:
            # Say where rather than offer a key that would only refuse.
            lines.append(f"[1]Borrow [2]Pay back [0]Back. "
                         f"{t['bank'].capitalize()}: "
                         f"{t['places'][game.BANK_PLACE]}.")
    elif menu == 'loan_amt':
        op = nav['op']
        most = MONEY_OPS[op][0](state)
        verb = 'pay back' if op == 'repay' else op
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
            # The icon already communicates deal/bust; keep the item name but
            # drop the longer sentence so the three-line status header stays
            # inside one packet.
            lines.append(f"{icon} {t['goods'][item]}")
        airport_option = (" [7]Airport" if _at_the_airport(state)
                          else (" [7]Border" if _at_the_border(state) else ""))
        lines.append(f"[1]Mkt [2]Travel [3]Bag [4]Gear "
                     f"[5]$ [6]E{airport_option} [0]X")
        if state['moves'] == 0 and state['days'] == 30 and not _at_the_airport(state) and not _at_the_border(state):
            lines.append("[7]365d")

    # One packet beats a few words, but only just: each screen gives up
    # its least useful text rather than spend a second packet of airtime.
    # On trade screens that is the slash legend; on the main screen it is
    # the title, then decorative status separators at extreme balances.
    # Keep every action and statistic rather than truncating a screen.
    screen = "\n".join(lines)
    if len(screen.encode('utf-8')) > MAX_SCREEN_BYTES:
        if lines[-1].startswith(_FOOTER_KEY):
            # The legend goes; [M]ore and [0]Back stay, being the only
            # ways off the screen.
            lines = lines[:-1] + [lines[-1][len(_FOOTER_KEY):]]
        elif menu == 'main':
            # Preserve the title by compressing separators and labels first;
            # only a truly impossible packet should drop it.
            lines = [line.replace(' | ', ' ').replace(' | Gear ', ' G ').replace(' | Net ', ' N ')
                     for line in lines]
            screen = "\n".join(lines)
            if len(screen.encode('utf-8')) > MAX_SCREEN_BYTES and not note and lines and lines[0] == t['title']:
                lines = lines[1:]
        screen = "\n".join(lines)
    if menu == 'main' and len(screen.encode('utf-8')) > MAX_SCREEN_BYTES:
        screen = screen.replace(' | ', ' ')
    if menu == 'main' and len(screen.encode('utf-8')) > MAX_SCREEN_BYTES:
        screen = screen.replace(' | Gear ', ' G ').replace(' | Net ', ' N ')
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
        has_airport = _at_the_airport(state)
        has_border = _at_the_border(state)
        top = 7 if has_airport or has_border or (state['moves'] == 0 and state['days'] == 30) else 6
        # Letters for the two people reach for every turn, spelling what
        # the buttons say.
        if word in ('t', 'travel'):
            return {'menu': 'move'}
        if word in ('a', 'airport') and has_airport:
            return {'menu': 'airport'}
        if word in ('b', 'border') and has_border:
            return {'menu': 'border'}
        if word in ('m', 'market'):
            return {'menu': 'market', 'start': 0}
        choice = _number(word, 1, top)
        if choice == 7 and has_airport:
            return {'menu': 'airport'}
        if choice == 7 and has_border:
            return {'menu': 'border'}
        if choice == 7:
            turn.act("new 365")
            turn.note("Now a 365-day run.")
            return {'menu': 'main'}
        return {'menu': {1: 'market', 2: 'move', 3: 'bag',
                         4: 'gear', 5: 'loan', 6: 'confirm_end'}[choice]}

    if word == '0':
        # One level up from every sub-screen.
        up = {'buy_qty': 'item', 'sell_qty': 'item', 'item': 'market',
              'loan_amt': 'loan', 'flight_confirm': 'airport', 'border': 'main'}
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

    if menu == 'airport':
        destinations = _airport_destinations(state)
        destination = destinations[_number(word, 1, len(destinations)) - 1]
        return {'menu': 'flight_confirm', 'destination': destination}

    if menu == 'border':
        choice = _number(word, 1, 2)
        before, after = turn.act('border ' + ('legal' if choice == 1 else 'fence'))
        if after['phase'] == 'police':
            turn.note('Border police encounter.')
            return {'menu': 'main'}
        if after['place'] != before['place']:
            turn.note(f"Arrived in {game.CITY_LABELS[game.city(after['place'])]}.")
            city_line, _ = game.arrival_descriptions(
                before['seed'], before['moves'], before['day'] + 1,
                after['place'], from_airport=True)
            if city_line:
                turn.note(city_line)
        else:
            turn.note('Turned back at the border.')
        return {'menu': 'market', 'start': 0}

    if menu in ('move', 'flight_confirm'):
        fare = 0
        if menu == 'flight_confirm':
            if word not in ('y', 'yes'):
                return {'menu': 'airport'}
            destination = nav['destination']
            fare = game.flight_fare(state, destination)
            if state['cash'] < fare:
                raise _Stop(f"You need ${fare} cash for that flight.")
            place = game.AIRPORTS[destination]
            before, after = turn.act(f"flight {destination} yes")
        else:
            others = _others(state)
            place = others[_number(word, 1, len(others)) - 1]
            before, after = turn.act(f"travel {place}")
        if after['moves'] == before['moves']:
            raise _Stop("That didn't work.")
        if after['phase'] == 'ended' and after['day'] == before['day']:
            turn.note(t['deadline'])
        elif after['phase'] == 'ended':
            turn.note(f"Day {after['day']}: {t['places'][place]}. That was the last day.")
        else:
            turn.note(f"Day {after['day']}: {t['places'][place]}.")
        if after['moves'] != before['moves'] and after['phase'] != 'ended':
            city_line, district_line = game.arrival_descriptions(
                before['seed'], before['moves'], before['day'] + 1, place,
                from_airport=(menu == 'flight_confirm'))
            if city_line:
                turn.note(city_line)
            if district_line:
                turn.note(district_line)
        if after['phase'] != 'ended':
            cash_found = after['cash'] - before['cash'] + fare
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
        top = 4 if _at_the_bank(state) else 2
        op = ('borrow', 'repay', 'deposit', 'withdraw')[_number(word, 1, top) - 1]
        return {'menu': 'loan_amt', 'op': op}

    if menu == 'loan_amt':
        op = nav['op']
        most, command = MONEY_OPS[op][0](state), MONEY_OPS[op][1]
        amount = _amount(word, most)
        before, after = turn.act(f"{command} {amount}")
        if after['moves'] == before['moves']:
            raise _Stop("That didn't work.")
        turn.note({
            'borrow': f"Borrowed ${amount} from {t['loan_long']}.",
            'repay': f"Paid back ${amount}.",
            'deposit': f"Put ${amount} in the {t['bank']}.",
            'withdraw': f"Took ${amount} out of the {t['bank']}.",
        }[op])
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
