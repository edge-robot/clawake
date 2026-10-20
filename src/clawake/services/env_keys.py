"""Key names of member env files, without values (ADR-005 item 4).

The helper exists so that checks like "is ``token_env`` declared?" never handle secret
values: each line is read as bytes, only the part before the first ``=`` is decoded, and
the rest of the line is dropped without being decoded, returned, logged or kept.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")


class EnvKeysError(OSError):
    """An env file exists but cannot be read; the message never contains file content."""


def env_file_keys(path: Path) -> frozenset[str]:
    """Return the variable names declared in an env file (``KEY=value`` lines).

    Blank lines, comments and lines that are not ``KEY=...`` (for example ``export KEY=...``
    or ``KEY = ...``, which Podman's env-file format does not accept) declare nothing.
    A missing file declares nothing; an unreadable file raises ``EnvKeysError``.
    """
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return frozenset()
    except OSError as exc:
        raise EnvKeysError(f"cannot read env file {path}: {exc.strerror}") from None
    keys: set[str] = set()
    with handle:
        for line in handle:
            name, separator, _ = line.lstrip().partition(b"=")
            if separator and _KEY.fullmatch(name):
                keys.add(name.decode("ascii"))
    return frozenset(keys)
