"""Offline calling-agent price store (`<state_root>/prices.json`) and `price_request` builder.

`agroute` never fetches prices from the network. When model discovery surfaces a model key
missing from user configuration, the built-in catalog, and non-expired `<state_root>/prices.json`
(or whose `prices.json` entry has expired), `agroute` emits a deterministic `price_request`
payload for the calling agent to fulfill via `agroute prices resolve --file <path>`.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import json
from pathlib import Path

from .files import atomic_write
from .pricing import RATE_CATEGORIES, _CATALOG, resolve_price
from .state import check, claim, identity_locked
from .validation import AgRouteError, format_timestamp, keys, number, parse_timestamp, table, text

SNAPSHOT_FORMAT = "agroute-price-snapshot-v2"
REQUEST_FORMAT = "agroute-price-request-v1"
PENDING_FORMAT = "agroute-price-pending-v1"
RESOLVE_REPORT_FORMAT = "agroute-price-resolve-v1"
SHOW_FORMAT = "agroute-price-show-v1"

DEFAULT_PRICED_TTL_DAYS = 30
DEFAULT_ALIAS_TTL_DAYS = 30
DEFAULT_UNLISTED_TTL_DAYS = 7

VALID_STATUSES = ("priced", "alias", "unlisted")

PRICE_REQUEST_CONTRACT = (
    "Resolve every entry in `unpriced_models` and write a JSON object "
    '`{"models": {"<key>": <resolution>, ...}}` to a local file, then run '
    "`agroute prices resolve --file <path> --json`. "
    "Each `<resolution>` must use one of three statuses:\n"
    '1. `"priced"`: official first-party provider list pricing in USD per 1M tokens across all six '
    "categories (`input`, `output`, `cache_read`, `cache_write`, `cache_write_5m`, `cache_write_1h`; "
    "for Anthropic set `cache_write` = `cache_write_5m`; for OpenAI set all three cache-write fields "
    "to the 30-minute write rate; for Google or listings without a cache-write fee set all three "
    "cache-write fields equal to `input`): "
    '`{"status": "priced", "provider": "<anthropic|openai|google|...>", "source": "<official URL>", '
    '"rates": {"input": ..., "output": ..., "cache_read": ..., "cache_write": ..., '
    '"cache_write_5m": ..., "cache_write_1h": ...}}` (default TTL: 30 days).\n'
    '2. `"alias"`: internal harness/orchestrator wrapper backed by a known catalog family key '
    '(produces a labelled `guess:` card): '
    '`{"status": "alias", "alias_of": "<catalog_key>", "reason": "<why this maps to alias_of>"}` '
    "(default TTL: 30 days).\n"
    '3. `"unlisted"`: internal/experimental model with no published token pricing and no clear catalog '
    "alias (negative cache entry so the model stays unpriced without re-prompting until expiry): "
    '`{"status": "unlisted", "reason": "<why unlisted>"}` (default TTL: 7 days).'
)


def snapshot_path(config) -> Path:
    return config.state_root / "prices.json"


def _validate_model_key(raw_key: str, where: str) -> str:
    key = text(raw_key, where, max_length=200)
    if "/" in key or ":" in key or key.strip() != key:
        raise AgRouteError(f"{where}: expected bare catalog key without agent prefix, got {raw_key!r}")
    return key


def _validate_entry(model: str, raw_entry: dict, where: str, *, default_now: datetime | None = None) -> dict:
    table(raw_entry, where)
    status = raw_entry.get("status")
    if status not in VALID_STATUSES:
        raise AgRouteError(f"{where}.status: expected one of {list(VALID_STATUSES)}, got {status!r}")

    optional_time = ["resolved_at", "expires_at"] if default_now is not None else []
    required_time = [] if default_now is not None else ["resolved_at", "expires_at"]

    if status == "priced":
        keys(raw_entry, ["status", "provider", "source", "rates", *required_time], optional_time, where=where)
    elif status == "alias":
        keys(raw_entry, ["status", "alias_of", "reason", *required_time], optional_time, where=where)
    else:
        keys(raw_entry, ["status", "reason", *required_time], optional_time, where=where)

    if raw_entry.get("resolved_at") is not None:
        resolved_dt = parse_timestamp(raw_entry["resolved_at"], f"{where}.resolved_at")
        resolved_iso = format_timestamp(resolved_dt)
    elif default_now is not None:
        resolved_dt = default_now
        resolved_iso = format_timestamp(resolved_dt)
    else:
        raise AgRouteError(f"{where}.resolved_at: required")

    if raw_entry.get("expires_at") is not None:
        expires_dt = parse_timestamp(raw_entry["expires_at"], f"{where}.expires_at")
        expires_iso = format_timestamp(expires_dt)
    elif default_now is not None:
        ttl_days = (
            DEFAULT_UNLISTED_TTL_DAYS
            if status == "unlisted"
            else DEFAULT_ALIAS_TTL_DAYS
            if status == "alias"
            else DEFAULT_PRICED_TTL_DAYS
        )
        expires_dt = resolved_dt + timedelta(days=ttl_days)
        expires_iso = format_timestamp(expires_dt)
    else:
        raise AgRouteError(f"{where}.expires_at: required")

    if expires_dt <= resolved_dt:
        raise AgRouteError(f"{where}.expires_at: must be strictly after resolved_at ({resolved_iso})")

    if status == "priced":
        provider = text(raw_entry["provider"], f"{where}.provider", max_length=64)
        source = text(raw_entry["source"], f"{where}.source", max_length=512)
        rates_raw = table(raw_entry["rates"], f"{where}.rates")
        keys(rates_raw, RATE_CATEGORIES, where=f"{where}.rates")
        rates = {
            cat: number(rates_raw[cat], f"{where}.rates.{cat}", nonnegative=True)
            for cat in RATE_CATEGORIES
        }
        return {
            "status": "priced",
            "provider": provider,
            "source": source,
            "resolved_at": resolved_iso,
            "expires_at": expires_iso,
            "resolved": resolved_dt,
            "expires": expires_dt,
            "rates": rates,
        }

    if status == "alias":
        alias_of = _validate_model_key(raw_entry["alias_of"], f"{where}.alias_of")
        if alias_of == model:
            raise AgRouteError(f"{where}.alias_of: model {model!r} cannot alias itself")
        reason = text(raw_entry["reason"], f"{where}.reason", max_length=512)
        return {
            "status": "alias",
            "alias_of": alias_of,
            "reason": reason,
            "resolved_at": resolved_iso,
            "expires_at": expires_iso,
            "resolved": resolved_dt,
            "expires": expires_dt,
        }

    reason = text(raw_entry["reason"], f"{where}.reason", max_length=512)
    return {
        "status": "unlisted",
        "reason": reason,
        "resolved_at": resolved_iso,
        "expires_at": expires_iso,
        "resolved": resolved_dt,
        "expires": expires_dt,
    }


def _strip_runtime_fields(entry: dict) -> dict:
    return {k: deepcopy(v) for k, v in entry.items() if k not in ("resolved", "expires")}


def _validate_alias_targets(models: dict[str, dict], where: str) -> None:
    """Verify every alias chain terminates at a known `_CATALOG` or `priced` model without cycles."""
    for start_key, entry in models.items():
        if entry["status"] != "alias":
            continue
        seen = {start_key}
        cur = entry["alias_of"]
        while True:
            if cur in seen:
                raise AgRouteError(f"{where}.{start_key}.alias_of: circular alias chain involving {cur!r}")
            seen.add(cur)
            target_entry = models.get(cur)
            if target_entry is not None:
                if target_entry["status"] == "priced":
                    break
                if target_entry["status"] == "alias":
                    cur = target_entry["alias_of"]
                    continue
                raise AgRouteError(
                    f"{where}.{start_key}.alias_of: target {cur!r} is marked 'unlisted' and has no price"
                )
            if cur in _CATALOG:
                break
            raise AgRouteError(
                f"{where}.{start_key}.alias_of: unknown target {cur!r} (expected a built-in catalog key or 'priced' entry)"
            )


def _validate_snapshot(raw: dict, path: Path) -> dict:
    where = f"price snapshot {path}"
    table(raw, where)
    keys(raw, ["format", "updated_at", "models"], where=where)
    if raw["format"] != SNAPSHOT_FORMAT:
        raise AgRouteError(f"{where}: unsupported format {raw['format']!r}")
    updated = parse_timestamp(raw["updated_at"], f"{where}.updated_at")
    models_raw = table(raw["models"], f"{where}.models")
    models: dict[str, dict] = {}
    for model_key, entry_raw in models_raw.items():
        key = _validate_model_key(model_key, f"{where}.models.{model_key}")
        models[key] = _validate_entry(key, entry_raw, f"{where}.models.{key}", default_now=None)
    _validate_alias_targets(models, f"{where}.models")
    return {
        "format": SNAPSHOT_FORMAT,
        "updated_at": format_timestamp(updated),
        "updated": updated,
        "models": models,
    }


def read_snapshot(config) -> dict | None:
    """Return the validated price snapshot at `<state_root>/prices.json`, or `None` if absent."""
    check(config)
    path = snapshot_path(config)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgRouteError(
            f"price snapshot: invalid JSON at {path}: {exc}; repair with agroute prices resolve --file <path>"
        ) from exc
    try:
        return _validate_snapshot(raw, path)
    except AgRouteError as exc:
        raise AgRouteError(f"{exc}; repair with agroute prices resolve --file <path>") from exc


@contextmanager
def _prices_locked(config):
    with identity_locked(config):
        config.state_root.mkdir(parents=True, exist_ok=True)
        with (config.state_root / "prices.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                check(config)
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def build_price_request(config, now_dt: datetime, pending_by_key: dict[str, dict]) -> dict | None:
    """Build an `agroute-price-request-v1` payload from collected pending model metadata, or None."""
    if not pending_by_key:
        return None
    unpriced_models = []
    for key in sorted(pending_by_key):
        info = pending_by_key[key]
        row = {
            "key": key,
            "agents": sorted(info["agents"]),
            "sample_models": sorted(info["sample_models"]),
            "display_names": list(dict.fromkeys(info["display_names"])),
        }
        if info.get("expired_at") is not None:
            row["expired_at"] = info["expired_at"]
        unpriced_models.append(row)
    return {
        "format": REQUEST_FORMAT,
        "evaluated_at": format_timestamp(now_dt),
        "state_path": str(snapshot_path(config)),
        "resolve_command": "agroute prices resolve --file <path> --json",
        "unpriced_models": unpriced_models,
        "contract": PRICE_REQUEST_CONTRACT,
    }


def resolve_prices(config, payload: dict, now_dt: datetime) -> dict:
    """Validate calling-agent price resolutions and persist `<state_root>/prices.json` atomically."""
    where = "price resolution"
    table(payload, where)
    keys(payload, ["models"], ["format"], where=where)
    if "format" in payload and payload["format"] not in ("agroute-price-resolution-v1", SNAPSHOT_FORMAT):
        raise AgRouteError(f"{where}.format: unsupported format {payload['format']!r}")
    models_in = table(payload["models"], f"{where}.models")
    if not models_in:
        raise AgRouteError(f"{where}.models: expected at least one model resolution")

    incoming: dict[str, dict] = {}
    for raw_key, raw_entry in models_in.items():
        key = _validate_model_key(raw_key, f"{where}.models.{raw_key}")
        incoming[key] = _validate_entry(key, raw_entry, f"{where}.models.{key}", default_now=now_dt)

    path = snapshot_path(config)
    updated_iso = format_timestamp(now_dt)
    with _prices_locked(config):
        try:
            before, previous_error = read_snapshot(config), None
        except AgRouteError as exc:
            before, previous_error = None, str(exc)

        merged_runtime: dict[str, dict] = {}
        if before is not None:
            merged_runtime.update(deepcopy(before["models"]))
        merged_runtime.update(incoming)
        _validate_alias_targets(merged_runtime, f"{where}.models")

        serializable = {
            "format": SNAPSHOT_FORMAT,
            "updated_at": updated_iso,
            "models": {k: _strip_runtime_fields(v) for k, v in sorted(merged_runtime.items())},
        }
        validated = _validate_snapshot(serializable, path)
        claim(config)
        atomic_write(path, json.dumps(serializable, indent=2, sort_keys=True) + "\n")

    resolved_summaries = {}
    for key in sorted(incoming):
        entry = validated["models"][key]
        effective = resolve_price(key, now_dt, validated)
        resolved_summaries[key] = {
            **_strip_runtime_fields(entry),
            "effective_rates": deepcopy(effective["rates"]) if effective is not None else None,
        }

    return {
        "format": RESOLVE_REPORT_FORMAT,
        "path": str(path),
        "updated_at": updated_iso,
        "previous_snapshot_error": previous_error,
        "resolved_models": sorted(incoming),
        "total_models": len(validated["models"]),
        "models": resolved_summaries,
    }


def show_prices(config, now_dt: datetime) -> dict:
    """Inspect `<state_root>/prices.json` with active/expired status annotations."""
    snap = read_snapshot(config)
    path = snapshot_path(config)
    if snap is None:
        return {
            "format": SHOW_FORMAT,
            "evaluated_at": format_timestamp(now_dt),
            "path": str(path),
            "exists": False,
            "updated_at": None,
            "models": {},
        }
    models_out = {}
    for key in sorted(snap["models"]):
        entry = snap["models"][key]
        active = entry["resolved"] <= now_dt < entry["expires"]
        expired = now_dt >= entry["expires"]
        eff = resolve_price(key, now_dt, snap) if active else None
        models_out[key] = {
            **_strip_runtime_fields(entry),
            "active": active,
            "expired": expired,
            "effective_rates": deepcopy(eff["rates"]) if eff is not None else None,
        }
    return {
        "format": SHOW_FORMAT,
        "evaluated_at": format_timestamp(now_dt),
        "path": str(path),
        "exists": True,
        "updated_at": snap["updated_at"],
        "models": models_out,
    }


def format_pending_report(report: dict) -> str:
    req = report.get("price_request")
    if req is None:
        return f"no unpriced discovered models (state: {report['state_path']})\n"
    lines = [
        f"pending price resolutions: {report['pending_count']} model(s) (state: {report['state_path']})"
    ]
    for item in req["unpriced_models"]:
        disp = f" [{', '.join(item['display_names'])}]" if item["display_names"] else ""
        exp = f" (expired {item['expired_at']})" if item.get("expired_at") else ""
        lines.append(
            f"  - {item['key']}: agents={','.join(item['agents'])} samples={','.join(item['sample_models'])}{disp}{exp}"
        )
    lines.append(f"resolve with: {req['resolve_command']}")
    return "\n".join(lines) + "\n"


def format_resolve_report(report: dict) -> str:
    lines = [
        f"resolved {len(report['resolved_models'])} model(s) in {report['path']} "
        f"(total stored: {report['total_models']}, updated {report['updated_at']})"
    ]
    for key in report["resolved_models"]:
        entry = report["models"][key]
        status = entry["status"]
        if status == "priced":
            rates = entry["rates"]
            lines.append(
                f"  priced {key}: input={rates['input']} output={rates['output']} "
                f"cache_read={rates['cache_read']} (expires {entry['expires_at']})"
            )
        elif status == "alias":
            lines.append(
                f"  alias {key} -> {entry['alias_of']}: {entry['reason']} (expires {entry['expires_at']})"
            )
        else:
            lines.append(f"  unlisted {key}: {entry['reason']} (expires {entry['expires_at']})")
    if report["previous_snapshot_error"]:
        lines.append(f"replaced unreadable snapshot: {report['previous_snapshot_error']}")
    return "\n".join(lines) + "\n"


def format_show_report(report: dict) -> str:
    if not report["exists"]:
        return f"no price snapshot at {report['path']}\n"
    lines = [
        f"price snapshot at {report['path']} ({len(report['models'])} model(s), updated {report['updated_at']})"
    ]
    for key, entry in report["models"].items():
        state_lbl = "expired" if entry["expired"] else ("active" if entry["active"] else "future")
        if entry["status"] == "priced":
            rates = entry["rates"]
            lines.append(
                f"  {key} [{entry['status']}, {state_lbl}]: input={rates['input']} output={rates['output']} "
                f"cache_read={rates['cache_read']} expires={entry['expires_at']}"
            )
        elif entry["status"] == "alias":
            lines.append(
                f"  {key} [alias -> {entry['alias_of']}, {state_lbl}]: {entry['reason']} expires={entry['expires_at']}"
            )
        else:
            lines.append(
                f"  {key} [unlisted, {state_lbl}]: {entry['reason']} expires={entry['expires_at']}"
            )
    return "\n".join(lines) + "\n"
