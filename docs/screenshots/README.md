# Project Screenshots

Desktop screenshots of the RepoPilot Agent web interface, captured on 2026-09-05.
The empty workspace screenshot (01) was refreshed on 2026-09-06.
The GitHub workflow screenshots (09 and 10) were added on 2026-09-06.

All images are original **1920 x 1080 PNGs**, captured at **100% browser zoom**
and **device pixel ratio 1**. These are viewport captures, without resizing,
image compositing, or full-page stitching. Some views are scrolled to their
main review content; the application's layout and styles are unchanged.

Screenshot 01 shows the application's initial workspace with no task or results.
Screenshots 02-08 use the actual local application with deterministic
demonstration data for a fictional `search-service` repository. Tasks, paths,
patches, test results, usage figures, and approvals
are examples, not records of live repository or provider activity. No API keys
or personal filesystem paths are included. Capture required no backend API
calls, Git commands, commits, pushes, or external writes. Commands displayed
in the delivery screenshots are example suggestions and were not executed.

Screenshots 09 and 10 show **GitHub** as the repository source, using selected public
records from [openai/codex](https://github.com/openai/codex), retrieved through
read-only GitHub REST requests on 2026-09-06. The application renders the saved
response in its normal GitHub view. The Issue and PR task inputs were populated
using the existing **Use as task** buttons. These screenshots show task setup;
no repository was cloned, Agent task started, LLM call made, or Git operation
performed. Review and CI statuses are the upstream records at capture time.

Sources: [Issue #43148](https://github.com/openai/codex/issues/43148),
[its discussion](https://github.com/openai/codex/issues/43148#issuecomment-5556733822),
[PR #31456](https://github.com/openai/codex/pull/31456),
[changed files](https://github.com/openai/codex/pull/31456/files), and
[checks](https://github.com/openai/codex/pull/31456/checks).
PR head: `a1709a444da697b4a9d71acbfeabf4d273702d93`.
Body and comment previews use the application's existing 800-character limit.

| Image | View | Shown content |
| --- | --- | --- |
| [01-workspace-overview.png](01-workspace-overview.png) | Empty workspace | Default task settings and run modes, with no task entered or results loaded. |
| [02-exact-write-approval.png](02-exact-write-approval.png) | Write approval | Exact file scope, inspectable diff, approve and reject controls. |
| [03-task-checkpoints.png](03-task-checkpoints.png) | Task activity | Execution phases, task controls, latest checkpoint, and saved history. |
| [04-agent-trajectory.png](04-agent-trajectory.png) | Trajectory | Event metrics, integrity, action sequence, and replay controls. |
| [05-validation-results.png](05-validation-results.png) | Validation results | Passing regression output and the execution plan. |
| [06-delivery-readiness.png](06-delivery-readiness.png) | Delivery readiness | Local delivery prerequisites and suggested next steps. |
| [07-delivery-draft.png](07-delivery-draft.png) | Delivery draft | Change summary, validation notes, PR title and body, and diff statistics. |
| [08-run-history.png](08-run-history.png) | Run history | Saved runs, pinning, run actions, and selected run details. |
| [09-github-issue-task.png](09-github-issue-task.png) | Issue to task | Issue #43148 imported with its title, labels, URL, body, and comments; sandbox run mode selected. |
| [10-github-pr-task.png](10-github-pr-task.png) | PR to task | PR #31456 imported as a task, with its branch, changed files, discussion, and review context. |

For a compact project introduction, start with images **01, 02, 04, 05, and 07**.
Include **09 and 10** to demonstrate GitHub task intake.
