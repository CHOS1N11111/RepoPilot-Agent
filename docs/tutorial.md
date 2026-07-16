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

To use Codex-like multi-step exploration before planning and proposal generation, enable iterative agent mode:

```bash
python repopilot.py run --repo . --task "fix parser behavior" --use-llm --iterative-agent --agent-max-steps 6
```

This mode lets the LLM choose read-only actions such as `search_files`, `read_file`, and `inspect_git_status` across several smaller calls. It does not write files directly; file changes still require a generated proposal and human approval.

Use JSON output when you want to inspect structured fields:

```bash
python repopilot.py run --repo . --task "inspect LLM trace fields" --use-llm --json
```

Important LLM-related fields:

- `plan_metadata`: whether planning came from rules or an LLM.
- `patch_proposal_metadata`: whether proposal generation came from rules or an LLM.
- `patch_review`: LLM review of the proposed diff when available.
- `agent_steps`: read-only iterative agent actions when iterative mode is enabled.
- `llm_traces`: prompt previews, output previews, parse status, fallback state, and latency.
- `context_summary`: which files were included, truncated, omitted, or eligible for direct edits.

## Step 4: Start The Web UI

Start the local server:

```bash
python repopilot.py serve
```

Open:

```text
http://127.0.0.1:8765
```

The web UI is local. It gives you the full workflow in tabs:

- Summary: plan, proposal, validation, safety, repair feedback, and timeline.
- LLM I/O: prompt preview, output preview, trace status, and context budget.
- GitHub: open issues, pull requests, reviews, files, comments, and checks.
- Diff: current working tree diff or staged diff.
- Delivery: PR readiness, suggested commit message, pull request draft, and explicit PR creation.
- History: saved local runs, memory reuse, pinning, deletion, and clearing.

Before running an LLM workflow from the web UI, fill in the model, API endpoint URL, API key, and timeout fields or start the server from a shell that already has the matching environment variables. Use the complete Chat Completions endpoint, for example `https://api.openai.com/v1/chat/completions`; RepoPilot does not append `/chat/completions` to the value you enter. Click `Test LLM Connection` first. A successful test means the provider accepted the OpenAI-compatible chat completions request; a failed test shows a redacted diagnostic message without storing your API key.

Enable `Iterative agent` when you want RepoPilot to make several smaller read-only LLM calls before the main plan/proposal calls. The Summary tab shows `Agent Steps`, and the LLM I/O Trace tab shows each `agent_step_N` prompt and raw output.

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
It also stores proposal sessions, rollback metadata, and LLM trace history for local inspection.

In the web UI:

- Open History to inspect saved runs.
- Open saved run details to inspect persisted LLM trace history.
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

Include read-only iterative exploration:

```bash
python repopilot.py eval --use-llm --iterative-agent --agent-max-steps 6 --no-llm-fallback
```

LLM reports contain aggregate call metadata but exclude API keys, raw prompts, and raw outputs. See `evals/README.md` before adding or changing cases.

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

Examples of this style can be found in the OpenAI Agents Python quickstart, the Hugging Face smolagents README, and the Microsoft AutoGen README.
