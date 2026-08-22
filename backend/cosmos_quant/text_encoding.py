from __future__ import annotations

import re
from typing import Any

_LATIN1_RUN = re.compile(r"[\x80-\xff]+")


def _decode_latin1_utf8(value: str) -> str:
    """Undo one or more UTF-8-as-Latin-1 decoding passes.

    Windows PowerShell 5.1 can decode an ``application/json`` response as an
    ANSI code page when the charset is omitted. A later JSON round-trip then
    persists strings such as ``Ã©Â¡Âº``. Legitimate text is left untouched when
    it is not valid UTF-8 bytes represented as Latin-1.
    """

    current = value
    for _ in range(3):
        try:
            decoded = current.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if decoded == current:
            break
        current = decoded
    return current


def repair_mojibake(value: str) -> str:
    repaired = _decode_latin1_utf8(value)
    if repaired != value:
        return repaired
    return _LATIN1_RUN.sub(lambda match: _decode_latin1_utf8(match.group(0)), value)


def repair_text_tree(value: Any) -> Any:
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [repair_text_tree(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_text_tree(item) for key, item in value.items()}
    return value
