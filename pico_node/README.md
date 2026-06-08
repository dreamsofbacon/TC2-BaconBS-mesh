# Pico BBS Cache Node (Phase 4)

A low-power **CircuitPython** mesh node for a Raspberry Pi Pico that keeps a
**local copy of the BBS** (bulletins / mail / channels) on an SD card and serves
it instantly, online or off. It stays current over the mesh by speaking the
**same `HAVE` / `WANT` / `EVENT` op_log discovery protocol the full nodes use** —
as a pull-only *subscriber*.

It deliberately does **not** run SQLite, the sync engine, or the HASHZ
hash-repair layer (none of which fit a microcontroller). Instead it pulls each
record it's missing with a `HASHMISS` and parses the record frame the owner
sends back — exactly how the full nodes fetch a missing record. The trade-off:
no automatic hash-repair self-heal, so a record whose reply is permanently lost
(and later pruned from the gateway's op_log) is re-requested on the next wake
via the outstanding-HASHMISS list; a true gap needs a re-snapshot.

The hard, hardware-independent core is implemented **and fully host-tested**
(`tests/test_pico_*.py`, 51 tests, run on your PC — no hardware). What remains
is on-hardware bring-up plus one gateway-side change.

---

## Hardware

### Two boards, wired over UART (not USB)
Meshtastic firmware and CircuitPython can't share one MCU, so the Pico is a
companion to a Meshtastic radio, wired over a 3-wire UART:

```
Raspberry Pi Pico              Meshtastic radio (e.g. Heltec V3)
GP0 (UART TX) ───────────────►  RXD  (Serial-module pin you pick)
GP1 (UART RX) ◄───────────────  TXD  (Serial-module pin you pick)
GND           ────────────────  GND
(GP2 optional ► MOSFET ► radio VCC, to power-gate the radio between wakes)
```
TX/RX **crossed**, common ground, both 3.3 V (no level shifter). On the radio:
Meshtastic **Serial module**, `mode = PROTO`, `baud = 115200`, `rxd`/`txd` set
to the GPIOs you wired.

### SD card — yes (this design)
Unlike a thin relay, this node stores data, so it needs writable storage. Use a
small **SD card over SPI**; it's durable, large, and avoids wearing the Pico's
internal flash. The cache footprint is bounded (see below), so a tiny card is
plenty. Mount it (e.g. at `/sd`) and point `CACHE_PATH` there.

### Recommended parts
- Radio: a **Heltec V3** flashed with Meshtastic (same as your other nodes).
- Controller: a **Raspberry Pi Pico** / **Pico 2** (not a Pico W — no Wi-Fi needed).
- An **SD breakout** (SPI) + a microSD card; jumper wires; later a LiPo + load switch.

---

## How it handles database size
The cache is **capped, prune-oldest**: each scope keeps at most `MAX_RECORDS`
of the newest items (by date); older ones are dropped. So the on-card footprint
has a hard ceiling no matter how long the node runs. The gateway side is bounded
too (Phase 0 prunes `op_log` and `api_mailbox`). No unbounded growth on either end.

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `store.py` | Pico + PC | Bounded SD cache: capped records, board filter, op_log watermarks, JSON persistence. |
| `opsync.py` | Pico + PC | HAVE→WANT, EVENT→watermark+HASHMISS, deletes. |
| `records.py` | Pico + PC | Parse BULLETIN/MAIL/CHANNELCOMMENT (+ CONT/META chunking, base64 sender). |
| `syncclient.py` | Pico + PC | Glue: routes a frame to the right handler, upserts records, tracks outstanding HASHMISS. |
| `meshtastic_link.py` | Pico + PC | Meshtastic stream framing + ToRadio/FromRadio text. |
| `minipb.py` | Pico + PC | Minimal protobuf. |
| `wire.py` | Pico + PC | node-id math + (optional) API/AI request helpers. |
| `code.py` | Pico only | Wake → learn node num → re-request → sync window → save → deep-sleep. |
| `config.py` | Pico only | Wiring, gateway ID, cache path/cap, duty cycle. |

## Install
1. Flash **CircuitPython 9.x** to the Pico (hold BOOTSEL, drag the `.uf2`).
2. Copy `store.py`, `opsync.py`, `records.py`, `syncclient.py`,
   `meshtastic_link.py`, `minipb.py`, `wire.py`, `config.py`, `code.py` to the
   `CIRCUITPY` drive. (Skip `README.md` and `__pycache__`.)
3. Wire and mount the SD card; set `CACHE_PATH` (e.g. `/sd/bbs`).
4. Edit `config.py`: `GATEWAY_NODE_ID`, `UART_TX`/`UART_RX`, duty cycle.
5. Watch the serial console for the `sync wake complete: ...` line.

## Gateway integration (the one live-side change, do when hardware is ready)
For the gateway to answer this non-bbs node's `WANT`/`HASHMISS`, the Pico must be
in the gateway's `bbs_nodes`. But a plain `bbs_nodes` entry would also make the
gateway try to *push-sync and hash-repair to* the Pico, which can't reciprocate
(no HASHZ) — causing perpetual "mismatch" churn. So the gateway needs a
lightweight **subscriber mode**: answer a listed peer's pull requests
(WANT/HASHMISS, HAVE broadcasts) but exclude it from the push-sync and
mismatch-repair loops. Authorize it with the existing gateway allow-list. This
is the next live change and needs a deploy + sign-off — held until hardware.

## Proven vs. next
- **Proven (51 host tests):** protobuf + framing, op_log HAVE/WANT/EVENT,
  BULLETIN/MAIL/CHANNELCOMMENT parsing incl. chunk reassembly and base64 sender,
  bounded cache + watermarks + persistence, and the full SyncClient round-trip
  (HAVE→WANT, EVENT→HASHMISS→record→cached, deletes, re-request).
- **Next (on hardware):** Serial-module PROTO bring-up; confirm `want_config`
  returns our node number; gateway subscriber mode; a real multi-record sync
  over the air; power-gating + deep-sleep current; then a reader UI for the cache.
