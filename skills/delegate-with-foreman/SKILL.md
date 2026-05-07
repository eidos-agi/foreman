---
name: delegate-with-foreman
description: Delegate scoped coding tasks to AI workers through Foreman. Use when a coding task can be split into independent implementation slices that should run in isolated git worktrees while Codex remains architect and QA.
---

# Delegate With Foreman

Use Foreman when the user explicitly wants Codex to parallelize coding work through AI coding workers, or when a repo task has independent file-scoped implementation slices.

## Operating Model

- Codex is architect and QA.
- Claude Code, Codex CLI, Gemini CLI, and Aider workers are engineers.
- Each worker gets one concrete task, one git worktree, one branch, and one verification command.
- Codex collects diffs, runs or reviews verification, then chooses merge, PR, or discard.

## Tool Flow

1. Split work into independent specs with acceptance criteria.
2. Choose the worker engine: `claude` by default, `codex` for hard reasoning/QA, `gemini` for broad alternate passes, `aider` for narrow patch tasks, `smoke` only for plumbing tests.
3. Call `foreman_delegate` once per independent task.
4. Use `foreman_list` and `foreman_tail` while workers run.
5. Use `foreman_collect` to review changed files and diff.
6. Run final verification yourself from the parent repo before `foreman_finalize`.

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
