# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project uses semantic
versioning.

## [0.3.1] - 2026-05-20

### Added
- PyPI packaging metadata for `eidos-foreman`.
- Trusted-publisher GitHub Actions release workflow.
- CI workflow for tests, build verification, and installed CLI smoke checks.
- Pytest coverage for CLI help, MCP tool listing, and Foreman smoke behavior.
- FOSS and security policy files.

### Changed
- MCP shim can resolve the Foreman CLI from either a local checkout or an
  installed wheel.
