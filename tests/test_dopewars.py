"""Rules, transaction durability, restart determinism and real BBS routing."""
import json
import sqlite3
import types
from copy import deepcopy
from unittest import mock

import pytest

import dopewars as g
import dopewars_door as door
import db_operations as db
from player_identity import meshcore_player_number, player_key


def act(s, text):
    return g.command(s, text)[0]


def test_initialization():
    s = g.new_game(19)
    assert s == g.new_game(19)
    assert s != g.new_game(20)
    assert (s['day'], s['cash'], s['debt'], s['hp']) == (1, 2400, 1200, 100)
    assert sum(s['inventory'].values()) == 0
    assert g.validate(json.loads(json.dumps(s))) == s


def test_buy_sell_stock_cash_and_pure_api():
    s = g.new_game(19)
    before = deepcopy(s)
    price = s['market']['weed']['price']
    bought = act(s, 'buy weed 2')
    assert s == before
    assert bought['cash'] == s['cash'] - 2 * price
    assert bought['inventory']['weed'] == 2
    assert bought['market']['weed']['stock'] == s['market']['weed']['stock'] - 2
    sold = act(bought, 's weed 2')
    assert sold['cash'] == s['cash']
    assert sold['market'] == s['market']
    assert sold['inventory'] == s['inventory']
    assert sold['draw'] == s['draw']


@pytest.mark.parametrize('text', ['buy weed 0', 'buy weed -1', 'buy weed 1.5',
    'buy weed 99999999999999999', 'sell weed 1', 'buy unknown 1', 'buy weed 1000',
    'travel docks', 'travel nowhere', 'loan borrow 10001', 'loan repay 1201',
    'loan forgive 10', 'equipment spaceship', 'fight', 'new', '!CM', 'x extra',
    'finish now', 'buy weed １２', 'buy weed ²', 'garbage', 'buy', 'travel'])
def test_invalid_commands_do_not_change_state(text):
    s = g.new_game(19)
    assert act(s, text) == s


def test_limits():
    s = g.new_game(19)
    s['cash'] = 0
    assert act(s, 'buy weed 1') == s
    s['cash'] = 2400
    s['inventory']['hash'] = s['capacity']
    assert act(s, 'buy weed 1') == s
    s['inventory'] = {k: 0 for k in g.GOODS}
    s['inventory']['weed'] = 1
    s['cash'] = g.MAX_CASH
    assert act(s, 'sell weed 1') == s
    assert act(s, 'loan borrow 1') == s
    s['market']['weed']['stock'] = 0
    assert act(s, 'buy weed 1') == s


def test_loans_interest_and_equipment():
    s = g.new_game(19)
    borrowed = act(s, 'loan borrow 100')
    assert (borrowed['cash'], borrowed['debt']) == (2500, 1300)
    repaid = act(borrowed, 'loan repay 101')
    assert (repaid['cash'], repaid['debt']) == (2399, 1199)
    arrived = act(repaid, 'travel uptown')
    assert arrived['debt'] == 1259  # round interest up
    assert arrived['day'] == 2 and arrived['place'] == 'uptown'
    assert arrived['market'] != repaid['market']
    bag = act(s, 'equipment bag')
    assert (bag['capacity'], bag['cash']) == (70, 1500)
    assert act(bag, 'equipment bag') == bag
    assert act(s, 'equipment vest')['armor'] == 1
    assert act(s, 'equipment weapon')['weapon'] == 1
    s['hp'] = 80
    assert act(s, 'equipment medkit')['hp'] == 100


def police():
    s = g.new_game(19)
    s.update(phase='police', enemy_hp=45)
    return s


def test_police_combat_run_surrender_and_death():
    s = police()
    assert act(s, 'travel uptown') == s
    assert act(s, 'buy weed 1') == s
    assert act(s, 'equipment medkit') == s
    hit = act(s, 'fight')
    assert 0 < hit['enemy_hp'] < 45 and 0 < hit['hp'] < 100
    s.update(enemy_hp=1, weapon=1)
    assert act(s, 'fight')['hp'] == 100  # no retaliation from defeated enemy
    assert act(s, 'fight')['phase'] == 'market'
    s = police()
    s['inventory']['weed'] = 10
    surrendered = act(s, 'surrender')
    assert surrendered['cash'] == 1800
    assert sum(surrendered['inventory'].values()) == 0
    assert surrendered['phase'] == 'market'
    with mock.patch.object(g, 'draw', return_value=1):
        assert act(s, 'run')['phase'] == 'market'
    with mock.patch.object(g, 'draw', return_value=25):
        s['hp'] = 1
        dead = act(s, 'fight')
        assert dead['hp'] == 0 and dead['outcome'] == 'Defeated'
        assert g.score(dead) == 0
    with mock.patch.object(g, 'draw', return_value=99):
        assert act(police(), 'run')['hp'] < 100


def test_police_spawn_on_travel():
    with mock.patch.object(g, 'draw', return_value=1):
        s = act(g.new_game(19), 'travel uptown')
    assert s['phase'] == 'police' and s['enemy_hp'] == 45


def test_completion_bankruptcy_and_new_game():
    s = g.new_game(19)
    s['inventory']['weed'] = 2
    done = act(s, 'finish')
    assert done['outcome'] == 'Completed'
    assert g.score(done) == 1200 + 2 * s['market']['weed']['price']
    assert act(done, 'buy weed 1') == done
    assert act(done, 'new')['day'] == 1
    assert act(done, 'new')['seed'] != done['seed']
    s['cash'] = 0
    s['inventory']['weed'] = 0
    assert act(s, 'finish')['outcome'] == 'Bankrupt'
    assert g.score(act(g.new_game(19), 'bankrupt')) == 0
    s = police()
    s['day'] = g.DAYS
    assert act(s, 'surrender')['phase'] == 'ended'


def test_full_runs_stay_valid_and_end_on_day_30():
    for seed in range(100):
        s = g.new_game(seed)
        for _ in range(100):
            g.validate(s)
            if s['phase'] == 'ended':
                break
            text = 'surrender' if s['phase'] == 'police' else 'travel ' + ('uptown' if s['place'] == 'docks' else 'docks')
            s = act(s, text)
            assert len(g.view(s)) < 600
        assert s['phase'] == 'ended' and s['day'] == 30


@pytest.fixture
def connection():
    con = sqlite3.connect(':memory:')
    with mock.patch.object(db.thread_local, 'connection', con, create=True):
        db.initialize_database()
        yield con
    con.close()


def stored(con, user=42):
    return json.loads(con.execute('SELECT state_json FROM dopewars_runs WHERE user_id=?', (player_key(user),)).fetchone()[0])


def put(con, s, user=42):
    door.play(user)
    con.execute('UPDATE dopewars_runs SET state_json=? WHERE user_id=?', (json.dumps(s), player_key(user)))
    con.commit()


def test_save_resume_and_identity_isolation(connection):
    user = meshcore_player_number('abcdef0123456789')
    door.play(user)
    door.play(user, 'loan repay 100')
    before = stored(connection, user)
    assert door.play(user, 'quit')[1]
    assert stored(connection, user) == before
    assert door.play(user)[0] == g.view(before)
    door.play(42)
    assert stored(connection, 42)['debt'] == 1200
    for text in ('market', 'inventory', 'help', 'save'):
        door.play(user, text)
        assert stored(connection, user) == before


def test_restart_preserves_random_sequence_and_encounter(tmp_path):
    path = tmp_path / 'bbs.db'
    con = sqlite3.connect(path)
    with mock.patch.object(db.thread_local, 'connection', con, create=True):
        db.initialize_database()
        put(con, police())
        door.play(42, 'fight')
        expected = stored(con)
    con.close()
    con = sqlite3.connect(path)
    try:
        with mock.patch.object(db.thread_local, 'connection', con, create=True):
            assert door.play(42)[0] == g.view(expected)
            for text in ('surrender', 'travel uptown', 'surrender', 'travel docks'):
                expected = act(expected, text)
                door.play(42, text)
                assert stored(con) == expected
    finally:
        con.close()


def test_score_once_and_atomically(connection):
    put(connection, g.new_game(19))
    _, _, result = door.play(42, 'finish', 'Trader')
    assert result == (1200, 1)
    assert db.get_game_scoreboard('dopewars')[0][0] == 'Trader'
    with mock.patch.object(door, 'upsert_game_score') as submit:
        door.play(42)
        door.play(42, 'finish')
        door.play(42, 'quit')
        submit.assert_not_called()


def test_failed_score_or_save_rolls_back(connection):
    s = g.new_game(19)
    put(connection, s)
    with mock.patch.object(door, 'upsert_game_score', side_effect=sqlite3.OperationalError('locked')):
        with pytest.raises(sqlite3.OperationalError):
            door.play(42, 'finish')
    assert stored(connection) == s
    connection.execute("CREATE TRIGGER reject_save BEFORE UPDATE ON dopewars_runs BEGIN SELECT RAISE(ABORT, 'full'); END")
    connection.commit()
    with pytest.raises(sqlite3.IntegrityError):
        door.play(42, 'finish')
    assert stored(connection) == s
    assert db.get_game_scoreboard('dopewars') == []


@pytest.mark.parametrize('raw', ['{broken', '[]', '{"version":999}', '{}',
    json.dumps({**g.new_game(19), 'cash': -1}),
    json.dumps({**g.new_game(19), 'market': {'weed': 12}})])
def test_bad_save_preserved(connection, raw):
    door.play(42)
    connection.execute('UPDATE dopewars_runs SET state_json=?', (raw,))
    connection.commit()
    with pytest.raises(door.SaveUnavailable):
        door.play(42)
    assert connection.execute('SELECT state_json FROM dopewars_runs').fetchone()[0] == raw


def test_menu_and_dispatch_owns_global_commands(connection):
    import command_handlers as ch
    import message_processing as mp
    import utils
    iface = types.SimpleNamespace(nodes={}, bbs_nodes=[])
    try:
        with mock.patch.object(ch, 'send_message'), mock.patch.object(ch, 'get_node_id_from_num', return_value='!abc'), mock.patch.object(ch, 'get_node_short_name', return_value='Trader'):
            ch.handle_games_command(42, iface)
            index = next(i for i, (gid, _) in enumerate(ch.GAME_LIST, 1) if gid == 'dopewars')
            ch.handle_games_steps(42, str(index), iface)
            assert ch.get_user_state(42)['command'] == 'DOPEWARS'
            # Played by menu since Candy Wars: 6 = the loan screen, 2 = pay
            # back, 100 = the amount -- the same move as "loan repay 100".
            mp.process_message(42, '6 2 100', iface)
            assert stored(connection)['debt'] == 1100
            for text in ('s weed 1', 'm', 'i', 'h', 'n', '!CM', '!BB', 'save', 'equipment', 'travel uptown'):
                with mock.patch.object(mp, 'handle_dopewars_steps') as dispatch:
                    mp.process_message(42, text, iface)
                    dispatch.assert_called_once_with(42, text, iface)
            mp.process_message(42, '!x', iface)
            assert ch.get_user_state(42)['command'] == 'GAMES_MENU'
    finally:
        utils.user_states.pop(42, None)


def test_365_option_without_reroll_or_discarding_progress():
    s = g.new_game(19)
    long = act(s, 'new 365')
    assert long['days'] == 365 and long['loan_due'] == 30
    assert long['market'] == s['market'] and long['draw'] == s['draw']
    assert 'D1/365' in g.view(long)
    long = act(long, 'loan repay 1')
    assert act(long, 'new 30') == long
    assert act(long, 'new 365') == long
    assert act(s, 'new 100') == s
    assert act(act(long, 'finish'), 'new 30')['days'] == 30


def test_day_30_deadline_can_be_paid_but_not_extended():
    s = g.new_game(19, 365)
    s['day'] = 30
    more = act(s, 'loan borrow 1')
    assert more['loan_due'] == 30
    partial = act(more, 'loan repay 1')
    assert partial['loan_due'] == 30
    failed, reply, _ = g.command(partial, 'travel uptown')
    assert failed['phase'] == 'ended' and failed['outcome'] == 'Bankrupt'
    assert g.score(failed) == 0 and 'deadline' in reply
    assert failed['draw'] == partial['draw']
    paid = act(s, 'loan repay 1200')
    assert paid['debt'] == 0 and paid['loan_due'] == 0
    arrived = act(paid, 'travel uptown')
    assert arrived['day'] == 31 and arrived['phase'] != 'ended'
    borrowed = act(paid, 'loan borrow 100')
    assert borrowed['loan_due'] == 59


def test_365_full_run_and_police_on_deadline():
    s = act(g.new_game(19, 365), 'loan repay 1200')
    for _ in range(800):
        g.validate(s)
        if s['phase'] == 'ended':
            break
        s = act(s, 'surrender' if s['phase'] == 'police' else
                'travel ' + ('uptown' if s['place'] == 'docks' else 'docks'))
    assert s['day'] == 365 and s['outcome'] == 'Completed'
    s = g.new_game(19, 365)
    s.update(day=30, phase='police', enemy_hp=1)
    s = act(s, 'fight')
    assert s['phase'] == 'market' and s['day'] == 30
    assert act(s, 'loan repay 1200')['debt'] == 0


def test_365_deadline_and_choice_persist(connection):
    put(connection, g.new_game(19))
    door.play(42, 'new 365')
    assert stored(connection)['days'] == 365
    assert stored(connection)['loan_due'] == 30
    assert 'D1/365' in door.play(42)[0]
    s = stored(connection)
    s['day'] = 30
    put(connection, s)
    _, _, result = door.play(42, 'travel uptown', 'Trader')
    assert result[0] == 0
    assert stored(connection)['outcome'] == 'Bankrupt'
