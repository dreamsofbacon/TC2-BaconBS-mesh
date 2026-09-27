"""Airport rules and the persisted, packet-sized door menu in both themes."""
import json
import sqlite3
from copy import deepcopy
from itertools import permutations
from unittest import mock

import pytest

import db_operations as db
import dopewars as game
import dopewars_door as door
import dopewars_menu as menu
from dopewars_theme import theme
from player_identity import player_key


@pytest.fixture
def connection():
    with sqlite3.connect(':memory:') as conn:
        with mock.patch.object(db.thread_local, 'connection', conn, create=True):
            db.initialize_database()
            yield conn
    conn.close()


def save(conn, state):
    door.play(42)
    conn.execute('UPDATE dopewars_runs SET state_json=? WHERE user_id=?',
                 (json.dumps(state), player_key(42)))
    conn.commit()


def load():
    return door.play_state(42)[1]


@pytest.mark.parametrize('origin,destination', tuple(permutations(game.CITIES, 2)))
def test_routes_fares_and_normal_travel_processing(origin, destination):
    fares = set()
    for day in range(1, 31):
        state = game.new_game(19)
        state.update(place=game.AIRPORTS[origin], day=day)
        before = deepcopy(state)
        fare = game.flight_fare(state, destination)
        low, high = game.FLIGHT_FARE_BANDS[frozenset((origin, destination))]
        assert low <= fare <= high
        assert state == before
        assert game.flight_fare(door._load(json.dumps(state)), destination) == fare
        reverse = dict(state, place=game.AIRPORTS[destination])
        assert game.flight_fare(reverse, origin) == fare
        fares.add(fare)
    assert len(fares) > 1
    for seed in range(50):
        state = game.new_game(seed)
        state.update(place=game.AIRPORTS[origin], day=2)
        paid = deepcopy(state)
        paid['cash'] -= game.flight_fare(state, destination)
        local = next(p for p in game.CITY_PLACES[origin] if p != state['place'])
        expected = game.command(paid, f'travel {local}')[0]
        expected['place'] = game.AIRPORTS[destination]
        actual = game.command(state, f'flight {destination} yes')[0]
        assert actual == expected  # draw stream, market, interest, encounters and loot
        assert game.validate(actual) == actual


@pytest.mark.parametrize('text', ['flight miami', 'flight miami no',
                                  'flight nowhere yes', 'flight new-york yes'])
def test_invalid_flights_are_noops(text):
    state = game.new_game(19)
    state['place'] = 'queens'
    assert game.command(state, text)[0] == state


def test_flight_requires_airport_and_cash_not_bank():
    state = game.new_game(19)
    assert game.command(state, 'flight miami yes')[0] == state
    state.update(place='queens', cash=0, bank=10000)
    assert game.command(state, 'flight miami yes')[0] == state


@pytest.mark.parametrize('pg13', [False, True])
@pytest.mark.parametrize('origin,destination', tuple(permutations(game.CITIES, 2)))
def test_menu_through_door(connection, pg13, origin, destination):
    state = game.new_game(19)
    state.update(place=game.AIRPORTS[origin], debt=0, loan_due=0, cash=10000)
    save(connection, state)
    nav = None

    def send(text):
        nonlocal nav
        screen, leave, nav = menu.handle(42, text, 'Pilot', pg13, nav)
        assert not leave
        assert len(screen.encode('utf-8')) <= 200, screen
        return screen

    assert '[7]Airport' in send(None)
    assert 'Airport:' in send('7')
    index = menu._airport_destinations(state).index(destination) + 1
    assert 'Fly to' in send(str(index))
    assert load() == state
    send('0')
    assert load() == state
    send(str(index))
    send('no')
    assert load() == state
    send(str(index))
    fare = game.flight_fare(state, destination)
    with mock.patch.object(game, 'draw', return_value=99):
        screen = send('yes')
    arrived = load()
    assert arrived['place'] == game.AIRPORTS[destination]
    assert arrived['cash'] == state['cash'] - fare
    assert arrived['day'] == 2
    assert theme(pg13)['places'][arrived['place']] in screen
    assert '[7]Airport' in send(None)  # reconnect through durable save
    send('2')
    assert len(menu._others(arrived)) == 7
    local = menu._others(arrived)[0]
    with mock.patch.object(game, 'draw', return_value=99):
        send('1')
    assert load()['place'] == local


@pytest.mark.parametrize('pg13', [False, True])
def test_unaffordable_menu_flight_is_small_and_preserves_save(connection, pg13):
    state = game.new_game(19)
    state.update(place='queens', cash=0, bank=10000)
    save(connection, state)
    screen, _, nav = menu.handle(42, '7 1 yes', 'Pilot', pg13)
    assert nav['menu'] == 'flight_confirm'
    assert load() == state
    assert len(screen.encode('utf-8')) <= 200, screen
    assert 'cash' in screen


@pytest.mark.parametrize('pg13', [False, True])
def test_airport_packet_budget(pg13):
    for origin in game.CITIES:
        state = game.new_game(19)
        state.update(place=game.AIRPORTS[origin], cash=game.MAX_CASH)
        for destination in game.CITIES:
            if destination == origin:
                continue
            for nav in ({'menu': 'airport'},
                        {'menu': 'flight_confirm', 'destination': destination}):
                screen = menu.render(state, nav, theme(pg13), 'Pick 1-2.')
                assert len(screen.encode('utf-8')) <= 200, screen


@pytest.mark.parametrize('pg13', [False, True])
@pytest.mark.parametrize('seed', range(40))
def test_flight_arrival_matches_ground_menu(connection, pg13, seed):
    """Flights share themed loot/encounter/deadline handling, not just rules."""
    state = game.new_game(seed)
    state.update(place='queens', moves=1, cash=999999, debt=99999)
    fare = game.flight_fare(state, game.NEW_ORLEANS)
    save(connection, state)
    flight, _, flight_nav = menu.handle(42, '7 1 yes', 'Pilot', pg13)
    arrived = load()
    assert len(flight.encode('utf-8')) <= 200, flight
    assert theme(pg13)['places'][arrived['place']] in flight or arrived['phase'] == 'police'
    local_state = deepcopy(state)
    local_state.update(place='french-quarter', cash=state['cash'] - fare)
    save(connection, local_state)
    ground, _, ground_nav = menu.handle(42, '2 1', 'Pilot', pg13)
    assert flight_nav == ground_nav
    assert flight == ground


@pytest.mark.parametrize('pg13', [False, True])
@pytest.mark.parametrize('deadline', [False, True])
def test_flight_final_day_and_deadline(connection, pg13, deadline):
    state = game.new_game(19, 365 if deadline else 30)
    state.update(place='queens', day=30 if deadline else 29, moves=5)
    save(connection, state)
    with mock.patch.object(game, 'draw', return_value=99):
        screen, _, _ = menu.handle(42, '7 1 yes', 'Pilot', pg13)
    arrived = load()
    assert arrived['phase'] == 'ended'
    assert len(screen.encode('utf-8')) <= 200
    if deadline:
        assert arrived['place'] == state['place']
        assert arrived['day'] == state['day']
        assert theme(pg13)['deadline'] in screen
    else:
        assert arrived['place'] == 'kenner'
        assert arrived['day'] == 30


@pytest.mark.parametrize('pg13', [False, True])
def test_airport_entry_screen_at_save_limits(pg13):
    for place in game.AIRPORTS.values():
        state = game.new_game(19, 365)
        state.update(place=place, day=365, moves=1000, cash=game.MAX_CASH,
                     bank=game.MAX_CASH, debt=game.MAX_CASH, loan_due=394,
                     weapon=1, armor=1, event='deal:mushrooms')
        game.validate(state)
        screen = menu.render(state, {'menu': 'main'}, theme(pg13))
        assert len(screen.encode('utf-8')) <= 200, screen
        assert '[7]Airport' in screen
        assert '[0]X' in screen
