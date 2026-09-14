"""Keep each attached radio's clock set from this node's.

A radio with no GPS, no phone and no network has nothing to set its clock
from, so after a reboot it keeps whatever time it woke up with. bbs.local's
Meshtastic radio came back on 2026-09-12 about 48 days slow. Everything that
radio timestamps was wrong from then on: every broadcast looked seven weeks
old and was dropped from Public Chatter, and the "last heard" time it reports
for every other node was seven weeks stale too.

The node itself is on NTP, so it sets the radio: as soon as a link is up --
startup and every reconnect, because a reconnect is usually a radio that
rebooted -- and again every few hours, because a radio's clock drifts.

Only a trustworthy clock is ever pushed. A Raspberry Pi has no real-time
clock either; until NTP has synced, it runs on whatever fake-hwclock saved at
the last shutdown, and copying that into the radio would replace one wrong
clock with another.

MQTT links have no radio, and are skipped.
"""

import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone

# No real clock is earlier than this; a node reporting an earlier time has
# not synced since boot. The day this module was written.
MIN_PLAUSIBLE_EPOCH = int(datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp())

DEFAULT_INTERVAL_HOURS = 6.0
# When a set fails or the host clock is not yet trusted, try again soon:
# NTP usually syncs within a minute of boot.
RETRY_SECONDS = 60.0
FAILURE_RETRY_SECONDS = 600.0
# A MeshCore radio this close to our time is left alone.
MESHCORE_TOLERANCE_SECONDS = 30

_NTP_CACHE_SECONDS = 60.0
_ntp_cache = {"checked_at": None, "value": None}


def is_enabled() -> bool:
    from utils import _config_bool
    return _config_bool("radio_clock", "enabled", True)


def interval_seconds() -> float:
    from utils import _config_float
    hours = _config_float("radio_clock", "interval_hours", DEFAULT_INTERVAL_HOURS)
    return max(0.25, hours) * 3600.0


def _ntp_synchronized():
    """True or False from systemd, or None where there is no systemd to ask.

    Cached briefly: this is only asked when a set is due, but a node whose
    clock is not yet synced asks once a minute until it is.
    """
    checked_at = _ntp_cache["checked_at"]
    if checked_at is not None and time.monotonic() - checked_at < _NTP_CACHE_SECONDS:
        return _ntp_cache["value"]
    value = None
    if shutil.which("timedatectl"):
        try:
            result = subprocess.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=3)
            answer = result.stdout.strip().lower()
            if result.returncode == 0 and answer in ("yes", "no"):
                value = answer == "yes"
        except (OSError, subprocess.SubprocessError):
            value = None
    _ntp_cache["checked_at"] = time.monotonic()
    _ntp_cache["value"] = value
    return value


def host_clock_trusted(now_epoch=None):
    """(trusted, reason). Never trusted before MIN_PLAUSIBLE_EPOCH; where
    systemd can say whether NTP has synced, it has to say yes."""
    now_epoch = time.time() if now_epoch is None else now_epoch
    if now_epoch < MIN_PLAUSIBLE_EPOCH:
        return False, "this node's clock reads earlier than any real date"
    if _ntp_synchronized() is False:
        return False, "this node's clock has not synced with NTP yet"
    return True, ""


def _is_mqtt(interface) -> bool:
    return str(getattr(interface, "protocol_name", "")).casefold().startswith("mqtt")


def set_radio_clock(interface, epoch: int) -> str:
    """Set one radio's clock. Returns what happened, for the log.

    Raises on failure so the caller can back off.
    """
    sync = getattr(interface, "sync_device_time", None)
    if callable(sync):
        # MeshCore: the interface reads the radio's time first, and only sets
        # it when it is wrong.
        return sync(epoch, tolerance_seconds=MESHCORE_TOLERANCE_SECONDS)
    local_node = getattr(interface, "localNode", None)
    set_time = getattr(local_node, "setTime", None)
    if not callable(set_time):
        raise RuntimeError("this radio interface cannot set its time")
    # Meshtastic: an admin message to the directly attached radio, over the
    # serial/TCP/BLE link -- nothing is transmitted on air, and for the local
    # node the library does not wait for a reply. The radio cannot be asked
    # its time, so it is simply set. Under the BBS send lock, because it is a
    # write on the same link a reply from another thread may be using.
    from utils import interface_send_lock
    with interface_send_lock(interface):
        set_time(int(epoch))
    return "set"


def maintain(interface, link_name: str = "", *, now_monotonic=None) -> None:
    """Set this link's radio clock if it is due. Never raises.

    The schedule lives on the interface object, so a reconnect -- which
    builds a new one -- is due immediately.
    """
    if interface is None or _is_mqtt(interface):
        return
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    due = getattr(interface, "_radio_clock_next_at", None)
    if due is not None and now_monotonic < due:
        return
    label = f"[{link_name}] " if link_name else ""
    try:
        if not is_enabled():
            _schedule(interface, now_monotonic + 3600.0)
            return
        trusted, reason = host_clock_trusted()
        if not trusted:
            if not getattr(interface, "_radio_clock_waiting_logged", False):
                logging.warning(f"{label}Not setting the radio's clock yet: {reason}.")
                interface._radio_clock_waiting_logged = True
            _schedule(interface, now_monotonic + RETRY_SECONDS)
            return
        epoch = int(time.time())
        outcome = set_radio_clock(interface, epoch)
        stamp = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logging.info(f"{label}Radio clock {outcome} ({stamp}).")
        interface._radio_clock_waiting_logged = False
        _schedule(interface, now_monotonic + interval_seconds())
    except Exception as exc:
        logging.warning(f"{label}Could not set the radio's clock: {exc}")
        _schedule(interface, now_monotonic + FAILURE_RETRY_SECONDS)


def _schedule(interface, at: float) -> None:
    try:
        interface._radio_clock_next_at = at
    except Exception:
        pass
