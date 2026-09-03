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
from app_paths import resolve_app_path
from ssh_auth import AuthResult, authenticate


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
    max_sessions_per_account: int = 2
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
        max_sessions_per_account=integer("max_sessions_per_account", 2),
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
        self._auth_stage = "bbs" if auth else "account_username"
        self.channel = None
        self.session = None
        self._line = []
        self._last_was_cr = False
        self._idle_handle = None
        self._closed = False

    def connection_made(self, channel):
        self.channel = channel

    def shell_requested(self):
        if self.auth is None:
            self.channel.write(
                "SSH access accepted. Register or log in to your BBS account.\r\n"
                "Use new:YourAlias the first time.\r\n"
                "BBS username: ")
            self._reset_idle_timer()
            return True
        self._start_bbs()
        return True

    def _start_bbs(self) -> None:
        self.session = bbs_emulator.start_ssh_session(
            self.auth.account_id, self.auth.alias,
            max_text_bytes=self.config.max_text_bytes)
        if self.auth.registered:
            self.channel.write(
                f"Account {self.auth.alias} created. Future logins use "
                f"{self.auth.alias}.\r\n")
        self.channel.write("Connected to Bacon BBS. Type 0 to go back.\r\n")
        self._send_to_bbs("?")
        self._reset_idle_timer()

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
                    if self._auth_stage != "account_password":
                        self.channel.write("\b \b")
                continue
            if character in {"\r", "\n"}:
                if character == "\n" and self._last_was_cr:
                    self._last_was_cr = False
                    continue
                self._last_was_cr = character == "\r"
                self.channel.write("\r\n")
                raw_line = "".join(self._line)
                line = raw_line if self._auth_stage == "account_password" else raw_line.strip()
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
                if self._auth_stage != "account_password":
                    self.channel.write(character)
        self._reset_idle_timer()

    def _handle_account_auth(self, line: str) -> None:
        if self._auth_stage == "account_username":
            if not line:
                self.channel.write("BBS username: ")
                return
            self._account_username = line
            self._auth_stage = "account_password"
            self.channel.write("BBS password: ")
            return

        auth = authenticate(
            self._account_username, line, self.source_address,
            registration_enabled=self.config.registration_enabled,
            registration_limit_per_hour=self.config.registration_limit_per_hour,
            login_limit_per_hour=self.config.login_limit_per_hour,
        )
        if auth is None:
            self._account_username = ""
            self._auth_stage = "account_username"
            self.channel.write("BBS authentication failed.\r\nBBS username: ")
            return
        if not self.limiter.promote_pending(auth.account_id):
            self.channel.write("Too many sessions for this account.\r\n")
            self.channel.exit(1)
            return
        self.auth = auth
        self._pending = False
        self._auth_stage = "bbs"
        self._start_bbs()

    def eof_received(self):
        if self.channel:
            self.channel.exit(0)
        return False

    def connection_lost(self, exc):
        self._cleanup()

    def _send_to_bbs(self, text: str) -> None:
        chunks, error = self.session.send(text)
        for chunk in chunks:
            body = str(chunk.get("text") or "").replace("\n", "\r\n")
            self.channel.write(body + "\r\n")
        if error:
            logging.error("SSH BBS handler error for %s: %s", self.auth.alias, error)
            self.channel.write("The BBS could not process that command.\r\n")
        self._write_prompt()

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
            self.channel.write("\r\nSession closed after being idle.\r\n")
            self.channel.exit(0)
        except BrokenPipeError:
            self._cleanup()

    def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._idle_handle:
            self._idle_handle.cancel()
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
        if self.config.username and self.config.password:
            self.auth = None
            return hmac.compare_digest(username, self.config.username) and hmac.compare_digest(
                password, self.config.password)
        self.auth = authenticate(
            username, password, self.source_address,
            registration_enabled=self.config.registration_enabled,
            registration_limit_per_hour=(
                self.config.registration_limit_per_hour),
            login_limit_per_hour=self.config.login_limit_per_hour,
        )
        return self.auth is not None

    def session_requested(self):
        if self.config.username and self.config.password:
            if not self.limiter.reserve_pending():
                return False
            return BBSClientSession(
                None, self.config, self.limiter, self.source_address,
                pending=True)
        if self.auth is None or not self.limiter.reserve(self.auth.account_id):
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
    return await asyncssh.create_server(
        lambda: BBSSSHServer(config, limiter),
        config.host, config.port,
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
                    logging.info("SSH BBS listening on %s:%s", config.host, config.port)
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
