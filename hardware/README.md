# Hardware — Pico cache node

Physical design for the Phase 4 low-power cache node (see `../pico_node/`).

## Decisions
- **Goal:** a **solar-powered, mostly-sleeping** cache node. Low power is the
  top priority.
- **Controller:** **Seeed XIAO nRF52840** (~5 µA sleep) running our CircuitPython
  code — *not* a Pico (RP2040 sleeps at ~1 mA, ~200× worse — too thirsty for
  solar). Bench bring-up can use a Feather nRF52840 (or a Pico, power aside).
- **Radio:** **RAK4631 + RAK19007** (low idle, USB-C + LiPo charging). Bench on
  the **Heltec V3** already on hand.
- **Power:** solar via a **CN3065** charger → LiPo; radio + SD switched off
  during sleep by GPIO load switches. See `power-budget.md`.
- **Connecting board:** hand-wired **protoboard first**, then a KiCad PCB.
- **Case:** parametric **OpenSCAD** (`case.scad`).

## Files
| File | What |
|---|---|
| `wiring.md` | Pin-by-pin UART + SPI(SD) + power-gating + solar wiring (XIAO nRF52840 + RAK, plus Heltec bench), BOM, and bring-up order. |
| `power-budget.md` | Component currents, duty-cycle math, the wake-interval ↔ autonomy table, and solar panel/battery sizing. |
| `case.scad` | Parametric enclosure (tray + lid) holding the RAK19007 + controller side-by-side with a battery bay, USB-C and antenna cutouts. |

## Using the case
1. Install [OpenSCAD](https://openscad.org/) (free).
2. Open `case.scad`. The variables at the top drive everything; values marked
   **VERIFY** should be confirmed with calipers/datasheet against your boards.
3. Set `SHOW = "tray"`, press F6, export STL; repeat with `SHOW = "lid"`.
4. Print, test-fit, tweak the variables, repeat. Because it's code, a dimension
   change is a one-line edit — no remodeling.

## Status
Design + BOM drafted. Next physical steps are gated on parts (RAK) and on the
**gateway subscriber-mode** software change (lets the node actually sync). The
whole device-side stack is already host-tested in `../pico_node/`.
