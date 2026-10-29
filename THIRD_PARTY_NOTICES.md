# Third-party notices and dependency review

No third-party library source or binaries are vendored in this repository, wheel or sdist. GitHub-only runtime uses the Python standard library. Python and the separately installed GitHub CLI retain their own licenses. Dependencies below are separately fetched; do not relicense them under the project MIT license. When redistributing dependency binaries/source, retain their accompanying licenses, copyright and notices, including certificate-bundle MPL terms and component-specific notices. No dependency bundle is part of this release candidate.

Initial installed metadata/license review: 2026-10-21. Targeted build-backend and certifi security maintenance: 2026-10-24 (unreleased):

| Distribution | Pinned version | Declared license | Scope |
| --- | --- | --- | --- |
| cos-python-sdk-v5 | 1.9.44 | MIT | optional COS |
| certifi | 2026.10.19 | MPL-2.0 | optional COS transitive / CA bundle |
| charset-normalizer | 3.5.1 | MIT | optional COS transitive |
| crcmod | 1.7 | MIT | optional COS transitive |
| idna | 3.20 | BSD-3-Clause | optional COS transitive |
| pycryptodome | 3.23.0 | BSD + public-domain components; see LICENSE.rst | optional COS transitive |
| requests | 2.34.2 | Apache-2.0 | optional COS transitive |
| six | 1.17.0 | MIT | optional COS transitive |
| urllib3 | 2.8.0 | MIT | optional COS transitive |
| xmltodict | 1.0.4 | MIT | optional COS transitive |
| setuptools | 84.1.0 | MIT | build only; includes its own wheel builder |

`requirements-cos.lock` preserves predecessor optional versions except the targeted certifi CA-bundle security update; no general upgrade was performed. `requirements-build.lock` pins the build backend. These are exact-version constraints, not hash-pinned or signed dependency attestations. Package hashes in the private review packet identify built first-party files, not all possible future dependency downloads. Availability/security drift requires a fresh check when changing dependencies.

Source review found no vendored third-party implementation or attributed copied block in the allowlisted tree. This is a scoped engineering inspection, not proof of all authorship rights. The rights holder aiyang-bai confirmed publication rights and authorized the MIT grant on 2026-10-22; commit author labels alone were not used as ownership evidence.

Build-backend change rationale (2026-10-22): the inherited 65.5.0 failed real Python 3.12 CI. The initially tested 68.2.2 remained affected by PyPI advisories GHSA-5rjg-fvgr-3xxf, GHSA-cx63-2mw6-8hw5 and GHSA-h35f-9h28-mq5c; fixed versions include 78.1.1, 70.0.0 and 83.0.0 respectively. The selected 83.0.0 has no vulnerabilities listed in its PyPI version JSON at review time and requires Python >=3.10. This is a time-bounded advisory check, not a guarantee of vulnerability absence. See https://pypi.org/pypi/setuptools/83.0.0/json . Optional COS pins were not changed.

Security maintenance rationale (2026-10-24, unreleased): a fresh advisory check found setuptools 83.0.0 affected by [GHSA-xmj3-9gf7-h52w](https://github.com/advisories/GHSA-xmj3-9gf7-h52w), fixed in 84.1.0. The separate wheel 0.45.1 tool is affected by [CVE-2026-24049](https://github.com/advisories/GHSA-8rrh-rw8j-w5fx); it is removed because this project only invokes setuptools' backend, which has included wheel building since setuptools 70.1. See the [upstream wheel guidance](https://pypi.org/project/wheel/0.46.2/). No first-party code invokes legacy wheel installation or `wheel unpack`, so these dependency advisories do not establish an exploit in the publisher.

Certifi 2026.7.22 retains an affected emSign trust root; [GHSA-6q48-6c7w-9gw2](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2) lists 2026.10.18 as patched. This change pins 2026.10.19, which restores unaffected roots after that removal; the [PyPI advisory metadata](https://pypi.org/pypi/certifi/2026.7.22/json) recommends 2026.10.19 or later. Only that optional COS transitive pin is updated. GitHub-only runtime remains standard-library-only. These development changes do not replace the already-published rc2 packages or validate real COS service use. Setuptools 84.1.0 and certifi 2026.10.19 had no advisories listed in their PyPI version JSON at this check; that is not exhaustive security assurance.
