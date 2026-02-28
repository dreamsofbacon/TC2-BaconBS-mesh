import os
import shutil
import sys
import types
from pathlib import Path


def ensure_stub_meshtastic() -> None:
    if "meshtastic" not in sys.modules:
        meshtastic_stub = types.ModuleType("meshtastic")
        meshtastic_stub.BROADCAST_NUM = 0
        sys.modules["meshtastic"] = meshtastic_stub


def run_smoke_tests() -> bool:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    created_config = False
    config_path = root / "config.ini"

    if not config_path.exists():
        shutil.copy(root / "example_config.ini", config_path)
        created_config = True

    ensure_stub_meshtastic()

    import message_processing as mp
    import command_handlers as ch

    class FakeMyInfo:
        my_node_num = 123

    class FakeInterface:
        def __init__(self):
            self.bbs_nodes = ["!peer"]
            self.allowed_nodes = []
            self.myInfo = FakeMyInfo()
            self.nodes = {
                "!self": {
                    "num": 123,
                    "user": {
                        "shortName": "ME",
                        "longName": "MyNode",
                        "hwModel": "X",
                        "role": "Client",
                    },
                    "lastHeard": 1,
                    "deviceMetrics": {"batteryLevel": 50},
                }
            }

    interface = FakeInterface()
    results: list[tuple[str, bool]] = []

    try:
        calls: list[str] = []
        mp.add_bulletin = lambda *a, **k: calls.append("add_bulletin")
        mp.add_mail = lambda *a, **k: calls.append("add_mail")
        mp.delete_bulletin = lambda *a, **k: calls.append("delete_bulletin")
        mp.delete_mail = lambda *a, **k: calls.append("delete_mail")
        mp.add_channel = lambda *a, **k: calls.append("add_channel")
        mp.send_message = lambda *a, **k: None
        mp.get_recipient_id_by_mail = lambda uid: "!self"

        malformed_sync_cases = [
            "BULLETIN|bad",
            "MAIL|bad",
            "DELETE_BULLETIN|",
            "DELETE_MAIL|",
            "CHANNEL|bad",
        ]

        try:
            for message in malformed_sync_cases:
                mp.process_message(123, message, interface, is_sync_message=True)
            results.append(("sync malformed", True))
        except Exception:
            results.append(("sync malformed", False))

        try:
            mp.process_message(123, "CHANNEL|name|url", interface, is_sync_message=True)
            results.append(("sync channel valid", "add_channel" in calls))
        except Exception:
            results.append(("sync channel valid", False))

        sent: list[str] = []
        ch.send_message = lambda message, destination, iface: sent.append(message)
        ch.update_user_state = lambda *a, **k: None
        ch.handle_bulletin_command = lambda sender_id, iface: sent.append("BULLETIN_MENU_CALLED")
        ch.handle_channel_directory_command = lambda sender_id, iface: sent.append("CHANNEL_MENU_CALLED")

        try:
            ch.handle_bb_steps(123, "abc", 1, {}, interface, [])
            results.append(("bb invalid", any("Invalid board selection" in msg for msg in sent)))
        except Exception:
            results.append(("bb invalid", False))

        try:
            ch.handle_mail_steps(123, "bad", 2, {}, interface, [])
            results.append(("mail invalid", any("Invalid message number" in msg for msg in sent)))
        except Exception:
            results.append(("mail invalid", False))

        try:
            ch.handle_mail_steps(123, "99", 6, {"nodes": [{"num": 1, "longName": "A"}]}, interface, [])
            results.append(("mail out of range", any("Invalid selection" in msg for msg in sent)))
        except Exception:
            results.append(("mail out of range", False))

        try:
            ch.handle_channel_directory_steps(123, "oops", 2, {}, interface)
            results.append(("channel invalid", any("Invalid channel number" in msg for msg in sent)))
        except Exception:
            results.append(("channel invalid", False))

        for name, ok in results:
            print(f"{name}: {'PASS' if ok else 'FAIL'}")

        return all(ok for _, ok in results)
    finally:
        if created_config and config_path.exists():
            config_path.unlink()


def main() -> int:
    return 0 if run_smoke_tests() else 1


if __name__ == "__main__":
    raise SystemExit(main())
