# Foreman SDK

Shared client and schema package for future Foreman integrations.

Intended ownership:

- `ControlJobSpec` schema
- event/status enums
- HTTP client for the control plane
- typed helpers for Slack, web UI, and other clients

The first schema is still in the CLI runtime while the product surface stabilizes. Extract it here when the control-plane API stops moving every turn.
