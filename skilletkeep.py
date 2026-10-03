"""Skillet Keep: a daily-turn adventure in the Baconfall world.

The dial-up door games were built for exactly this: a few turns a day,
picked up where you left them, in a town other callers pass through too.
Each day gives twelve turns. Spend them in the Wilds for gold and
experience, buy a better blade and better armor, rest at the Inn, keep gold
safe in the Bank, and when you have earned it, face the Warden of your
level. Ten Wardens stand between a new arrival and the Ash King.

Built for one packet a turn. A fight is resolved whole and reported in one
message -- who won, in how many blows, what it cost -- because trading
blows one at a time would spend a radio round trip on every swing.

The engine is pure and seeded. The town is shared: the Board lists who else
is in the keep and what happened lately, and a Duel is fought against
another player's current strength without touching their save. Saves
travel with the fleet's save sync, so the roster grows to include players
from every node that syncs them; the news stays local to each node.
"""
import sys

import door_kit

GAME_ID = 'skilletkeep'
COMMAND = 'SKILLETKEEP'
NAME = 'Skillet Keep'
SAVE_VERSION = 1

TURNS_PER_DAY = 12
MAX_LEVEL = 10
EXIT_WORDS = {'x', '!x', 'q', 'quit', 'exit'}

BLADES = ("Wooden Spoon", "Cleaver", "Iron Skillet", "Saltsteel Fang",
          "Ember Blade", "First Flame Brand")
ARMORS = ("Apron", "Leather Jerkin", "Rind Mail", "Saltglass Plate",
          "Cinder Guard", "Ashproof Aegis")
GEAR_PRICES = (0, 120, 450, 1100, 2400, 5000)

MONSTERS = ("Grease Imp", "Char Hound", "Rind Raider", "Pepper Bandit",
            "Salt Revenant", "Dune Gnasher", "Smoke Wraith", "Cinder Knight",
            "Skillet Golem", "Ash Reaver", "Ember Drake", "Hollow Butcher")
WARDENS = ("the Gate Warden", "the Boar of Cinders", "the Hickory Warden",
           "the Salt Wyrm", "the Dune Warden", "the Hollow Butcher",
           "the Skillet Warden", "the Smoke Tyrant", "the Cinder Lord",
           "the Ash King")

DEPTHS = {1: ("Near", -1), 2: ("Deep", 0), 3: ("Far", 2)}

RULES = ("12 turns a day. Fight in the Wilds for gold and xp, buy gear at the "
         "Smith, heal at the Inn, bank gold to keep it. Earn enough xp, then "
         "beat your Warden to level up. Fall and you lose half your gold.")


# ── Numbers ─────────────────────────────────────────────────────────────────

def max_hp(state) -> int:
    return 30 + 12 * state['level']


def attack(state) -> int:
    return 5 + 3 * state['level'] + 4 * state['blade']


def defense(state) -> int:
    return state['level'] + 3 * state['armor']


def xp_needed(level) -> int:
    """Experience to be let in to see this level's Warden."""
    return 40 * level * level + 40 * level


def inn_price(state) -> int:
    return 8 * state['level']


def monster(state, depth) -> dict:
    """What the Wilds send out at this depth, for this player's level."""
    level = max(1, state['level'] + DEPTHS[depth][1])
    name = MONSTERS[min(len(MONSTERS) - 1, level - 1 + door_kit.draw(state, 0, 1))]
    return {'name': name, 'hp': 10 + 7 * level + door_kit.draw(state, 0, 2 * level),
            'attack': 3 + 2 * level, 'defense': level - 1,
            'gold': 10 * level + door_kit.draw(state, 0, 6 * level),
            'xp': 8 * level + 4}


def warden(level) -> dict:
    return {'name': WARDENS[level - 1], 'hp': 26 + 16 * level,
            'attack': 5 + 3 * level, 'defense': level,
            'gold': 60 * level, 'xp': 0}


def fight(state, foe) -> dict:
    """Trade blows until one side is down. The player strikes first.

    Returns ``{'won', 'blows', 'lost'}`` and leaves the player's HP reduced.
    Every blow lands for at least 1, so a fight always ends.
    """
    foe_hp, start, blows = foe['hp'], state['hp'], 0
    while True:
        blows += 1
        foe_hp -= max(1, attack(state) + door_kit.draw(state, -2, 2) - foe['defense'])
        if foe_hp <= 0:
            return {'won': True, 'blows': blows, 'lost': start - state['hp']}
        state['hp'] -= max(1, foe['attack'] + door_kit.draw(state, -2, 2) - defense(state))
        if state['hp'] <= 0:
            state['hp'] = 0
            return {'won': False, 'blows': blows, 'lost': start}


# ── State ───────────────────────────────────────────────────────────────────

def new_game(seed) -> dict:
    state = {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
             'day': '', 'days': 0, 'name': '',
             'level': 1, 'xp': 0, 'renown': 0, 'crowns': 0,
             'hp': 0, 'gold': 30, 'bank': 0, 'blade': 0, 'armor': 0,
             'turns': TURNS_PER_DAY, 'down': False, 'dueled': False,
             'kills': 0, 'falls': 0}
    state['hp'] = max_hp(state)
    return state


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    for key in ('level', 'xp', 'hp', 'gold', 'bank', 'blade', 'armor', 'turns'):
        if not isinstance(state[key], int) or state[key] < 0:
            raise ValueError(f"bad {key}")
    if not 1 <= state['level'] <= MAX_LEVEL or state['blade'] > 5 or state['armor'] > 5:
        raise ValueError("out of range")
    return state


def new_day(state, day) -> bool:
    """Start *day* if it has not been started: fresh turns, and anyone who
    fell yesterday is back on their feet. True if the day turned."""
    if state['day'] == day:
        return False
    state.update(day=day, days=state['days'] + 1, turns=TURNS_PER_DAY,
                 down=False, dueled=False, hp=max_hp(state))
    return True


def _fall(state, events, news, by) -> None:
    lost = state['gold'] // 2
    state['gold'] -= lost
    state.update(turns=0, down=True, falls=state['falls'] + 1)
    events.append(f"You fall to {by}. You are carried home, {lost} gold lighter. "
                  "Rest until tomorrow.")
    news.append(f"{state['name']} fell to {by}.")


def _spend_turn(state) -> None:
    if state['down']:
        raise ValueError("You are resting until tomorrow.")
    if state['turns'] <= 0:
        raise ValueError("No turns left today. Come back tomorrow.")
    state['turns'] -= 1


def explore(state, depth, events, news) -> None:
    """One turn in the Wilds: a whole fight, reported in a sentence."""
    if depth not in DEPTHS:
        raise ValueError("Choose 1, 2 or 3.")
    _spend_turn(state)
    foe = monster(state, depth)
    outcome = fight(state, foe)
    if not outcome['won']:
        _fall(state, events, news, f"a {foe['name']}")
        return
    state['gold'] += foe['gold']
    state['xp'] += foe['xp']
    state['renown'] += foe['xp']
    state['kills'] += 1
    events.append(f"You beat a {foe['name']} in {outcome['blows']} blows, "
                  f"-{outcome['lost']} HP. +{foe['gold']} gold +{foe['xp']} xp.")


def challenge(state, events, news) -> None:
    """Face this level's Warden. Winning is how a level is gained."""
    need = xp_needed(state['level'])
    if state['xp'] < need:
        raise ValueError(f"The Warden will not see you until {need} xp. "
                         f"You have {state['xp']}.")
    _spend_turn(state)
    foe = warden(state['level'])
    outcome = fight(state, foe)
    if not outcome['won']:
        _fall(state, events, news, foe['name'])
        return
    state['gold'] += foe['gold']
    if state['level'] >= MAX_LEVEL:
        state['crowns'] += 1
        state['renown'] += 500
        events.append(f"The Ash King falls! The First Flame is home. You take "
                      f"crown {state['crowns']} and begin again, wiser.")
        news.append(f"{state['name']} defeated the Ash King!")
        state.update(level=1, xp=0, blade=0, armor=0)
        state['hp'] = max_hp(state)
        return
    state['level'] += 1
    state['xp'] = 0
    state['hp'] = max_hp(state)
    events.append(f"You beat {foe['name']} in {outcome['blows']} blows! "
                  f"Level {state['level']}. +{foe['gold']} gold. Fully healed.")
    news.append(f"{state['name']} beat {foe['name']} and reached level {state['level']}.")


def rest(state, events) -> None:
    if state['down']:
        raise ValueError("You are resting until tomorrow.")
    if state['hp'] >= max_hp(state):
        raise ValueError("You are already rested.")
    price = inn_price(state)
    if state['gold'] < price:
        raise ValueError(f"A bed costs {price} gold. You carry {state['gold']}.")
    state['gold'] -= price
    state['hp'] = max_hp(state)
    events.append(f"You rest at the Inn. HP full. -{price} gold.")


def buy(state, slot, events) -> None:
    """Buy the next blade or the next armor; there is no skipping ahead."""
    key, names = (('blade', BLADES) if slot == 1 else ('armor', ARMORS))
    tier = state[key] + 1
    if tier >= len(names):
        raise ValueError("The Smith has nothing better.")
    price = GEAR_PRICES[tier]
    if state['gold'] < price:
        raise ValueError(f"The {names[tier]} costs {price} gold. You carry {state['gold']}.")
    state['gold'] -= price
    state[key] = tier
    events.append(f"You buy the {names[tier]}.")


def bank(state, choice, events) -> None:
    if choice == 1:
        if not state['gold']:
            raise ValueError("You carry no gold.")
        events.append(f"You deposit {state['gold']} gold.")
        state['bank'] += state['gold']
        state['gold'] = 0
    elif choice == 2:
        if not state['bank']:
            raise ValueError("Your account is empty.")
        events.append(f"You withdraw {state['bank']} gold.")
        state['gold'] += state['bank']
        state['bank'] = 0
    else:
        raise ValueError("Choose 1 or 2.")


def duel(state, rival, events, news) -> None:
    """A sparring match against another player as they stand now.

    Once a day, and it costs a turn. The rival's own save is not touched:
    they lose nothing and gain nothing, and read about it on the Board.
    """
    if state['dueled']:
        raise ValueError("One duel a day.")
    if rival is None:
        raise ValueError("Nobody else is in the keep to duel.")
    _spend_turn(state)
    state['dueled'] = True
    foe = {'name': rival['name'], 'hp': max_hp(rival), 'attack': attack(rival),
           'defense': defense(rival)}
    outcome = fight(state, foe)
    if not outcome['won']:
        # A duel is to the first fall, not to ruin: no gold is lost.
        state['hp'] = 1
        events.append(f"{foe['name']} beats you in the yard. You limp off with 1 HP.")
        news.append(f"{foe['name']} beat {state['name']} in a duel.")
        return
    bounty = 15 * rival['level']
    state['gold'] += bounty
    events.append(f"You beat {foe['name']} in {outcome['blows']} blows, "
                  f"-{outcome['lost']} HP. The crowd throws {bounty} gold.")
    news.append(f"{state['name']} beat {foe['name']} in a duel.")


def score(state) -> int:
    """Lifetime experience: it only ever goes up, crowns and all."""
    return state['renown']


# ── Screens ─────────────────────────────────────────────────────────────────

def _town(state) -> list:
    return [f"{NAME}: {state['name'] or 'Stranger'} Lv{state['level']} "
            f"HP {state['hp']}/{max_hp(state)} XP {state['xp']}/{xp_needed(state['level'])}",
            f"Gold {state['gold']} Bank {state['bank']} Turns {state['turns']}/{TURNS_PER_DAY}",
            "[1]Wilds [2]Smith [3]Inn [4]Bank [5]Warden [6]Board [7]Duel",
            "[?]Rules [X]Exit"]


def _wilds(state) -> list:
    return [f"The Wilds. HP {state['hp']}/{max_hp(state)}, {state['turns']} turns left.",
            "[1]Near, easy [2]Deep, fair [3]Far, hard and rich",
            "[0]Back to the keep"]


def _smith(state) -> list:
    def offer(key, names):
        tier = state[key] + 1
        if tier >= len(names):
            return f"{names[state[key]]}: the best there is"
        return f"{names[state[key]]} -> {names[tier]}, {GEAR_PRICES[tier]} gold"
    return [f"The Smith. You carry {state['gold']} gold.",
            "[1]Blade: " + offer('blade', BLADES),
            "[2]Armor: " + offer('armor', ARMORS),
            "[0]Back"]


def _bank(state) -> list:
    return [f"The Bank. Carried {state['gold']}, banked {state['bank']}.",
            "[1]Deposit all [2]Withdraw all [0]Back"]


_SCREENS = {'town': _town, 'wilds': _wilds, 'smith': _smith, 'bank': _bank}


def render(state, menu='town', note='') -> str:
    screen = "\n".join(_SCREENS.get(menu, _town)(state))
    if not note:
        return screen
    if door_kit.fits(f"{note}\n{screen}"):
        return f"{note}\n{screen}"
    # The note may be several lines (the Board); it goes ahead, whole.
    return door_kit.MESSAGE_SEPARATOR.join(door_kit.pack(note.splitlines()) + [screen])


def board(roster, news) -> str:
    """Who is in the keep, strongest first, and what happened lately."""
    lines = ["The Board:"]
    for rank, other in enumerate(roster[:5], start=1):
        crowns = f" {other['crowns']}x crowned" if other.get('crowns') else ""
        lines.append(f"{rank}. {other['name']} Lv{other['level']}{crowns}")
    if len(lines) == 1:
        lines.append("Nobody has signed it yet.")
    lines += news[:3]
    return "\n".join(lines)


# ── Door ────────────────────────────────────────────────────────────────────

def _roster(conn) -> list:
    """Every player in the keep, strongest first. Saves sync between
    nodes, so on a fleet that syncs them this is everyone, everywhere."""
    players = [other for other in door_kit.all_saves(GAME_ID, conn)
               if other.get('name') and isinstance(other.get('level'), int)]
    players.sort(key=lambda p: (-p.get('crowns', 0), -p['level'], -p['xp'], p['name']))
    return players


def _news_table(conn) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS door_news (
        id INTEGER PRIMARY KEY AUTOINCREMENT, game_id TEXT NOT NULL,
        day TEXT NOT NULL, text TEXT NOT NULL)''')


def _rival(roster, state):
    """The other player nearest this one in level."""
    others = [p for p in roster if p['name'] != state['name']]
    if not others:
        return None
    return min(others, key=lambda p: (abs(p['level'] - state['level']), p['name']))


def handle(user_id, text, short_name, nav=None):
    """One message in, one reply out: (reply, leave, nav)."""
    from db_operations import get_db_connection, upsert_game_score

    word = (text or '').strip().lower()
    menu = (nav or {}).get('menu', 'town') if text is not None else 'town'
    if word in EXIT_WORDS:
        return f"{NAME} saved. Your turns keep until midnight UTC.", True, None
    first, new_state = door_kit.first_visit(sys.modules[__name__])

    def turn(state):
        conn = get_db_connection()
        _news_table(conn)
        state['name'] = (short_name or str(user_id))[:16]
        events, news = [], []
        if new_day(state, door_kit.fleet_day()):
            events.append(f"Day {state['days']} in the keep. {TURNS_PER_DAY} turns.")
        level_before, crowns_before = state['level'], state['crowns']
        next_menu = menu
        try:
            if text is None or word == '':
                pass
            elif word in door_kit.HELP_WORDS:
                events.append(RULES)
            elif word == '0':
                next_menu = 'town'
            elif menu == 'wilds':
                explore(state, _number(word), events, news)
                if state['down'] or state['turns'] == 0:
                    next_menu = 'town'
            elif menu == 'smith':
                buy(state, _number(word), events)
            elif menu == 'bank':
                bank(state, _number(word), events)
            else:
                next_menu = _town_choice(state, word, events, news, conn)
        except ValueError as refused:
            events.append(str(refused))

        for line in news:
            conn.execute("INSERT INTO door_news (game_id, day, text) VALUES (?, ?, ?)",
                         (GAME_ID, state['day'], line))
        # The scoreboard is synced to every node, so it is written only when
        # something worth telling the fleet happens, not after every fight.
        if state['level'] != level_before or state['crowns'] != crowns_before:
            upsert_game_score(user_id, GAME_ID, state['name'], score(state), 0,
                              state['days'], commit=False)
        screen = render(state, next_menu, " ".join(events))
        if text is None:
            screen = door_kit.opening(RULES, screen, first)
        return state, (screen, {'menu': next_menu})

    reply, next_nav = door_kit.run_turn(GAME_ID, user_id, new_state, turn, validate)
    return reply, False, next_nav


def _number(word) -> int:
    try:
        return int(word)
    except ValueError:
        raise ValueError("Send the number of your choice.") from None


def _town_choice(state, word, events, news, conn) -> str:
    """What a key does at the town screen. Returns the screen to show next."""
    choice = _number(word)
    if choice == 1:
        if state['down']:
            raise ValueError("You are resting until tomorrow.")
        if state['turns'] <= 0:
            raise ValueError("No turns left today. Come back tomorrow.")
        return 'wilds'
    if choice == 2:
        return 'smith'
    if choice == 3:
        rest(state, events)
    elif choice == 4:
        return 'bank'
    elif choice == 5:
        challenge(state, events, news)
    elif choice == 6:
        lately = [row[0] for row in conn.execute(
            "SELECT text FROM door_news WHERE game_id = ? ORDER BY id DESC LIMIT 3",
            (GAME_ID,))]
        events.append(board(_roster(conn), lately))
    elif choice == 7:
        duel(state, _rival(_roster(conn), state), events, news)
    else:
        raise ValueError("Choose 1 to 7.")
    return 'town'
