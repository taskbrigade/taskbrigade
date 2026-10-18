"""Envelope encryption of the data store (research/konzept-datenzonen.md §3, guide docs/data-store.md).

- KEK (key encryption key): 32 random bytes in `TBR_DATA_KEY_FILE` (default `/etc/taskbrigade/data.key`), separate
  from `master.key`. Written as one base64 line; a raw 32-byte file is accepted too.
- DEK (data encryption key): 32 random bytes per dataset, stored only wrapped with the KEK
  (`datasets.dek_wrapped`). Deleting the wrapped DEK makes every copy of the file unreadable (crypto-shredding).
- File format v1: header `MAGIC | version | chunk size | file id`, then records `final flag | length | nonce |
  AES-256-GCM(chunk)`. The associated data of each record is the header plus the record index and the final flag,
  so reordered, dropped, truncated or appended records fail authentication.

The module has no database or web dependencies; it only sees bytes.
"""
import base64
import hashlib
import os
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.i18n import _

KEY_BYTES = 32
CHUNK_SIZE = 1024 * 1024
MAGIC = b"TBRDS\x00"
VERSION = 1
NONCE_BYTES = 12
TAG_BYTES = 16
FILE_ID_BYTES = 16
_HEADER = struct.Struct(">6sBI16s")  # magic, version, chunk size, file id
_RECORD = struct.Struct(">BI")  # final flag, plaintext length
WRAP_VERSION = 1
KEK_ID_BYTES = 8
WRAPPED_BYTES = 1 + KEK_ID_BYTES + NONCE_BYTES + KEY_BYTES + TAG_BYTES  # 69, fits VARBINARY(128)


class DataKeyError(RuntimeError):
    """The data key (KEK) is missing, unreadable or malformed."""


class DecryptError(ValueError):
    """Ciphertext or wrapped key does not match the key, or was modified."""


# --- KEK ---------------------------------------------------------------------------------------------------------

def _parse_key(raw: bytes) -> bytes:
    if len(raw) == KEY_BYTES:
        return raw
    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except ValueError:
        key = b""
    if len(key) != KEY_BYTES:
        raise DataKeyError(_("The data key must hold 32 bytes (one base64 line)"))
    return key


def create_kek(path: Path) -> bool:
    """Write a new KEK to `path` (mode 0640, never overwritten). Return False if the file already exists."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as f:
        f.write(base64.b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n")
    return True


def load_kek(path: Path) -> bytes:
    """Read the KEK; refuse a file that every user on the host may read."""
    try:
        mode = path.stat().st_mode
        raw = path.read_bytes()
    except FileNotFoundError as e:
        raise DataKeyError(_("%(path)s is missing – run `tbr-admin init-data-key`", path=path)) from e
    except OSError as e:
        raise DataKeyError(_("%(path)s cannot be read: %(error)s", path=path, error=e.strerror or e)) from e
    if mode & 0o007:
        raise DataKeyError(_("%(path)s is readable by other users – run `chmod 0640 %(path)s`", path=path))
    return _parse_key(raw)


def kek_id(kek: bytes) -> bytes:
    """Short fingerprint of the KEK, stored with each wrapped DEK to name a wrong key clearly."""
    return hashlib.sha256(b"tbr-kek-id\x00" + kek).digest()[:KEK_ID_BYTES]


# --- DEK ---------------------------------------------------------------------------------------------------------

def new_dek() -> bytes:
    return secrets.token_bytes(KEY_BYTES)


def _wrap_aad(context: str) -> bytes:
    return b"tbr-dek/1\x00" + context.encode()


def wrap_dek(kek: bytes, dek: bytes, context: str) -> bytes:
    """Encrypt the DEK with the KEK; `context` (the dataset key) binds it to one dataset row."""
    nonce = secrets.token_bytes(NONCE_BYTES)
    return bytes([WRAP_VERSION]) + kek_id(kek) + nonce + AESGCM(kek).encrypt(nonce, dek, _wrap_aad(context))


def unwrap_dek(kek: bytes, wrapped: bytes | None, context: str) -> bytes:
    if not wrapped:
        raise DecryptError(_("The dataset key was deleted – the data cannot be decrypted any more"))
    if len(wrapped) != WRAPPED_BYTES or wrapped[0] != WRAP_VERSION:
        raise DecryptError(_("Unknown format of the wrapped dataset key"))
    if wrapped[1:1 + KEK_ID_BYTES] != kek_id(kek):
        raise DecryptError(_("The dataset was encrypted with a different data key (TBR_DATA_KEY_FILE)"))
    nonce = wrapped[1 + KEK_ID_BYTES:1 + KEK_ID_BYTES + NONCE_BYTES]
    try:
        return AESGCM(kek).decrypt(nonce, wrapped[1 + KEK_ID_BYTES + NONCE_BYTES:], _wrap_aad(context))
    except InvalidTag as e:
        raise DecryptError(_("The wrapped dataset key does not belong to this dataset or was modified")) from e


# --- Files -------------------------------------------------------------------------------------------------------

def _record_aad(header: bytes, index: int, final: bool) -> bytes:
    return header + struct.pack(">QB", index, 1 if final else 0)


class EncryptingWriter:
    """Encrypt a byte stream into `out` (a binary file object) in chunks; nothing in plaintext touches the disk.
    Also counts the plaintext bytes and its SHA-256."""

    def __init__(self, out, dek: bytes, chunk_size: int = CHUNK_SIZE):
        self._out, self._aead, self._chunk = out, AESGCM(dek), chunk_size
        self._header = _HEADER.pack(MAGIC, VERSION, chunk_size, secrets.token_bytes(FILE_ID_BYTES))
        self._buf = bytearray()
        self._index = 0
        self._closed = False
        self.size = 0
        self._sha = hashlib.sha256()
        out.write(self._header)

    @property
    def sha256(self) -> str:
        return self._sha.hexdigest()

    def _emit(self, data: bytes, final: bool) -> None:
        nonce = secrets.token_bytes(NONCE_BYTES)
        ct = self._aead.encrypt(nonce, data, _record_aad(self._header, self._index, final))
        self._out.write(_RECORD.pack(1 if final else 0, len(data)) + nonce + ct)
        self._index += 1

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ValueError("writer is closed")
        self._buf += data
        self.size += len(data)
        self._sha.update(data)
        # Keep at least one full chunk back: only `close` knows which record is the last one.
        while len(self._buf) > self._chunk:
            self._emit(bytes(self._buf[:self._chunk]), False)
            del self._buf[:self._chunk]

    def close(self) -> None:
        if not self._closed:
            self._emit(bytes(self._buf), True)
            self._buf.clear()
            self._closed = True


def _read_exact(f, n: int) -> bytes:
    data = f.read(n)
    if len(data) != n:
        raise DecryptError(_("The encrypted file is truncated"))
    return data


def decrypt_chunks(f, dek: bytes):
    """Yield the plaintext chunks of an encrypted file object. Raises `DecryptError` on a wrong key or any change
    of the file – a consumer must treat everything already yielded as unconfirmed until the generator ends."""
    header = f.read(_HEADER.size)
    if len(header) != _HEADER.size:
        raise DecryptError(_("Not an encrypted dataset file"))
    magic, version, chunk_size, _file_id = _HEADER.unpack(header)
    if magic != MAGIC or version != VERSION:
        raise DecryptError(_("Not an encrypted dataset file"))
    aead = AESGCM(dek)
    index = 0
    while True:
        head = f.read(_RECORD.size)
        if not head:
            raise DecryptError(_("The encrypted file is truncated"))
        if len(head) != _RECORD.size:
            raise DecryptError(_("The encrypted file is truncated"))
        flag, length = _RECORD.unpack(head)
        if flag not in (0, 1) or length > chunk_size:
            raise DecryptError(_("The encrypted file is damaged"))
        final = flag == 1
        nonce = _read_exact(f, NONCE_BYTES)
        ct = _read_exact(f, length + TAG_BYTES)
        try:
            yield aead.decrypt(nonce, ct, _record_aad(header, index, final))
        except InvalidTag as e:
            raise DecryptError(_("Decryption failed: wrong key or the file was modified")) from e
        index += 1
        if final:
            break
    if f.read(1):
        raise DecryptError(_("The encrypted file has data after its last record"))


def decrypt_file(path: Path, dek: bytes) -> bytes:
    """Whole plaintext of a small file (tests and checks); large files use `decrypt_chunks`."""
    with open(path, "rb") as f:
        return b"".join(decrypt_chunks(f, dek))
