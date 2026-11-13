"""Per-token rate cards for emitted candidates: built-in catalog, resolved price store, user prices.

The built-in catalog is a hardcoded snapshot of published list prices, verified against each
provider's pricing page at ``VERIFIED_AT`` and updated in this package when prices are
re-checked. A price in force at verification takes effect at ``VERIFIED_AT``; a published
future change takes effect at its own start. Unknown or newer discovered models are resolved
offline by the calling agent into ``<state_root>/prices.json``. Every card follows the
``howhard-rate-card-v1`` contract: USD per million tokens in six categories.
"""

from copy import deepcopy

from .validation import format_timestamp, parse_timestamp

RATE_CARD_FORMAT = "howhard-rate-card-v1"
RATE_CATEGORIES = ("input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h")
VERIFIED_AT = "2026-11-12T02:18:20+00:00"
GUESS_VERSION = "agroute-guess-2026-11-12"
USER_SOURCE = "user config"
USER_VERSION = "user-config"

_ANTHROPIC = {
    "provider": "anthropic",
    "source": "https://platform.claude.com/docs/en/about-claude/pricing",
    "version": "anthropic-list-2026-11-12",
}
_OPENAI = {
    "provider": "openai",
    "source": "https://developers.openai.com/api/docs/pricing",
    "version": "openai-list-2026-11-12",
}
_GOOGLE = {
    "provider": "google",
    "source": "https://cloud.google.com/vertex-ai/generative-ai/pricing",
    "version": "google-cloud-list-2026-11-12",
}
_OPENAI_OSS = {
    "provider": "openai-oss",
    "source": "published open-weight market estimate (gpt-oss-120b)",
    "version": "openai-oss-estimate-2026-11-12",
}


def anthropic_rates(base, output, cache_hit, write_5m, write_1h):
    """An unlabelled cache write is Anthropic's default 5-minute write."""
    return {
        "input": base,
        "output": output,
        "cache_read": cache_hit,
        "cache_write": write_5m,
        "cache_write_5m": write_5m,
        "cache_write_1h": write_1h,
    }


def openai_rates(base, output, cached_input, write_30m):
    """Codex writes at OpenAI's default 30-minute lifetime, and howhard counts every Codex
    write as ``cache_write``; the optional 12-hour write is not represented."""
    return {
        "input": base,
        "output": output,
        "cache_read": cached_input,
        "cache_write": write_30m,
        "cache_write_5m": write_30m,
        "cache_write_1h": write_30m,
    }


def google_rates(base, output, cached_input):
    """Implicit caching bills writes as ordinary input; hourly storage is excluded."""
    return {
        "input": base,
        "output": output,
        "cache_read": cached_input,
        "cache_write": base,
        "cache_write_5m": base,
        "cache_write_1h": base,
    }


def unwritten_cache_rates(base, output, cached_input):
    """A listing without a cache-write price bills writes as ordinary input."""
    return google_rates(base, output, cached_input)


def _entry(source, *periods):
    timed = []
    for start, rates in periods:
        effective_at = max((VERIFIED_AT, start), key=lambda value: parse_timestamp(value, "rate card effective_at"))
        timed.append((parse_timestamp(effective_at, "rate card effective_at"), effective_at, rates))
    return {**source, "periods": timed}


_CATALOG = {
    "claude-fable-5-2": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(8, 40, 0.20, 10, 16))),
    "claude-mythos-5-2": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(8, 40, 0.20, 10, 16))),
    "claude-fable-5-1": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(10, 50, 0.25, 12.50, 20))),
    "claude-mythos-5-1": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(10, 50, 0.25, 12.50, 20))),
    "claude-fable-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(10, 50, 1, 12.50, 20))),
    "claude-mythos-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(10, 50, 1, 12.50, 20))),
    "claude-opus-5-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(4, 20, 0.20, 5, 8))),
    "claude-opus-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(5, 25, 0.50, 6.25, 10))),
    "claude-opus-4-8": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(5, 25, 0.50, 6.25, 10))),
    "claude-opus-4-7": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(5, 25, 0.50, 6.25, 10))),
    "claude-opus-4-6": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(5, 25, 0.50, 6.25, 10))),
    "claude-opus-4-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(5, 25, 0.50, 6.25, 10))),
    "claude-sonnet-5-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(2, 10, 0.10, 2.50, 4))),
    "claude-sonnet-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(2, 10, 0.20, 2.50, 4))),
    "claude-sonnet-4-6": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(3, 15, 0.30, 3.75, 6))),
    "claude-sonnet-4-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(3, 15, 0.30, 3.75, 6))),
    # The "prompts over 100,000 tokens" row: agent sessions routinely exceed that size.
    "claude-haiku-5-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(0.50, 2.50, 0.05, 0.625, 1))),
    "claude-haiku-4-5": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(1, 5, 0.10, 1.25, 2))),
    "claude-haiku-4-5-20251001": _entry(_ANTHROPIC, (VERIFIED_AT, anthropic_rates(1, 5, 0.10, 1.25, 2))),
    # Standard short-context rows (prompts up to 272K input tokens).
    "gpt-6-astra": _entry(_OPENAI, (VERIFIED_AT, openai_rates(10.00, 50.00, 1.00, 12.50))),
    "gpt-6-sol": _entry(_OPENAI, (VERIFIED_AT, openai_rates(2.00, 10.00, 0.20, 2.50))),
    "gpt-6.1-sol": _entry(_OPENAI, (VERIFIED_AT, openai_rates(2.00, 10.00, 0.10, 2.50))),
    "gpt-6.1-terra": _entry(_OPENAI, (VERIFIED_AT, openai_rates(1.00, 5.00, 0.05, 1.25))),
    "gpt-6-luna": _entry(_OPENAI, (VERIFIED_AT, openai_rates(0.10, 0.50, 0.01, 0.125))),
    # Google Cloud Global Standard rates, with published scheduled changes.
    "gemini-4-hydrogen": _entry(_GOOGLE, (VERIFIED_AT, google_rates(1.50, 7.50, 0.075))),
    "gemini-4-carbon": _entry(
        _GOOGLE,
        (VERIFIED_AT, google_rates(2.00, 10.00, 0.10)),
        ("2026-12-01T00:00:00+00:00", google_rates(4.00, 20.00, 0.20)),
    ),
    "gemini-4-argon": _entry(
        _GOOGLE,
        (VERIFIED_AT, google_rates(2.00, 10.00, 0.10)),
        ("2026-12-01T00:00:00+00:00", google_rates(4.00, 20.00, 0.20)),
    ),
    "gemini-3.8-flash": _entry(
        _GOOGLE,
        (VERIFIED_AT, google_rates(0.75, 3.75, 0.075)),
        ("2027-01-01T00:00:00+00:00", google_rates(1.50, 7.50, 0.15)),
    ),
    "gemini-3.7-flash": _entry(
        _GOOGLE,
        (VERIFIED_AT, google_rates(0.75, 3.75, 0.075)),
        ("2027-01-01T00:00:00+00:00", google_rates(1.50, 7.50, 0.15)),
    ),
    "gemini-3.6-flash": _entry(
        _GOOGLE,
        (VERIFIED_AT, google_rates(0.75, 3.75, 0.075)),
        ("2027-01-01T00:00:00+00:00", google_rates(1.50, 7.50, 0.15)),
    ),
    "gemini-3.5-flash-lite": _entry(_GOOGLE, (VERIFIED_AT, google_rates(0.30, 2.50, 0.03))),
    "gemini-3.1-pro": _entry(_GOOGLE, (VERIFIED_AT, google_rates(2.00, 12.00, 0.20))),
    # Published open-weight market estimate when no first-party list price is published.
    "gpt-oss-120b": _entry(_OPENAI_OSS, (VERIFIED_AT, unwritten_cache_rates(0.15, 0.60, 0.075))),
}

# Antigravity's native subagent tiers name no concrete model. Each exact `agy:<tier>` key
# carries a labelled guess priced as one catalog model.
_TIER_GUESSES = {
    "agy:pro": ("gemini-4-argon", "the Antigravity pro tier"),
    "agy:flash": ("gemini-3.8-flash", "the Antigravity flash tier"),
    "agy:flash_lite": ("gemini-3.5-flash-lite", "the Antigravity flash_lite tier"),
}


def _builtin(key, at):
    entry = _CATALOG.get(key)
    if entry is None:
        return None
    in_effect = [period for period in entry["periods"] if period[0] <= at]
    if not in_effect:
        return None
    effective, effective_at, rates = max(in_effect, key=lambda period: period[0])
    return {
        "provider": entry["provider"],
        "source": entry["source"],
        "version": entry["version"],
        "effective": effective,
        "effective_at": effective_at,
        "rates": rates,
    }


def _active_snapshot_entry(key, at, snapshot):
    if snapshot is None:
        return None
    entry = snapshot["models"].get(key)
    if entry is None:
        return None
    if entry["resolved"] > at or at >= entry["expires"]:
        return None
    return entry


def resolve_price(key, at, snapshot=None, *, _seen=()):
    """Resolve catalog ``key`` at ``at`` from ``prices.json`` (non-expired) and ``_CATALOG``."""
    if key is None or key in _seen:
        return None
    if key in _TIER_GUESSES:
        target, subject = _TIER_GUESSES[key]
        base = resolve_price(target, at, snapshot, _seen=(*_seen, key))
        if base is None:
            return None
        return {
            **base,
            "version": GUESS_VERSION,
            "source": f"guess: no published price for {subject}; priced as {target} from {base['version']}",
        }

    builtin = _builtin(key, at)
    entry = _active_snapshot_entry(key, at, snapshot)
    if entry is None:
        return builtin

    status = entry["status"]
    if status == "unlisted":
        return None
    if status == "alias":
        target = entry["alias_of"]
        base = resolve_price(target, at, snapshot, _seen=(*_seen, key))
        if base is None:
            return None
        return {
            **base,
            "version": GUESS_VERSION,
            "source": f"guess: {entry['reason']}; priced as {target} from {base['version']}",
        }

    resolved_card = {
        "provider": entry["provider"],
        "source": entry["source"],
        "version": f"resolved-{format_timestamp(entry['resolved'])}",
        "effective": entry["resolved"],
        "effective_at": entry["resolved_at"],
        "rates": entry["rates"],
    }
    if builtin is None or resolved_card["effective"] > builtin["effective"]:
        return resolved_card
    return builtin


def needs_price_resolution(key, at, snapshot=None, *, _seen=()):
    """Return True when a discovered model key lacks a built-in/active entry or has an expired entry."""
    if not key or key.startswith("agy:") or key in _seen:
        return False
    if snapshot is not None and key in snapshot["models"]:
        raw_entry = snapshot["models"][key]
        if at >= raw_entry["expires"]:
            return True
        if raw_entry["resolved"] <= at:
            if raw_entry["status"] in ("priced", "unlisted"):
                return False
            if raw_entry["status"] == "alias":
                return resolve_price(key, at, snapshot, _seen=_seen) is None
    return _builtin(key, at) is None


def rate_card(price, model):
    """A howhard-rate-card-v1 card for a resolved ``price``, labelled with the emitted ``model``."""
    return {
        "format": RATE_CARD_FORMAT,
        "source": price["source"],
        "version": price["version"],
        "effective_at": price["effective_at"],
        "provider": price["provider"],
        "model": model,
        "service_tier": "standard",
        "basis": "estimate",
        "rates": deepcopy(price["rates"]),
    }


def user_rate_card(rates, agent, model, at):
    """A card for a per-model user price, in effect at the evaluation time ``at``."""
    return rate_card(
        {
            "provider": agent,
            "source": USER_SOURCE,
            "version": USER_VERSION,
            "effective_at": at.isoformat(),
            "rates": rates,
        },
        model,
    )
