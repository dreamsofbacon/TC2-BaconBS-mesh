"""Cache-node configuration — edit for your wiring and gateway.

Copy onto the controller's CIRCUITPY drive alongside code.py and the modules.
This code runs on any CircuitPython board; the build target is a Seeed XIAO
nRF52840 (~5 uA sleep) for solar operation. The folder is named pico_node for
historical reasons — a Pico works for bench bring-up but sleeps ~200x heavier.
Pin names below are XIAO nRF52840 board attributes; change them for your board.
"""

# --- Peers -----------------------------------------------------------------
# The BBS node(s) we pull from (must list us in their [sync] subscriber_nodes).
GATEWAY_NODE_ID = "!0408b778"

# --- Local cache -----------------------------------------------------------
CACHE_PATH = "/sd/bbs"     # SD mount point
MAX_RECORDS = 300          # per-scope cap (oldest pruned) — device storage safety net

# --- UART wiring to the Meshtastic radio -----------------------------------
# Wire: controller TX -> radio RX, controller RX -> radio TX, GND <-> GND.
UART_TX = "TX"             # XIAO D6
UART_RX = "RX"             # XIAO D7
UART_BAUD = 115200

# --- microSD over SPI ------------------------------------------------------
SD_SCK = "SCK"             # XIAO D8
SD_MOSI = "MOSI"           # XIAO D10
SD_MISO = "MISO"           # XIAO D9
SD_CS = "D1"

# --- Power gating (drives high-side load switches) -------------------------
RADIO_EN = "D2"            # switches the radio supply; None = always powered
SD_EN = "D3"               # switches the microSD supply; None = always powered
RADIO_BOOT_SECONDS = 15.0  # radio reboot/rejoin time after power-on (gated radio)

# --- Duty cycle (the main power knob — see hardware/power-budget.md) --------
SLEEP_SECONDS = 1800       # deep sleep between wakes (30 min) — solar-friendly default
SYNC_SECONDS = 60          # listen/sync window each wake
SEND_GAP_SECONDS = 1.5     # spacing between our outgoing frames
