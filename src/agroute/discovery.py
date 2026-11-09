"""Automatic local model discovery for Codex, Claude Code, and Antigravity."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .files import atomic_write

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\r")
_MODEL_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{1,120}$")
_PAREN_EFFORT = re.compile(r"\(([A-Za-z0-9_-]+)\)\s*$")
_EFFORT_SUFFIXES = ("low", "medium", "high", "xhigh", "max", "ultra", "standard")

AGY_NATIVE_SUBAGENT_CANDIDATES = [
    {"model": "pro", "effort": "standard", "tier": "pro"},
    {"model": "flash", "effort": "standard", "tier": "flash"},
    {"model": "flash_lite", "effort": "standard", "tier": "flash_lite"},
]

_AGY_CACHE_TTL_SECONDS = 3600.0
_MEM_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def _claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".claude").resolve()


def _agy_cache_file() -> Path | None:
    if os.environ.get("AGROUTE_DISABLE_SPOOL"):
        return None
    env = os.environ.get("AGROUTE_SPOOL_DIR")
    if env:
        return Path(env).expanduser().resolve() / "agy_models_cache.json"
    if os.environ.get("AGROUTE_STATE_DIR"):
        return None
    base = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agroute"
    ).resolve()
    return base / "agy_models_cache.json"


def _ordered_efforts(supported: list[str], default_effort: str | None = None) -> list[str]:
    """Keep every declared effort, with known levels first and extensions in catalog order."""
    if not supported:
        return [default_effort or "medium"]
    canonical = ["low", "medium", "high", "xhigh", "max"]
    return list(dict.fromkeys([e for e in canonical if e in supported] + supported))


def _codex_generation(model: dict) -> int | None:
    """Read a numeric generation from the slug, without assuming a model family prefix."""
    match = re.search(r"(?:^|[-_])(\d+)(?:[._-]|$)", model["slug"])
    return int(match.group(1)) if match else None


def discover_codex_candidates(*, codex_home: Path | None = None) -> list[dict]:
    """Discover available Codex models and supported reasoning efforts from models_cache.json."""
    home = codex_home or _codex_home()
    cache_path = home / "models_cache.json"
    if not cache_path.is_file():
        return []
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    raw_models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return []

    visible = [
        m
        for m in raw_models
        if isinstance(m, dict)
        and isinstance(m.get("slug"), str)
        and m.get("visibility", "list") != "hide"
    ]
    if not visible:
        return []

    visible.sort(key=lambda m: (m.get("priority", 9999), str(m.get("slug", ""))))
    generations = [_codex_generation(m) for m in visible]
    newest = max((g for g in generations if g is not None), default=None)
    # Opaque slugs without a generation cannot be identified as older models.
    selected_models = [m for m, g in zip(visible, generations) if g is None or g == newest]

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in selected_models:
        slug = str(m["slug"]).strip()
        levels_raw = m.get("supported_reasoning_levels")
        supported: list[str] = []
        if isinstance(levels_raw, list):
            for entry in levels_raw:
                if isinstance(entry, dict) and isinstance(entry.get("effort"), str):
                    supported.append(entry["effort"])
                elif isinstance(entry, str):
                    supported.append(entry)
        default_eff = m.get("default_reasoning_level") if isinstance(m.get("default_reasoning_level"), str) else None
        for eff in _ordered_efforts(supported, default_eff):
            pair = (slug, eff)
            if pair not in seen:
                seen.add(pair)
                candidates.append({"model": slug, "effort": eff})
    return candidates


def _infer_claude_tier(model_entry: dict) -> str | None:
    runtime = model_entry.get("runtime")
    if isinstance(runtime, dict):
        family = runtime.get("family")
        if isinstance(family, str) and family.strip():
            return family.strip().lower()
    short_name = model_entry.get("short_name")
    if isinstance(short_name, str) and short_name.strip():
        first = short_name.strip().split()[0].lower()
        if first.isalpha():
            return first
    model_id = str(model_entry.get("id") or "").strip().lower()
    parts = [p for p in model_id.split("-") if p.isalpha() and p != "claude"]
    return parts[0] if parts else None


def discover_claude_candidates(*, claude_home: Path | None = None) -> list[dict]:
    """Discover available Claude Code models and effort levels from ~/.claude/cache/model-catalog/."""
    home = claude_home or _claude_home()
    catalog_dir = home / "cache" / "model-catalog"
    if not catalog_dir.is_dir():
        return []
    try:
        files = sorted(
            catalog_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return []

    raw_models: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        models = (
            ((data.get("catalog") or {}).get("config") or {}).get("models")
            if isinstance(data, dict)
            else None
        )
        if isinstance(models, list) and models:
            raw_models = [m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)]
            if raw_models:
                break
    if not raw_models:
        return []

    main_models = [m for m in raw_models if m.get("section") == "main" or m.get("quick_select") is True]
    selected_models = main_models or raw_models

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in selected_models:
        model_id = str(m["id"]).strip()
        runtime = m.get("runtime") if isinstance(m.get("runtime"), dict) else {}
        supported = [
            str(e)
            for e in (runtime.get("effort_levels") or [])
            if isinstance(e, str) and e
        ]
        default_eff = runtime.get("default_effort") if isinstance(runtime.get("default_effort"), str) else "medium"
        tier = _infer_claude_tier(m)
        efforts = _ordered_efforts(supported, default_eff)
        for eff in efforts:
            pair = (model_id, eff)
            if pair not in seen:
                seen.add(pair)
                cell: dict = {"model": model_id, "effort": eff}
                if tier:
                    cell["tier"] = tier
                candidates.append(cell)
    return candidates


def parse_agy_models_output(raw_text: str) -> list[dict]:
    """Parse `agy models` output into candidate cells without assuming static model prefixes."""
    cleaned = _ANSI_ESCAPE.sub("\n", raw_text or "")
    entries: list[tuple[str, str]] = []
    seen_slugs: set[str] = set()
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("fetching "):
            continue
        parts = stripped.split(None, 1)
        if not parts:
            continue
        first = parts[0].strip()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if _MODEL_SLUG.match(first) and ("-" in first or "." in first):
            if first not in seen_slugs:
                seen_slugs.add(first)
                entries.append((first, rest))

    candidates: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for slug, display in entries:
        effort = "medium"
        paren = _PAREN_EFFORT.search(display)
        if paren and paren.group(1).lower() in _EFFORT_SUFFIXES:
            effort = paren.group(1).lower()
        else:
            for suffix in _EFFORT_SUFFIXES:
                if slug.endswith(f"-{suffix}"):
                    effort = suffix
                    break
        lower_slug = slug.lower()
        slug_parts = set(re.split(r"[-.]", lower_slug))
        if "flash_lite" in lower_slug or ("flash" in slug_parts and "lite" in slug_parts) or "lite" in slug_parts:
            tier = "flash_lite"
        elif "flash" in slug_parts:
            tier = "flash"
        else:
            tier = "pro"
        pair = (slug, effort)
        if pair not in seen:
            seen.add(pair)
            candidates.append({"model": slug, "effort": effort, "tier": tier})
    return candidates


def discover_agy_cli_candidates(*, refresh: bool = False, timeout: float = 15.0) -> list[dict]:
    """Discover available Antigravity CLI models via `agy models` with local caching."""
    now = time.time()
    cache_file = _agy_cache_file()
    mem_key = str(cache_file or "agy_cli")
    if not refresh and mem_key in _MEM_CACHE:
        ts, cached_list = _MEM_CACHE[mem_key]
        if now - ts < _AGY_CACHE_TTL_SECONDS and cached_list:
            return deepcopy(cached_list)

    if not refresh and cache_file is not None and cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8", errors="replace"))
            if (
                isinstance(data, dict)
                and isinstance(data.get("fetched_at"), (int, float))
                and now - float(data["fetched_at"]) < _AGY_CACHE_TTL_SECONDS
                and isinstance(data.get("candidates"), list)
                and data["candidates"]
            ):
                _MEM_CACHE[mem_key] = (float(data["fetched_at"]), data["candidates"])
                return deepcopy(data["candidates"])
        except (OSError, ValueError):
            pass

    if shutil.which("agy") is None:
        return []
    try:
        proc = subprocess.run(
            ["agy", "models"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    candidates = parse_agy_models_output((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if candidates:
        _MEM_CACHE[mem_key] = (now, deepcopy(candidates))
        if cache_file is not None:
            try:
                atomic_write(
                    cache_file,
                    json.dumps(
                        {
                            "format": "agroute-agy-models-cache-v1",
                            "fetched_at": now,
                            "candidates": candidates,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                )
            except OSError:
                pass
    return candidates


def discover_agent_candidates(
    agent: str,
    surface: str,
    *,
    refresh: bool = False,
    codex_home: Path | None = None,
    claude_home: Path | None = None,
) -> list[dict]:
    """Return dynamically discovered candidates for (agent, surface)."""
    from .adapters import get_adapter

    try:
        adapter = get_adapter(agent)
    except Exception:
        return []
    return adapter.discover_candidates(
        surface,
        refresh=refresh,
        codex_home=codex_home,
        claude_home=claude_home,
    )
