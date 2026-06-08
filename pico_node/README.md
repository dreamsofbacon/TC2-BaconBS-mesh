# Pico BBS Gateway Client (Phase 4 — feasibility spike)

A low-power **CircuitPython** mesh client for a Raspberry Pi Pico. It does **not**
run the sync engine or a database. It wakes periodically, talks to an attached
Meshtastic radio over UART, sends queued API/AI requests to the BBS gateway,
polls the gateway mailbox for replies, then deep-sleeps. The CPython gateway
needs only one small change (see *Gateway integration* below).

This is the **spike**: the hard, hardware-independent part — hand-rolled
Meshtastic protobuf-over-UART framing + the chunked-response reassembly — is
implemented here and covered by `tests/test_pico_node.py` (runs on your PC, no
hardware). The remaining work is on-hardware bring-up.

---

## Your two questions

### How do I connect the node?

You need **two boards**: a Meshtastic radio (the RF + Meshtastic firmware) and
the Pico (your CircuitPython logic). They are different MCUs — Meshtastic
firmware can't run on a Pico, and CircuitPython can't run on the radio — so the
Pico is a *companion* wired to the radio over a 3-wire serial (UART) link:

```
   Raspberry Pi Pico                 Meshtastic radio (e.g. Heltec V3)
   ----------------                  --------------------------------
   GP0 (UART TX) ───────────────────► RX  (Serial module RXD pin)
   GP1 (UART RX) ◄─────────────────── TX  (Serial module TXD pin)
   GND          ─────────────────────  GND
   (GP2 optional) ──► MOSFET/load switch ──► radio VCC   (power gating, optional)
```

- TX→RX and RX→TX are **crossed** (the classic UART gotcha).
- Common ground is required.
- Both run at **3.3 V logic** — no level shifter needed for Pico ↔ ESP32/nRF52.

On the **radio**, enable Meshtastic's **Serial module** and set it to **PROTO**
mode on the UART pins you wired, baud **115200**. PROTO mode streams the binary
`ToRadio`/`FromRadio` protobufs this client speaks. (Config → Module Config →
Serial: `enabled=true`, `mode=PROTO`, `rxd`/`txd` = your pins, `baud=115200`.)

Recommended starter hardware (uses what you already have):
- **Radio:** a Heltec V3 flashed with Meshtastic (same as your other nodes).
  For the *lowest* sleep current later, an nRF52 board (RAK4631 / Wio-WM1110)
  is better, but a Heltec is fine for bring-up.
- **Controller:** a **Raspberry Pi Pico** (RP2040) or **Pico 2** (RP2350 — more
  RAM, nicer for buffers). Plain Pico is fine; you do **not** need a Pico W
  (the whole point is operating without Wi-Fi).
- A couple of jumper wires; later a LiPo + load switch for true field power.

### Do I need an SD card?

**No.** The Pico has onboard flash that CircuitPython exposes as the `CIRCUITPY`
drive; that holds `code.py` and the few modules here. This node keeps **no
database** — at most a tiny flat file for a request queue — so there's nothing
that needs an SD card. (One CircuitPython quirk: a program can't write to the
`CIRCUITPY` flash while it's also mounted over USB. For a headless field node
that's not an issue; if you want runtime-writable state while developing, either
use the `nvm` byte store or remount the FS writable in code. For this spike we
keep requests in `config.py`, so no writes are needed.)

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `minipb.py` | Pico + PC | Minimal protobuf (varint/fixed32/bytes/submessage). |
| `meshtastic_link.py` | Pico + PC | Meshtastic stream framing + ToRadio/FromRadio for text. |
| `wire.py` | Pico + PC | BBS frame builders + chunked APIRESP reassembly + gap-fill. |
| `code.py` | Pico only | Wake → send → poll → listen → deep-sleep loop (imports `board`). |
| `config.py` | Pico only | Your wiring, gateway node ID, duty cycle, prompts. |

## Flashing / install

1. Install **CircuitPython 9.x** on the Pico (drag the UF2 while holding BOOTSEL).
2. Copy `minipb.py`, `meshtastic_link.py`, `wire.py`, `config.py`, and `code.py`
   onto the `CIRCUITPY` drive.
3. Edit `config.py`: set `GATEWAY_NODE_ID` to your gateway's node ID
   (`!0408b778`), the `UART_TX`/`UART_RX` pins, and add a prompt to
   `QUEUED_PROMPTS` to test.
4. Open the serial console (e.g. `screen`/Mu/Thonny) to watch `REPLY ...` output.

## Gateway integration (one change, do when hardware is ready)

Today the gateway only processes `APIREQ`/`APIPOLL` from nodes in its
`bbs_nodes` sync list; a direct DM from a non-bbs node (the Pico) goes down the
*user/menu* path where those frames aren't handled (`message_processing.on_receive`,
the `to_id == my_node_num` branch). To let the Pico talk to the gateway directly,
route `API*` frames in that direct-message branch through the gateway handlers,
authorized via the **gateway lock-down allow-list** already built
(`gateway.is_requester_authorized`). That keeps access controlled — add the
Pico's node ID to **Settings → API Gateway → Allowed requester nodes**.

The reply path needs no change: the gateway already sends `APIRESP` to whoever
asked, and persists it to the mailbox for `APIPOLL` retrieval (Phase 2), so a
Pico that slept through the immediate reply still gets it on its next wake.

## What's proven vs. what's next

- **Proven (host tests):** protobuf encode/decode, stream (de)framing incl.
  resync across split/garbage reads, text ToRadio build + FromRadio parse,
  node-id math, chunked APIRESP reassembly, and gap-spec computation.
- **Next (on hardware):** Serial-module PROTO bring-up on the radio; confirm
  `want_config` returns our node number; a real round-trip to the gateway;
  power-gating + deep-sleep current measurement; then a small request-queue file
  and a button/display.
