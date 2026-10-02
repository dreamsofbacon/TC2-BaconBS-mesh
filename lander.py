"""Lunar Lander: set a craft down on the Moon with the fuel you have.

The 1969 teletype game. Each step is five seconds: the Moon pulls, the
engine pushes back by however much fuel is burned, and the craft either
arrives gently or does not. A turn is one burn -- or several at once,
"20 20 16", which over a radio is the difference between twelve round trips
and four.

Every word of text is new. The engine is pure; it uses no random numbers at
all, so the same burns always fly the same descent.
"""
import sys

import door_kit

GAME_ID = 'lander'
COMMAND = 'LANDER'
NAME = 'Lunar Lander'
SAVE_VERSION = 1

STEP_SECONDS = 5
GRAVITY = 1.6            # m/s^2, so 8 m/s gained each step
THRUST = 0.5             # m/s shed per unit of fuel burned
MAX_BURN = 40            # units per step
HOVER_BURN = 16          # the burn that exactly cancels gravity
START = {'alt': 1500.0, 'speed': 40.0, 'fuel': 400}
MAX_BURNS_PER_MESSAGE = 6
# 0 is a burn here, so it cannot also mean exit.
EXIT_WORDS = {'x', '!x', 'q', 'quit', 'exit'}

SOFT, FIRM, HARD = 2.0, 5.0, 10.0   # m/s at touchdown

RULES = (f"Land at {SOFT:g} m/s or less. Each step is {STEP_SECONDS}s: gravity "
         f"adds 8 m/s, each fuel unit burned sheds 0.5. Burn 0-{MAX_BURN}; "
         f"{HOVER_BURN} holds your speed. Send one burn or several: 20 20 16.")


# ── Engine ──────────────────────────────────────────────────────────────────

def new_game(seed=0) -> dict:
    return {'v': SAVE_VERSION, 'seed': int(seed), 'draws': 0,
            'phase': 'play', 'outcome': '', 'steps': 0, 'impact': 0.0, **START}


def validate(state) -> dict:
    if state.get('v') != SAVE_VERSION:
        raise ValueError(f"save version {state.get('v')!r}")
    if state['phase'] not in ('play', 'ended'):
        raise ValueError("bad phase")
    for key in ('alt', 'speed', 'fuel', 'steps'):
        if not isinstance(state[key], (int, float)):
            raise ValueError(f"bad {key}")
    return state


def _outcome(speed) -> str:
    if speed <= SOFT:
        return 'soft'
    if speed <= FIRM:
        return 'firm'
    if speed <= HARD:
        return 'hard'
    return 'crash'


def step(state, burn) -> None:
    """Fly one step with this burn. Speed is metres a second downward."""
    burn = min(burn, state['fuel'])
    state['fuel'] -= burn
    state['steps'] += 1
    accel = GRAVITY - burn * THRUST / STEP_SECONDS       # downward, m/s^2
    speed, alt = state['speed'], state['alt']
    fallen = speed * STEP_SECONDS + 0.5 * accel * STEP_SECONDS ** 2
    if fallen < alt:
        state['alt'] = round(alt - fallen, 1)
        state['speed'] = round(speed + accel * STEP_SECONDS, 1)
        return
    # The ground arrives partway through the step. Solve for the moment:
    # alt = speed*t + accel*t^2/2, taking the first time it is true.
    if abs(accel) < 1e-9:
        moment = alt / speed
    else:
        root = max(0.0, speed * speed + 2 * accel * alt) ** 0.5
        moment = (-speed + root) / accel
    state['impact'] = round(max(0.0, speed + accel * moment), 1)
    state['alt'], state['speed'] = 0.0, state['impact']
    state['phase'], state['outcome'] = 'ended', _outcome(state['impact'])


def fly(state, burns) -> None:
    """Fly each burn in turn, stopping at the ground. With the tank dry
    there is nothing left to decide, so the craft falls the rest of the way."""
    for burn in burns:
        if state['phase'] != 'play':
            return
        step(state, burn)
    while state['phase'] == 'play' and state['fuel'] == 0:
        step(state, 0)


def check(burns) -> None:
    if not 1 <= len(burns) <= MAX_BURNS_PER_MESSAGE:
        raise ValueError(f"Send 1 to {MAX_BURNS_PER_MESSAGE} burns.")
    if any(not 0 <= burn <= MAX_BURN for burn in burns):
        raise ValueError(f"A burn is 0 to {MAX_BURN}.")


def score(state) -> int:
    """A gentler touchdown first, then the fuel brought home."""
    if state['outcome'] in ('', 'crash'):
        return 0
    return int((HARD - state['impact']) * 30) + state['fuel']


def result(state):
    """A landing you walk away from is a result. A crater is not."""
    if state['outcome'] in ('', 'crash'):
        return None
    return score(state), state['steps']


# ── Screens ─────────────────────────────────────────────────────────────────

_VERDICTS = {
    'soft': "A perfect landing.",
    'firm': "Down safely, with a jolt.",
    'hard': "Down hard. The craft will not fly again, but you will.",
    'crash': "A new crater.",
}


def _speed(speed) -> str:
    if speed < 0:
        return f"{-speed:g} m/s up"
    return f"{speed:g} m/s down"


def render(state, note='') -> str:
    lines = [note] if note else []
    if state['phase'] == 'ended':
        lines.append(f"Touchdown at {state['impact']:g} m/s after "
                     f"{state['steps'] * STEP_SECONDS}s. {_VERDICTS[state['outcome']]}")
        if state['outcome'] != 'crash':
            lines.append(f"Fuel left {state['fuel']}. Score {score(state)}.")
        lines.append("[1]Fly again [X]Exit")
    else:
        lines.append(f"Alt {state['alt']:g} m. Speed {_speed(state['speed'])}. "
                     f"Fuel {state['fuel']}.")
        lines.append(f"Burn 0-{MAX_BURN} per {STEP_SECONDS}s; {HOVER_BURN} holds speed.")
        lines.append("Send a burn, or several: 20 20 16  [?]Rules [X]Exit")
    screen = "\n".join(lines)
    if note and not door_kit.fits(screen):
        return door_kit.MESSAGE_SEPARATOR.join(
            door_kit.pack([note]) + ["\n".join(lines[1:])])
    return screen


# ── Door ────────────────────────────────────────────────────────────────────

def respond(state, word) -> str:
    try:
        burns = [int(part) for part in word.replace(',', ' ').split()]
    except ValueError:
        return "Send a burn as a number, or several: 20 20 16."
    try:
        check(burns)
    except ValueError as refused:
        return str(refused)
    fly(state, burns)
    return ''


def handle(user_id, text, short_name, nav=None):
    return door_kit.handle(sys.modules[__name__], user_id, text, short_name)
