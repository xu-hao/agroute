"""Shared strict validation. Booleans are never accepted as numbers."""

from datetime import datetime, timezone
import math
import re

AGENTS = ("codex", "claude", "agy")


class AgRouteError(ValueError):
    """Invalid input or an unsuccessful routing operation, suitable for a CLI diagnostic."""


def table(value, where):
    if not isinstance(value, dict):
        raise AgRouteError(f"{where}: expected a table/object")
    return value


def keys(value, required, optional=(), where="value"):
    if not isinstance(value, dict):
        raise AgRouteError(f"{where}: expected a table/object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    if missing or unknown:
        raise AgRouteError(f"{where}: missing keys {sorted(missing)}; unknown keys {sorted(unknown)}")
    return value


def boolean(value, where):
    if type(value) is not bool:
        raise AgRouteError(f"{where}: expected a boolean")
    return value


def number(value, where, *, positive=False, nonnegative=False, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise AgRouteError(f"{where}: expected a finite number")
    if positive and value <= 0 or nonnegative and value < 0:
        raise AgRouteError(f"{where}: expected a {'positive' if positive else 'non-negative'} number")
    if maximum is not None and value > maximum:
        raise AgRouteError(f"{where}: expected a number <= {maximum}")
    return float(value)


def percentage(value, where):
    return number(value, where, nonnegative=True, maximum=100.0)


def text(value, where, *, label=False, max_length=256):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AgRouteError(f"{where}: expected a non-empty trimmed string")
    if max_length is not None and len(value) > max_length:
        raise AgRouteError(f"{where}: string exceeds maximum length {max_length}")
    if label and not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise AgRouteError(f"{where}: names must contain only letters, digits, '_' or '-'")
    return value


def agent_id(value, where="agent"):
    text(value, where, label=True, max_length=32)
    if value not in AGENTS:
        raise AgRouteError(f"{where}: unsupported agent '{value}' (expected one of {list(AGENTS)})")
    return value


def effort_id(value, where="effort"):
    text(value, where, label=True, max_length=64)
    if value.lower() in ("model_default", "none", "null"):
        raise AgRouteError(f"{where}: '{value}' is not an enforceable native effort/control identifier")
    return value


def parse_timestamp(value, where="timestamp"):
    if isinstance(value, (int, float)) and type(value) is not bool and math.isfinite(value):
        if value <= 0:
            raise AgRouteError(f"{where}: expected a positive Unix timestamp")
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise AgRouteError(f"{where}: expected a timezone-aware ISO-8601 timestamp or Unix epoch")
    raw = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise AgRouteError(f"{where}: invalid ISO-8601 timestamp '{value}'") from exc
    if parsed.utcoffset() is None:
        raise AgRouteError(f"{where}: expected timezone-aware ISO-8601 timestamp")
    return parsed.astimezone(timezone.utc)


def format_timestamp(dt):
    if dt is None:
        return None
    if isinstance(dt, (int, float)) or isinstance(dt, str):
        dt = parse_timestamp(dt)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
