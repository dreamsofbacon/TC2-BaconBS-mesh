import configparser
import gc
import os
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


def initialize_config(config_file: str = None) -> dict[str, Any]:
    """
    Function reads and parses system configuration file

    Returns a dict with the following entries:
    config - parsed config file
    interface_type - type of the active interface
    hostname - host name for TCP interface
    port - serial port name for serial interface
    bbs_nodes - list of peer nodes to sync with

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
    hostname = config['interface'].get('hostname', None)
    port = config['interface'].get('port', None)
    tcp_port = config['interface'].getint('tcp_port', fallback=5000)
    baudrate = config['interface'].getint('baudrate', fallback=115200)
    ble_address = config['interface'].get('ble_address', fallback=None)
    ble_pin = config['interface'].get('ble_pin', fallback=None)
    channel_index = config['interface'].getint('channel_index', fallback=0)

    bbs_nodes = config.get('sync', 'bbs_nodes', fallback='').split(',')
    bbs_nodes = [node.strip() for node in bbs_nodes if node.strip()]
    subscriber_nodes = config.get('sync', 'subscriber_nodes', fallback='').split(',')
    subscriber_nodes = [node.strip() for node in subscriber_nodes if node.strip()]

    sync_interval_raw = config.get('sync', 'sync_interval_minutes', fallback='5').strip()
    try:
        sync_interval_minutes = int(sync_interval_raw)
    except ValueError:
        sync_interval_minutes = 5
    sync_interval_minutes = max(1, sync_interval_minutes)

    print(f"Configured to sync with the following BBS nodes: {bbs_nodes}")

    allowed_nodes = config.get('allow_list', 'allowed_nodes', fallback='').split(',')
    allowed_nodes = [node.strip() for node in allowed_nodes if node.strip()]

    print(f"Nodes with Urgent board permissions: {allowed_nodes}")

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
        'mqtt_topic': 'meshtastic.receive'
    }



def get_interface(system_config:dict[str, Any]) -> Any:
    """
    Open the configured Meshtastic or MeshCore radio interface.

    Meshtastic serial/TCP modes return the native library interface. MeshCore
    serial/TCP/BLE modes return ``MeshCoreInterface``, which presents the small
    synchronous compatibility surface used by the BBS.

    Args:
        system_config (dict[str, Any]): A dict with system configuration. See description of initialize_config() for details.

    Raises:
        ValueError: Exception raised in the following cases:
                - Type of interface not provided in the system config
                - Multiple serial ports present in the system, and no port specified in the configuration
                - Serial port interface requested, but no ports found in the system
                - Hostname not provided for TCP interface

    Returns:
        A connected radio interface.
    """
    interface_type = system_config['interface_type']
    valid_types = ('serial', 'tcp', 'meshcore_serial', 'meshcore_tcp', 'meshcore_ble')
    if interface_type not in valid_types:
        raise ValueError("Invalid interface type specified in config file")
    if interface_type in ('tcp', 'meshcore_tcp') and not system_config['hostname']:
        raise ValueError("Hostname must be specified for TCP interface")

    failures = 0
    while True:
        device = None
        try:
            if interface_type == 'serial':
                device = _resolve_serial_device(system_config)  # ValueError here is fatal (config), not retried
                iface = meshtastic.serial_interface.SerialInterface(device)
            elif interface_type == 'tcp':
                iface = meshtastic.tcp_interface.TCPInterface(hostname=system_config['hostname'])
            else:
                from meshcore_interface import MeshCoreInterface

                meshcore_transport = interface_type.removeprefix('meshcore_')
                if meshcore_transport == 'serial':
                    device = _resolve_serial_device(system_config)
                iface = MeshCoreInterface(
                    meshcore_transport,
                    port=device,
                    baudrate=int(system_config.get('baudrate', 115200)),
                    hostname=system_config.get('hostname'),
                    tcp_port=int(system_config.get('tcp_port', 5000)),
                    ble_address=system_config.get('ble_address'),
                    ble_pin=system_config.get('ble_pin'),
                    channel_index=int(system_config.get('channel_index', 0)),
                    receive_topic=system_config.get('mqtt_topic', 'meshtastic.receive'),
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
                    _reset_serial_radio(device or _resolve_serial_device(system_config))
                except Exception:
                    time.sleep(5)
            else:
                time.sleep(5)
