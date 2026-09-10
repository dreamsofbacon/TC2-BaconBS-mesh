"""Baconfall: The Last Sizzle. Pure, serializable turn-based game engine.

No clocks, network calls or process-global player state. A saved seed and draw
counter make encounters reproducible across restarts without storing RNG blobs.
"""
from copy import deepcopy
import random

GAME_ID = 'baconfall'
GAME_NAME = 'Baconfall: The Last Sizzle'
VERSION = 1
ROLES = {
    '1': ('Iron Rind', 52, 6, 2, 'Guard counters for 3 damage.'),
    '2': ('Smoke Ranger', 46, 8, 1, 'Strike deals +2 against charging foes.'),
    '3': ('Maple Witch', 50, 6, 1, 'Sizzle costs 2 heat instead of 3.'),
}
REGIONS = ('Hickory Hollows', 'Saltglass Dunes', 'Black Skillet Citadel')
BOSSES = ('The Boar of Cinders', 'The Salt Wyrm', 'The Hollow Butcher', 'The Ash King')
RELICS = {
    '1': ('Hickory Heart', '+12 max HP; heal 12.'),
    '2': ('Saltsteel Fang', '+2 strike damage.'),
    '3': ('Maple Ember', 'Sizzle deals +5 damage.'),
}
ENEMIES = (
    ('Grease Imp', 'Char Hound', 'Rind Raider'),
    ('Salt Revenant', 'Pepper Bandit', 'Dune Gnasher'),
    ('Cinder Knight', 'Smoke Wraith', 'Skillet Golem'),
)
INTRO = (
    'BACONFALL: THE LAST SIZZLE\n'
    'The Ash King has stolen the First Flame. Breakfast is turning to ash. '
    'Cross three realms, claim three bacon relics, and bring the sizzle home.\n'
    '[1] Iron Rind: tough, counterattacks\n[2] Smoke Ranger: hard hitter\n'
    '[3] Maple Witch: stronger, cheaper fire magic\nChoose your hero. H help | X save & leave'
)
HELP = (
    'BACONFALL FIELD GUIDE\n'
    'Choose numbered routes and rewards. Beat each realm guardian, then the Ash King.\n'
    'Combat: A strike (+1 heat), G guard (+2 heat, blocks 7 damage), '
    'S sizzle (12 piercing damage, costs 3 heat), E eat (one ration, heals 16). '
    'Heat caps at 6. Enemy intent shows its NEXT action. Guard heavy blows; '
    'sizzle through armor. Maple Witch deals 14 for 2 heat. Eating still gives the enemy a turn.\n'
    'H help | I hero | M map | L look: free, no enemy turn. X saves and leaves. '
    'NEW asks before abandoning a run. No timer. Death ends the expedition.'
)


def new_game(seed):
    return dict(version=VERSION, seed=seed, draw=0, phase='hero', role='',
                hp=0, max_hp=0, attack=0, armor=0, heat=0, rations=3,
                gold=0, act=0, room=0, moves=0, renown=0, relics=[],
                sizzle=12, enemy=None, routes=[], confirm=False, outcome='')


def _roll(s, low, high):
    result = random.Random(f"{s['seed']}:{s['draw']}").randint(low, high)
    s['draw'] += 1
    return result


def score(s):
    # Finite rooms/rewards; no points for waiting, hoarding or repeat commands.
    return s['renown'] + (1000 + s['hp'] * 5 if s['outcome'] == 'victory' else 0)


def _map(s):
    lines = ['THE ROAD TO THE FIRST FLAME']
    for i, region in enumerate(REGIONS):
        mark = 'cleared' if i < s['act'] else ('here' if i == s['act'] else 'ahead')
        lines.append(f'{i + 1}. {region} [{mark}]')
    lines.append('4. The Ash Throne' + (' [here]' if s['act'] == 3 else ''))
    return '\n'.join(lines)


def _hero(s):
    return (f"{s['role']} | HP {s['hp']}/{s['max_hp']} | heat {s['heat']}/6\n"
            f"Strike {s['attack']} | armor {s['armor']} | sizzle {s['sizzle']}\n"
            f"Rations {s['rations']} | fat coins {s['gold']} | score {score(s)}\n"
            f"Relics: {', '.join(s['relics']) or 'none'}")


def _intent(s):
    e = s['enemy']
    return e['pattern'][e['turn'] % len(e['pattern'])]


def view(s):
    if s['confirm']:
        return 'Abandon this expedition? YES starts hero selection; NO keeps your save.'
    p = s['phase']
    if p == 'hero':
        return INTRO
    if p == 'ended':
        ending = ('DAWN OF THE BACON AGE! You free the First Flame. Across the mesh, '
                  'silent skillets sing again. Breakfast is saved.' if s['outcome'] == 'victory'
                  else 'Your flame goes dark. The smoke carries your name home.')
        return f"{ending}\nScore {score(s)} | {s['moves']} turns | {len(s['relics'])} relics\nNEW expedition | X games"
    status = f"HP {s['hp']}/{s['max_hp']} | heat {s['heat']}/6 | food {s['rations']} | coins {s['gold']}"
    if p == 'combat':
        e = s['enemy']
        intent = _intent(s)
        tells = {'jab': f"slashes for {e['power']}", 'crush': f"CRUSHES for {e['power'] + 7}",
                 'brace': 'braces: strike damage reduced by 5', 'charge': 'charges: no attack'}
        return (f"{e['name']} | HP {e['hp']}/{e['max_hp']}\nIntent: {tells[intent]}\n{status}\n"
                'A strike | G guard | S sizzle | E eat\nH help | I hero | M map | X save')
    if p == 'relic':
        options = '\n'.join(f'[{k}] {name}: {effect}' for k, (name, effect) in RELICS.items())
        return f'The guardian falls. Claim a bacon relic (duplicates stack):\n{options}'
    if p == 'camp':
        return (f"Last hearth before {BOSSES[s['act']]}. Choose ONE blessing:\n{status}\n"
                '[1] Rest: heal 18, free\n[2] Forge: +2 strike, 12 coins\n'
                '[3] Feast: +8 max HP, heal 8, 10 coins\n[4] Pack: +2 rations, 8 coins\nX save | H help')
    if p == 'event':
        return (f"The Bacon Beacon sputters. A stranded cook offers a bargain.\n{status}\n"
                '[1] Share 1 ration: heal 12, earn 60 renown\n'
                '[2] Stoke with 6 HP: +2 strike\n[3] Salvage: gain 7 coins')
    if p == 'cache':
        return (f"A sealed smokehouse! Take one supply:\n{status}\n"
                '[1] 2 rations\n[2] 10 fat coins\n[3] Cool spring: heal 14, +2 heat')
    choices = '\n'.join(f'[{i}] {label}' for i, (_, label) in enumerate(s['routes'], 1))
    return (f"{REGIONS[s['act']]} | crossing {s['room'] + 1}/3\n{status}\n"
            f'{choices}\nH help | I hero | M map | X save')


def _routes(s):
    s['phase'] = 'route'
    pool = [('battle', 'Hunt a raider: fight, 9 coins, 80 renown'),
            ('elite', 'Raid a warband: harder fight, 17 coins, 140 renown'),
            ('cache', 'Search a smokehouse: supplies'),
            ('event', 'Follow the bacon beacon: a bargain')]
    first = _roll(s, 0, 3)
    second = (first + _roll(s, 1, 3)) % 4
    s['routes'] = [list(pool[first]), list(pool[second])]


def _advance(s):
    s['room'] += 1
    if s['room'] == 3:
        s['phase'] = 'camp'
    else:
        _routes(s)


def _fight(s, kind):
    act = s['act']
    boss = kind == 'boss'
    elite = kind == 'elite'
    hp = (46 + 14 * act) if boss else (17 + 9 * act + (12 if elite else 0))
    power = (9 + 2 * act) if boss else (5 + 2 * act + (2 if elite else 0))
    pattern = (['charge', 'crush', 'brace', 'jab'] if boss else
               (['jab', 'charge', 'crush'] if elite else ['jab', 'brace', 'charge', 'crush']))
    name = BOSSES[act] if boss else ENEMIES[act][_roll(s, 0, 2)]
    s['enemy'] = dict(name=name, hp=hp, max_hp=hp, power=power,
                      pattern=pattern, turn=0, kind=kind, enraged=False)
    s['phase'] = 'combat'


def _combat(s, key):
    e = s['enemy']
    cost = 2 if s['role'] == 'Maple Witch' else 3
    if key not in ('a', 'g', 's', 'e'):
        return 'Choose A, G, S or E. H explains combat.', False
    if key == 's' and s['heat'] < cost:
        return f'Sizzle needs {cost} heat. Strike or guard to build heat.', False
    if key == 'e' and (s['rations'] == 0 or s['hp'] == s['max_hp']):
        return 'No ration needed or none left. Choose another action.', False
    intent = _intent(s)
    hit = 0
    if key == 'a':
        hit = s['attack'] + (2 if s['role'] == 'Smoke Ranger' and intent == 'charge' else 0)
        hit = max(1, hit - (5 if intent == 'brace' else 0))
        s['heat'] = min(6, s['heat'] + 1)
    elif key == 's':
        hit = s['sizzle']
        s['heat'] -= cost
    elif key == 'g':
        s['heat'] = min(6, s['heat'] + 2)
    else:
        s['rations'] -= 1
        s['hp'] = min(s['max_hp'], s['hp'] + 16)
    e['hp'] -= hit
    lines = [f'You deal {hit}.' if hit else ('You guard.' if key == 'g' else 'You eat: heal up to 16.')]
    if e['hp'] > 0:
        damage = e['power'] + (7 if intent == 'crush' else 0) if intent in ('jab', 'crush') else 0
        taken = max(0, damage - s['armor'] - (7 if key == 'g' else 0))
        s['hp'] = max(0, s['hp'] - taken)
        if damage:
            lines.append(f'Enemy deals {taken}.')
            if key == 'g' and s['role'] == 'Iron Rind':
                e['hp'] -= 3
                lines.append('Iron Rind counters for 3.')
        e['turn'] += 1
    if s['hp'] == 0:
        s['phase'], s['outcome'] = 'ended', 'defeat'
    elif e['hp'] <= 0:
        kind = e['kind']
        reward = {'battle': (9, 80), 'elite': (17, 140), 'boss': (15, 250)}[kind]
        s['gold'] += reward[0]
        s['renown'] += reward[1]
        lines.append(f"{e['name']} defeated! +{reward[0]} coins, +{reward[1]} renown.")
        if kind == 'boss':
            if s['act'] == 3:
                s['phase'], s['outcome'] = 'ended', 'victory'
            else:
                s['phase'] = 'relic'
        else:
            _advance(s)
    elif e['name'] == 'The Ash King' and not e['enraged'] and e['hp'] <= e['max_hp'] // 2:
        e['enraged'] = True
        e['power'] += 3
        lines.append('The crown cracks! The Ash King gains +3 attack. Watch his intent.')
    return ' '.join(lines), True


def command(state, text):
    """Return (new state, reply, leave). Invalid/help commands cost no turns."""
    s = deepcopy(state)
    key = text.strip().lower()
    if key in ('x', '0', 'exit', '!x', '!0', '!exit', '!cancel'):
        s['confirm'] = False
        return s, 'Expedition saved on this BBS node. Choose Baconfall to resume.', True
    if s['confirm']:
        if key == 'yes':
            # New seed is derived from this run, not supplied by user input.
            s = new_game(_roll(s, 0, 2**63 - 1))
            return s, view(s), False
        if key == 'no':
            s['confirm'] = False
        return s, view(s), False
    if key == 'new':
        s['confirm'] = True
        return s, view(s), False
    if key in ('h', 'help', '?'):
        return s, HELP, False
    if key in ('l', 'look', ''):
        return s, view(s), False
    if key in ('i', 'hero', 'status', 'm', 'map'):
        return s, (_hero(s) if key in ('i', 'hero', 'status') else _map(s)), False
    phase = s['phase']
    note = ''
    if phase == 'hero' and key in ROLES:
        role, hp, attack, armor, _ = ROLES[key]
        s.update(role=role, hp=hp, max_hp=hp, attack=attack, armor=armor,
                 sizzle=14 if role == 'Maple Witch' else 12)
        _routes(s)
        note = 'The skillet gates open. Your expedition begins.'
    elif phase == 'route' and key in ('1', '2'):
        kind = s['routes'][int(key) - 1][0]
        if kind in ('battle', 'elite'):
            _fight(s, kind)
        else:
            s['phase'] = kind
    elif phase == 'combat':
        note, valid = _combat(s, key)
        if not valid:
            return s, note, False
    elif phase in ('event', 'cache', 'relic', 'camp') and key in ('1', '2', '3', '4'):
        if key == '4' and phase != 'camp':
            return s, 'Choose 1, 2 or 3.', False
        if phase == 'cache':
            if key == '1':
                s['rations'] += 2
                note = 'You pack two bacon rations.'
            elif key == '2':
                s['gold'] += 10
                note = 'You pocket ten fat coins.'
            else:
                s['hp'] = min(s['max_hp'], s['hp'] + 14)
                s['heat'] = min(6, s['heat'] + 2)
                note = 'Springwater restores up to 14 HP and 2 heat.'
            _advance(s)
        elif phase == 'event':
            if key == '1':
                if s['rations'] < 1:
                    return s, 'You need a ration to share. Choose 2 or 3.', False
                s['rations'] -= 1
                s['hp'] = min(s['max_hp'], s['hp'] + 12)
                s['renown'] += 60
                note = 'The cook shares a restorative broth. +60 renown, heal up to 12.'
            elif key == '2':
                if s['hp'] <= 6:
                    return s, 'That would extinguish your flame. Choose 1 or 3.', False
                s['hp'] -= 6
                s['attack'] += 2
                note = 'You kindle the beacon with your flame. -6 HP, +2 strike.'
            else:
                s['gold'] += 7
                note = 'You salvage seven fat coins.'
            _advance(s)
        elif phase == 'camp':
            price = {'1': 0, '2': 12, '3': 10, '4': 8}[key]
            if s['gold'] < price:
                return s, 'Not enough coins. Rest is free, or choose another blessing.', False
            s['gold'] -= price
            if key == '1':
                s['hp'] = min(s['max_hp'], s['hp'] + 18)
            elif key == '2':
                s['attack'] += 2
            elif key == '3':
                s['max_hp'] += 8
                s['hp'] = min(s['max_hp'], s['hp'] + 8)
            else:
                s['rations'] += 2
            note = 'The hearth fades. The guardian awakens.'
            _fight(s, 'boss')
        else:
            name = RELICS[key][0]
            s['relics'].append(name)
            if key == '1':
                s['max_hp'] += 12
                s['hp'] += 12
            elif key == '2':
                s['attack'] += 2
            else:
                s['sizzle'] += 5
            s['act'] += 1
            s['room'] = 0
            s['rations'] += 1
            s['hp'] = min(s['max_hp'], s['hp'] + 10)
            note = f'{name} claimed. A rescued cook gives you 1 ration and heals up to 10 HP.'
            if s['act'] == 3:
                _fight(s, 'boss')
                note += ' Three relics ignite the road to the Ash Throne!'
            else:
                _routes(s)
    else:
        return s, 'Choose an option shown below.\n' + view(s), False
    s['moves'] += 1
    return s, (note + '\n' if note else '') + view(s), False
