# Slack Integration

Future generic Slack adapter for creating and monitoring Foreman work.

A Slack-facing client should:

- resolve the intended repo
- build a structured Foreman job
- submit it to the control plane
- report status back to the Slack thread
- require approval for merge, push, deploy, or destructive actions

Foreman should remain reusable. Bot-specific behavior belongs outside Foreman; this package should only contain generic Slack transport helpers if they prove reusable.
