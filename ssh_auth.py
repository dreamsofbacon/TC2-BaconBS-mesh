"""Credential and registration boundary for SSH access to the BBS."""

import re
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from db_operations import (
    create_ssh_account,
    get_account_alias,
    get_ssh_credentials,
    link_rate_limit_ok,
    record_link_attempt,
)


_ALIAS_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,19}\Z")
_SCRYPT_LENGTH = 32
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1


@dataclass(frozen=True)
class AuthResult:
    account_id: str
    alias: str
    registered: bool = False


def valid_alias(alias: str) -> bool:
    return bool(_ALIAS_PATTERN.fullmatch(str(alias or '').strip()))


def valid_password(password: str) -> bool:
    value = str(password or '')
    return 10 <= len(value) <= 128


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    derived = Scrypt(
        salt=salt, length=_SCRYPT_LENGTH,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
    ).derive(str(password).encode('utf-8'))
    return derived.hex(), salt.hex()


def verify_password(password: str, password_hash: str,
                    password_salt: str) -> bool:
    try:
        expected = bytes.fromhex(str(password_hash))
        salt = bytes.fromhex(str(password_salt))
        Scrypt(
            salt=salt, length=len(expected),
            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
        ).verify(str(password).encode('utf-8'), expected)
        return True
    except (InvalidKey, TypeError, ValueError):
        return False


def authenticate(alias: str, password: str, source_address: str,
                 registration_enabled: bool = True,
                 registration_limit_per_hour: int = 5,
                 login_limit_per_hour: int = 20) -> AuthResult | None:
    """Authenticate an alias, or explicitly register with ``new:<alias>``."""
    username = str(alias or '').strip()
    source_key = f"ssh-ip:{str(source_address or 'unknown').strip()}"
    registering = username.casefold().startswith('new:')
    clean_alias = username[4:] if registering else username

    if not valid_alias(clean_alias) or not valid_password(password):
        if registering:
            record_link_attempt(source_key, 'ssh_register', False)
        else:
            record_link_attempt(source_key, 'ssh_login', False)
        return None

    if registering:
        if not registration_enabled or not link_rate_limit_ok(
                source_key, 'ssh_register', registration_limit_per_hour):
            record_link_attempt(source_key, 'ssh_register', False)
            return None
        password_hash, password_salt = hash_password(password)
        account_id = create_ssh_account(
            clean_alias, password_hash, password_salt)
        record_link_attempt(source_key, 'ssh_register', account_id is not None)
        if account_id is None:
            return None
        return AuthResult(account_id, clean_alias, True)

    if not link_rate_limit_ok(source_key, 'ssh_login', login_limit_per_hour):
        record_link_attempt(source_key, 'ssh_login', False)
        return None
    credentials = get_ssh_credentials(clean_alias)
    authenticated = bool(
        credentials and verify_password(password, credentials[1], credentials[2]))
    record_link_attempt(source_key, 'ssh_login', authenticated)
    if not authenticated:
        return None
    return AuthResult(credentials[0], get_account_alias(credentials[0]), False)