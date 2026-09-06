"""Reading public chatter from a radio.

Three things were wrong with the old menu, and each is a separate class here.

It asked you to type a number from 1 to 168 to mean a time window, which on
a phone keypad is a worse way to say "a week" than pressing one key.

It sent one message per reply, so reading a busy channel meant pressing N
over and over, each round trip costing a LoRa exchange.

And it had no way to narrow by source at all, which on a node bridged to
several others is most of what you want -- the traffic arriving is mostly
somebody else's.

The batching rule worth keeping straight: a reply carries as many entries as
fit a byte ceiling, and the pagination cursor follows what was actually sent,
not what was fetched. Getting that wrong silently skips messages.
"""
import sqlite3
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import command_handlers as ch
import db_operations
import utils

CHAT = "5a582498f3d5f2b91a9ea3bbb21c6f1f2355bc3eca060cfac6a98a5105f69930"
BBS = "hW9UeHYKg+eUfhBDhHNBoT38QCnmlAXebk1OR/l6LGc="
SENDER = 4242


class _Iface:
    nodes = {}
    bbs_nodes = []


class _Radio(unittest.TestCase):
    def setUp(self):
        db_operations.thread_local.connection = sqlite3.connect(":memory:")
        db_operations.initialize_database()
        self.now = datetime.now(timezone.utc)
        self.sent = []
        self._real_send = ch.send_message
        ch.send_message = lambda text, sid, iface: self.sent.append(text)
        self.iface = _Iface()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        ch.send_message = self._real_send
        utils.user_states.pop(SENDER, None)
        conn = getattr(db_operations.thread_local, "connection", None)
        if conn is not None:
            conn.close()
            del db_operations.thread_local.connection

    def add(self, n, network="meshcore", index=2, name="Channel 2",
            sender="brown dog", content="hello", capture=CHAT, hops=1):
        ts = (self.now - timedelta(minutes=n)).isoformat().replace("+00:00", "Z")
        db_operations.add_public_chatter(
            unique_id=f"u{n}", network=network, channel_index=index,
            channel_name=name, sender_node_id=None, sender_name=sender,
            content=content, message_timestamp=ts, captured_at=ts,
            capture_node_id=capture,
            expires_at=(self.now + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
            hops=hops)

    def open_menu(self):
        self.sent.clear()
        ch.handle_public_chatter_command(SENDER, self.iface)
        return self.sent[-1]

    def say(self, text):
        self.sent.clear()
        ch.handle_public_chatter_steps(
            SENDER, text, self.iface, utils.get_user_state(SENDER))
        return self.sent[-1] if self.sent else ""

    def state(self):
        return utils.get_user_state(SENDER) or {}


class TheMenuIsSimplerTests(_Radio):
    def test_the_window_is_picked_not_typed(self):
        """"168" to mean a week is a worse thing to type than one key."""
        menu = self.open_menu()
        self.assertNotIn("1-168", menu)
        for label in ("1h", "3h", "6h", "24h", "3d", "7d"):
            with self.subTest(label=label):
                self.assertIn(label, menu)

    def test_it_offers_the_same_windows_as_the_web_page(self):
        self.assertEqual(
            [hours for hours, _ in ch.CHATTER_WINDOWS], [1, 3, 6, 24, 72, 168])

    def test_the_menu_fits_a_single_packet(self):
        """A menu that arrives in two packets is a menu you read twice."""
        self.assertLessEqual(len(self.open_menu().encode("utf-8")), 220)

    def test_a_number_selects_its_window(self):
        self.add(1)
        self.open_menu()
        self.say("2")
        self.assertEqual(self.state()["hours"], 3)

    def test_an_out_of_range_choice_is_explained(self):
        self.open_menu()
        reply = self.say("9")
        self.assertIn("1-6", reply)
        self.assertEqual(self.state().get("step"), 1)

    def test_zero_still_leaves(self):
        self.open_menu()
        self.say("0")
        self.assertNotEqual(self.state().get("command"), "PUBLIC_CHATTER")


class ManyResultsPerReplyTests(_Radio):
    def test_one_reply_carries_several_entries(self):
        """The old menu sent exactly one, so a busy window meant pressing N
        once per message."""
        for n in range(8):
            self.add(n, content=f"message {n}")
        self.open_menu()
        reply = self.say("4")
        for n in range(ch.CHATTER_BATCH):
            with self.subTest(n=n):
                self.assertIn(f"message {n}", reply)

    def test_a_batch_is_capped_by_bytes_not_only_by_count(self):
        """One very long message must not turn a batch into a broadcast."""
        for n in range(8):
            self.add(n, content=f"mark{n} " + "x" * 400)
        self.open_menu()
        reply = self.say("4")
        shown = sum(1 for n in range(8) if f"mark{n}" in reply)
        self.assertLess(shown, ch.CHATTER_BATCH,
                        "the byte ceiling should have stopped this short")
        self.assertGreaterEqual(shown, 1)

    def test_a_single_oversized_message_still_goes_out(self):
        """Better a long reply than an empty one."""
        self.add(1, content="y" * 1200)
        self.open_menu()
        self.assertIn("yyyy", self.say("4"))

    def _page_through(self, count, limit=40):
        """Walk every page, returning the markers seen in order."""
        seen = []
        page = self.say("4")
        for _ in range(limit):
            for n in range(count):
                if f"mark{n} " in page:
                    seen.append(n)
            if "[M]ore" not in page:
                break
            page = self.say("m")
        return seen

    def test_nothing_is_skipped_when_the_byte_ceiling_trims_a_batch(self):
        """The load-bearing rule. The query fetches a batch, the byte ceiling
        may send fewer, and the cursor must follow what was SENT. Following
        the fetch instead drops every trimmed entry on the floor -- silently,
        because each page still looks full.
        """
        for n in range(8):
            self.add(n, content=f"mark{n} " + "x" * 400)
        self.open_menu()
        seen = self._page_through(8)
        self.assertEqual(sorted(seen), list(range(8)),
                         f"entries were skipped: saw {sorted(seen)}")

    def test_the_ceiling_really_does_trim_here(self):
        """Otherwise the test above proves nothing: with no trimming, a
        cursor that followed the fetch would look correct."""
        for n in range(8):
            self.add(n, content=f"mark{n} " + "x" * 400)
        self.open_menu()
        first = self.say("4")
        shown = sum(1 for n in range(8) if f"mark{n} " in first)
        self.assertLess(shown, ch.CHATTER_BATCH)

    def test_paging_never_repeats_an_entry(self):
        for n in range(10):
            self.add(n, content=f"mark{n} ")
        self.open_menu()
        seen = self._page_through(10)
        self.assertEqual(len(seen), len(set(seen)), f"repeated: {seen}")
        self.assertEqual(sorted(seen), list(range(10)))

    def test_more_is_not_offered_once_everything_is_shown(self):
        self.add(1)
        self.open_menu()
        self.assertNotIn("[M]ore", self.say("4"))

    def test_an_empty_window_says_so_and_keeps_the_controls(self):
        self.open_menu()
        reply = self.say("1")
        self.assertIn("Nothing heard", reply)
        self.assertIn("[T]ime", reply)


class ChoosingSourcesTests(_Radio):
    def seed(self):
        self.add(1, network="meshcore", index=0, name="Public", capture=CHAT,
                 content="mc-public")
        self.add(2, network="meshcore", index=2, name="Channel 2", capture=CHAT,
                 content="mc-two")
        self.add(3, network="meshtastic", index=0, name="LongFast", capture=BBS,
                 content="mt-long")
        self.open_menu()
        self.say("4")

    def options(self, kind=None):
        return [o["value"] for o in self.state().get("filter_options", [])
                if kind is None or o["kind"] == kind]

    def test_the_filter_screen_is_channels_only(self):
        """The node dimension moved out to Node View.

        It was headed "Heard by:" and listed raw capture ids -- which are
        stamped per RADIO, not per node, so on a two-radio node it read as
        two unlabelled 64-character keys meaning "my MeshCore radio" and
        "my Meshtastic radio". With one dimension left there is no sub-header
        to print either."""
        self.seed()
        reply = self.say("f")
        self.assertIn("Channels", reply)
        self.assertNotIn("Heard by:", reply)
        self.assertNotIn(CHAT, reply)
        self.assertNotIn(BBS, reply)

    def test_only_sources_actually_present_are_offered(self):
        """A channel with nothing in it is a filter that can only empty the
        screen."""
        self.seed()
        self.say("f")
        # Selected by kind, not by looking for a slash: a Meshtastic capture
        # id is base64 and contains one.
        self.assertEqual(sorted(self.options("channel")),
                         ["meshcore/0", "meshcore/2", "meshtastic/0"])
        # Every option is a channel now; nothing else may sneak into the list.
        self.assertEqual(sorted(self.options()), sorted(self.options("channel")))

    def test_several_can_be_toggled_in_one_reply(self):
        """Over LoRa, one round trip per choice is the difference between a
        filter and a chore."""
        self.seed()
        self.say("f")
        self.say("1 2")
        self.assertEqual(len(self.state()["channels"]), 2)

    def test_toggling_the_same_number_again_clears_it(self):
        self.seed()
        self.say("f")
        self.say("1")
        self.say("1")
        self.assertEqual(self.state()["channels"], [])

    def test_selected_sources_are_marked(self):
        self.seed()
        self.say("f")
        self.assertIn("[1]*", self.say("1"))

    def test_channels_combine_as_or(self):
        self.seed()
        self.say("f")
        self.say("1 2")
        reply = self.say("d")
        self.assertIn("mc-public", reply)
        self.assertIn("mc-two", reply)
        self.assertNotIn("mt-long", reply)

    def test_a_channel_and_the_lens_combine_as_and(self):
        """Picking a channel and a node that never met must return nothing,
        not the union of the two. Same property as before, one layer up:
        the node half now comes from the session lens."""
        self.seed()
        ch.set_view_scope(SENDER, [BBS])
        self.addCleanup(ch.clear_view_scope, SENDER)
        self.say("f")
        options = self.state()["filter_options"]
        mc_two = next(i for i, o in enumerate(options, 1)
                      if o["value"] == "meshcore/2")
        self.say(str(mc_two))
        reply = self.say("d")
        self.assertIn("Nothing heard", reply)

    def test_the_lens_narrows_the_chatter_feed(self):
        """And the other direction: a lens that matched nothing above must
        still match its own node's traffic, or the test above passes on a
        lens that is simply ignored."""
        self.seed()
        ch.set_view_scope(SENDER, [CHAT])
        self.addCleanup(ch.clear_view_scope, SENDER)
        self.say("f")
        reply = self.say("d")
        self.assertIn("mc-public", reply)
        self.assertNotIn("mt-long", reply)

    def test_selecting_nothing_shows_everything(self):
        """An empty selection is no constraint, not an empty screen."""
        self.seed()
        self.say("f")
        reply = self.say("d")
        for content in ("mc-public", "mc-two", "mt-long"):
            with self.subTest(content=content):
                self.assertIn(content, reply)

    def test_all_clears_the_channel_selection(self):
        self.seed()
        self.say("f")
        self.say("1")
        self.say("a")
        self.assertEqual(self.state()["channels"], [])
        self.assertNotIn("nodes", self.state())

    def test_all_leaves_the_session_lens_alone(self):
        """[A]ll on a channel screen means all channels. Silently widening
        the whole session from here would be a different promise than the
        key makes, and the user would have no idea it had happened."""
        self.seed()
        ch.set_view_scope(SENDER, [CHAT])
        self.addCleanup(ch.clear_view_scope, SENDER)
        self.say("f")
        self.say("1")
        self.say("a")
        self.assertEqual(ch.get_view_scope(SENDER), (CHAT,))

    def test_an_active_filter_is_visible_from_the_results(self):
        """Otherwise a short list reads as a quiet mesh."""
        self.seed()
        self.say("f")
        self.say("1")
        self.assertIn("[F]ilter*", self.say("d"))

    def test_no_filter_shows_a_plain_control(self):
        """The star means "narrowed"; with nothing selected there must not
        be one, or every screen would look filtered."""
        self.seed()
        self.say("t")
        reply = self.say("4")
        self.assertIn("[F]ilter", reply)
        self.assertNotIn("[F]ilter*", reply)

    def test_a_bad_number_does_not_change_the_selection(self):
        self.seed()
        self.say("f")
        self.say("99")
        self.assertEqual(self.state()["channels"], [])

    def test_changing_the_window_keeps_the_selection(self):
        """The sources you picked are still the sources you want."""
        self.seed()
        self.say("f")
        self.say("1")
        chosen = list(self.state()["channels"])
        self.say("t")
        self.say("5")
        self.assertEqual(self.state()["channels"], chosen)


class ReadabilityTests(_Radio):
    def test_the_two_networks_are_distinguishable(self):
        """'meshcore' and 'meshtastic' both truncate to 'me', which would
        make channel 2 on one look like channel 2 on the other."""
        self.assertNotEqual(ch._network_tag("meshcore"),
                            ch._network_tag("meshtastic"))
        self.assertEqual(ch._network_tag("meshcore"), "MC")
        self.assertEqual(ch._network_tag("meshtastic"), "MT")

    def test_an_unknown_network_still_gets_a_tag(self):
        self.assertEqual(ch._network_tag("weird"), "WE")
        self.assertEqual(ch._network_tag(""), "??")

    def test_a_capture_id_is_shortened(self):
        """64 hex characters do not fit a line."""
        self.assertLessEqual(len(ch._short_node(CHAT)), 14)
        self.assertTrue(ch._short_node(CHAT).startswith("5a582498"))

    def test_a_short_id_is_left_alone(self):
        self.assertEqual(ch._short_node("!1bbecf78"), "!1bbecf78")

    def test_hops_are_shown_and_direct_is_not_zero(self):
        self.add(1, hops=0)
        self.add(2, hops=3, content="three")
        self.open_menu()
        reply = self.say("4")
        self.assertIn("direct", reply)
        self.assertIn("3h", reply)

    def test_unknown_hops_are_simply_absent(self):
        self.add(1, hops=None, content="quiet")
        self.open_menu()
        self.assertNotIn("None", self.say("4"))


class QueryLayerTests(_Radio):
    """The plural filters the menu is built on."""

    def seed(self):
        self.add(1, network="meshcore", index=0, name="Public", capture=CHAT)
        self.add(2, network="meshcore", index=2, name="Channel 2", capture=CHAT)
        self.add(3, network="meshtastic", index=0, name="LongFast", capture=BBS)

    def fetch(self, **kwargs):
        return db_operations.get_public_chatter_history(hours=24, **kwargs)["entries"]

    def test_several_channels_are_returned(self):
        self.seed()
        self.assertEqual(
            len(self.fetch(channel_keys=["meshcore/0", "meshcore/2"])), 2)

    def test_a_channel_key_is_network_qualified(self):
        """meshcore/0 must not drag in meshtastic/0."""
        self.seed()
        entries = self.fetch(channel_keys=["meshcore/0"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["network"], "meshcore")

    def test_several_capture_nodes_are_returned(self):
        self.seed()
        self.assertEqual(len(self.fetch(capture_node_ids=[CHAT, BBS])), 3)

    def test_the_two_filters_intersect(self):
        self.seed()
        self.assertEqual(
            self.fetch(channel_keys=["meshtastic/0"], capture_node_ids=[CHAT]), [])

    def test_an_empty_list_is_no_constraint(self):
        self.seed()
        self.assertEqual(len(self.fetch(channel_keys=[], capture_node_ids=[])), 3)

    def test_a_malformed_key_matches_nothing_rather_than_everything(self):
        """A filter that silently does the opposite of what was asked is
        worse than one that returns nothing."""
        self.seed()
        self.assertEqual(self.fetch(channel_keys=["nonsense"]), [])

    def test_the_singular_filters_still_work(self):
        """The web feed and existing callers pass these."""
        self.seed()
        self.assertEqual(len(self.fetch(network="meshtastic")), 1)
        self.assertEqual(len(self.fetch(capture_node_id=BBS)), 1)


if __name__ == "__main__":
    unittest.main()
