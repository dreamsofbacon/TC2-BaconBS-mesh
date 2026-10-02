"""Durable DopeWars door sessions in the BBS database.

A database transaction serializes turns, including across server/web emulator
processes. Saves are kept with every other game's saves, so where
[sync] sync_zork_saves is on a run follows its player between nodes.

They used to live in a table of their own, dopewars_runs, which never left
the node. A run found there is moved across the first time its owner plays.
"""
import logging
import zlib
import secrets
from copy import deepcopy

import door_kit
import dopewars as game
from db_operations import (clear_sync_tombstone, ensure_game_saves_table,
                           get_db_connection, upsert_game_score, write_game_save)
from player_identity import player_key

LEGACY_TABLE = 'dopewars_runs'


class SaveUnavailable(ValueError):
    """Keep an unreadable or future-version save intact for the operator."""


# ── The stored form ─────────────────────────────────────────────────────────
#
# Two thirds of a save is the market: sixteen goods, each a price and a
# stock. But the market is not really state. market() builds it from nothing
# except the run's seed and its draw counter, and trading only moves stock.
# So the stored form keeps the draw the market was built from and the stock
# that has changed since, and rebuilds the rest on load.
#
#     "market": {"@": 412, "d": {"weed": -6}}
#
# The engine does not record which draw that was, and is left alone: it is
# found by stepping back from the current draw until one regenerates exactly
# the stored prices. Sixteen prices do not match by accident. Where none
# does -- a market made under older rules, or brought through a migration --
# the whole market is stored as before, so the compact form is only ever
# written when it provably rebuilds what it replaced.
#
# The bag is stored the same way: only what is actually carried.

_MARKET_DRAW = '@'
_STOCK_CHANGES = 'd'
_PRICE_CHECK = '#'
# How far back to look. A market is some fifty draws, and an arrival and a
# long fight add more; past this the search stops and the market is stored
# whole.
_SEARCH_BACK = 600


def _market_from(seed, market_draw):
    """The market as market() built it at that draw, and the draw after."""
    probe = {'seed': seed, 'draw': market_draw}
    game.market(probe)
    return probe['market'], probe['draw']


def _find_market_draw(state, hint=None):
    """The draw this market was built from, or None if it cannot be rebuilt."""
    prices = {item: offer['price'] for item, offer in state['market'].items()}
    candidates = [] if hint is None else [hint]
    candidates += range(state['draw'] - 1, max(-1, state['draw'] - _SEARCH_BACK), -1)
    # market() draws the prices first, one a good, in catalogue order.
    event_good = state.get('event', '').partition(':')[2]
    probe_index, probe_good = next(
        (index, item) for index, item in enumerate(game.GOODS) if item != event_good)
    for market_draw in candidates:
        if not 0 <= market_draw < state['draw']:
            continue
        # One price costs one hash to check, and rules out nearly every
        # candidate before a whole market is built. Not the good a deal or
        # a bust is about, though: its price was changed after it was drawn.
        drawn = max(1, game.GOODS[probe_good] * game.draw(
            {'seed': state['seed'], 'draw': market_draw + probe_index}, 60, 170) // 100)
        if drawn != prices[probe_good] and market_draw != hint:
            continue
        base, after = _market_from(state['seed'], market_draw)
        if after <= state['draw'] and all(
                base[item]['price'] == price for item, price in prices.items()):
            return market_draw
    return None


def compact(state, hint=None) -> dict:
    """The state as it is stored. Never loses anything: expand() of the
    result is the state again, or the market is left whole."""
    market_draw = _find_market_draw(state, hint)
    if market_draw is None:
        return state
    base, _after = _market_from(state['seed'], market_draw)
    changes = {item: offer['stock'] - base[item]['stock']
               for item, offer in state['market'].items()
               if offer['stock'] != base[item]['stock']}
    stored = dict(state)
    stored['market'] = {_MARKET_DRAW: market_draw, _STOCK_CHANGES: changes,
                        _PRICE_CHECK: _price_check(state['market'])}
    stored['inventory'] = {item: held for item, held in state['inventory'].items() if held}
    return stored


def _price_check(market) -> int:
    """A fingerprint of the prices, kept with a compact save.

    If market() is ever changed, a save made before the change rebuilds a
    different market after it. That cannot be undone -- the old prices are
    gone -- but it should not happen silently, and this is how it is seen.
    """
    return zlib.crc32(",".join(
        f"{item}:{market[item]['price']}" for item in sorted(market)).encode('ascii'))


def expand(stored):
    """A stored save as the engine's state, and the draw its market came
    from (None for a save stored whole)."""
    market = stored.get('market') if isinstance(stored, dict) else None
    if not isinstance(market, dict) or _MARKET_DRAW not in market:
        return stored, None
    market_draw = market[_MARKET_DRAW]
    if type(market_draw) is not int or not 0 <= market_draw < stored['draw']:
        raise ValueError('Invalid market draw')
    state = dict(stored)
    state['market'], _after = _market_from(stored['seed'], market_draw)
    if market.get(_PRICE_CHECK) != _price_check(state['market']):
        logging.warning(
            "DopeWars market rebuilt with different prices than it was saved "
            "with (seed %s, draw %s): the market rules have changed since.",
            stored['seed'], market_draw)
    for item, change in market.get(_STOCK_CHANGES, {}).items():
        state['market'][item]['stock'] += change
    state['inventory'] = {item: stored['inventory'].get(item, 0) for item in game.GOODS}
    if set(stored['inventory']) - set(game.GOODS):
        raise ValueError('Invalid inventory')
    return state, market_draw


def _open(raw):
    """(state, market draw) from a stored save, validated."""
    try:
        state, market_draw = expand(door_kit.decode_save(raw))
        return game.validate(game.migrate(state)), market_draw
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise SaveUnavailable('DopeWars save could not be read. Ask the operator to inspect it; it has been preserved.') from exc


def _load(raw):
    """The validated state from a stored save, in any form it has had."""
    return _open(raw)[0]


def _store(state, hint=None) -> bytes:
    """The bytes to save. The compact form is checked by expanding it
    again; anything that does not come back identical is stored whole."""
    try:
        stored = compact(state, hint)
        if stored is not state and expand(stored)[0] != state:
            raise ValueError('compact form did not round-trip')
    except Exception:
        logging.exception('DopeWars save stored whole: could not compact it')
        stored = state
    return door_kit.encode_save(stored)


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
    ensure_game_saves_table()
    conn = get_db_connection()
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        raw, from_legacy = door_kit.read_save_or_legacy(
            conn, run_key, game.GAME_ID, LEGACY_TABLE)
        if raw is not None:
            state, market_draw = _open(raw)
        else:
            state, market_draw = game.new_game(secrets.randbits(63)), 0
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
        write_game_save(conn, run_key, game.GAME_ID, _store(state, market_draw))
        if from_legacy:
            door_kit.drop_legacy_save(conn, run_key, LEGACY_TABLE)
    if raw is None or from_legacy:
        # A first synced save may meet the marker a deleted one left behind.
        clear_sync_tombstone('zork_saves', f"{run_key}:{game.GAME_ID}")
    return before, state, reply, leave, result
