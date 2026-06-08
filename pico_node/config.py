"""Pico node configuration — edit these for your wiring and gateway.

Copy this onto the Pico's CIRCUITPY drive alongside code.py and the modules.
All values are plain constants so no special tooling is needed to change them.
"""

# --- Gateway ---------------------------------------------------------------
# The BBS node that fulfills API/AI requests (your bbs.local gateway's node ID).
GATEWAY_NODE_ID = "!0408b778"

# Used only if the radio hasn't reported our own node number yet.
NODE_ID_FALLBACK = "!00000000"

# --- UART wiring to the Meshtastic radio -----------------------------------
# Pin names are board attributes, e.g. "GP0"/"GP1" on a Raspberry Pi Pico.
# Wire: Pico TX -> radio RX, Pico RX -> radio TX, GND <-> GND.
UART_TX = "GP0"
UART_RX = "GP1"
UART_BAUD = 115200

# Optional GPIO that drives a load switch / MOSFET to cut radio power between
# wakes for battery savings. Set to None if the radio is always powered.
RADIO_EN = None            # e.g. "GP2"
RADIO_BOOT_SECONDS = 3.0   # time for the radio to boot after power-on

# --- Duty cycle ------------------------------------------------------------
SLEEP_SECONDS = 600        # deep sleep between wakes (10 min)
LISTEN_SECONDS = 45        # how long to wait for replies each wake
SEND_GAP_SECONDS = 1.5     # spacing between queued sends
GAP_COOLDOWN_SECONDS = 10  # min spacing between APIRESPGAP nudges per request

# --- What to ask -----------------------------------------------------------
# Simplest request source: a static list of prompts sent every wake. A real
# deployment would replace this with a button-triggered prompt or a small
# queue file on the CIRCUITPY flash.
QUEUED_PROMPTS = [
    # "In 3 sentences, what is amateur radio?",
]
