# Pico cache node — wiring & bill of materials

**Hardware decision:** build target is **RAK4631 + RAK19007 WisBlock base**;
bench bring-up is on the **Heltec V3** you already own. Connecting board is
**hand-wired protoboard first**, KiCad PCB later. Case is `case.scad` (OpenSCAD).

Three things connect to the Pico in both setups:
1. the Meshtastic **radio** over a 3-wire UART,
2. a **microSD** card over SPI (the local cache), and
3. **power**.

> Pins marked **VERIFY** depend on the exact board revision / your Meshtastic
> Serial-module config — confirm against the board pinout before soldering.

---

## 1) Bench setup — Heltec V3 + Pico + microSD

### UART: Pico ⇄ Heltec (crossed)
| Pico (CircuitPython) | Heltec V3 | Notes |
|---|---|---|
| `GP0` (UART0 TX) | Serial RXD GPIO **(VERIFY)** | pick a free Heltec GPIO, e.g. 19 or 45–48 |
| `GP1` (UART0 RX) | Serial TXD GPIO **(VERIFY)** | crossed: Heltec TX → Pico RX |
| `GND` | `GND` | common ground is mandatory |

On the Heltec, in Meshtastic set **Serial module**: `enabled=true`,
`mode=PROTO`, `baud=115200`, `rxd`/`txd` = the two GPIOs you wired.

### microSD over SPI (Pico SPI0)
| Pico | SD breakout | Notes |
|---|---|---|
| `GP2` | SCK / CLK | |
| `GP3` | MOSI / DI | |
| `GP4` | MISO / DO | |
| `GP5` | CS | any free GPIO; set in `config.py` later |
| `3V3(OUT)` | VCC (3.3 V) | use a 3.3 V SD breakout, not a 5 V one |
| `GND` | GND | |

### Power (bench)
Each board on its own USB cable. Easiest while developing; no battery yet.

---

## 2) Build target — RAK4631 / RAK19007 + Pico + microSD

The RAK19007 base board gives you **USB-C + LiPo charging + a 3.3 V rail** for
free, so the Pico and SD run off the RAK's battery and you don't need a separate
regulator or (thanks to the nRF52's µA idle) a radio power-gate.

### UART: Pico ⇄ RAK19007 IO header (crossed)
The nRF52 `Serial1` TX/RX appear on the WisBlock **IO** header. **VERIFY** the
exact pins against the RAK19007 pinout, then:
| Pico | RAK19007 IO header | Notes |
|---|---|---|
| `GP0` (TX) | UART **RX** pin (VERIFY) | crossed |
| `GP1` (RX) | UART **TX** pin (VERIFY) | crossed |
| `VSYS` | `3V3` out (VERIFY pin) | power the Pico from the RAK rail |
| `GND` | `GND` | |

In Meshtastic on the RAK, set the **Serial module** the same way
(`PROTO`, `115200`) on the IO-header UART pins.

### microSD
Same Pico SPI wiring as the bench table above (GP2–GP5 + 3V3 + GND).

### Power (build)
LiPo → RAK19007 JST **PHR-2 (2 mm)** battery connector; charge over the
RAK's USB-C. The Pico draws from the RAK 3.3 V rail. Keep the radio always-on
(no MOSFET) and only deep-sleep the Pico — simplest and lowest-friction.

---

## Bill of materials

### Build (RAK target)
| Qty | Part | ~Price | Notes |
|---|---|---|---|
| 1 | RAK4631 WisBlock Core (nRF52840 + SX1262, your region's MHz) | $15–20 | Meshtastic-supported |
| 1 | RAK19007 WisBlock Base Board (2nd gen, USB-C) | $9.99 | power/charge/USB |
| 1 | Raspberry Pi Pico (or Pico 2 — more RAM) | $4–6 | not Pico W |
| 1 | microSD SPI breakout (3.3 V) + microSD card (≥1 GB) | $5–8 | the cache store |
| 1 | LiPo battery, JST-PH 2.0, e.g. 1000–2000 mAh | $8–12 | check polarity! |
| 1 | Antenna + pigtail (IPEX→SMA) for your band | $4–8 | match 868/915 MHz |
| — | Protoboard, headers, wire, M2/M2.5 screws + standoffs | $5 | |

### Bench (use what you have)
| Qty | Part | Notes |
|---|---|---|
| 1 | Heltec V3 (already owned) | bench radio |
| 1 | Pico + microSD breakout + card | same as build |
| — | Jumper wires | |

---

## Bring-up order (no custom board needed yet)
1. Flash CircuitPython to the Pico; copy `pico_node/` files; wire SD only →
   confirm it mounts and the cache reads/writes.
2. Add the UART to the Heltec; set the Serial module to PROTO → confirm
   `learn_node_num()` returns the Pico's node number (proves framing works).
3. Add the Pico's node ID to the gateway and bring up **gateway subscriber
   mode** (the pending live change) → watch a real sync populate the SD cache.
4. Once the wiring is proven on protoboard, spin the KiCad PCB and print the
   `case.scad` enclosure (verify the VERIFY dimensions first).
