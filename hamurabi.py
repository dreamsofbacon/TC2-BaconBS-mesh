"""Hamurabi: rule a city-state for ten years, one message a year.

The 1968 teletype game, which is very nearly this medium already. Each year
the steward reports, and the ruler answers with three numbers: acres to buy
(or, negative, to sell), bushels to feed the people, and acres to plant.
One packet out and one line back per year, so a whole reign is ten turns.

The rules are the classic ones; every word of text is new. The engine is
pure: a state dict in, a state dict out, with every random draw taken from
the seed and a counter held in the state.

* Each person needs 20 bushels a year and can farm 10 acres.
* One bushel seeds two acres. The harvest is 1 to 5 bushels an acre.
* Rats may eat a share of the store; a plague may halve the people.
* Starve more than 45% in one year and the reign ends on the spot.
"""
import random

import door_kit

GAME_ID = 'hamurabi'
COMMAND = 'HAMURABI'
NAME = 'Hamurabi'
SAVE_VERSION = 1

YEARS = 10
BUSHELS_PER_PERSON = 20
ACRES_PER_PERSON = 10

RULES = ("10 years. Each year send 3 numbers: acres to buy (negative sells), "
         "bushels to feed, acres to plant. A person eats 20 bushels and farms "
         "10 acres. 1 bushel seeds 2 acres. Starve over 45% and you're out.")


class Refused(ValueError):
    """An order the steward cannot carry out, said in the player's terms."""


# ── Engine ──────────────────────────────────────────────────────────────────

def _draw(state, low, high) -> int:
    """The next random number, from the seed and how many were drawn before.
    Seeding with a string is stable across Python versions and platforms."""
    state['draws'] += 1
    return random.Random(f"{state['seed']}:{state['draws']}").randint(low, high)


def new_game(seed) -> dict:
    state = {
        'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
        'phase': 'play', 'outcome': '',
        'year': 1, 'pop': 100, 'grain': 2800, 'land': 1000,
        # What the steward reports on the first screen: the year before.
        'last': {'yield': 3, 'rats': 200, 'starved': 0, 'came': 5, 'plague': False},
        'starved_pct_sum': 0.0, 'deaths': 0,
    }
    state['price'] = _draw(state, 17, 26)
    return state


def validate(state) -> dict:
    """Refuse a save this version cannot play, rather than guess at it."""
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    for key in ('seed', 'draws', 'year', 'pop', 'grain', 'land', 'price', 'deaths'):
        if not isinstance(state[key], int) or state[key] < 0:
            raise ValueError(f"bad {key}")
    if state['phase'] not in ('play', 'ended'):
        raise ValueError("bad phase")
    return state


def check(state, buy, feed, plant) -> None:
    """Raise Refused, naming the limit, if these orders cannot be met."""
    if feed < 0 or plant < 0:
        raise Refused("Feed and plant cannot be negative.")
    if buy < 0 and -buy > state['land']:
        raise Refused(f"You own only {state['land']} acres.")
    grain = state['grain'] - buy * state['price']
    if grain < 0:
        raise Refused(f"{buy} acres cost {buy * state['price']}; you have "
                      f"{state['grain']} bushels.")
    if feed > grain:
        raise Refused(f"Only {grain} bushels are left to feed with.")
    land = state['land'] + buy
    if plant > land:
        raise Refused(f"You would own only {land} acres.")
    if plant > state['pop'] * ACRES_PER_PERSON:
        raise Refused(f"{state['pop']} people can farm only "
                      f"{state['pop'] * ACRES_PER_PERSON} acres.")
    seed_grain = (plant + 1) // 2
    if seed_grain > grain - feed:
        raise Refused(f"Planting {plant} acres needs {seed_grain} bushels; "
                      f"{grain - feed} would be left.")


def play_year(state, buy, feed, plant) -> dict:
    """Carry out one year's orders. Raises Refused and changes nothing if
    they cannot be met."""
    if state['phase'] != 'play':
        raise Refused("The reign is over.")
    check(state, buy, feed, plant)

    state['land'] += buy
    state['grain'] -= buy * state['price'] + feed + (plant + 1) // 2

    harvest_per_acre = _draw(state, 1, 5)
    state['grain'] += plant * harvest_per_acre

    # Rats get into the store on an even roll, and take that share of it.
    roll = _draw(state, 1, 5)
    eaten = state['grain'] // roll if roll % 2 == 0 else 0
    state['grain'] -= eaten

    pop = state['pop']
    starved = max(0, pop - feed // BUSHELS_PER_PERSON)
    came = int(_draw(state, 1, 5) * (20 * state['land'] + state['grain']) / pop / 100) + 1
    plague = _draw(state, 1, 100) <= 15

    state['last'] = {'yield': harvest_per_acre, 'rats': eaten, 'starved': starved,
                     'came': 0, 'plague': False}
    state['deaths'] += starved
    state['starved_pct_sum'] += starved * 100.0 / pop

    if starved > pop * 0.45:
        state['pop'] = pop - starved
        state['phase'], state['outcome'] = 'ended', 'deposed'
        return state

    state['pop'] = pop - starved + came
    state['last']['came'] = came
    if plague:
        state['pop'] //= 2
        state['last']['plague'] = True

    if state['year'] >= YEARS:
        state['phase'], state['outcome'] = 'ended', 'finished'
    else:
        state['year'] += 1
        state['price'] = _draw(state, 17, 26)
    return state


def years_played(state) -> int:
    return state['year'] if state['phase'] == 'ended' else state['year'] - 1


def average_starved_pct(state) -> float:
    return state['starved_pct_sum'] / max(1, years_played(state))


def score(state) -> int:
    """Acres per person, less the share of the people starved. Nothing for
    a ruler who was deposed."""
    if state['outcome'] != 'finished':
        return 0
    acres_each = state['land'] / max(1, state['pop'])
    return max(0, int(acres_each * 100) - int(average_starved_pct(state) * 10))


def verdict(state) -> str:
    if state['outcome'] == 'deposed':
        return (f"{state['last']['starved']} starved in one year. You are "
                "deposed and driven from the city.")
    starved, acres = average_starved_pct(state), state['land'] / max(1, state['pop'])
    if starved > 33 or acres < 7:
        return "The people remember you as a tyrant."
    if starved > 10 or acres < 9:
        return "A hard reign. Few will mourn its end."
    if starved > 3 or acres < 10:
        return "A fair reign, though some would have had another ruler."
    return "A golden reign. They will carve your name in stone."


# ── Screens ─────────────────────────────────────────────────────────────────

def render(state, note='') -> str:
    last = state['last']
    if state['phase'] == 'ended':
        lines = [note] if note else []
        lines += [
            f"Reign over after {years_played(state)} years: {state['pop']} people, "
            f"{state['land']} acres, {state['deaths']} starved.",
            verdict(state),
            f"Score {score(state)}. [1]Rule again [0]Exit",
        ]
        return "\n".join(lines)
    report = (f"Harvest {last['yield']}/acre, rats ate {last['rats']}. "
              f"{last['starved']} starved, {last['came']} came.")
    if last['plague']:
        report += " Plague halved the people!"
    status = (f"Yr {state['year']}/{YEARS} People {state['pop']} "
              f"Grain {state['grain']} Land {state['land']}")
    ask = f"Land costs {state['price']}. Send: buy feed plant"
    # The same screen at three widths. One packet beats a few words: the
    # worked example goes first, and only numbers no real reign reaches
    # push the report into a message of its own. Nothing is ever cut.
    full = [status, report, ask, "e.g. 0 2000 1000  [?]Rules [0]Exit"]
    compact = [status, report, ask + " [?]Rules [0]Exit"]
    sep = door_kit.MESSAGE_SEPARATOR
    for body in (full, compact):
        screen = "\n".join(body)
        if note and door_kit.fits(f"{note}\n{screen}"):
            return f"{note}\n{screen}"
        if door_kit.fits(screen):
            # A note too long to sit above goes ahead as its own message.
            return sep.join(door_kit.pack([note]) + [screen]) if note else screen
    return sep.join((door_kit.pack([note]) if note else [])
                    + door_kit.pack([status, report]) + [compact[-1]])


# ── Door ────────────────────────────────────────────────────────────────────

def _numbers(text):
    """Three whole numbers from one line, or None. Commas are forgiven."""
    parts = text.replace(',', ' ').split()
    if len(parts) != 3:
        return None
    try:
        return [int(part) for part in parts]
    except ValueError:
        return None


def respond(state, word) -> str:
    """Apply one line of input; return the note for the next screen."""
    orders = _numbers(word)
    if orders is None:
        return "Send three numbers: buy feed plant."
    try:
        play_year(state, *orders)
    except Refused as refused:
        return str(refused)
    return ''


def result(state):
    """A finished reign is a result. Being deposed is not: a zero on the
    scoreboard for it would be noise."""
    if state['outcome'] != 'finished':
        return None
    return score(state), years_played(state)


def handle(user_id, text, short_name, nav=None):
    """One message in, one reply out: (reply, leave, nav)."""
    import sys
    return door_kit.handle(sys.modules[__name__], user_id, text, short_name)
