import atexit
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.request


DEFAULT_STORY_URL = "https://raw.githubusercontent.com/historicalsource/zork1/master/COMPILED/zork1.z3"
DEFAULT_STORY_PATH = os.path.join("data", "zork1.z3")
MAX_RESPONSE_CHARS = 900


_sessions_lock = threading.Lock()
_sessions = {}


class ZorkSession:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.output_queue: queue.Queue[str] = queue.Queue()
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
    configured = os.getenv("BBS_ZORK_INTERPRETER", "").strip()
    candidates = [configured] if configured else ["dfrotz", "frotz"]

    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return [candidate]
    return None


def _ensure_story_file() -> tuple[bool, str]:
    story_path = os.getenv("BBS_ZORK_STORY_PATH", DEFAULT_STORY_PATH).strip() or DEFAULT_STORY_PATH
    story_url = os.getenv("BBS_ZORK_STORY_URL", DEFAULT_STORY_URL).strip() or DEFAULT_STORY_URL
    autodownload = os.getenv("BBS_ZORK_AUTODOWNLOAD", "true").strip().lower() not in {"0", "false", "no"}

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


def start_zork_session(user_id: int) -> str:
    with _sessions_lock:
        if user_id in _sessions:
            return "Zork session already running. Enter a command, or send X to exit."

    interpreter_cmd = _get_interpreter_command()
    if interpreter_cmd is None:
        return (
            "No Zork interpreter found. Install frotz/dfrotz and try again.\n"
            "You can also set BBS_ZORK_INTERPRETER to a custom command."
        )

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

    intro = session.read_output()
    if not intro:
        intro = "Zork started. Enter commands like LOOK, NORTH, TAKE LAMP, INVENTORY."
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
