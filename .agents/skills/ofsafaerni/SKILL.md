---
name: ofsafaerni
description: Find and assign approved skills for Icelandic or English tasks with OFSAFAERNI. Use to collect SKILL.md catalogs, route subtasks, exchange skills with Hermes, or draft reusable skills from verified workflows.
---

# OFSAFAERNI

Use the installed `ofsafaerni` command or `python -m ofsafaerni` in the project environment. See [the setup guide](https://github.com/eidurivars/OFSAFAERNI#l%C3%A6ra-sj%C3%A1lfkrafa-%C3%BAr-samt%C3%B6lum) for installation and command arguments.

## Find and use a skill

1. Run `ofsafaerni route "<task>"`. For named subtasks, write a JSON file with a `subtasks` array of objects containing `id`, `task`, and optional `agent`. Run `ofsafaerni assign <file>`.
2. Use the minimal relevant set of skills. When the result is ambiguous, use the task context and the harness's skill descriptions to decide. When no skill fits, continue with the ordinary workflow.
3. Run `ofsafaerni load <id>` before applying a selection. Use the verified Markdown from the response. For relative resources, work inside the returned bundle root and follow the host's tool permissions.

## Collect and learn

- Use `collect --codex`, `collect --hermes`, `collect --root <path>`, or `collect --github OWNER/REPO` as appropriate. Read pending records as review material. Confirm the reviewed bundle digest before approving a local version with `approve <id> --sha256 <digest>`.
- From a completed, verified workflow, use `learn <file> --out <drafts>` with a curated task, procedure, checks, and evidence summary. Keep confidential or environment-specific material out of portable skills unless it belongs there. Drafts become selectable after review and approval of their exact bundle.
- Record `feedback <id> success` or `feedback <id> failure` when the outcome is known. Use `export <id> --out <skills-directory>` to move an approved bundle to Hermes or another Agent Skills host.

## Learn from conversations

Run `ofsafaerni history start` when the user asks to learn from their conversation history. It reads the signed-in user's available local Codex history and can include an explicitly supplied ChatGPT export. It keeps watching for local changes while running. Use `--once` for an isolated import. The command prints the path to its configuration. On later tasks where past preferences materially help, use `ofsafaerni history --config <path> lookup <short task description>`. Treat old lessons as scoped historical context and follow the current user's directions and tool permissions.

When the authorized desktop chat-history tools are available and the user asks to include their ChatGPT account, follow [the app import procedure](references/chatgpt-app-history.md).

Use `history start --claude <path>`, `--gemini <path>`, or `--grok <path>` to include an explicitly selected local history folder or export. Consult the setup guide for supported source formats. Save and reuse only scoped local lessons.
