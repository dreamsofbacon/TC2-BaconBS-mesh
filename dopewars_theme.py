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

import dopewars as game

CANDY = 'candy'
MATURE = 'mature'

# Icons are per theme, like every other word here.
#
# The engine has its own GOOD_ICONS and its own emoji in the text it returns
# ("POLICE", "Deal", "Bust"), and those are right for Dope Wars. They cannot
# be shown as they are: the menu never prints engine text, which is what
# stops a police siren turning up in a game about a hall monitor. So the
# icons come through here, and Candy Wars gets its own set.

THEMES = {
    CANDY: {
        'title': 'Candy Wars',
        # Short names earn their place: ten of these share one screen
        # with a price and a stock count, inside one Meshtastic packet.
        'goods': {'ludes': 'Gumdrops', 'weed': 'Gum',
                  'speed': 'Pop rocks', 'hash': 'Jelly beans',
                  'mushrooms': 'Cookies', 'opium': 'Nougat',
                  'acid': 'Chocolate', 'oxy': 'Mints',
                  'heroin': 'Toffee', 'cocaine': 'Rock candy'},
        'icons': {'ludes': '\N{DANGO}', 'weed': '\N{BUBBLE TEA}',
                  'speed': '\N{POPCORN}', 'hash': '\N{CANDY}',
                  'mushrooms': '\N{COOKIE}', 'opium': '\N{HONEY POT}',
                  'acid': '\N{CHOCOLATE BAR}', 'oxy': '\N{LOLLIPOP}',
                  'heroin': '\N{CUSTARD}', 'cocaine': '\N{SHORTCAKE}'},
        'encounter_icon': '\N{RAISED HAND}',
        'deal_icon': '\N{PARTY POPPER}',
        'bust_icon': '\N{HOURGLASS WITH FLOWING SAND}',
        'places': {'bronx': 'Playground', 'brooklyn': 'Cafeteria',
                   'manhattan': 'Gym', 'queens': 'Library',
                   'staten-island': 'Art room', 'harlem': 'Music room',
                   'coney-island': 'Schoolyard',
                   'central-park': 'Sports field'},
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
        # Kept short: this rides on the main screen, under a status line
        # that can carry a five-figure debt, and the pair has to fit one
        # packet together. The Dope Wars wording below is Materva's and
        # fits as it is.
        'deal': '{item} is cheap today!',
        'bust': '{item} is scarce today.',
        'loot_cash': 'You found ${amount}.',
        'loot_goods': 'You found {qty} {item}.',
        'loot_full': 'You found a treat, but your backpack was full.',
        'outcomes': {'Completed': 'School is out', 'Bankrupt': 'Broke',
                     'Defeated': 'Sent to the office'},
    },
    MATURE: {
        'title': 'Dope Wars',
        'goods': {'ludes': 'Ludes', 'weed': 'Weed',
                  'speed': 'Speed', 'hash': 'Hash',
                  'mushrooms': 'Shrooms', 'opium': 'Opium',
                  'acid': 'Acid', 'oxy': 'Oxy',
                  'heroin': 'Heroin', 'cocaine': 'Coke'},
        # The engine's own GOOD_ICONS, which is where these came from.
        'icons': dict(game.GOOD_ICONS),
        'encounter_icon': '\N{POLICE CARS REVOLVING LIGHT}',
        'deal_icon': '\N{PACKAGE}',
        'bust_icon': '\N{POLICE CARS REVOLVING LIGHT}',
        'places': {'bronx': 'Bronx', 'brooklyn': 'Brooklyn',
                   'manhattan': 'Manhattan', 'queens': 'Queens',
                   'staten-island': 'Staten Is.', 'harlem': 'Harlem',
                   'coney-island': 'Coney Is.',
                   'central-park': 'Central Pk'},
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
