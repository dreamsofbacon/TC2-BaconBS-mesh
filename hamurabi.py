"""Hamurabi: rule a city-state for ten years, one message a year.

The 1968 teletype game, which is very nearly this medium already. Each year
the steward reports, and the ruler sets a plan from a menu: acres to buy
(or, negative, to sell), bushels to feed the people, and acres to plant.
Each year opens with a plan that feeds everyone and plants what it can, so
a ruler changes only what they want to and then ends the year.

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

RULES = ("Rule for 10 years. Each year set a plan -- land to buy or sell, "
         "grain to feed, acres to plant -- then [4] ends the year. Choose by "
         "number; [?] shows this. Starve over 45% in a year and you are out.")


class Refused(door_kit.Refused):
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
    state['plan'] = default_plan(state)
    return state


def most_plantable(state) -> int:
    """Acres the plan's land and the people can plant, seed aside."""
    return min(state['land'] + state['plan']['buy'], state['pop'] * ACRES_PER_PERSON)


def grain_left(state) -> int:
    """What the store would hold after this year's plan, before harvest."""
    plan = state['plan']
    return (state['grain'] - plan['buy'] * state['price'] - plan['feed']
            - (plan['plant'] + 1) // 2)


def default_plan(state) -> dict:
    """Feed everyone, then plant all the seed left will cover. A starting
    point: the ruler changes what they want to."""
    feed = min(state['pop'] * BUSHELS_PER_PERSON, state['grain'])
    plant = min(state['land'], state['pop'] * ACRES_PER_PERSON,
                2 * (state['grain'] - feed))
    return {'buy': 0, 'feed': feed, 'plant': plant}


def validate(state) -> dict:
    """Refuse a save this version cannot play, rather than guess at it."""
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    for key in ('seed', 'draws', 'year', 'pop', 'grain', 'land', 'price', 'deaths'):
        if not isinstance(state[key], int) or state[key] < 0:
            raise ValueError(f"bad {key}")
    if state['phase'] not in ('play', 'ended'):
        raise ValueError("bad phase")
    if 'plan' not in state:       # a save from before the menus
        state['plan'] = default_plan(state)
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
        state['plan'] = default_plan(state)
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

def _report(state) -> str:
    last = state['last']
    report = (f"Crop {last['yield']}/acre, rats {last['rats']}, "
              f"starved {last['starved']}, came {last['came']}.")
    if last['plague']:
        report += " Plague halved the people!"
    return report


def _plan_line(state) -> str:
    plan, left = state['plan'], grain_left(state)
    spare = f"{left} left" if left >= 0 else f"{-left} short"
    return (f"Plan: buy {plan['buy']} feed {plan['feed']} plant {plan['plant']}, "
            f"{spare}. Land {state['price']}/acre")


def _screen(state, nav) -> list:
    if state['phase'] == 'ended':
        return [f"Reign over after {years_played(state)} years: {state['pop']} people, "
                f"{state['land']} acres, {state['deaths']} starved.",
                verdict(state),
                f"Score {score(state)}. [1]Rule again [0]Exit"]
    plan = state['plan']
    menu = nav.get('menu')
    if menu == 'land':
        return [f"Land costs {state['price']} bu an acre. You own {state['land']} "
                f"acres and {state['grain']} bu. Send acres to buy, or a minus to "
                f"sell: -50 sells 50. Now {plan['buy']}. [B]Back"]
    if menu == 'feed':
        need = state['pop'] * BUSHELS_PER_PERSON
        return [f"Each person eats {BUSHELS_PER_PERSON} bu a year: {need} feeds all "
                f"{state['pop']}. Less and some starve; over 45% and you are deposed. "
                f"Send bushels. Now {plan['feed']}. [B]Back"]
    if menu == 'plant':
        return [f"An acre takes 1/2 bu of seed and a person farms {ACRES_PER_PERSON}: "
                f"up to {most_plantable(state)} acres. It yields 1-5 bu an acre. "
                f"Send acres. Now {plan['plant']}. [B]Back"]
    return [f"Yr {state['year']}/{YEARS} Pop {state['pop']} "
            f"Grain {state['grain']} Land {state['land']}",
            _report(state),
            _plan_line(state),
            "[1]Land [2]Feed [3]Plant [4]End year [?]Help [0]Exit"]


def render(state, note='', nav=None) -> str:
    lines = _screen(state, nav or {})
    if note or door_kit.fits("\n".join(lines)):
        return door_kit.render_with_note(note, lines)
    # Only numbers no real reign reaches push the report into a message of
    # its own. Nothing is ever cut.
    return door_kit.MESSAGE_SEPARATOR.join(
        door_kit.pack(lines[:2]) + ["\n".join(lines[2:])])


# ── Door ────────────────────────────────────────────────────────────────────

AGAIN = "A new city, and ten years to rule it."
EXIT_WORDS = door_kit.MENU_EXIT_WORDS


def step(state, nav, word):
    """One menu choice. Returns (note, next nav, leave)."""
    menu = nav.get('menu')
    if menu in ('land', 'feed', 'plant'):
        if word in door_kit.BACK_WORDS:
            return '', {}, False
        number = door_kit.amount(word, "a whole number, or B to go back")
        if menu != 'land' and number < 0:
            raise Refused("Send a number of 0 or more.")
        if menu == 'land' and -number > state['land']:
            raise Refused(f"You own only {state['land']} acres.")
        state['plan']['buy' if menu == 'land' else menu] = number
        return '', {}, False
    if word == '0':
        return '', {}, True
    picked = door_kit.choice(word, 4)
    if picked < 4:
        return '', {'menu': ('land', 'feed', 'plant')[picked - 1]}, False
    plan = state['plan']
    play_year(state, plan['buy'], plan['feed'], plan['plant'])
    return '', {}, False


def respond(state, word) -> str:
    """One choice from the main screen: the menu door contract, for
    callers that keep no screen of their own."""
    note, _nav, _leave = step(state, {}, word)
    return note


def result(state):
    """A finished reign is a result. Being deposed is not: a zero on the
    scoreboard for it would be noise."""
    if state['outcome'] != 'finished':
        return None
    return score(state), years_played(state)


def handle(user_id, text, short_name, nav=None):
    """One message in, one reply out: (reply, leave, nav)."""
    import sys
    return door_kit.menu_handle(sys.modules[__name__], user_id, text, short_name, nav)
