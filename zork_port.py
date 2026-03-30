import atexit
import configparser
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
import tempfile
import urllib.request
from typing import List, Optional, Tuple

from db_operations import get_zork_save, upsert_zork_save


# ---------------------------------------------------------------------------
# Game registry – every Infocom title we support
# ---------------------------------------------------------------------------
GAMES: dict[str, dict] = {
    'zork1': {
        'name': 'Zork I',
        'story_path': os.path.join('data', 'zork1.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/zork1/master/COMPILED/zork1.z3',
    },
    'zork2': {
        'name': 'Zork II',
        'story_path': os.path.join('data', 'zork2.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/zork2/master/COMPILED/zork2.z3',
    },
    'zork3': {
        'name': 'Zork III',
        'story_path': os.path.join('data', 'zork3.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/zork3/master/COMPILED/zork3.z3',
    },
    'hhgttg': {
        'name': "Hitchhiker's Guide",
        'story_path': os.path.join('data', 'hhgttg.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/hitchhikersguide/master/COMPILED/s4.z3',
    },
    'deadline': {
        'name': 'Deadline',
        'story_path': os.path.join('data', 'deadline.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/deadline/master/COMPILED/deadline.z3',
    },
    'enchanter': {
        'name': 'Enchanter',
        'story_path': os.path.join('data', 'enchanter.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/enchanter/master/COMPILED/enchanter.z3',
    },
    'planetfall': {
        'name': 'Planetfall',
        'story_path': os.path.join('data', 'planetfall.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/planetfall/master/COMPILED/planetfall.z3',
    },
    'starcross': {
        'name': 'Starcross',
        'story_path': os.path.join('data', 'starcross.z3'),
        'story_url': 'https://raw.githubusercontent.com/historicalsource/starcross/master/COMPILED/starcross.z3',
    },
}

# Legacy constants kept for any external references
DEFAULT_STORY_URL = GAMES['zork1']['story_url']
DEFAULT_STORY_PATH = GAMES['zork1']['story_path']
MAX_RESPONSE_CHARS = 900

_config = configparser.ConfigParser()
_config.read("config.ini")


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from [zork] section of config.ini. Empty string if absent."""
    return _config.get("zork", key, fallback=fallback).strip()


# Sessions are keyed by (user_id, game_id) – each game has an independent session
_sessions_lock = threading.Lock()
_sessions: dict[tuple, "ZorkSession"] = {}
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


def _get_interpreter_command() -> Optional[List[str]]:
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
        "No Z-machine interpreter found.\n"
        f"Tried: {tried}\n"
        f"Configured interpreter: {configured_hint}\n"
        "Install frotz/dfrotz, or set [zork] interpreter in config.ini."
    )


def _ensure_story_file(game_id: str = 'zork1') -> tuple[bool, str]:
    game = GAMES.get(game_id, GAMES['zork1'])
    # For zork1 only, honour legacy env-var / config overrides
    if game_id == 'zork1':
        story_path = os.getenv("BBS_ZORK_STORY_PATH") or _cfg("story_path") or game['story_path']
        story_url = os.getenv("BBS_ZORK_STORY_URL") or _cfg("story_url") or game['story_url']
    else:
        story_path = game['story_path']
        story_url = game['story_url']

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


def _temp_save_file_path(user_id: int, game_id: str = 'zork1') -> str:
    temp_dir = tempfile.gettempdir()
    return os.path.join(temp_dir, f"tc2_{game_id}_{user_id}_{int(time.time() * 1000)}.qzl")


def has_zork_save(user_id: int, game_id: str = 'zork1') -> bool:
    """Return True if a DB-backed save exists for this user and game."""
    return get_zork_save(user_id, game_id) is not None


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


def _autosave(session: "ZorkSession", user_id: int, game_id: str = 'zork1') -> None:
    """Save game via interpreter, then store resulting save file bytes in SQLite."""
    if session.process.poll() is not None or session.process.stdin is None:
        return
    save_path = _temp_save_file_path(user_id, game_id)
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
                upsert_zork_save(user_id, f.read(), game_id)
    finally:
        try:
            if os.path.exists(save_path):
                os.remove(save_path)
        except Exception:
            pass


def has_zork_session(user_id: int, game_id: str = 'zork1') -> bool:
    with _sessions_lock:
        session = _sessions.get((user_id, game_id))
    return session is not None and session.process.poll() is None


def resume_zork_session(user_id: int, game_id: str = 'zork1') -> str:
    with _sessions_lock:
        session = _sessions.get((user_id, game_id))

    if session is None:
        return f"No active {GAMES.get(game_id, {}).get('name', 'game')} session."

    if session.process.poll() is not None:
        with _sessions_lock:
            _sessions.pop((user_id, game_id), None)
        return "Your previous session ended. Start a new one from Games."

    if session.last_output:
        return f"Resuming:\n\n{session.last_output}"

    return "Session resumed. Enter your next command."


def start_zork_session(user_id: int, game_id: str = 'zork1') -> str:
    with _sessions_lock:
        if (user_id, game_id) in _sessions:
            return resume_zork_session(user_id, game_id)

    interpreter_cmd = _get_interpreter_command()
    if interpreter_cmd is None:
        return _missing_interpreter_message()

    story_ok, story_path = _ensure_story_file(game_id)
    if not story_ok:
        return (
            f"Story file not found at '{story_path}'.\n"
            "Set BBS_ZORK_AUTODOWNLOAD=true to auto-download."
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
        return f"Failed to start game: {exc}"

    session = ZorkSession(process)
    with _sessions_lock:
        _sessions[(user_id, game_id)] = session

    intro = session.read_output()

    save_blob = get_zork_save(user_id, game_id)
    if save_blob:
        save_path = _temp_save_file_path(user_id, game_id)
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
        game_name = GAMES.get(game_id, {}).get('name', 'Game')
        intro = f"{game_name} started. Enter commands like LOOK, NORTH, TAKE LAMP, INVENTORY."
    session.last_output = intro
    return intro


def send_zork_command(user_id: int, command: str, game_id: str = 'zork1') -> str:
    with _sessions_lock:
        session = _sessions.get((user_id, game_id))

    if session is None:
        return "No active game session. Go to the Games menu to start a game."

    if not command.strip():
        return "Enter a command, or send X to exit."

    if session.process.poll() is not None:
        with _sessions_lock:
            _sessions.pop((user_id, game_id), None)
        return "Game session ended. Go to the Games menu to start a new one."

    output = session.send(command.strip())
    if not output:
        return "[No output]"

    if session.process.poll() is None:
        _autosave(session, user_id, game_id)

    return output


def stop_zork_session(user_id: int, game_id: str = 'zork1') -> None:
    with _sessions_lock:
        session = _sessions.pop((user_id, game_id), None)

    if session is not None:
        session.stop()


def stop_all_sessions() -> None:
    with _sessions_lock:
        keys = list(_sessions.keys())

    for user_id, game_id in keys:
        stop_zork_session(user_id, game_id)


atexit.register(stop_all_sessions)


# ---------------------------------------------------------------------------
# Score parsing
# ---------------------------------------------------------------------------

# Matches Infocom "Your score is 57 (total of 350 points), in 32 moves."
# or "Your score is 57 of a possible 350, in 32 moves."
_SCORE_RE = re.compile(
    r'score is (\d+)\s*'
    r'(?:[\(\[](?:total of |out of )?(\d+)|of a possible (\d+))?'
    r'.*?in (\d+)\s*moves?',
    re.IGNORECASE | re.DOTALL,
)


def parse_game_score(text: str) -> Optional[Tuple[int, int, int]]:
    """Parse Infocom score output. Returns (score, max_score, moves) or None."""
    m = _SCORE_RE.search(text)
    if not m:
        return None
    score = int(m.group(1))
    max_score = int(m.group(2) or m.group(3) or 0)
    moves = int(m.group(4))
    return score, max_score, moves
