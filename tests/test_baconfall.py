"""Full expeditions, tactical rules, durable turns, and BBS dispatch."""
import json
import sqlite3
import sys
import types
from copy import deepcopy
from unittest import mock

import pytest

if 'meshtastic' not in sys.modules:
    sys.modules['meshtastic'] = types.SimpleNamespace(BROADCAST_NUM=0)

import baconfall as g
import baconfall_port as port
import db_operations as db


def hero(role='1', seed=42):
    return g.command(g.new_game(seed), role)[0]


def fight(role='1', kind='boss', act=0):
    s = hero(role)
    s['act'] = act
    g._fight(s, kind)
    return s


def policy(s):
    """Conservative player: reads intent, protects HP, develops strike."""
    p = s['phase']
    if p == 'route':
        priority = {'cache': 4, 'event': 3, 'battle': 2, 'elite': 1}
        return str(max(range(2), key=lambda i: priority[s['routes'][i][0]]) + 1)
    if p == 'cache':
        return '1' if s['rations'] < 2 else ('3' if s['hp'] < s['max_hp'] - 8 else '2')
    if p == 'event':
        return '2' if s['hp'] > 20 else '3'
    if p == 'camp':
        return '1' if s['hp'] < s['max_hp'] - 12 else ('2' if s['gold'] >= 12 else '1')
    if p == 'relic':
        return '2'
    e, intent = s['enemy'], g._intent(s)
    cost = 2 if s['role'] == 'Maple Witch' else 3
    if e['hp'] <= s['attack'] - (5 if intent == 'brace' else 0):
        return 'a'
    if s['heat'] >= cost and e['hp'] <= s['sizzle']:
        return 's'
    if s['hp'] < s['max_hp'] - 16 and s['rations'] and intent in ('charge', 'brace'):
        return 'e'
    if intent == 'crush':
        return 'g'
    if s['heat'] >= cost and (intent == 'brace' or s['heat'] >= 5):
        return 's'
    return 'a'


@pytest.mark.parametrize('role', ['1', '2', '3'])
def test_all_heroes_can_complete_many_expeditions(role):
    wins = 0
    for seed in range(50):
        s = hero(role, seed)
        for _ in range(200):
            if s['phase'] == 'ended':
                break
            s, text, _ = g.command(s, policy(s))
            # Round-trip every turn, as a real save/restart would do.
            s = json.loads(json.dumps(s))
            assert 0 <= s['hp'] <= s['max_hp']
            assert 0 <= s['heat'] <= 6
            assert s['gold'] >= 0 and s['rations'] >= 0
            assert len(text) < 850
        assert s['outcome'] in ('victory', 'defeat'), (role, seed, s)
        if s['outcome'] == 'victory':
            wins += 1
            assert len(s['relics']) == 3
            assert g.score(s) >= 2000
            assert 'Breakfast is saved' in g.view(s)
    assert wins >= 35, (role, wins)


def test_reckless_attacks_can_lose():
    s = hero('2', 0)
    for _ in range(200):
        if s['phase'] == 'ended':
            break
        s, _, _ = g.command(s, 'a' if s['phase'] == 'combat' else policy(s))
    assert s['outcome'] == 'defeat'
    assert s['hp'] == 0


@pytest.mark.parametrize('key', ['h', 'i', 'm', 'l', '?', '!CM', 'garbage', 's', 'e'])
def test_information_and_invalid_actions_do_not_spend_a_turn(key):
    s = fight()
    original = deepcopy(s)
    after, _, _ = g.command(s, key)
    assert s == original  # pure API cannot mutate callers' saves
    assert after == original


def test_guard_blocks_crush_and_rind_counters():
    s = fight()
    s['enemy']['turn'] = 1
    after, text, _ = g.command(s, 'g')
    assert after['hp'] == s['hp'] - (16 - 7 - 2)
    assert after['enemy']['hp'] == s['enemy']['hp'] - 3
    assert after['heat'] == 2
    assert 'counters' in text


def test_sizzle_pierces_armor_and_witch_discount():
    s = fight('3')
    s['enemy']['turn'] = 2
    s['heat'] = 2
    after, _, _ = g.command(s, 's')
    assert after['enemy']['hp'] == s['enemy']['hp'] - 14
    assert after['heat'] == 0
    s['role'] = 'Iron Rind'
    assert g.command(s, 's')[0] == s


def test_ranger_bonus_and_killing_blow_prevents_retaliation():
    s = fight('2')
    after, _, _ = g.command(s, 'a')
    assert after['enemy']['hp'] == s['enemy']['hp'] - 10
    s['enemy']['turn'] = 1
    s['enemy']['hp'] = 1
    after, _, _ = g.command(s, 'a')
    assert after['hp'] == s['hp']
    assert after['phase'] == 'relic'


def test_eating_spends_food_and_enemy_still_attacks():
    s = fight()
    s['hp'] = 20
    s['enemy']['turn'] = 1
    after, _, _ = g.command(s, 'e')
    assert after['rations'] == s['rations'] - 1
    assert after['hp'] == 20 + 16 - (16 - 2)


def test_ash_king_enrages_once_and_advertises_new_power():
    s = fight(act=3)
    s['enemy']['hp'] = 48
    after, text, _ = g.command(s, 'a')
    assert after['enemy']['enraged']
    assert after['enemy']['power'] == 18
    assert 'CRUSHES for 25' in text
    after, _, _ = g.command(after, 'g')
    assert after['enemy']['power'] == 18


@pytest.mark.parametrize('phase,key', [('camp', '2'), ('camp', '3'), ('camp', '4'), ('event', '1'), ('event', '2')])
def test_unaffordable_choices_do_not_change_state(phase, key):
    s = hero()
    s.update(phase=phase, gold=0, hp=6, rations=0)
    assert g.command(s, key)[0] == s


def test_rewards_are_one_time_and_new_run_requires_confirmation():
    s = hero()
    s['phase'] = 'cache'
    after, _, _ = g.command(s, '2')
    assert after['gold'] == 10 and after['phase'] == 'route'
    confirmed, _, _ = g.command(after, 'new')
    assert confirmed['confirm']
    assert g.command(confirmed, 'no')[0] == after
    reset, _, _ = g.command(confirmed, 'yes')
    assert reset['phase'] == 'hero' and reset['moves'] == 0
    assert reset['seed'] != after['seed']


def test_relics_stack_and_route_choices_are_distinct():
    s = hero()
    assert s['routes'][0] != s['routes'][1]
    for _ in range(3):
        s['phase'] = 'relic'
        s, _, _ = g.command(s, '3')
    assert s['sizzle'] == 27
    assert s['phase'] == 'combat' and s['enemy']['name'] == 'The Ash King'


@pytest.fixture
def connection():
    con = sqlite3.connect(':memory:')
    with mock.patch.object(db.thread_local, 'connection', con, create=True):
        db.initialize_database()
        yield con
    con.close()


def stored(con, user=42):
    # Read through the connection given: some tests check from a second one.
    from player_identity import player_key as _key
    row = con.execute('SELECT save_data FROM zork_saves WHERE user_id=? AND game_id=?',
                      (_key(user), g.GAME_ID)).fetchone()
    import door_kit
    return door_kit.decode_save(row[0])


def put(con, state, user=42):
    import door_kit
    port.play(user)
    door_kit.store_save(g.GAME_ID, user, state)


def test_save_exit_resume_and_player_isolation(connection):
    port.play(42)
    port.play(42, '2')
    before = stored(connection)
    _, leave, _ = port.play(42, '!x')
    assert leave
    assert stored(connection) == before
    assert port.play(42)[0] == g.view(before)
    port.play(99)
    assert stored(connection, 99)['phase'] == 'hero'
    assert stored(connection) == before


def test_completed_run_records_score_atomically_and_does_not_republish(connection):
    s = fight(act=3)
    s['enemy']['hp'] = 1
    put(connection, s)
    _, _, result = port.play(42, 'a', 'Crispy')
    assert result is not None
    assert db.get_game_scoreboard(g.GAME_ID)[0][0] == 'Crispy'
    assert stored(connection)['outcome'] == 'victory'
    connection.execute('DELETE FROM game_scores')
    connection.commit()
    port.play(42, 'l')
    port.play(42)
    assert db.get_game_scoreboard(g.GAME_ID) == []


def test_failed_score_write_rolls_back_winning_turn(connection):
    s = fight(act=3)
    s['enemy']['hp'] = 1
    put(connection, s)
    with mock.patch.object(port, 'upsert_game_score', side_effect=sqlite3.OperationalError('locked')):
        with pytest.raises(sqlite3.OperationalError):
            port.play(42, 'a')
    assert stored(connection) == s


@pytest.mark.parametrize('raw', ['{broken', '{"version":999}', '{}', '[]'])
def test_unreadable_save_is_preserved(connection, raw):
    port.play(42)
    connection.execute('UPDATE zork_saves SET save_data=? WHERE game_id=?',
                       (raw.encode(), g.GAME_ID))
    connection.commit()
    with pytest.raises(port.SaveUnavailable):
        port.play(42)
    assert bytes(connection.execute(
        'SELECT save_data FROM zork_saves WHERE game_id=?', (g.GAME_ID,)).fetchone()[0]) == raw.encode()


def test_menu_launch_routes_input_and_exit(connection):
    import command_handlers as ch
    import message_processing as mp
    import utils
    iface = types.SimpleNamespace(nodes={}, bbs_nodes=[])
    with mock.patch.object(ch, 'send_message'), mock.patch.object(ch, 'get_node_id_from_num', return_value='!abc'), mock.patch.object(ch, 'get_node_short_name', return_value='Crispy'):
        ch.handle_games_command(42, iface)
        for key in ch.games_menu_keys(g.GAME_ID):
            ch.handle_games_steps(42, key, iface)
        assert ch.get_user_state(42)['command'] == 'BACONFALL'
        mp.process_message(42, '2', iface)
        assert stored(connection)['role'] == 'Smoke Ranger'
        for command in ('s', 'n', '!CM', 'h', 'm'):
            with mock.patch.object(ch, 'handle_baconfall_steps') as dispatch:
                mp.process_message(42, command, iface)
                dispatch.assert_called_once_with(42, command, iface)
        mp.process_message(42, '!x', iface)
        assert ch.get_user_state(42)['command'] == 'GAMES_MENU'
    utils.user_states.pop(42, None)


def test_reopen_database_restores_exact_combat(tmp_path):
    path = tmp_path / 'bbs.db'
    con = sqlite3.connect(path)
    with mock.patch.object(db.thread_local, 'connection', con, create=True):
        db.initialize_database()
        put(con, fight())
        port.play(42, 'a')
        expected = stored(con)
    con.close()
    con = sqlite3.connect(path)
    try:
        with mock.patch.object(db.thread_local, 'connection', con, create=True):
            assert port.play(42)[0] == g.view(expected)
            port.play(42, 'g')
            assert stored(con) == g.command(expected, 'g')[0]
    finally:
        con.close()


def test_concurrent_turns_are_serialized(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'concurrent.db'
    con = sqlite3.connect(path)
    with mock.patch.object(db.thread_local, 'connection', con, create=True):
        db.initialize_database()
        initial = fight()
        put(con, initial)
    con.close()

    def turn():
        db.thread_local.connection = sqlite3.connect(path, timeout=5)
        try:
            port.play(42, 'a')
        finally:
            db.thread_local.connection.close()
            del db.thread_local.connection

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(turn) for _ in range(2)]
        for future in futures:
            future.result()
    con = sqlite3.connect(path)
    try:
        expected = g.command(g.command(initial, 'a')[0], 'a')[0]
        assert stored(con) == expected
    finally:
        con.close()


def test_failed_save_write_also_rolls_back_score(connection):
    s = fight(act=3)
    s['enemy']['hp'] = 1
    put(connection, s)
    connection.execute('''CREATE TRIGGER reject_save BEFORE UPDATE ON zork_saves
        BEGIN SELECT RAISE(ABORT, 'disk failed'); END''')
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        port.play(42, 'a')
    assert stored(connection) == s
    assert db.get_game_scoreboard(g.GAME_ID) == []


def test_malformed_statistics_do_not_overwrite_save(connection):
    s = hero()
    s['hp'] = 'broken'
    put(connection, s)
    with pytest.raises(port.SaveUnavailable):
        port.play(42, '1')
    assert stored(connection) == s


def test_full_expedition_through_real_bbs_delivery(connection):
    import bbs_emulator as emu
    import command_handlers as ch
    import utils
    node = '!0000002a'
    iface = emu.EmulatorInterface({node: {'num': 42, 'user': {'id': node, 'shortName': 'Crispy'}}})
    session = emu.EmulatorSession('baconfall-test', 42, node, iface, 'Crispy', True)
    transcript = []
    try:
        _, error = session.send('!g')
        assert error is None
        for key in ch.games_menu_keys(g.GAME_ID):
            _, error = session.send(key)
            assert error is None
        # Fix the seed, but all player actions below use the real BBS router.
        put(connection, g.new_game(42))
        session.send('1')
        for _ in range(200):
            s = stored(connection)
            if s['phase'] == 'ended':
                break
            chunks, error = session.send(policy(s))
            assert error is None
            assert chunks
            assert all(len(c['text'].encode('utf-8')) <= iface.max_text_bytes for c in chunks)
            transcript.extend(c['text'] for c in chunks)
        assert stored(connection)['outcome'] == 'victory'
        assert 'Breakfast is saved' in ''.join(transcript)
        assert db.get_game_scoreboard(g.GAME_ID)[0][0] == 'Crispy'
        session.send('x')
        assert session.menu_state()['command'] == 'GAMES_MENU'
    finally:
        utils.user_states.pop(42, None)


def test_a_run_can_finish_with_no_game_scores_table(connection):
    """Only initialize_database created game_scores, and the SSH front end
    and the web admin do not run it. The turn that ENDS a run is the only
    one that writes a score, so a missing table left a player able to play
    forever and never finish -- and unlike a busy database, it never
    cleared on its own."""
    s = fight(act=3)
    s['enemy']['hp'] = 1
    put(connection, s)
    connection.execute('DROP TABLE game_scores')
    connection.commit()

    _, _, result = port.play(42, 'a', 'Crispy')
    assert result is not None
    assert stored(connection)['outcome'] == 'victory'
    assert db.get_game_scoreboard(g.GAME_ID)[0][0] == 'Crispy'


def test_a_score_writes_without_any_door_pre_flight(connection):
    """The zork and trivia paths in command_handlers call upsert_game_score
    directly, never ensure_game_saves_table, so the guard has to live in the
    writer and not only in the doors' pre-flight."""
    connection.execute('DROP TABLE game_scores')
    connection.commit()
    db.upsert_game_score(99, 'zork1', 'Zorker', 50, 350, 20)
    assert db.get_game_scoreboard('zork1')[0][0] == 'Zorker'


def test_ensuring_the_scores_table_does_not_commit(connection):
    """It is called from inside upsert_game_score, which Baconfall calls
    with commit=False from inside its own BEGIN IMMEDIATE. A commit here
    would end the caller's transaction halfway through a turn -- the exact
    atomicity that transaction exists for. So it must leave an open
    transaction open, and roll back with it."""
    connection.execute('DROP TABLE game_scores')
    connection.commit()
    connection.execute('BEGIN IMMEDIATE')
    connection.execute(
        "INSERT INTO zork_saves (user_id, game_id, save_data, updated_at)"
        " VALUES ('pending', 'x', X'00', 'now')")

    db._ensure_game_scores_table(connection)
    assert connection.in_transaction

    connection.rollback()
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name = 'game_scores'"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM zork_saves WHERE user_id = 'pending'"
    ).fetchone()[0] == 0


def test_the_two_definitions_of_game_scores_cannot_drift(connection):
    """initialize_database and the lazy ensure share one DDL string. Two
    copies would be two schemas the first time either was edited, and the
    lazy one is the copy nobody looks at."""
    connection.execute('DROP TABLE game_scores')
    connection.commit()
    db._ensure_game_scores_table(connection)
    lazy = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'game_scores'").fetchone()[0]
    connection.execute('DROP TABLE game_scores')
    db.initialize_database()
    eager = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'game_scores'").fetchone()[0]
    assert lazy == eager


def test_a_busy_database_is_retried_rather_than_reported(connection):
    """The maintenance pass in the server process takes an exclusive lock
    for as long as VACUUM runs, and a turn in the SSH process waited it out
    and told the player their turn was lost. A lock that clears is now the
    player's problem for a fraction of a second instead."""
    attempts = []
    real = port._play_once

    def flaky(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError('database is locked')
        return real(*args, **kwargs)

    with mock.patch.object(port, '_play_once', side_effect=flaky), \
            mock.patch.object(port.time, 'sleep') as slept:
        reply, leave, result = port.play(1, None, 'Crispy')
    assert len(attempts) == 3
    assert reply
    # Waited between attempts rather than spinning on the lock.
    assert [call.args[0] for call in slept.call_args_list] == [0.2, 0.5]


def test_a_lock_that_never_clears_is_still_reported(connection):
    """Bounded: a held lock must surface as the retry message, not hang the
    session forever waiting for it."""
    with mock.patch.object(port, '_play_once',
                           side_effect=sqlite3.OperationalError('database is locked')), \
            mock.patch.object(port.time, 'sleep'):
        with pytest.raises(sqlite3.OperationalError):
            port.play(1, None, 'Crispy')


def test_a_real_fault_is_not_retried(connection):
    """Only contention passes. A missing table is a fault to see at once,
    not something to sit on for two seconds first."""
    calls = []

    def broken(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError('no such table: game_scores')

    with mock.patch.object(port, '_play_once', side_effect=broken), \
            mock.patch.object(port.time, 'sleep') as slept:
        with pytest.raises(sqlite3.OperationalError):
            port.play(1, None, 'Crispy')
    assert len(calls) == 1
    slept.assert_not_called()


def test_storage_failure_gives_player_retry_message(connection):
    import command_handlers as ch
    with mock.patch.object(port, 'play', side_effect=sqlite3.OperationalError('locked')), \
            mock.patch.object(ch, 'send_message') as send, \
            mock.patch.object(ch, 'get_node_id_from_num', return_value='!abc'), \
            mock.patch.object(ch, 'get_node_short_name', return_value='Crispy'):
        ch.handle_baconfall_steps(42, 'a', types.SimpleNamespace())
        assert 'previous save is intact' in send.call_args.args[0]
