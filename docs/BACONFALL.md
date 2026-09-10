# Baconfall: The Last Sizzle

An original bacon fantasy expedition for Bacon BBS. The Ash King has stolen the
First Flame, and breakfast across the realm is turning to ash. Your job: cross
three realms, claim three bacon relics, and storm the Ash Throne.

Open **Games → Baconfall: The Last Sizzle**. Existing game numbers stay the same;
Baconfall is appended to the menu. It runs entirely inside the BBS: no downloads,
interpreter, API key, or extra dependency. A run usually takes about 60 turns;
there is no timer, so you can play a few moves at a time over radio or SSH.

## Pick a hero

| Hero | Starting HP | Strike | Armor | Specialty |
| --- | ---: | ---: | ---: | --- |
| Iron Rind | 52 | 6 | 2 | Guard counterattacks for 3 when an enemy attacks. |
| Smoke Ranger | 46 | 8 | 1 | Strike gains +2 against a charging enemy. |
| Maple Witch | 50 | 6 | 1 | Sizzle deals 14 and costs 2 heat instead of 3. |

Iron Rind is a good first expedition. Smoke Ranger rewards aggressive timing.
Maple Witch converts heat into piercing damage more efficiently.

## Explore and build your hero

Each of **Hickory Hollows**, **Saltglass Dunes**, and **Black Skillet Citadel** has
three crossings. At each crossing, choose between two routes. Routes vary with
the expedition, and their risks and rewards are displayed before you commit:

- **Hunt a raider:** a normal fight, 9 fat coins and 80 renown.
- **Raid a warband:** a tougher fight, 17 coins and 140 renown.
- **Smokehouse:** choose two rations, ten coins, or healing and heat.
- **Bacon beacon:** share food for healing and renown, sacrifice six HP for
  permanent strike power, or salvage coins.

After the third crossing, choose **one** camp blessing: free healing, a weapon
upgrade, greater maximum HP, or more rations. Then face the region's guardian:
**The Boar of Cinders**, **The Salt Wyrm**, or **The Hollow Butcher**.

Each defeated guardian offers a relic:

- **Hickory Heart:** +12 maximum HP and heal 12.
- **Saltsteel Fang:** +2 strike damage.
- **Maple Ember:** +5 sizzle damage.

Duplicates stack. The rescued cook also gives one ration and heals up to ten HP.
After the third relic, you face **The Ash King**. At half health his crown cracks
and he gains three attack power. Defeat him to restore the First Flame.

## Fight by reading intent

The combat screen tells you what the enemy will do **this turn**. There are no
hidden hit rolls. A killing blow prevents retaliation.

| Command | Effect |
| --- | --- |
| `A` | Strike; gain 1 heat. |
| `G` | Guard; gain 2 heat and block 7 damage in addition to armor. |
| `S` | Sizzle; deal 12 piercing damage for 3 heat (14 for 2 as Maple Witch). |
| `E` | Eat one ration to heal up to 16 HP. The enemy still gets its turn. |

Heat caps at six. An enemy may slash, charge without attacking, brace to reduce
strike damage by five, or unleash a heavy crush. Guard a crush, eat during a
charge or brace, and use sizzle to bypass bracing. Armor reduces incoming damage;
guarding adds to it. Invalid commands and unaffordable actions never spend turns.

## Pause, resume, and scores

- `H` or `?`: field guide.
- `I`: hero statistics and relics.
- `M`: campaign map.
- `L`: repeat the current scene.
- `X` (also `0` or `!X`): save and return to Games.
- `NEW`: start over only after replying `YES`; `NO` keeps the expedition.

Information commands are free. Every turn saves automatically, so disconnects
and BBS restarts preserve the expedition. Reopen Baconfall to resume. **Saves are
local to this BBS node and sender identity**; moving to another BBS node or a
different device starts a separate expedition. SSH resumes through the account's
stable sender number. Synthetic web-emulator identities are for testing, not
portable player accounts.

Death ends the run. Death and victory both record earned renown in the existing
**Scores** and **Hall of Fame**. Every boss is worth 250 renown. Victory adds
1,000 points plus five per remaining HP. Higher scores win; fewer turns break
ties. Stronger routes offer more points, but make survival harder. Restarting
never removes a previous high score. Scores use the BBS's existing mesh sync;
unfinished expeditions do not generate radio save-sync traffic.

## Implementation and verification

`baconfall.py` is a pure state machine with seeded route generation. Its state
contains the seed and random draw count, so restarting cannot reroll a route.
`baconfall_port.py` stores one JSON save per sender in `baconfall_runs`, in the
normal BBS database (including `BBS_DB_PATH`/the Docker volume). Its SQLite write
transaction serializes turns across processes. The ending and scoreboard update
commit together. Viewing an old ending does not republish a deleted score.
Unsupported or unreadable saves are preserved for operator inspection.

The door uses ordinary message delivery and packet splitting for radio, SSH,
and the web emulator. Existing game menu entries and Z-machine save handling
are unchanged. Tests cover full campaigns for all heroes, tactics, costs,
rewards, restart confirmation, save isolation, transactional score failure,
corrupt-save preservation, and dispatch ahead of global shortcuts.

Run the focused suite with:

```sh
python -m pytest tests/test_baconfall.py tests/test_trivia_king.py -q
```
