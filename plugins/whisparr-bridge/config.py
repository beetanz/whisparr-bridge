import copy
import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional
from typing import List, Any
import tomli
from pydantic import ValidationError
from stashapi import log as stash_log
from pydantic import BaseModel, Field, field_validator
from pydantic import ConfigDict
import json

class PluginConfig(BaseModel):
    # Core
    WHISPARR_URL: str = ""
    WHISPARR_KEY: str = ""
    STASHDB_ENDPOINT_SUBSTR: str = "stashdb.org"

    # Behavior
    MONITORED: bool = True
    MOVE_FILES: bool = False
    WHISPARR_RENAME: bool = True
    QUALITY_PROFILE: str = "Any"
    ROOT_FOLDER: Optional[str] = None
    IGNORE_TAGS: List[str] = Field(default_factory=list)

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FILE_ENABLE: bool = False
    LOG_FILE_LEVEL: str = "DEBUG"
    LOG_FILE_LOCATION: str = ""
    LOG_FILE_TYPE: str = "SINGLE-FILE"
    LOG_FILE_MAX_BYTES: int = 5_000_000
    LOG_FILE_BACKUP_COUNT: int = 3
    LOG_FILE_ROTATE_WHEN: str = "midnight"
    LOG_FILE_USE_COLOR: bool = False
    LOG_CONSOLE_ENABLE: bool = False

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

    @field_validator("WHISPARR_URL", "WHISPARR_KEY", mode="before")
    @classmethod
    def not_empty(cls, v: str):
        if not v or not str(v).strip():
            raise ValueError("must not be empty")
        return v.strip()


def load_from_toml(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)


def load_plugin_config(
    toml_path: str = "config.toml", stash: Optional[dict] = None
) -> PluginConfig:
    """
    Load and validate plugin configuration.

    1. Load defaults from PluginConfig.
    2. Override with TOML file if present.
    3. Optionally merge Stash plugin settings if `stash` dict is provided.
    """
    config = PluginConfig()

    path = Path(toml_path)
    if path.is_file():
        try:
            with path.open("rb") as f:
                toml_data = tomli.load(f)
            config = config.model_copy(update=toml_data)
            stash_log.info("Configuration loaded and validated from %s", toml_path)
        except Exception as e:
            stash_log.exception("Failed to load/validate config from TOML: %s", e)
            raise
    else:
        stash_log.warning("Config file %s not found. Using defaults.", toml_path)

    if stash:
        plugin_cfg = stash.get("plugins", {}).get("whisparr-bridge", {})
        if isinstance(plugin_cfg, dict):
            try:
                config = config.model_copy(update=plugin_cfg)
                stash_log.info("Stash plugin settings merged and validated into config")
            except ValidationError as e:
                stash_log.exception("Stash plugin settings validation failed: %s", e)
                raise
        else:
            stash_log.warning(
                "Stash plugin settings not found or invalid. Skipping merge."
            )

    if not config.WHISPARR_URL or not config.WHISPARR_KEY:
        stash_log.error("Whisparr URL and API key must be set in config.")
        raise ValueError("Missing critical Whisparr configuration fields")

    return config


# =========================
# Logging Setup
# =========================

# =========================
# Helpers (config-aware)
# =========================
# Assume CONFIG is set globally after loading
CONFIG: Optional[PluginConfig] = None


def truncate_path(p: Path) -> str:
    s = str(p)
    if CONFIG is None:
        return s if len(s) <= 100 else f"...{s[-97:]}"  # fallback max length
    return (
        s
        if len(s) <= CONFIG.MAX_PATH_LENGTH
        else f"...{s[-(CONFIG.MAX_PATH_LENGTH-3):]}"
    )


def safe_json_preview(data:Any) -> str:
    """
    Convert data to JSON string for logging, redacting API keys and truncating
    long output based on CONFIG.MAX_LOG_BODY.
    """
    if CONFIG is None:
        max_len = 1000
    else:
        max_len = CONFIG.MAX_LOG_BODY
    try:
        if isinstance(data, dict):
            redacted = dict(data)
            for k in ("apiKey", "X-Api-Key", "apikey"):
                if k in redacted:
                    redacted[k] = "***REDACTED***"
            text = json.dumps(redacted, default=str)
        else:
            text = json.dumps(data, default=str)

        return text if len(text) <= max_len else text[:max_len] + "...(truncated)"
    except TypeError:
        return "<unserializable>"


LOG_COLORS = {
    "DEBUG": "\033[36m",  # Cyan
    "INFO": "\033[32m",  # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",  # Red
    "CRITICAL": "\033[41m",  # Red background
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


def setup_logger(config: PluginConfig) -> logging.Logger:
    """Configure main logger based on PluginConfig."""
    logger = logging.getLogger("stash_whisparr")
    logger.handlers.clear()

    if config.LOG_FILE_ENABLE:
        log_file_path = config.LOG_FILE_LOCATION or "stashtest.log"
        if config.LOG_FILE_TYPE.upper() == "SINGLE-FILE":
            file_handler = logging.FileHandler(
                log_file_path, mode="w", encoding="utf-8"
            )
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
    def __init__(self, main_logger: logging.Logger, stash_log):
        self.main_logger = main_logger
        self.stash_logger = stash_log

    def debug(self, msg: str, *args, **kwargs):
        self.main_logger.debug(msg, *args, **kwargs)
        self.stash_logger.debug(msg)

    def info(self, msg: str, *args, **kwargs):
        self.main_logger.info(msg, *args, **kwargs)
        self.stash_logger.info(msg)

    def warning(self, msg: str, *args, **kwargs):
        self.main_logger.warning(msg, *args, **kwargs)
        self.stash_logger.warning(msg)

    def error(self, msg: str, *args, **kwargs):
        self.main_logger.error(msg, *args, **kwargs)
        self.stash_logger.error(msg)

    def exception(self, msg: str, *args, **kwargs):
        self.main_logger.exception(msg, *args, **kwargs)
        self.stash_logger.error(msg)


def load_config_logging(toml_path: str, STASH_DATA: dict):
    global CONFIG
    # Load config first
    CONFIG = load_plugin_config(toml_path=toml_path, stash=STASH_DATA)

    # Setup logger based on the loaded config
    python_logger = setup_logger(CONFIG)

    # Combine with stash log
    dual_logger = DualLogger(python_logger, stash_log)

    return dual_logger, CONFIG
