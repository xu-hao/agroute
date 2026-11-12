"""Explicit `agroute prices refresh`: fetch OpenRouter's listing into a local price snapshot.

Routing never fetches. Only this command reads the network; candidates read the snapshot
it leaves at ``<state_root>/prices.json``. See docs/reference.md (refreshing prices).
"""

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import http.client
import json
import math
import time
import urllib.request

from . import __version__
from .files import atomic_write
from .pricing import (
    OPENROUTER_MODELS_URL,
    RATE_CATEGORIES,
    anthropic_rates,
    google_rates,
    openai_rates,
    resolve_price,
    unwritten_cache_rates,
)
from .state import check, claim, identity_locked
from .validation import AgRouteError, format_timestamp, keys, number, parse_timestamp, table, text

SNAPSHOT_FORMAT = "agroute-price-snapshot-v1"
REPORT_FORMAT = "agroute-price-refresh-v1"
ATTEMPTS = 3
TIMEOUT_SECONDS = 15.0
BACKOFF_SECONDS = (1.0, 2.0)

# Exact OpenRouter model ID -> (agroute catalog keys, category rule). Batch variants and
# every unlisted ID are ignored. The rules are the built-in catalog's per-provider rules.
OPENROUTER_MODELS = {
    "anthropic/claude-fable-5.2": (("claude-fable-5-2",), "anthropic"),
    "anthropic/claude-fable-5.1": (("claude-fable-5-1",), "anthropic"),
    "anthropic/claude-fable-5": (("claude-fable-5",), "anthropic"),
    "anthropic/claude-opus-5.5": (("claude-opus-5-5",), "anthropic"),
    "anthropic/claude-opus-5": (("claude-opus-5",), "anthropic"),
    "anthropic/claude-opus-4.8": (("claude-opus-4-8",), "anthropic"),
    "anthropic/claude-opus-4.7": (("claude-opus-4-7",), "anthropic"),
    "anthropic/claude-opus-4.6": (("claude-opus-4-6",), "anthropic"),
    "anthropic/claude-sonnet-5.5": (("claude-sonnet-5-5",), "anthropic"),
    "anthropic/claude-sonnet-5": (("claude-sonnet-5",), "anthropic"),
    "anthropic/claude-sonnet-4.6": (("claude-sonnet-4-6",), "anthropic"),
    "anthropic/claude-haiku-5.5": (("claude-haiku-5-5",), "anthropic-over-100k"),
    "anthropic/claude-haiku-4.5": (("claude-haiku-4-5", "claude-haiku-4-5-20251001"), "anthropic"),
    "openai/gpt-6-astra": (("gpt-6-astra",), "openai"),
    "openai/gpt-6-sol": (("gpt-6-sol",), "openai"),
    "openai/gpt-6.1-sol": (("gpt-6.1-sol",), "openai"),
    "openai/gpt-6.1-terra": (("gpt-6.1-terra",), "openai"),
    "openai/gpt-6-luna": (("gpt-6-luna",), "openai"),
    "google/gemini-4-argon": (("gemini-4-argon",), "google"),
    "google/gemini-3.8-flash": (("gemini-3.8-flash",), "google"),
    "google/gemini-3.5-flash-lite": (("gemini-3.5-flash-lite",), "google"),
    "openai/gpt-oss-120b": (("gpt-oss-120b",), "listing-without-writes"),
}

_RULE_FIELDS = {
    "anthropic": ("prompt", "completion", "input_cache_read", "input_cache_write", "input_cache_write_1h"),
    "anthropic-over-100k": ("prompt", "completion", "input_cache_read", "input_cache_write", "input_cache_write_1h"),
    "openai": ("prompt", "completion", "input_cache_read", "input_cache_write"),
    # Google's input_cache_write on OpenRouter is a storage-derived charge, not a write price.
    "google": ("prompt", "completion", "input_cache_read"),
    "listing-without-writes": ("prompt", "completion", "input_cache_read"),
}


class KeptBuiltin(Exception):
    """A listing lacks the tier the built-in catalog deliberately uses."""


def _http_get(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": f"agroute/{__version__}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _sleep(seconds):
    time.sleep(seconds)


def _per_mtok(value, where):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AgRouteError(f"{where}: expected a per-token USD price")
    try:
        amount = Decimal(str(value)) * 1_000_000
    except InvalidOperation as exc:
        raise AgRouteError(f"{where}: invalid per-token USD price {value!r}") from exc
    rate = float(amount)
    if not math.isfinite(rate) or rate < 0:
        raise AgRouteError(f"{where}: expected a non-negative per-token USD price")
    return rate


def _rates(model_id, pricing, rule):
    where = f"price source: {model_id}"
    if rule == "anthropic-over-100k":
        tiers = [t for t in pricing.get("overrides") or [] if isinstance(t, dict) and t.get("min_prompt_tokens") == 100000]
        if len(tiers) != 1:
            raise KeptBuiltin(f"{model_id} lists no single over-100K prompt tier")
        pricing, where = tiers[0], f"{where} over-100K tier"
    missing = [field for field in _RULE_FIELDS[rule] if field not in pricing]
    if missing:
        raise AgRouteError(f"{where}: missing price categories {missing}")
    price = {field: _per_mtok(pricing[field], f"{where}.{field}") for field in _RULE_FIELDS[rule]}
    base, output, cached = price["prompt"], price["completion"], price["input_cache_read"]
    if rule in ("anthropic", "anthropic-over-100k"):
        return anthropic_rates(base, output, cached, price["input_cache_write"], price["input_cache_write_1h"])
    if rule == "openai":
        return openai_rates(base, output, cached, price["input_cache_write"])
    if rule == "google":
        return google_rates(base, output, cached)
    return unwritten_cache_rates(base, output, cached)


def _parse_listing(body):
    try:
        listing = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise AgRouteError(f"price source: response is not JSON: {exc}") from exc
    data = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(data, list):
        raise AgRouteError("price source: response lacks a 'data' model list")
    by_id = {}
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in OPENROUTER_MODELS:
            if not isinstance(item.get("pricing"), dict):
                raise AgRouteError(f"price source: {item['id']} lacks a pricing object")
            by_id[item["id"]] = item["pricing"]
    missing = sorted(set(OPENROUTER_MODELS) - set(by_id))
    if missing:
        raise AgRouteError(f"price source: mapped models missing from the response: {missing}")
    models, kept = {}, []
    for model_id, (agroute_keys, rule) in OPENROUTER_MODELS.items():
        try:
            rates = _rates(model_id, by_id[model_id], rule)
        except KeptBuiltin as reason:
            kept.extend({"model": key, "reason": f"{reason}; the built-in card stays in use"} for key in agroute_keys)
            continue
        for key in agroute_keys:
            models[key] = {"openrouter_id": model_id, "rates": rates}
    return models, kept


def _fetch_with_retries():
    failures = []
    for attempt in range(1, ATTEMPTS + 1):
        try:
            body = _http_get(OPENROUTER_MODELS_URL, TIMEOUT_SECONDS)
            return body, _parse_listing(body)
        except (OSError, ValueError, http.client.HTTPException, AgRouteError) as exc:
            failures.append(f"attempt {attempt}: {exc}")
            if attempt < ATTEMPTS:
                _sleep(BACKOFF_SECONDS[attempt - 1])
    raise AgRouteError(
        f"prices refresh: failed after {ATTEMPTS} attempts to read {OPENROUTER_MODELS_URL}; "
        f"the previous snapshot is unchanged ({'; '.join(failures)})"
    )


def snapshot_path(config):
    return config.state_root / "prices.json"


def _validate_snapshot(raw, path):
    where = f"price snapshot {path}"
    keys(raw, ["format", "source", "fetched_at", "version", "content_sha256", "models"], where=where)
    if raw["format"] != SNAPSHOT_FORMAT:
        raise AgRouteError(f"{where}: unsupported format {raw['format']!r}")
    for key in ("source", "version", "content_sha256"):
        text(raw[key], f"{where}.{key}")
    fetched = parse_timestamp(raw["fetched_at"], f"{where}.fetched_at")
    models = {}
    for model, entry in table(raw["models"], f"{where}.models").items():
        keys(entry, ["openrouter_id", "rates"], where=f"{where}.models.{model}")
        text(entry["openrouter_id"], f"{where}.models.{model}.openrouter_id")
        rates = keys(table(entry["rates"], f"{where}.models.{model}.rates"), RATE_CATEGORIES,
                     where=f"{where}.models.{model}.rates")
        models[model] = {"openrouter_id": entry["openrouter_id"],
                         "rates": {c: number(rates[c], f"{where}.models.{model}.rates.{c}", nonnegative=True)
                                   for c in RATE_CATEGORIES}}
    return {"source": raw["source"], "fetched_at": raw["fetched_at"], "fetched": fetched,
            "version": raw["version"], "content_sha256": raw["content_sha256"], "models": models}


def read_snapshot(config):
    """The validated refreshed price snapshot, or ``None`` when none has been written."""
    check(config)
    path = snapshot_path(config)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgRouteError(f"price snapshot: invalid JSON at {path}: {exc}; rerun agroute prices refresh") from exc
    return _validate_snapshot(raw, path)


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


def _diff(keys_, now, before, after):
    changes, unchanged = [], []
    for key in keys_:
        old, new = resolve_price(key, now, before), resolve_price(key, now, after)
        old_rates = old["rates"] if old else None
        new_rates = new["rates"] if new else None
        row = {"model": key, "old_version": old["version"] if old else None,
               "new_version": new["version"] if new else None}
        if old_rates == new_rates:
            unchanged.append(row)
            continue
        row["status"] = "added" if old is None else "dropped" if new is None else "changed"
        row["categories"] = {
            category: {"old": old_rates[category] if old else None, "new": new_rates[category] if new else None}
            for category in RATE_CATEGORIES
            if (old_rates or {}).get(category) != (new_rates or {}).get(category)
        }
        changes.append(row)
    return changes, unchanged


def refresh(config, now):
    """Fetch, map and atomically store a new snapshot; report the effective price drift."""
    body, (models, kept) = _fetch_with_retries()
    fetched_at = now.isoformat()
    snapshot = {
        "format": SNAPSHOT_FORMAT,
        "source": OPENROUTER_MODELS_URL,
        "fetched_at": fetched_at,
        "version": f"openrouter-{format_timestamp(now)}",
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "models": models,
    }
    path = snapshot_path(config)
    with _prices_locked(config):
        try:
            before, previous_error = read_snapshot(config), None
        except AgRouteError as exc:
            # A refresh is how an unreadable snapshot is repaired; the report names it.
            before, previous_error = None, str(exc)
        after = _validate_snapshot(snapshot, path)
        claim(config)
        atomic_write(path, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    compared = sorted(set(models) | set(before["models"] if before else ()) | {k["model"] for k in kept})
    changes, unchanged = _diff(compared, now, before, after)
    return {
        "format": REPORT_FORMAT,
        "path": str(path),
        "source": OPENROUTER_MODELS_URL,
        "fetched_at": fetched_at,
        "version": snapshot["version"],
        "content_sha256": snapshot["content_sha256"],
        "previous_fetched_at": before["fetched_at"] if before else None,
        "previous_snapshot_error": previous_error,
        "models": len(models),
        "changes": changes,
        "unchanged": [row["model"] for row in unchanged],
        "kept_builtin": kept,
    }


def format_report(report):
    lines = [
        f"price snapshot {report['version']} written to {report['path']} "
        f"({report['models']} models, previous fetch {report['previous_fetched_at'] or 'none'})"
    ]
    for row in report["changes"]:
        cats = ", ".join(f"{c} {v['old']} -> {v['new']}" for c, v in row["categories"].items())
        lines.append(f"{row['status']} {row['model']}: {cats} ({row['old_version']} -> {row['new_version']})")
    if report["previous_snapshot_error"]:
        lines.append(f"replaced unreadable snapshot: {report['previous_snapshot_error']}")
    for row in report["kept_builtin"]:
        lines.append(f"kept {row['model']}: {row['reason']}")
    lines.append(f"unchanged: {len(report['unchanged'])} models")
    return "\n".join(lines) + "\n"
