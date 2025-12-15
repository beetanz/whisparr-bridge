#!/usr/bin/env python3
# =========================
# Imports
# =========================

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

# 3rd Party
import requests
# Local
from config import (PluginConfig, load_config_logging, safe_json_preview,
                    truncate_path)
from pydantic import (BaseModel, ConfigDict, Field, ValidationError,
                      computed_field, field_validator)
from requests.adapters import HTTPAdapter
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


def has_ignored_tag(scene: "StashSceneModel", ignore_tags: List) -> Optional[str]:
    for tag in scene.tags:
        if tag in ignore_tags:
            return tag
    return None


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


class RenameCommand(Command):
    name: str = "RenameFiles"
    movieIds: List[int]


class WhisparrScene(RetrievedModel):
    title: str
    id: int
    path: Path

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

    @property
    @computed_field
    def stashdb_id(self) -> Optional[str]:
        for sid in self.stash_ids:
            if self.stashdb_endpoint_substr in sid.get("endpoint", ""):
                return sid.get("stash_id")
        return None

    @property
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

    def process_scene(self) -> None:
        """
        Process the Stash scene: find it in Whisparr, create if missing,
        and handle file imports/moves.
        """
        self.whisparr_scene = self.find_existing_scene()

        if not self.whisparr_scene:
            self.create_scene()

        self.process_stash_files()

    def find_existing_scene(self) -> Optional[WhisparrScene]:
        if self.stash_scene.stashdb_id is None:
            logger.error("No StashDB ID for %s, skipping", self.stash_scene.title)
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
        logger.info("Movie already exists in Whisparr: %s", scenes[0].title)
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

    def process_stash_files(self) -> None:
        """Process each file in the Stash scene."""
        if not self.whisparr_scene:
            raise SceneNotFoundError(
                "Whisparr scene not set up. Call process_scene() first."
            )

        for stash_path in self.stash_scene.paths:
            logger.info("Checking Stash file: %s", truncate_path(stash_path))
            if not stash_path.exists():
                logger.warning("File does not exist: %s", truncate_path(stash_path))
                continue
            try:
                if self.ensure_file_location(stash_path):
                    self.import_stash_file(stash_path)
            except Exception as e:
                logger.exception(
                    "Error processing file %s: %s", truncate_path(stash_path), e
                )

    def ensure_file_location(self, stash_path: Path) -> bool:
        """Ensure the file is in the correct Whisparr directory, moving it if necessary."""
        assert self.whisparr_scene is not None
        target_dir = self.whisparr_scene.path
        if not target_dir:
            logger.error("Whisparr scene has no path defined.")
            return False

        if stash_path.parent.resolve() == target_dir.resolve():
            logger.info(
                "File already in Whisparr directory: %s", truncate_path(stash_path)
            )
            return True

        if self.move:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.exception(
                    "Failed to create target directory %s: %s",
                    truncate_path(target_dir),
                    e,
                )
                return False

            target_file = target_dir / stash_path.name
            counter = 1
            while target_file.exists():
                target_file = (
                    target_dir / f"{stash_path.stem}_{counter}{stash_path.suffix}"
                )
                counter += 1

            try:
                shutil.move(str(stash_path), str(target_file))
                logger.info(
                    "Moved file to Whisparr directory: %s", truncate_path(target_file)
                )
            except Exception as e:
                logger.exception(
                    "Failed to move file %s to %s: %s",
                    truncate_path(stash_path),
                    truncate_path(target_file),
                    e,
                )
                return False
            return True

        logger.debug(
            "File not in target directory, and MOVE_FILES=False: %s",
            truncate_path(stash_path),
        )
        return False

    def import_stash_file(self, stash_path: Path) -> None:
        matched_preview = self._get_matching_preview_file(stash_path)
        if matched_preview is None:
            return
        self._execute_manual_import(matched_preview)
        if self.rename:
            self._queue_rename()

    def _get_manual_import_preview(
        self, stash_path: Path
    ) -> List[ManualImportPreviewFile]:
        assert self.whisparr_scene is not None
        params = ManualImportParams(
            folder=stash_path.parent.as_posix(), movieId=self.whisparr_scene.id
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
            raise ManualImportError(f"Manual import preview failed: {previews}")
        return previews

    def _get_matching_preview_file(
        self, stash_path: Path
    ) -> Optional[ManualImportPreviewFile]:
        previews = self._get_manual_import_preview(stash_path)
        matched = next((f for f in previews if f.path.name == stash_path.name), None)
        if not matched:
            logger.info("All files already imported to Whisparr: %s", stash_path)
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

    def _queue_rename(self) -> None:
        """Queue rename command if enabled and files were imported/moved."""
        if not self.whisparr_scene:
            logger.warning("Cannot queue rename; Whisparr scene not set.")
            return
        try:
            command = RenameCommand(movieIds=[self.whisparr_scene.id])
            status, resp = self.http_json(
                method="POST",
                url=f"{self.url}/api/v3/command",
                api_key=self.key,
                body=command,
            )
            if status in (200, 201):
                logger.info(
                    "Rename command queued for movie ID: %s", self.whisparr_scene.id
                )
            else:
                logger.error("Rename command failed: %s", resp)
        except Exception as e:
            logger.exception("Failed to queue rename command: %s", e)

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


# =========================
# Main
# =========================


def main(scene_id: Optional[int] = None) -> None:
    """
    Main entry point for the Whisparr bridge hook.

    This function reads scene data from stdin (as JSON), loads the plugin configuration,
    initializes the Stash interface, retrieves the scene from Stash, checks for ignored tags,
    and processes the scene with Whisparr (adding the movie, importing files, and optionally renaming).

    Args:
        scene_id (Optional[int]): If provided, overrides the scene ID from the Stash hook data.
    """

    # 1. Read and parse hook data from stdin
    STASH_DATA = None
    try:
        raw_data = sys.stdin.read()
        if not raw_data.strip():
            print("No input data received from Stash hook.")
            return
        STASH_DATA = json.loads(raw_data)
    except Exception as e:
        print(f"Failed to parse input JSON: {e}")
        return

    # 2. Load configuration and initialize logger
    try:
        logger, config = load_config_logging(
            toml_path="config.toml", STASH_DATA=STASH_DATA
        )
    except Exception as e:
        print(f"Failed to load configuration: {e}")
        return

    # 3. Initialize Stash interface
    try:
        stash = StashInterface(STASH_DATA["server_connection"])
    except KeyError:
        logger.error("Missing 'server_connection' in Stash data.")
        return
    except Exception as e:
        logger.exception("Failed to initialize StashInterface: %s", e)
        return

    # 4. Determine scene ID
    hook_context = (STASH_DATA.get("args") or {}).get("hookContext") or {}
    scene_id = scene_id or hook_context.get("id")
    if not scene_id:
        logger.info("No scene ID provided by hook; exiting.")
        return

    # 5. Fetch scene from Stash
    scene = None
    try:
        scene_data = stash.find_scene(scene_id)
        if not scene_data:
            logger.error("Scene %s not found in Stash.", scene_id)
            return

        scene = StashSceneModel(
            **scene_data, stashdb_endpoint_substr=config.STASHDB_ENDPOINT_SUBSTR
        )
    except ValidationError as e:
        logger.exception("Scene data validation failed: %s", e)
        return
    except Exception as e:
        logger.exception("Unexpected error fetching scene: %s", e)
        return

    logger.info("Processing scene: %s", scene.title)

    # 6. Skip scene if it has ignored tags
    ignored_tag = has_ignored_tag(scene, config.IGNORE_TAGS)
    if ignored_tag:
        logger.info(
            "Scene '%s' skipped due to ignored tag: %s", scene.title, ignored_tag
        )
        return

    # 7. Process scene with Whisparr
    whisparr = WhisparrInterface(config=config, stash_scene=scene)
    try:
        whisparr.process_scene()
        logger.info("Scene processing completed successfully.")
    except WhisparrError as e:
        logger.exception("Whisparr processing error: %s", e)
    except Exception as e:
        logger.exception("Unexpected error during scene processing: %s", e)
