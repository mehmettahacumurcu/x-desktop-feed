import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final


_PAIRING_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_PAIRING_CODE_LENGTH: Final = 8
_PAIRING_LIFETIME_S: Final = 120.0
_MAXIMUM_ATTEMPTS: Final = 5
_STATE_VERSION: Final = 1


class PairingError(RuntimeError):
    pass


@dataclass(frozen=True)
class PairingOffer:
    code: str
    expires_at: float


class PairingStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._offer: PairingOffer | None = None
        self._failed_attempts = 0

    def issue(self) -> PairingOffer:
        with self._lock:
            offer = PairingOffer(
                "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(_PAIRING_CODE_LENGTH)),
                self._clock() + _PAIRING_LIFETIME_S,
            )
            self._offer = offer
            self._failed_attempts = 0
            return offer

    def exchange(self, code: str) -> str:
        with self._lock:
            offer = self._offer
            if offer is None or self._clock() >= offer.expires_at:
                self._offer = None
                raise PairingError("pairing code is invalid or expired")
            if self._failed_attempts >= _MAXIMUM_ATTEMPTS:
                self._offer = None
                raise PairingError("pairing attempt limit reached")
            valid_shape = (
                isinstance(code, str)
                and len(code) == _PAIRING_CODE_LENGTH
                and all(character in _PAIRING_ALPHABET for character in code)
            )
            if not valid_shape or not hmac.compare_digest(
                code.encode("ascii"), offer.code.encode("ascii")
            ):
                self._failed_attempts += 1
                if self._failed_attempts >= _MAXIMUM_ATTEMPTS:
                    self._offer = None
                raise PairingError("pairing code is invalid or expired")

            token = secrets.token_urlsafe(32)
            self._write_digest(self._digest(token))
            self._offer = None
            self._failed_attempts = 0
            return token

    def authenticate(self, token: str) -> bool:
        if not isinstance(token, str):
            return False
        with self._lock:
            expected_digest = self._read_digest()
            if expected_digest is None:
                return False
            return hmac.compare_digest(expected_digest, self._digest(token))

    def revoke(self) -> None:
        with self._lock:
            self._offer = None
            self._failed_attempts = 0
            try:
                self._path.unlink(missing_ok=True)
            except OSError as error:
                raise PairingError("could not revoke pairing state") from error

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _read_digest(self) -> str | None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise PairingError("could not read pairing state") from error

        if (
            type(payload) is not dict
            or set(payload) != {"version", "token_sha256"}
            or type(payload["version"]) is not int
            or payload["version"] != _STATE_VERSION
            or type(payload["token_sha256"]) is not str
            or len(payload["token_sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in payload["token_sha256"])
        ):
            raise PairingError("pairing state is invalid")
        return payload["token_sha256"]

    def _write_digest(self, digest: str) -> None:
        temporary_path: Path | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                text=True,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(
                    {"version": _STATE_VERSION, "token_sha256": digest},
                    temporary_file,
                    separators=(",", ":"),
                )
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self._path)
            temporary_path = None
        except (OSError, TypeError, ValueError) as error:
            raise PairingError("could not persist pairing state") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
