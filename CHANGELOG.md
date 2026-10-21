# Changelog

## 0.2.0rc1 — first public candidate (2026-10-22)

- Extract independent reviewed snapshot with fresh history.
- Move staging, locks, profiles and reports out of installed package directories; add environment and CLI data-root controls.
- Make COS optional and keep its real-service status unverified.
- Add synthetic offline demo, install/path/concurrency regression coverage and bounded maintenance support.
- Preserve valid wheel compatibility tags while validating explicit product/version identity.
- Rewrite documentation, disabled examples, dependency notices and read-only CI.

Breaking operational detail: MANIFEST_PATH is relative to the configured data root. Old package-adjacent profiles/data are not discovered or migrated automatically. Retained CLI/import/package names and V1 manifest identity semantics remain compatible.

The predecessor's historical tests and releases are not a release or real-provider validation of this version. The rights holder approved MIT licensing and the first public candidate release on 2026-10-22.
