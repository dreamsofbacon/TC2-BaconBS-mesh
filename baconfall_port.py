"""Durable, node-local Baconfall door sessions in the BBS database.

A database transaction serializes turns, including across server/web emulator
processes. Saves are deliberately not broadcast with Z-machine save files.
"""
import json
import secrets

import baconfall as game
from db_operations import get_db_connection, upsert_game_score


class SaveUnavailable(ValueError):
    """Keep an unreadable or future-version save intact for the operator."""


def _load(raw):
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get('version') != game.VERSION:
            raise ValueError('unsupported version')
        # Catch truncated/manual edits before any mutation can replace the save.
        if not game.new_game(0).keys() <= state.keys():
            raise ValueError('missing fields')
        phases = {'hero', 'route', 'combat', 'event', 'cache', 'camp', 'relic', 'ended'}
        if state['phase'] not in phases:
            raise ValueError('unknown phase')
        for field in ('seed', 'draw', 'hp', 'max_hp', 'attack', 'armor', 'heat',
                      'rations', 'gold', 'act', 'room', 'moves', 'renown', 'sizzle'):
            if type(state[field]) is not int or state[field] < 0:
                raise ValueError('invalid statistic')
        if state['hp'] > state['max_hp'] or state['heat'] > 6 or state['act'] > 3 or state['room'] > 3:
            raise ValueError('out-of-range statistic')
        if not isinstance(state['relics'], list) or any(
                item not in {r[0] for r in game.RELICS.values()} for item in state['relics']):
            raise ValueError('invalid relics')
        if state['phase'] == 'combat':
            enemy = state['enemy']
            for field in ('hp', 'max_hp', 'power', 'turn'):
                if type(enemy[field]) is not int:
                    raise ValueError('invalid enemy statistic')
            if (not enemy['pattern'] or any(i not in ('jab', 'crush', 'brace', 'charge')
                    for i in enemy['pattern']) or enemy['kind'] not in ('battle', 'elite', 'boss')):
                raise ValueError('invalid enemy')
        if state['phase'] == 'route' and (len(state['routes']) != 2 or any(
                r[0] not in ('battle', 'elite', 'cache', 'event') for r in state['routes'])):
            raise ValueError('invalid routes')
        game.view(state)
        return state
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise SaveUnavailable('Baconfall save could not be read. Ask the operator to inspect it; it has been preserved.') from exc


def play(user_id, text=None, short_name=None):
    """Load/start or advance one turn; return (reply, leave, terminal score).

    This owns its transaction and must be called outside any existing write
    transaction, like the other top-level door handlers.
    """
    conn = get_db_connection()
    conn.execute('''CREATE TABLE IF NOT EXISTS baconfall_runs (
        user_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)''')
    conn.commit()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT state_json FROM baconfall_runs WHERE user_id = ?',
                           (str(user_id),)).fetchone()
        state = _load(row[0]) if row else game.new_game(secrets.randbits(63))
        previous_phase = state['phase']
        if text is None:
            reply, leave = game.view(state), False
        else:
            state, reply, leave = game.command(state, text)
        result = None
        if state['phase'] == 'ended' and previous_phase != 'ended':
            result = (game.score(state), state['moves'])
            upsert_game_score(user_id, game.GAME_ID, short_name or str(user_id),
                              result[0], 0, result[1], commit=False)
        conn.execute('''INSERT INTO baconfall_runs (user_id, state_json) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET state_json = excluded.state_json''',
                     (str(user_id), json.dumps(state, separators=(',', ':'))))
    return reply, leave, result
