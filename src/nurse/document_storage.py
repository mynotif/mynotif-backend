"""Private, collision-resistant staging storage for v3 document assets."""

import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


def save_staging(import_id, role, content, extension):
    key = f"private/document-imports/{import_id}/{role}/{uuid.uuid4().hex}.{extension}"
    stored = default_storage.save(key, ContentFile(content))
    if stored != key:
        # A storage backend must never silently rename into another object's key.
        default_storage.delete(stored)
        raise OSError("Private storage key collision")
    return key


def open_private(key):
    return default_storage.open(key, "rb")


def delete_private(key):
    default_storage.delete(key)


def list_private_prefix(prefix):
    """List keys under a staging prefix so crashes before DB insert are recoverable."""
    pending = [prefix.rstrip("/")]
    keys = []
    while pending:
        path = pending.pop()
        try:
            directories, files = default_storage.listdir(path)
        except FileNotFoundError:
            continue
        keys.extend(f"{path}/{name}" for name in files)
        pending.extend(f"{path}/{name}" for name in directories)
    return keys
