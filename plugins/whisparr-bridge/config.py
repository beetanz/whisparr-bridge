import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomli
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      field_validator)
from stashapi import log as stash_log
from stashapi.stashapp import StashInterface


# =========================
# Plugin Configuration
# =========================
class PluginConfig(BaseModel):
    # Core
    WHISPARR_URL: str
    WHISPARR_KEY: str
    STASHDB_ENDPOINT_SUBSTR: str = "stashdb.org"

    # Behavior
    MONITORED: bool = True
    MOVE_FILES: bool = False
    WHISPARR_RENAME: bool = True
    QUALITY_PROFILE: str = "Any"
    ROOT_FOLDER: Optional[Path] = None
    IGNORE_TAGS: List[str] = Field(default_factory=list)
    DEV_MODE: bool = False

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE_ENABLE: bool = False
    LOG_FILE_LEVEL: str = "DEBUG"
    LOG_FILE_LOCATION: Path = Path("./logs")
    LOG_FILE_TYPE: str = "SINGLE-FILE"
    LOG_FILE_MAX_BYTES: int = 5_000_000
    LOG_FILE_BACKUP_COUNT: int = 3
    LOG_FILE_ROTATE_WHEN: str = "midnight"
    LOG_FILE_USE_COLOR: bool = False
    LOG_CONSOLE_ENABLE: bool = True

    PATH_MAPPING: Dict[str, str] = Field(default_factory=dict)

    # Limits
    MAX_LOG_BODY: int = 1000
    MAX_PATH_LENGTH: int = 100

    model_config = ConfigDict(extra="ignore")

    # ----------------------
    # Validators
    # ----------------------
    @field_validator("IGNORE_TAGS", mode="before")
    @classmethod
    def normalize_ignore_tags(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [str(tag) for tag in parsed]
            except json.JSONDecodeError:
                return [t.strip() for t in v.split(",") if t.strip()]
        return list(v)

    @field_validator("LOG_FILE_LOCATION", "ROOT_FOLDER", mode="before")
    @classmethod
    def normalize_paths(cls, v):
        if v in ("", None):
            return None
        return Path(v).expanduser().resolve()

    @field_validator("WHISPARR_URL", "WHISPARR_KEY", mode="before")
    @classmethod
    def not_empty(cls, v: str):
        if not v or not str(v).strip():
            raise ValueError("must not be empty")
        return v.strip()


# =========================
# Config Loaders
# =========================
def load_from_toml(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)


def load_plugin_config(
    toml_path: str = "config.toml",
    stash: Optional[dict] = None,
) -> PluginConfig:

    merged: dict = {}

    # ---- TOML ----
    path = Path(toml_path).expanduser().resolve(strict=False)
    if path.is_file():
        try:
            with path.open("rb") as f:
                merged.update(tomli.load(f))
            stash_log.info(f"Configuration loaded from {toml_path}")
        except Exception as e:
            stash_log.error(f"Failed to load config from TOML: {e}")
            raise
    else:
        stash_log.info(f"Config file {toml_path} not found.")

    # ---- STASH UI ----
    if stash:
        stash_api = StashInterface(stash["server_connection"])
        try:
            stash_config = stash_api.get_configuration()
            plugin_cfg = stash_config.get("plugins", {}).get("whisparr-bridge", {})
            stash_log.debug(f"SettingsFromUI: {plugin_cfg}")
            merged.update(plugin_cfg)
        except Exception as e:
            stash_log.error(f"Failed to load Stash plugin settings: {e}")

    # ---- VALIDATE ONCE ----
    try:
        config = PluginConfig.model_validate(merged)
    except ValidationError as e:
        stash_log.error(f"Configuration validation failed: {e}")
        raise

    # ---- FINAL CHECKS ----
    if not config.WHISPARR_URL or not config.WHISPARR_KEY:
        stash_log.error("Whisparr URL and API key must be set in config.")
        raise ValueError("Missing critical Whisparr configuration fields")

    # if config.DEV_MODE:
    stash_log.debug(f"Config Loaded as {safe_json_preview(config)}")

    return config


# =========================
# Helpers (config-aware)
# =========================
CONFIG: Optional[PluginConfig] = None


def truncate_path(p: Path) -> str:
    s = str(p)
    if CONFIG is None:
        return s if len(s) <= 100 else f"...{s[-97:]}"
    return (
        s
        if len(s) <= CONFIG.MAX_PATH_LENGTH
        else f"...{s[-(CONFIG.MAX_PATH_LENGTH-3):]}"
    )


def safe_json_preview(data: Any) -> str:
    max_len = CONFIG.MAX_LOG_BODY if CONFIG else 1000
    try:
        if isinstance(data, dict):
            redacted = dict(data)
            for k in ("apiKey", "X-Api-Key", "apikey", "WHISPARR_KEY"):
                if k in redacted:
                    redacted[k] = "***REDACTED***"
            text = json.dumps(redacted, default=str)
        else:
            text = json.dumps(data, default=str)
        return text if len(text) <= max_len else text[:max_len] + "...(truncated)"
    except TypeError:
        return "<unserializable>"


# =========================
# Logging
# =========================
def switch_scene_log(logger: logging.Logger, scene_id: int):
    """Switch the log file to a new scene-specific file."""
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            base_dir = Path(handler.baseFilename).parent
            new_file = base_dir / f"scene_{scene_id}.log"
            # Close current file and reassign
            handler.close()
            handler.baseFilename = str(new_file)
            handler.stream = handler._open()
            logger.info(f"Logging switched to scene {scene_id}")
            return
    raise RuntimeError("No FileHandler found to switch log file")


class ColoredFormatter(logging.Formatter):
    LOG_COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[41m",
        "RESET": "\033[0m",
    }

    def __init__(self, fmt=None, use_color=True):
        super().__init__(fmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if self.use_color:
            color = self.LOG_COLORS.get(record.levelname, "")
            reset = self.LOG_COLORS["RESET"]
            msg = f"{color}{msg}{reset}"
        return msg


def setup_logger(config, default_scene_id: int = 0) -> logging.Logger:
    """Initialize the centralized logger with options from PluginConfig."""
    logger = logging.getLogger("stash_whisparr")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    # Determine log file path
    if config.LOG_FILE_ENABLE:
        log_file_path = config.LOG_FILE_LOCATION / "WhisparrBridge.log"
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        file_formatter = ColoredFormatter(
            "%(asctime)s - %(levelname)s - %(message)s",
            use_color=config.LOG_FILE_USE_COLOR,
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(
            getattr(logging, config.LOG_FILE_LEVEL.upper(), logging.DEBUG)
        )
        logger.addHandler(file_handler)

    # Console handler
    if config.LOG_CONSOLE_ENABLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_formatter = ColoredFormatter(
            "%(asctime)s - %(levelname)s - %(message)s", use_color=True
        )
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(
            getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
        )
        logger.addHandler(console_handler)

    return logger

class StashHandler(logging.Handler):
    def __init__(self, stash_logger):
        super().__init__()
        self.stash_logger = stash_logger

    def emit(self, record):
        try:
            msg = self.format(record)
            level = record.levelname.lower()
            log_fn = getattr(self.stash_logger, level, self.stash_logger.info)
            log_fn(msg)
        except Exception:
            self.handleError(record)

def load_config_logging(toml_path: str, STASH_DATA: dict, dev: bool):
    global CONFIG

    # Build kwargs for load_plugin_config
    kwargs = {}
    if not dev:
        kwargs["stash"] = STASH_DATA

    CONFIG = load_plugin_config(toml_path=toml_path, **kwargs)

    python_logger = setup_logger(CONFIG)

    if not dev:
        stash_handler = StashHandler(stash_log)
        stash_handler.setLevel(logging.DEBUG)
        stash_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        python_logger.addHandler(stash_handler)

    return python_logger, CONFIG
