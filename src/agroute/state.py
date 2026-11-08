"""Project capture and durable project state have independent roots."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess

from .files import atomic_write
from .validation import AgRouteError

KNOWN_STORE_ENTRIES = {
    "usage.json",
    "usage.lock",
    "plans.jsonl",
    "store.json",
    "project.json",
    "project.lock",
}


def git_path(root, option):
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), "rev-parse", option],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (root / result.stdout.strip()).resolve() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def validate_store(path):
    if not path.exists():
        return
    if not path.is_dir():
        raise AgRouteError(f"state: expected a directory at {path}")
    names = {entry.name for entry in path.iterdir()}
    if names and not (names & KNOWN_STORE_ENTRIES):
        raise AgRouteError(f"state: refusing unrelated existing directory {path}")
    if "usage.json" in names and "store.json" not in names:
        raise AgRouteError(f"state: refusing unclaimed usage store at {path}")


def read_identity(path):
    if not path.exists():
        return ""
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError) as exc:
        raise AgRouteError(f"state: invalid project identity at {path}") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "id"}
        or value["format"] != "agroute-project-v1"
        or not isinstance(value["id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["id"])
    ):
        raise AgRouteError(f"state: invalid project identity at {path}")
    return value["id"]


def roots(project, *, explicit=False, state_directory=None):
    project = Path(project).resolve()
    common = git_path(project, "--git-common-dir")
    if not explicit:
        checkout = git_path(project, "--show-toplevel")
        if checkout:
            project = checkout
    identity_path = common / "agroute-project.json" if common else project / ".agroute/project.json"
    env_state = os.environ.get("AGROUTE_STATE_DIR")
    chosen_state = state_directory if state_directory is not None else env_state
    if chosen_state is not None:
        store = Path(chosen_state).expanduser().resolve()
    elif explicit:
        store = project / ".agroute"
    else:
        store = common / "agroute" if common else project / ".agroute"
    store = store.resolve()
    if project.is_relative_to(store):
        raise AgRouteError("state: directory must not equal or contain the project capture root")
    if (not explicit or chosen_state is not None) and store.exists():
        validate_store(store)
    return project, store, identity_path, read_identity(identity_path)


def check(config):
    identity = read_identity(config.identity_path)
    if config.project_identity and config.project_identity != identity:
        raise AgRouteError("state: this project's identity changed or is missing")
    config.project_identity = identity
    path = config.state_root / "store.json"
    if path.exists():
        try:
            existing = json.loads(path.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise AgRouteError(f"state: invalid store identity at {path}") from exc
        if not identity or existing != {"format": "agroute-store-v1", "project": identity}:
            raise AgRouteError(
                f"state: store belongs to another project or lacks this project's identity: {config.state_root}"
            )


@contextmanager
def identity_locked(config):
    check(config)
    path = config.identity_path.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            check(config)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def claim(config):
    """Called after data validation, under both project identity and storage locks."""
    check(config)
    if not config.project_identity:
        config.project_identity = secrets.token_hex(16)
        atomic_write(
            config.identity_path,
            json.dumps({"format": "agroute-project-v1", "id": config.project_identity}, sort_keys=True) + "\n",
        )
    path = config.state_root / "store.json"
    if not path.exists():
        atomic_write(
            path,
            json.dumps({"format": "agroute-store-v1", "project": config.project_identity}, sort_keys=True) + "\n",
        )
