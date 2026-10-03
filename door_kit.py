"""What every menu-driven door game needs, in one place.

Three things, each lifted from a game that had already had to solve it:

* Packets. A reply is one or more messages of at most one packet each,
  split between lines or words and never cut short (from Dope Wars).
* Saves. One JSON save per player per game, read and written inside a
  single transaction so two processes cannot interleave a turn, and kept
  where the fleet's save sync will carry it to the other nodes.
* The fleet's day. Daily content is derived from the date, so every node
  shows the same puzzle without a byte of sync traffic.
"""
import hashlib
import json
import zlib
from datetime import datetime, timezone

# One Meshtastic packet of text. A screen over this spends a second packet
# of airtime on every turn that shows it.
MAX_SCREEN_BYTES = 200

# One reply can be several messages. A game's render() separates them with
# this, and messages() turns the reply into what is actually sent.
MESSAGE_SEPARATOR = "\f"


# ── Packets ─────────────────────────────────────────────────────────────────

def split_words(text, budget=MAX_SCREEN_BYTES):
    """One line longer than a packet, as pieces that each fit, split
    between words. Nothing is dropped."""
    pieces, current = [], ""
    for word in text.split(' '):
        candidate = f"{current} {word}" if current else word
        if len(candidate.encode('utf-8')) <= budget:
            current = candidate
            continue
        if current:
            pieces.append(current)
        # A single word wider than a packet, which no real text has, is
        # split by characters rather than lost.
        while len(word.encode('utf-8')) > budget:
            cut = budget
            while len(word[:cut].encode('utf-8')) > budget:
                cut -= 1
            pieces.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        pieces.append(current)
    return pieces


def pack(lines, budget=MAX_SCREEN_BYTES):
    """Lines packed into as few messages as fit one packet each."""
    out, current = [], []
    for line in lines:
        pieces = (split_words(line, budget)
                  if len(line.encode('utf-8')) > budget else [line])
        for piece in pieces:
            if current and len("\n".join(current + [piece]).encode('utf-8')) > budget:
                out.append("\n".join(current))
                current = []
            current.append(piece)
    if current:
        out.append("\n".join(current))
    return out


def messages(reply):
    """A reply as the messages to send: split where the game split it, and
    anything still over a packet split again rather than truncated."""
    return [message for part in reply.split(MESSAGE_SEPARATOR)
            for message in pack(part.split("\n"))]


def fits(text) -> bool:
    return len(text.encode('utf-8')) <= MAX_SCREEN_BYTES


# ── The fleet's day ─────────────────────────────────────────────────────────

def fleet_day(now=None) -> str:
    """Today, as every node agrees on it: the UTC date, 'YYYY-MM-DD'."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime('%Y-%m-%d')


def daily_seed(game_id: str, day: str = None) -> int:
    """A number every node derives identically for this game on this day.

    A hash of the two, not Python's hash() -- that is salted per process
    and would give each node its own puzzle.
    """
    digest = hashlib.sha256(f"{game_id}:{day or fleet_day()}".encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big')


# ── Saves ───────────────────────────────────────────────────────────────────

class SaveUnavailable(ValueError):
    """A save that could not be read. It is left exactly as it was, for the
    operator to look at, rather than replaced with a fresh game."""


# A save is JSON, and JSON is repetitive: a Candy Wars save is mostly sixteen
# copies of {"price":..,"stock":..}. Saves travel between nodes whenever they
# change, at about 110 bytes a radio packet, so they are stored compressed
# when that is smaller -- 959 bytes to 405 for that one, nine packets to four.
#
# The first byte says which form it is. JSON text never starts with 0x01, so
# a plain save -- one written before this, one too small to gain, or one that
# arrived from a node still on older code -- reads exactly as it always did.
_COMPRESSED = b'\x01'


def decode_save(raw) -> dict:
    """A stored save as a dict, whichever form it was stored in.

    Raises ValueError for anything that is not a save, including a
    compressed one that is damaged, so callers have one thing to catch.
    """
    if isinstance(raw, str):
        return json.loads(raw)
    raw = bytes(raw)
    if raw[:1] == _COMPRESSED:
        try:
            raw = zlib.decompress(raw[1:])
        except zlib.error as exc:
            raise ValueError(f"damaged save: {exc}") from exc
    return json.loads(raw.decode('utf-8'))


def encode_save(state) -> bytes:
    """A save as bytes to store: compressed when that is the smaller form."""
    plain = json.dumps(state, separators=(',', ':')).encode('utf-8')
    packed = _COMPRESSED + zlib.compress(plain, 9)
    return packed if len(packed) < len(plain) else plain


_decode, _encode = decode_save, encode_save


def run_turn(game_id, user_id, new_state, turn, load=None):
    """Load this player's save, apply one turn, and write it back.

    ``new_state()`` makes the save for someone who has none. ``turn(state)``
    returns ``(state, result)``; the state is saved and the result handed
    back. ``load(raw_state)`` may validate or migrate a stored save, raising
    to refuse it. Everything runs inside one BEGIN IMMEDIATE transaction, so
    a turn is all or nothing and a score written by ``turn`` commits with it.

    The save goes in the same table the adventure games' saves use, which
    is what the fleet already syncs: where [sync] sync_zork_saves is on, a
    game started on one node can be carried on at another. Nothing is sent
    per turn -- the sync cycle notices the save changed and fetches it, and
    the newer of two copies wins.
    """
    from db_operations import (clear_sync_tombstone, ensure_game_saves_table,
                               get_db_connection, read_game_save, write_game_save)
    from player_identity import player_key

    run_key = player_key(user_id)
    ensure_game_saves_table()
    conn = get_db_connection()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = read_game_save(conn, run_key, game_id)
        if raw is None:
            state = new_state()
        else:
            try:
                state = _decode(raw)
                if load is not None:
                    state = load(state)
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                raise SaveUnavailable(
                    f"{game_id} save could not be read; it has been kept.") from exc
        state, result = turn(state)
        write_game_save(conn, run_key, game_id, _encode(state))
    if raw is None:
        # A save deleted here earlier left a marker that would stop this new
        # one being accepted elsewhere. Only a first save can meet one.
        clear_sync_tombstone('zork_saves', f"{run_key}:{game_id}")
    return result


def load_save(game_id, user_id):
    """This player's save as a dict, or None. For reading, not for a turn."""
    from db_operations import ensure_game_saves_table, get_db_connection, read_game_save
    from player_identity import player_key
    ensure_game_saves_table()
    raw = read_game_save(get_db_connection(), player_key(user_id), game_id)
    return None if raw is None else _decode(raw)


def store_save(game_id, user_id, state) -> None:
    """Replace this player's save outright."""
    from db_operations import ensure_game_saves_table, get_db_connection, write_game_save
    from player_identity import player_key
    ensure_game_saves_table()
    conn = get_db_connection()
    write_game_save(conn, player_key(user_id), game_id, _encode(state))
    conn.commit()


def all_saves(game_id, conn=None) -> list:
    """Every readable save for one game, on any node that has synced here."""
    from db_operations import get_db_connection
    saves = []
    for (raw,) in (conn or get_db_connection()).execute(
            "SELECT save_data FROM zork_saves WHERE game_id = ?", (game_id,)):
        try:
            saves.append(_decode(raw))
        except (ValueError, TypeError):
            continue
    return saves


# ── The door itself ─────────────────────────────────────────────────────────

EXIT_WORDS = {'0', 'x', '!x', 'q', 'quit', 'exit'}
HELP_WORDS = {'?', 'h', 'help', 'rules'}


def handle(game, user_id, text, short_name):
    """One message in, one reply out, for any small menu-driven game.

    Returns ``(reply, leave, nav)``, the shape ``register_menu_door`` wants.
    The game module supplies:

    * ``GAME_ID``, ``NAME`` and ``RULES`` (one packet of how to play)
    * ``new_game(seed)`` and ``validate(state)``
    * ``render(state, note='')``
    * ``respond(state, word)`` -> the note to show above the next screen
      (or ''), after applying one lower-cased, stripped line of input
    * ``result(state)`` -> ``(score, moves)`` for a game worth recording,
      or None for one that is not

    What is the same for every game lives here: leaving saves and exits,
    '?' shows the rules, an ended game offers [1] to play again, and a score
    is written once, in the same transaction as the move that ended it.
    """
    from db_operations import upsert_game_score

    word = (text or '').strip().lower()
    # A game where 0 is a move (a burn of nothing) names its own way out.
    if word in getattr(game, 'EXIT_WORDS', EXIT_WORDS):
        return f"{game.NAME} saved. Carry on from Games.", True, None
    first, new_state = first_visit(game)

    def turn(state):
        if text is None or word == '':
            return state, opening(game.RULES, game.render(state), first)
        if word in HELP_WORDS:
            return state, MESSAGE_SEPARATOR.join(pack([game.RULES]) + [game.render(state)])
        if state['phase'] == 'ended':
            if word == '1':
                state = game.new_game(_seed())
            return state, game.render(state)
        note = game.respond(state, word)
        if state['phase'] == 'ended':
            outcome = game.result(state)
            if outcome is not None:
                upsert_game_score(user_id, game.GAME_ID, short_name or str(user_id),
                                  outcome[0], 0, outcome[1], commit=False)
        return state, game.render(state, note or '')

    reply = run_turn(game.GAME_ID, user_id, new_state, turn, game.validate)
    return reply, False, None


def _seed() -> int:
    import secrets
    return secrets.randbits(63)


def first_visit(game, make=None):
    """A new-save maker that remembers whether it was used. A player with
    no save has never opened this game, so their first screen comes with
    the rules; after that it goes straight to the menu, and [?] brings the
    rules back. Returns ``(first, new_state)``; ``first`` is a list, empty
    until ``new_state`` runs."""
    first = []

    def new_state():
        first.append(True)
        return make() if make is not None else game.new_game(_seed())

    return first, new_state


def opening(rules, screen, first) -> str:
    """The screen, with the rules sent ahead of it on a first visit."""
    return MESSAGE_SEPARATOR.join(pack([rules]) + [screen]) if first else screen


MENU_EXIT_WORDS = {'x', '!x', 'q', 'quit', 'exit'}
# On a screen that asks for an amount, 0 is an amount -- no food, no land,
# no signs -- so the way back is a letter.
BACK_WORDS = {'b', 'back'}


class Refused(ValueError):
    """A choice the game cannot carry out, said in the player's terms."""


def menu_handle(game, user_id, text, short_name, nav=None):
    """One message in, one reply out, for a game played entirely by
    numbered menus. Returns ``(reply, leave, nav)``.

    Here [0] means "back" inside a submenu and "save and leave" on the
    game's main screen, so only the exit words leave from anywhere. The
    game module supplies, beyond what ``handle`` needs:

    * ``render(state, note='', nav=None)`` -- the screen *nav* names
    * ``step(state, nav, word)`` -> ``(note, next_nav, leave)`` for one
      choice, raising ``Refused`` (or the game's own) with what to do instead
    * optionally ``screen_help(state, nav)`` -> more help for that screen,
      sent after the rules when a player asks, or None
    * optionally ``AGAIN``, the note shown when a new game starts

    An ended game offers [1] to play again and [0] to leave, and its score
    is written in the same transaction as the move that ended it.
    """
    from db_operations import upsert_game_score

    word = (text or '').strip().lower()
    if word in getattr(game, 'EXIT_WORDS', MENU_EXIT_WORDS):
        return f"{game.NAME} saved. Carry on from Games.", True, None
    nav = dict(nav or {}) if text is not None else {}
    first, new_state = first_visit(game)
    leaving = (f"{game.NAME} saved. Carry on from Games.", True, {})

    def turn(state):
        if text is None or word == '':
            return state, (opening(game.RULES, game.render(state, '', nav), first),
                           False, nav)
        if word in HELP_WORDS:
            more = getattr(game, 'screen_help', lambda _s, _n: None)(state, nav)
            parts = pack([game.RULES]) + (pack([more]) if more else [])
            return state, (MESSAGE_SEPARATOR.join(parts + [game.render(state, '', nav)]),
                           False, nav)
        if state['phase'] == 'ended':
            if word == '0':
                return state, leaving
            if word != '1':
                return state, (game.render(state, "Choose 1 to play again, or 0 to leave."),
                               False, {})
            state = game.new_game(_seed())
            return state, (game.render(state, getattr(game, 'AGAIN', '')), False, {})
        try:
            note, next_nav, leave = game.step(state, nav, word)
        except (Refused, getattr(game, 'Refused', Refused)) as refused:
            return state, (game.render(state, str(refused), nav), False, nav)
        if leave:
            return state, leaving
        if state['phase'] == 'ended':
            outcome = game.result(state)
            if outcome is not None:
                upsert_game_score(user_id, game.GAME_ID, short_name or str(user_id),
                                  outcome[0], 0, outcome[1], commit=False)
            next_nav = {}
        return state, (game.render(state, note, next_nav), False, next_nav)

    return run_turn(game.GAME_ID, user_id, new_state, turn, game.validate)


def choice(word, top) -> int:
    """A menu number from 1 to *top*, or Refused saying what to send."""
    try:
        number = int(word)
    except ValueError:
        raise Refused(f"Choose 1-{top}.") from None
    if not 1 <= number <= top:
        raise Refused(f"Choose 1-{top}.")
    return number


def amount(word, what="a number") -> int:
    """A whole number someone typed, forgiving $, c, commas and spaces."""
    cleaned = word.replace('$', '').replace(',', '').replace(' ', '').rstrip('c')
    try:
        return int(cleaned)
    except ValueError:
        raise Refused(f"Send {what}.") from None


def render_with_note(note, lines) -> str:
    """*lines* as one screen with *note* above it; a note too long to share
    the packet goes ahead as its own message. Nothing is cut."""
    screen = "\n".join(lines)
    if not note:
        return screen
    if fits(f"{note}\n{screen}"):
        return f"{note}\n{screen}"
    return MESSAGE_SEPARATOR.join(pack(note.splitlines()) + [screen])


def draw(state, low, high) -> int:
    """The next random whole number for a game whose state carries 'seed'
    and 'draws'. A string seed is stable across Python versions and
    platforms, so a saved game replays the same wherever it is loaded."""
    import random
    state['draws'] += 1
    return random.Random(f"{state['seed']}:{state['draws']}").randint(low, high)


# ── Daily puzzles ───────────────────────────────────────────────────────────

def previous_day(day: str) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def new_daily_state() -> dict:
    return {'v': 1, 'day': '', 'guesses': [], 'done': False, 'won': False,
            'streak': 0, 'last_win': ''}


def daily_handle(game, user_id, text, short_name):
    """One message in, one reply out, for a once-a-day puzzle.

    The puzzle is the same for everyone on the fleet's day, and each player
    gets one go at it. The game module supplies ``GAME_ID``, ``NAME``,
    ``RULES``, ``MAX_GUESSES``, ``answer(day)``, ``normalise(text)`` (the
    guess as stored, or ValueError saying what is wrong with it) and
    ``feedback(answer, guess)`` (one short line).

    A streak counts days solved in a row; missing a day, or failing one,
    ends it. The score is the streak first and fewer guesses second, written
    in the same transaction as the guess that solved it.
    """
    from db_operations import upsert_game_score

    word = (text or '').strip().lower()
    if word in EXIT_WORDS:
        return f"{game.NAME}: see you tomorrow.", True, None
    first, new_state = first_visit(game, new_daily_state)

    def turn(state):
        today = fleet_day()
        if state['day'] != today:
            if state['last_win'] != previous_day(today):
                state['streak'] = 0
            state.update(day=today, guesses=[], done=False, won=False)
        if text is None or word == '':
            return state, opening(game.RULES, daily_render(game, state), first)
        if word in HELP_WORDS:
            return state, MESSAGE_SEPARATOR.join(
                pack([game.RULES]) + [daily_render(game, state)])
        if state['done']:
            return state, daily_render(game, state)
        try:
            guess = game.normalise(word)
        except ValueError as refused:
            return state, daily_render(game, state, str(refused))
        if guess in state['guesses']:
            return state, daily_render(game, state, "You tried that already.")
        state['guesses'].append(guess)
        if guess == game.answer(today):
            state['streak'] += 1
            state.update(done=True, won=True, last_win=today)
            score = state['streak'] * 10 + game.MAX_GUESSES + 1 - len(state['guesses'])
            upsert_game_score(user_id, game.GAME_ID, short_name or str(user_id),
                              score, 0, len(state['guesses']), commit=False)
        elif len(state['guesses']) >= game.MAX_GUESSES:
            state.update(done=True, won=False, streak=0)
        return state, daily_render(game, state)

    def load(state):
        if state.get('v') != 1 or not isinstance(state.get('guesses'), list):
            raise ValueError("bad daily save")
        return state

    reply = run_turn(game.GAME_ID, user_id, new_state, turn, load)
    return reply, False, None


def daily_render(game, state, note='') -> str:
    """Every guess so far with its feedback, then what to do next."""
    answer = game.answer(state['day'])
    lines = [f"{game.NAME} {len(state['guesses'])}/{game.MAX_GUESSES}"]
    lines += [game.feedback(answer, guess) for guess in state['guesses']]
    if state['done'] and state['won']:
        lines.append(f"Solved in {len(state['guesses'])}! Streak {state['streak']}. "
                     "A new one tomorrow. [0]Exit")
    elif state['done']:
        lines.append(f"It was {game.show(answer)}. A new one tomorrow. [0]Exit")
    else:
        lines.append(f"{game.PROMPT} [?]Rules [0]Exit")
    if note:
        lines.insert(0, note)
    screen = "\n".join(lines)
    if note and not fits(screen):
        return MESSAGE_SEPARATOR.join(pack([note]) + ["\n".join(lines[1:])])
    return screen


# ── Saves that used to be local ─────────────────────────────────────────────

def read_save_or_legacy(conn, run_key, game_id, legacy_table):
    """This player's save, looking in the game's old local table if the
    synced one has nothing. Returns ``(raw, from_legacy)``.

    Candy Wars and Baconfall each kept saves in a table of their own, which
    never left the node. They are kept with every other game's saves now.
    A run in progress is carried over the first time its owner plays: the
    caller writes it to the synced table and calls ``drop_legacy_save`` in
    the same transaction, so the move happens whole or not at all.
    """
    import sqlite3
    from db_operations import read_game_save

    raw = read_game_save(conn, run_key, game_id)
    if raw is not None:
        return raw, False
    try:
        row = conn.execute(
            f"SELECT state_json FROM {legacy_table} WHERE user_id = ?",
            (run_key,)).fetchone()
    except sqlite3.OperationalError:
        return None, False          # the old table was never created here
    return (row[0], True) if row else (None, False)


def drop_legacy_save(conn, run_key, legacy_table) -> None:
    conn.execute(f"DELETE FROM {legacy_table} WHERE user_id = ?", (run_key,))
