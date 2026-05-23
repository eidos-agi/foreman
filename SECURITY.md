# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Foreman, please report it
responsibly.

Email: daniel@eidosagi.com

Please include:
- A description of the vulnerability
- Steps to reproduce
- Potential impact
- Whether the issue affects CLI, MCP, worker processes, or generated worktrees

We will acknowledge reports within 48 hours and aim to provide a fix within 7
days for critical issues.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |

## Disclosure Policy

We follow coordinated disclosure. Please give us reasonable time to address the
issue before public disclosure.

## Release Security Checks

Before publishing a Foreman release, run:

```bash
gitleaks detect --source . --no-git --redact --exit-code 1
python -m pip_audit --path .venv/lib/python3.11/site-packages
```

Release-blocking findings must be fixed before creating a version tag.
