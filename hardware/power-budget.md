# Power budget — solar cache node

Goal: a **solar-powered, mostly-sleeping** cache node. It is a *periodic puller*
— it wakes, syncs the cache over the mesh, and sleeps. It does **not** need to be
reachable between syncs, which is what lets us power almost everything down.

All numbers below are planning estimates — **measure your actual build** with a
multimeter / power profiler before sizing the final panel and battery.

## Why the controller is an nRF52840, not a Pico
The Raspberry Pi Pico (RP2040) sleeps at ~**0.8–1.3 mA** — far too thirsty to
sit idle on harvested solar power. An **nRF52840** (e.g. Seeed XIAO nRF52840)
sleeps at ~**5 µA** — roughly **200× lower**. Same CircuitPython code runs on
both; only pin names change. For a solar sleeping node this single swap is the
difference between "weeks of autonomy" and "dead by morning."

## Component currents (estimates)

| Component | Asleep (gated off) | Awake / active |
|---|---|---|
| nRF52840 controller (XIAO) | ~5 µA | ~5–15 mA |
| RAK4631 radio (SX1262) | ~0 (power-gated) | ~5–15 mA RX, ~40–120 mA TX bursts |
| microSD card | ~0 (power-gated) | ~20–100 mA on writes, ~1 mA idle |
| CN3065 solar charger (no sun) | <3 µA | — |
| **Total** | **~5–20 µA** | **~30–60 mA average during a sync** |

The radio and SD are switched off during sleep via GPIO-driven load switches
(`RADIO_EN` / `SD_EN`), which `code.py` already accounts for (`RADIO_EN` + a boot
delay). The cache lives on the SD, but it's only powered while syncing/reading.

## Duty-cycle math (the wake interval is the main knob)

Per cycle: a **wake** of ~75 s (radio boot + sync) at ~40 mA, then **sleep** at
~0.015 mA for the rest.

| Wake interval | Avg current | Autonomy on 2000 mAh (no sun) |
|---|---|---|
| every 10 min | ~5.0 mA | ~17 days |
| every 30 min | ~1.7 mA | ~7 weeks |
| every 60 min | ~0.9 mA | ~13 weeks |

Because reads are served from the **local cache** (no radio needed), sync
freshness can be relaxed — **30–60 min is a sensible default** and buys large
autonomy headroom. Make it a config value (`SLEEP_SECONDS`) so you can tune it.

## Solar sizing
- **Charger:** the **RAK19007 has an onboard solar input (P1)** + LiPo charger,
  so a panel can charge the battery directly through the WisBlock base — no
  separate charger needed (verify the accepted input range for your panel). If
  you don't use it, a **CN3065** mini solar LiPo charger (input 4.4–6 V, idle
  <3 µA) is the drop-in alternative; it pulls 80–100 mA even in weak light —
  comfortably more than the ~1–2 mA average draw above.
- **Panel:** a **6 V, 1–2 W** panel (≈ 160–330 mA peak in full sun). Even a few
  hours of decent light per day replaces a full day's consumption many times over.
- **Battery:** 1× LiPo, **1000–2000 mAh**, JST-PH 2.0. The battery is really a
  buffer for night / cloudy stretches; the duty cycle is so low the panel rarely
  has to work hard.

Rule of thumb: at a 30-min wake interval the node needs ~**40 mAh/day**. A 1–2 W
panel harvests that in well under an hour of sun, so even a poor solar week keeps
the 2000 mAh buffer topped up.

## Sleep-current gotchas to check on the real build
- **SD cards leak** — some draw mA even "idle." Power the SD from a switched rail
  (`SD_EN`) so it's fully off during sleep.
- **Onboard LEDs / regulators** on dev boards add µA–mA. The XIAO's lowest sleep
  needs the power LED handled; the Feather idles higher for this reason.
- **Radio gating:** cutting the RAK's supply means it reboots/rejoins each wake
  (~15–30 s). That's fine for a periodic puller; if you ever need it reachable
  between syncs, leave it powered (Meshtastic light-sleep) and accept ~10–15 mA
  average instead.
