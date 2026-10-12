"""Loading game configs, game adapters and the .env file."""
import importlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES_DIR = ROOT / "games"


def load_dotenv(path: str | Path = ROOT / ".env") -> None:
    """Minimal .env reader (KEY=VALUE lines). Real environment variables win,
    so secrets can also be supplied by the OS or a service manager."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_stream_config() -> dict:
    return json.loads((ROOT / "config" / "stream.json").read_text(encoding="utf-8"))


def available_games() -> list[str]:
    return sorted(p.parent.name for p in GAMES_DIR.glob("*/config.json"))


def load_game_config(game: str) -> dict:
    path = GAMES_DIR / game / "config.json"
    if not path.is_file():
        raise FileNotFoundError(f"No config for game '{game}'. Available: {', '.join(available_games())}")
    config = json.loads(path.read_text(encoding="utf-8"))
    config["id"] = game
    # Relative paths in a game config are relative to the project root.
    if config.get("build_dir"):
        config["build_dir"] = str(ROOT / config["build_dir"])
    return config


def resolve_mode(config: dict, mode: str | None) -> tuple[str, dict]:
    name = mode or config.get("default_mode", "normal")
    modes = config.get("modes", {})
    if name not in modes:
        raise ValueError(f"Unknown mode '{name}' for {config['name']}. Available: {', '.join(modes)}")
    return name, modes[name]


def load_adapter_class(config: dict):
    """config["adapter"] is "package.module:ClassName"."""
    module_name, _, class_name = config["adapter"].partition(":")
    return getattr(importlib.import_module(module_name), class_name)
