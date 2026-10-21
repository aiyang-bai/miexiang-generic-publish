# Third-party notices and dependency review

No third-party library source or binaries are vendored in this repository, wheel or sdist. GitHub-only runtime uses the Python standard library. Python and the separately installed GitHub CLI retain their own licenses. Dependencies below are separately fetched; do not relicense them under the project MIT license. When redistributing dependency binaries/source, retain their accompanying licenses, copyright and notices, including certificate-bundle MPL terms and component-specific notices. No dependency bundle is part of this release candidate.

Reviewed installed distribution metadata and included license files on 2026-10-21:

| Distribution | Pinned version | Declared license | Scope |
| --- | --- | --- | --- |
| cos-python-sdk-v5 | 1.9.44 | MIT | optional COS |
| certifi | 2026.7.22 | MPL-2.0 | optional COS transitive / CA bundle |
| charset-normalizer | 3.5.1 | MIT | optional COS transitive |
| crcmod | 1.7 | MIT | optional COS transitive |
| idna | 3.20 | BSD-3-Clause | optional COS transitive |
| pycryptodome | 3.23.0 | BSD + public-domain components; see LICENSE.rst | optional COS transitive |
| requests | 2.34.2 | Apache-2.0 | optional COS transitive |
| six | 1.17.0 | MIT | optional COS transitive |
| urllib3 | 2.8.0 | MIT | optional COS transitive |
| xmltodict | 1.0.4 | MIT | optional COS transitive |
| setuptools | 83.0.0 | MIT | build only |
| wheel | 0.45.1 | MIT | build only |

`requirements-cos.lock` preserves the predecessor's exact optional dependency versions; no general upgrade was performed. `requirements-build.lock` pins the build backend. These are exact-version constraints, not hash-pinned or signed dependency attestations. Package hashes in the private review packet identify built first-party files, not all possible future dependency downloads. Availability/security drift requires a fresh check when changing dependencies.

Source review found no vendored third-party implementation or attributed copied block in the allowlisted tree. This is a scoped engineering inspection, not proof of all authorship rights. The rights holder aiyang-bai confirmed publication rights and authorized the MIT grant on 2026-10-22; commit author labels alone were not used as ownership evidence.

Build-backend change rationale (2026-10-22): the inherited 65.5.0 failed real Python 3.12 CI. The initially tested 68.2.2 remained affected by PyPI advisories GHSA-5rjg-fvgr-3xxf, GHSA-cx63-2mw6-8hw5 and GHSA-h35f-9h28-mq5c; fixed versions include 78.1.1, 70.0.0 and 83.0.0 respectively. The selected 83.0.0 has no vulnerabilities listed in its PyPI version JSON at review time and requires Python >=3.10. This is a time-bounded advisory check, not a guarantee of vulnerability absence. See https://pypi.org/pypi/setuptools/83.0.0/json . Optional COS pins were not changed.
