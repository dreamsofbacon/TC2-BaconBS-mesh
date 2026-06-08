"""Pico BBS gateway client — CircuitPython entry point (runs as code.py).

A low-power, non-syncing mesh client. It does NOT run the sync engine or a
database. On each wake it:
  1. powers up the attached Meshtastic radio (optional load-switch on RADIO_EN),
  2. learns its own node number (want_config),
  3. sends any queued API requests to the gateway,
  4. polls the gateway mailbox (APIPOLL) and reassembles replies,
  5. hands replies to on_reply() (print / display / log),
  6. powers the radio down and deep-sleeps for SLEEP_SECONDS.

This file only runs on the Pico (it imports board/busio). The protocol logic it
calls (meshtastic_link, wire) is plain Python and is covered by the host test
suite. Edit config.py for your wiring and gateway.

NOTE: requires the gateway-side change that accepts APIREQ/APIPOLL as a direct
message from a non-bbs node (see pico_node/README.md "Gateway integration").
"""

import time
import board
import busio
import digitalio

import config
import meshtastic_link as link
import wire

try:
    import alarm  # deep sleep (CircuitPython)
except ImportError:
    alarm = None


def _make_uart():
    return busio.UART(
        getattr(board, config.UART_TX),
        getattr(board, config.UART_RX),
        baudrate=config.UART_BAUD,
        timeout=0.1,
    )


def _radio_power(on):
    pin_name = getattr(config, "RADIO_EN", None)
    if not pin_name:
        return
    en = digitalio.DigitalInOut(getattr(board, pin_name))
    en.direction = digitalio.Direction.OUTPUT
    en.value = bool(on)


def _pump(uart, reader, deadline):
    """Read available bytes until *deadline*, yielding decoded FromRadio dicts."""
    while time.monotonic() < deadline:
        data = uart.read(256)
        if data:
            for payload in reader.feed(data):
                yield link.parse_fromradio(payload)
        else:
            time.sleep(0.02)


def learn_node_num(uart, reader, timeout=5.0):
    uart.write(link.build_want_config(0xC0FFEE))
    for msg in _pump(uart, reader, time.monotonic() + timeout):
        if "my_node_num" in msg:
            return msg["my_node_num"]
        if "config_complete_id" in msg:
            break
    return None


def on_reply(rid, status, body):
    """Override to drive a display / GPIO / log. Default just prints."""
    print("REPLY rid=%s status=%s: %s" % (rid, status, body))


def run_once():
    _radio_power(True)
    time.sleep(config.RADIO_BOOT_SECONDS)
    uart = _make_uart()
    reader = link.StreamReader()
    assembler = wire.ResponseAssembler()

    my_num = learn_node_num(uart, reader)
    my_id = wire.num_to_node_id(my_num) if my_num else config.NODE_ID_FALLBACK
    gw_num = wire.node_id_to_num(config.GATEWAY_NODE_ID)
    pkt_id = (int(time.monotonic() * 1000) & 0x7FFFFFFF) or 1

    # 1) Send any queued requests (config.QUEUED_PROMPTS is the simplest source;
    #    a real deployment would read a small flat file or a button-triggered
    #    prompt). Each gets a short rid.
    for i, prompt in enumerate(config.QUEUED_PROMPTS):
        rid = "%04x%02x" % (pkt_id & 0xFFFF, i & 0xFF)
        frame = wire.build_ai_request(rid, my_id, prompt)
        uart.write(link.build_text_toradio(gw_num, frame, pkt_id + i))
        time.sleep(config.SEND_GAP_SECONDS)

    # 2) Poll the mailbox for anything queued for us while asleep.
    uart.write(link.build_text_toradio(gw_num, wire.build_apipoll(my_id), pkt_id + 999))

    # 3) Collect replies for a listening window, asking for gap-fill as needed.
    deadline = time.monotonic() + config.LISTEN_SECONDS
    last_gap = {}
    while time.monotonic() < deadline:
        for msg in _pump(uart, reader, time.monotonic() + 0.5):
            text = msg.get("text")
            if not text:
                continue
            done = assembler.feed(text)
            if done:
                on_reply(*done)
        # Nudge gap-fill for slow/partial responses (rate-limited per rid).
        now = time.monotonic()
        for rid in assembler.pending_rids():
            if now - last_gap.get(rid, 0) < config.GAP_COOLDOWN_SECONDS:
                continue
            spec = assembler.gap_spec(rid)
            if spec:
                uart.write(link.build_text_toradio(
                    gw_num, wire.build_apirespgap(rid, spec), pkt_id))
                last_gap[rid] = now

    uart.deinit()
    _radio_power(False)


def deep_sleep(seconds):
    if alarm is None:
        time.sleep(seconds)
        return
    until = alarm.time.TimeAlarm(monotonic_time=time.monotonic() + seconds)
    alarm.exit_and_deep_sleep_until_alarms(until)


while True:
    try:
        run_once()
    except Exception as exc:  # never let a transient error stop the cycle
        print("run_once error:", exc)
    deep_sleep(config.SLEEP_SECONDS)
