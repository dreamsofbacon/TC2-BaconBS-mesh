"""Lemonade Stand: ten days, a pitcher, and the weather.

The 1973 classroom game. Each morning there is a forecast; the player
decides how many glasses to make, how many signs to put up and what to
charge, in one line: "30 2 12". Cheap lemonade sells; signs bring people;
heat doubles the crowd, cloud thins it, and now and then a storm takes the
lot. One packet out and one line back per day.

The demand curve is the classic one. Every word of text is new, and there
is nothing here that needs a second theme: it is safe for anyone.
"""
import math
import sys

import door_kit

GAME_ID = 'lemonade'
COMMAND = 'LEMONADE'
NAME = 'Lemonade Stand'
SAVE_VERSION = 1

DAYS = 10
START_CASH = 200          # cents
SIGN_COST = 15            # cents
MAX_PRICE = 100           # cents a glass

WEATHER = {'sunny': ("sunny", 1.0), 'cloudy': ("cloudy", 0.6), 'hot': ("hot and dry", 2.0)}

RULES = ("10 days. Each day send 3 numbers: glasses to make, signs to buy, "
         "price in cents, e.g. 30 2 12. A lower price and more signs sell more. "
         "Heat doubles the crowd, cloud thins it, a storm ruins the day.")


class Refused(ValueError):
    """Orders the stand cannot carry out, said in the player's terms."""


# ── Engine ──────────────────────────────────────────────────────────────────

def glass_cost(day) -> int:
    """Lemons get dearer as the summer goes on."""
    return 2 if day <= 2 else 4 if day <= 6 else 5


def _forecast(state) -> str:
    """The first two days are always fine, so nobody opens on a storm."""
    if state['day'] <= 2:
        return 'sunny'
    roll = door_kit.draw(state, 1, 10)
    return 'sunny' if roll <= 6 else 'cloudy' if roll <= 8 else 'hot'


def new_game(seed) -> dict:
    state = {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
             'phase': 'play', 'outcome': '', 'day': 1, 'cash': START_CASH,
             'last': None}
    state['weather'] = _forecast(state)
    return state


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    if state['weather'] not in WEATHER or state['phase'] not in ('play', 'ended'):
        raise ValueError("bad state")
    if not isinstance(state['cash'], int) or not isinstance(state['day'], int):
        raise ValueError("bad numbers")
    return state


def demand(price, signs, weather) -> int:
    """How many glasses the day would buy, at this price with these signs."""
    if price < 10:
        base = (10 - price) / 10 * 0.8 * 30 + 30
    else:
        base = 100 * 30 / price ** 2
    drawn_by_signs = 1 - math.exp(-0.5 * signs)
    return int(WEATHER[weather][1] * (base + base * drawn_by_signs))


def check(state, glasses, signs, price) -> int:
    """The day's outlay in cents. Raises Refused if it cannot be done."""
    if glasses < 0 or signs < 0:
        raise Refused("Glasses and signs cannot be negative.")
    if not 1 <= price <= MAX_PRICE:
        raise Refused(f"Charge 1 to {MAX_PRICE} cents a glass.")
    cost = glasses * glass_cost(state['day']) + signs * SIGN_COST
    if cost > state['cash']:
        raise Refused(f"That costs {money(cost)}; you have {money(state['cash'])}.")
    return cost


def play_day(state, glasses, signs, price) -> dict:
    if state['phase'] != 'play':
        raise Refused("The summer is over.")
    cost = check(state, glasses, signs, price)
    # A cloudy day turns to a thunderstorm one time in four.
    storm = state['weather'] == 'cloudy' and door_kit.draw(state, 1, 4) == 1
    sold = 0 if storm else min(glasses, demand(price, signs, state['weather']))
    profit = sold * price - cost
    state['cash'] += profit
    state['last'] = {'made': glasses, 'sold': sold, 'price': price,
                     'profit': profit, 'storm': storm}
    if state['day'] >= DAYS:
        state['phase'], state['outcome'] = 'ended', 'finished'
    else:
        state['day'] += 1
        state['weather'] = _forecast(state)
    return state


def score(state) -> int:
    """What the stand finished with, in cents."""
    return max(0, state['cash'])


def result(state):
    return (score(state), DAYS) if state['outcome'] == 'finished' else None


# ── Screens ─────────────────────────────────────────────────────────────────

def money(cents) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100}.{abs(cents) % 100:02d}"


def _yesterday(last) -> str:
    if last is None:
        return "Your first day. You have a pitcher and a table."
    if last['storm']:
        return f"A storm! Nothing sold; you lost {money(-last['profit'])}."
    word = "made" if last['profit'] >= 0 else "lost"
    return (f"Sold {last['sold']} of {last['made']} at {last['price']}c: "
            f"{word} {money(abs(last['profit']))}.")


def render(state, note='') -> str:
    lines = [note] if note else []
    if state['phase'] == 'ended':
        earned = state['cash'] - START_CASH
        lines += [_yesterday(state['last']),
                  f"Summer's over. You finish with {money(state['cash'])}, "
                  f"{'up' if earned >= 0 else 'down'} {money(abs(earned))}.",
                  "[1]Another summer [0]Exit"]
    else:
        lines += [f"Day {state['day']}/{DAYS}: {WEATHER[state['weather']][0]}. "
                  f"Cash {money(state['cash'])}",
                  _yesterday(state['last']),
                  f"A glass costs {glass_cost(state['day'])}c to make, a sign {SIGN_COST}c.",
                  "Send: glasses signs price  e.g. 30 2 12 [?]Rules [0]Exit"]
    screen = "\n".join(lines)
    if note and not door_kit.fits(screen):
        return door_kit.MESSAGE_SEPARATOR.join(
            door_kit.pack([note]) + ["\n".join(lines[1:])])
    return screen


# ── Door ────────────────────────────────────────────────────────────────────

def respond(state, word) -> str:
    parts = word.replace(',', ' ').replace('c', ' ').split()
    try:
        orders = [int(part) for part in parts]
    except ValueError:
        orders = []
    if len(orders) != 3:
        return "Send three numbers: glasses signs price."
    try:
        play_day(state, *orders)
    except Refused as refused:
        return str(refused)
    return ''


def handle(user_id, text, short_name, nav=None):
    return door_kit.handle(sys.modules[__name__], user_id, text, short_name)
