# RepoPilot Agent Tutorial

This tutorial walks through a complete RepoPilot Agent workflow, from a first local run to LLM-backed proposals, GitHub context, diff review, validation, repair, and delivery notes.

RepoPilot is designed as a local human-in-the-loop coding agent. It can inspect a repository, understand a task, build a plan, propose file-level changes, preview diffs, wait for approval, apply edits, run validation, and prepare commit or pull request draft text.

## What You Will Learn

By the end of this tutorial, you will know how to:

- Run RepoPilot against a local repository.
- Start the local web UI.
- Use an OpenAI-compatible LLM without storing API keys in the project.
- Analyze a GitHub repository URL through a local cached clone.
- Import GitHub issue or pull request context into a task.
- Review proposed diffs before any file is changed.
- Apply approved proposals and run validation.
- Generate a repair proposal when validation fails.
- Inspect history, memory, Git state, GitHub state, and delivery drafts.

## Prerequisites

Use Python 3.10 or newer:

```bash
python --version
```

Use Git for repository state, GitHub URL cloning, and diff inspection:

```bash
git --version
```

From a fresh checkout:

```bash
git clone https://github.com/CHOS1N11111/RepoPilot-Agent.git
cd RepoPilot-Agent
```

RepoPilot currently has no required third-party Python dependencies. You can run it directly through the root launcher:

```bash
python repopilot.py --help
```

Optional editable install:

```bash
python -m pip install -e .
repopilot --help
```

## API Key Safety

RepoPilot can run without an LLM. When you enable LLM mode, keep secrets outside the repository.

Recommended PowerShell session-only setup:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
$env:REPOPILOT_MODEL = "gpt-4o-mini"
```

Recommended macOS/Linux session-only setup:

```bash
export OPENAI_API_KEY="your-api-key"
export REPOPILOT_MODEL="gpt-4o-mini"
```

Do not commit API keys. Do not paste keys into README files, issues, logs, screenshots, or terminal output. The repository ignores `.env` files, but session-only environment variables are safer for quick testing.

## Step 1: Run A Local No-LLM Analysis

Start with deterministic mode so you can understand the baseline behavior:

```bash
python repopilot.py run --repo . --task "improve validation feedback for failed tests"
```

Read the output in this order:

1. Relevant files: which files RepoPilot selected and why.
2. Related memory: previous local runs that might help the current task.
3. Plan: the proposed implementation steps.
4. Proposed changes: file-level suggestions and risk notes.
5. Recommended validation: commands or manual checks suggested by RepoPilot.
6. Summary: short workflow result.

This mode does not call a model and does not write files.

## Step 2: Run With Validation

Validation commands are allowlisted. For this project, the full unit test suite is:

```bash
python repopilot.py run --repo . --task "check repository health" --validate "python -m unittest discover -s tests"
```

If a validation command is not allowlisted, RepoPilot reports it as rejected instead of running it.

Currently allowed validation prefixes include:

- `python -m unittest`
- `python -m pytest`
- `pytest`
- `npm test`
- `npm run test`
- `npm run lint`
- `ruff check`

## Step 3: Run With An LLM

After setting `OPENAI_API_KEY`, enable LLM mode:

```bash
python repopilot.py run --repo . --task "add clearer docs for the web workflow" --use-llm --model gpt-4o-mini
```

Add validation:

```bash
python repopilot.py run --repo . --task "add clearer docs for the web workflow" --use-llm --model gpt-4o-mini --validate "python -m unittest discover -s tests"
```

If the model call fails or returns invalid JSON, RepoPilot falls back to deterministic planning unless you add:

```bash
--no-llm-fallback
```

Some OpenAI-compatible gateways do not support `response_format`. RepoPilot uses JSON mode by default and automatically retries once without it when a provider rejects or mishandles that option. If the provider still returns a non-JSON response, RepoPilot shows the HTTP status, content type, and a short redacted body preview to help diagnose endpoint, model, or gateway issues.

You can still disable provider-side JSON mode manually for debugging:

```bash
python repopilot.py run --repo . --task "inspect LLM trace fields" --use-llm --no-json-mode
```

You can also set:

```powershell
$env:REPOPILOT_DISABLE_JSON_MODE = "1"
```

If a large patch proposal times out, increase the timeout:

```powershell
$env:REPOPILOT_LLM_TIMEOUT_SECONDS = "240"
```

or pass it per CLI run:

```bash
python repopilot.py run --repo . --task "inspect LLM trace fields" --use-llm --llm-timeout 240
```

To use Codex-like multi-step exploration and virtual edit preparation before planning and proposal generation, enable iterative agent mode:

```bash
python repopilot.py run --repo . --task "fix parser behavior" --use-llm --iterative-agent --agent-max-steps 6
```

This mode lets the LLM choose non-writing actions such as `search_files`, `read_file`, `inspect_repository_map`, `inspect_git_status`, `inspect_diff`, `propose_patch`, and `inspect_proposed_diff` across several smaller calls. Each call returns one `AgentDecision` v2 object and RepoPilot validates it before executing the action:

```json
{
  "version": 2,
  "rationale": "Read the parser implementation selected by search.",
  "action": {"kind": "read_file", "arguments": {"path": "src/parser.py"}},
  "expected_evidence": "The parser implementation and its edge-case behavior.",
  "state_update": {
    "focus": "Understand parser behavior.",
    "add_findings": ["src/parser.py matched the previous search."],
    "add_open_questions": ["How are empty values handled?"],
    "resolve_open_questions": ["Which file implements parsing?"],
    "plan_updates": [],
    "acceptance_updates": []
  },
  "finish_reason": "",
  "user_question": ""
}
```

The action schema is specific to its kind: search uses `query`, read uses `path`, repository-map inspection accepts optional `query` and `limit`, Git status accepts no arguments, diff inspection accepts an optional boolean `staged`, `propose_patch` requires `path`, `expected_sha256`, and exact hunks, `inspect_proposed_diff` accepts no arguments, and finish uses `selected_paths`. The core controller also supports an opt-in `ask_user` action with empty arguments, a non-empty `user_question`, and the same question added to Working State. `plan_updates` upsert bounded plan items with `pending`, `in_progress`, or `completed` status. `acceptance_updates` upsert bounded criteria and attach evidence. RepoPilot rejects extra fields, extra action arguments, unsafe paths, wrong types, oversized hunk payloads, empty expected evidence, and inconsistent finish, question, plan, or acceptance fields. The actions run through RepoPilot's typed runtime and produce persistent decision-action-observation events. They do not write files; real changes still require a reviewed proposal and human approval.

For a change task, the virtual edit sequence is:

1. Call `read_file` for the target and keep its complete-file `sha256`.
2. Call `propose_patch` with that hash and one or more exact old/new text hunks.
3. For another revision of the same file, use the previous proposal's `resulting_sha256`, not the disk hash.
4. Call `inspect_proposed_diff` after the latest revision.
5. Finish only after Working State reports no proposal conflict or uninspected revision.

`propose_patch` applies the hunks to a run-scoped in-memory overlay, runs Python or JSON syntax checks when applicable, and returns a cumulative diff from the real baseline to the latest virtual content. It never calls the file-write path. Every proposal and inspection rechecks the real baseline hash. If another process changes the target, RepoPilot reports `stale_repository`; re-read the file and propose with the new real hash to reset that file's virtual baseline. A stale virtual revision reports `stale_virtual_revision` instead of replacing newer proposed work.

A completed plan update must include `evidence_action_ids`. An acceptance update passes its criterion only when it includes valid evidence ids and a summary. Each id must identify a previously completed evidence-producing observation shown in Working State, such as `read_file`, Repository Map inspection, Git status, or diff inspection. `search_files` returns candidates and is deliberately not sufficient. A virtual proposal proves what the Agent wants to change, not that the task is complete, so `propose_patch`, the current action, a user question, model prose, and `finish` are also not acceptance evidence. Required criteria remain required even if a later model update sets `required` to false.

`finish` is a guarded controller decision. RepoPilot previews the state update, then checks that every plan item is complete, every required acceptance criterion has passed, and every virtual proposal is conflict-free and inspected at its latest revision. When blockers remain, it records a `finish_blocked` event with status `acceptance_incomplete`, adds that observation to Working State, and asks the LLM for another non-writing decision if budget remains. The finish action is not authorized or executed in that case.

After an evidence-backed finish, RepoPilot includes the complete bounded Agent plan and acceptance state in the downstream planner prompt. The planner can therefore turn the investigation trajectory into the final implementation plan instead of reconstructing intent only from file snippets. Deterministic fallback planning remains available if that LLM call fails.

Before each `agent_step_N` call, Context Manager v2 rebuilds the prompt context in this priority order:

1. Agent Working State.
2. Remaining Agent-step, tool-call, validation-command, and elapsed-time budget.
3. Current acceptance criteria.
4. Pinned memory.
5. Task-relevant Repository Map.
6. Current staged and unstaged Git diff.
7. The three newest detailed observations.
8. Deterministic summaries of older evidence.
9. Initial ranked repository context.

The complete packet is capped at 20,000 characters, and every section also has its own limit. High-priority sections are assembled first, so a large diff or repository map cannot displace Working State or remaining-budget information. The packet is rebuilt every round: after a tool observation, the next prompt sees updated state, one fewer available step/tool call, refreshed recent evidence, and older steps moved into the compact evidence section.

Every section is passed through a defense-in-depth redaction filter before the LLM request. RepoPilot removes common credential assignments, Bearer tokens, OpenAI-style tokens, GitHub tokens, and complete private-key blocks. Do not rely on redaction as a reason to commit secrets; keep credentials in the process environment.

Use JSON output when you want to inspect structured fields:

```bash
python repopilot.py run --repo . --task "inspect LLM trace fields" --use-llm --json
```

Important LLM-related fields:

- `plan_metadata`: whether planning came from rules or an LLM.
- `patch_proposal_metadata`: whether proposal generation came from rules or an LLM.
- `patch_review`: LLM review of the proposed diff when available.
- `agent_steps`: non-writing iterative decisions with rationale, expected evidence, state update, observation, selected paths, and finish reason.
- `agent_run_id`: stable identifier for the typed runtime execution.
- `agent_events`: ordered runtime lifecycle, action, observation, approval, replay, and recovery events.
- `agent_state`: latest versioned working-state snapshot with objective, focus, findings, open questions, implementation plan, acceptance state, virtual-edit metadata, expected evidence, lifecycle fields, selected paths, and bounded observations with action ids.
- `agent_stop_reason`: explicit controller result such as `finished`, `step_limit`, `input_required`, `policy_denied`, or `failed`.
- `agent_pending_question`: user question attached to an `input_required` stop, when core question support is enabled.
- `agent_completion_ready`: whether plan, required acceptance criteria, and virtual proposal review are complete.
- `agent_completion_blockers`: stable `plan:<id>`, `acceptance:<id>`, and `proposal:<path>:<reason>` identifiers preventing finish.
- `agent_proposed_edits`: bounded path, hash, revision, hunk-count, conflict, and inspection metadata.
- `agent_proposed_diff`: bounded cumulative virtual diff; it is a proposal and does not mean the repository changed.
- `llm_traces`: prompt previews, output previews, parse status, fallback state, and latency.
- `context_summary`: file inclusion/edit diagnostics for planner and proposal calls, or per-section usage plus full/truncated/omitted/redacted state for iterative Agent calls.
- `repository_map`: indexed file/symbol/relation counts and the task-ranked files, symbols, and dependencies.

## Step 4: Start The Web UI

Start the local server:

```bash
python repopilot.py serve
```

The current directory is checked for unfinished task runs during startup. When serving from another directory, select the repository explicitly:

```bash
python repopilot.py serve --repo "C:/path/to/repository"
```

Open:

```text
http://127.0.0.1:8765
```

The web UI is local. It gives you the full workflow in tabs:

- Task Run: sandbox lifecycle, current phase, pause/resume/cancel controls, event history, and local branch delivery.
- Summary: plan, Repository Map, proposal, validation, safety, repair feedback, and timeline.
- LLM I/O: prompt preview, output preview, trace status, and context budget.
- GitHub: open issues, pull requests, reviews, files, comments, and checks.
- Diff: current working tree diff or staged diff.
- Delivery: PR readiness, suggested commit message, pull request draft, and explicit PR creation.
- History: saved local runs, memory reuse, pinning, deletion, and clearing.

Before running an LLM workflow from the web UI, fill in the model, API endpoint URL, API key, and timeout fields or start the server from a shell that already has the matching environment variables. Use the complete Chat Completions endpoint, for example `https://api.openai.com/v1/chat/completions`; RepoPilot does not append `/chat/completions` to the value you enter. Click `Test LLM Connection` first. A successful test means the provider accepted the OpenAI-compatible chat completions request; a failed test shows a redacted diagnostic message without storing your API key.

Enable `Iterative agent` when you want RepoPilot to make several smaller non-writing LLM calls before the main plan/proposal calls. The Summary tab shows each typed decision in `Agent Steps`, the latest `Agent Working State`, virtual proposal revisions and cumulative diff, and typed `Runtime Events`; the LLM I/O Trace tab shows each `agent_step_N` prompt and raw output.

Open an `agent_step_N` entry in LLM I/O and inspect `Context Budget`. A summary such as `repository_map 2500/2500 chars (truncated)` means that section reached its own limit; `omitted` means the total packet was already full when the lower-priority section was reached; `redacted` means at least one sensitive value was replaced before the request.

Agent Working State is a compact controller snapshot rather than a transcript. Version 4 adds up to 12 virtual-edit metadata records to version 3's bounded plan, acceptance, focus, findings, questions, expected evidence, lifecycle, and selected-path fields. Each virtual-edit record contains only its path, real and virtual hashes, revision, cumulative hunk count, conflict status, and inspection state. RepoPilot seeds one investigation step plus preliminary acceptance criteria, applies structured updates deterministically, and records initial, per-action, and terminal snapshots. Questions are matched case-insensitively, duplicate findings/questions are suppressed, and only bounded values plus the eight newest observation summaries and action ids are retained. Version 1 through 3 snapshots remain readable with empty later fields. Complete virtual file contents are process-local rather than restored from Working State; complete command output, API keys, and provider endpoints are also excluded. Saved History derives the latest state from persisted `working_state_updated` events and the latest bounded virtual diff observation.

Findings in Working State are model-maintained investigation notes. They help the next decision stay focused, but RepoPilot does not treat them as passing tests, completion evidence, or permission to modify files. The core controller can opt into `ask_user`; a valid question pauses with `input_required`, persists the open question, and exposes it as `agent_pending_question`. The top-level CLI/Web workflow deliberately does not offer this action yet because it cannot durably bind an answer and resume the same controller run.

Runtime events are ordered by sequence number. A normal controller cycle records `decision_recorded`, `action_authorized`, `action_started`, `action_completed`, and `working_state_updated`; stale hashes or hunk conflicts record `action_conflict`. `decision_recorded` stores the validated bounded decision envelope. `action_authorized` proves policy allowed the exact action; denied, approval-required, and acceptance-incomplete finish actions do not receive it. `finish_blocked` is recoverable inside the current loop and reports exactly which plan, acceptance, or proposal-review ids need work. `input_required` records a question pause. `action_replayed` means a completed idempotent result was reused without executing the tool again. `action_recovery_required` means RepoPilot found an interrupted reservation and stopped automatic replay so you can inspect the sandbox first. Runtime stopping observations still end the current loop immediately; patch conflicts and `finish_blocked` are controller observations that allow a later corrective decision within the remaining budget.

The runtime tool registry contains `search_files`, `read_file`, `inspect_repository_map`, `inspect_git_status`, `inspect_diff`, `propose_patch`, `inspect_proposed_diff`, `apply_patch`, `edit_file`, `run_command`, `validate`, `ask_user`, and `finish`. The current iterative LLM Agent receives a non-writing policy that includes the virtual proposal tools, and the top-level workflow further removes `ask_user` until durable answer/resume support is implemented. Real edit and command tools require an explicit allowed path or exact command plus action approval, and RepoPilot never exposes commit or push as runtime tools.

The Repository Map is built locally from scanned files. For Python it uses the standard AST to index classes, functions, methods, signatures, and imports. It also recognizes common JavaScript/TypeScript declarations and relative imports, links source files to tests, and ranks entries against the current task. Planner and proposal prompts receive a bounded map section, while the Summary tab shows the counts and most relevant entries.

For approved runtime writes, prefer `apply_patch` over `edit_file`. First read the file and keep the returned `sha256`; then submit exact `old_text`/`new_text` hunks with `expected_occurrences`. RepoPilot reports `conflict` without writing if the hash is stale or a hunk is missing or ambiguous. It rejects invalid Python and JSON before writing, then reads the result back and verifies its hash. The existing Web proposal flow remains server-stored and per-file approved; the structured tool is the lower-level write contract for the unified runtime.

Use `Repair max attempts` to cap how many failed-validation repair proposal rounds RepoPilot can create for a proposal chain. The default is `2`, and `0` disables repair proposal generation while still showing validation failure analysis.

## Step 5: Choose A Repository Source

RepoPilot supports local paths and GitHub URLs.

For a local repository:

1. Set Repository source to `Local path`.
2. Enter a path such as `.`.
3. Run the workflow.

For a GitHub repository:

1. Set Repository source to `GitHub URL`.
2. Enter a URL such as `https://github.com/owner/repo`.
3. Optionally enter a branch.
4. Click `Sync Repository`.
5. Run the workflow after the repository is cloned into the local cache.

GitHub repositories are cloned under `.repopilot/repos/` by default. You can override that location:

```powershell
$env:REPOPILOT_REPO_CACHE = "E:\RepoPilotCache"
```

RepoPilot protects cached clones with local changes. If the cached working tree is dirty, sync fetches metadata but skips checkout and pull.

## Step 6: Use GitHub Issues Or Pull Requests As Tasks

In the web UI:

1. Select a repository source.
2. Open the GitHub tab.
3. Click Refresh.
4. Use an issue or pull request as the task.
5. Run analysis or generate a proposal.

RepoPilot reads bounded GitHub context, including:

- Issue title, labels, body preview, URL, and recent comments.
- Pull request title, body preview, source and target branches.
- Pull request changed files, file stats, and patch previews.
- Pull request conversation comments and inline review comments.
- Review states and reviewer metadata.
- Check runs, legacy statuses, conclusions, and output summary previews.

For private repositories or higher rate limits, set:

```powershell
$env:GITHUB_TOKEN = "your-github-token"
```

or:

```powershell
$env:GH_TOKEN = "your-github-token"
```

## Step 7: Generate A Proposal

Use `Generate Proposal` when you want RepoPilot to prepare an apply-ready proposal without immediately running the full workflow.

In the web UI:

1. Enter a task.
2. Enable LLM if you want model-backed edits.
3. Add optional validation commands.
4. Click `Generate Proposal`.
5. Inspect the Summary tab and proposed diff.

The proposal is stored server-side with a `proposal_id`. The browser cannot submit arbitrary file edits for apply; it can only ask the local server to apply selected file edits from a known proposal. Proposal sessions are also saved in `.repopilot/memory.sqlite3`, so the web server can restore a generated proposal after restart when the request includes the same repository input.

## Step 8: Review Safety And Diff

Before applying anything, check:

- Proposed files: the paths RepoPilot wants to change.
- Proposed diff: the actual edit preview.
- Safety check: path protection, duplicate edits, no-op edits, large deletion checks, weak task relevance, and other findings.
- Validation plan: recommended commands and manual checks.
- LLM review: model critique of the proposal when LLM mode is enabled.

Do not apply a proposal if the diff touches unexpected files.

## Step 9: Apply Approved Changes

When the proposal looks correct:

1. Review the apply-ready file checkboxes in `Proposed Changes`.
2. Leave only the files you want to approve checked.
3. Click `Apply Proposal`.
4. Confirm the browser prompt.
5. RepoPilot writes only the approved server-stored proposal edits.
6. RepoPilot runs configured or recommended validation commands.
7. Open the Diff tab to inspect the final working tree diff.

RepoPilot does not commit, push, or open pull requests automatically.

If you want to undo the applied proposal before committing:

1. Click `Revert Applied Proposal`.
2. Confirm the browser prompt.
3. RepoPilot restores files from the internal pre-apply rollback snapshot.
4. Open the Diff tab again to confirm the working tree returned to the expected state.

Rollback is intentionally cautious. If a file was changed again after the proposal was applied, RepoPilot refuses to revert it so it does not overwrite your manual work.

Rollback snapshots are persisted with the proposal session. If the web server restarts after apply, `Revert Applied Proposal` can still restore the approved files as long as those files have not changed again after apply.

## Step 10: Repair Failed Validation

If validation fails, RepoPilot builds bounded validation feedback:

- Failed command.
- Exit code.
- Short output excerpt.
- Suspected files.
- Failure signals.
- Repair steps.
- A generated repair task.

In the web UI:

1. Read the Validation Feedback panel.
2. Check the displayed repair budget, such as `next attempt 1/2`.
3. Click `Generate Repair Proposal`.
4. Review the repair diff.
5. Apply only if the repair is correct.
6. Rerun validation.

Repair proposals inherit the original proposal's retry budget. If a repair proposal is applied and validation fails again, RepoPilot can generate the next repair attempt until the configured budget is exhausted. Once exhausted, the failure analysis remains visible, but `Generate Repair Proposal` is disabled and the API rejects further repair generation for that proposal.

This keeps the repair loop explicit, bounded, and human-approved.

## Step 11: Inspect Git State

CLI:

```bash
python repopilot.py git status --repo .
```

JSON:

```bash
python repopilot.py git status --repo . --json
```

RepoPilot shows:

- Current branch.
- Upstream branch.
- Ahead and behind counts.
- Remotes.
- Latest commit.
- Working tree changes.
- Diff stats.

## Step 12: Generate Delivery Text

CLI:

```bash
python repopilot.py git summary --repo . --validation "python -m unittest discover -s tests"
```

Pull request draft:

```bash
python repopilot.py git pr-draft --repo . --validation "python -m unittest discover -s tests"
```

Web UI:

1. Open the Delivery tab.
2. Click `Check PR Readiness` to inspect branch, upstream, dirty working tree, push state, and suggested commands.
3. Click `Generate Delivery Draft`.
4. Review the suggested commit message, change summary, validation notes, PR readiness, and PR body.
5. Commit and push manually if readiness reports blockers.
6. Click `Create Pull Request` only after the branch is clean, pushed, on a non-base branch, and the browser confirmation matches what you intend to publish.

RepoPilot still does not commit or push. Pull request creation is gated by readiness checks, requires explicit confirmation, and uses `GITHUB_TOKEN` or `GH_TOKEN` from the local server environment.

## Step 13: Use Local Memory

RepoPilot saves local workflow history in:

```text
.repopilot/memory.sqlite3
```

Memory helps later runs by reusing compact summaries, validation outcomes, and task metadata. It does not store API keys.
It also stores proposal sessions, rollback metadata, LLM trace history, runtime action reservations, and ordered runtime events for local inspection.

In the web UI:

- Open History to inspect saved runs.
- Open saved run details to inspect persisted LLM trace history.
- Inspect the Runtime Events section to understand tool execution, replay, or recovery decisions.
- Pin important runs so they are prioritized in future planning.
- Delete one run when it is no longer useful.
- Clear history for the current repository.
- Check Disable memory for a clean-context run.

CLI clean-context run:

```bash
python repopilot.py run --repo . --task "analyze without previous run history" --no-memory
```

## Step 14: Run Evaluations

Run the deterministic baseline without configuring an API:

```bash
python repopilot.py eval
```

This runs the cases under `evals/cases/` against self-contained fixture repositories. Memory is disabled, proposed edits are never applied, and the command exits with status `1` when a case fails.

The summary shows pass rate, score, relevant-file recall, proposal-file recall, runtime, LLM calls, LLM failures, fallback stages, and provider latency. Inspect failed criteria under each case instead of treating the aggregate score as the only signal.

Write the structured report to an ignored local directory:

```bash
python repopilot.py eval --output evals/results/baseline.json
```

After the deterministic baseline passes, evaluate the configured model:

```bash
python repopilot.py eval --use-llm --model gpt-4o-mini --no-llm-fallback
```

Include non-writing iterative analysis and virtual proposal actions:

```bash
python repopilot.py eval --use-llm --iterative-agent --agent-max-steps 6 --no-llm-fallback
```

LLM reports contain aggregate call metadata but exclude API keys, raw prompts, and raw outputs. See `evals/README.md` before adding or changing cases.

## Step 15: Use A Git Worktree Sandbox

Start from a clean repository and create a detached sandbox:

```bash
python repopilot.py sandbox create --repo .
```

The source repository must be clean because the sandbox starts from a committed Git snapshot. RepoPilot refuses creation instead of silently omitting uncommitted source files.

Copy the printed path into `--repo` for CLI work:

```bash
python repopilot.py run --repo "C:/path/from/create" --task "fix parser behavior" --use-llm
```

In the Web UI, use `Create Sandbox` in the repository controls. RepoPilot creates and selects it automatically. The sandbox selector can switch between managed worktrees, and `Refresh Sandboxes` reloads their clean/dirty state. Proposal approval, patch application, validation, diff inspection, and history use the selected sandbox path.

List sandboxes later:

```bash
python repopilot.py sandbox list --repo .
```

Remove a clean sandbox after reviewing its diff:

```bash
python repopilot.py sandbox remove --repo . --path "C:/path/from/create"
```

Dirty sandboxes are preserved. The CLI requires explicit `--force`, while the Web UI shows a second confirmation before discarding changes:

```bash
python repopilot.py sandbox remove --repo . --path "C:/path/from/create" --force
```

Forced removal permanently discards uncommitted sandbox changes. It does not modify the primary worktree. Set `REPOPILOT_WORKTREE_ROOT` when the operating system's temporary directory is not suitable.

## Step 16: Run A Complete Sandboxed Task

Use this flow when you want RepoPilot to manage the complete Agent lifecycle instead of running analysis and proposal generation as separate requests:

1. Start from a clean source repository with the desired base commit checked out.
2. Enter the task and optional validation command.
3. Configure and test the LLM connection when model-backed edits are needed.
4. Click `Start Sandboxed Task`.
5. Open the Task Run tab and follow `Sandbox`, `Explore`, `Approval`, `Apply`, `Validate`, and `Complete`.
6. At `awaiting_approval`, inspect the acceptance criteria, execution budget, Summary, LLM I/O, and proposed Diff tabs.
7. Select the approved files and click `Apply Proposal`.
8. Leave `Auto-generate repairs` enabled to let RepoPilot diagnose failed validation and prepare the next bounded proposal automatically. Disable it when you want to use the manual repair button.
9. When the run reaches `completed`, inspect the final working Diff.
10. Optionally enter a feature branch name and click `Create Branch`.

Each task receives its own detached managed worktree. RepoPilot automatically selects that path in the repository controls, so proposal application, validation, rollback, Git inspection, and diff display all target the sandbox rather than the source worktree.

Task runs are saved in the source repository's `.repopilot/memory.sqlite3`. RepoPilot adds `.repopilot/` to the clone's local Git `info/exclude`; it does not modify the tracked `.gitignore`. API keys are request-scoped and are not serialized into the task-run record.

The advanced task settings include four execution limits: Agent steps, tool calls, validation commands, and elapsed seconds. Agent exploration consumes step and tool-call capacity. Applying an approved file and running a validation command also consume tool-call capacity. RepoPilot checks the remaining command and tool budget before writing, records elapsed usage, and preserves the proposal without applying it when the configured capacity is insufficient.

Acceptance criteria are generated from the task, proposal file scope, and validation commands. After apply, the Completion Evidence panel reports whether files changed, whether all changed paths were approved, and whether each required command passed. A task run is `completed` only when all required criteria pass and its execution budget is not exceeded. A missing automated validation command is shown as a manual-review recommendation rather than silently presented as automated proof.

Existing UTF-8 proposal files are converted from full replacement content into exact-text structured hunks. Each patch carries the file's proposal-time SHA-256. If the file changes before approval, RepoPilot rejects the patch instead of overwriting the newer content. If a later file conflicts during a multi-file apply, earlier writes from that apply attempt are restored from the captured snapshots.

When automatic repair is enabled, the apply request passes the current LLM settings directly to a background worker. The API key remains request-scoped and is not stored in SQLite. The task moves from `validating` to `diagnosing`, then `replanning`. A successful generation returns the task to `awaiting_approval`; inspect the new diff before approving it. RepoPilot never automatically applies a generated repair.

The Repair Loop panels show each attempt's trigger failure fingerprint, proposal fingerprint, resulting failure fingerprint, status, and affected paths. Fingerprints are hashes of normalized evidence, not complete test logs. The loop stops with an explicit reason when validation repeats unchanged, a proposal repeats, a proposal is a no-op, no safe proposal is generated, or retry/execution capacity is exhausted. Stopped LLM analyses remain available in History, including their traces.

`Pause` and `Cancel` are checkpoint operations. They take effect after the current safe operation returns, so they do not terminate an in-flight LLM HTTP request or interrupt a file write halfway through. Paused, cancelled, failed, and server-interrupted runs preserve their sandboxes.

At Web server startup, RepoPilot scans the repository selected by `serve --repo` for task records left in an active state. Those records become `interrupted` with the previous state, UTC detection timestamp, and `server_restart` reason saved in SQLite. The Task Run tab shows these details in a dedicated notice. This detection step never resumes a worker, calls an LLM, runs validation, or changes sandbox files. Loading a different repository's Task Run history performs the same idempotent check. Inspect the preserved sandbox before using the existing `Resume` or `Cancel` action.

Resume is always manual and checkpoint-aware. The Task Run tab shows one of these checkpoints before enabling the action:

- `source_restart`: create a new sandbox from a clean source repository.
- `sandbox_analysis`: restart analysis in an existing clean sandbox.
- `sandbox_inspection`: require inspection and a clean sandbox after an interrupted write, validation, diagnosis, or re-planning phase.
- `approval`: return to the saved proposal approval state without starting a worker.
- `repair_approval`: return to the saved repair approval state without starting a worker.

`Check Readiness` runs a read-only recovery preflight and displays each check as passed, failed, warning, or not required. It compares the Resume Plan and latest execution checkpoint, verifies the required source or sandbox, inspects Git cleanliness and the saved sandbox HEAD, and confirms that approval checkpoints still have a matching persisted proposal session. Legacy tasks without execution-checkpoint history receive a warning instead of being permanently blocked. The check does not call an LLM, run validation, write files, or change task status.

The `Saved Execution Profile` panel shows the non-sensitive configuration captured when the task started: LLM mode and model, endpoint fingerprint, JSON and fallback modes, memory, iterative exploration, LLM timeout, repair automation and retry limit, and execution limits. RepoPilot stores only an SHA-256 fingerprint of the normalized endpoint, not the complete URL. It never stores the API key. Tasks created before this feature show no saved profile and remain compatible.

When you click `Check Readiness`, RepoPilot builds a temporary credential-free profile from the current Web form and compares it with the saved profile. The Recovery Readiness panel labels the result `matched`, `changed`, or `legacy`. For a changed profile it lists each field with its saved and current value; endpoint values are abbreviated hashes rather than URLs. This comparison is request-scoped and is not saved to task history. A difference is currently a warning only: it does not change `ready` and does not block Resume. Resume repeats the same comparison server-side so the displayed report reflects the settings sent with that request.

Clicking Resume refreshes that report before opening a confirmation that names the checkpoint. The request includes that exact checkpoint and explicit confirmation, preventing a stale browser from approving a different recovery path. The server reruns readiness after confirmation and then validates request-scoped LLM configuration before changing the task state. If any step fails, the task remains `interrupted`, `paused`, `failed`, or `cancelled` with the sandbox preserved. A successful resume records its UTC time, checkpoint, and cumulative resume count. API keys are not part of the readiness report or persisted recovery state.

The Task Run tab also contains `Latest Checkpoint` and `Checkpoint History`. These are execution snapshots rather than Resume Plan choices. Each snapshot shows the completed phase, resulting status, intended next action, timestamp, sandbox or proposal reference, execution-budget usage and remaining capacity, and repair attempt. RepoPilot writes them only after a stable boundary such as queueing, sandbox creation, analysis completion, proposal readiness, validation completion, repair readiness, pause, cancellation, interruption, or failure. The UI shows the newest 20 entries in reverse chronological order, while SQLite retains the latest 100. These records are diagnostic only: they do not trigger automatic resume or replay.

Branch creation is intentionally separate from task execution. It is available only after successful completion, requires explicit confirmation, validates the branch name, and verifies that the destination is a registered RepoPilot worktree. RepoPilot leaves the diff uncommitted and unpushed for manual review.

## Recommended End-To-End Test

Use this sequence when you want to verify the project manually:

1. Run the unit tests:

   ```bash
   python -m unittest discover -s tests
   ```

2. Run the deterministic evaluation baseline:

   ```bash
   python repopilot.py eval
   ```

3. Run a deterministic CLI workflow:

   ```bash
   python repopilot.py run --repo . --task "inspect documentation workflow" --validate "python -m unittest discover -s tests"
   ```

4. Run an LLM CLI workflow with a small documentation task:

   ```bash
   python repopilot.py run --repo . --task "suggest a README wording improvement" --use-llm --model gpt-4o-mini --json
   ```

5. Start the web UI:

   ```bash
   python repopilot.py serve
   ```

6. In the browser, generate a proposal for a small, low-risk documentation change.

7. Review proposed diff and safety output.

8. Apply only if the diff is expected.

9. Test `Revert Applied Proposal` once on a low-risk documentation change.

10. Run validation and inspect the final Git diff.

11. Generate delivery text.

12. Commit and push manually when you are satisfied.

## Troubleshooting

### The LLM does not run

Check that `OPENAI_API_KEY` is set in the same terminal session:

```powershell
$env:OPENAI_API_KEY
```

If the key is missing, set it again and restart the web server.

### GitHub status is unavailable

Check that the repository has a GitHub remote:

```bash
git remote -v
```

For private repositories or rate limits, set `GITHUB_TOKEN` or `GH_TOKEN`.

### GitHub URL sync fails

Check that `git clone` works for the repository from your terminal. Private repositories require Git credentials.

### Validation was rejected

RepoPilot only runs allowlisted validation commands. Use one of the supported prefixes or add a new allowlist entry in `src/repopilot_agent/validator.py`.

### The web server port is busy

Start on another port:

```bash
python repopilot.py serve --port 8766
```

## Documentation Style References

This tutorial follows a common open-source documentation pattern:

- Start with prerequisites and the fastest working command.
- Keep secrets and safety guidance close to LLM setup.
- Show one complete path before explaining every option.
- Add troubleshooting after the main workflow.

Examples of this split between a concise repository entry point and deeper documentation can be found in [Aider](https://github.com/Aider-AI/aider), [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent), [LangGraph](https://github.com/langchain-ai/langgraph), and [OpenHands](https://github.com/OpenHands/OpenHands).
