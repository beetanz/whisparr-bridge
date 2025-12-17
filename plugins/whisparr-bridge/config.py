import copy
import json
import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
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
LOG_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[41m",
    "RESET": "\033[0m",
}


class ColoredFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, use_color=True):
        super().__init__(fmt, datefmt)
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        record_copy = copy.copy(record)
        if self.use_color:
            color = LOG_COLORS.get(record.levelname, "")
            reset = LOG_COLORS["RESET"]
            record_copy.msg = f"{color}{record_copy.msg}{reset}"
        return super().format(record_copy)


def setup_logger(config: PluginConfig, scene_id: int) -> logging.Logger:
    logger = logging.getLogger("stash_whisparr")
    logger.handlers.clear()

    if config.LOG_FILE_ENABLE:
        log_file_path = config.LOG_FILE_LOCATION / f"{str(scene_id)}.log"
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        log_file_path.touch(exist_ok=True)

        if config.LOG_FILE_TYPE.upper() == "SINGLE-FILE":
            file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
        elif config.LOG_FILE_TYPE.upper() == "ROTATING_SIZE":
            file_handler = RotatingFileHandler(
                log_file_path,
                maxBytes=config.LOG_FILE_MAX_BYTES,
                backupCount=config.LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
        elif config.LOG_FILE_TYPE.upper() == "ROTATING_TIME":
            file_handler = TimedRotatingFileHandler(
                log_file_path,
                when=config.LOG_FILE_ROTATE_WHEN,
                backupCount=config.LOG_FILE_BACKUP_COUNT,
                encoding="utf-8",
            )
        else:
            raise NotImplementedError(
                f"LOG_FILE_TYPE '{config.LOG_FILE_TYPE}' not implemented."
            )

        file_formatter = ColoredFormatter(
            fmt="%(asctime)s - %(levelname)s - %(message)s",
            use_color=config.LOG_FILE_USE_COLOR,
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(
            getattr(logging, config.LOG_FILE_LEVEL.upper(), logging.DEBUG)
        )
        logger.addHandler(file_handler)

    if config.LOG_CONSOLE_ENABLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_formatter = ColoredFormatter(
            fmt="%(asctime)s - %(levelname)s - %(message)s", use_color=True
        )
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(
            getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
        )
        logger.addHandler(console_handler)

    logger.setLevel(logging.DEBUG)
    return logger


class DualLogger:
    def _format(self, msg, args):
        if args:
            try:
                return msg % args
            except Exception:
                return f"{msg} {' '.join(map(str, args))}"
        return msg

    def __init__(self, main_logger: logging.Logger, stash_logger):
        self.main_logger = main_logger
        self.stash_logger = stash_logger

    def debug(self, msg: str, *args, **kwargs):
        formatted = self._format(msg, args)
        self.main_logger.debug(msg, *args, **kwargs)
        self.stash_logger.debug(formatted)

    def info(self, msg: str, *args, **kwargs):
        formatted = self._format(msg, args)
        self.main_logger.info(msg, *args, **kwargs)
        self.stash_logger.info(formatted)

    def warning(self, msg: str, *args, **kwargs):
        formatted = self._format(msg, args)
        self.main_logger.warning(msg, *args, **kwargs)
        self.stash_logger.warning(formatted)

    def error(self, msg: str, *args, **kwargs):
        formatted = self._format(msg, args)
        self.main_logger.error(formatted, **kwargs)
        self.stash_logger.error(formatted)

    def exception(self, msg: str, *args, **kwargs):
        formatted = self._format(msg, args)
        self.main_logger.exception(formatted, **kwargs)
        self.stash_logger.error(formatted)


def load_config_logging(
    toml_path: str, STASH_DATA: dict, dev: bool, scene_id: int, stash_log=None
):
    global CONFIG

    # Build kwargs for load_plugin_config
    kwargs = {}
    if not dev:
        kwargs["stash"] = STASH_DATA

    CONFIG = load_plugin_config(toml_path=toml_path, **kwargs)

    python_logger = setup_logger(CONFIG, scene_id)

    # Wrap logger only if not in dev mode
    try:
        dual_logger = python_logger if dev else DualLogger(python_logger, stash_log)
    except Exception as e:
        print(f"logging initialization failed: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        raise

    return dual_logger, CONFIG
