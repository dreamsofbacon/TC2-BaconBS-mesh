# DopeWars

A native, single-player BaconBBS door. No GTK, desktop interface, external game
process, downloaded assets, or new runtime dependencies.

## Launch and commands

Open **Games** in the BBS and select **DopeWars** by its displayed number.
The same menu is used over radio, SSH and the web emulator. Selecting the game
starts a default 30-day run or resumes the player's existing run. Before the
first action, send `new 365` for a 365-day game or `new 30` for the short game.
Choosing a length does not reroll the opening market. Once play begins, the
duration cannot be changed until the run ends. After a BBS restart, open
Games again; the in-memory menu session can reset but the game save remains.

Commands are case-insensitive. Amounts must be positive whole numbers.

| Command | Effect |
| --- | --- |
| `market` / `m` | Prices and available quantities at this location |
| `inventory` / `i` | Cash, debt, health, goods and equipment |
| `buy weed 2` / `b weed 2` | Buy two units, subject to stock, cash and bag space |
| `sell weed 2` / `s weed 2` | Sell two owned units at the current price |
| `travel uptown` / `t uptown` | Advance one day, accrue interest, generate a market and possible police encounter |
| `loan borrow 100` | Borrow, up to $10,000 outstanding debt |
| `loan repay 100` | Repay debt using available cash |
| `equipment` / `e` | Show equipment and prices |
| `equipment bag` | $900, increase bag capacity from 40 to 70 once |
| `equipment vest` | $1,200, reduce damage taken by 8 once |
| `equipment weapon` | $1,600, add 15 damage once |
| `equipment medkit` | $250, restore up to 30 health outside encounters |
| `fight` | Attack the police; surviving opponents retaliate |
| `run` | 60% escape chance; a failed attempt causes damage |
| `surrender` | Lose goods and 25% of cash; leave the encounter |
| `finish` | End early and liquidate goods at current market prices |
| `bankrupt` | End the run immediately with zero score |
| `help` / `h` / `?` | Command summary |
| `save` | Confirm automatic saving without leaving |
| `quit` / `q` / `x` / `!x` / `save/quit` | Save and return to Games, not end the run |
| `new 30` / `new 365` | Choose duration before the first action, or start again after ending |
| `new` | Start again with the same duration after ending |

Goods: `weed`, `hash`, `acid`, `cocaine`.
Locations: `docks`, `uptown`, `suburbs`, `station`.

## Rules

Start on day 1 with $2,400, $1,200 debt, 100 health and a 40-unit bag.
Buy low, travel and sell high. Only travel advances the calendar and regenerates
the market. Staying put, viewing screens, reconnecting and invalid commands
never reroll prices. Each trip adds 5% interest, rounded up, and has a 25% chance
of a police encounter. No trading, borrowing or equipment purchases during
an encounter; save/quit and information commands remain available.

Combat deals 10–22 damage plus the weapon bonus. Police deal 12–26 damage minus
the vest reduction (at least 1). Defeating police causes no reward or cash gain.
Zero health ends the run with zero cash and score. Resolve any day-30 police
encounter before automatic final liquidation in the 30-day mode. The 365-day mode
liquidates on day 365 instead. You can finish early outside
combat. A run is bankrupt if final cash cannot cover the debt.

### Loan deadline in the 365-day option

The starting loan is due **by the end of day 30**. You may trade and repay on
day 30, including after resolving a police encounter. Trying to travel to day
31 with any debt remaining ends the run as bankrupt, with zero score. Cash in
your pocket is not automatically applied: use `loan repay AMOUNT`. The deadline
is based on game days, never wall-clock time, so disconnecting does not consume
your repayment window.

Interest is still 5% per trip. Additional borrowing or partial repayment does
not extend the existing deadline. After fully repaying, a new loan is due at
the end of `current day + 29` (30 game days including the borrowing day).
These rules also apply to new borrowing in the short game; final liquidation
settles the run even if a fresh loan's deadline would fall after the last day.

Final score is `max(0, cash - debt)` after liquidation; equipment has no resale
value. Cash cannot exceed $1,000,000,000. Trades/borrowing that exceed this limit
are rejected; final liquidation caps cash at the limit. There is no real money.

## Saving, identity and scores

`dopewars_door.py` reuses Baconfall's SQLite door transaction pattern. Each
accepted action is committed before its response is sent. State, seed, random
draw counter, inventory, market and pending encounter are stored together in
`dopewars_runs` in the existing BBS database. `BEGIN IMMEDIATE` serializes writers;
score submission and the terminal save share a transaction. Failed writes roll
back, and malformed/future-version saves are preserved for operator inspection.

Runs are **node-local**, keyed by the existing canonical player identity
(including MeshCore's `mc-` key). Different identities have different games;
this does not introduce account/device linking. Back up the BBS database to
back up runs. No DopeWars saves are broadcast or included in Z-machine save sync.

A completed run submits once to the existing `game_scores` system as `dopewars`.
Both durations currently share that scoreboard (longer runs have more earning
opportunities). The existing high-score promotion and score synchronization apply; no new
multiplayer state or sync protocol is introduced. A lost response can be
recovered by viewing the saved state. As with other doors, this does not add
packet-ID deduplication: deliberately resending a valid action can perform it
again. The persisted SHA-256 seed/counter stream gives repeatable future draws
across interpreter/service restarts; it is for gameplay, not security.

## Source and licensing findings

Reference inspected: [ajhwb/druxlord](https://github.com/ajhwb/druxlord), commit
`fd6f788425b01d2e10e2edcfb1a533b3b5be8d4e`.

- Its `readme` explicitly says **GPL v2** and identifies **Ardhan Madras
  <ajhwb@knac.com>** as author. Its `copying` contains GPL version 2.
- No project-specific "or any later version" grant was found. The example
  wording inside the GPL license appendix is not such a grant. BaconBBS ships
  GPLv3, so direct incorporation of GPLv2-only code would present a compatibility
  problem. No Druxlord C code has been translated or incorporated here.
- Druxlord calls itself a clone of **Drug Lord 2** by Geek Hideout. Its city,
  drug, weapon, rank and interface content has no separate provenance or
  third-party permission record in that checkout. Permission for inherited
  Drug Lord 2 content could not be established. No artwork files were present.
- The reference separates data/market generation into `druxlord.c/.h`, while
  `window.c` builds GTK screens and `window-cb.c` contains mostly UI callbacks
  and several empty handlers. It is not a complete playable rules engine for
  the requested loans, combat, persistence and endings.

**Implementation choice:** DopeWars contains newly authored Python rules,
original messages, new price/balance data, a different small location set and
no imported assets. Only general trading-game concepts and ordinary drug names
are used. It is not a line-for-line reproduction of Druxlord, nor affiliated
with Drug Lord 2 or other projects named DopeWars. The reference credit above
is preserved for transparency; it does not claim permission for unclear content.
The new code follows this repository's [GPLv3 license](../LICENSE).
No Druxlord/Drug Lord 2 text, art, distinctive item set or balance tables are
redistributed. Future requests to reuse those materials require a new review.
Name/trademark clearance has not been established for public distribution.

## Development and tests

## Local mesh presentation

Market goods, inventory, equipment and actions are shown on individual lines,
with blank lines separating status, market and command sections. Drug display
labels are 🌿 weed, 🟫 hash, 🌀 acid and ❄️ cocaine. The DEA Emoji Decoded
reference informed the choices; hash and acid use distinct fallback symbols
because they are not covered. Names stay visible and command/save identifiers
remain unchanged. No game rules or save schema changed.

The existing UTF-8-aware transport splitter handles radio packet limits. At a
200-byte budget, the seed-19 opening market is two packets, inventory and
equipment one each, and help three. Actual packet counts vary with state and
transport limits. This presentation applies to the local installation only.

Pure rules and rendering: `dopewars.py`; database adapter: `dopewars_door.py`.
The game registry, launch handler and both dispatch guards follow existing
BaconBBS door patterns. All text goes through the existing `send_message`
rendering/transport path. Existing menu numbers are preserved; DopeWars is appended.

In an isolated checkout, install existing test dependencies and run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q tests/test_dopewars.py
.venv/bin/python -m pytest -q tests/test_dopewars.py tests/test_baconfall.py tests/test_trivia_king.py tests/test_menu_navigation.py tests/test_score_account_names.py tests/test_meshcore_player_identity.py
```

Tests sandbox runtime paths through `tests/conftest.py`; do not point development
commands at a live BBS database. Coverage includes initialization, trading and
limits, interest, equipment, travel, police/combat, end conditions, save/resume,
restart randomness, identity separation, corrupt saves, atomic score submission,
write rollback, dispatch collisions, both durations, deadline repayment and
default, borrowing without deadline extension, and complete 365-day runs. Hardware/radio delivery and gameplay
balance still need operator playtesting. No live server changes are needed to
run these tests.

## Initial implementation review record

- Fork remote: `https://github.com/materva/TC2-BaconBS-mesh.git` (`origin`).
- Upstream: `https://github.com/dreamsofbacon/TC2-BaconBS-mesh.git` (`upstream`).
- Both remotes were fetched. Local/fork main at `6bb9845` was 49 commits behind
  upstream with zero main-only commits. Local `main` advanced to the exact
  upstream commit `3f4f2769df0dd91480c7a926bb7311fa57defb0f`.
- Branch: `feat/dopewars`, based on that synchronized commit in a separate
  worktree. Existing feature branches and dirty older checkouts were preserved.
- Remote fork main remains unchanged: pushing is intentionally deferred until
  review. No merge, deployment, service restart or live-server edit was performed.
- Python 3.12: focused and neighboring regression command above passed **241
  tests and 73 subtests**. Compilation of changed Python modules and
  `git diff --check` also passed. The full repository suite was not run.
