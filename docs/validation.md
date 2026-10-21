# Validation scope

Windows x64 / CPython 3.11 is the locally exercised platform. Python 3.12, native Linux and native macOS are NOT_RUN until actual results exist. CI configuration is not a CI result. Do not expand the declared Python range based on import success alone.

Fresh candidate tests preserve original coverage: identity/path handling, streams, hashes/lengths, bounded HTTPS hosts, authorization, no-overwrite, SDK header bridge, draft recovery, manifest field stability, remote replay, and source freeze binding. Optional installed-SDK checks and missing-extra checks execute in separate environments and state their skips. Additional tests exercise directory precedence, Unicode/spaces, rejection of unwritable/link roots and process contention. The offline demo is synthetic throughout.

Packaging checks build a wheel and source distribution, enumerate members, scan tracked and packaged text, install the wheel without dependencies into a fresh virtual environment, run from outside the checkout without PYTHONPATH, exercise console and module entry points, and verify no package-directory writes. Private receipts record exact versions, commands, commit, timestamps, exit codes and hashes. No live provider credentials are supplied to CI or local fixture tests.

LIVE_E2E: NOT_RUN for this candidate. COS: IMPLEMENTED_UNVERIFIED. Regional reachability, signing, SmartScreen, installer execution, external adoption and production acceptance: NOT_PROVEN. A historical GitHub predecessor test is context only. Public release requires explicit approval and an authorized test repository; real artifact and manifest downloads are required before claiming public byte verification.
