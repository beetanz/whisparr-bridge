import sys
import copy
import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
from typing import Optional
import tomli
from pydantic import ValidationError
from stashapi.stashapp import StashInterface


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

def load_plugin_config(toml_path: str = "config.toml", stash: Optional[StashInterface] = None) -> PluginConfig:
    """
    Load and validate plugin configuration.

    1. Load defaults from PluginConfig.
    2. Override with TOML file if present.
    3. Optionally merge Stash plugin settings if `stash` is provided.
    """
    # Start with defaults
    config = PluginConfig()

    # 1️⃣ Load TOML file
    path = Path(toml_path)
    if path.is_file():
        try:
            with path.open("rb") as f:
                toml_data = tomli.load(f)
            config = config.model_copy(update=toml_data)
            logger.info("Configuration loaded and validated from %s", toml_path)
        except Exception as e:
            logger.exception("Failed to load/validate config from TOML: %s", e)
            raise
    else:
        logger.warning("Config file %s not found. Using defaults.", toml_path)

    # 2️⃣ Merge Stash plugin settings if stash provided
    if stash:
        plugin_cfg = stash.get_configuration().get("plugins", {}).get("whisparr-bridge", {})
        if isinstance(plugin_cfg, dict):
            try:
                config = config.model_copy(update=plugin_cfg)
                logger.info("Stash plugin settings merged and validated into config")
            except ValidationError as e:
                logger.exception("Stash plugin settings validation failed: %s", e)
                raise
        else:
            logger.warning("Stash plugin settings not found or invalid. Skipping merge.")

    # 3️⃣ Validate critical fields
    if not config.WHISPARR_URL or not config.WHISPARR_KEY:
        logger.error("Whisparr URL and API key must be set in config.")
        raise ValueError("Missing critical Whisparr configuration fields")

    return config

# =========================
# Logging Setup
# =========================

LOG_COLORS = {
    "DEBUG": "\033[36m",    # Cyan
    "INFO": "\033[32m",     # Green
    "WARNING": "\033[33m",  # Yellow
    "ERROR": "\033[31m",    # Red
    "CRITICAL": "\033[41m", # Red background
    "RESET": "\033[0m"
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

def setup_main_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        logger.handlers.clear()

    if CONFIG.LOG_FILE_ENABLE:
        log_file_path = CONFIG.LOG_FILE_LOCATION or "stashtest.log"
        if CONFIG.LOG_FILE_TYPE.upper() == "SINGLE-FILE":
            file_handler = logging.FileHandler(log_file_path, mode="w", encoding="utf-8")
        elif CONFIG.LOG_FILE_TYPE.upper() == "ROTATING_SIZE":
            file_handler = RotatingFileHandler(log_file_path, maxBytes=CONFIG.LOG_FILE_MAX_BYTES,
                                               backupCount=CONFIG.LOG_FILE_BACKUP_COUNT, encoding="utf-8")
        elif CONFIG.LOG_FILE_TYPE.upper() == "ROTATING_TIME":
            file_handler = TimedRotatingFileHandler(log_file_path, when=CONFIG.LOG_FILE_ROTATE_WHEN,
                                                    backupCount=CONFIG.LOG_FILE_BACKUP_COUNT, encoding="utf-8")
        else:
            raise NotImplementedError(f"LOG_FILE_TYPE '{CONFIG.LOG_FILE_TYPE}' not implemented.")
        file_formatter = ColoredFormatter(fmt="%(asctime)s - %(levelname)s - %(message)s",
                                          use_color=CONFIG.LOG_FILE_USE_COLOR)
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(getattr(logging, CONFIG.LOG_FILE_LEVEL.upper(), logging.DEBUG))
        logger.addHandler(file_handler)

    if CONFIG.LOG_CONSOLE_ENABLE:
        console_handler = logging.StreamHandler(sys.stdout)
        console_formatter = ColoredFormatter(fmt="%(asctime)s - %(levelname)s - %(message)s", use_color=True)
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(getattr(logging, CONFIG.LOG_LEVEL.upper(), logging.INFO))
        logger.addHandler(console_handler)

    logger.setLevel(logging.DEBUG)
    return logger

python_logger = setup_main_logger("stash_whisparr")

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

logger = DualLogger(python_logger, log)