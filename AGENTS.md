# AGENTS.md

This file follows the [AGENTS.md standard](https://agents-md.org). Claude Code, Antigravity,
Gemini CLI, Cursor, OpenCode, and other compatible tools read it.

Read **[README.md](README.md)**. It is the single source for this repository: the commands,
the safety rules, the architecture of `migrate_logi_ergo.py`, and the known limits.

Two rules apply to every session here:

1. Menu items 4 to 7, `--apply`, and `--restore` need explicit consent. Ask the user in the
   current session before you run one of them. Those paths stop the software of the user and
   write to live application state. The read-only paths need no consent.
2. Keep every operating system difference inside a `Platform` subclass, and keep the plan
   phase free of writes. The [Development notes](README.md#development-notes) section
   explains both.

Do not copy content from README.md into this file. Update README.md instead.
