"""Encryption at rest for the few secrets SyncHub must hold (the file-server login for the workers' shares).

The key is a file in the data folder (synchub.key, mode 600), not part of the database, so a copy of the database alone does not reveal a secret.
Keep the key with your backups: without it a stored secret cannot be read and has to be entered again (nothing else is lost).
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from config import DATA_DIR

logger = logging.getLogger(__name__)
KEY_FILE = DATA_DIR / "synchub.key"


def _key():
    try:
        return KEY_FILE.read_bytes().strip()
    except FileNotFoundError:
        pass
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    logger.info(f"Made the encryption key {KEY_FILE}; keep it with your database backups")
    return key


def encrypt(text):
    return Fernet(_key()).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token):
    """The text, or None when the token is not valid for this key (the key file was replaced or lost)."""
    try:
        return Fernet(_key()).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError):
        return None
