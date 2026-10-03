"""Lemonade Stand: ten days, a pitcher, and the weather.

The 1973 classroom game. Each morning there is a forecast; the player
sets a plan from a menu -- how many glasses to make, how many signs to put
up and what to charge -- and opens the stand. Cheap lemonade sells; signs bring people;
heat doubles the crowd, cloud thins it, and now and then a storm takes the
lot. Yesterday's plan carries over, so a day can be one key.

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

# What a first morning's plan is before the player changes it: 55c of 200c.
FIRST_PLAN = {'glasses': 20, 'signs': 1, 'price': 10}

WEATHER = {'sunny': ("sunny", 1.0), 'cloudy': ("cloudy", 0.6), 'hot': ("hot and dry", 2.0)}

RULES = ("Run a lemonade stand for 10 days. Each day set a plan -- glasses "
         "to make, signs, price -- then [4] opens the stand. Cheap sells more; "
         "heat doubles the crowd. Choose by number; [?] shows this.")


class Refused(door_kit.Refused):
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
             'last': None, 'plan': dict(FIRST_PLAN)}
    state['weather'] = _forecast(state)
    return state


def plan_cost(state) -> int:
    plan = state['plan']
    return plan['glasses'] * glass_cost(state['day']) + plan['signs'] * SIGN_COST


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    if state['weather'] not in WEATHER or state['phase'] not in ('play', 'ended'):
        raise ValueError("bad state")
    if not isinstance(state['cash'], int) or not isinstance(state['day'], int):
        raise ValueError("bad numbers")
    state.setdefault('plan', dict(FIRST_PLAN))    # a save from before the menus
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


def render(state, note='', nav=None) -> str:
    plan, menu = state['plan'], (nav or {}).get('menu')
    if state['phase'] == 'ended':
        earned = state['cash'] - START_CASH
        lines = [_yesterday(state['last']),
                 f"Summer's over. You finish with {money(state['cash'])}, "
                 f"{'up' if earned >= 0 else 'down'} {money(abs(earned))}.",
                 "[1]Another summer [0]Exit"]
    elif menu == 'glasses':
        each = glass_cost(state['day'])
        lines = [f"A glass costs {each}c to make today; you have "
                 f"{money(state['cash'])}, enough for {state['cash'] // each}. "
                 f"Glasses not sold are poured away. How many? Now {plan['glasses']}. "
                 "[B]Back"]
    elif menu == 'signs':
        lines = [f"A sign costs {SIGN_COST}c and brings more people past; each one "
                 f"adds less than the last. How many? Now {plan['signs']}. [B]Back"]
    elif menu == 'price':
        lines = [f"Charge 1-{MAX_PRICE} cents a glass. Cheap sells more, and over "
                 f"10c the crowd thins fast. Cents a glass? Now {plan['price']}c. [B]Back"]
    else:
        lines = [f"Day {state['day']}/{DAYS}: {WEATHER[state['weather']][0]}. "
                 f"Cash {money(state['cash'])}",
                 _yesterday(state['last']),
                 f"Plan: {plan['glasses']} glasses, {plan['signs']} "
                 f"sign{'' if plan['signs'] == 1 else 's'}, "
                 f"{plan['price']}c each. Costs {money(plan_cost(state))}",
                 "[1]Glasses [2]Signs [3]Price [4]Open stand [?]Help [0]Exit"]
        if not note and not door_kit.fits("\n".join(lines)):
            # Only sums no summer reaches; the day's news goes ahead.
            return door_kit.MESSAGE_SEPARATOR.join(
                door_kit.pack(lines[:2]) + ["\n".join(lines[2:])])
    return door_kit.render_with_note(note, lines)


# ── Door ────────────────────────────────────────────────────────────────────

AGAIN = "A new summer, and the same pitcher."
EXIT_WORDS = door_kit.MENU_EXIT_WORDS
PLAN_KEYS = ('glasses', 'signs', 'price')


def step(state, nav, word):
    """One menu choice. Returns (note, next nav, leave)."""
    menu = nav.get('menu')
    if menu in PLAN_KEYS:
        if word in door_kit.BACK_WORDS:
            return '', {}, False
        number = door_kit.amount(word, "a whole number, or B to go back")
        if number < 0:
            raise Refused("Send a number of 0 or more.")
        if menu == 'price' and not 1 <= number <= MAX_PRICE:
            raise Refused(f"Charge 1 to {MAX_PRICE} cents a glass.")
        state['plan'][menu] = number
        cost = plan_cost(state)
        if cost > state['cash']:
            return (f"That plan costs {money(cost)}; you have "
                    f"{money(state['cash'])}."), {}, False
        return '', {}, False
    if word == '0':
        return '', {}, True
    picked = door_kit.choice(word, 4)
    if picked < 4:
        return '', {'menu': PLAN_KEYS[picked - 1]}, False
    plan = state['plan']
    play_day(state, plan['glasses'], plan['signs'], plan['price'])
    return '', {}, False


def respond(state, word) -> str:
    """One choice from the main screen: the menu door contract, for
    callers that keep no screen of their own."""
    note, _nav, _leave = step(state, {}, word)
    return note


def handle(user_id, text, short_name, nav=None):
    return door_kit.menu_handle(sys.modules[__name__], user_id, text, short_name, nav)
