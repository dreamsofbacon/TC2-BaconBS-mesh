import configparser
import gc
import os
import re
import time
from typing import Any, Optional
from app_paths import resolve_app_path
import meshtastic.mesh_interface
import meshtastic.stream_interface
import meshtastic.serial_interface
import meshtastic.tcp_interface
import serial
import serial.tools.list_ports
import argparse


# Unattended-recovery tunables. A wedged radio (USB-serial enumerates but the
# ESP32 stops answering the Meshtastic handshake) is recovered remotely by
# pulsing DTR/RTS — the same auto-reset circuit esptool uses — so no physical
# access is required. After too many failures we give up on THIS interface
# and return None -- same as an unreachable MQTT broker (_open_mqtt_interface)
# -- rather than taking the whole process down. A single wedged radio must
# not block the other radio (or MQTT links) from running; server.py's
# per-link reconnect loop (_reconnect_link) keeps retrying this interface
# in the background so it can rejoin once it recovers, with no restart
# needed.
_MAX_CONNECT_FAILURES_BEFORE_EXIT = int(os.environ.get("BBS_MAX_CONNECT_FAILURES", "8"))
_RADIO_BOOT_WAIT_SECONDS = float(os.environ.get("BBS_RADIO_BOOT_WAIT_SECONDS", "8"))

# MQTT links are internet-bridge-only, not the last physical radio on the
# node -- an unreachable broker is never worth a supervised process restart
# the way a wedged serial radio is. Bounded retry, then give up and continue
# without that link (see _open_mqtt_interface).
_MQTT_MAX_CONNECT_FAILURES = int(os.environ.get("BBS_MQTT_MAX_CONNECT_FAILURES", "5"))


def _reset_serial_radio(device: str) -> bool:
    """Hardware-reset the ESP32 behind a USB-serial radio by pulsing the
    auto-reset lines (RTS→EN, DTR→GPIO0). Recovers a wedged radio over the
    existing USB cable with no physical access. Best-effort; never raises."""
    if not device:
        return False
    try:
        s = serial.Serial(device, 115200)
        try:
            s.setDTR(False)   # GPIO0 high → normal boot (not flash mode)
            s.setRTS(True)    # EN low  → hold ESP32 in reset
            time.sleep(0.2)
            s.setRTS(False)   # EN high → release; chip boots
            time.sleep(0.1)
        finally:
            s.close()
        print(f"Sent DTR/RTS reset pulse to radio on {device}; waiting {_RADIO_BOOT_WAIT_SECONDS:.0f}s for boot...")
        time.sleep(_RADIO_BOOT_WAIT_SECONDS)
        return True
    except Exception as e:
        print(f"Radio reset pulse failed on {device}: {e}")
        return False


def _print_serial_device_diagnostic(device: Optional[str]) -> None:
    """Explain a serial connect failure in terms of what's actually on the
    system right now.

    A configured /dev/ttyUSBn can silently become wrong: those numbers are
    assigned in enumeration order, so a reboot/USB reconnect can renumber
    them or swap which radio is which. Both produce failures that look
    nothing like their cause -- a missing path reads as "radio is dead",
    and a swapped path reads as "radio is broken" while the BBS actually
    handshakes the wrong protocol at a perfectly healthy board. Printing
    the real device list plus the stable-alias hint turns either into an
    obvious fix. Best-effort: never allowed to disturb the connect loop.
    """
    try:
        if not device:
            return
        exists = os.path.exists(device)
        print(f"  Configured port: {device} ({'present' if exists else 'DOES NOT EXIST'})")
        try:
            ports = sorted(p.device for p in serial.tools.list_ports.comports())
            print(f"  Serial devices present: {', '.join(ports) if ports else '(none)'}")
        except Exception:
            pass
        by_id_dir = '/dev/serial/by-id'
        if os.path.isdir(by_id_dir):
            aliases = sorted(os.listdir(by_id_dir))
            if aliases:
                print("  Stable aliases (prefer these over /dev/ttyUSBn):")
                for alias in aliases:
                    full = os.path.join(by_id_dir, alias)
                    print(f"    {full} -> {os.path.realpath(full)}")
        if not exists:
            print("  Hint: the device path changed. Set 'port' to a "
                  "/dev/serial/by-id/... or /dev/serial/by-path/... alias so it "
                  "survives renumbering (see the README's 'Use stable device paths').")
    except Exception:
        pass


class NoSerialPortsDetected(ValueError):
    """Auto-detection found no serial ports at all.

    A ValueError subclass, so anything that already treats interface
    configuration problems as fatal keeps behaving the same. It is separated
    out because this is the one serial-resolution failure that retrying CAN
    fix: an unplugged radio, a board that has not finished enumerating, or an
    install that simply has not been given a radio yet.

    Treated as a config error it killed the entire node -- second radio, MQTT
    links, everything -- for precisely the condition the background reconnect
    loop exists to ride out. A container with no device passed through never
    got past this, so its web admin, the only way to configure a radio in the
    first place, was never reachable long enough to use.
    """


def _resolve_serial_device(system_config: dict) -> str:
    """Return the concrete serial device path, auto-detecting when not set."""
    if system_config.get('port'):
        return system_config['port']
    ports = list(serial.tools.list_ports.comports())
    if len(ports) == 1:
        return ports[0].device
    if len(ports) > 1:
        # Genuinely fatal: retrying cannot pick one, a human has to.
        port_list = ', '.join(p.device for p in ports)
        raise ValueError(f"Multiple serial ports detected: {port_list}. Specify one with the 'port' argument.")
    raise NoSerialPortsDetected("No serial ports detected.")


def init_cli_parser() -> argparse.Namespace:
    """Function build the CLI parser and parses the arguments.

    Returns:
        argparse.ArgumentParser: Argparse namespace with processed CLI args
    """
    parser = argparse.ArgumentParser(description="Bacon BBS mesh radio")
    
    parser.add_argument(
        "--config", "-c",
        action="store",
        help="System configuration file",
        default=None)
    
    parser.add_argument(
        "--interface-type", "-i",
        action="store",
        choices=['serial', 'tcp', 'meshcore_serial', 'meshcore_tcp', 'meshcore_ble'],
        help="Node interface type",
        default=None)
    
    parser.add_argument(
        "--port", "-p",
        action="store",
        help="Serial port",
        default=None)
    
    parser.add_argument(
        "--host", 
        action="store",
        help="TCP host address",
        default=None)

    parser.add_argument(
        "--tcp-port",
        action="store",
        type=int,
        help="TCP port (MeshCore default: 5000)",
        default=None)

    parser.add_argument(
        "--baudrate",
        action="store",
        type=int,
        help="Serial baud rate (MeshCore default: 115200)",
        default=None)

    parser.add_argument(
        "--ble-address",
        action="store",
        help="MeshCore BLE address (omit to scan)",
        default=None)

    parser.add_argument(
        "--ble-pin",
        action="store",
        help="Optional MeshCore BLE pairing PIN",
        default=None)

    parser.add_argument(
        "--channel-index",
        action="store",
        type=int,
        help="MeshCore channel index used for broadcast notifications",
        default=None)
    
    parser.add_argument(
        "--mqtt-topic", '-t', 
        action="store",
        help="MQTT topic to subscribe",
        default='meshtastic.receive')
    #
    # Add extra arguments here
    #...
    
    args = parser.parse_args()
    
    return args
    
    
def merge_config(system_config:dict[str, Any], args:argparse.Namespace) -> dict[str, Any]:
    """Function merges configuration read from the config file and provided on the CLI.
    
    CLI arguments override values defined in the config file.
    system_config argument is mutated by the function.

    Args:
        system_config (dict[str, Any]): System config dict returned by initialize_config()
        args (argparse.Namespace): argparse namespace with parsed CLI args

    Returns:
        dict[str, Any]: system config dict with merged configurations
    """
    
    if args.interface_type is not None:
        system_config['interface_type'] = args.interface_type
        
    if args.port is not None:
        system_config['port'] = args.port
        
    if args.host is not None:
        system_config['hostname'] = args.host

    if args.tcp_port is not None:
        system_config['tcp_port'] = args.tcp_port

    if args.baudrate is not None:
        system_config['baudrate'] = args.baudrate

    if args.ble_address is not None:
        system_config['ble_address'] = args.ble_address

    if args.ble_pin is not None:
        system_config['ble_pin'] = args.ble_pin

    if args.channel_index is not None:
        system_config['channel_index'] = args.channel_index

    if args.mqtt_topic is not None:
        system_config['mqtt_topic'] = args.mqtt_topic
    
    return system_config


def _read_interface_settings(section) -> dict[str, Any]:
    """Read the connection settings shared by primary/secondary [interfaceN]
    sections. Does NOT read 'type' -- callers handle that themselves since
    it's required for the primary section but optional (absent = disabled)
    for the secondary one."""
    return {
        'hostname': section.get('hostname', fallback=None),
        'port': section.get('port', fallback=None),
        'tcp_port': section.getint('tcp_port', fallback=5000),
        'baudrate': section.getint('baudrate', fallback=115200),
        'ble_address': section.get('ble_address', fallback=None),
        'ble_pin': section.get('ble_pin', fallback=None),
        'channel_index': section.getint('channel_index', fallback=0),
    }


def _read_node_list(config: configparser.ConfigParser, section: str, key: str) -> list:
    raw = config.get(section, key, fallback='').split(',')
    return [node.strip() for node in raw if node.strip()]


def _read_mqtt_settings(section) -> dict[str, Any]:
    """Read connection settings for one [mqttN] section.

    The tls_* keys are the advanced/certificate options -- see
    mqtt_interface.MqttInterface for how each is applied. All optional:
    absent means 'tls = true' uses the system CA store, matching the
    behavior from before these existed.
    """
    return {
        'host': section.get('host', fallback=None),
        'port': section.getint('port', fallback=1883),
        'username': section.get('username', fallback=None),
        'password': section.get('password', fallback=None),
        'tls': section.getboolean('tls', fallback=False),
        'tls_ca_certs': section.get('tls_ca_certs', fallback=None),
        'tls_certfile': section.get('tls_certfile', fallback=None),
        'tls_keyfile': section.get('tls_keyfile', fallback=None),
        'tls_keyfile_password': section.get('tls_keyfile_password', fallback=None),
        'tls_insecure': section.getboolean('tls_insecure', fallback=False),
        'topic_prefix': section.get('topic_prefix', fallback=None),
        'local_id': section.get('local_id', fallback=None),
        'client_id': section.get('client_id', fallback=None),
        'keepalive': section.getint('keepalive', fallback=60),
        # What this broker receives, beyond the sync traffic the bridge
        # exists for. Each is independent so one node can send full
        # telemetry to a home broker while a remote bridge gets sync only.
        # publish_status defaults true (the pre-existing behavior); the
        # rest default false so an existing config's traffic is unchanged.
        'publish_status': section.getboolean('publish_status', fallback=True),
        'publish_clients': section.getboolean('publish_clients', fallback=False),
        'publish_telemetry': section.getboolean('publish_telemetry', fallback=False),
        'publish_activity': section.getboolean('publish_activity', fallback=False),
        'publish_sync_stats': section.getboolean('publish_sync_stats', fallback=False),
        # Root for PUBLISHED DATA only. Blank = use topic_prefix. Lets
        # telemetry slot into an existing hierarchy (e.g. a Home Assistant
        # tree) without touching topic_prefix, which identifies the bridge
        # relationship and must stay stable for sync to work.
        'publish_prefix': section.get('publish_prefix', fallback=None),
        # Only publish devices seen this recently. The roster accumulates
        # every node ever heard, which on a busy mesh is hundreds of
        # entries -- far more than a bridge needs, and expensive on a
        # metered link. 0 = no limit (publish everything ever recorded).
        'publish_clients_max_age_hours': section.getint(
            'publish_clients_max_age_hours', fallback=24),
    }


_MQTT_SECTION_RE = re.compile(r'^mqtt(\d+)$')


def discover_mqtt_link_names(config: configparser.ConfigParser) -> list:
    """Scan for [mqttN] sections (N = 1, 2, 3, ...), skipping any with
    'enabled = false'. Sorted by N so link ordering/naming stays stable
    across restarts regardless of the section order in config.ini.

    Deliberately open-ended, unlike [interface2]'s fixed single slot --
    "multiple simultaneous MQTT links" means a node can bridge to several
    independent remote sites at once, not just one secondary connection.
    """
    found = []
    for section_name in config.sections():
        match = _MQTT_SECTION_RE.match(section_name)
        if not match:
            continue
        if not config.getboolean(section_name, 'enabled', fallback=True):
            continue
        found.append((int(match.group(1)), section_name))
    found.sort(key=lambda item: item[0])
    return [name for _, name in found]


def parse_mqtt_links(config: configparser.ConfigParser) -> list:
    """Build the mqtt_links list from a parsed config.

    Shared by startup (initialize_config) and the runtime reload
    (reload_mqtt_links) so both paths can never drift apart in how they
    interpret an [mqttN] section.
    """
    links: list = []
    for link_name in discover_mqtt_link_names(config):
        settings = _read_mqtt_settings(config[link_name])
        if not settings['host'] or not settings['topic_prefix']:
            print(f"[{link_name}] Skipping MQTT link: 'host' and 'topic_prefix' are both required")
            continue
        local_id = settings['local_id'] or f"{link_name}-node"
        links.append({
            'name': link_name,
            'host': settings['host'],
            'port': settings['port'],
            'username': settings['username'],
            'password': settings['password'],
            'tls': settings['tls'],
            'tls_ca_certs': settings['tls_ca_certs'],
            'tls_certfile': settings['tls_certfile'],
            'tls_keyfile': settings['tls_keyfile'],
            'tls_keyfile_password': settings['tls_keyfile_password'],
            'tls_insecure': settings['tls_insecure'],
            'topic_prefix': settings['topic_prefix'],
            'local_id': local_id,
            'client_id': settings['client_id'],
            'keepalive': settings['keepalive'],
            'publish_status': settings['publish_status'],
            'publish_clients': settings['publish_clients'],
            'publish_telemetry': settings['publish_telemetry'],
            'publish_activity': settings['publish_activity'],
            'publish_sync_stats': settings['publish_sync_stats'],
            'publish_prefix': settings['publish_prefix'],
            'publish_clients_max_age_hours': settings['publish_clients_max_age_hours'],
            'sync_section': f'sync_{link_name}',
            'allow_section': f'allow_list_{link_name}',
            'bbs_nodes_key': f'bbs_nodes_{link_name}',
            'allowed_nodes_key': f'allowed_nodes_{link_name}',
            'subscriber_nodes_key': f'subscriber_nodes_{link_name}',
        })
    return links


def reload_mqtt_links(system_config: dict[str, Any]) -> list:
    """Re-read every [mqttN] section from config.ini and refresh
    system_config['mqtt_links'] in place.

    Without this, mqtt_links stays frozen at whatever was on disk when the
    process started, so a broker edited through the web admin would be
    rebuilt from STALE settings on reconnect -- appearing to succeed while
    silently ignoring the change. Called by get_mqtt_interface_by_name (so
    a plain reconnect applies edits) and by server.reload_links_from_config
    (which also adds/removes whole links).
    """
    config = configparser.ConfigParser()
    config.read(system_config.get('config_file') or resolve_app_path(
        os.getenv("BBS_CONFIG_PATH"), "config.ini"))
    fresh = parse_mqtt_links(config)
    system_config['mqtt_links'] = fresh
    return fresh


def initialize_config(config_file: str = None) -> dict[str, Any]:
    """
    Function reads and parses system configuration file

    Returns a dict with the following entries:
    config - parsed config file
    interface_type - type of the active interface
    hostname - host name for TCP interface
    port - serial port name for serial interface
    bbs_nodes - list of peer nodes to sync with

    Also optionally reads a second radio for dual-radio bridge mode: an
    [interface2] section (type/hostname/port/etc., same keys as [interface])
    plus [sync2]/[allow_list2] for its peer lists. Entirely additive -- a
    config file without [interface2] (or with 'enabled = false') behaves
    identically to before dual-radio support existed; 'interface2_enabled'
    is False and get_secondary_interface() returns None.

    Args:
        config_file (str, optional): Path to config file. Function reads from './config.ini' if this arg is set to None. Defaults to None.

    Returns:
        dict: dict with system configuration, ad described above
    """
    config = configparser.ConfigParser()

    if config_file is None:
        config_file = resolve_app_path(os.getenv("BBS_CONFIG_PATH"), "config.ini")
    config.read(config_file)

    # A node with no radio is a real deployment, not a misconfiguration:
    # an MQTT-only mirror of another BBS, or a fresh install whose radio
    # has not been chosen yet. A missing [interface] section means the
    # same thing, so it resolves to 'none' rather than a KeyError.
    interface_type = (
        config.get('interface', 'type', fallback='none').strip().lower() or 'none')
    _primary = _read_interface_settings(config['interface'])
    hostname = _primary['hostname']
    port = _primary['port']
    tcp_port = _primary['tcp_port']
    baudrate = _primary['baudrate']
    ble_address = _primary['ble_address']
    ble_pin = _primary['ble_pin']
    channel_index = _primary['channel_index']

    bbs_nodes = _read_node_list(config, 'sync', 'bbs_nodes')
    subscriber_nodes = _read_node_list(config, 'sync', 'subscriber_nodes')

    sync_interval_raw = config.get('sync', 'sync_interval_minutes', fallback='5').strip()
    try:
        sync_interval_minutes = int(sync_interval_raw)
    except ValueError:
        sync_interval_minutes = 5
    sync_interval_minutes = max(1, sync_interval_minutes)

    print(f"Configured to sync with the following BBS nodes: {bbs_nodes}")

    allowed_nodes = _read_node_list(config, 'allow_list', 'allowed_nodes')

    print(f"Nodes with Urgent board permissions: {allowed_nodes}")

    # --- Optional secondary radio (dual-radio bridge mode) -----------------
    interface2_enabled = False
    interface2_type = None
    _secondary = {
        'hostname': None, 'port': None, 'tcp_port': 5000, 'baudrate': 115200,
        'ble_address': None, 'ble_pin': None, 'channel_index': 0,
    }
    bbs_nodes2: list = []
    subscriber_nodes2: list = []
    allowed_nodes2: list = []
    if config.has_section('interface2'):
        _sect2 = config['interface2']
        _type2_raw = _sect2.get('type', fallback='').strip().lower()
        interface2_enabled = bool(_type2_raw) and _sect2.getboolean('enabled', fallback=True)
        if interface2_enabled:
            interface2_type = _type2_raw
            _secondary = _read_interface_settings(_sect2)
            bbs_nodes2 = _read_node_list(config, 'sync2', 'bbs_nodes')
            subscriber_nodes2 = _read_node_list(config, 'sync2', 'subscriber_nodes')
            allowed_nodes2 = _read_node_list(config, 'allow_list2', 'allowed_nodes')
            print(f"Dual-radio bridge mode enabled: interface2 = {interface2_type}, "
                  f"bridging to BBS nodes: {bbs_nodes2}")

    # --- Optional MQTT internet-bridge links (0, 1, or many) ----------------
    # Unlike interface2's single fixed slot, this is a genuinely open-ended
    # list -- see discover_mqtt_link_names(). Absent [mqttN] sections means
    # mqtt_links is empty and behavior is identical to before MQTT support
    # existed.
    mqtt_links: list = parse_mqtt_links(config)
    for _link in mqtt_links:
        print(f"MQTT link '{_link['name']}' configured: {_link['host']}:{_link['port']} "
              f"topic_prefix={_link['topic_prefix']}")

    return {
        'config': config,
        'config_file': config_file,
        'interface_type': interface_type,
        'hostname': hostname,
        'port': port,
        'tcp_port': tcp_port,
        'baudrate': baudrate,
        'ble_address': ble_address,
        'ble_pin': ble_pin,
        'channel_index': channel_index,
        'bbs_nodes': bbs_nodes,
        'subscriber_nodes': subscriber_nodes,
        'sync_interval_minutes': sync_interval_minutes,
        'allowed_nodes': allowed_nodes,
        'mqtt_topic': 'meshtastic.receive',
        'interface2_enabled': interface2_enabled,
        'interface2_type': interface2_type,
        'interface2_hostname': _secondary['hostname'],
        'interface2_port': _secondary['port'],
        'interface2_tcp_port': _secondary['tcp_port'],
        'interface2_baudrate': _secondary['baudrate'],
        'interface2_ble_address': _secondary['ble_address'],
        'interface2_ble_pin': _secondary['ble_pin'],
        'interface2_channel_index': _secondary['channel_index'],
        'bbs_nodes2': bbs_nodes2,
        'subscriber_nodes2': subscriber_nodes2,
        'allowed_nodes2': allowed_nodes2,
        'mqtt_links': mqtt_links,
    }



def _open_interface(cfg: dict[str, Any]) -> Any:
    """
    Open a Meshtastic or MeshCore radio interface described by ``cfg``.

    ``cfg`` uses the same key names as the top-level system_config dict
    (interface_type/hostname/port/tcp_port/baudrate/ble_address/ble_pin/
    channel_index/mqtt_topic) so both the primary and secondary interfaces
    can share this one connect-with-retry implementation -- see
    get_interface() and get_secondary_interface().

    Meshtastic serial/TCP modes return the native library interface. MeshCore
    serial/TCP/BLE modes return ``MeshCoreInterface``, which presents the small
    synchronous compatibility surface used by the BBS.

    Raises:
        ValueError: Exception raised in the following cases:
                - Type of interface not provided
                - Multiple serial ports present in the system, and no port specified in the configuration
                - Hostname not provided for TCP interface

        A serial interface with NO ports present is deliberately not in that
        list: it is retried like any other failed connect, because an
        unplugged or not-yet-enumerated radio is exactly what the reconnect
        loop is for. See NoSerialPortsDetected.

    Returns:
        A connected radio interface, or None if it never connected after
        _MAX_CONNECT_FAILURES_BEFORE_EXIT attempts (caller should treat this
        the same as an interface that dropped mid-session -- see
        server.py's RadioLink / _reconnect_link).
    """
    interface_type = cfg['interface_type']
    # 'none' is a node with no radio of its own -- it syncs over MQTT only.
    valid_types = ('none', 'serial', 'tcp', 'meshcore_serial', 'meshcore_tcp',
                   'meshcore_ble')
    if interface_type not in valid_types:
        raise ValueError("Invalid interface type specified in config file")
    if interface_type in ('tcp', 'meshcore_tcp') and not cfg.get('hostname'):
        raise ValueError("Hostname must be specified for TCP interface")

    if interface_type == 'none':
        # Deliberately no radio. Returning None immediately (rather than
        # letting it fall into the retry loop) is what stops the node
        # spending the rest of its life reconnecting to a device that was
        # never meant to exist.
        return None

    failures = 0
    while True:
        device = None
        try:
            if interface_type == 'serial':
                # "Multiple ports, pick one" is fatal -- retrying cannot
                # choose. "No ports at all" is not: it is an unplugged or
                # not-yet-enumerated radio, and falls through to the retry
                # path below like any other failed connect.
                device = _resolve_serial_device(cfg)
                iface = meshtastic.serial_interface.SerialInterface(device)
            elif interface_type == 'tcp':
                iface = meshtastic.tcp_interface.TCPInterface(hostname=cfg['hostname'])
            else:
                from meshcore_interface import MeshCoreInterface

                meshcore_transport = interface_type.removeprefix('meshcore_')
                if meshcore_transport == 'serial':
                    device = _resolve_serial_device(cfg)
                iface = MeshCoreInterface(
                    meshcore_transport,
                    port=device,
                    baudrate=int(cfg.get('baudrate', 115200)),
                    hostname=cfg.get('hostname'),
                    tcp_port=int(cfg.get('tcp_port', 5000)),
                    ble_address=cfg.get('ble_address'),
                    ble_pin=cfg.get('ble_pin'),
                    channel_index=int(cfg.get('channel_index', 0)),
                    receive_topic=cfg.get('mqtt_topic', 'meshtastic.receive'),
                )
            if failures:
                print(f"Radio connection recovered after {failures} failed attempt(s).")
            return iface
        except (NoSerialPortsDetected, PermissionError,
                meshtastic.mesh_interface.MeshInterface.MeshInterfaceError,
                ConnectionResetError, ConnectionRefusedError, OSError) as e:
            failures += 1
            print(f"Radio connect attempt {failures} failed: {type(e).__name__}: {e}")
            # Once per connect cycle, not per attempt -- the device list
            # can't change between back-to-back retries, so repeating it
            # would just bury the actual error.
            if failures == 1 and interface_type in ('serial', 'meshcore_serial'):
                _print_serial_device_diagnostic(device)
            # Force release of any serial handle left open by the failed attempt
            # (meshtastic may not close pyserial on handshake failure → the next
            # attempt would hit 'could not exclusively lock port' forever).
            # `e.__traceback__` keeps every frame on the stack alive -- including
            # whichever one inside SerialInterface.__init__() still holds the open
            # pyserial handle -- so gc.collect() cannot free it while `e` is still
            # bound. Drop the reference first so the leaked handle actually becomes
            # collectible.
            del e
            gc.collect()
            if failures >= _MAX_CONNECT_FAILURES_BEFORE_EXIT:
                print(
                    f"{failures} consecutive connect failures on {interface_type} "
                    f"({device or cfg.get('hostname') or 'unknown device'}); giving up on this "
                    "interface for now. The rest of the node (other radio / MQTT links / web "
                    "admin) is unaffected -- a background reconnect loop will keep retrying."
                )
                return None
            # Escalate on every other failure: hardware-reset a wedged radio.
            # (Serial only — TCP devices can't be DTR/RTS reset over the network.)
            if interface_type == 'serial' and failures % 2 == 0:
                try:
                    _reset_serial_radio(device or _resolve_serial_device(cfg))
                except Exception:
                    time.sleep(5)
            else:
                time.sleep(5)


def get_interface(system_config: dict[str, Any]) -> Any:
    """Open the configured primary radio interface. See _open_interface()."""
    return _open_interface({
        'interface_type': system_config['interface_type'],
        'hostname': system_config.get('hostname'),
        'port': system_config.get('port'),
        'tcp_port': system_config.get('tcp_port', 5000),
        'baudrate': system_config.get('baudrate', 115200),
        'ble_address': system_config.get('ble_address'),
        'ble_pin': system_config.get('ble_pin'),
        'channel_index': system_config.get('channel_index', 0),
        'mqtt_topic': system_config.get('mqtt_topic', 'meshtastic.receive'),
    })


def get_secondary_interface(system_config: dict[str, Any]) -> Any:
    """Open the optional secondary radio interface for dual-radio bridge mode.

    Returns None when [interface2] is absent, has no 'type', or has
    'enabled = false' -- i.e. every deployment that doesn't opt into bridge
    mode. Uses the SAME mqtt_topic as the primary interface: both radios'
    received-packet events must land on one shared pypubsub topic for
    server.py's single receive_packet subscriber to see traffic from either
    side (see meshcore_interface.py's receive_topic default and the project
    plan's discussion of why this makes bridging need no relay layer)."""
    if not system_config.get('interface2_enabled'):
        return None
    return _open_interface({
        'interface_type': system_config['interface2_type'],
        'hostname': system_config.get('interface2_hostname'),
        'port': system_config.get('interface2_port'),
        'tcp_port': system_config.get('interface2_tcp_port', 5000),
        'baudrate': system_config.get('interface2_baudrate', 115200),
        'ble_address': system_config.get('interface2_ble_address'),
        'ble_pin': system_config.get('interface2_ble_pin'),
        'channel_index': system_config.get('interface2_channel_index', 0),
        'mqtt_topic': system_config.get('mqtt_topic', 'meshtastic.receive'),
    })


def _open_mqtt_interface(link_cfg: dict[str, Any]):
    """Open one configured MQTT link with bounded retry-then-give-up.

    Unlike _open_interface's serial-radio retry loop, an unreachable MQTT
    broker is never fatal to the whole process (no os._exit) -- a node
    without its MQTT bridge is still a fully functional local BBS. After
    repeated failures this gives up and returns None; the caller logs and
    continues without that link. paho itself owns transient reconnects once
    connected (see mqtt_interface.py's reconnect_delay_set) -- this retry
    loop only covers the initial connect.
    """
    from mqtt_interface import MqttInterface

    name = link_cfg['name']
    failures = 0
    while True:
        try:
            iface = MqttInterface(
                host=link_cfg['host'],
                port=link_cfg['port'],
                topic_prefix=link_cfg['topic_prefix'],
                local_id=link_cfg['local_id'],
                username=link_cfg.get('username'),
                password=link_cfg.get('password'),
                tls=link_cfg.get('tls', False),
                tls_ca_certs=link_cfg.get('tls_ca_certs'),
                tls_certfile=link_cfg.get('tls_certfile'),
                tls_keyfile=link_cfg.get('tls_keyfile'),
                tls_keyfile_password=link_cfg.get('tls_keyfile_password'),
                tls_insecure=link_cfg.get('tls_insecure', False),
                publish_kinds={
                    kind: bool(link_cfg.get(f'publish_{kind}', default))
                    for kind, default in (
                        ('status', True), ('clients', False), ('telemetry', False),
                        ('activity', False), ('sync_stats', False),
                    )
                },
                publish_prefix=link_cfg.get('publish_prefix'),
                publish_clients_max_age_hours=link_cfg.get(
                    'publish_clients_max_age_hours', 24),
                client_id=link_cfg.get('client_id'),
                link_name=name,
                keepalive=link_cfg.get('keepalive', 60),
                receive_topic=link_cfg.get('mqtt_topic', 'meshtastic.receive'),
            )
            if failures:
                print(f"[{name}] MQTT connection recovered after {failures} failed attempt(s).")
            return iface
        except ValueError as e:
            # Configuration error (missing/unreadable cert file, keyfile
            # without certfile, blank required field) -- retrying can't fix
            # it, and burning the full retry budget on it just buries the
            # message and delays startup. Fail fast and loudly instead.
            print(f"[{name}] MQTT configuration error, not retrying: {e}")
            return None
        except Exception as e:
            failures += 1
            print(f"[{name}] MQTT connect attempt {failures} failed: {type(e).__name__}: {e}")
            if failures >= _MQTT_MAX_CONNECT_FAILURES:
                print(
                    f"[{name}] {failures} consecutive MQTT connect failures; giving up. "
                    "The local BBS continues running without this link."
                )
                return None
            time.sleep(5)


def get_mqtt_interfaces(system_config: dict[str, Any]) -> list:
    """Open every configured [mqttN] link (see discover_mqtt_link_names).

    Returns a list of dicts, one per successfully connected link: the
    link's full config (name, sync_section, allow_section, *_key, plus the
    connection settings) with its live 'interface' added. Carrying the
    connection settings through is what lets server.py record a baseline
    for change detection -- see reload_links_from_config. A link that
    fails to connect at startup is skipped (logged, not fatal), matching
    _open_mqtt_interface's give-up-without-exiting behavior.
    """
    results = []
    for link_cfg in system_config.get('mqtt_links', []):
        iface = _open_mqtt_interface(link_cfg)
        if iface is None:
            continue
        entry = dict(link_cfg)
        entry['interface'] = iface
        results.append(entry)
    return results


def get_mqtt_interface_by_name(system_config: dict[str, Any], name: str):
    """Reconnect helper: rebuild ONE named MQTT link's interface from
    system_config alone (mirrors get_secondary_interface's contract) --
    used as a RadioLink.reconnect_fn closure. Returns None if the link is
    no longer in config.ini (e.g. removed since startup -- matching
    interface2's existing "no longer configured" reconnect behavior) or
    fails to reconnect.

    Re-reads config.ini first (reload_mqtt_links) so reconnecting a link
    actually applies settings edited since startup. Rebuilding from the
    startup snapshot instead would silently ignore the edit while
    reporting success, which is worse than refusing outright."""
    for link_cfg in reload_mqtt_links(system_config):
        if link_cfg['name'] == name:
            return _open_mqtt_interface(link_cfg)
    return None
