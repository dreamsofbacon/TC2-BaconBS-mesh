import atexit
import configparser
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
import tempfile
import urllib.request

from db_operations import get_zork_save, upsert_zork_save


DEFAULT_STORY_URL = "https://raw.githubusercontent.com/historicalsource/zork1/master/COMPILED/zork1.z3"
DEFAULT_STORY_PATH = os.path.join("data", "zork1.z3")
MAX_RESPONSE_CHARS = 900

_config = configparser.ConfigParser()
_config.read("config.ini")


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from [zork] section of config.ini. Empty string if absent."""
    return _config.get("zork", key, fallback=fallback).strip()


_sessions_lock = threading.Lock()
_sessions = {}
_last_interpreter_candidates: list[str] = []


class ZorkSession:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.output_queue: queue.Queue[str] = queue.Queue()
        self.last_output = ""
        self.reader_thread = threading.Thread(target=self._read_output, daemon=True)
        self.reader_thread.start()

    def _read_output(self) -> None:
        if self.process.stdout is None:
            return
        try:
            for line in self.process.stdout:
                if line:
                    self.output_queue.put(line)
        except Exception:
            return

    def read_output(self, settle_seconds: float = 1.2) -> str:
        chunks = []
        last_data_time = time.time()

        while True:
            try:
                line = self.output_queue.get(timeout=0.2)
                chunks.append(line)
                last_data_time = time.time()
            except queue.Empty:
                if time.time() - last_data_time >= settle_seconds:
                    break

        text = "".join(chunks).strip()
        if len(text) > MAX_RESPONSE_CHARS:
            text = f"{text[:MAX_RESPONSE_CHARS]}\n\n[Output truncated]"
        if text:
            self.last_output = text
        return text

    def send(self, command: str) -> str:
        if self.process.stdin is None:
            return "Zork session is unavailable."

        try:
            self.process.stdin.write(command + "\n")
            self.process.stdin.flush()
        except Exception as exc:
            return f"Error sending command to Zork: {exc}"

        return self.read_output()

    def stop(self) -> None:
        try:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=1.5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


def _get_interpreter_command() -> list[str] | None:
    global _last_interpreter_candidates

    # env var overrides config, config overrides built-in defaults
    configured = (os.getenv("BBS_ZORK_INTERPRETER") or _cfg("interpreter") or "").strip()
    if configured:
        candidates = [configured]
    else:
        candidates = [
            "dfrotz",
            "frotz",
            "dumb-frotz",
            "dumb_frotz",
            "/usr/games/dfrotz",
            "/usr/bin/dfrotz",
            "/usr/games/frotz",
            "/usr/bin/frotz",
        ]
    _last_interpreter_candidates = [candidate for candidate in candidates if candidate]

    for candidate in candidates:
        if not candidate:
            continue

        parts = shlex.split(candidate)
        if not parts:
            continue

        exe = parts[0]
        if os.path.isfile(exe) or shutil.which(exe):
            return parts

        # Common Windows case where configured path omits .exe
        if os.name == "nt" and not exe.lower().endswith(".exe"):
            exe_with_ext = exe + ".exe"
            if os.path.isfile(exe_with_ext) or shutil.which(exe_with_ext):
                return [exe_with_ext, *parts[1:]]

    return None


def _missing_interpreter_message() -> str:
    tried = ", ".join(_last_interpreter_candidates or ["dfrotz", "frotz"])
    configured = (os.getenv("BBS_ZORK_INTERPRETER") or _cfg("interpreter") or "").strip()
    configured_hint = configured if configured else "(not set)"
    return (
        "No Zork interpreter found.\n"
        f"Tried: {tried}\n"
        f"Configured BBS_ZORK_INTERPRETER / [zork].interpreter: {configured_hint}\n"
        "Install frotz/dfrotz on the host, or set [zork] interpreter in config.ini to a full executable path."
    )


def _ensure_story_file() -> tuple[bool, str]:
    story_path = os.getenv("BBS_ZORK_STORY_PATH") or _cfg("story_path") or DEFAULT_STORY_PATH
    story_url = os.getenv("BBS_ZORK_STORY_URL") or _cfg("story_url") or DEFAULT_STORY_URL
    autodownload_raw = os.getenv("BBS_ZORK_AUTODOWNLOAD") or _cfg("autodownload", "true")
    autodownload = autodownload_raw.strip().lower() not in {"0", "false", "no"}

    if os.path.exists(story_path):
        return True, story_path

    if not autodownload:
        return False, story_path

    os.makedirs(os.path.dirname(story_path), exist_ok=True)
    try:
        urllib.request.urlretrieve(story_url, story_path)
        return True, story_path
    except Exception:
        return False, story_path


def _temp_save_file_path(user_id: int) -> str:
    temp_dir = tempfile.gettempdir()
    return os.path.join(temp_dir, f"tc2_zork_{user_id}_{int(time.time() * 1000)}.qzl")


def has_zork_save(user_id: int) -> bool:
    """Return True if a DB-backed save exists for this user."""
    return get_zork_save(user_id) is not None


def _drain_output(session: "ZorkSession", settle_seconds: float = 0.8) -> None:
    """Drain queued output without updating last_output."""
    last_data_time = time.time()
    while True:
        try:
            session.output_queue.get(timeout=0.2)
            last_data_time = time.time()
        except queue.Empty:
            if time.time() - last_data_time >= settle_seconds:
                break


def _autosave(session: "ZorkSession", user_id: int) -> None:
    """Save game via interpreter, then store resulting save file bytes in SQLite."""
    if session.process.poll() is not None or session.process.stdin is None:
        return
    save_path = _temp_save_file_path(user_id)
    try:
        session.process.stdin.write("save\n")
        session.process.stdin.flush()
        time.sleep(0.4)
        session.process.stdin.write(save_path + "\n")
        session.process.stdin.flush()
    except Exception:
        return
    _drain_output(session)
    try:
        if os.path.exists(save_path):
            with open(save_path, "rb") as f:
                upsert_zork_save(user_id, f.read())
    finally:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception:
            pass


def has_zork_session(user_id: int) -> bool:
    with _sessions_lock:
        session = _sessions.get(user_id)
    return session is not None and session.process.poll() is None


def resume_zork_session(user_id: int) -> str:
    with _sessions_lock:
        session = _sessions.get(user_id)

    if session is None:
        return "No active Zork session."

    if session.process.poll() is not None:
        with _sessions_lock:
            _sessions.pop(user_id, None)
        return "Your previous Zork session ended. Start a new one with Z."

    if session.last_output:
        return f"Resuming your Zork session:\n\n{session.last_output}"

    return "Resuming your Zork session. Enter your next command."


def start_zork_session(user_id: int) -> str:
    with _sessions_lock:
        if user_id in _sessions:
            return resume_zork_session(user_id)

    interpreter_cmd = _get_interpreter_command()
    if interpreter_cmd is None:
        return _missing_interpreter_message()

    story_ok, story_path = _ensure_story_file()
    if not story_ok:
        return (
            f"Zork story file not found at '{story_path}'.\n"
            "Set BBS_ZORK_STORY_PATH or enable BBS_ZORK_AUTODOWNLOAD=true."
        )

    cmd = [*interpreter_cmd, story_path]
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        return f"Failed to start Zork: {exc}"

    session = ZorkSession(process)
    with _sessions_lock:
        _sessions[user_id] = session

    # Drain the intro splash (we'll replace it if restoring from a save)
    intro = session.read_output()

    save_blob = get_zork_save(user_id)
    if save_blob:
        save_path = _temp_save_file_path(user_id)
        try:
            with open(save_path, "wb") as f:
                f.write(save_blob)

            if session.process.stdin is not None and session.process.poll() is None:
                try:
                    session.process.stdin.write("restore\n")
                    session.process.stdin.flush()
                    time.sleep(0.4)
                    session.process.stdin.write(save_path + "\n")
                    session.process.stdin.flush()
                except Exception:
                    pass
            restore_output = session.read_output()
            if restore_output:
                # Send 'look' so the player immediately sees their current location
                look_output = session.send("look")
                result = look_output if look_output else restore_output
                session.last_output = result
                return result
        finally:
            try:
                if os.path.exists(save_path):
                    os.remove(save_path)
            except Exception:
                pass

    if not intro:
        intro = "Zork started. Enter commands like LOOK, NORTH, TAKE LAMP, INVENTORY."
    session.last_output = intro
    return intro

def send_zork_command(user_id: int, command: str) -> str:
    with _sessions_lock:
        session = _sessions.get(user_id)

    if session is None:
        return "No active Zork session. Open Utilities and choose Zork first."

    if not command.strip():
        return "Enter a command for Zork, or send X to exit."

    if session.process.poll() is not None:
        with _sessions_lock:
            _sessions.pop(user_id, None)
        return "Zork session ended. Start a new one from Utilities."

    output = session.send(command.strip())
    if not output:
        return "[No output]"

    if session.process.poll() is None:
        _autosave(session, user_id)

    return output


def stop_zork_session(user_id: int) -> None:
    with _sessions_lock:
        session = _sessions.pop(user_id, None)

    if session is not None:
        session.stop()


def stop_all_sessions() -> None:
    with _sessions_lock:
        user_ids = list(_sessions.keys())

    for user_id in user_ids:
        stop_zork_session(user_id)


atexit.register(stop_all_sessions)
