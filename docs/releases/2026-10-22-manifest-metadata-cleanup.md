# Manifest metadata cleanup — 2026-10-22

The Owner authorized a targeted history rewrite of `release-manifests` to replace earlier account-default email metadata with the verified public GitHub noreply identity. The rc1 manifest commit changed only its author/committer email fields; the rc2 manifest commit changed only its parent reference. Current documentation was updated with an ordinary new commit. Source history and release tags were not rewritten.

## Current consumer entries

- [rc2 manifest](https://raw.githubusercontent.com/aiyang-bai/miexiang-generic-publish/d660879b1dd8ff2f3cdadbb45da86698e55d7d48/manifests/miexiang-generic-publish/candidate/0.2.0rc2/release-manifest.json): 1982 bytes, SHA-256 `84b442c4da8da15280d85b20ec48c884181942b0248bc3fc1b47215928399eda`.
- [rc1 manifest](https://raw.githubusercontent.com/aiyang-bai/miexiang-generic-publish/a3835f041c183d2b2c0d6ed5bf5a1b04a3e675c0/manifests/miexiang-generic-publish/candidate/0.2.0rc1/release-manifest.json): 1956 bytes, SHA-256 `0d00df36cab182b5ac45418d6fede7521fb0495a6c5987ed5c707e0ade96b974`.

Both manifest files are byte-for-byte identical to their original publication versions. Their commit-based URLs changed because commit metadata and parent identity are part of Git object identity. Original verification observations, including historical URLs, are retained; `current_manifest` in the [verification JSON](0.2.0rc2-verification.json) identifies the current rc2 entry point.

## Actual verification

- Anonymous HTTPS downloads of both current manifests, the rc2 wheel and its checksum file matched the original sizes and SHA-256 values.
- The downloaded wheel installed in a fresh Windows virtual environment without COS. The installed CLI/library/data-directory smoke and package version checks passed.
- The released rc2 executor returned `IDEMPOTENT_ALREADY_PUBLISHED`, zero uploaded bytes, 45,258 freshly downloaded verification bytes and `REMOTE_WRITE_PERFORMED=false`.
- Before/after snapshots confirmed the replay did not change the manifest branch, release asset IDs/digests/timestamps or source tag objects. No package, tag or version was replaced.
- The two replacement commits have unchanged file trees. Current public refs no longer reach the replaced email metadata. Private original evidence and recovery material remain preserved locally and are not published.

## Remaining cache limitation

Known old GitHub commit API and web endpoints returned HTTP 200 at the 2026-10-22 check. The old rc2 commit can still reference its old rc1 parent through those endpoints. Public branch repair does not establish deletion of cached objects or other people's clones. Cache removal remains **NOT_PROVEN**; a support request requires separate Owner review and has not been sent.

[GitHub's official removal guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository) describes changed commit hashes, retained caches/clones and support eligibility limitations. This repository does not promise that a force push removes all historical copies.

COS remains **IMPLEMENTED_UNVERIFIED**. This repair does not add native Linux/macOS, installer/signing or broad geographic validation.
