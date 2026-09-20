"""Standalone SSH transport for the Bacon BBS command interface."""

import argparse
import asyncio
import configparser
import hmac
import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

import asyncssh

import bbs_emulator
import db_operations
import ssh_terminal
from app_paths import resolve_app_path
from ssh_auth import (AuthResult, authenticate, authenticate_reset, hash_password,
                      valid_alias, valid_password)


# How often to look for replies that arrived after the command that asked
# for them. Ask Nomad's slow ack is armed at 8 seconds, so a second here is
# well inside the window a user would notice, and the check is a dict pop on
# an empty buffer when there is nothing waiting.
LATE_REPLY_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class SSHConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 2222
    host_key: str = "data/ssh_host_key"
    username: str = ""
    password: str = ""
    registration_enabled: bool = True
    registration_limit_per_hour: int = 5
    login_limit_per_hour: int = 20
    max_sessions: int = 20
    # One session per account, because an account now has one stable sender
    # number and user_states is keyed by it: a second concurrent session for
    # the same account would share its menu position, so typing in one window
    # would move the other. A radio user has exactly one identity and one
    # conversation, and an SSH account is the same thing over a different
    # transport. Raising this trades that isolation away.
    max_sessions_per_account: int = 1
    idle_timeout_seconds: int = 1800
    max_text_bytes: int = 8192


def load_config(path: Optional[str] = None) -> SSHConfig:
    parser = configparser.ConfigParser()
    config_path = resolve_app_path(
        path or os.getenv("BBS_CONFIG_PATH"), "config.ini")
    parser.read(config_path)
    section = parser["ssh"] if parser.has_section("ssh") else {}

    def integer(name, default, minimum=1):
        try:
            return max(minimum, int(section.get(name, default)))
        except (TypeError, ValueError):
            return default

    def boolean(name, default):
        value = str(section.get(name, str(default))).strip().casefold()
        return value in {"1", "true", "yes", "on"}

    return SSHConfig(
        enabled=boolean("enabled", False),
        host=str(section.get("host", "127.0.0.1")).strip() or "127.0.0.1",
        port=integer("port", 2222),
        host_key=str(section.get("host_key", "data/ssh_host_key")).strip()
        or "data/ssh_host_key",
        username=str(section.get("username", "")).strip(),
        password=str(section.get("password", "")),
        registration_enabled=boolean("registration_enabled", True),
        registration_limit_per_hour=integer(
            "registration_limit_per_hour", 5, minimum=0),
        login_limit_per_hour=integer("login_limit_per_hour", 20, minimum=0),
        max_sessions=integer("max_sessions", 20),
        max_sessions_per_account=integer("max_sessions_per_account", 1),
        idle_timeout_seconds=integer("idle_timeout_seconds", 1800),
        max_text_bytes=integer("max_text_bytes", 8192, minimum=1024),
    )


class SessionLimiter:
    def __init__(self, total_limit: int, account_limit: int):
        self.total_limit = total_limit
        self.account_limit = account_limit
        self._total = 0
        self._by_account = {}
        self._lock = threading.Lock()

    def reserve(self, account_id: str) -> bool:
        with self._lock:
            account_count = self._by_account.get(account_id, 0)
            if self._total >= self.total_limit or account_count >= self.account_limit:
                return False
            self._total += 1
            self._by_account[account_id] = account_count + 1
            return True

    def reserve_pending(self) -> bool:
        with self._lock:
            if self._total >= self.total_limit:
                return False
            self._total += 1
            return True

    def promote_pending(self, account_id: str) -> bool:
        with self._lock:
            account_count = self._by_account.get(account_id, 0)
            if account_count >= self.account_limit:
                return False
            self._by_account[account_id] = account_count + 1
            return True

    def release_pending(self) -> None:
        with self._lock:
            if self._total:
                self._total -= 1

    def release(self, account_id: str) -> None:
        with self._lock:
            count = self._by_account.get(account_id, 0)
            if count <= 1:
                self._by_account.pop(account_id, None)
            else:
                self._by_account[account_id] = count - 1
            if self._total:
                self._total -= 1


class BBSClientSession(asyncssh.SSHServerSession):
    def __init__(self, auth: Optional[AuthResult], config: SSHConfig,
                 limiter: SessionLimiter, source_address: str,
                 pending: bool = False):
        self.auth = auth
        self.config = config
        self.limiter = limiter
        self.source_address = source_address
        self._pending = pending
        self._account_username = ""
        self._registration_password = ""
        self._auth_stage = "bbs" if auth else "account_username"
        # A password reset: "reset:<alias>" with the code from a linked radio,
        # either as the SSH login itself or typed at the shared-gate prompt.
        self._reset_alias = ""
        self._reset_code = ""
        self._reset_password = ""
        if auth is not None and getattr(auth, "reset_code", ""):
            self._reset_alias = auth.alias
            self._reset_code = auth.reset_code
            self._auth_stage = "reset_new_password"
        self.channel = None
        self.session = None
        self._line = []
        self._last_was_cr = False
        self._idle_handle = None
        self._late_handle = None
        self._closed = False
        # Set from the client's pty request. No pty (a command run over ssh
        # rather than an interactive login) means no colour and 80 columns.
        self._width = ssh_terminal.DEFAULT_WIDTH
        self._colour = False

    def connection_made(self, channel):
        self.channel = channel

    def pty_requested(self, term_type, term_size, term_modes):
        self._colour = ssh_terminal.wants_colour(term_type)
        self._set_width(term_size[0] if term_size else 0)
        return True

    def terminal_size_changed(self, width, height, pixwidth, pixheight):
        self._set_width(width)

    def _set_width(self, width) -> None:
        # One column short of the edge: a character in the last column puts
        # many terminals into a pending-wrap state that swallows the CRLF.
        try:
            width = int(width)
        except (TypeError, ValueError):
            width = 0
        self._width = (width - 1) if width > ssh_terminal.MIN_WIDTH else ssh_terminal.DEFAULT_WIDTH

    def shell_requested(self):
        if self.auth is None:
            self._say(
                "SSH access accepted. Register or log in to your BBS account.\n"
                "Forgot your password? Enter reset:<username> with a code from "
                "a linked radio.",
                prompt="BBS username: ")
            self._reset_idle_timer()
            return True
        if self._auth_stage == "reset_new_password":
            self._say(f"Reset code accepted for {self._reset_alias}.",
                      prompt="New password: ")
            self._reset_idle_timer()
            return True
        self._start_bbs()
        return True

    def _start_bbs(self) -> None:
        self.session = bbs_emulator.start_ssh_session(
            self.auth.account_id, self.auth.alias,
            max_text_bytes=self.config.max_text_bytes)
        if self.auth.registered:
            self._say(f"Account {self.auth.alias} created. Future logins use "
                      f"{self.auth.alias}.")
        self._say("Connected to Bacon BBS. [0] goes back, and disconnects "
                  "from the main menu.")
        self._send_to_bbs("?")
        self._reset_idle_timer()
        self._schedule_drain()

    def data_received(self, data, datatype):
        for character in data:
            if character == "\x03":
                self.channel.exit(0)
                return
            if character == "\x04":
                self.channel.exit(0)
                return
            if character in {"\x08", "\x7f"}:
                if self._line:
                    self._line.pop()
                    if not self._auth_stage.endswith("password") and self._auth_stage != "registration_confirm":
                        self.channel.write("\b \b")
                continue
            if character in {"\r", "\n"}:
                if character == "\n" and self._last_was_cr:
                    self._last_was_cr = False
                    continue
                self._last_was_cr = character == "\r"
                self.channel.write("\r\n")
                raw_line = "".join(self._line)
                hidden_input = self._auth_stage.endswith("password") or self._auth_stage == "registration_confirm"
                line = raw_line if hidden_input else raw_line.strip()
                self._line.clear()
                if self._auth_stage != "bbs":
                    self._handle_account_auth(line)
                elif line:
                    self._send_to_bbs(line)
                else:
                    self._write_prompt()
                continue
            self._last_was_cr = False
            if character.isprintable() and len(self._line) < 4096:
                self._line.append(character)
                if not self._auth_stage.endswith("password") and self._auth_stage != "registration_confirm":
                    self.channel.write(character)
        self._reset_idle_timer()

    def _handle_account_auth(self, line: str) -> None:
        if self._auth_stage.startswith("reset_"):
            self._handle_password_reset(line)
            return
        if self._auth_stage == "account_username":
            if not line:
                self.channel.write("BBS username: ")
                return
            if line.casefold().startswith("reset:"):
                self._reset_alias = line[6:].strip()
                self._auth_stage = "reset_code"
                self.channel.write("Reset code from your linked radio: ")
                return
            if not valid_alias(line):
                self.channel.write(
                    "Username must be 3-20 characters using letters, numbers, dots, underscores, or hyphens.\r\n"
                    "BBS username: ")
                return
            self._account_username = line
            if db_operations.alias_owner(line) is not None:
                self._auth_stage = "login_password"
                self.channel.write("BBS password: ")
            elif not self.config.registration_enabled:
                self._account_username = ""
                self._say("That account does not exist and registration is "
                          "disabled.", prompt="BBS username: ")
            elif db_operations.alias_conflicts_with_roster(line):
                self._account_username = ""
                self._say("That username is unavailable.", prompt="BBS username: ")
            else:
                self._auth_stage = "registration_password"
                self.channel.write("New account. Create password: ")
            return

        if self._auth_stage == "registration_password":
            if not valid_password(line):
                self._say("Password must be 10-128 characters.",
                          prompt="Create password: ")
                return
            self._registration_password = line
            self._auth_stage = "registration_confirm"
            self.channel.write("Confirm password: ")
            return

        if self._auth_stage == "registration_confirm":
            if not hmac.compare_digest(line, self._registration_password):
                self._registration_password = ""
                self._auth_stage = "registration_password"
                self._say("Passwords do not match.", prompt="Create password: ")
                return
            line = self._registration_password
            self._registration_password = ""
            auth_username = f"new:{self._account_username}"
        else:
            auth_username = self._account_username

        auth = authenticate(
            auth_username, line, self.source_address,
            registration_enabled=self.config.registration_enabled,
            registration_limit_per_hour=self.config.registration_limit_per_hour,
            login_limit_per_hour=self.config.login_limit_per_hour,
        )
        if auth is None:
            self._account_username = ""
            self._auth_stage = "account_username"
            self._say("BBS authentication failed.", prompt="BBS username: ")
            return
        if not self.limiter.promote_pending(auth.account_id):
            self._say("That account is already signed in somewhere else.")
            self.channel.exit(1)
            return
        self.auth = auth
        self._pending = False
        self._auth_stage = "bbs"
        self._start_bbs()

    def _handle_password_reset(self, line: str) -> None:
        if self._auth_stage == "reset_code":
            auth = authenticate_reset(self._reset_alias, line, self.source_address)
            if auth is None:
                self._reset_alias = ""
                self._auth_stage = "account_username"
                self._say("That reset code is not valid.", prompt="BBS username: ")
                return
            self._reset_alias = auth.alias
            self._reset_code = auth.reset_code
            self._auth_stage = "reset_new_password"
            self.channel.write("New password: ")
            return

        if self._auth_stage == "reset_new_password":
            if not valid_password(line):
                self._say("Password must be 10-128 characters.",
                          prompt="New password: ")
                return
            self._reset_password = line
            self._auth_stage = "reset_confirm_password"
            self.channel.write("Confirm new password: ")
            return

        if self._auth_stage == "reset_confirm_password":
            if not hmac.compare_digest(line, self._reset_password):
                self._reset_password = ""
                self._auth_stage = "reset_new_password"
                self._say("Passwords do not match.", prompt="New password: ")
                return
            password_hash, password_salt = hash_password(self._reset_password)
            self._reset_password = ""
            changed = db_operations.set_ssh_password_with_reset_code(
                self._reset_alias, self._reset_code, password_hash, password_salt)
            self._reset_code = ""
            if changed:
                self._say(f"Password changed. Log in again as {self._reset_alias} "
                          "with your new password.")
            else:
                self._say("That reset code has expired or was already used. "
                          "Request a new one from your linked radio.")
            self.channel.exit(0 if changed else 1)

    def eof_received(self):
        if self.channel:
            self.channel.exit(0)
        return False

    def connection_lost(self, exc):
        self._cleanup()

    def _say(self, text: str, prompt: str = "") -> None:
        """Write the server's own words, wrapped like everything else.

        These lines are written straight to the channel rather than coming
        back from the BBS, so they missed the wrapper: on a 60-column
        terminal the connection banner ran to 74 characters and wrapped
        wherever the terminal happened to break it. A prompt is written
        after it, unwrapped, because it has to keep the cursor on its line.
        """
        body = ssh_terminal.render(text, self._width, self._colour)
        self.channel.write(body + "\r\n")
        if prompt:
            self.channel.write(prompt)

    def _write_chunks(self, chunks) -> bool:
        """Write captured reply chunks to the terminal. True if any were."""
        wrote = False
        for chunk in chunks:
            # Wrapped to the terminal's width and coloured for it. The BBS
            # text itself is unchanged; see ssh_terminal.
            body = ssh_terminal.render(str(chunk.get("text") or ""),
                                       self._width, self._colour)
            self.channel.write(body + "\r\n")
            wrote = True
        return wrote

    def _send_to_bbs(self, text: str) -> None:
        chunks, error = self.session.send(text)
        self._write_chunks(chunks)
        if error:
            logging.error("SSH BBS handler error for %s: %s", self.auth.alias, error)
            self._say("The BBS could not process that command.")
        # [0] Exit at the top level. The handler cannot close the connection
        # itself -- it has no idea it is talking to SSH -- so it raises this
        # flag and the transport hangs up.
        if getattr(self.session.interface, "session_ended", False):
            self.channel.exit(0)
            return
        self._write_prompt()

    def _drain_late_replies(self) -> None:
        """Deliver replies that arrived after the command that asked for them.

        Ask Nomad hands the question to a worker thread and answers through
        the same interface up to a minute later; Web Fetch does the same.
        Nothing else on this transport drains that buffer -- _send_to_bbs
        runs only when the user types -- so the answer sat there until the
        next keypress and was then printed alongside the reply to whatever
        was just typed. That keypress was also consumed by the prompt the
        answer had only now displayed, so the user lost both their input and
        their place, and the feature looked hung.

        The web Emulator page never had this: it polls for exactly this
        reason. This is that poll, for a terminal.
        """
        self._late_handle = None
        if self._closed or not self.channel:
            return
        try:
            if self.session and self._auth_stage == "bbs":
                if self._write_chunks(self.session.drain()):
                    self._write_prompt()
        except BrokenPipeError:
            self._cleanup()
            return
        except Exception:
            logging.exception("SSH late-reply drain failed")
        self._schedule_drain()

    def _schedule_drain(self) -> None:
        if self._closed:
            return
        self._late_handle = asyncio.get_running_loop().call_later(
            LATE_REPLY_POLL_SECONDS, self._drain_late_replies)

    def _write_prompt(self) -> None:
        if self.channel:
            self.channel.write("> ")

    def _reset_idle_timer(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
        self._idle_handle = asyncio.get_running_loop().call_later(
            self.config.idle_timeout_seconds, self._idle_disconnect)

    def _idle_disconnect(self) -> None:
        if not self.channel or self._closed:
            return
        try:
            self.channel.write("\r\n")
            self._say("Session closed after being idle.")
            self.channel.exit(0)
        except BrokenPipeError:
            self._cleanup()

    def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._idle_handle:
            self._idle_handle.cancel()
        if self._late_handle:
            self._late_handle.cancel()
        if self.session:
            bbs_emulator.end_session(self.session.token)
        if self._pending:
            self.limiter.release_pending()
        elif self.auth:
            self.limiter.release(self.auth.account_id)


class BBSSSHServer(asyncssh.SSHServer):
    def __init__(self, config: SSHConfig, limiter: SessionLimiter):
        self.config = config
        self.limiter = limiter
        self.connection = None
        self.auth = None
        self.gate_passed = False
        self.source_address = "unknown"

    def connection_made(self, connection):
        self.connection = connection
        peer = connection.get_extra_info("peername")
        if peer:
            self.source_address = str(peer[0])

    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        """The shared gate, and then -- always -- the user's own account.

        The gate used to be exclusive: configuring [ssh] username/password
        returned from this method before per-account auth was ever reached,
        so every account login and every new: registration was refused
        while it was set. A node whose operator wanted the gate "available"
        found it was the only way in, and nobody could use their own name.
        It is additive now, which is what having the option means.

        The gate is checked first and does not consume an account's rate
        limit. A gate username that collides with a real alias wins, so an
        operator setting one should not pick an alias in use.
        """
        self.auth = None
        self.gate_passed = False
        if self.config.username and self.config.password:
            if (hmac.compare_digest(username, self.config.username)
                    and hmac.compare_digest(password, self.config.password)):
                self.gate_passed = True
                return True
        self.auth = authenticate(
            username, password, self.source_address,
            registration_enabled=self.config.registration_enabled,
            registration_limit_per_hour=(
                self.config.registration_limit_per_hour),
            login_limit_per_hour=self.config.login_limit_per_hour,
        )
        return self.auth is not None

    def session_requested(self):
        # Which door they came through decides the session, not which doors
        # exist: with the gate additive, "a gate is configured" no longer
        # means "this visitor used it".
        if self.auth is None:
            if not self.gate_passed:
                return False
            if not self.limiter.reserve_pending():
                return False
            return BBSClientSession(
                None, self.config, self.limiter, self.source_address,
                pending=True)
        if not self.limiter.reserve(self.auth.account_id):
            return False
        return BBSClientSession(
            self.auth, self.config, self.limiter, self.source_address)


def ensure_host_key(path: str) -> str:
    resolved = resolve_app_path(path, "data/ssh_host_key")
    if os.path.exists(resolved):
        return resolved
    os.makedirs(os.path.dirname(resolved) or ".", exist_ok=True)
    key_data = asyncssh.generate_private_key("ssh-ed25519").export_private_key()
    try:
        with open(resolved, "xb") as key_file:
            key_file.write(key_data)
        if os.name != "nt":
            os.chmod(resolved, 0o600)
    except FileExistsError:
        pass
    return resolved


async def start_server(config: SSHConfig):
    limiter = SessionLimiter(
        config.max_sessions, config.max_sessions_per_account)
    host_key = ensure_host_key(config.host_key)
    listen_hosts = [host.strip() for host in config.host.split(",") if host.strip()]
    return await asyncssh.create_server(
        lambda: BBSSSHServer(config, limiter),
        listen_hosts if len(listen_hosts) > 1 else listen_hosts[0], config.port,
        server_host_keys=[host_key],
        encoding="utf-8",
        line_editor=False,
    )


async def run(config_path: Optional[str] = None) -> None:
    db_operations.initialize_database()
    active_config = None
    listener = None
    try:
        while True:
            config = load_config(config_path)
            if config != active_config:
                if listener is not None:
                    listener.close()
                    await listener.wait_closed()
                    listener = None
                if config.enabled:
                    listener = await start_server(config)
                    logging.info("SSH BBS listening on %s port %s", config.host, config.port)
                else:
                    logging.info("SSH BBS listener disabled")
                active_config = config
            await asyncio.sleep(2)
    finally:
        if listener is not None:
            listener.close()
            await listener.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bacon BBS SSH transport")
    parser.add_argument("--config", help="Path to config.ini")
    arguments = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run(arguments.config))
    except (OSError, RuntimeError, asyncssh.Error) as exc:
        logging.error("SSH service could not start: %s", exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
