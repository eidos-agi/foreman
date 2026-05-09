---
name: delegate-with-foreman
description: Delegate scoped coding tasks to AI workers through Foreman. Use when a coding task can be split into independent implementation slices that should run in isolated git worktrees while Codex remains architect and QA.
---

# Delegate With Foreman

Use Foreman when the user explicitly wants Codex to parallelize coding work through AI coding workers, or when a repo task has independent file-scoped implementation slices.

## Operating Model

- Codex is architect and QA.
- Claude Code, Codex CLI, Gemini CLI, Aider, opencode, and local Gemma workers are engineers.
- Each worker gets one concrete task, one git worktree, one branch, and one verification command.
- Codex collects diffs, runs or reviews verification, then chooses merge, PR, or discard.

## Tool Flow

1. Split work into independent specs with acceptance criteria.
2. Choose the worker engine: `claude` by default, `codex` for hard reasoning/QA, `gemini` for broad alternate passes, `aider` for narrow patch tasks, `opencode` for alternate agent/model runs, `gemma4` for throttled local Gemma-family runs, and `smoke` only for plumbing tests.
3. Call `foreman_delegate` once per independent task.
4. Treat `foreman_delegate` as fire-and-return: record the returned `worker_id`, `run_id`, worktree path, and log path.
5. Use `foreman_status` for a cheap lifecycle check and `foreman_tail` for bounded recent logs while workers run.
6. Use `foreman_collect` to review changed files and diff.
7. Run final verification yourself from the parent repo before `foreman_finalize`.

For Slack or other remote-adjacent callers, prefer the control-plane path when available: submit a structured job with `foreman control-submit`, let a local or rented-machine agent lease it with `foreman agent-run --once`, then report back from `control-status`. This keeps Foreman reusable while preserving the same isolated worktree execution model.

## Worker Spec Template

```text
Goal:
Acceptance criteria:
Files in scope:
Files out of scope:
Verification command:
Notes:
```

Keep worker specs narrow. Do not delegate broad architecture, secrets, deploys, money movement, destructive git actions, or human-only approvals.
