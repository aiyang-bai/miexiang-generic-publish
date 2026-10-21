# Miexiang Generic Publisher

[中文说明](README.zh-CN.md)

Publish one explicitly identified software artifact, then fetch the final consumer URL and verify its size and SHA-256. Keep retries tied to product, channel and version; reject changed bytes for the same identity.

**Version: 0.2.0rc2 (corrected public candidate). Licensed under MIT.**
The distribution has no required PyPI runtime dependencies. GitHub publishing uses the separately installed GitHub CLI (`gh`) and its OS credential store. COS is an optional experimental adapter, `IMPLEMENTED_UNVERIFIED` against a real service.

[Download corrected 0.2.0rc2](https://github.com/aiyang-bai/miexiang-generic-publish/releases/tag/candidate-miexiang-generic-publish-miexiang-generic-publish-0.2.0rc2) · [Actual public verification](docs/releases/0.2.0rc2.md)

rc1 is retained as a prior candidate with a known branch-replay defect and an open historical Git metadata item; use rc2.

## Install and verify without an account

Use Python 3.11 or 3.12 (declared range). Windows/Python 3.11 is locally verified; the Windows Python 3.11/3.12 CI matrix passed. Native Linux/macOS are NOT_RUN.

From a source checkout:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install .
.venv/Scripts/miexiang-publish offline-demo
```

Or install the [public wheel](https://github.com/aiyang-bai/miexiang-generic-publish/releases/download/candidate-miexiang-generic-publish-miexiang-generic-publish-0.2.0rc2/miexiang_generic_publish-0.2.0rc2-py3-none-any.whl) with `python -m pip install --no-deps <wheel-path>`, then run `miexiang-publish offline-demo`. No PyPI release is claimed. The demo creates synthetic in-memory evidence, checks local hashes, length failures, conflict behavior and manifest identity. It returns `OFFLINE_FIXTURE_PASS`, `REMOTE_VERIFICATION=NOT_RUN`, and no public release manifest. It never reads profiles, authenticates, contacts a provider or uploads.

## Data and configuration

Runtime files never belong in site-packages. Set the data directory before using the library; the CLI supports:

```powershell
miexiang-publish --data-dir "./publisher data" offline-demo
miexiang-publish --data-dir "./publisher data" --profile "./profile.json" auth-status
```

Precedence: `--data-dir` > `MIEXIANG_PUBLISH_DATA_DIR` > OS user data directory. Defaults: `%LOCALAPPDATA%/miexiang-publish` on Windows, `~/Library/Application Support/miexiang-publish` on macOS, `$XDG_DATA_HOME/miexiang-publish` (or `~/.local/share/miexiang-publish`) on Linux. These platform branches have unit coverage; that is not a claim of native Linux/macOS execution. The default profile is `profile.json` in that directory; `--profile` overrides it. Locks, staging and reports share the selected data root. API users may call `miexiang_publish.paths.configure_runtime(path)` once before concurrent work. It is process-wide configuration; changing it while another thread publishes is unsupported. Use the same data root for all processes sharing release identities; separate roots or machines do not share local locks. Runtime roots reject symlink/junction traversal. A failed override never falls back to another disk.

Copy [the disabled GitHub profile](examples/github.profile.json) outside the checkout, replace `example/release-fixture` with your own explicitly approved public test repository, and grant write authorization only when you intend remote writes. `cn-software-v1` is the retained profile-format identifier, not a China availability guarantee. This project neither creates accounts nor grants repository rights.

## Authorized real publishing

Install `gh` separately, log in interactively using its OS credential store, and verify the intended account yourself. Environment token overrides and plaintext credential backends are intentionally rejected. Never paste tokens into configuration. GitHub publishing creates a draft release, uploads without `--clobber`, publishes the release, fetches public asset bytes, and stores the manifest through the repository contents API. The account needs release/tag and repository contents write access. Use a dedicated artifact repository/branch rather than an unrelated production source repository.

```powershell
miexiang-publish --profile ./profile.json preview --product-id example-tool --version 1.0.0 --channel test --artifact ./example.bin
miexiang-publish --profile ./profile.json publish --product-id example-tool --version 1.0.0 --channel test --artifact ./example.bin
```

`preview` is read-only but can contact providers; it is not offline. `PREVIEW_READY` describes a completed preview; check each provider's readiness/identity before deciding to publish. `publish` requires explicit profile authorization. Public consumers must be able to retrieve both artifact and manifest without authentication. Installation, signing, malware review, geographically diverse availability and uptime are outside byte verification.

[Status codes and retry rules](docs/agent-usage.md) · [Contribution checks](CONTRIBUTING.md) · [Maintenance](MAINTENANCE.md) · [Limitations](docs/validation.md)

## Optional COS adapter

`python -m pip install '.[cos]'` installs the pinned direct SDK. To reproduce the tested full optional dependency set, install `requirements-cos.lock`. The GitHub-only `requirements.lock` is intentionally empty. COS uses named Windows Credential Manager references; no secrets are stored in profiles. No account-free command needs this extra. Missing SDK errors name the extra. Real COS uploads, policies, costs and reachability remain **IMPLEMENTED_UNVERIFIED**; mocked and installed-SDK header tests are not real COS E2E.

## Provenance and license

This independent repository is a reviewed snapshot of a private predecessor, with fresh history and no inherited production configuration. The exact source commit and exclusions are described in [provenance](docs/provenance.md). No global skill is installed or modified. The legacy candidate-freeze helper is retained for regression coverage of predecessor evidence contracts; it is not the OSS release builder and its synthetic test evidence is not provider proof.

Copyright (c) 2026 aiyang-bai. The rights holder confirmed publication rights and authorized the project [MIT license](LICENSE). Separately installed dependencies retain their own [third-party notices](THIRD_PARTY_NOTICES.md).
