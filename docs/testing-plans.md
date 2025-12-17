Top 5 Priority Tests

process_single_scene success path

Mock StashHelpers to return a valid scene.

Mock WhisparrInterface so that scene is found or created, files are moved, and manual import succeeds.

Verify that the function returns True.

Why: This is your “happy path” and the main orchestrator.

Ignored tags handling

Provide a scene with a tag that’s in config.IGNORE_TAGS.

Verify that process_single_scene returns early and no Whisparr processing occurs.

Why: Skipping scenes early is a key guard clause.

FileManager file not found / move failure

Mock FileManager.exists() to raise FileNotFoundError or return False.

Mock FileManager.move() to simulate a failure.

Verify that the orchestration still continues without crashing (or logs the error).

Why: Ensures resilience to missing or locked files.

Whisparr scene creation failure

Mock WhisparrInterface.create_scene() to raise WhisparrError.

Verify that process_single_scene returns False and logs the failure.

Why: Confirms that your error handling works for external API failures.

Manual import already done

Mock _get_manual_import_preview() to return no files to import.

Verify that process_single_scene completes successfully and does not call _execute_manual_import().

Why: Ensures idempotency — rerunning the same scene doesn’t break anything.

✅ Bonus Tip:
Use dependency injection / mocking for StashHelpers, WhisparrInterface, and FileManager. That way you can test the orchestration logic in process_single_scene without touching the file system or real APIs.



Unit Testing Focus Areas

Unit tests are for isolated components, typically testing one class or function at a time.

File Operations (FileManager)

Test file existence logic (source vs destination vs same file).

Test move operations, including retries and error handling.

Test path mapping (map_to_local_fs) with different server/local path combinations.

Stash Scene Handling (StashHelpers)

Validate scene fetching returns StashSceneModel.

Handle edge cases: missing scene, validation errors, empty fields.

Check ignored tags logic.

Whisparr Operations (WhisparrInterface)

Scene finding/creation logic: returns correct object or raises expected errors.

File processing: correctly maps and moves files (can mock FileManager).

Manual import & command queuing: returns success/failure without calling the real API.

HTTP Layer (http_json)

Ensure correct exceptions are raised on HTTP errors.

JSON parsing into models works correctly.

Mock responses for both success and failure scenarios.

Integration Testing Focus Areas

Integration tests cover interactions between components, closer to real workflows.

Single Scene Processing

Run process_single_scene end-to-end with a mock Stash scene.

Verify: ignored tags skip, scene creation triggers, files moved, manual import invoked.

Whisparr + FileManager Integration

Mock the Whisparr API but test that FileManager moves are actually attempted.

Test scenarios where files exist or don’t exist in the expected locations.

Full CLI or Plugin Workflow

Trigger main workflow with a test config.

Ensure preprocessor loads config, process_single_scene orchestrates correctly.

Confirm logs and return values reflect expected success/failure outcomes.

Edge Case Scenarios

Scene not found in Stash.

Scene already exists in Whisparr.

HTTP failures (timeouts, 500 errors).