"""Real channel names in the chatter feed.

"Channel 2" identifies nothing. Both radios know what their channels are
actually called, and neither was being asked: MeshCore synthesised a label
from the index, and Meshtastic packets carry no name at all so everything but
the primary fell back to a number.

The names are read once at connect, not per packet -- they change only when
someone reconfigures the radio.

Configuration is untouched: channels are still selected by index in
config.ini, because an index is what the radio addresses and a name can be
edited out from under it.
"""
import re
import sqlite3
import sys
import types
import unittest
from types import SimpleNamespace

# server.py reaches config_init, which imports the real radio libraries.
# radio_stubs installs them additively so this file agrees with whatever
# another test module already put in sys.modules.
import radio_stubs
radio_stubs.install()

import db_operations
import public_chatter
import server


class _Interface:
    protocol_name = "meshcore"
    public_chatter_channels = [0, 2, 3, 4]
    public_chatter_capture_node_id = "!capture"

    def __init__(self, channel_names=None):
        if channel_names is not None:
            self.channel_names = channel_names


def observe(interface, text="hello", channel_index=2, **extra):
    packet = {
        "decoded": {"portnum": "TEXT_MESSAGE_APP",
                    "payload": text.encode("utf-8")},
        "to": 0,
        "channel_index": channel_index,
    }
    packet.update(extra)
    return public_chatter.normalize_broadcast(packet, interface)


class CaptureUsesTheRealNameTests(unittest.TestCase):
    def test_a_name_on_the_packet_wins(self):
        """MeshCore stamps the name it read at connect onto each packet."""
        observation = observe(_Interface(), channel_name="Roanoke VA")
        self.assertEqual(observation["channel_name"], "Roanoke VA")

    def test_the_interface_table_is_used_when_the_packet_has_none(self):
        """Meshtastic packets carry no channel name, so the table server.py
        built from the local node's channel config answers instead."""
        observation = observe(_Interface({2: "Roanoke VA"}))
        self.assertEqual(observation["channel_name"], "Roanoke VA")

    def test_an_unnamed_channel_still_falls_back_to_its_number(self):
        """Better a number than a blank label."""
        observation = observe(_Interface({3: "Other"}), channel_index=2)
        self.assertEqual(observation["channel_name"], "")

    def test_the_primary_channel_keeps_its_conventional_name(self):
        self.assertEqual(
            observe(_Interface(), channel_index=0)["channel_name"], "Public")

    def test_a_real_name_replaces_the_conventional_one(self):
        self.assertEqual(
            observe(_Interface({0: "Home"}), channel_index=0)["channel_name"],
            "Home")

    def test_an_interface_with_no_table_at_all_still_works(self):
        """Every interface predates this attribute; none may crash without it."""
        observation = observe(_Interface(), channel_index=2)
        self.assertIsNotNone(observation)


class PlaceholderTests(unittest.TestCase):
    """The writer and the backfill must agree on what counts as a stand-in,
    or real names get overwritten or placeholders never get replaced."""

    def test_a_bare_number_is_a_placeholder(self):
        self.assertIn("Channel 2", db_operations.channel_name_placeholders(2))

    def test_an_empty_label_is_a_placeholder(self):
        self.assertIn("", db_operations.channel_name_placeholders(2))

    def test_both_transports_defaults_for_the_primary_are_placeholders(self):
        primary = db_operations.channel_name_placeholders(0)
        self.assertIn("Public", primary)
        self.assertIn("LongFast", primary)

    def test_those_defaults_are_not_placeholders_on_other_channels(self):
        """A channel someone actually named "Public" must keep that name."""
        self.assertNotIn("Public", db_operations.channel_name_placeholders(2))

    def test_the_capture_path_only_ever_writes_known_placeholders(self):
        """If the writer invents a stand-in the backfill does not know about,
        those rows are stranded with a label nothing will ever correct."""
        for index, network in ((0, "meshcore"), (0, "meshtastic")):
            interface = _Interface()
            interface.protocol_name = network
            written = observe(interface, channel_index=index)["channel_name"]
            with self.subTest(network=network):
                self.assertIn(written,
                              db_operations.channel_name_placeholders(index))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.addCleanup(self._close)

    def _close(self):
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def add(self, unique_id, channel_index, channel_name, network="meshcore"):
        db_operations.add_public_chatter(
            unique_id=unique_id, network=network, channel_index=channel_index,
            channel_name=channel_name, sender_node_id=None, sender_name="",
            content="hello", message_timestamp="2026-09-02T14:00:00Z",
            captured_at="2026-09-02T14:00:00Z", capture_node_id="!c",
            expires_at="2026-09-09T14:00:00Z", hops=None)

    def names(self):
        return {row[0] for row in db_operations.thread_local.connection.execute(
            "SELECT channel_name FROM public_chatter")}

    def test_a_numbered_label_is_replaced(self):
        self.add("u1", 2, "Channel 2")
        self.assertEqual(
            db_operations.backfill_channel_names("meshcore", {2: "Roanoke VA"}), 1)
        self.assertEqual(self.names(), {"Roanoke VA"})

    def test_an_empty_label_is_replaced(self):
        self.add("u1", 2, "")
        db_operations.backfill_channel_names("meshcore", {2: "Roanoke VA"})
        self.assertEqual(self.names(), {"Roanoke VA"})

    def test_a_real_name_is_left_alone(self):
        """A channel renamed mid-week keeps its history honest rather than
        being retconned to the new name."""
        self.add("u1", 2, "Old Name")
        self.assertEqual(
            db_operations.backfill_channel_names("meshcore", {2: "New Name"}), 0)
        self.assertEqual(self.names(), {"Old Name"})

    def test_another_network_is_not_touched(self):
        """meshcore channel 2 and meshtastic channel 2 are unrelated."""
        self.add("u1", 2, "Channel 2", network="meshtastic")
        db_operations.backfill_channel_names("meshcore", {2: "Roanoke VA"})
        self.assertEqual(self.names(), {"Channel 2"})

    def test_another_index_is_not_touched(self):
        self.add("u1", 3, "Channel 3")
        db_operations.backfill_channel_names("meshcore", {2: "Roanoke VA"})
        self.assertEqual(self.names(), {"Channel 3"})

    def test_the_primary_conventional_names_are_replaced(self):
        self.add("u1", 0, "Public")
        db_operations.backfill_channel_names("meshcore", {0: "Home"})
        self.assertEqual(self.names(), {"Home"})

    def test_several_channels_in_one_pass(self):
        self.add("u1", 2, "Channel 2")
        self.add("u2", 3, "Channel 3")
        self.assertEqual(
            db_operations.backfill_channel_names(
                "meshcore", {2: "Two", 3: "Three"}), 2)
        self.assertEqual(self.names(), {"Two", "Three"})

    def test_nothing_to_do_is_not_an_error(self):
        self.assertEqual(db_operations.backfill_channel_names("meshcore", {}), 0)
        self.assertEqual(db_operations.backfill_channel_names("meshcore", None), 0)

    def test_a_blank_name_is_never_written(self):
        """An unnamed slot must not blank out a row's existing label."""
        self.add("u1", 2, "Channel 2")
        db_operations.backfill_channel_names("meshcore", {2: "   "})
        self.assertEqual(self.names(), {"Channel 2"})

    def test_the_filter_list_stops_showing_duplicates(self):
        """DISTINCT includes the name, so one channel under two labels
        appeared twice in the page's channel dropdown."""
        self.add("u1", 2, "Channel 2")
        self.add("u2", 2, "Roanoke VA")
        db_operations.backfill_channel_names("meshcore", {2: "Roanoke VA"})
        channels = db_operations.get_public_chatter_filters()["channels"]
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["channel_name"], "Roanoke VA")


class MeshtasticChannelTableTests(unittest.TestCase):
    """server.py reads these off the local node rather than asking the air."""

    def channel(self, index, name, role=1):
        return SimpleNamespace(
            index=index, role=role, settings=SimpleNamespace(name=name))

    def node(self, channels, modem_preset=None):
        local = SimpleNamespace(channels=channels)
        if modem_preset is not None:
            local.localConfig = SimpleNamespace(
                lora=SimpleNamespace(modem_preset=modem_preset))
        return SimpleNamespace(localNode=local)

    def test_named_channels_are_read(self):
        names = server._meshtastic_channel_names(
            self.node([self.channel(0, "Home"), self.channel(2, "Roanoke")]))
        self.assertEqual(names, {0: "Home", 2: "Roanoke"})

    def test_a_disabled_slot_is_skipped(self):
        """role 0 is an empty slot, not a channel that lacks a name."""
        names = server._meshtastic_channel_names(
            self.node([self.channel(0, "Home"), self.channel(1, "", role=0)]))
        self.assertEqual(names, {0: "Home"})

    def test_an_unnamed_primary_falls_back_to_the_modem_preset(self):
        """Meshtastic leaves the default primary's name blank and clients
        show the preset in its place, so the feed should say what they say."""
        names = server._meshtastic_channel_names(
            self.node([self.channel(0, "")], modem_preset=3))
        self.assertEqual(names, {0: "MediumSlow"})

    def test_an_unnamed_primary_with_no_preset_says_longfast(self):
        names = server._meshtastic_channel_names(self.node([self.channel(0, "")]))
        self.assertEqual(names, {0: "LongFast"})

    def test_an_unnamed_secondary_is_left_out(self):
        """It has no conventional name, so its number is the honest label."""
        names = server._meshtastic_channel_names(
            self.node([self.channel(2, "")]))
        self.assertEqual(names, {})

    def test_a_radio_with_no_channel_table_yields_nothing(self):
        self.assertEqual(
            server._meshtastic_channel_names(SimpleNamespace()), {})
        self.assertEqual(
            server._meshtastic_channel_names(
                SimpleNamespace(localNode=SimpleNamespace(channels=None))), {})

    def test_a_malformed_entry_does_not_lose_the_good_ones(self):
        names = server._meshtastic_channel_names(
            self.node([SimpleNamespace(), self.channel(2, "Roanoke")]))
        self.assertEqual(names, {2: "Roanoke"})

    def test_meshcore_names_are_not_overwritten(self):
        """MeshCore reads its own from the radio at connect; re-deriving them
        from a Meshtastic channel table it does not have would erase them."""
        interface = SimpleNamespace(
            channel_names={2: "Roanoke VA"}, protocol_name="meshcore")
        server._attach_channel_names(interface)
        self.assertEqual(interface.channel_names, {2: "Roanoke VA"})


class MeshCoreQueriesTheRadioTests(unittest.TestCase):
    def source(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parent.parent
                / "meshcore_interface.py").read_text(encoding="utf-8")

    def test_it_asks_the_radio_for_names(self):
        self.assertIn("get_channel(index)", self.source())

    def handler_body(self):
        """The body of _on_channel_message alone, up to the next method.

        Sliced rather than searched for in the whole file: the connect path
        legitimately calls get_channel, so asserting over the file would
        always pass and prove nothing.
        """
        source = self.source()
        start = source.index("    async def _on_channel_message(")
        body = []
        for line in source[start:].splitlines()[1:]:
            if re.match(r"    (async )?def ", line):
                break
            body.append(line)
        return "\n".join(body)

    def test_it_asks_once_at_connect_not_per_message(self):
        """A round trip per captured packet would be absurd, so the handler
        must read the cached table rather than query the radio."""
        self.assertIn("await self._refresh_channel_names()", self.source())
        self.assertNotIn("get_channel", self.handler_body())
        self.assertIn("self.channel_names.get(", self.handler_body())

    def test_an_unreadable_slot_does_not_stop_the_others(self):
        self.assertRegex(
            self.source(),
            r"for index in range\(MAX_CHANNELS\):[\s\S]*?except Exception:[\s\S]*?continue")

    def test_the_stored_name_is_used_when_capturing(self):
        self.assertIn("self.channel_names.get(", self.source())


class ConfigurationStillUsesNumbersTests(unittest.TestCase):
    """Selecting which channels to capture stays index-based: an index is
    what the radio addresses, and a name can be edited out from under it."""

    def test_the_capture_allow_list_is_still_numeric(self):
        self.assertIn(
            "allowed = {int(value) for value in getattr(interface, "
            "'public_chatter_channels', [])}",
            (__import__("pathlib").Path(public_chatter.__file__)
             .read_text(encoding="utf-8")))

    def test_a_channel_is_still_matched_by_index(self):
        interface = _Interface({2: "Roanoke VA"})
        interface.public_chatter_channels = [2]
        self.assertIsNotNone(observe(interface, channel_index=2))
        self.assertIsNone(observe(interface, channel_index=3))


if __name__ == "__main__":
    unittest.main()
