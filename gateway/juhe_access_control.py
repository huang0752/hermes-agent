"""Dynamic runtime access-control store for Juhe conversations."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


def _coerce_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        items = [value]

    seen: set[str] = set()
    normalized: list[str] = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


@dataclass(frozen=True)
class _FileSignature:
    exists: bool
    mtime_ns: int | None
    size: int | None


@dataclass(frozen=True)
class _AccessControlState:
    dm_allow_from: tuple[str, ...]
    group_trigger_user_ids: tuple[str, ...]
    source: str


class JuheAccessControlStore:
    """Reads Juhe DM allowlists and group trigger users from a runtime JSON file."""

    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        fallback_dm_allow_from: list[str] | None = None,
        fallback_group_trigger_user_ids: list[str] | None = None,
    ) -> None:
        self.base_dir = base_dir or (get_hermes_home() / "juhe")
        self.path = self.base_dir / "access_control.json"
        self._fallback_state = _AccessControlState(
            dm_allow_from=tuple(_coerce_string_list(fallback_dm_allow_from)),
            group_trigger_user_ids=tuple(_coerce_string_list(fallback_group_trigger_user_ids)),
            source="fallback",
        )
        self._cached_signature: _FileSignature | None = None
        self._cached_state: _AccessControlState | None = None
        self._last_known_good_file_state: _AccessControlState | None = None

    def get_dm_allow_from(self) -> list[str]:
        return list(self._load_state().dm_allow_from)

    def get_group_trigger_user_ids(self) -> list[str]:
        return list(self._load_state().group_trigger_user_ids)

    def has_file_access_controls(self) -> bool:
        state = self._load_state()
        if state.source != "file":
            return False
        return bool(state.dm_allow_from or state.group_trigger_user_ids)

    def _load_state(self) -> _AccessControlState:
        signature = self._get_signature()
        if self._cached_signature == signature and self._cached_state is not None:
            return self._cached_state

        if not signature.exists:
            self._last_known_good_file_state = None
            return self._cache_state(signature, self._fallback_state)

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = self._parse_state(raw)
        except Exception as exc:
            state = self._last_known_good_file_state or self._fallback_state
            logger.warning(
                "Juhe access control file %s is invalid; using %s values (%s)",
                self.path,
                state.source,
                exc,
            )
            return self._cache_state(signature, state)

        self._last_known_good_file_state = state
        return self._cache_state(signature, state)

    def _cache_state(
        self,
        signature: _FileSignature,
        state: _AccessControlState,
    ) -> _AccessControlState:
        self._cached_signature = signature
        self._cached_state = state
        return state

    def _get_signature(self) -> _FileSignature:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return _FileSignature(exists=False, mtime_ns=None, size=None)
        return _FileSignature(exists=True, mtime_ns=stat.st_mtime_ns, size=stat.st_size)

    def _parse_state(self, raw: Any) -> _AccessControlState:
        if not isinstance(raw, dict):
            raise ValueError("top-level JSON value must be an object")
        return _AccessControlState(
            dm_allow_from=tuple(_coerce_string_list(raw.get("dm_allow_from"))),
            group_trigger_user_ids=tuple(_coerce_string_list(raw.get("group_trigger_user_ids"))),
            source="file",
        )
