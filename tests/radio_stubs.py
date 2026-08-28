"""Shared stand-ins for the meshtastic and serial libraries.

Several test modules need these, and each one used to build its own by
assigning straight into sys.modules. Whichever imported last won, and it
replaced the earlier module object wholesale -- so a module that had already
been imported against the first stub (config_init holds its own reference to
`meshtastic`) was left pointing at a different object than the tests were
patching. That showed up as an exception class not being caught, and as
`cannot import name 'BROADCAST_NUM'` in an unrelated file, both only when
two particular test files ran in the same session.

Installing is therefore additive: an existing module is reused and only the
missing attributes are filled in, so every importer ends up with the same
object no matter the order.
"""
import sys
import types
from unittest.mock import MagicMock


class MeshInterfaceError(Exception):
    """Stands in for meshtastic's own. Read it back from the installed module
    via ``mesh_interface_error()`` rather than raising this class directly:
    if another module got there first, its class is the one config_init will
    catch."""


def _module(name):
    existing = sys.modules.get(name)
    if isinstance(existing, types.ModuleType):
        return existing
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install():
    """Ensure both libraries are importable, without clobbering what is there."""
    mesh = _module("meshtastic")
    for sub in ("mesh_interface", "stream_interface", "serial_interface", "tcp_interface"):
        setattr(mesh, sub, _module(f"meshtastic.{sub}"))

    if not hasattr(mesh, "BROADCAST_NUM"):
        mesh.BROADCAST_NUM = 0
    if not hasattr(mesh.stream_interface, "StreamInterface"):
        mesh.stream_interface.StreamInterface = object
    if not hasattr(mesh.mesh_interface, "MeshInterface"):
        mesh.mesh_interface.MeshInterface = types.SimpleNamespace(
            MeshInterfaceError=MeshInterfaceError)

    serial = _module("serial")
    if not hasattr(serial, "Serial"):
        serial.Serial = MagicMock(name="Serial")
    serial.tools = _module("serial.tools")
    serial.tools.list_ports = _module("serial.tools.list_ports")
    if not hasattr(serial.tools.list_ports, "comports"):
        serial.tools.list_ports.comports = lambda: []
    return mesh, serial


def mesh_interface_error():
    """The error class config_init will actually catch."""
    return sys.modules["meshtastic"].mesh_interface.MeshInterface.MeshInterfaceError
