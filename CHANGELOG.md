# Changelog

All notable changes to needle. Format roughly follows
[Keep a Changelog](https://keepachangelog.com); dates
are ISO 8601 in UTC.

Pre-existing release tags (if any) are still visible
via `git log --tags --oneline`; this file starts
empty and is filled forward from this point.

## [Unreleased]

## [0.2.1] - 2026-05-22

### Fixed
- `GET /` liveness now reports `version: "0.2.1"`
  (or whatever needle's current tag is) instead of
  inheriting url2code's hardcoded engine version
  (`"1.0.7"`). The fix lives upstream in url2code
  v1.0.8 (ApiConfig.version is now overridable);
  needle's tools.yaml sets `api.version: "0.2.1"`
  to take advantage of it.
- Dockerfile now pins `URL2CODE_TAG=1.0.8` as the
  default base image. Previous releases used
  `latest`, which made build outputs depend on
  whatever url2code happened to be tagged latest
  at build time.

### Notes
- No tests added in this patch; the version
  override is covered upstream in url2code's
  test_executor.py.

## [0.2.0] - 2026-05-22

Robustness sprint -- no new features, every change
tightens an existing failure mode.

### Added -- container hardening (NEEDLE1)
- docker-compose.yaml: read_only root filesystem with
  tmpfs /tmp, cap_drop ALL, security_opt
  no-new-privileges. Same shape as lwe v0.9.0.

### Added -- per-endpoint timeouts (NEEDLE2)
- Every endpoint now pins command.timeout_seconds
  explicitly:
  - /identify          30s
  - /timestamps        60s
  - admin-library-add  120s
  - admin-fine-build   300s
  - admin-library-list 10s
  - admin-categories   5s (was already pinned)
  Two new tests in test_config.py hold the bounds so
  a future YAML edit that wedges in 0s or 9999s fires
  in CI before the image builds.

### Added -- shell-wrapper hardening (NEEDLE3)
- bin/audfprint: set -eu (was set -e), and a path-safety
  guard that refuses any -d / --dbase argument outside
  /data/ or containing a `..` segment. Defends the
  `{category}` and `{id}` substitution against a
  malicious request field. Five new shell-level tests
  in tests/test_audfprint_wrapper.py cover the refusal
  branches.

### Added -- operator integrity helper (NEEDLE4)
- bin/needle-data-check (new). POSIX-sh script that
  walks /data, asserts every .pklz is non-empty and
  parseable, and warns on disk usage. Exit codes are
  designed for a cron-driven alert pipeline (Apprise
  example in DEPLOYMENT.md). Six new tests in
  test_data_check.py cover every exit-code branch.

### Added -- supply-chain step 3 (NEEDLE5)
- .github/workflows/cve-scan.yml: daily Grype scan
  at 11:00 UTC, pulling the SBOM that release.yml
  attached via oras and uploading SARIF to the
  GitHub Security tab. Closes the supply-chain plan
  whose first two steps (syft + oras attach) were
  already in place.

### Docs (NEEDLE6)
- DEPLOYMENT.md gains a Backups / Restore / Integrity
  section: weekly rsync snapshot strategy, the restore
  procedure, the new needle-data-check helper, and a
  pattern for piping its output into Apprise on a
  cron schedule.

### Tests
- 13 new tests (40 -> 53 static). E2E unchanged.
