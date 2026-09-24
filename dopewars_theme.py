"""The words the trading door is played in: Candy Wars, or Dope Wars.

The engine (dopewars.py) knows its goods and places by fixed ids -- weed,
docks -- and those ids are in every save and every score. Nothing here
changes them. A theme is only the words a player reads, so a run is the same
run whichever theme it is viewed in, and switching PG-13 mode mid-run changes
the vocabulary and nothing else.

Candy Wars is the default: a school, the Hall Monitor, and no fighting,
weapons or drugs anywhere in its text. tests/test_candywars.py drives every
screen in this theme against a list of words that must never appear.
"""

CANDY = 'candy'
MATURE = 'mature'

THEMES = {
    CANDY: {
        'title': 'Candy Wars',
        'goods': {'weed': 'Gum', 'hash': 'Jelly beans',
                  'mushrooms': 'Cookies', 'acid': 'Chocolate',
                  'oxy': 'Mints', 'cocaine': 'Rock candy'},
        'places': {'bronx': 'Playground', 'brooklyn': 'Cafeteria',
                   'manhattan': 'Gym', 'queens': 'Library',
                   'staten-island': 'Art room'},
        'hp': 'Energy',
        'owed': 'owe',
        'loan': 'Sibling',
        'loan_long': 'your big sibling',
        'gear': {'bag': ('Bigger backpack', '+30 room'),
                 'vest': ('Hoodie', 'lose less energy'),
                 'weapon': ('Hall pass', 'talk your way out'),
                 'medkit': ('Juice box', '+30 energy')},
        'encounter': 'The Hall Monitor stops you!',
        'fight': 'Talk your way out',
        'run': 'Run for it',
        'surrender': 'Hand over your candy',
        'fought_off': 'You talked your way out of it.',
        'escaped': 'You got away.',
        'hit': 'The Hall Monitor gives you a lecture. -{n} energy.',
        'surrendered': 'You handed over your candy and ${fine} of your allowance.',
        'defeated': 'Out of energy. Sent to the office -- the run is over.',
        'deadline': "You didn't pay your big sibling back in time. The run is over.",
        'deal': 'A big delivery made {item} cheap!',
        'bust': '{item} is hard to find today.',
        'loot_cash': 'You found ${amount}.',
        'loot_goods': 'You found {qty} {item}.',
        'loot_full': 'You found a treat, but your backpack was full.',
        'outcomes': {'Completed': 'School is out', 'Bankrupt': 'Broke',
                     'Defeated': 'Sent to the office'},
    },
    MATURE: {
        'title': 'Dope Wars',
        'goods': {'weed': 'Weed', 'hash': 'Hash',
                  'mushrooms': 'Mushrooms', 'acid': 'Acid',
                  'oxy': 'Oxy', 'cocaine': 'Cocaine'},
        'places': {'bronx': 'Bronx', 'brooklyn': 'Brooklyn',
                   'manhattan': 'Manhattan', 'queens': 'Queens',
                   'staten-island': 'Staten Island'},
        'hp': 'HP',
        'owed': 'debt',
        'loan': 'Loan',
        'loan_long': 'the loan shark',
        'gear': {'bag': ('Bigger bag', '+30 room'),
                 'vest': ('Vest', 'take less damage'),
                 'weapon': ('Weapon', 'hit harder'),
                 'medkit': ('Medkit', '+30 HP')},
        'encounter': 'Police stop you!',
        'fight': 'Fight',
        'run': 'Run',
        'surrender': 'Surrender',
        'fought_off': 'Police driven off.',
        'escaped': 'Escaped.',
        'hit': 'You were hit. -{n} HP.',
        'surrendered': 'Goods confiscated; paid a ${fine} fine.',
        'defeated': 'Defeated. You lost your cash -- the run is over.',
        'deadline': 'Loan deadline missed. The run is over.',
        'deal': 'A shipment made {item} cheap!',
        'bust': 'A bust made {item} scarce.',
        'loot_cash': 'Street loot: found ${amount}.',
        'loot_goods': 'Street loot: found {qty} {item}.',
        'loot_full': 'Street loot spotted, but your bag was full.',
        'outcomes': {'Completed': 'Completed', 'Bankrupt': 'Bankrupt',
                     'Defeated': 'Defeated'},
    },
}


def theme(pg13: bool) -> dict:
    return THEMES[MATURE if pg13 else CANDY]


def title(pg13: bool) -> str:
    return theme(pg13)['title']
