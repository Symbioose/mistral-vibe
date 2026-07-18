Use `task` to delegate work to autonomous subagents.

When to use:
- The request contains two or more independent units of work (e.g. "add validation to the parser, document the config format, fix the lint errors"): spawn one subagent per unit, in parallel in a single turn, rather than working through the units sequentially yourself. This is the default for decomposable work; the user does not need to ask for it.
- Units are independent when neither needs the other's output and they touch different files. Never split a single tightly coupled unit across subagents.
- Stay inline for simple lookups: to read a specific file use `read_file`, and to find a specific symbol use `grep`.

Usage:
- Pick the `agent` that fits the work; see the available subagents in your system prompt. Use `worker` for anything that changes files and `explore` for read-only research.
- Provide a detailed, self-contained task description and state exactly what the subagent should return, since it runs autonomously and its only output is a final message. Subagents cannot ask the user questions.
- Once delegated, do not duplicate that work yourself; the subagent's result is not shown to the user, so summarize it back to them.
- Explore-style subagents run read-only: they cannot modify files.
- Worker-style subagents (e.g. `worker`) edit files in their own isolated git worktree on a dedicated branch; the parent checkout is untouched until the branch is merged. The task result reports the branch, the files changed, and any conflicting paths — after workers finish, merge their branches (e.g. `git merge <branch>`) and verify the combined result.
