"""Who sent a MeshCore public message.

MeshCore channel messages carry no sender identity at all -- a channel is
encrypted with a shared key and the frame has no pubkey field, unlike a
MeshCore direct message. So the feed showing "Unknown sender" was accurate,
not broken, and the roster join for a long name could never fire because
there was no node id to join on.

The only sender information that exists is the "Name: " prefix clients write
into the body by convention. Parsing it is worthwhile but must stay
conservative: attributing a message to somebody who never sent it is worse
than showing nothing.

Every sample below is real traffic captured off the air.
"""
import sys
import types
import unittest
from datetime import datetime, timezone

if "meshtastic" not in sys.modules:
    sys.modules["meshtastic"] = types.SimpleNamespace(BROADCAST_NUM=0)
elif not hasattr(sys.modules["meshtastic"], "BROADCAST_NUM"):
    sys.modules["meshtastic"].BROADCAST_NUM = 0

import public_chatter
from public_chatter import split_meshcore_sender as split


class RealTrafficTests(unittest.TestCase):
    def test_a_plain_name_and_message(self):
        self.assertEqual(split("brown dog: Test"), ("brown dog", "Test"))

    def test_a_name_with_a_space(self):
        """'brown dog' is one sender, not a word and a sender."""
        name, _ = split("brown dog: @[Digital_Elite] gotcha in roanoke va")
        self.assertEqual(name, "brown dog")

    def test_an_emoji_only_name(self):
        """A telemetry beacon whose whole name is one emoji, and whose body
        is a colon-separated location payload."""
        self.assertEqual(
            split("\U0001F50B: MM:YhW9uyPnvQ:36.23537,-86.71230"),
            ("\U0001F50B", "MM:YhW9uyPnvQ:36.23537,-86.71230"))

    def test_a_name_ending_in_an_emoji(self):
        name, body = split(
            "N4NOV \U0001F3E0: \U0001F916 @[KQ4KZW M2] 5 hops to LYH VA")
        self.assertEqual(name, "N4NOV \U0001F3E0")
        self.assertTrue(body.startswith("\U0001F916"))

    def test_a_callsign_name(self):
        self.assertEqual(split("AZ229: @[KF4O] Thumbs up!"),
                         ("AZ229", "@[KF4O] Thumbs up!"))


class TheDelimiterTests(unittest.TestCase):
    """Colon-space, first occurrence. Every other rule loses to real data."""

    def test_only_the_first_colon_space_splits(self):
        """A rightmost split would make the sender 'Route'."""
        name, body = split(
            "N4NOV: 5 hops to LYH VA @ 18:28 \nRoute: c9b4f2→c07733")
        self.assertEqual(name, "N4NOV")
        self.assertIn("Route: c9b4f2", body)

    def test_a_colon_without_a_space_is_not_a_delimiter(self):
        """'MM:YhW9uyPnvQ' is a payload, not a sender named MM."""
        self.assertEqual(split("MM:YhW9uyPnvQ:36.2,-86.7"),
                         ("", "MM:YhW9uyPnvQ:36.2,-86.7"))

    def test_a_message_with_no_prefix_is_left_alone(self):
        self.assertEqual(split("just a message"), ("", "just a message"))

    def test_a_timestamp_alone_does_not_create_a_sender(self):
        self.assertEqual(split("meeting at 18:30"), ("", "meeting at 18:30"))


class RefusingToGuessTests(unittest.TestCase):
    """Each of these would attribute a message to someone who never sent it."""

    def test_a_multi_line_head_is_not_a_name(self):
        self.assertEqual(split("line one\nline two: body"),
                         ("", "line one\nline two: body"))

    def test_an_overlong_head_is_not_a_name(self):
        text = "x" * 40 + ": body"
        self.assertEqual(split(text), ("", text))

    def test_a_name_at_the_length_limit_is_still_accepted(self):
        name = "n" * public_chatter._MESHCORE_SENDER_MAX
        self.assertEqual(split(name + ": body"), (name, "body"))

    def test_an_empty_name_is_refused(self):
        self.assertEqual(split(": body"), ("", ": body"))

    def test_an_empty_body_is_refused(self):
        """'Roger:' with nothing after it is a message, not an attribution."""
        self.assertEqual(split("Roger: "), ("", "Roger: "))

    def test_an_empty_string_is_safe(self):
        self.assertEqual(split(""), ("", ""))


class _Interface:
    protocol_name = "meshcore"
    public_chatter_channels = [0, 2, 3, 4]
    public_chatter_capture_node_id = "!capture"


class NormalizationTests(unittest.TestCase):
    """End to end through the capture path."""

    def observe(self, text, **extra):
        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP",
                        "payload": text.encode("utf-8")},
            "to": 0,
            "channel_index": 2,
            "channel_name": "Channel 2",
        }
        packet.update(extra)
        return public_chatter.normalize_broadcast(packet, _Interface())

    def test_the_sender_name_is_lifted_out_of_the_body(self):
        observation = self.observe("brown dog: Test")
        self.assertEqual(observation["sender_name"], "brown dog")
        self.assertEqual(observation["content"], "Test")

    def test_there_is_still_no_node_id_to_report(self):
        """The protocol does not carry one. Inventing one would be worse
        than an empty field, because mail is authorized by node id."""
        self.assertIsNone(self.observe("brown dog: Test")["sender_node_id"])

    def test_hops_come_from_the_path_length(self):
        self.assertEqual(self.observe("brown dog: Test", path_len=3)["hops"], 3)

    def test_the_id_is_derived_from_the_body_as_it_arrived(self):
        """Stripping the prefix before hashing would make a node running this
        code and one running the old code disagree about which packet they
        are looking at, and every MeshCore message would sync twice.

        A packet with no native id hashes its content, so this is the case
        where it actually bites.
        """
        sent_at = 1788451200          # fixed, so both sides agree on time
        observation = self.observe("brown dog: Test", sender_timestamp=sent_at)

        # Exactly what the old code -- which never parsed a prefix -- hashed.
        expected = public_chatter.make_message_id(
            "meshcore", 2, None, None,
            datetime.fromtimestamp(sent_at, timezone.utc),
            "brown dog: Test")

        self.assertEqual(observation["unique_id"], expected)
        # ...while the stored body is still the stripped one.
        self.assertEqual(observation["content"], "Test")

    def test_an_unparsed_message_keeps_its_body(self):
        observation = self.observe("just a message")
        self.assertEqual(observation["sender_name"], "")
        self.assertEqual(observation["content"], "just a message")


class MeshtasticIsUntouchedTests(unittest.TestCase):
    """Meshtastic carries real sender identity, so this parse must never run
    there -- a message legitimately beginning 'Warning: ...' would otherwise
    acquire a sender called Warning."""

    class _Meshtastic:
        protocol_name = "Meshtastic"
        public_chatter_channels = [0]
        public_chatter_capture_node_id = "!capture"

    def test_a_meshtastic_body_is_never_split(self):
        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP",
                        "payload": b"Warning: check the repeater"},
            "to": 0xFFFFFFFF,
            "channel": 0,
            "fromId": "!1bbecf78",
        }
        observation = public_chatter.normalize_broadcast(
            packet, self._Meshtastic())
        self.assertEqual(observation["content"], "Warning: check the repeater")
        self.assertEqual(observation["sender_name"], "")

    def test_a_meshcore_message_with_a_real_sender_is_not_split(self):
        """A MeshCore direct message does carry a pubkey, so if one ever
        reaches this path its identity wins over the convention."""
        packet = {
            "decoded": {"portnum": "TEXT_MESSAGE_APP",
                        "payload": b"Warning: check the repeater"},
            "to": 0,
            "channel_index": 2,
            "fromId": "abc123",
        }
        observation = public_chatter.normalize_broadcast(packet, _Interface())
        self.assertEqual(observation["content"], "Warning: check the repeater")


if __name__ == "__main__":
    unittest.main()
