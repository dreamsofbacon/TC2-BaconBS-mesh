# Hardware — Pico cache node

Physical design for the Phase 4 low-power cache node (see `../pico_node/`).

## Decisions
- **Radio:** build target **RAK4631 + RAK19007** (nRF52840, far better battery
  life; the base board provides USB-C + LiPo charging). Bench bring-up on the
  **Heltec V3** already on hand.
- **Connecting board:** hand-wired **protoboard first**, then a KiCad PCB once
  the wiring is proven.
- **Case:** parametric **OpenSCAD** (`case.scad`) — variables for board sizes,
  standoffs, and port cutouts; export STL and print.

## Files
| File | What |
|---|---|
| `wiring.md` | Pin-by-pin UART + SPI(SD) + power wiring for both the Heltec bench and the RAK build, plus the bill of materials and bring-up order. |
| `case.scad` | Parametric enclosure (tray + lid) holding the RAK19007 and the Pico side-by-side, with USB-C and antenna cutouts. |

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
