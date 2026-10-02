"""Oregon Trail: 2040 miles, a wagon, and eighteen fortnights before winter.

The 1971 teletype game, cut to fit a radio. The original asked several
questions a turn and timed how fast you could type BANG; here a fortnight
is one line. First the outfit, in one message: dollars for oxen, food,
ammunition, clothing and supplies. Then each turn an action and how well to
eat: "1 2" travels eating moderately, "2 1" hunts and eats poorly, and at a
fort "3 40 0 10 0 2" buys food and clothing before moving on.

The trail's arithmetic follows the classic listing closely -- mileage from
the oxen, what a fort charges, what goes wrong and what it costs. Every word
of text is new, and hunting is a roll rather than a race against a clock.
"""
import sys

import door_kit

GAME_ID = 'oregon'
COMMAND = 'OREGON'
NAME = 'Oregon Trail'
SAVE_VERSION = 1

TRAIL_MILES = 2040
MAX_TURNS = 18            # fortnights; after that, winter
PURSE = 700
OXEN_RANGE = (200, 300)
MEALS = {1: 13, 2: 18, 3: 23}       # food eaten a fortnight
DOCTOR = 20
BULLETS_PER_DOLLAR = 50

RULES = ("Reach Oregon in 18 turns. Outfit: 5 amounts in $: oxen(200-300) food "
         "ammo clothes supplies. Then: 1 travel, 2 hunt, 3 fort (buy: food ammo "
         "clothes supplies), then meal 1-3. e.g. 1 2")


class Refused(ValueError):
    """Orders that cannot be carried out, said in the player's terms."""


# ── Engine ──────────────────────────────────────────────────────────────────

def new_game(seed) -> dict:
    return {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
            'phase': 'outfit', 'outcome': '', 'turn': 0, 'miles': 0,
            'oxen': 0, 'food': 0, 'ammo': 0, 'clothes': 0, 'supplies': 0,
            'cash': PURSE, 'sick': False, 'meal': 2}


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    if state['phase'] not in ('outfit', 'play', 'ended'):
        raise ValueError("bad phase")
    for key in ('turn', 'miles', 'oxen', 'food', 'ammo', 'clothes', 'supplies', 'cash'):
        if not isinstance(state[key], int):
            raise ValueError(f"bad {key}")
    return state


def outfit(state, oxen, food, ammo, clothes, supplies) -> None:
    """Spend the purse before setting out. What is not spent stays as cash."""
    if not OXEN_RANGE[0] <= oxen <= OXEN_RANGE[1]:
        raise Refused(f"Spend ${OXEN_RANGE[0]}-${OXEN_RANGE[1]} on oxen.")
    if min(food, ammo, clothes, supplies) < 0:
        raise Refused("Amounts cannot be negative.")
    spent = oxen + food + ammo + clothes + supplies
    if spent > PURSE:
        raise Refused(f"That is ${spent}; you have ${PURSE}.")
    state.update(oxen=oxen, food=food, ammo=ammo * BULLETS_PER_DOLLAR,
                 clothes=clothes, supplies=supplies, cash=PURSE - spent,
                 phase='play', turn=1)


def at_fort(state) -> bool:
    """A fort stands at every other stop, as on the original trail."""
    return state['turn'] % 2 == 0


def _die(state, how, events) -> None:
    state['phase'], state['outcome'] = 'ended', how
    events.append({
        'starved': "The food is gone. The trail ends here.",
        'untreated': "No money for a doctor. The trail ends here.",
        'medicine': "No supplies left to treat it. The trail ends here.",
        'winter': "Winter closes the passes with Oregon still ahead.",
    }[how])


def _illness(state, events) -> None:
    """How bad it is depends on how well everyone has been eating."""
    meal = state['meal']
    roll = door_kit.draw(state, 1, 100)
    if roll < 10 + 35 * (meal - 1):
        events.append("A mild fever passes.")
        state['miles'] -= 5
        state['supplies'] -= 2
    elif roll < 100 - 40 // (4 ** (meal - 1)):
        events.append("A bad fever slows everyone.")
        state['miles'] -= 5
        state['supplies'] -= 5
    else:
        events.append(f"Someone is seriously ill. A doctor will cost ${DOCTOR}.")
        state['miles'] -= 10
        state['sick'] = True
    if state['supplies'] < 0:
        _die(state, 'medicine', events)


# (chance out of 100, what it says, changes). A change is a fixed amount, or
# a (low, high) range drawn fresh. Ammunition is in bullets.
_EVENTS = (
    (6, "The wagon breaks down.", {'miles': (-20, -15), 'supplies': -8}),
    (5, "An ox goes lame.", {'miles': -25, 'oxen': -20}),
    (4, "A broken arm to set.", {'miles': (-9, -5), 'supplies': (-5, -2)}),
    (4, "An ox wanders off. Half a day to find it.", {'miles': -17}),
    (5, "Bad water. You detour for a clean spring.", {'miles': (-12, -2)}),
    (10, "Heavy rain.", {'food': -10, 'ammo': -500, 'supplies': -15, 'miles': (-15, -5)}),
    (5, "Fire in the wagon.", {'food': -40, 'ammo': -400, 'supplies': (-11, -3), 'miles': -15}),
    (5, "Lost in fog.", {'miles': (-15, -10)}),
    (5, "The wagon is swamped at a ford.", {'food': -30, 'clothes': -20, 'miles': (-40, -20)}),
    (10, "Hail.", {'miles': (-15, -5), 'ammo': -200, 'supplies': (-7, -4)}),
)


def _event(state, events) -> None:
    """At most one thing goes wrong a fortnight."""
    roll = door_kit.draw(state, 1, 100)
    passed = 0
    for chance, text, changes in _EVENTS:
        passed += chance
        if roll <= passed:
            events.append(text)
            for key, amount in changes.items():
                if isinstance(amount, tuple):
                    amount = door_kit.draw(state, *amount)
                state[key] += amount
            return
    if roll <= passed + 5:
        # Bandits: bullets see them off; without bullets they take money.
        if state['ammo'] >= 100:
            state['ammo'] -= door_kit.draw(state, 60, 100)
            events.append("Bandits! You drive them off.")
        else:
            state['cash'] -= state['cash'] // 3
            state['sick'] = True
            events.append(f"Bandits! They take a third of your cash and leave "
                          f"a wound. A doctor will cost ${DOCTOR}.")
    elif roll <= passed + 10:
        events.append("A snake bite.")
        state['ammo'] -= 10
        state['supplies'] -= 5
        if state['supplies'] < 0:
            _die(state, 'medicine', events)
    elif roll <= passed + 18:
        # Cold: not enough clothing means illness.
        if state['clothes'] < door_kit.draw(state, 22, 26):
            events.append("Cold nights, and too little to wear.")
            _illness(state, events)
        else:
            events.append("Cold nights, but everyone is warmly dressed.")
    elif roll <= passed + 24:
        _illness(state, events)


def _mountains(state, events) -> None:
    """Past 950 miles the trail climbs, and most fortnights it costs."""
    if state['miles'] <= 950 or door_kit.draw(state, 1, 10) > 6:
        return
    roll = door_kit.draw(state, 1, 10)
    if roll == 1:
        events.append("You lose the trail in the mountains.")
        state['miles'] -= 60
    elif roll == 2:
        events.append("The wagon is damaged on the rocks.")
        state['supplies'] -= 5
        state['ammo'] -= 200
        state['miles'] -= door_kit.draw(state, 20, 50)
    elif roll == 3:
        events.append("A blizzard in the pass.")
        state['food'] -= 25
        state['supplies'] -= 10
        state['ammo'] -= 300
        state['miles'] -= door_kit.draw(state, 30, 70)
        if state['clothes'] < door_kit.draw(state, 18, 20):
            _illness(state, events)
    else:
        events.append("Slow going in the mountains.")
        state['miles'] -= door_kit.draw(state, 45, 95)


def check(state, action, meal, buys=()) -> None:
    if action not in (1, 2, 3):
        raise Refused("Action is 1 travel, 2 hunt or 3 fort.")
    if meal not in MEALS:
        raise Refused("Meal is 1 poor, 2 moderate or 3 well.")
    if action == 2 and state['ammo'] < 40:
        raise Refused("Too few bullets to hunt.")
    if action == 3:
        if not at_fort(state):
            raise Refused("No fort here. There is one next turn.")
        if len(buys) != 4 or min(buys) < 0:
            raise Refused("At a fort send: 3 food ammo clothes supplies meal.")
        if sum(buys) > state['cash']:
            raise Refused(f"That is ${sum(buys)}; you have ${state['cash']}.")
    elif buys:
        raise Refused("Only a fort sells goods.")


def play_turn(state, action, meal, buys=()) -> list:
    """One fortnight. Returns what happened, in order."""
    if state['phase'] != 'play':
        raise Refused("The journey is over.")
    check(state, action, meal, buys)
    events = []
    state['meal'] = meal

    if state['sick']:
        state['sick'] = False
        if state['cash'] < DOCTOR:
            _die(state, 'untreated', events)
            return events
        state['cash'] -= DOCTOR
        events.append(f"The doctor is paid ${DOCTOR}.")

    progress = 200 + (state['oxen'] - 220) // 5 + door_kit.draw(state, 0, 10)
    if action == 3:
        food, ammo, clothes, supplies = buys
        state['cash'] -= sum(buys)
        # A fort charges half as much again as home did.
        state['food'] += food * 2 // 3
        state['ammo'] += ammo * 2 // 3 * BULLETS_PER_DOLLAR
        state['clothes'] += clothes * 2 // 3
        state['supplies'] += supplies * 2 // 3
        progress -= 45
        events.append("You trade at the fort.")
    elif action == 2:
        shot = door_kit.draw(state, 1, 6)
        state['ammo'] -= 10 + 3 * shot
        progress -= 45
        if shot == 1:
            events.append("You hunt all day and hit nothing.")
        else:
            meat = 30 + 12 * shot
            state['food'] += meat
            events.append(f"Good hunting: {meat} lb of meat.")

    state['food'] -= MEALS[meal]
    if state['food'] < 0:
        state['food'] = 0
        _die(state, 'starved', events)
        return events

    state['miles'] += progress
    _event(state, events)
    if state['phase'] == 'play':
        _mountains(state, events)
    for key in ('food', 'ammo', 'clothes', 'oxen', 'supplies'):
        state[key] = max(0, state[key])
    state['miles'] = max(0, state['miles'])
    if state['phase'] != 'play':
        return events

    if state['miles'] >= TRAIL_MILES:
        state['miles'] = TRAIL_MILES
        state['phase'], state['outcome'] = 'ended', 'arrived'
        events.append("Oregon! You made it.")
    elif state['turn'] >= MAX_TURNS:
        _die(state, 'winter', events)
    else:
        state['turn'] += 1
    return events


def score(state) -> int:
    """What you arrived with, and how early."""
    if state['outcome'] != 'arrived':
        return 0
    goods = (state['cash'] + state['food'] + state['clothes'] + state['supplies']
             + state['ammo'] // BULLETS_PER_DOLLAR)
    return max(1, goods + 50 * (MAX_TURNS - state['turn']))


def result(state):
    """Arriving is a result. The trail keeps no list of those it took."""
    return (score(state), state['turn']) if state['outcome'] == 'arrived' else None


# ── Screens ─────────────────────────────────────────────────────────────────

def _stores(state) -> str:
    return (f"Food {state['food']} Ammo {state['ammo']} Clothes {state['clothes']} "
            f"Supplies {state['supplies']} Cash ${state['cash']}")


def render(state, note='') -> str:
    if state['phase'] == 'outfit':
        lines = [f"Oregon Trail: {TRAIL_MILES} miles, {MAX_TURNS} turns. You have ${PURSE}.",
                 "Send 5 amounts: oxen(200-300) food ammo clothes supplies",
                 "e.g. 250 200 60 80 40. The rest is cash. [?]Rules [0]Exit"]
    elif state['phase'] == 'ended':
        if state['outcome'] == 'arrived':
            lines = [f"Arrived in {state['turn']} turns. " + _stores(state),
                     f"Score {score(state)}. [1]Go again [0]Exit"]
        else:
            lines = [f"The journey ended at mile {state['miles']}, turn {state['turn']}.",
                     "[1]Set out again [0]Exit"]
    else:
        fort = " [3]Fort" if at_fort(state) else ""
        lines = [f"Turn {state['turn']}/{MAX_TURNS}, mile {state['miles']}/{TRAIL_MILES}.",
                 _stores(state),
                 f"[1]Travel [2]Hunt{fort}, then meal 1-3. e.g. 1 2 [?]Rules [0]Exit"]
    screen = "\n".join(lines)
    if not note:
        return screen
    if door_kit.fits(f"{note}\n{screen}"):
        return f"{note}\n{screen}"
    # What happened on the trail goes ahead as its own message, whole.
    return door_kit.MESSAGE_SEPARATOR.join(door_kit.pack([note]) + [screen])


# ── Door ────────────────────────────────────────────────────────────────────

def respond(state, word) -> str:
    try:
        numbers = [int(part) for part in word.replace(',', ' ').replace('$', ' ').split()]
    except ValueError:
        numbers = None
    try:
        if state['phase'] == 'outfit':
            if not numbers or len(numbers) != 5:
                return "Send five amounts: oxen food ammo clothes supplies."
            outfit(state, *numbers)
            return "You set out from Independence."
        if not numbers:
            return "Send an action and a meal, e.g. 1 2."
        action = numbers[0]
        if action == 3:
            if len(numbers) not in (5, 6):
                return "At a fort send: 3 food ammo clothes supplies meal."
            buys = tuple(numbers[1:5])
            meal = numbers[5] if len(numbers) == 6 else 2
        else:
            if len(numbers) > 2:
                return "Send an action and a meal, e.g. 1 2."
            buys = ()
            meal = numbers[1] if len(numbers) == 2 else 2
        return " ".join(play_turn(state, action, meal, buys))
    except Refused as refused:
        return str(refused)


def handle(user_id, text, short_name, nav=None):
    return door_kit.handle(sys.modules[__name__], user_id, text, short_name)
