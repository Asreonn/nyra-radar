"""Typed access to DB-backed runtime settings."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from radar.config import DEFAULT_SETTINGS, default_setting
from radar.db.models import Setting


def get(session: Session, key: str, fallback: Any = None) -> Any:
    row = session.get(Setting, key)
    if row is not None:
        return row.value
    value = default_setting(key)
    return fallback if value is None else value


def set_value(session: Session, key: str, value: Any) -> Any:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(f"unknown setting: {key}")
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=value)
        session.add(row)
    else:
        row.value = value
    return value


def all_values(session: Session) -> dict[str, Any]:
    values = {key: default_setting(key) for key in DEFAULT_SETTINGS}
    for row in session.query(Setting).all():
        values[row.key] = row.value
    return values
