"""DopeWars: original text-only trading rules. SPDX-License-Identifier: GPL-3.0-only.

No GTK or database dependencies. Commands return a new JSON-safe state; a saved
seed and draw counter define randomness independently of Python's random module.
See docs/dopewars.md for provenance and deliberately simplified rules.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

GAME_ID = 'dopewars'
VERSION = 5
DAYS = 30
MAX_CASH = 1_000_000_000
LOAN_LIMIT = 10_000
BASE_STOCK_MIN = 10
BASE_STOCK_MAX = 60
COCAINE_BUST_PRICE_MIN = 300
COCAINE_BUST_PRICE_MAX = 500
COCAINE_BUST_STOCK_MAX = 3
# Cheapest first: the menu numbers goods in this order, and a trading
# screen reads best as a price list.
GOODS = {'ludes': 40, 'weed': 90, 'speed': 150, 'peyote': 190,
         'hash': 230, 'mushrooms': 320, 'mda': 380, 'opium': 430,
         'acid': 540, 'ketamine': 620, 'meth': 750, 'oxy': 900,
         'pcp': 1000, 'heroin': 1250, 'crystal': 1500, 'cocaine': 1800}
NYC = 'new-york'
CHICAGO = 'chicago'
SAN_DIEGO = 'san-diego'
TIJUANA = 'tijuana'
NEW_ORLEANS = 'new-orleans'
MIAMI = 'miami'
CITIES = (NYC, NEW_ORLEANS, CHICAGO, MIAMI, SAN_DIEGO, TIJUANA)
CITY_LABELS = {NYC: 'New York', NEW_ORLEANS: 'New Orleans',
               CHICAGO: 'Chicago', MIAMI: 'Miami',
               SAN_DIEGO: 'San Diego', TIJUANA: 'Tijuana'}
CITY_PLACES = {
    NYC: ('bronx', 'brooklyn', 'manhattan', 'queens', 'staten-island',
          'harlem', 'coney-island', 'central-park'),
    NEW_ORLEANS: ('kenner', 'french-quarter', 'central-business-district',
                  'garden-district', 'treme', 'bywater',
                  'uptown-new-orleans', 'mid-city'),
    CHICAGO: ('chicago-loop', 'chicago-river-north', 'chicago-wicker-park',
              'chicago-logan-square', 'chicago-pilsen', 'chicago-ohare',
              'chicago-hyde-park', 'chicago-bronzeville'),
    SAN_DIEGO: ('san-diego-middletown', 'san-diego-little-italy',
                'san-diego-barrio-logan', 'san-diego-hillcrest',
                'san-diego-north-park', 'san-diego-pacific-beach',
                'san-diego-ocean-beach', 'san-ysidro'),
    MIAMI: ('flagami', 'downtown-miami', 'brickell', 'south-beach',
            'little-havana', 'wynwood', 'coconut-grove', 'edgewater'),
    TIJUANA: ('tijuana-zona-rio', 'tijuana-centro', 'tijuana-playas',
              'tijuana-otay', 'tijuana-la-mesa', 'tijuana-five-ten',
              'tijuana-agua-caliente', 'tijuana-libertad'),
}
PLACES = tuple(place for city in CITIES for place in CITY_PLACES[city])
PLACE_CITY = {place: city for city, places in CITY_PLACES.items() for place in places}
AIRPORTS = {NYC: 'queens', NEW_ORLEANS: 'kenner',
            CHICAGO: 'chicago-ohare', MIAMI: 'flagami',
            SAN_DIEGO: 'san-diego-middletown'}
BORDER_PLACES = {'san-ysidro': 'tijuana-zona-rio',
                 'tijuana-zona-rio': 'san-ysidro',
                 'tijuana-centro': 'san-ysidro',
                 'tijuana-playas': 'san-ysidro',
                 'tijuana-otay': 'san-ysidro',
                 'tijuana-la-mesa': 'san-ysidro',
                 'tijuana-five-ten': 'san-ysidro',
                 'tijuana-agua-caliente': 'san-ysidro',
                 'tijuana-libertad': 'san-ysidro'}
FLIGHT_FARE_BANDS = {
    frozenset((CHICAGO, SAN_DIEGO)): (175, 325),
    frozenset((NYC, SAN_DIEGO)): (200, 350),
    frozenset((NYC, CHICAGO)): (200, 350),
    frozenset((NYC, NEW_ORLEANS)): (175, 325),
    frozenset((NYC, MIAMI)): (200, 350),
    frozenset((NEW_ORLEANS, CHICAGO)): (175, 325),
    frozenset((NEW_ORLEANS, MIAMI)): (125, 275),
    frozenset((NEW_ORLEANS, SAN_DIEGO)): (175, 325),
    frozenset((CHICAGO, MIAMI)): (175, 325),
    frozenset((MIAMI, SAN_DIEGO)): (125, 275),
}
# The bank is in one town on purpose. Reachable everywhere it would be
# free insurance -- deposit before every trip, withdraw on arrival --
# which is all keypresses and no decision. Having to go there makes
# banking a detour weighed against the risk of carrying.
BANK_PLACE = 'brooklyn'
PLACE_LABELS = {
    'bronx': 'Bronx', 'brooklyn': 'Brooklyn', 'manhattan': 'Manhattan',
    'queens': 'Queens', 'staten-island': 'Staten Is.', 'harlem': 'Harlem',
    'coney-island': 'Coney Is.', 'central-park': 'Central Pk',
    'chicago-loop': 'The Loop', 'chicago-river-north': 'River North',
    'chicago-wicker-park': 'Wicker Park', 'chicago-logan-square': 'Logan Square',
    'chicago-pilsen': 'Pilsen', 'chicago-ohare': "O'Hare",
    'chicago-hyde-park': 'Hyde Park', 'chicago-bronzeville': 'Bronzeville',
    'san-diego-middletown': 'Middletown', 'san-diego-little-italy': 'Little Italy',
    'san-diego-barrio-logan': 'Barrio Logan', 'san-diego-hillcrest': 'Hillcrest',
    'san-diego-north-park': 'North Park', 'san-diego-pacific-beach': 'Pacific Beach',
    'san-diego-ocean-beach': 'Ocean Beach', 'san-ysidro': 'San Ysidro',
    'tijuana-zona-rio': 'Zona Río', 'tijuana-centro': 'Centro',
    'tijuana-playas': 'Playas', 'tijuana-otay': 'Otay',
    'tijuana-la-mesa': 'La Mesa', 'tijuana-five-ten': '5 y 10',
    'tijuana-agua-caliente': 'Agua Caliente', 'tijuana-libertad': 'Libertad',
    'tijuana': 'Tijuana',
    # Legacy labels used while constructing the replaced inline catalog.
    'kenner': 'Kenner', 'french-quarter': 'French Quarter',
    'central-business-district': 'Central Business District',
    'garden-district': 'Garden District', 'treme': 'Treme',
    'bywater': 'Bywater', 'uptown-new-orleans': 'Uptown', 'mid-city': 'Mid-City',
    'flagami': 'Flagami', 'downtown-miami': 'Downtown', 'brickell': 'Brickell',
    'south-beach': 'South Beach', 'little-havana': 'Little Havana',
    'wynwood': 'Wynwood', 'coconut-grove': 'Coconut Grove', 'edgewater': 'Edgewater',
}

# Arrival copy is deliberately short: the numbered door puts it above the
# status screen, which must fit in one Meshtastic packet.
CITY_DESCRIPTIONS = {
    NYC: (
        'New York rises around you: busy streets, bright signs, and opportunity.',
        'The city hums with traffic, horns, and deals waiting on every corner.',
        'Skyscrapers cut the sky while the sidewalks carry a steady current of people.',
        'Cold wind funnels between buildings, carrying the smell of food and rain.',
        'New York never seems to sleep; somewhere nearby, a market is opening.',
        'The streets are crowded, loud, and full of places to disappear into.',
        'A restless city stretches in every direction, stitched together by trains.',
        'Neon, brick, and concrete make a hard-edged maze around you.',
        'You arrive beneath a skyline that makes every plan feel possible.',
        'The city greets you with rushing feet and a thousand competing sounds.',
    ),
    NEW_ORLEANS: (
        'New Orleans welcomes you with warm air, old brick, and music in the distance.',
        'The city feels relaxed on the surface, but every street has a story.',
        'Humidity hangs over the roads as the smell of food drifts from nearby kitchens.',
        'Balconies, battered walls, and bright signs frame the city around you.',
        'A brass note floats through the air somewhere beyond the next block.',
        'The streets are slow-moving, colorful, and never quite predictable.',
        'Warm weather and older buildings give the city an unmistakable character.',
        'The city is alive with porch talk, cooking smells, and distant music.',
        'Rain clouds gather over a city that knows how to keep moving.',
        'New Orleans feels like a conversation already in progress.',
    ),
    MIAMI: (
        'Miami greets you with bright sun, warm air, and water somewhere nearby.',
        'Palm trees sway over streets where business and pleasure share the pavement.',
        'The heat settles in quickly, softened by a breeze off the coast.',
        'Colorful buildings and polished towers rise beneath a wide blue sky.',
        'The city glitters in the sun, but the best opportunities hide in the shade.',
        'Music, traffic, and ocean air mix together around you.',
        'Miami moves at a quick pace, even when the heat tells you to slow down.',
        'A storm may be brewing offshore; for now, the streets are bright and busy.',
        'The coast is close, the weather is warm, and nobody looks surprised to see you.',
        'Sunlight flashes off glass and water as the city opens up around you.',
    ),
}

DISTRICT_DESCRIPTIONS = {
    place: tuple(f'{PLACE_LABELS[place]}: {text}.' for text in texts)
    for place, texts in {
        'bronx': ('Block after block carries its own rhythm', 'The neighborhood is lively and watchful', 'Street art brightens the concrete', 'Local traffic fills the avenue', 'A cool breeze cuts between the buildings', 'People move with somewhere to be', 'The sidewalks offer plenty of cover', 'A corner shop does brisk business', 'The neighborhood feels close-knit', 'The weather changes nothing about the pace'),
        'brooklyn': ('Brownstones line the busy streets', 'The neighborhood balances old brick and new money', 'A bakery smell follows you down the block', 'Delivery trucks squeeze past parked cars', 'The sidewalks are crowded but orderly', 'Small businesses keep the corners bright', 'A train rumbles somewhere nearby', 'The air is cool and carries a hint of rain', 'Locals watch the street without staring', 'There is always another side street to explore'),
        'manhattan': ('Tall buildings turn the street into a canyon', 'Crowds stream past without slowing down', 'Taxis and delivery bikes compete for space', 'The skyline disappears into low clouds', 'Every block feels like a different world', 'Office workers spill into the streets', 'Bright signs reflect off wet pavement', 'The city noise is almost physical here', 'A subway entrance breathes warm air nearby', 'There is no shortage of eyes on the street'),
        'queens': ('Air travelers and locals share the busy roads', 'The neighborhood is a patchwork of languages and food', 'Planes pass overhead on their way to the runway', 'A steady breeze moves through the broad streets', 'Small stores crowd the corners', 'The weather is mild, but the traffic is not', 'The area feels practical and always in motion', 'You can hear several neighborhoods at once', 'Rain beads on signs and windshields', 'The district is an easy place to blend in'),
        'staten-island': ('The water is never far from the quieter streets', 'A ferry horn sounds across the gray morning', 'The district feels calmer than the city across the bay', 'Wind comes off the harbor carrying salt', 'Small roads wind past older homes', 'Clouds move quickly over the shoreline', 'The neighborhood keeps its own pace', 'A damp chill settles over the waterfront', 'The view is peaceful, but business still moves', 'The island feels removed without being empty'),
        'harlem': ('Music and conversation spill onto the sidewalks', 'Brownstone blocks glow in the afternoon light', 'The neighborhood is proud, busy, and observant', 'A warm breeze carries food smells down the avenue', 'People gather beneath awnings to escape the weather', 'The street corners are full of stories', 'Rain darkens the brick and slows the traffic', 'A distant beat keeps time with the city', 'The area feels welcoming but nobody misses much', 'The district has energy in every direction'),
        'coney-island': ('The boardwalk air smells of salt and fried food', 'Bright signs stand out beneath the open sky', 'A sea breeze keeps the heat moving', 'The beach is busy despite the gathering clouds', 'Tourists and locals weave through the same streets', 'The district feels festive even on a quiet day', 'Waves roll in beyond the buildings', 'Wind snaps at awnings along the avenue', 'The shoreline gives the neighborhood its mood', 'Summer seems close even when the sky is gray'),
        'central-park': ('Trees and paths break up the surrounding city noise', 'Joggers and cyclists pass beneath a clear sky', 'The park air is cooler than the streets outside', 'A sudden shower darkens the paths', 'Open green space makes the skyline look distant', 'Birdsong competes with traffic beyond the trees', 'Visitors gather wherever the sun breaks through', 'The paths offer many routes and few explanations', 'Wind moves through the branches overhead', 'The district is peaceful, but never completely quiet'),
        'kenner': ('Runways and low roads spread out beneath the humid sky', 'Travelers hurry past with bags and tired eyes', 'The airport district runs on schedules and coffee', 'Warm rain taps against the terminal windows', 'A plane climbs overhead as traffic crawls below', 'The district feels temporary, built for arrivals and departures', 'Bright signs point in every direction', 'The air smells of jet fuel and wet pavement', 'A calm breeze crosses the broad airport roads', 'People here are always headed somewhere else'),
        'french-quarter': ('Old balconies overlook streets full of color and noise', 'Music leaks from doorways into the warm evening air', 'The pavement shines after a sudden shower', 'Food, rain, and river air mingle around you', 'The district is crowded, bright, and hard to read', 'Ironwork shadows stretch across the old brick', 'A brass rhythm echoes from somewhere nearby', 'The heat makes every shaded doorway valuable', 'Tourists drift while locals move with purpose', 'The Quarter feels awake even before sunset'),
        'central-business-district': ('Glass towers rise over busy, practical streets', 'Workers hurry beneath a sky heavy with rain', 'The district is all offices, traffic, and quick decisions', 'Warm air funnels between the taller buildings', 'Lunch crowds fill the sidewalks', 'The skyline reflects in puddles along the curb', 'Delivery vans and pedestrians compete for every lane', 'The city feels focused here', 'A brief storm sends everyone under cover', 'Business continues no matter what the weather does'),
        'garden-district': ('Shaded streets pass grand homes and old live oaks', 'The air is warm and carries the scent of wet leaves', 'Porches and gardens soften the city noise', 'A slow rain darkens the broad sidewalks', 'The neighborhood is quiet enough to hear birds', 'Moss hangs above streets that seem older than the traffic', 'Sunlight breaks through the branches in patches', 'The district feels elegant, watchful, and lived in', 'A warm breeze moves through the gardens', 'Old walls hide newer stories'),
        'treme': ('Porches, music, and conversation fill the warm air', 'The neighborhood wears its history openly', 'A passing shower leaves the streets shining', 'Food smells drift from homes and corner kitchens', 'The district is lively without needing to hurry', 'Music competes with the hum of traffic', 'People watch the weather and carry on', 'The heat settles over the rooftops', 'Every block feels connected to the next', 'The streets have a rhythm of their own'),
        'bywater': ('Bright homes and industrial edges share the same horizon', 'The river breeze cuts through the heavy heat', 'Colorful walls stand out beneath gathering clouds', 'The district feels creative, rough, and open-ended', 'Rainwater gathers quickly along the uneven streets', 'Music and machinery echo from different directions', 'The air is thick with weather from the river', 'Old buildings hold up beneath a bright sky', 'A slow afternoon settles over the neighborhood', 'There is room here for strange plans'),
        'uptown-new-orleans': ('Live oaks shade long streets and busy porches', 'The warm air carries music from farther down the block', 'Rain clouds build over a neighborhood that keeps moving', 'The streets mix old homes with fresh activity', 'A humid breeze moves through the tree canopy', 'People linger outside while the weather allows it', 'The district feels settled but never still', 'Sunlight flashes across wet pavement', 'Food and conversation travel easily here', 'The neighborhood has a comfortable confidence'),
        'mid-city': ('Canals, roads, and old buildings meet under wide skies', 'The neighborhood is busy with practical movement', 'A humid breeze carries the promise of rain', 'Water glints beyond the traffic', 'The district feels central without feeling polished', 'A short storm rolls over the rooftops', 'Street life gathers around every useful corner', 'The air is warm and heavy with summer', 'People know their routes through this maze', 'The neighborhood keeps going after dark'),
        'flagami': ('Wide roads, palms, and airport traffic fill the warm air', 'The district moves between warehouses, homes, and runways', 'Bright sun bounces off cars and low buildings', 'A tropical shower passes quickly overhead', 'The heat is strong, but the breeze helps', 'Travelers and locals share the same busy corners', 'The neighborhood feels practical and close to the airport', 'Clouds gather over a very bright street', 'Traffic carries the sound of the city in every direction', 'There is always a flight or a deal nearby'),
        'downtown-miami': ('Glass towers shine above streets warmed by the sun', 'The bay breeze reaches between the buildings', 'Workers and tourists fill the sidewalks', 'A quick rain leaves the pavement gleaming', 'The skyline looks sharp beneath the blue sky', 'Traffic, music, and construction compete for attention', 'Heat rises from the street after noon', 'Storm clouds build beyond the towers', 'The district is polished on one block and rough on the next', 'Water and concrete frame every decision'),
        'brickell': ('Towers and palms rise together along busy streets', 'The air is warm, polished, and full of traffic', 'A sea breeze reaches the shaded sidewalks', 'Rain runs down glass while business continues below', 'The district glitters even under storm clouds', 'Restaurants and offices keep the streets active', 'Sunlight flashes across towers and expensive cars', 'The heat makes every patch of shade valuable', 'The neighborhood feels fast and carefully dressed', 'The bay is close, but the city is closer'),
        'south-beach': ('Bright buildings face a hot breeze from the water', 'Music and traffic follow the shoreline', 'The sun is strong and the streets are busy', 'A sudden shower sends people beneath bright awnings', 'Salt air mixes with food and fuel', 'Palm shadows stretch across the pavement', 'The district stays lively after the heat fades', 'Clouds build over the water and move on', 'Colorful walls and white sand shape the view', 'Everyone here seems to be going somewhere fun'),
        'little-havana': ('Music, coffee, and warm air fill the lively streets', 'The neighborhood is colorful, crowded, and welcoming', 'A tropical rain leaves the sidewalks shining', 'Food smells travel from open doors and busy kitchens', 'The heat encourages a slower pace', 'Conversation carries easily from one corner to the next', 'Bright signs stand out beneath the afternoon clouds', 'The district feels social even when the street is quiet', 'A warm breeze moves through the palms', 'There is always a song somewhere nearby'),
        'wynwood': ('Murals turn nearly every wall into a landmark', 'Bright paint and hot pavement make the district glow', 'A quick storm darkens the colorful streets', 'Music and conversation spill from open doors', 'The air is warm, damp, and full of possibility', 'Artists, tourists, and locals share the same corners', 'Sunlight brings new details out of every wall', 'The district looks different from every direction', 'A humid breeze carries the smell of food trucks', 'Color is the first thing you notice here'),
        'coconut-grove': ('Tropical trees shade winding streets near the water', 'The breeze is warm, salty, and easy to follow', 'Rain moves through the canopy in a sudden burst', 'The neighborhood feels relaxed but not sleepy', 'Boats and traffic share the same humid horizon', 'Greenery crowds the sidewalks and walls', 'Sunlight breaks through leaves after the shower', 'The district keeps a coastal, unhurried rhythm', 'Warm air carries food and sea smells together', 'The water is never far from view'),
        'edgewater': ('The bay opens beyond towers and busy waterfront roads', 'A warm breeze moves between the buildings', 'Storm clouds gather over the water', 'The district is bright, modern, and close to the shore', 'Rain makes the waterfront lights shimmer', 'Traffic hums beneath balconies facing the bay', 'Sunlight flashes across glass and waves', 'The heat lingers after the afternoon shower', 'The skyline and water share the same horizon', 'The district feels open even among the towers'),
    }.items()
}


def _load_editable_descriptions():
    path = Path(__file__).with_name('dopewars_descriptions.txt')
    cities, districts, section, rows = {}, {}, None, []

    def finish():
        if section is None:
            return
        if len(rows) != 10 or [n for n, _ in rows] != list(range(1, 11)):
            raise RuntimeError(f'{path.name}: {section} must contain entries 1 through 10')
        target = cities if section.startswith('CITY:') else districts
        target[section.split(':', 1)[1]] = tuple(text for _, text in rows)

    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('[') and line.endswith(']'):
            finish()
            section, rows = line[1:-1], []
            if not (section.startswith('CITY:') or section.startswith('DISTRICT:')):
                raise RuntimeError(f'{path.name}: unknown section {section}')
            continue
        if section is None or '|' not in line:
            raise RuntimeError(f'{path.name}: entry outside a section')
        number, text = line.split('|', 1)
        rows.append((int(number), text.strip()))
    finish()
    if set(cities) != set(CITIES) or set(districts) != set(PLACES):
        raise RuntimeError(f'{path.name}: catalog does not match the game map')
    return cities, districts


CITY_DESCRIPTIONS, DISTRICT_DESCRIPTIONS = _load_editable_descriptions()
# Display only: commands and saved item identifiers remain plain text.
GOOD_ICONS = {'ludes': '💤', 'weed': '🌿', 'speed': '⚡',
              'peyote': '🌵', 'hash': '🟫', 'mushrooms': '🍄',
              'mda': '💜', 'opium': '🌺', 'acid': '🌀',
              'ketamine': '🐴', 'meth': '🔥', 'oxy': '💊',
              'pcp': '🧪', 'heroin': '💉', 'crystal': '💎',
              'cocaine': '❄️'}
HELP = ('TRADE\nB ITEM QTY - buy\nS ITEM QTY - sell\nM - market\nI - inventory\n'
        'Items: ludes, weed, speed, peyote, hash,\nmushrooms, mda, opium, acid, ketamine,\nmeth, oxy, pcp, heroin, crystal, cocaine\n'
        'Example: B weed 2\n\n'
        'TRAVEL\nT DISTRICT - within your current city only\n'
        + ''.join(CITY_LABELS[c] + ': ' + ', '.join(CITY_PLACES[c]) + '\n'
                  for c in CITIES) +
        'Each trip: +1 day, +5% debt\n\n'
        'AIRPORT\n'
        'From Queens, Kenner or Flagami:\n'
        'flight CITY yes - confirm cash fare\n'
        'Cities: new-york, new-orleans, miami\n'
        'Flights cost cash, take one day and use the normal travel rules.\n\n'
        'MONEY & GEAR\nloan borrow AMOUNT\nloan repay AMOUNT\n'
        'bank deposit AMOUNT\nbank withdraw AMOUNT\n'
        'The bank is in Brooklyn; what is in it is safe\n'
        'from a police stop.\nE - equipment menu\n\n'
        'ENCOUNTERS\nfight - attack\nrun - try to escape\n'
        'surrender - lose goods + 25% cash\n\n'
        'YOUR RUN\nsave - autosave status\nX - save & exit\n'
        'finish - sell all & end\nbankrupt - end with zero\n'
        'new 30 / new 365 - before play or after ending')


def good_label(item):
    return f"{GOOD_ICONS[item]} {item}"


def status(s):
    due = f" (due D{s['loan_due']})" if s['debt'] else ''
    saved = f" | Bank ${s['bank']}" if s['bank'] else ''
    return (f"DopeWars D{s['day']}/{s['days']} | {PLACE_LABELS[s['place']]}\n"
            f"Cash ${s['cash']} | Debt ${s['debt']}{due}{saved}\n"
            f"HP {s['hp']} | Bag {sum(s['inventory'].values())}/{s['capacity']}")


def inventory_view(s):
    goods = '\n'.join(f"{good_label(k)} x{q}"
                      for k, q in s['inventory'].items() if q) or 'empty'
    return (f"{status(s)}\n\nBAG\n{goods}\n\n"
            f"GEAR\nWeapon: {'yes' if s['weapon'] else 'no'}\n"
            f"Vest: {'yes' if s['armor'] else 'no'}\n\nM - market\nH - help\nX - save & exit")


def draw(s, low, high):
    data = sha256(f"dopewars:{s['seed']}:{s['draw']}".encode()).digest()
    s['draw'] += 1
    return low + int.from_bytes(data[:8], 'big') % (high - low + 1)


def city(place):
    return PLACE_CITY[place]


def airport_city(place):
    current = city(place)
    return current if AIRPORTS.get(current) == place else None


def flight_fare(s, destination):
    """Return a stable daily fare without consuming the gameplay draw stream."""
    origin = airport_city(s['place'])
    if not origin or destination not in AIRPORTS or destination == origin:
        raise ValueError('Flights leave only from an airport to another city.')
    low, high = FLIGHT_FARE_BANDS[frozenset((origin, destination))]
    key = f"flight:{s['seed']}:{s['day']}:{min(origin, destination)}:{max(origin, destination)}"
    value = int.from_bytes(sha256(key.encode()).digest()[:8], 'big')
    return low + value % (high - low + 1)


def market(s):
    """Stock five to nine goods and roll an occasional price event."""
    s['market'] = {
        item: {'price': max(1, base * draw(s, 60, 170) // 100), 'stock': 0}
        for item, base in GOODS.items()
    }
    choices = list(GOODS)
    stocked = []
    # Clamped, not merely drawn: draw() is stubbed in tests and a count
    # past the catalogue pops an empty list.
    count = min(9, max(5, draw(s, 5, 9)))
    for _ in range(count):
        item = choices.pop(draw(s, 0, len(choices) - 1) % len(choices))
        stocked.append(item)
        s['market'][item]['stock'] = draw(s, BASE_STOCK_MIN, BASE_STOCK_MAX)

    s['event'] = ''
    if draw(s, 1, 100) <= 20:
        item = stocked[draw(s, 0, len(stocked) - 1) % len(stocked)]
        if draw(s, 1, 2) == 1:
            s['market'][item]['price'] = max(
                1, GOODS[item] * draw(s, 30, 55) // 100)
            s['market'][item]['stock'] = max(
                s['market'][item]['stock'], draw(s, 25, 40))
            s['event'] = f'deal:{item}'
        else:
            if item == 'cocaine':
                # Cocaine is the premium risk/reward trade: it costs the most
                # to carry, but a cocaine bust creates the biggest payout.
                s['market'][item]['price'] = GOODS[item] * draw(
                    s, COCAINE_BUST_PRICE_MIN, COCAINE_BUST_PRICE_MAX) // 100
                scarce_stock = draw(s, 1, COCAINE_BUST_STOCK_MAX)
            else:
                s['market'][item]['price'] = GOODS[item] * draw(s, 220, 350) // 100
                scarce_stock = draw(s, 1, 5)
            s['market'][item]['stock'] = min(s['market'][item]['stock'], scarce_stock)
            s['event'] = f'bust:{item}'


def new_game(seed, days=DAYS):
    if days not in (30, 365):
        raise ValueError('Choose 30 or 365 days.')
    s = dict(version=VERSION, seed=seed, draw=0, day=1, days=days, loan_due=30, place=PLACES[0],
             cash=2400, debt=1200, bank=0, hp=100, capacity=40, weapon=0, armor=0,
             inventory={item: 0 for item in GOODS}, market={}, phase='market',
             enemy_hp=0, moves=0, outcome='', event='')
    market(s)
    return s


def score(s):
    # Banked money is yours; it is only out of reach of a police stop.
    return max(0, s['cash'] + s['bank'] - s['debt'])


def view(s):
    header = status(s)
    if s['phase'] == 'ended':
        return (f"{header}\n\n{s['outcome']} | Score {score(s)}\n\n"
                "NEW 30 - short run\nNEW 365 - long run\nX - exit")
    if s['phase'] == 'police':
        return (f"{header}\n\n🚨 POLICE | HP {s['enemy_hp']}\n"
                "FIGHT - attack\nRUN - try to escape\n"
                "SURRENDER - lose goods + 25% cash\n\nX - save & exit")
    # Only what can be traded here: with sixteen goods, a full listing
    # is mostly "stock 0" and pushes this reply past the size the door
    # holds itself to. The menu's Market screen filters the same way.
    listing = '\n'.join(
        f"{good_label(k)} ${v['price']} | stock {v['stock']}"
        for k, v in s['market'].items()
        if v['stock'] or s['inventory'][k])
    options = '\nNEW 30 / NEW 365 - game length' if s['moves'] == 0 else ''
    event = ''
    if s['event']:
        kind, item = s['event'].split(':', 1)
        event = (f"\n{'📦 Deal' if kind == 'deal' else '🚨 Bust'}: "
                 f"{good_label(item)} {'is cheap' if kind == 'deal' else 'is scarce'}")
    return (f"{header}\n\nMARKET{event}\n{listing}\n\n"
            "B item qty - buy\nS item qty - sell\nT place - travel\n"
            "I - bag\nE - gear\nH - all commands & places\nX - save & exit" + options)


def _finish(s, outcome):
    s['phase'], s['outcome'], s['enemy_hp'] = 'ended', outcome, 0


def _settle(s):
    proceeds = sum(q * s['market'][k]['price'] for k, q in s['inventory'].items())
    s['cash'] = min(MAX_CASH, s['cash'] + proceeds)
    s['inventory'] = {k: 0 for k in GOODS}
    _finish(s, 'Completed' if s['cash'] + s['bank'] >= s['debt']
            else 'Bankrupt')


def _arrival(s):
    s['enemy_hp'] = 0
    s['phase'] = 'market'
    if s['day'] == s['days']:
        _settle(s)


def _loot(s):
    """Occasionally award modest loot after an uneventful trip."""
    if draw(s, 1, 100) > 15:
        return ''
    if draw(s, 1, 2) == 1:
        amount = draw(s, 50, 250)
        s['cash'] = min(MAX_CASH, s['cash'] + amount)
        return f'cash:{amount}'
    room = s['capacity'] - sum(s['inventory'].values())
    if room <= 0:
        return 'full'
    item = tuple(GOODS)[draw(s, 0, len(GOODS) - 1)]
    qty = min(room, draw(s, 1, 3))
    s['inventory'][item] += qty
    return f'goods:{item}:{qty}'


def _arrival_description(s, destination, from_airport):
    """Return a fresh arrival line without consuming gameplay RNG."""
    return arrival_descriptions(s['seed'], s['moves'], s['day'],
                                destination, from_airport)


def arrival_descriptions(seed, moves, day, destination, from_airport=False):
    """Choose the city/district copy for one arrival without changing state."""
    key = f'arrival:{seed}:{moves}:{day}:{destination}'
    value = int.from_bytes(sha256(key.encode()).digest()[:8], 'big')
    district = _short_arrival(DISTRICT_DESCRIPTIONS[destination][value % 10])
    city_line = ''
    current_city = city(destination)
    if from_airport:
        city_line = _short_arrival(CITY_DESCRIPTIONS[current_city][(value // 10) % 10])
    return city_line, district if not from_airport else ''


def _short_arrival(text, limit=52):
    """Keep arrival copy short enough to share a packet with an encounter."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(' ', 1)[0] + '…'


def _travel(s, destination, fare=0, from_airport=False):
    """Apply one ground trip or flight, returning its travel side effects."""
    if s['debt'] and s['day'] >= s['loan_due']:
        s['cash'] = s['bank'] = 0
        s['inventory'] = {k: 0 for k in GOODS}
        _finish(s, 'Bankrupt')
        return {'deadline': True, 'interest': 0, 'police': False, 'loot': ''}
    if fare:
        s['cash'] -= fare
    s['place'] = destination
    s['day'] += 1
    city_description, district_description = _arrival_description(
        s, destination, from_airport)
    interest = (s['debt'] * 5 + 99) // 100 if s['debt'] else 0
    s['debt'] += interest
    market(s)
    if draw(s, 1, 100) <= 25:
        s['phase'], s['enemy_hp'] = 'police', 45
        return {'deadline': False, 'interest': interest, 'police': True, 'loot': '',
                'city_description': city_description,
                'district_description': district_description}
    _arrival(s)
    loot = _loot(s) if s['phase'] != 'ended' else ''
    return {'deadline': False, 'interest': interest, 'police': False, 'loot': loot,
            'city_description': city_description,
            'district_description': district_description}


def _travel_reply(result, prefix='Arrived'):
    if result['deadline']:
        return 'Loan deadline missed.'
    reply = (f'{prefix}. Debt increased 5% (+${result["interest"]}).'
             if result['interest'] else f'{prefix}. Debt-free: no interest charged.')
    if result['police']:
        reply += ' Police stop!'
    elif result['loot']:
        reply += ' Loot ' + result['loot'] + '.'
    if result.get('city_description'):
        reply += ' ' + result['city_description']
    if result.get('district_description'):
        reply += ' ' + result['district_description']
    return reply


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
        elif verb == 'border' and len(args) == 1 and args[0] in ('legal', 'fence'):
            destination = BORDER_PLACES.get(s['place'])
            if not destination:
                raise ValueError('The border terminal is in San Ysidro.')
            # Both routes take one game day.  A successful crossing is 65%;
            # the next 15% is an incident, and the rest turns the player back.
            outcome = draw(s, 1, 100)
            s['day'] += 1
            interest = (s['debt'] * 5 + 99) // 100 if s['debt'] else 0
            s['debt'] += interest
            market(s)
            if outcome <= 65:
                s['place'] = destination
                _arrival(s)
                reply = f"Crossed {'legally' if args[0] == 'legal' else 'the fence'} into {CITY_LABELS[city(s['place'])]}."
            elif outcome <= 80:
                s['phase'], s['enemy_hp'] = 'police', 45
                reply = 'Border incident: police encounter.'
            else:
                reply = 'Turned back at the border.'
        elif verb == 'travel' and len(args) == 1:
            if args[0] not in PLACES or args[0] == s['place']:
                raise ValueError('Choose a different district in this city.')
            if city(args[0]) != city(s['place']):
                raise ValueError('Travel between cities is by airport.')
            result = _travel(s, args[0])
            reply = _travel_reply(result)
            if result['deadline']:
                s['moves'] += 1
                return s, reply + '\n' + view(s), False
        elif verb == 'flight' and len(args) == 2 and args[1] in ('yes', 'confirm'):
            destination = args[0]
            origin = airport_city(s['place'])
            if not origin or destination not in CITIES or destination == origin:
                raise ValueError('Choose another city from this airport.')
            fare = flight_fare(s, destination)
            if s['cash'] < fare:
                raise ValueError(f'You need ${fare} cash for that flight.')
            result = _travel(s, AIRPORTS[destination], fare, from_airport=True)
            reply = _travel_reply(result, f'Boarded for {CITY_LABELS[destination]} for ${fare}')
            if result['deadline']:
                s['moves'] += 1
                return s, reply + '\n' + view(s), False
        elif verb == 'bank' and len(args) == 2 and args[0] in ('deposit', 'withdraw'):
            if s['place'] != BANK_PLACE:
                raise ValueError('The bank is in '
                                 + PLACE_LABELS[BANK_PLACE] + '.')
            amount = _amount(args[1])
            if args[0] == 'deposit':
                if amount > s['cash'] or s['bank'] + amount > MAX_CASH:
                    raise ValueError('More than you are carrying, or over '
                                     'the bank limit.')
                s['cash'] -= amount
                s['bank'] += amount
            else:
                if amount > s['bank'] or s['cash'] + amount > MAX_CASH:
                    raise ValueError('More than the bank holds, or over '
                                     'the cash limit.')
                s['bank'] -= amount
                s['cash'] += amount
            reply = (f"Deposited ${amount}." if args[0] == 'deposit'
                     else f"Withdrew ${amount}.")
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
            s['cash'] = s['bank'] = 0
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
              'cash': (0, MAX_CASH), 'debt': (0, MAX_CASH),
              'bank': (0, MAX_CASH), 'hp': (0, 100),
              'capacity': (40, 70), 'weapon': (0, 1), 'armor': (0, 1),
              'enemy_hp': (0, 45), 'moves': (0, 10**12)}
    for key, (low, high) in limits.items():
        if type(s[key]) is not int or not low <= s[key] <= high:
            raise ValueError('Invalid statistic')
    if s['days'] not in (30, 365) or s['day'] > s['days'] or bool(s['debt']) != bool(s['loan_due']):
        raise ValueError('Invalid duration/deadline')
    if s['phase'] not in ('market', 'police', 'ended') or s['place'] not in PLACES:
        raise ValueError('Invalid location/phase')
    if (not isinstance(s['event'], str) or len(s['event']) > 32
            or (s['event'] and not any(
                s['event'] == f'{kind}:{item}'
                for kind in ('deal', 'bust') for item in GOODS))):
        raise ValueError('Invalid market event')
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


_V1_GOODS = ('weed', 'hash', 'acid', 'cocaine')
_V1_PLACES = {'docks': 'brooklyn', 'uptown': 'manhattan',
              'suburbs': 'queens', 'station': 'bronx'}
_V1_KEYS = {'version', 'seed', 'draw', 'day', 'days', 'loan_due', 'place',
            'cash', 'debt', 'hp', 'capacity', 'weapon', 'armor', 'inventory',
            'market', 'phase', 'enemy_hp', 'moves', 'outcome'}


def migrate(state):
    """Walk an older save up to the current schema, one version at a
    time, without rerolling its future RNG."""
    if not isinstance(state, dict):
        return state
    if state.get('version') == 1:
        state = _migrate_v1(state)
    if state.get('version') == 2:
        state = _widen_catalogue(state, 3)
    if state.get('version') == 3:
        state = _widen_catalogue(state, 4)
    if state.get('version') == 4:
        state = _open_an_account(state)
    return state


def _open_an_account(state):
    """v4 -> v5: an empty bank account. Nothing else moves."""
    s = deepcopy(state)
    s['version'] = 5
    s.setdefault('bank', 0)
    return s


def _widen_catalogue(state, version):
    """A schema hop that only adds goods: v2 -> v3 -> v4.

    The new ones start absent from the bag and unstocked here, so an
    in-flight run keeps its cash, debt and holdings exactly and meets
    them on its next market -- the next time the player travels.

    Both hops do the same thing, so they share the code rather than
    each getting a near-copy that could drift."""
    s = deepcopy(state)
    s['version'] = version
    for item, base in GOODS.items():
        s['inventory'].setdefault(item, 0)
        s['market'].setdefault(item, {'price': base, 'stock': 0})
    return s


def _migrate_v1(state):
    """v1 -> v2: the four-good catalogue and the renamed places."""
    if set(state) != _V1_KEYS:
        raise ValueError('Unknown save schema')
    s = deepcopy(state)
    if not isinstance(s.get('inventory'), dict) or set(s['inventory']) != set(_V1_GOODS):
        raise ValueError('Invalid inventory')
    if not isinstance(s.get('market'), dict) or set(s['market']) != set(_V1_GOODS):
        raise ValueError('Invalid market')
    s['version'] = 2
    s['place'] = _V1_PLACES.get(s['place'], s['place'])
    s['event'] = ''
    for item in GOODS:
        s['inventory'].setdefault(item, 0)
        s['market'].setdefault(item, {'price': GOODS[item], 'stock': 0})
    return s
