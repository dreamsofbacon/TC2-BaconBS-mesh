"""Pico node configuration — edit these for your wiring and gateway.

Copy onto the Pico's CIRCUITPY drive alongside code.py and the modules.
All values are plain constants — no special tooling needed to change them.
"""

# --- Peers -----------------------------------------------------------------
# The BBS node we send WANT/HASHMISS re-requests to (your bbs.local gateway).
# We also passively sync from any peer whose HAVE broadcasts we hear.
GATEWAY_NODE_ID = "!0408b778"

# --- Local cache -----------------------------------------------------------
# Where the bounded read-cache lives. Use the SD mount point in a real build,
# e.g. "/sd/bbs". Each scope is capped at MAX_RECORDS (oldest pruned).
CACHE_PATH = "/sd/bbs"
MAX_RECORDS = 300

# --- UART wiring to the Meshtastic radio -----------------------------------
# Pin names are board attributes (e.g. "GP0"/"GP1" on a Pico).
# Wire: Pico TX -> radio RX, Pico RX -> radio TX, GND <-> GND.
UART_TX = "GP0"
UART_RX = "GP1"
UART_BAUD = 115200

# Optional GPIO driving a load switch / MOSFET to cut radio power between wakes.
RADIO_EN = None            # e.g. "GP2"; None = radio always powered
RADIO_BOOT_SECONDS = 3.0   # time for the radio to boot after power-on

# --- Duty cycle ------------------------------------------------------------
SLEEP_SECONDS = 600        # deep sleep between wakes (10 min)
SYNC_SECONDS = 60          # listen/sync window each wake (long enough to catch a HAVE)
SEND_GAP_SECONDS = 1.5     # spacing between our outgoing frames
