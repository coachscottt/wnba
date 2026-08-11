"""Loads config.yaml — every tunable number lives there, not in code."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env() -> dict:
    """Read .env as KEY=value pairs; real environment variables override the
    file (that's how the GitHub Actions secret arrives — there is no .env on
    the runner). Values are secrets: never print or log them."""
    import os

    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    for key in ("ODDS_API_KEY",):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env
