"""Pico BBS cache node — CircuitPython entry point (runs as code.py).

A low-power op_log *subscriber*. It does not run the hash-repair engine or a
database; it keeps a bounded local cache (bulletins / mail / channels) on an SD
card and stays current over the mesh using the same HAVE/WANT/EVENT discovery
protocol the full nodes use. Each wake:

  1. power up the radio (optional load switch on RADIO_EN),
  2. learn our own node number,
  3. re-request anything we asked for last time but never got (outstanding HASHMISS),
  4. listen: route every incoming sync frame through SyncClient, replying to
     the frame's sender (WANT to a HAVE, HASHMISS to an EVENT),
  5. persist the cache to SD,
  6. power the radio down and deep-sleep.

This file runs only on the Pico (imports board/busio). The protocol logic it
drives (store, syncclient, opsync, records, meshtastic_link, wire) is plain
Python and is covered by the host test suite. Requires the gateway-side
"subscriber" support so this non-bbs node's WANT/HASHMISS are answered.
"""

import time
import board
import busio
import digitalio

import config
import meshtastic_link as link
import store as store_mod
import syncclient

try:
    import alarm
except ImportError:
    alarm = None


def _set_pin(name, value):
    """Drive a load-switch enable pin (None = no switch / always on)."""
    if not name:
        return None
    pin = digitalio.DigitalInOut(getattr(board, name))
    pin.direction = digitalio.Direction.OUTPUT
    pin.value = bool(value)
    return pin


def _mount_sd():
    """Power + mount the microSD at /sd. Returns the SPI bus so it can be deinit'd."""
    import storage
    import sdcardio
    _set_pin(getattr(config, "SD_EN", None), True)
    time.sleep(0.2)
    spi = busio.SPI(getattr(board, config.SD_SCK), getattr(board, config.SD_MOSI),
                    getattr(board, config.SD_MISO))
    sd = sdcardio.SDCard(spi, getattr(board, config.SD_CS))
    storage.mount(storage.VfsFat(sd), "/sd")
    return spi


def _unmount_sd(spi):
    import storage
    try:
        storage.umount("/sd")
    except Exception:
        pass
    if spi is not None:
        spi.deinit()
    _set_pin(getattr(config, "SD_EN", None), False)


def _make_uart():
    return busio.UART(getattr(board, config.UART_TX), getattr(board, config.UART_RX),
                      baudrate=config.UART_BAUD, timeout=0.1)


def _radio_power(on):
    pin = getattr(config, "RADIO_EN", None)
    if not pin:
        return
    en = digitalio.DigitalInOut(getattr(board, pin))
    en.direction = digitalio.Direction.OUTPUT
    en.value = bool(on)


def _pump(uart, reader, deadline):
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


def _send_text(uart, dest_num, text, pkt_id):
    uart.write(link.build_text_toradio(dest_num, text, pkt_id))


def run_once():
    spi_sd = _mount_sd()
    cache = store_mod.CacheStore(config.CACHE_PATH, max_records=config.MAX_RECORDS).load()
    client = syncclient.SyncClient(cache)

    _radio_power(True)
    time.sleep(config.RADIO_BOOT_SECONDS)
    uart = _make_uart()
    reader = link.StreamReader()
    pkt = (int(time.monotonic() * 1000) & 0x7FFFFFFF) or 1

    learn_node_num(uart, reader)  # also lets the radio settle / drains config stream

    # Re-request records we asked for last wake but never received (our stand-in
    # for the hash-repair self-heal). Send to the gateway.
    gw_num = None
    try:
        import wire
        gw_num = wire.node_id_to_num(config.GATEWAY_NODE_ID)
    except Exception:
        pass
    if gw_num is not None:
        for frame in client.outstanding_hashmiss():
            pkt += 1
            _send_text(uart, gw_num, frame, pkt)
            time.sleep(config.SEND_GAP_SECONDS)

    # Listen: the gateway broadcasts HAVE periodically; respond and absorb the
    # EVENT/record stream. Replies go back to whoever sent the frame.
    deadline = time.monotonic() + config.SYNC_SECONDS
    while time.monotonic() < deadline:
        for msg in _pump(uart, reader, time.monotonic() + 0.5):
            text = msg.get("text")
            sender = msg.get("from")
            if not text or sender is None:
                continue
            for reply in client.handle_frame(text):
                pkt += 1
                _send_text(uart, sender, reply, pkt)

    cache.save()
    uart.deinit()
    _radio_power(False)
    print("sync wake complete: bulletins=%d mail=%d channels=%d" % (
        cache.count("bulletins"), cache.count("mail"), cache.count("channels")))
    _unmount_sd(spi_sd)


def deep_sleep(seconds):
    if alarm is None:
        time.sleep(seconds)
        return
    alarm.exit_and_deep_sleep_until_alarms(
        alarm.time.TimeAlarm(monotonic_time=time.monotonic() + seconds))


while True:
    try:
        run_once()
    except Exception as exc:
        print("run_once error:", exc)
    deep_sleep(config.SLEEP_SECONDS)
