# Wiring & bill of materials — solar cache node

**Hardware decisions:**
- **Controller:** **Seeed XIAO nRF52840** (~5 µA sleep) running our CircuitPython
  cache code — *not* a Pico (the RP2040 sleeps at ~1 mA, too thirsty for solar).
  Bench bring-up can use an Adafruit Feather nRF52840 (easier wiring, plug-in
  LiPo) — same code, different pin names.
- **Radio:** RAK4631 + RAK19007 WisBlock base (low idle, USB-C + LiPo charging).
  Bench bring-up on the **Heltec V3** you already own.
- **Power:** solar via a CN3065 charger → LiPo; the radio and SD are **switched
  off during sleep** by GPIO load switches.
- **Board:** hand-wired protoboard first, KiCad PCB later. Case: `case.scad`.

> Pins marked **VERIFY** depend on board revision / Meshtastic Serial config —
> confirm against the actual pinout before soldering. XIAO CircuitPython pin
> names (`board.TX`, `board.D2`, …) are used below.

---

## Connections (XIAO nRF52840 controller)

### 1) UART: controller ⇄ radio (crossed)
| XIAO nRF52840 | Radio (RAK19007 IO header / Heltec GPIO) | Notes |
|---|---|---|
| `TX` (D6) | UART **RX** (VERIFY) | crossed |
| `RX` (D7) | UART **TX** (VERIFY) | crossed |
| `GND` | `GND` | mandatory common ground |

Set the radio's Meshtastic **Serial module**: `enabled=true`, `mode=PROTO`,
`baud=115200`, `rxd`/`txd` = the pins you wired.

### 2) microSD over SPI (the cache)
| XIAO | SD breakout (3.3 V) | Notes |
|---|---|---|
| `SCK` (D8) | SCK/CLK | |
| `MOSI` (D10) | MOSI/DI | |
| `MISO` (D9) | MISO/DO | |
| `D1` | CS | any free GPIO; set in `config.py` |
| via `SD_EN` switch | VCC (3.3 V) | **powered through a load switch**, not direct |
| `GND` | GND | |

### 3) Power gating (key to the µA sleep)
A GPIO drives a **high-side load switch** (P-MOSFET or a load-switch IC like
TPS22918/AP2281) on each subsystem's supply, so they're fully off during sleep:
| XIAO GPIO | Switches | When high/low |
|---|---|---|
| `D2` → `RADIO_EN` | radio supply rail | on only during the sync window |
| `D3` → `SD_EN` | microSD VCC | on only while syncing/reading |

`code.py` already toggles `RADIO_EN` with a boot delay; add the same pattern for
`SD_EN`. (Cutting radio power means it reboots/rejoins each wake — fine for a
periodic puller. See `power-budget.md`.)

### 4) Power / solar
The **RAK19007 has an onboard solar connector (P1)** plus the LiPo connector
(P2), so the WisBlock base can charge the battery from a panel directly — a
separate **CN3065 is likely unnecessary** (VERIFY the RAK19007's accepted solar
input range against your panel):
```
  6V panel ──► RAK19007 P1 (solar in) ──► onboard charger ──► LiPo on P2
                                                               │
                                                               ├─► XIAO (runs controller)
                                                               └─► GPIO-switched rails ─► radio, SD
```
If you'd rather not route power through the RAK base (or its solar input doesn't
suit your panel), fall back to a **CN3065** charger between the panel and LiPo
(idles <3 µA with no sun). Either way the controller runs from the LiPo and the
radio/SD hang off GPIO load switches.

---

## Bench bring-up (use what you have)
Wire a **Feather nRF52840** (or even a Pico just to prove the protocol — power
doesn't matter on the bench) to the **Heltec V3** over the same crossed UART, plus
the SD over SPI. No solar/load-switches needed on the bench. Confirm
`learn_node_num()` returns the radio's node number, then a real sync into the
cache, before building the solar unit.

---

## Bill of materials

### Solar build
| Qty | Part | ~Price | Notes |
|---|---|---|---|
| 1 | RAK4631 WisBlock Core (your band's MHz) | $15–20 | Meshtastic radio |
| 1 | RAK19007 WisBlock Base (USB-C) | $9.99 | power/charge/USB |
| 1 | **Seeed XIAO nRF52840** | ~$10 | controller, ~5 µA sleep |
| 1 | microSD SPI breakout (3.3 V) + microSD card | $5–8 | the cache store |
| 1 | **Solar panel, 6 V, 1–2 W** | $8–15 | wire to RAK19007 P1 (solar in) |
| (opt) | CN3065 solar LiPo charger | $3–5 | only if not using the RAK's onboard solar input |
| 1 | LiPo, 1000–2000 mAh, JST-PH 2.0 | $8–12 | check polarity! |
| 2 | Load switch (TPS22918 / AP2281) or P-MOSFET + parts | $2–4 | radio + SD gating |
| 1 | Antenna + IPEX→SMA pigtail for your band | $4–8 | |
| — | Protoboard, headers, wire, M2/M2.5 screws + standoffs | $5 | |

### Bench (mostly owned)
| Qty | Part | Notes |
|---|---|---|
| 1 | Heltec V3 | bench radio |
| 1 | Feather nRF52840 (or Pico) + microSD breakout + card | controller + cache |
| — | Jumper wires | |

---

## Bring-up order
1. CircuitPython on the controller; copy `pico_node/` files; wire **SD only** →
   confirm the cache mounts and reads/writes on the SD card.
2. Add the crossed UART to the radio; set Serial module to PROTO →
   `learn_node_num()` returns the radio's node number (framing works).
3. Add the controller's node ID to the gateway's **Subscriber Nodes**
   (Settings → Subscriber Nodes) → watch a real sync fill the SD cache.
4. Add the load switches + CN3065 + panel + LiPo; measure sleep current; tune
   `SLEEP_SECONDS` to your solar budget (see `power-budget.md`).
5. Once proven on protoboard, spin the KiCad PCB and print `case.scad`.
