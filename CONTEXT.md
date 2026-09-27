# DopeWars Domain

DopeWars is a single-player trading game whose map is divided into city
districts. Movement changes the market and advances the game day.

## Map and travel

**City**:
A group of eight districts sharing a local market map. The current game has
New York, New Orleans, and Miami.

**District**:
A named trading location within a city. Ground travel moves between districts
in the current city and consumes one day.

**Airport district**:
The one district in a city that exposes the Airport menu. It is Queens in New
York, Kenner in New Orleans, and Flagami in Miami.

**Flight**:
A bidirectional movement between two cities' airport districts. A flight costs
cash, consumes one day, and uses the same market and encounter processing as
ground travel.

**Daily fare**:
The fare for a city pair on a particular game day. It is deterministic for the
game seed, day, and route, so save/reload and repeated views agree; it is only
needed when the player is at an airport district.

## Economy

**Fare band**:
The inclusive low/high range used to generate a daily fare for a city pair.
The New Orleans–Miami route is cheapest, while the two New York routes use
higher bands reflecting their longer real-world distances.

**Boarding confirmation**:
The explicit yes/no step after a player selects a destination and sees its fare.
Only confirmation with enough cash commits the flight.
