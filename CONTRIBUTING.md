# Contributing

The candidate is currently private pending public/license approval. Once public, send a minimal reproducible issue or a focused PR. Do not invent activity, edit production configs, attach credentials or upload private artifacts. Contributions must be yours to submit; identify any copied code and its license/notice requirements. No CLA or email contact is invented here.

Use Python 3.11 or 3.12. From a disposable virtual environment:

```powershell
python -m pip install -r requirements-build.lock
python scripts/build_candidate.py
python -m pip install --no-deps --force-reinstall <built-wheel>
python -m unittest discover -s tests -v
python -m miexiang_publish offline-demo
python scripts/audit_public.py
```

Set TEMP/TMP and MIEXIANG_PUBLISH_DATA_DIR to a disposable location of your choice before running tests. Tests never need provider credentials. The baseline GitHub-only job skips the installed COS SDK test. The optional COS job installs `requirements-cos.lock` and runs the same suite, skipping the absent-extra test. Neither job publishes.

Changes must preserve no-overwrite, explicit identity, bounded consumers, HTTPS host restrictions, stable manifest replay and secret-free outputs. Cover changed behavior and failure cases. Record native environments actually run; mark others NOT_RUN. The CI workflow has read-only permissions and no release job or provider secrets. Do not use pull_request_target to execute contributor code with secrets.
