"""DopeWars: original text-only trading rules. SPDX-License-Identifier: GPL-3.0-only.

No GTK or database dependencies. Commands return a new JSON-safe state; a saved
seed and draw counter define randomness independently of Python's random module.
See docs/dopewars.md for provenance and deliberately simplified rules.
"""
from copy import deepcopy
from hashlib import sha256

GAME_ID = 'dopewars'
VERSION = 1
DAYS = 30
MAX_CASH = 1_000_000_000
LOAN_LIMIT = 10_000
GOODS = {'weed': 90, 'hash': 230, 'acid': 540, 'cocaine': 1800}
PLACES = ('docks', 'uptown', 'suburbs', 'station')
# Display only: commands and saved item identifiers remain plain text.
GOOD_ICONS = {'weed': '🌿', 'hash': '🟫', 'acid': '🌀', 'cocaine': '❄️'}
HELP = ('TRADE\nB ITEM QTY - buy\nS ITEM QTY - sell\nM - market\nI - inventory\n'
        'Items: weed, hash, acid, cocaine\nExample: B weed 2\n\n'
        'TRAVEL\nT docks\nT uptown\nT suburbs\nT station\n'
        'Each trip: +1 day, +5% debt\n\n'
        'MONEY & GEAR\nloan borrow AMOUNT\nloan repay AMOUNT\nE - equipment menu\n\n'
        'ENCOUNTERS\nfight - attack\nrun - try to escape\n'
        'surrender - lose goods + 25% cash\n\n'
        'YOUR RUN\nsave - autosave status\nX - save & exit\n'
        'finish - sell all & end\nbankrupt - end with zero\n'
        'new 30 / new 365 - before play or after ending')


def good_label(item):
    return f"{GOOD_ICONS[item]} {item}"


def status(s):
    due = f" (due D{s['loan_due']})" if s['debt'] else ''
    return (f"DopeWars D{s['day']}/{s['days']} | {s['place']}\n"
            f"Cash ${s['cash']} | Debt ${s['debt']}{due}\n"
            f"HP {s['hp']} | Bag {sum(s['inventory'].values())}/{s['capacity']}")


def inventory_view(s):
    goods = '\n'.join(f"{good_label(k)} x{q}" for k, q in s['inventory'].items())
    return (f"{status(s)}\n\nBAG\n{goods}\n\n"
            f"GEAR\nWeapon: {'yes' if s['weapon'] else 'no'}\n"
            f"Vest: {'yes' if s['armor'] else 'no'}\n\nM - market\nH - help\nX - save & exit")


def draw(s, low, high):
    data = sha256(f"dopewars:{s['seed']}:{s['draw']}".encode()).digest()
    s['draw'] += 1
    return low + int.from_bytes(data[:8], 'big') % (high - low + 1)


def market(s):
    s['market'] = {item: {'price': base * draw(s, 45, 190) // 100,
                          'stock': draw(s, 0, 35)} for item, base in GOODS.items()}


def new_game(seed, days=DAYS):
    if days not in (30, 365):
        raise ValueError('Choose 30 or 365 days.')
    s = dict(version=VERSION, seed=seed, draw=0, day=1, days=days, loan_due=30, place=PLACES[0],
             cash=2400, debt=1200, hp=100, capacity=40, weapon=0, armor=0,
             inventory={item: 0 for item in GOODS}, market={}, phase='market',
             enemy_hp=0, moves=0, outcome='')
    market(s)
    return s


def score(s):
    return max(0, s['cash'] - s['debt'])


def view(s):
    header = status(s)
    if s['phase'] == 'ended':
        return (f"{header}\n\n{s['outcome']} | Score {score(s)}\n\n"
                "NEW 30 - short run\nNEW 365 - long run\nX - exit")
    if s['phase'] == 'police':
        return (f"{header}\n\n🚨 POLICE | HP {s['enemy_hp']}\n"
                "FIGHT - attack\nRUN - try to escape\n"
                "SURRENDER - lose goods + 25% cash\n\nX - save & exit")
    listing = '\n'.join(f"{good_label(k)} ${v['price']} | stock {v['stock']}"
                        for k, v in s['market'].items())
    options = '\nNEW 30 / NEW 365 - game length' if s['moves'] == 0 else ''
    return (f"{header}\n\nMARKET\n{listing}\n\n"
            "B item qty - buy\nS item qty - sell\nT place - travel\n"
            "I - bag\nE - gear\nH - all commands & places\nX - save & exit" + options)


def _finish(s, outcome):
    s['phase'], s['outcome'], s['enemy_hp'] = 'ended', outcome, 0


def _settle(s):
    proceeds = sum(q * s['market'][k]['price'] for k, q in s['inventory'].items())
    s['cash'] = min(MAX_CASH, s['cash'] + proceeds)
    s['inventory'] = {k: 0 for k in GOODS}
    _finish(s, 'Completed' if s['cash'] >= s['debt'] else 'Bankrupt')


def _arrival(s):
    s['enemy_hp'] = 0
    s['phase'] = 'market'
    if s['day'] == s['days']:
        _settle(s)


def command(state, text):
    """Invalid/read-only commands neither advance time nor consume randomness."""
    s = deepcopy(state)
    words = (text or '').strip().lower().split()
    if not words:
        return s, view(s), False
    verb, args = words[0], words[1:]
    verb = {'b': 'buy', 's': 'sell', 't': 'travel', 'm': 'market',
            'i': 'inventory', 'e': 'equipment', 'h': 'help', '?': 'help',
            'x': 'quit', '!x': 'quit', 'q': 'quit', 'save/quit': 'quit'}.get(verb, verb)
    if not args:
        if verb == 'quit':
            return s, 'DopeWars saved. Resume from Games.', True
        if verb == 'save':
            return s, 'Saved. Every action is saved automatically.', False
        if verb == 'help':
            return s, HELP, False
        if verb == 'market':
            return s, view(s), False
        if verb == 'inventory':
            return s, inventory_view(s), False
    if verb == 'new' and (not args or args in (['30'], ['365'])):
        if s['phase'] == 'ended' or s['moves'] == 0:
            days = int(args[0]) if args else s['days']
            # Selecting a length before playing must not reroll the market.
            seed = draw(s, 0, 2**63 - 1) if s['phase'] == 'ended' else s['seed']
            fresh = new_game(seed, days)
            return fresh, 'New game.\n' + view(fresh), False
        return s, 'Finish this run before starting another.', False
    if s['phase'] == 'ended':
        return s, view(s), False
    try:
        if s['phase'] == 'police':
            if args or verb not in ('fight', 'run', 'surrender'):
                raise ValueError('Police encounter: fight, run or surrender; help/save/quit also work.')
            if verb == 'surrender':
                s['inventory'] = {k: 0 for k in GOODS}
                s['cash'] -= s['cash'] // 4
                _arrival(s)
                reply = 'Goods confiscated; paid a 25% cash fine.'
            elif verb == 'run' and draw(s, 1, 100) <= 60:
                _arrival(s)
                reply = 'Escaped.'
            else:
                if verb == 'fight':
                    s['enemy_hp'] = max(0, s['enemy_hp'] - draw(s, 10, 22) - 15 * s['weapon'])
                if s['enemy_hp'] == 0:
                    _arrival(s)
                    reply = 'Police driven off.'
                else:
                    s['hp'] = max(0, s['hp'] - max(1, draw(s, 12, 26) - 8 * s['armor']))
                    reply = 'You were hit.'
                    if s['hp'] == 0:
                        s['cash'] = 0
                        _finish(s, 'Defeated')
        elif verb in ('buy', 'sell') and len(args) == 2:
            item, raw = args
            qty = _amount(raw)
            if item not in GOODS:
                raise ValueError('Unknown item: ' + ', '.join(GOODS))
            offer = s['market'][item]
            cost = qty * offer['price']
            if verb == 'buy':
                if qty > offer['stock'] or cost > s['cash'] or sum(s['inventory'].values()) + qty > s['capacity']:
                    raise ValueError('Not enough market stock, cash or bag space.')
                s['cash'] -= cost
                s['inventory'][item] += qty
                offer['stock'] -= qty
            else:
                if qty > s['inventory'][item] or s['cash'] + cost > MAX_CASH:
                    raise ValueError('Not enough inventory or cash limit exceeded.')
                s['inventory'][item] -= qty
                s['cash'] += cost
                offer['stock'] += qty
            reply = f"{'Bought' if verb == 'buy' else 'Sold'} {qty} {good_label(item)} for ${cost}."
        elif verb == 'travel' and len(args) == 1:
            if args[0] not in PLACES or args[0] == s['place']:
                raise ValueError('Choose a different place: ' + ', '.join(PLACES))
            if s['debt'] and s['day'] >= s['loan_due']:
                s['cash'] = 0
                s['inventory'] = {k: 0 for k in GOODS}
                _finish(s, 'Bankrupt')
                s['moves'] += 1
                return s, 'Loan deadline missed.\n' + view(s), False
            s['place'] = args[0]
            s['day'] += 1
            s['debt'] += (s['debt'] * 5 + 99) // 100
            market(s)
            reply = 'Arrived. Debt accrued 5% interest.'
            if draw(s, 1, 100) <= 25:
                s['phase'], s['enemy_hp'] = 'police', 45
                reply += ' Police stop!'
            else:
                _arrival(s)
        elif verb == 'loan' and len(args) == 2 and args[0] in ('borrow', 'repay'):
            amount = _amount(args[1])
            if args[0] == 'borrow':
                if s['debt'] + amount > LOAN_LIMIT or s['cash'] + amount > MAX_CASH:
                    raise ValueError('Loan limit $10000 or cash limit exceeded.')
                if not s['debt']:
                    s['loan_due'] = s['day'] + 29
                s['debt'] += amount
                s['cash'] += amount
            else:
                if amount > min(s['debt'], s['cash']):
                    raise ValueError('Repayment exceeds cash or debt.')
                s['cash'] -= amount
                s['debt'] -= amount
                if not s['debt']:
                    s['loan_due'] = 0
            reply = f"Loan {'borrowed' if args[0] == 'borrow' else 'repaid'}: ${amount}."
        elif verb == 'equipment':
            if not args:
                return s, ('EQUIPMENT\nE bag - $900 (+30 space, once)\n'
                           'E vest - $1200 (less damage)\nE weapon - $1600 (more damage)\n'
                           'E medkit - $250 (+30 HP)\n\nM - market\nX - save & exit'), False
            if len(args) != 1 or args[0] not in ('bag', 'vest', 'weapon', 'medkit'):
                raise ValueError('Equipment: bag, vest, weapon, medkit.')
            item = args[0]
            field, target, price = {'bag': ('capacity', 70, 900), 'vest': ('armor', 1, 1200),
                                    'weapon': ('weapon', 1, 1600), 'medkit': ('hp', min(100, s['hp'] + 30), 250)}[item]
            if s[field] >= target or s['cash'] < price:
                raise ValueError('Already equipped/healthy, or not enough cash.')
            s[field], s['cash'] = target, s['cash'] - price
            reply = f'Purchased {item}.'
        elif verb == 'bankrupt' and not args:
            s['cash'] = 0
            s['inventory'] = {k: 0 for k in GOODS}
            _finish(s, 'Bankrupt')
            reply = 'Run ended by bankruptcy.'
        elif verb == 'finish' and not args:
            _settle(s)
            reply = 'Inventory liquidated at current prices.'
        else:
            raise ValueError('Invalid command. HELP for commands.')
    except ValueError as exc:
        return deepcopy(state), str(exc), False
    s['moves'] += 1
    return s, reply + '\n\n' + view(s), False


def _amount(raw):
    if not raw.isascii() or not raw.isdigit() or len(raw) > 10 or not 0 < int(raw) <= MAX_CASH:
        raise ValueError('Use a positive whole amount (maximum 1000000000).')
    return int(raw)


def validate(s):
    """Reject malformed/future saves rather than silently resetting them."""
    if not isinstance(s, dict) or set(s) != set(new_game(0)) or s['version'] != VERSION:
        raise ValueError('Unknown save schema')
    limits = {'seed': (0, 2**63 - 1), 'draw': (0, 10**12), 'day': (1, 365),
              'days': (30, 365), 'loan_due': (0, 394),
              'cash': (0, MAX_CASH), 'debt': (0, MAX_CASH), 'hp': (0, 100),
              'capacity': (40, 70), 'weapon': (0, 1), 'armor': (0, 1),
              'enemy_hp': (0, 45), 'moves': (0, 10**12)}
    for key, (low, high) in limits.items():
        if type(s[key]) is not int or not low <= s[key] <= high:
            raise ValueError('Invalid statistic')
    if s['days'] not in (30, 365) or s['day'] > s['days'] or bool(s['debt']) != bool(s['loan_due']):
        raise ValueError('Invalid duration/deadline')
    if s['phase'] not in ('market', 'police', 'ended') or s['place'] not in PLACES:
        raise ValueError('Invalid location/phase')
    if s['outcome'] not in ('', 'Completed', 'Bankrupt', 'Defeated') or bool(s['outcome']) != (s['phase'] == 'ended'):
        raise ValueError('Invalid outcome')
    if (s['phase'] == 'police') != (s['enemy_hp'] > 0) or (s['hp'] == 0 and s['phase'] != 'ended'):
        raise ValueError('Invalid encounter')
    if not isinstance(s['inventory'], dict) or set(s['inventory']) != set(GOODS):
        raise ValueError('Invalid inventory')
    if any(type(q) is not int or q < 0 for q in s['inventory'].values()) or sum(s['inventory'].values()) > s['capacity']:
        raise ValueError('Invalid capacity')
    if not isinstance(s['market'], dict) or set(s['market']) != set(GOODS):
        raise ValueError('Invalid market')
    for offer in s['market'].values():
        if not isinstance(offer, dict) or set(offer) != {'price', 'stock'}:
            raise ValueError('Invalid offer')
        if any(type(offer[k]) is not int or not lo <= offer[k] <= hi for k, lo, hi in
               (('price', 1, 10000), ('stock', 0, 105))):
            raise ValueError('Invalid offer values')
    return s
