"""A tiny atomic JSON store for small lists of pydantic models.

Used by the web console to persist agent definitions, automations, and
uploaded-file metadata. NOT a database — it loads the whole file on each access
and writes atomically (temp file + ``os.replace``). This is deliberate: the
data sets are tiny (a handful of user-authored records), and keeping the store
trivial avoids pulling in a dependency or building a schema for something that
is just a persisted list.

Corrupt or unreadable files raise :class:`StoreError` (fail closed) rather than
silently returning empty data, so a user never loses configuration to a
silent reset.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from errors import StorageError

T = TypeVar("T", bound=BaseModel)


class StoreError(StorageError):
    """Raised when a store file is corrupt, unreadable, or fails to write."""


class JsonStore(Generic[T]):
    """An atomic JSON file holding a list of ``T`` records keyed by ``id``."""

    def __init__(self, path: Path, model: type[T]) -> None:
        self._path = Path(path)
        self._model = model
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _load_raw(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise StoreError(f"cannot read store {self._path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StoreError(f"corrupt store {self._path}: {exc}") from exc
        if not isinstance(data, list):
            raise StoreError(f"store {self._path} must hold a JSON list")
        return data

    def list(self) -> list[T]:
        out: list[T] = []
        for item in self._load_raw():
            try:
                out.append(self._model.model_validate(item))
            except ValidationError as exc:
                raise StoreError(f"invalid record in {self._path}: {exc}") from exc
        return out

    def get(self, record_id: str) -> T | None:
        for item in self.list():
            if item.id == record_id:
                return item
        return None

    def upsert(self, record: T) -> T:
        records = self.list()
        replaced = False
        for i, existing in enumerate(records):
            if existing.id == record.id:
                records[i] = record
                replaced = True
                break
        if not replaced:
            records.append(record)
        self._write(records)
        return record

    def delete(self, record_id: str) -> bool:
        records = self.list()
        new = [r for r in records if r.id != record_id]
        if len(new) == len(records):
            return False
        self._write(new)
        return True

    def _write(self, records: list[T]) -> None:
        payload = json.dumps(
            [r.model_dump(mode="json") for r in records],
            indent=2, ensure_ascii=False, default=str,
        )
        # Atomic write: temp file in the same directory, then os.replace.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self._path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise StoreError(f"cannot write store {self._path}: {exc}") from exc


__all__ = ["JsonStore", "StoreError"]
