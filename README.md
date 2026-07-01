# RepoPilot Agent

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-OpenAI--compatible-111827)
![Workflow](https://img.shields.io/badge/Workflow-Human--in--the--loop-176B87)
![Status](https://img.shields.io/badge/Status-Local%20MVP-18794E)

RepoPilot Agent is a local coding workflow agent for turning GitHub issues, bug reports, or feature requests into reviewed, validated code-change proposals. It scans a repository, retrieves relevant files, plans the work, asks an optional OpenAI-compatible LLM for patch proposals, previews diffs, waits for human approval, applies approved edits, reruns validation, and summarizes the result.

The project is designed around practical agent engineering: tool use, repository understanding, Git/GitHub awareness, structured LLM outputs, traceability, and human-in-the-loop safety.

## Agent Workflow

```text
Task or GitHub issue
-> repository scan
-> relevant file search
-> related memory lookup
-> deterministic or LLM plan
-> patch proposal
-> LLM self-review
-> proposed diff preview
-> human approval
-> protected file application
-> validation rerun
-> Git diff and PR draft support
```

```mermaid
flowchart LR
    A["GitHub issue or task"] --> B["Repository scan"]
    B --> C["Relevant file search"]
    C --> M["Related memory"]
    M --> D["Plan"]
    D --> E["Patch proposal"]
    E --> F["LLM self-review"]
    F --> G["Proposed diff"]
    G --> H{"Human approval"}
    H -->|Approve| I["Protected apply"]
    H -->|Reject| E
    I --> J["Validation rerun"]
    J --> K["Git diff and PR draft"]
```

## Highlights

- 🧭 Dependency-light Python implementation using the standard library for the current MVP.
- 🖥️ Local web UI for task input, LLM settings, proposal review, timelines, GitHub state, and diffs.
- 🧠 Optional OpenAI-compatible LLM integration with deterministic fallback.
- ✅ Strict LLM JSON schema parsing for plans, patch proposals, and patch reviews.
- 🔍 LLM call traces with prompt previews, raw outputs, parse status, fallback state, and latency.
- 🧠 Local memory reuse for related previous runs, validation outcomes, and task summaries.
- 🧹 Memory controls for disabling lookup, deleting saved runs, and clearing local history.
- 🛡️ LLM self-review for proposed diffs before human approval.
- 🔐 Server-side proposal sessions so the browser applies proposals by `proposal_id`, not raw edits.
- 🧪 Validation command allowlist for safer test and lint execution.
- 🌿 Git workflow awareness for branch state, remotes, changes, diff stats, commit messages, and PR drafts.
- 🔗 GitHub awareness for open issues, pull requests, reviews, and CI/check status.
- 📦 Delivery draft panel for suggested commit messages, validation notes, and PR-ready text.

## Capability Map

| Area                   | What RepoPilot Does                                                                                   |
| ---------------------- | ----------------------------------------------------------------------------------------------------- |
| 📁 Repository scanning | Reads supported text files and ignores Git, dependency, build, cache, and local note paths.           |
| 🔎 Retrieval           | Scores files with task terms, path intent, symbols, multi-snippets, and source/test pairing.          |
| 🧭 Planning            | Builds deterministic plans or LLM-generated engineering plans.                                        |
| 🧩 Patch proposal      | Produces file-level change intent, risk notes, validation suggestions, and optional LLM file edits.   |
| 🧠 LLM governance      | Centralizes prompts, validates schemas, records traces, and runs patch self-review.                   |
| 🧠 Memory              | Retrieves related local run history and feeds concise lessons into planning.                          |
| 🖐️ Web approval      | Stores proposals server-side, previews proposed diffs, and applies approved proposals by ID.          |
| 🧪 Validation          | Recommends narrow validation commands, runs allowlisted commands, and reports command results.        |
| 🌿 Git                 | Inspects branch/upstream/ahead/behind, changed files, latest commit, diff stats, and delivery drafts. |
| 🔗 GitHub              | Reads issues, PRs, reviews, and CI/check status from the repository remote.                           |

## Architecture

```text
repopilot.py
  CLI entry point

src/repopilot_agent/
  scanner.py            repository file scanning
  search.py             lightweight relevance search
  planner.py            deterministic and LLM planning
  patch_proposer.py     patch proposal and LLM patch review
  patch_apply.py        protected file edit application
  safety.py             structured pre-apply safety checks
  workflow.py           end-to-end local workflow
  validator.py          allowlisted validation runner
  validation_planner.py recommended validation command planner
  memory.py             SQLite history and related-run retrieval
  git_tools.py          local Git inspection
  git_summary.py        commit message and PR draft generation
  github_tools.py       GitHub REST API inspection
  repo_source.py        local path and GitHub URL resolution
  web_server.py         local stdlib HTTP server
  web_sessions.py       in-memory proposal sessions and timeline events
  context_builder.py    bounded LLM context packet construction
  llm/
    base.py             provider protocol and message model
    openai_compatible.py OpenAI-compatible client
    prompts.py          prompt templates
    schema.py           strict JSON parsers
    tracing.py          LLM call tracing
```

## Quick Start

Run the local workflow from the project root:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior"
```

Run with validation:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --validate "python -m unittest discover -s tests"
```

Print JSON output:

```bash
python repopilot.py run --repo . --task "inspect validation workflow" --json
```

## Web UI

Start the local web UI:

```bash
python repopilot.py serve
```

Open:

```text
http://127.0.0.1:8765
```

The web UI supports:

- Repository source selection for local paths, GitHub URLs, or auto detection.
- Repository sync controls for cached GitHub clones, branch checkout, latest commit display, and local-change protection.
- 🧠 LLM model, API base URL, and API key inputs.
- Memory lookup toggle for clean-context runs.
- 📌 Task input and GitHub issue import.
- 🚦 Workflow execution and standalone proposal generation.
- 🔍 LLM input/output, self-review, and call trace inspection.
- 🕒 Agent timeline showing scan, search, plan, proposal, review, approval, apply, and validation events.
- 🧾 Proposed diff preview before file writes.
- 🖐️ Human-approved patch application by server-side `proposal_id`.
- 📦 Delivery draft generation for commit message and PR body preparation.
- 🔗 GitHub issue/PR/review/check display.
- 🌿 Working tree and staged diff display.
- History controls for opening, reusing, deleting, or clearing saved runs.

API keys entered in the UI are sent only to the local server for that request and are not written to disk.

## Repository Sources

The web UI can analyze either a local repository path or a GitHub repository URL.

- Local path mode uses the directory exactly like the CLI `--repo` option.
- GitHub URL mode accepts inputs such as `https://github.com/owner/repo`, `git@github.com:owner/repo.git`, or `owner/repo`.
- GitHub repositories are cloned into a local cache under `.repopilot/repos/` before analysis.
- Existing cached clones are reused on later runs.
- Use `Sync Repository` to clone missing repositories, fetch remote updates, checkout a branch, and fast-forward pull cached clones.
- Branch input can select a remote branch during first clone or switch a clean cached clone to another branch.
- If local changes are present in the cached working tree, RepoPilot fetches metadata but skips checkout and pull.
- All patch previews, approved file writes, validation commands, Git diffs, and history records operate on the local cached working tree.
- Set `REPOPILOT_REPO_CACHE` to override the clone cache directory.

The first run for a GitHub URL requires `git clone` network access and any credentials required by that repository. RepoPilot still does not commit, push, or open pull requests automatically.

## LLM Configuration

RepoPilot works without an LLM by using deterministic rules. To enable LLM-backed planning, patch proposals, and patch review:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm
```

Use a specific model:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm --model gpt-4o-mini
```

Disable deterministic fallback while debugging model output:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm --no-llm-fallback
```

Disable related memory lookup for a clean-context run:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --no-memory
```

Environment variables:

- `OPENAI_API_KEY`: API key for the OpenAI-compatible provider.
- `OPENAI_BASE_URL`: Optional API base URL. Defaults to `https://api.openai.com/v1`.
- `REPOPILOT_MODEL`: Optional default model name.

## LLM Context Management

RepoPilot builds explicit context packets before each LLM call. Planning receives compact ranked file previews, while patch proposal receives bounded file content for the most relevant files.

- Context packets have per-call character and file-count budgets.
- LLM traces include a context budget summary showing included, truncated, omitted, and edit-eligible files.
- Direct `file_edits` are accepted only for files whose full content fit into the patch context packet.
- If a file is too large and only a snippet was provided, RepoPilot keeps the model's file-level recommendation but blocks apply-ready edits for that file.

## Retrieval Quality

RepoPilot uses explainable local retrieval to decide which files should enter the agent context.

- Task terms are expanded with lightweight aliases and simple variants such as `parser` -> `parse`.
- Path-intent rules boost likely modules for web UI, GitHub/PR/CI, LLM, memory/history, and validation tasks.
- Python and JavaScript-like symbols receive extra weight when they match the task.
- File previews can include multiple matching snippets instead of only the first match.
- Source files and likely test files are paired so implementation and validation context travel together.

## Validation Planning

RepoPilot recommends validation before approved edits are applied.

- Python test files get direct `python -m unittest module.path` commands.
- Python source files prefer paired tests such as `tests/test_auth.py` when present.
- Python changes fall back to `python -m unittest discover -s tests` when no narrow test is found.
- JavaScript and TypeScript changes recommend `npm test` only when `package.json` exists.
- Documentation-only changes produce manual review notes instead of unsafe commands.
- Recommended commands are still run through the validation allowlist.

## Git And GitHub

Inspect local Git state:

```bash
python repopilot.py git status --repo .
```

Generate a commit summary and PR draft:

```bash
python repopilot.py git summary --repo . --validation "python -m unittest discover -s tests"
python repopilot.py git pr-draft --repo . --validation "python -m unittest discover -s tests"
```

The web UI also includes a Delivery tab that generates the same kind of commit message and PR draft from the current working tree. It does not commit, push, or create pull requests.

Inspect GitHub issue, pull request, review, and CI state:

```bash
python repopilot.py github status --repo .
```

Print GitHub state as JSON:

```bash
python repopilot.py github status --repo . --limit 10 --json
```

The GitHub command resolves the repository from the local `origin` remote. Public repositories can be read without a token, but `GITHUB_TOKEN` or `GH_TOKEN` is recommended for private repositories and higher rate limits.

RepoPilot reads bounded GitHub context for agent use:

- Open issue title, labels, body preview, URL, and recent comments.
- Open PR title, body preview, source/target branches, changed files, file stats, and patch previews.
- PR conversation comments and inline review comments.
- PR review states, review body previews, and reviewer metadata.
- Check runs, legacy statuses, conclusion, timing, and output summary previews.

## Local Memory

RepoPilot stores local web workflow history in SQLite under:

```text
.repopilot/memory.sqlite3
```

The memory layer records run metadata, tasks, summaries, proposal metadata, proposed diffs, LLM traces, validation results, and timeline events. API keys are not stored. The web UI exposes this through the History tab, where previous runs can be inspected or reused as new tasks.

RepoPilot also reuses memory during planning. Before a new run creates a plan, it searches recent local history for related tasks and summaries, then passes a compact memory context into the deterministic planner or LLM planner.

Memory context is intentionally bounded and inspectable:

- It includes task text, run summary, mode, applied/open status, match reasons, score, and saved validation command results.
- It does not inject stored API keys, raw LLM outputs, raw prompts, stdout/stderr logs, or proposal diff bodies into the planner prompt.
- If memory is missing or unavailable, RepoPilot falls back to the normal repository scan and retrieval workflow.
- Use `--no-memory` in the CLI or Disable memory in the web UI to skip related-memory lookup for a single run.
- Use the History tab to delete one saved run or clear the current repository history.

## Safety Model

RepoPilot is intentionally approval-first:

- ✅ It previews proposed diffs before writing files.
- 🔐 It applies only server-stored proposal edits by `proposal_id`.
- 🚧 It blocks repository escapes and sensitive paths such as `.git`, `.env`, and `log.md`.
- 🛡️ It runs structured safety checks for duplicate edits, unapproved paths, empty overwrites, large deletions, repeated generated content, and weak task relevance.
- 🧪 It runs validation commands only through an allowlist.
- 🧯 It keeps deterministic fallbacks for invalid or unavailable LLM output.
- 🔍 It exposes LLM traces and self-review output so decisions are inspectable.

## Tests

Run the test suite:

```bash
python -m unittest discover -s tests
```

Compile-check Python files:

```bash
python -m py_compile repopilot.py src/repopilot_agent/*.py tests/test_workflow.py
```

## Roadmap

- 💾 Persist proposal sessions and trace history in SQLite.
- 🧩 Add per-file approval controls before applying proposals.
- 🚀 Add GitHub pull request creation after explicit user approval.
- 🧠 Add pinned memory and per-project memory policies.
- ⚙️ Move the web backend to FastAPI when dependency-light constraints are relaxed.
- 🖥️ Build a richer React or Next.js dashboard for multi-run history and team workflows.
- 🧪 Add benchmark tasks from real open-source issues.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Status

RepoPilot Agent currently includes the CLI workflow, repository scanner, task-aware retrieval, related memory reuse, memory controls, deterministic planner, optional LLM planner, bounded LLM context management, strict LLM schema parsing, prompt templates, LLM call tracing, LLM patch proposal generation, LLM patch self-review, structured pre-apply safety checks, protected patch application, validation planning, validation runner, Git workflow awareness, delivery draft generation, GitHub workflow awareness, SQLite-backed local memory, local web UI, proposal sessions, timeline events, root launcher, and unit tests.
