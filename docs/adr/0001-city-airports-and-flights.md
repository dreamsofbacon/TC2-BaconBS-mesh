# City-scoped airports and flights

**Status: accepted**

DopeWars models New York, New Orleans, and Miami as separate eight-district
maps. Ground travel is restricted to districts within the current city;
cross-city movement is a bidirectional flight available only from that city's
single airport district. Flights use cash-only, distance-relative daily fares,
then reuse the existing one-day travel transition, market generation, and
encounter rules. This boundary keeps airport access meaningful without making
every district a global destination, while deterministic fares preserve the
same price after save/reload without adding persisted fare state.
