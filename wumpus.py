"""Hunt the Wumpus: a cave of twenty rooms, and something asleep in one.

Gregory Yob's 1973 game, played by number. Every room has three tunnels.
Somewhere are the Wumpus, two bottomless pits and two rooms of bats; the
player has five arrows and only their senses -- a smell, a draft, a rustle
-- to say what is next door. A turn is one number to move, or "s" and up to
five room numbers to send a crooked arrow through them.

The rules are the classic ones; every word of text is new. The engine is
pure and seeded, like the other small games.
"""
import sys

import door_kit

GAME_ID = 'wumpus'
COMMAND = 'WUMPUS'
NAME = 'Hunt the Wumpus'
SAVE_VERSION = 1

ARROWS = 5
MAX_ARROW_ROOMS = 5

# The classic cave: the corners of a dodecahedron, three tunnels each.
CAVE = {
    1: (2, 5, 8), 2: (1, 3, 10), 3: (2, 4, 12), 4: (3, 5, 14), 5: (1, 4, 6),
    6: (5, 7, 15), 7: (6, 8, 17), 8: (1, 7, 9), 9: (8, 10, 18), 10: (2, 9, 11),
    11: (10, 12, 19), 12: (3, 11, 13), 13: (12, 14, 20), 14: (4, 13, 15),
    15: (6, 14, 16), 16: (15, 17, 20), 17: (7, 16, 18), 18: (9, 17, 19),
    19: (11, 18, 20), 20: (13, 16, 19),
}

RULES = ("Find the Wumpus and shoot it. Send a room number to move. Send s "
         "and 1-5 rooms to shoot, e.g. s 4 14. Smell = Wumpus near, draft = "
         "pit near, rustle = bats near. A miss may wake it.")


# ── Engine ──────────────────────────────────────────────────────────────────

def new_game(seed) -> dict:
    state = {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
             'phase': 'play', 'outcome': '', 'arrows': ARROWS, 'moves': 0}
    # Six different rooms: nobody starts on top of anything.
    rooms = []
    while len(rooms) < 6:
        room = door_kit.draw(state, 1, 20)
        if room not in rooms:
            rooms.append(room)
    state['room'], state['wumpus'] = rooms[0], rooms[1]
    state['pits'], state['bats'] = rooms[2:4], rooms[4:6]
    return state


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    for key in ('room', 'wumpus'):
        if state[key] not in CAVE:
            raise ValueError(f"bad {key}")
    if len(state['pits']) != 2 or len(state['bats']) != 2:
        raise ValueError("bad hazards")
    if state['phase'] not in ('play', 'ended'):
        raise ValueError("bad phase")
    return state


def senses(state) -> list:
    """What the rooms next door give away, never which one."""
    near = CAVE[state['room']]
    found = []
    if state['wumpus'] in near:
        found.append("You smell something foul.")
    if any(pit in near for pit in state['pits']):
        found.append("You feel a draft.")
    if any(bat in near for bat in state['bats']):
        found.append("Wings rustle nearby.")
    return found


def _end(state, outcome) -> None:
    state['phase'], state['outcome'] = 'ended', outcome


def _wake(state, events) -> None:
    """A startled Wumpus moves three times in four, and eats whoever it
    finds where it stops."""
    if door_kit.draw(state, 1, 4) > 1:
        state['wumpus'] = CAVE[state['wumpus']][door_kit.draw(state, 0, 2)]
        events.append("The Wumpus shuffles off in the dark.")
    if state['wumpus'] == state['room']:
        events.append("It finds you first.")
        _end(state, 'eaten')


def _arrive(state, events) -> None:
    """Whatever is in the room the player now stands in."""
    for _ in range(10):  # bats can drop you onto more bats
        if state['room'] == state['wumpus']:
            events.append("You walk into the Wumpus!")
            _wake(state, events)
            return
        if state['room'] in state['pits']:
            events.append("The floor is gone. You fall.")
            _end(state, 'pit')
            return
        if state['room'] in state['bats']:
            state['room'] = door_kit.draw(state, 1, 20)
            events.append(f"Bats lift you and drop you in room {state['room']}.")
            continue
        return


def move(state, room) -> list:
    """Walk to an adjoining room. Returns what happened, in order."""
    if room not in CAVE[state['room']]:
        raise ValueError(f"No tunnel to {room}. From here: "
                         + " ".join(map(str, CAVE[state['room']])) + ".")
    state['moves'] += 1
    state['room'] = room
    events = []
    _arrive(state, events)
    return events


def shoot(state, path) -> list:
    """Send an arrow through up to five rooms. A room the arrow cannot reach
    from where it is sends it down a tunnel at random, as it always has."""
    if not 1 <= len(path) <= MAX_ARROW_ROOMS:
        raise ValueError(f"Name 1 to {MAX_ARROW_ROOMS} rooms for the arrow.")
    if any(room not in CAVE for room in path):
        raise ValueError("Rooms are numbered 1 to 20.")
    state['moves'] += 1
    state['arrows'] -= 1
    events, at = [], state['room']
    for room in path:
        at = room if room in CAVE[at] else CAVE[at][door_kit.draw(state, 0, 2)]
        if at == state['wumpus']:
            events.append(f"A howl from room {at}. The Wumpus is dead!")
            _end(state, 'won')
            return events
        if at == state['room']:
            events.append("The arrow comes back round and finds you.")
            _end(state, 'arrow')
            return events
    events.append("The arrow clatters away. A miss.")
    _wake(state, events)
    if state['phase'] == 'play' and state['arrows'] == 0:
        events.append("That was your last arrow.")
        _end(state, 'unarmed')
    return events


def score(state) -> int:
    """Fewer moves and more arrows kept is a better hunt."""
    if state['outcome'] != 'won':
        return 0
    return max(10, 200 - 4 * state['moves'] + 20 * state['arrows'])


def result(state):
    """Only a kill is a result; the cave keeps no list of the lost."""
    return (score(state), state['moves']) if state['outcome'] == 'won' else None


# ── Screens ─────────────────────────────────────────────────────────────────

def render(state, note='') -> str:
    lines = [note] if note else []
    if state['phase'] == 'ended':
        if state['outcome'] == 'won':
            lines.append(f"The hunt is over in {state['moves']} moves. "
                         f"Score {score(state)}.")
        else:
            lines.append(f"The hunt ends here, after {state['moves']} moves.")
        lines.append("[1]Hunt again [0]Exit")
    else:
        tunnels = " ".join(map(str, CAVE[state['room']]))
        lines.append(f"Room {state['room']}. Tunnels: {tunnels}. "
                     f"Arrows {state['arrows']}.")
        lines += senses(state)
        lines.append("Room number to move, s + rooms to shoot. [?]Rules [0]Exit")
    screen = "\n".join(lines)
    if note and not door_kit.fits(screen):
        return door_kit.MESSAGE_SEPARATOR.join(
            door_kit.pack([note]) + ["\n".join(lines[1:])])
    return screen


# ── Door ────────────────────────────────────────────────────────────────────

def respond(state, word) -> str:
    """Apply one line of input; return the note for the next screen."""
    parts = word.replace(',', ' ').split()
    shooting = parts[0] in ('s', 'shoot')
    try:
        rooms = [int(part) for part in (parts[1:] if shooting else parts)]
    except ValueError:
        return "Send a room number to move, or s and rooms to shoot."
    try:
        if shooting:
            events = shoot(state, rooms)
        elif len(rooms) == 1:
            events = move(state, rooms[0])
        else:
            return "One room to move. To shoot, start with s."
    except ValueError as refused:
        return str(refused)
    return " ".join(events)


def handle(user_id, text, short_name, nav=None):
    return door_kit.handle(sys.modules[__name__], user_id, text, short_name)
