"""Oregon Trail: 2040 miles, a wagon, and eighteen fortnights before winter.

The 1971 teletype game, played by menu like everything else on the BBS.
First the general store: pick an item, read what it is for, say how many
dollars to spend -- or take the ready-made outfit. Then each turn is a
choice off the trail screen: travel on, hunt, set the rations, or stop at a
fort when there is one. [?] on any screen explains that screen.

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

RULES = ("Reach Oregon, 2040 miles off, in 18 turns. Buy an outfit, then each "
         "turn travel, hunt or trade at a fort. Choose by number; [?] explains "
         "any screen. Run out of food, or medicine when ill, and it ends.")

ITEMS = ('oxen', 'food', 'ammo', 'clothes', 'supplies')
LABELS = {'oxen': 'Oxen', 'food': 'Food', 'ammo': 'Ammo', 'clothes': 'Clothes',
          'supplies': 'Supplies'}
# A balanced outfit for anyone who would rather not choose. It is the one the
# tests show arriving nearly nine times in ten.
READY_MADE = {'oxen': 250, 'food': 220, 'ammo': 40, 'clothes': 90, 'supplies': 80}
RATIONS = {1: 'poorly', 2: 'moderately', 3: 'well'}
FORTS = ("Fort Kearney", "Fort Laramie", "Fort Bridger", "Fort Hall",
         "Fort Boise", "Fort Walla Walla", "Fort Vancouver", "Fort Dalles",
         "Fort Clatsop")
FORT_STOP_MILES = 45


class Refused(ValueError):
    """Orders that cannot be carried out, said in the player's terms."""


# ── Engine ──────────────────────────────────────────────────────────────────

def new_game(seed) -> dict:
    return {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
            'phase': 'outfit', 'outcome': '', 'turn': 0, 'miles': 0,
            'oxen': 0, 'food': 0, 'ammo': 0, 'clothes': 0, 'supplies': 0,
            'cash': PURSE, 'sick': False, 'meal': 2,
            'cart': {item: 0 for item in ITEMS}, 'fort_stop': False}


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    if state['phase'] not in ('outfit', 'play', 'ended'):
        raise ValueError("bad phase")
    for key in ('turn', 'miles', 'oxen', 'food', 'ammo', 'clothes', 'supplies', 'cash'):
        if not isinstance(state[key], int):
            raise ValueError(f"bad {key}")
    # Saves from before the menus have neither; they start empty.
    state.setdefault('cart', {item: 0 for item in ITEMS})
    state.setdefault('fort_stop', False)
    if set(state['cart']) != set(ITEMS) or any(
            not isinstance(v, int) or v < 0 for v in state['cart'].values()):
        raise ValueError("bad cart")
    return state


# ── The store and the forts ─────────────────────────────────────────────────

def left_to_spend(state) -> int:
    return PURSE - sum(state['cart'].values())


def allocate(state, item, dollars) -> None:
    """Set what the outfit spends on one item. Replaces the earlier amount."""
    if state['phase'] != 'outfit':
        raise Refused("You have already set out.")
    if dollars < 0:
        raise Refused("Amounts cannot be negative.")
    most = left_to_spend(state) + state['cart'][item]
    if dollars > most:
        raise Refused(f"You have ${most} for {LABELS[item].lower()}.")
    if item == 'oxen' and dollars and not OXEN_RANGE[0] <= dollars <= OXEN_RANGE[1]:
        raise Refused(f"Oxen cost ${OXEN_RANGE[0]}-${OXEN_RANGE[1]}.")
    state['cart'][item] = dollars


def set_out(state) -> None:
    """Leave Independence with what is in the cart."""
    cart = state['cart']
    if not OXEN_RANGE[0] <= cart['oxen'] <= OXEN_RANGE[1]:
        raise Refused(f"No wagon moves without oxen: spend ${OXEN_RANGE[0]}-"
                      f"${OXEN_RANGE[1]} on them first.")
    outfit(state, *(cart[item] for item in ITEMS))


def fort_name(state) -> str:
    return FORTS[min(len(FORTS) - 1, state['turn'] // 2 - 1)]


def fort_buy(state, item, dollars) -> int:
    """Buy at a fort. Returns what was got. A fort charges half as much
    again as home, and stopping costs miles, taken when you travel on."""
    if state['phase'] != 'play' or not at_fort(state):
        raise Refused("There is no fort here.")
    if item == 'oxen':
        raise Refused("The fort has no oxen to sell.")
    if not 0 < dollars <= state['cash']:
        raise Refused(f"You have ${state['cash']}.")
    got = dollars * 2 // 3
    if item == 'ammo':
        got *= BULLETS_PER_DOLLAR
    state['cash'] -= dollars
    state[item] += got
    state['fort_stop'] = True
    return got


def set_rations(state, meal) -> None:
    if meal not in MEALS:
        raise Refused("Rations are 1 poorly, 2 moderately or 3 well.")
    state['meal'] = meal


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
    if state.get('fort_stop'):
        # Trading at a fort took the first days of this fortnight.
        progress -= FORT_STOP_MILES
        state['fort_stop'] = False
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

ITEM_HELP = {
    'oxen': "Oxen pull the wagon. Spend $200-$300; more oxen cover more miles a turn.",
    'food': "Food: $1 buys 1 lb. Everyone together eats 13-23 lb a turn, 18 at moderate rations.",
    'ammo': "Ammo: $1 buys 50 bullets. A hunt uses 13-28. Bullets also drive off bandits.",
    'clothes': "Clothes keep out the cold. With under about $25 of them the mountains bring illness.",
    'supplies': "Supplies are medicine and repairs. Run out while someone is ill and the trail ends.",
}

SCREEN_HELP = {
    'outfit': ("Spend your $700 before setting out; what is left goes with you as "
               "cash for forts and doctors. Pick an item to see what it is for. "
               "Not sure? [6] buys a balanced outfit, then [7] sets out."),
    'trail': ("Each Travel is two weeks on the trail; reach mile 2040 by turn 18. "
              "Hunting brings meat but costs bullets and miles. Every other turn "
              "there is a fort. Eating poorly saves food, but illness is worse."),
    'rations': ("Rations are how well everyone eats, from now on. Poorly saves "
                "food but makes illness more likely and worse; well costs food "
                "and keeps people healthy."),
    'fort': ("A fort sells food, ammo, clothes and supplies at half again home's "
             "price. Stopping costs about 45 miles of this turn. Buy, then [5] "
             "to travel on."),
}


def _stores(state) -> str:
    return (f"Food {state['food']}lb Bullets {state['ammo']} Clothes ${state['clothes']} "
            f"Supplies ${state['supplies']} Cash ${state['cash']}")


def home_menu(state) -> str:
    return {'outfit': 'outfit', 'play': 'trail'}.get(state['phase'], 'ended')


def _screen(state, nav) -> list:
    menu = nav.get('menu') or home_menu(state)
    if state['phase'] == 'ended':
        if state['outcome'] == 'arrived':
            return [f"You reached Oregon in {state['turn']} turns! " + _stores(state),
                    f"Score {score(state)}. [1]Set out again [0]Exit"]
        return [f"The journey ended at mile {state['miles']}, turn {state['turn']}.",
                "[1]Set out again [0]Exit"]
    if menu == 'spend':
        item = nav['item']
        most = left_to_spend(state) + state['cart'][item]
        return [ITEM_HELP[item],
                f"Spend how much on {LABELS[item].lower()}? Now ${state['cart'][item]}, "
                f"up to ${most}. Send dollars, or [B]Back"]
    if menu == 'outfit':
        cart = state['cart']
        return [f"General store, Independence. ${left_to_spend(state)} of ${PURSE} left.",
                " ".join(f"{LABELS[i]} ${cart[i]}" for i in ITEMS),
                "[1]Oxen [2]Food [3]Ammo [4]Clothes [5]Supplies",
                "[6]Ready-made outfit [7]Set out [?]Help [0]Exit"]
    if menu == 'rations':
        return [f"Rations, now {RATIONS[state['meal']]}:",
                "[1]Poorly: 13 lb a turn, more illness",
                "[2]Moderately: 18 lb a turn",
                "[3]Well: 23 lb a turn, less illness",
                "[0]Back"]
    if menu == 'fort':
        return [f"{fort_name(state)} trading post. Cash ${state['cash']}.",
                "[1]Food [2]Ammo [3]Clothes [4]Supplies",
                "[5]Travel on [?]Help [0]Back"]
    if menu == 'fort_spend':
        item = nav['item']
        return [ITEM_HELP[item],
                f"At the fort $3 buys what $2 did at home. Spend how much on "
                f"{LABELS[item].lower()}? Up to ${state['cash']}. [B]Back"]
    fort = " [4]Fort" if at_fort(state) else ""
    lines = [f"Turn {state['turn']} of {MAX_TURNS}. Mile {state['miles']} of {TRAIL_MILES}.",
             _stores(state),
             f"Eating {RATIONS[state['meal']]}." + (f" {fort_name(state)} is here."
                                                    if at_fort(state) else ""),
             f"[1]Travel on [2]Hunt [3]Rations{fort} [?]Help [0]Exit"]
    return lines


def render(state, note='', nav=None) -> str:
    """The screen for *nav*, with *note* -- what just happened -- above it.
    A note too long to share the packet goes ahead as its own message."""
    screen = "\n".join(_screen(state, nav or {}))
    if not note:
        return screen
    if door_kit.fits(f"{note}\n{screen}"):
        return f"{note}\n{screen}"
    return door_kit.MESSAGE_SEPARATOR.join(door_kit.pack(note.splitlines()) + [screen])


# ── Door ────────────────────────────────────────────────────────────────────

EXIT_WORDS = door_kit.MENU_EXIT_WORDS
AGAIN = "A new wagon, a new outfit to buy."


def _dollars(word):
    return door_kit.amount(word, "an amount in dollars, like 150")


_choice = door_kit.choice


def step(state, nav, word):
    """One menu choice. Returns (note, next nav, leave)."""
    menu = nav.get('menu') or home_menu(state)

    if menu == 'outfit':
        if word == '0':
            return '', {}, True
        choice = _choice(word, 7)
        if choice <= 5:
            return '', {'menu': 'spend', 'item': ITEMS[choice - 1]}, False
        if choice == 6:
            state['cart'] = dict(READY_MADE)
            return ("The storekeeper loads a balanced outfit. "
                    "[7] sets out, or change any item."), {}, False
        set_out(state)
        return f"You set out from Independence with ${state['cash']} in hand.", {}, False

    if menu == 'spend':
        if word in door_kit.BACK_WORDS:
            return '', {}, False
        allocate(state, nav['item'], _dollars(word))
        return f"{LABELS[nav['item']]}: ${state['cart'][nav['item']]}.", {}, False

    if menu == 'rations':
        if word == '0':
            return '', {}, False
        set_rations(state, _choice(word, 3))
        return f"Everyone eats {RATIONS[state['meal']]} from now on.", {}, False

    if menu == 'fort':
        if word == '0':
            return '', {}, False
        choice = _choice(word, 5)
        if choice == 5:
            return " ".join(play_turn(state, 1, state['meal'])), {}, False
        return '', {'menu': 'fort_spend', 'item': ITEMS[choice]}, False

    if menu == 'fort_spend':
        if word in door_kit.BACK_WORDS:
            return '', {'menu': 'fort'}, False
        item = nav['item']
        got = fort_buy(state, item, _dollars(word))
        unit = " bullets" if item == 'ammo' else (" lb" if item == 'food' else "")
        amount = f"{got}{unit}" if unit else f"${got} worth"
        return f"Bought {amount} of {LABELS[item].lower()}.", {'menu': 'fort'}, False

    # The trail.
    if word == '0':
        return '', {}, True
    top = 4 if at_fort(state) else 3
    choice = _choice(word, top)
    if choice == 1:
        return " ".join(play_turn(state, 1, state['meal'])), {}, False
    if choice == 2:
        return " ".join(play_turn(state, 2, state['meal'])), {}, False
    if choice == 3:
        return '', {'menu': 'rations'}, False
    return '', {'menu': 'fort'}, False


def respond(state, word) -> str:
    """One choice from the screen a player is on when they arrive: the
    menu door contract, for callers that keep no screen of their own."""
    note, _nav, _leave = step(state, {}, word)
    return note


def screen_help(state, nav):
    """More than the rules say, about the screen the player is on. An
    amount screen already shows its item's line, so its help is the help
    for the shop it belongs to."""
    menu = nav.get('menu') or home_menu(state)
    return SCREEN_HELP[{'spend': 'outfit', 'fort_spend': 'fort',
                        'ended': 'trail'}.get(menu, menu)]


def handle(user_id, text, short_name, nav=None):
    """One message in, one reply out: (reply, leave, nav)."""
    return door_kit.menu_handle(sys.modules[__name__], user_id, text, short_name, nav)
