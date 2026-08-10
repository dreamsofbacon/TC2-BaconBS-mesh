import configparser
import gc
import os
import re
import time
from typing import Any
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
# access is required. After too many failures we exit for the supervisor
# (systemd / wrapper) to restart the whole process from a clean slate.
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


def _resolve_serial_device(system_config: dict) -> str:
    """Return the concrete serial device path, auto-detecting when not set."""
    if system_config.get('port'):
        return system_config['port']
    ports = list(serial.tools.list_ports.comports())
    if len(ports) == 1:
        return ports[0].device
    if len(ports) > 1:
        port_list = ', '.join(p.device for p in ports)
        raise ValueError(f"Multiple serial ports detected: {port_list}. Specify one with the 'port' argument.")
    raise ValueError("No serial ports detected.")


def init_cli_parser() -> argparse.Namespace:
    """Function build the CLI parser and parses the arguments.

    Returns:
        argparse.ArgumentParser: Argparse namespace with processed CLI args
    """
    parser = argparse.ArgumentParser(description="BaconBS mesh radio BBS")
    
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
    """Read connection settings for one [mqttN] section."""
    return {
        'host': section.get('host', fallback=None),
        'port': section.getint('port', fallback=1883),
        'username': section.get('username', fallback=None),
        'password': section.get('password', fallback=None),
        'tls': section.getboolean('tls', fallback=False),
        'topic_prefix': section.get('topic_prefix', fallback=None),
        'local_id': section.get('local_id', fallback=None),
        'client_id': section.get('client_id', fallback=None),
        'keepalive': section.getint('keepalive', fallback=60),
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

    interface_type = config['interface']['type'].strip().lower()
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
    mqtt_links: list = []
    for link_name in discover_mqtt_link_names(config):
        settings = _read_mqtt_settings(config[link_name])
        if not settings['host'] or not settings['topic_prefix']:
            print(f"[{link_name}] Skipping MQTT link: 'host' and 'topic_prefix' are both required")
            continue
        local_id = settings['local_id'] or f"{link_name}-node"
        mqtt_links.append({
            'name': link_name,
            'host': settings['host'],
            'port': settings['port'],
            'username': settings['username'],
            'password': settings['password'],
            'tls': settings['tls'],
            'topic_prefix': settings['topic_prefix'],
            'local_id': local_id,
            'client_id': settings['client_id'],
            'keepalive': settings['keepalive'],
            'sync_section': f'sync_{link_name}',
            'allow_section': f'allow_list_{link_name}',
            'bbs_nodes_key': f'bbs_nodes_{link_name}',
            'allowed_nodes_key': f'allowed_nodes_{link_name}',
            'subscriber_nodes_key': f'subscriber_nodes_{link_name}',
        })
        print(f"MQTT link '{link_name}' configured: {settings['host']}:{settings['port']} "
              f"topic_prefix={settings['topic_prefix']}")

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
                - Serial port interface requested, but no ports found in the system
                - Hostname not provided for TCP interface

    Returns:
        A connected radio interface.
    """
    interface_type = cfg['interface_type']
    valid_types = ('serial', 'tcp', 'meshcore_serial', 'meshcore_tcp', 'meshcore_ble')
    if interface_type not in valid_types:
        raise ValueError("Invalid interface type specified in config file")
    if interface_type in ('tcp', 'meshcore_tcp') and not cfg.get('hostname'):
        raise ValueError("Hostname must be specified for TCP interface")

    failures = 0
    while True:
        device = None
        try:
            if interface_type == 'serial':
                device = _resolve_serial_device(cfg)  # ValueError here is fatal (config), not retried
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
        except (PermissionError, meshtastic.mesh_interface.MeshInterface.MeshInterfaceError,
                ConnectionResetError, ConnectionRefusedError, OSError) as e:
            failures += 1
            print(f"Radio connect attempt {failures} failed: {type(e).__name__}: {e}")
            # Force release of any serial handle left open by the failed attempt
            # (meshtastic may not close pyserial on handshake failure → the next
            # attempt would hit 'could not exclusively lock port' forever).
            gc.collect()
            if failures >= _MAX_CONNECT_FAILURES_BEFORE_EXIT:
                print(f"{failures} consecutive connect failures; exiting (rc=2) for supervised restart.")
                os._exit(2)
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
                client_id=link_cfg.get('client_id'),
                link_name=name,
                keepalive=link_cfg.get('keepalive', 60),
                receive_topic=link_cfg.get('mqtt_topic', 'meshtastic.receive'),
            )
            if failures:
                print(f"[{name}] MQTT connection recovered after {failures} failed attempt(s).")
            return iface
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

    Returns a list of dicts, one per successfully connected link, each
    carrying everything server.py needs to build a RadioLink: 'name',
    'interface', 'sync_section', 'allow_section', 'bbs_nodes_key',
    'allowed_nodes_key', 'subscriber_nodes_key'. A link that fails to
    connect at startup is skipped (logged, not fatal), matching
    _open_mqtt_interface's give-up-without-exiting behavior.
    """
    results = []
    for link_cfg in system_config.get('mqtt_links', []):
        iface = _open_mqtt_interface(link_cfg)
        if iface is None:
            continue
        results.append({
            'name': link_cfg['name'],
            'interface': iface,
            'sync_section': link_cfg['sync_section'],
            'allow_section': link_cfg['allow_section'],
            'bbs_nodes_key': link_cfg['bbs_nodes_key'],
            'allowed_nodes_key': link_cfg['allowed_nodes_key'],
            'subscriber_nodes_key': link_cfg['subscriber_nodes_key'],
        })
    return results


def get_mqtt_interface_by_name(system_config: dict[str, Any], name: str):
    """Reconnect helper: rebuild ONE named MQTT link's interface from
    system_config alone (mirrors get_secondary_interface's contract) --
    used as a RadioLink.reconnect_fn closure. Returns None if the link is
    no longer in system_config['mqtt_links'] (e.g. removed from
    config.ini since startup -- matching interface2's existing
    "no longer configured" reconnect behavior) or fails to reconnect."""
    for link_cfg in system_config.get('mqtt_links', []):
        if link_cfg['name'] == name:
            return _open_mqtt_interface(link_cfg)
    return None
