#!/usr/bin/env python3
# =========================
# Imports
# =========================
import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import requests
import tomli
from config import (PluginConfig, load_config_logging, safe_json_preview,
                    switch_scene_log, truncate_path)
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      computed_field, field_validator)
from requests.adapters import HTTPAdapter
from stashapi import log as stash_log
from stashapi.stashapp import StashInterface
from urllib3.util.retry import Retry

logger: logging.Logger
# =========================
# Custom Exceptions
# =========================


class WhisparrError(Exception):
    pass


class SceneNotFoundError(WhisparrError):
    pass


class ManualImportError(WhisparrError):
    pass


# =========================
# Helpers
# =========================
def load_from_toml(path: str) -> dict:
    p = Path(path).absolute()
    print(p)
    if not p.is_file():
        return {}
    with p.open("rb") as f:
        return tomli.load(f)


def has_ignored_tag(scene: "StashSceneModel", ignore_tags: List) -> Optional[str]:
    for tag in scene.tags:
        if tag in ignore_tags:
            return tag
    return None


def wait_for_file(path: Path, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """
    Wait until the file exists on disk or timeout is reached.
    Returns True if file appeared, False if timeout reached.
    """
    start_time = time.monotonic()
    while not path.exists():
        if time.monotonic() - start_time > timeout:
            logger.warning(
                "File did not appear in target folder within %.1f seconds: %s",
                timeout,
                path,
            )
            return False
        time.sleep(interval)
    return True


def map_to_local_fs(path: Path, mappings: dict) -> Path:
    """
    Translate a server path into a local filesystem path using the mapping.
    Preserves full relative path including filename.
    Handles leading slashes and Windows/Unix normalization.
    """
    path_str = path.as_posix()  # keep forward slashes
    logger.debug("Original path: %s", path_str)

    for server_prefix, local_prefix in mappings.items():
        server_prefix = Path(server_prefix).as_posix().rstrip("/")
        local_prefix = Path(local_prefix).as_posix().rstrip("/")

        # Remove any trailing slash from path before matching
        if path_str.startswith(server_prefix + "/") or path_str == server_prefix:
            rel_path = path_str[len(server_prefix) :].lstrip("/")
            mapped_path = Path(local_prefix) / Path(rel_path)
            return mapped_path

    # No mapping applied, return original path
    return path


# =========================
# Pydantic Models
# =========================


class RetrievedModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class BuiltModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FileQuality(RetrievedModel):
    id: int
    name: str
    source: str
    resolution: int


class FileQualityWrapper(RetrievedModel):
    quality: Optional[FileQuality]


class ManualImportFile(BuiltModel):
    path: str
    movieId: int
    folderName: str
    releaseGroup: str = ""
    languages: List[dict] = Field(
        default_factory=lambda: [{"id": 1, "name": "English"}]
    )
    indexerFlags: int = 0
    quality: Optional[FileQualityWrapper]


class Command(BuiltModel):
    name: str


class WhisparrSceneCreate(BuiltModel):
    title: str
    foreignId: str
    stashId: str
    monitored: bool
    qualityProfileId: int
    rootFolderPath: str
    addOptions: dict


class ManualImportParams(BuiltModel):
    folder: str
    movieId: int
    filterExistingFiles: bool = True


class ManualImportCommand(Command):
    name: str = "ManualImport"
    files: List[ManualImportFile]
    importMode: str = "auto"


class CommandResponse(RetrievedModel):
    id: int
    result: str = "unknown"
    status: str = "queued"


class RenameCommand(Command):
    name: str = "RenameFiles"
    movieIds: List[int]


class RefreshMovieCommand(Command):
    name: str = "RefreshMovie"
    movieIds: List[int]


class WhisparrStatistics(RetrievedModel):
    movieFileCount: int
    sizeOnDisk: int


class WhisparrScene(RetrievedModel):
    title: str
    id: int
    path: Path
    statistics: WhisparrStatistics

    @field_validator("path", mode="before")
    def convert_to_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class ManualImportPreviewFile(RetrievedModel):
    path: Path
    folderName: str
    size: int
    quality: Optional[FileQualityWrapper]

    @field_validator("path", mode="before")
    def convert_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class StashFile(RetrievedModel):
    path: Optional[Path]

    @field_validator("path", mode="before")
    def to_path(cls, v: Any) -> Optional[Path]:
        return Path(v) if v else None


class StashSceneModel(RetrievedModel):
    title: str = ""
    tags: List[str] = Field(default_factory=list)
    files: List[StashFile] = Field(default_factory=list)
    stash_ids: List[Dict[str, str]] = Field(default_factory=list)
    # Default fallback
    stashdb_endpoint_substr: str = "stashdb.org"

    @field_validator("tags", mode="before")
    # Stash returns [] or list[{"name": str, ...}]
    def extract_tag_names(cls, v: Any) -> List[str]:
        if not v:
            return []
        if isinstance(v[0], dict) and "name" in v[0]:
            return [item["name"] for item in v]
        return v

    @computed_field
    def stashdb_id(self) -> Optional[str]:
        for sid in self.stash_ids:
            if self.stashdb_endpoint_substr in sid.get("endpoint", ""):
                return sid.get("stash_id")
        return None

    @computed_field
    def paths(self) -> List[Path]:
        return [f.path for f in self.files if f.path]


# =========================
# HTTP Helper
# =========================


def http_json(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Union[BaseModel, dict]] = None,
    params: Optional[dict] = None,
    timeout: int = 30,
    response_model: Optional[Type[BaseModel]] = None,
    response_is_list: bool = False,
    dev: bool = False,
) -> Tuple[int, Union[BaseModel, List[BaseModel], dict, str]]:

    _session = requests.Session()
    _retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
    )
    _session.mount("http://", HTTPAdapter(max_retries=_retry))
    _session.mount("https://", HTTPAdapter(max_retries=_retry))

    if isinstance(body, BaseModel):
        body = body.model_dump(exclude_none=True, by_alias=True)

    headers = {"Accept": "application/json", "X-Api-Key": api_key}
    logger.debug(
        "%s %s params=%s body=%s", method, url, params, safe_json_preview(body)
    )

    try:
        r = _session.request(
            method, url, headers=headers, json=body, params=params, timeout=timeout
        )
        try:
            parsed = r.json()
        except ValueError:
            parsed = r.text

        if r.status_code >= 400:
            msg = f"HTTP {r.status_code} error for {method} {url}: {parsed}"
            logger.error(msg)
            raise WhisparrError(msg)

        if response_model:
            if dev:
                logger.debug("Attempting Parse to %s", response_model)
                logger.debug("raw data: %s", parsed)
            try:
                if response_is_list and isinstance(parsed, list):
                    return r.status_code, [response_model(**item) for item in parsed]
                elif not response_is_list and isinstance(parsed, dict):
                    return r.status_code, response_model(**parsed)
            except Exception as e:
                logger.exception("Failed to parse response into Pydantic model: %s", e)
                return r.status_code, parsed

        return r.status_code, parsed
    except requests.RequestException as e:
        logger.exception("HTTP request failed for %s %s", method, url)
        raise WhisparrError(f"HTTP request failed for {method} {url}: {e}") from e


class FileManager:
    def __init__(self, config: PluginConfig, source: Path, destination: Path):
        self.og_source: Path = source.parent
        self.og_destination: Path = destination
        self.name: str = source.name
        # Apply path mapping
        self.source: Path = self._path_mapping(source.parent, config.PATH_MAPPING)
        self.destination: Path = self._path_mapping(destination, config.PATH_MAPPING)

    def _path_mapping(self, path: Path, pathmap: dict) -> Path:
        """
        Map a server path to the local filesystem if a pathmap is provided.
        """
        if pathmap:
            logger.warning("Mapping paths: %s", pathmap)
            return map_to_local_fs(path, pathmap)
        return path

    def exists(self) -> Path:
        source_file = (self.source / self.name).resolve()
        destination_file = (self.destination / self.name).resolve()

        # Same physical file → check once
        if source_file == destination_file:
            if source_file.exists():
                logger.debug("Source and destination are the same file")
                return source_file

        if source_file.exists():
            logger.debug("File is in Stash Directory")
            return source_file

        if destination_file.exists():
            logger.debug("File is in the Whisparr Directory")
            return destination_file

        logger.error("Unable to Locate the File. Dumping info.")
        logger.error("Source: %s", source_file)
        logger.error("Destination: %s", destination_file)
        raise FileNotFoundError(source_file)

    def move(self, source: Path, retries: int = 5, delay: float = 0.5) -> bool:
        try:
            # Ensure source exists
            if not source.is_file():
                logger.warning("Source file does not exist: %s", self.source)
                return False

            # Construct full destination path
            target_file = (self.destination / self.name).resolve()
            target_file.parent.mkdir(parents=True, exist_ok=True)
            logger.info("source: %s", source)
            logger.info("target_file: %s", target_file)
            if source != target_file:
                # Move/replace the file
                source.replace(target_file)

                # Retry checking if the file exists with exponential backoff
                for attempt in range(retries):
                    if target_file.is_file():
                        logger.info(
                            "File moved successfully: %s -> %s",
                            self.source,
                            target_file,
                        )
                        return True
                    sleep_time = delay * (2**attempt)  # Exponential backoff
                    time.sleep(sleep_time)

                logger.warning(
                    "File move completed but target file still not found after retries: %s",
                    target_file,
                )
                return False
            else:
                logger.info("File is already in the correct directory")
                return False

        except Exception as e:
            logger.exception(
                "Failed to move file %s -> %s: %s", self.source, target_file, e
            )
            return False


# =========================
# Whisparr Interface
# =========================


class WhisparrInterface:
    def __init__(
        self,
        config: PluginConfig,
        stash_scene: StashSceneModel,
        http_func: Callable[..., Tuple[int, Any]] = http_json,
    ):
        self.stash_scene: StashSceneModel = stash_scene
        self.whisparr_scene: Optional[WhisparrScene] = None
        self.url: str = config.WHISPARR_URL
        self.key: str = config.WHISPARR_KEY
        self.monitored: bool = config.MONITORED
        self.move: bool = config.MOVE_FILES
        self.http_json = http_func
        self.rename: bool = config.WHISPARR_RENAME
        self.root_dir: str = str(config.ROOT_FOLDER)
        self.qualprofile: str = config.QUALITY_PROFILE
        self.config: Dict = config
        self.filenames: str = stash_scene.files

    def process_scene(self) -> None:
        """
        Process the Stash scene: find it in Whisparr, create if missing,
        and handle file imports/moves.
        """
        self.whisparr_scene = self.find_existing_scene()

        if not self.whisparr_scene:
            self.create_scene()
            self.whisparr_scene = self.find_existing_scene()
        did_move_files = self.process_stash_files()
        logger.debug("Did any file move operations happen? %s", did_move_files)
        if did_move_files:
            self._queue_command("RefreshMovie")
        self.import_stash_file()

    def find_existing_scene(self) -> Optional[WhisparrScene]:
        status, scenes = self.http_json(
            method="GET",
            url=f"{self.url}/api/v3/movie",
            api_key=self.key,
            params={"stashId": self.stash_scene.stashdb_id},
            response_model=WhisparrScene,
            response_is_list=True,
        )
        if status != 200 or not scenes:
            logger.info("No existing scenes found in Whisparr")
            return None
        if len(scenes) != 1:
            logger.error("Whisparr returned %d scenes", len(scenes))
            return None
        logger.info("Scene already exists in Whisparr: %s", scenes[0])
        return scenes[0]

    def create_scene(self) -> None:
        """
        Create a Whisparr scene based on the Stash scene.
        Guaranteed to have a stashdb_id at this point.
        """
        # Assert stashdb_id exists to satisfy mypy
        stashdb_id: str = self.stash_scene.stashdb_id  # type: ignore[assignment]

        scene_payload = WhisparrSceneCreate(
            title=self.stash_scene.title,
            foreignId=stashdb_id,
            stashId=stashdb_id,
            monitored=self.monitored,
            qualityProfileId=self.get_default_quality_profile(),
            rootFolderPath=self.get_default_root_folder(),
            addOptions={
                "monitor": "movieOnly" if self.monitored else "none",
                "searchForMovie": False,
            },
        )

        status, scene = self.http_json(
            method="POST",
            url=f"{self.url}/api/v3/movie",
            api_key=self.key,
            body=scene_payload,
            timeout=120,
            response_model=WhisparrScene,
        )

        self.whisparr_scene = scene

        if status in (200, 201):
            logger.info("Added movie '%s' to Whisparr", self.stash_scene.title)
        else:
            msg = f"Failed to add movie '{self.stash_scene.title}': {scene}"
            logger.error(msg)
            raise WhisparrError(msg)

    def process_stash_files(self):
        """Process each file in the Stash scene."""
        if not self.whisparr_scene:
            raise SceneNotFoundError(
                "Whisparr scene not set up. Call process_scene() first."
            )
        Stashfilez = []
        for stash_path in self.stash_scene.paths:
            try:
                logger.info("Checking Stash file: %s", truncate_path(stash_path))
                filehandl = FileManager(
                    self.config, source=stash_path, destination=self.whisparr_scene.path
                )
                logger.info("File is at %s", filehandl.exists())
                Stashfilez.append(filehandl)
            except Exception as e:
                logger.exception(
                    "Error processing file %s: %s", truncate_path(stash_path), e
                )

        files_moved = []
        if self.move:
            for file in Stashfilez:
                source = file.exists()
                moved = file.move(source)
                files_moved.append(moved)
        return any(files_moved)

    def import_stash_file(self) -> None:
        matched_preview = self._get_matching_preview_file()
        if matched_preview is None:
            return
        self._execute_manual_import(matched_preview)
        if self.rename:
            self._queue_command("RenameFiles")
        else:
            self._queue_command("RefreshMovie")

    def _get_manual_import_preview(self) -> List[ManualImportPreviewFile]:
        assert self.whisparr_scene is not None
        params = ManualImportParams(
            folder=self.whisparr_scene.path.as_posix(), movieId=self.whisparr_scene.id
        )
        status, previews = self.http_json(
            method="GET",
            url=f"{self.url}/api/v3/manualimport",
            api_key=self.key,
            params=params.model_dump(exclude_none=True, by_alias=True),
            response_model=ManualImportPreviewFile,
            response_is_list=True,
        )
        if status != 200 or not previews:
            if self.whisparr_scene.statistics.movieFileCount == len(
                self.stash_scene.files
            ):
                logger.info("File has already been imported to Whisparr")
            else:
                raise ManualImportError(f"Manual import preview failed: {previews}")
        return previews

    def _get_matching_preview_file(self) -> Optional[ManualImportPreviewFile]:
        previews = self._get_manual_import_preview()
        for g in self.filenames:
            matched = next((f for f in previews if f.path.name == g.path.name), None)
        if not matched:
            logger.info("All files already imported to Whisparr")
            return None
        return matched

    def _execute_manual_import(self, preview_file: ManualImportPreviewFile) -> None:
        assert self.whisparr_scene is not None
        command = ManualImportCommand(
            files=[
                ManualImportFile(
                    folderName=preview_file.folderName,
                    path=preview_file.path.as_posix(),
                    movieId=self.whisparr_scene.id,
                    quality=preview_file.quality,
                )
            ]
        )
        status, resp = self.http_json(
            method="POST",
            url=f"{self.url}/api/v3/command",
            api_key=self.key,
            body=command,
        )
        if status not in (200, 201):
            raise ManualImportError(f"Manual import command failed: {resp}")
        logger.info("Manual import executed successfully for %s", preview_file.path)

    def _queue_command(self, commandname: str = "RefreshMovie") -> None:
        try:
            if commandname == "RefreshMovie":
                command = RefreshMovieCommand(movieIds=[self.whisparr_scene.id])
            if commandname == "RenameFiles":
                command = RenameCommand(movieIds=[self.whisparr_scene.id])
            status, resp = self.http_json(
                method="POST",
                url=f"{self.url}/api/v3/command",
                api_key=self.key,
                body=command,
            )
            if status in (200, 201):
                logger.info(
                    "%s command queued for movie ID: %s",
                    commandname,
                    self.whisparr_scene.id,
                )
                logger.debug("response: %s", resp)
                # return CommandResponse(**resp.get("body"))
            else:
                logger.error("%s command failed: %s", commandname, resp)
        except Exception as e:
            logger.exception("Failed to queue %s command: %s", commandname, e)

    def get_default_quality_profile(self) -> int:
        status, qps = self.http_json(
            method="GET", url=f"{self.url}/api/v3/qualityprofile", api_key=self.key
        )
        any_id = next(
            (item["id"] for item in qps if item["name"] == self.qualprofile), None
        )
        if any_id is None and qps:
            any_id = qps[0]["id"]
        return int(any_id or 1)

    def get_default_root_folder(self) -> str:
        result = self.http_json(
            method="GET", url=f"{self.url}/api/v3/rootfolder", api_key=self.key
        )
        rfs: List[Dict[str, str]] = result[1]

        # Check for configured root_dir
        if self.root_dir != "":
            rf = next((rf for rf in rfs if rf["path"] == self.root_dir), None)
            if rf is not None:
                return rf["path"]

        # Fallback to first root folder if available
        if rfs:
            return rfs[0]["path"]

        # Safe fallback if list is empty
        raise ValueError("No root folders returned from API")


class StashHelpers:
    dev: bool = False
    STASH_DATA: Dict[str, Any] = {}
    _stash_conn = None
    toml_path: Path = None

    def __init__(self, scene_id: int):
        self.scene_id = scene_id

    @classmethod
    def open_conn(cls):
        """Lazy-load the stash connection."""
        if cls._stash_conn is None:
            try:
                cls._stash_conn = StashInterface(cls.STASH_DATA["server_connection"])
                logger.info("StashInterface connection established.")
            except KeyError:
                logger.error("Missing 'server_connection' in STASH_DATA.")
                return None
            except Exception as e:
                logger.exception("Failed to initialize StashInterface: %s", e)
                return None
        return cls._stash_conn


# =========================
# Main
# =========================
def preprocessor(dev: bool):
    global logger
    if dev:
        stash_log.info("\n\n***DEV_MODE***\n\n")
        toml_path = "../../dev.toml"
    else:
        toml_path = "/root/.stash/plugins/whisparr-bridge/config.toml"
    STASH_DATA = {}
    if not dev:
        try:
            raw_data = sys.stdin.read()
            if not raw_data.strip():
                stash_log.error("No input data received from Stash hook.")
                return
            STASH_DATA = json.loads(raw_data)
            toml_path = f'{STASH_DATA.get("PluginDir")}/config.toml'
        except Exception as e:
            print(f"Failed to parse input JSON: {e}")
            return
    else:
        dev_config = load_from_toml(toml_path)
        stash_log.info(f"TOML config:")
        stash_log.info(f"------------")
        for thing, data in dev_config.items():
            stash_log.info(f"{thing}|{data}")
        stash_log.info(f"------------")
        STASH_DATA["server_connection"] = dev_config.get("STASH_CONFIG")
    StashHelpers.STASH_DATA = STASH_DATA

    try:
        logger, config = load_config_logging(
            toml_path=toml_path, STASH_DATA=STASH_DATA, dev=dev
        )
    except Exception as e:
        stash_log.error(f"Failed to load configuration: {e}")
        return
    return config


def process_single_scene(config, scene_id):
    switch_scene_log(logger, f"{scene_id}")
    logger.info("SCENEID %d STARTING", scene_id)
    stash = StashHelpers.open_conn()
    scene = None
    try:
        scene_data = stash.find_scene(scene_id)
        if not scene_data:
            logger.error("Scene %s not found in Stash.", scene_id)
            return "Failed"
        scene = StashSceneModel(
            **scene_data, stashdb_endpoint_substr=config.STASHDB_ENDPOINT_SUBSTR
        )
    except ValidationError as e:
        logger.exception("Scene data validation failed: %s", e)
        return "Failed"
    except Exception as e:
        logger.exception("Unexpected error fetching scene: %s", e)
        return "Failed"
    if scene.stashdb_id is None:
        logger.error("No StashDB ID for %s, skipping", scene.title)
        return "NoStashDB ID"
    logger.info("Processing scene: %s", scene.title)
    ignored_tag = has_ignored_tag(scene, config.IGNORE_TAGS)
    if ignored_tag:
        logger.info(
            "Scene '%s' skipped due to ignored tag: %s", scene.title, ignored_tag
        )
        return "SkippedTag"
    whisparr = WhisparrInterface(config=config, stash_scene=scene)
    try:
        whisparr.process_scene()
        logger.info("Scene processing completed successfully.")
    except WhisparrError as e:
        logger.exception("Whisparr processing error: %s", e)
        return "Failed"
    except Exception as e:
        logger.exception(f"Unexpected error during scene processing: {e}")
        return "Failed"
    return "Success"


from typing import Optional


def bulk_processor(config: "PluginConfig") -> None:
    import sqlite3

    stash = StashHelpers.open_conn()
    generalconf: dict = stash.get_configuration().get("general")
    if not config.DEV_MODE:
        sqllite_db_loc: str = generalconf.get("databasePath")
    else:
        sqllite_db_loc = "stash-go.sqlite"
    # Fetch all scene IDs
    try:
        with sqlite3.connect(sqllite_db_loc) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM scenes")
            scene_ids: list[int] = [row[0] for row in cursor.fetchall()]
        logger.info(f"Found {len(scene_ids)} scenes")
    except Exception as e:
        logger.error("Failed to initialize DB: %s", e)
        return

    if len(scene_ids) == 0:
        logger.error("Stash DB is empty! exiting")
        return

    progress: float = 0
    progress_step: float = 1 / len(scene_ids)
    bulk_results: Path = Path(f"{config.LOG_FILE_LOCATION}/bulk_results.csv")

    with open(bulk_results, "a", newline="") as records:
        writer = csv.writer(records)
        if bulk_results.stat().st_size == 0:
            writer.writerow(["scene_id", "success"])
            records.flush()
        for i, scene in enumerate(reversed(scene_ids), start=1):
            # stash_log.debug(f"Processing Scene: {scene}")
            try:
                success: str = process_single_scene(config, scene)
                writer.writerow([scene, success])
                if i % 50 == 0:
                    records.flush()
            except Exception as err:
                logger.error(f"main function error: {err}")
                writer.writerow([scene, False])
                records.flush()
            progress += progress_step
            # stash_log.progress(progress)


def main(
    scene_id: Optional[int] = None,
    dev: Optional[bool] = False,
    bulk: Optional[bool] = False,
) -> None:
    global logger
    config: Optional["PluginConfig"] = preprocessor(dev)
    if not config:
        stash_log.error("Configuration could not be loaded; exiting.")
        return
    if bulk:
        logger.debug("Bulk update started")
        bulk_processor(config=config)
    else:
        logger.debug("Starting stage 2 - get Scene ID")
        # 2. Determine scene ID
        args = StashHelpers.STASH_DATA.get("args") or {}
        hook_context: dict = args.get(
            "hookContext"
        ) or {}
        scene_id = scene_id or hook_context.get("id")
        if not scene_id:
            stash_log.info("No scene ID provided by hook.")
            if args.get("mode") == "bulk":
                logger.info("Bulk update started")
                bulk_processor(config=config)
            else:
                stash_log.error("No Scene ID and not bulk, exiting")
                return
        process_single_scene(config=config, scene_id=scene_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the main function with optional arguments."
    )
    parser.add_argument(
        "--scene_id", type=int, help="Optional scene ID to run", default=None
    )
    parser.add_argument("--dev", action="store_true", help="Run in development mode")
    parser.add_argument("--bulk", action="store_true", help="Batch Process all scenes")
    args = parser.parse_args()
    main(scene_id=args.scene_id, dev=args.dev, bulk=args.bulk)
