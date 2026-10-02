"""Durable Baconfall door sessions in the BBS database.

A database transaction serializes turns, including across server/web emulator
processes. Saves are kept with every other game's saves, so where
[sync] sync_zork_saves is on an expedition follows its player between nodes.

They used to live in a table of their own, baconfall_runs, which never left
the node. A run found there is moved across the first time its owner plays.
"""
import secrets

import baconfall as game
import door_kit
from db_operations import (clear_sync_tombstone, ensure_game_saves_table,
                           get_db_connection, upsert_game_score, write_game_save)
from player_identity import player_key

LEGACY_TABLE = 'baconfall_runs'


class SaveUnavailable(ValueError):
    """Keep an unreadable or future-version save intact for the operator."""


def _load(raw):
    try:
        state = door_kit.decode_save(raw)
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
    # Filed under the same player key as scores and saves. Raw str(user_id)
    # here would give a MeshCore player a different identity for their run
    # than for their score -- and strand every run saved before the move to
    # "mc-" identities. See player_identity.
    run_key = player_key(user_id)
    ensure_game_saves_table()
    conn = get_db_connection()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        raw, from_legacy = door_kit.read_save_or_legacy(
            conn, run_key, game.GAME_ID, LEGACY_TABLE)
        state = _load(raw) if raw is not None else game.new_game(secrets.randbits(63))
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
        write_game_save(conn, run_key, game.GAME_ID, door_kit.encode_save(state))
        if from_legacy:
            door_kit.drop_legacy_save(conn, run_key, LEGACY_TABLE)
    if raw is None or from_legacy:
        # A first synced save may meet the marker a deleted one left behind.
        clear_sync_tombstone('zork_saves', f"{run_key}:{game.GAME_ID}")
    return reply, leave, result
