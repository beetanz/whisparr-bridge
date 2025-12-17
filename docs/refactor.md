Pre-Refactor Checklist

1. Confirm Core Logic is Stable

✅ process_single_scene works consistently in CLI and plugin modes.

✅ Single-file operations succeed, with proper logging and failure handling.

2. Ensure Config & Logging Separation

✅ preprocessor() fully handles configuration loading.

✅ Logger is initialized early and passed/shared consistently.

3. Identify Failure Paths

✅ All error/edge cases are clearly handled (e.g., scene not found, validation failure).

✅ WhisparrError vs generic exceptions are differentiated.

✅ FileManager handles file existence/move reliably.

4. Break Down Responsibilities

Core logic (process_single_scene) orchestrates.

StashHelpers handles scene fetch/validation.

WhisparrInterface handles all Whisparr operations.

FileManager handles file operations.

CLI vs Plugin layer only parses args or hook data, then calls core.

5. Add Type Hints

✅ Already done for bulk_processor and main.

Consider adding type hints for WhisparrInterface, StashHelpers, and FileManager methods.

6. Decide on Testing Strategy

✅ Manual testing confirmed single-file operations.

Optional: add unit tests for:

FileManager move/existence logic.

WhisparrInterface scene find/create.

StashHelpers scene fetch & validation.

7. Plan Refactor

Keep process_single_scene as the main orchestrator.

Move CLI and Plugin handling into separate entry points (without changing logic).

Ensure new structure allows injecting mocks/stubs for testing.



```mermaid
flowchart TD
    A[Core Logic: process_single_scene] --> B[Stash Interface: StashHelpers]
    B -->|Scene not found| Z[Return False]
    A --> C[Check Ignored Tags]
    C -->|Ignored| Z
    A --> D[Whisparr Interface: WhisparrInterface]
    D --> E[Find or Create Scene]
    E -->|Failure| Z
    D --> F[Move Files: FileManager]
    F -->|Failure| Z
    D --> G[Manual Import & Optional Rename/Refresh]
    G -->|Failure| Z
    A --> H[Return True on Success]

    subgraph Interface Layer
        I[CLI] --> A
        J[Plugin Hook] --> A
    end
```


```mermaid
classDiagram
    class process_single_scene {
        +True/False process(scene_id)
        +orchestrates entire flow
    }

    class StashHelpers {
        +open_conn()
        +find_scene(scene_id)
    }

    class StashSceneModel {
        +title
        +tags
        +files
        +stash_ids
        +stashdb_id()
        +paths()
    }

    class WhisparrInterface {
        +process_scene()
        +find_existing_scene()
        +create_scene()
        +process_stash_files()
        +import_stash_file()
        +_get_manual_import_preview()
        +_get_matching_preview_file()
        +_execute_manual_import()
        +_queue_command()
        +get_default_quality_profile()
        +get_default_root_folder()
    }

    class FileManager {
        +exists()
        +move(source)
    }

    class CLI {
        +parse_args()
        +main()
    }

    class Plugin {
        +main_hook()
    }

    %% Relationships
    process_single_scene --> StashHelpers
    process_single_scene --> StashSceneModel
    process_single_scene --> WhisparrInterface
    WhisparrInterface --> FileManager
    CLI --> process_single_scene
    Plugin --> process_single_scene
```