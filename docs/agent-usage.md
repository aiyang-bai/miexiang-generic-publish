# Agent contract

Keep package `miexiang-generic-publish`, import `miexiang_publish`, and CLI `miexiang-publish`. Global options precede the subcommand. Do not embed credentials, personal paths or private notes in public metadata. Use the disabled example only as a template. A configured public repository is not itself authorization for an Agent to publish.

| Status/code | Meaning and next action |
| --- | --- |
| OFFLINE_FIXTURE_PASS | Local synthetic checks only; remote verification NOT_RUN. |
| PREVIEW_READY | Preview completed, no write. Inspect PROVIDER_PREVIEW readiness and identity; it may be NOT_READY. |
| AUTH_REQUIRED / AUTH_NOT_READY | Profile, OS credential backend or repository access not ready; require normal user setup, never print credentials. |
| PUBLISH_SUCCESS | Required artifact and manifest consumer-byte verification succeeded in this invocation. |
| PUBLISH_SUCCESS_WITH_OPTIONAL_DEGRADED | Primary succeeded; inspect optional provider failures. |
| IDEMPOTENT_ALREADY_PUBLISHED | Existing matching artifact and stable manifest reused after verification; do not create another version just to retry. |
| CONFLICT_VERSION_HASH / VERSION_HASH_CONFLICT | Same identity, different bytes. Stop; select a genuinely new version if intended. |
| PARTIAL | A side effect may exist; inspect exact remote identity before retrying. Never blindly overwrite or delete. |
| HASH_MISMATCH | Length/hash mismatch; no success claim. Reconcile source and consumer bytes. |
| POLICY_REJECTED | Invalid identity, unauthorized channel, data path, metadata, budget or profile. Fix the explicit input. |
| FAILED / REMOTE_CONFLICT | Remote state conflicts or cannot be safely reconciled. Preserve evidence and inspect. |
| LOCK_HELD | Another local run or retained lock holds the identity; do not delete without confirming ownership and process termination. |
| PROVIDER_UNAVAILABLE / DOWNLOAD_FAILED | Report provider/network failure with bounded error; no public byte verification claim. |

CLI success is exit 0; handled failures exit 2. Library failures use `PublishError.code`. `MANIFEST_PATH` is now relative to the configured data directory, preventing absolute local path leakage in public results. Do not interpret relative paths against site-packages. Full local diagnostic paths stay in private operator records.

Compare stable release identity, including artifact names, display names, sizes, hashes, provider URLs and publication timestamps. Observation times may differ on legal replay; matching remote manifest bytes are reused. GitHub server-side immutable release enforcement is reported when present, not inferred from client no-overwrite checks. Local identity lock files survive a crash by design; manual recovery requires confirming the original run ended. Maintenance uses a separate OS lock that releases on process exit.

For library use, select the data root once before constructing adapters. Build `GitHubReleaseAdapter(profile.github, paths.STAGING_ROOT)`, then `GenericPublisher(profile, [adapter])`. `preview` can contact the provider. Avoid changing runtime configuration concurrently. Do not install or modify global Agent skills as part of ordinary use.
