"""Door games: the one place that knows which sessions are doors.

A door session owns its input outright -- inside one, "N" is north or "next
question" rather than Ask Nomad, and a "!" command is the game's to read.
The router has to know that in four separate places, and each used to carry
its own copy of the list ('ZORK', 'TRIVIA', 'BACONFALL', 'DOPEWARS'), with
an if/elif chain beside two of them. A fifth game meant finding all four.

Now a game registers once, where its handler is defined, and the router asks
here. This module imports nothing from the BBS, so anything may import it.
"""
import sys

# state['command'] -> handler(sender_id, message, interface)
_HANDLERS: dict = {}
# game_id -> launcher(sender_id, interface), for games opened from the menu
_LAUNCHERS: dict = {}


def register(command: str, handler, game_id: str = None, launch=None) -> None:
    """Make *command* a door session, answered by *handler*.

    *launch* opens the game from the Games menu for *game_id*. A door with
    several titles behind one command (the Z-machine games) leaves both out
    and keeps its own way in.
    """
    _HANDLERS[command] = handler
    if game_id is not None and launch is not None:
        _LAUNCHERS[game_id] = launch


def commands() -> tuple:
    return tuple(_HANDLERS)


def is_door(command) -> bool:
    return command in _HANDLERS


def in_session(state) -> bool:
    """True when this user's state is an open door game."""
    return bool(state and state.get('command') in _HANDLERS)


def _current(handler):
    """The function this name refers to now, not when it was registered.

    A module-level handler is looked up again by name, so replacing it --
    a test's mock, or a fix applied to a running process -- takes effect
    here exactly as it would at an ordinary call site.
    """
    module = sys.modules.get(getattr(handler, '__module__', None))
    return getattr(module, getattr(handler, '__name__', ''), handler)


def step(state, sender_id, message, interface) -> bool:
    """Hand one message to the door in *state*. False if it is not a door."""
    handler = _HANDLERS.get((state or {}).get('command'))
    if handler is None:
        return False
    _current(handler)(sender_id, message, interface)
    return True


def launch(game_id, sender_id, interface) -> bool:
    """Open *game_id* from the menu. False if it has no registered launcher."""
    launcher = _LAUNCHERS.get(game_id)
    if launcher is None:
        return False
    launcher(sender_id, interface)
    return True
