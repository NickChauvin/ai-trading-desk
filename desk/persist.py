"""Small durable writes shared by the paper ledger and the model budget."""
from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write text and replace `path` only after the bytes are flushed.

    A reader sees the previous file or the complete new file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
