"""Durable, node-local DopeWars door sessions in the BBS database.

A database transaction serializes turns, including across server/web emulator
processes. Saves are deliberately not broadcast with Z-machine save files.
"""
import json
import secrets
from copy import deepcopy

import dopewars as game
from db_operations import get_db_connection, upsert_game_score
from player_identity import player_key


class SaveUnavailable(ValueError):
    """Keep an unreadable or future-version save intact for the operator."""


def _load(raw):
    try:
        return game.validate(json.loads(raw))
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise SaveUnavailable('DopeWars save could not be read. Ask the operator to inspect it; it has been preserved.') from exc


def play(user_id, text=None, short_name=None):
    """Load/start or advance one turn; return (reply, leave, terminal score).

    This owns its transaction and must be called outside any existing write
    transaction, like the other top-level door handlers.
    """
    _before, _after, reply, leave, result = play_state(user_id, text, short_name)
    return reply, leave, result


def play_state(user_id, text=None, short_name=None):
    """play(), also handing back the saved state either side of the move.

    (before, after, reply, leave, result). The menu layer renders every screen
    from the state rather than from the engine's own reply text -- which is
    how Candy Wars keeps the engine's words ("Police stop!", "Unknown item:
    weed...") from ever reaching a player in that theme.
    """
    # Same canonical identity as scores, including MeshCore's mc- prefix.
    run_key = player_key(user_id)
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS dopewars_runs (
        user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)''')
    conn.commit()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT state_json FROM dopewars_runs WHERE user_id = ?',
                           (run_key,)).fetchone()
        state = _load(row[0]) if row else game.new_game(secrets.randbits(63))
        before = deepcopy(state)
        previous_phase = state['phase']
        if text is None:
            reply, leave = game.view(state), False
        else:
            state, reply, leave = game.command(state, text)
        result = None
        game.validate(state)
        if state['phase'] == 'ended' and previous_phase != 'ended':
            result = (game.score(state), state['moves'])
            upsert_game_score(user_id, game.GAME_ID, short_name or str(user_id),
                              result[0], 0, result[1], commit=False)
        conn.execute('''INSERT INTO dopewars_runs (user_id, state_json) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json''',
                     (run_key, json.dumps(state, separators=(',', ':'))))
    return before, state, reply, leave, result
