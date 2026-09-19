"""Schema versions of the files paperfill writes.

A file carries `schema` in its first record (recorder `start`, journal `run_start`).
Readers accept files without one as version 1 (written before the field existed) and
refuse files from a newer major version instead of misreading them.
"""

from __future__ import annotations

from typing import Any

RECORDING_SCHEMA = 1
JOURNAL_SCHEMA = 1


class SchemaError(ValueError):
    pass


def check(kind: str, record: dict[str, Any], supported: int) -> int:
    """Return the file's schema version or raise if it is newer than `supported`."""
    version = record.get("schema", 1)
    if not isinstance(version, int) or version < 1:
        raise SchemaError(f"{kind}: schema field {version!r} is not a positive integer")
    if version > supported:
        raise SchemaError(
            f"{kind}: schema {version} is newer than this paperfill supports ({supported}); "
            "upgrade paperfill"
        )
    return version
