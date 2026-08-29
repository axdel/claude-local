"""Deterministic password and signed-identity authentication service."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
from hmac import new as new_hmac

from ..repositories.user_repository import (
    DuplicateUsernameError,
    UserRecord,
    UserRepository,
)
from ..schemas import Role, UserCreate, UserCredentials

_PBKDF2_SCHEME = "pbkdf2_sha256"
_PASSWORD_ITERATIONS = 10_000
_PASSWORD_DIGEST_BYTES = 32
_SIGNING_KEY_BYTES = 32
_INVALID_CREDENTIALS_MESSAGE = "invalid username or password"


@dataclass(frozen=True, slots=True)
class PasswordRecord:
    """One canonical username-bound PBKDF2 password record."""

    salt: bytes
    digest: bytes

    @classmethod
    def from_credentials(cls, username: str, password: str) -> PasswordRecord:
        """Derive the canonical record for validated registration credentials."""
        salt = username.encode()
        return cls(
            salt=salt,
            digest=_derive_password(password, salt, _PASSWORD_ITERATIONS),
        )

    @classmethod
    def parse(cls, username: str, encoded_record: str) -> PasswordRecord | None:
        """Parse an exact canonical record bound to the supplied username."""
        try:
            expected_salt = username.encode()
        except UnicodeEncodeError:
            return None
        expected_prefix = f"{_PBKDF2_SCHEME}${_PASSWORD_ITERATIONS}${expected_salt.hex()}$"
        if len(encoded_record) != len(expected_prefix) + (2 * _PASSWORD_DIGEST_BYTES):
            return None
        try:
            scheme, iterations_text, salt_text, digest_text = encoded_record.split("$")
            digest = bytes.fromhex(digest_text)
        except ValueError:
            return None
        if (
            scheme != _PBKDF2_SCHEME
            or iterations_text != str(_PASSWORD_ITERATIONS)
            or salt_text != expected_salt.hex()
            or digest_text != digest.hex()
            or len(digest) != _PASSWORD_DIGEST_BYTES
        ):
            return None
        return cls(salt=expected_salt, digest=digest)

    def encode(self) -> str:
        """Serialize this record in its single canonical text representation."""
        return f"{_PBKDF2_SCHEME}${_PASSWORD_ITERATIONS}${self.salt.hex()}${self.digest.hex()}"


class UsernameAlreadyExistsError(ValueError):
    """Raised when registration requests an existing username."""


class InvalidCredentialsError(ValueError):
    """Raised when credentials or a signed token cannot identify a current user."""


@dataclass(frozen=True, slots=True)
class AuthService:
    """Own registration, credential verification, and signed user-identity tokens."""

    users: UserRepository
    signing_key: bytes

    def __post_init__(self) -> None:
        if len(self.signing_key) != _SIGNING_KEY_BYTES:
            raise ValueError("signing key must contain exactly 32 bytes")

    def register(self, user: UserCreate) -> UserRecord:
        """Register a normal user with deterministic benchmark password material."""
        password_hash = _hash_password(user.username, user.password)
        try:
            return self.users.create(user.username, password_hash, Role.USER)
        except DuplicateUsernameError as error:
            message = f"username already exists: {user.username}"
            raise UsernameAlreadyExistsError(message) from error

    def authenticate(self, credentials: UserCredentials) -> UserRecord:
        """Return the matching user after one fixed-cost password derivation."""
        user = self.users.get_by_username(credentials.username)
        record = None if user is None else PasswordRecord.parse(user.username, user.password_hash)
        salt = credentials.username.encode() if record is None else record.salt
        expected_digest = bytes(_PASSWORD_DIGEST_BYTES) if record is None else record.digest
        candidate_digest = _derive_password(
            credentials.password,
            salt,
            _PASSWORD_ITERATIONS,
        )
        digest_matches = compare_digest(candidate_digest, expected_digest)
        if user is None or record is None or not digest_matches:
            raise InvalidCredentialsError(_INVALID_CREDENTIALS_MESSAGE)
        return user

    def issue_token(self, user: UserRecord) -> str:
        """Return an HMAC-signed token containing only the canonical user identifier."""
        payload = str(user.id)
        return f"{payload}.{self._sign(payload).hex()}"

    def verify_token(self, token: str) -> UserRecord:
        """Verify the signature and reload the current user record."""
        try:
            payload, signature_text = token.split(".")
            user_id = int(payload)
            signature = bytes.fromhex(signature_text)
        except ValueError as error:
            raise InvalidCredentialsError("invalid token") from error
        if (
            user_id < 1
            or payload != str(user_id)
            or signature_text != signature.hex()
            or not compare_digest(signature, self._sign(payload))
        ):
            raise InvalidCredentialsError("invalid token")
        user = self.users.get_by_id(user_id)
        if user is None:
            raise InvalidCredentialsError("invalid token")
        return user

    def _sign(self, payload: str) -> bytes:
        return new_hmac(self.signing_key, payload.encode(), sha256).digest()


def _hash_password(username: str, password: str) -> str:
    return PasswordRecord.from_credentials(username, password).encode()


def _derive_password(password: str, salt: bytes, iterations: int) -> bytes:
    return pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        iterations,
        dklen=_PASSWORD_DIGEST_BYTES,
    )
