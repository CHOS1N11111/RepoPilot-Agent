# RepoPilot Agent

RepoPilot Agent is an AI-powered developer workflow assistant designed for real software engineering tasks. Given a GitHub issue, a bug report, or a feature request, it analyzes a codebase, plans the work, proposes code changes, runs validation commands, and produces a review-ready implementation summary.

The project is designed as a practical LLM/agent system rather than a simple chatbot. It focuses on tool use, repository understanding, human approval, execution traceability, and engineering automation.

## What This Project Will Do

RepoPilot Agent will help developers move from a vague task description to a validated code change. The system will read project files, search relevant code, reason about implementation steps, generate patches, run tests or linters, and explain the final result.

The long-term goal is to build a safe, observable, and extensible coding agent that can work with local repositories and GitHub workflows.

## Current MVP

The current version provides a dependency-light local workflow that can run without installing external packages:

- Scan a local repository while ignoring common build, dependency, cache, and Git directories.
- Read supported text files such as Python, Markdown, JavaScript, TypeScript, JSON, TOML, YAML, HTML, CSS, Go, Rust, Java, and shell files.
- Search repository files using task keywords and return ranked relevant files with match reasons and previews.
- Generate an engineering plan from the task and retrieved context, with optional LLM-backed planning.
- Propose file-level changes with rationale, suggested actions, confidence, risks, and validation suggestions, with optional LLM-backed proposal generation.
- Run validation commands through an allowlist.
- Inspect local Git workflow state, including branch, upstream, remotes, latest commit, working tree changes, and diff stats.
- Generate suggested commit messages and pull request drafts from local Git changes.
- Inspect GitHub repository collaboration state, including open issues, open pull requests, recent PR reviews, and CI/check status for PR heads.
- Use a local web UI for LLM settings, task input, workflow output, GitHub status, and diff review.
- Generate patch proposals from the web UI without running validation commands.
- Preview LLM-generated file edits as unified diffs before applying them.
- Apply approved file edits from the web UI with repository path protection and blocked sensitive files.
- Print a human-readable report or JSON report.
- Provide unit tests for scanner, search, LLM planning fallback, LLM patch proposal fallback, Git workflow, GitHub workflow, web server helpers, and workflow behavior.

This MVP uses deterministic local logic by default and can use an OpenAI-compatible LLM for planning and patch proposal generation when configured. If the LLM is unavailable or returns invalid JSON, RepoPilot falls back to deterministic behavior unless fallback is disabled.

## Core Features

- **Repository Understanding**: Index source code, README files, configuration files, and project documentation.
- **Task Planning**: Convert a user request into a clear engineering plan with actionable steps, either through deterministic rules or an optional LLM planner.
- **Code Search and Context Retrieval**: Combine semantic search with precise keyword search to locate relevant files and functions.
- **Patch Proposal**: Propose focused file-level changes, rationale, risk notes, validation suggestions, and LLM-generated editable file content before applying edits.
- **Human-in-the-Loop Approval**: Require user confirmation before applying file edits, running risky commands, or creating pull requests.
- **Test and Validation Runner**: Run project-specific tests, linters, or type checks and summarize the results.
- **Git Workflow Awareness**: Inspect branch state, remotes, latest commit, changed files, diff stats, and ahead/behind information.
- **GitHub Workflow Awareness**: Read open issues, open pull requests, PR reviews, and CI/check status from the GitHub REST API.
- **Local Web UI**: Run a dependency-light browser interface for LLM configuration, task execution, GitHub status, proposed diff review, approved patch application, and working-tree diff inspection.
- **Execution Trace**: Show each agent step, tool call, result, retry, and decision in a transparent timeline.
- **PR Summary Generation**: Generate concise pull request descriptions, risk notes, and test evidence.
- **Safety Controls**: Use command allowlists, LLM fallback behavior, sensitive file protection, and clear approval boundaries.

## Planned Architecture

- **Frontend**: React or Next.js dashboard for task input, execution timeline, diffs, approvals, and results.
- **Backend**: FastAPI service for project management, agent orchestration, file access, and tool execution.
- **Agent Orchestration**: LangGraph-style state machine with planner, coder, tester, and reviewer nodes.
- **Retrieval Layer**: LlamaIndex or a lightweight custom index for repository search and documentation retrieval.
- **Storage**: SQLite for task history, execution traces, repository metadata, and validation results.
- **Sandboxing**: Docker-based execution environment for safer command execution.
- **Model Layer**: Pluggable LLM provider interface for OpenAI-compatible APIs, with deterministic fallback.

## MVP Scope

The first version focuses on a complete local workflow:

1. Select or register a local repository.
2. Submit a bug report or feature request.
3. Generate an implementation plan with rules or an optional LLM planner.
4. Search and display relevant files.
5. Propose file-level changes for user review with rules or an optional LLM proposal module.
6. Run allowlisted validation commands.
7. Inspect Git state and generate commit or PR draft text.
8. Inspect GitHub issue, pull request, review, and CI state when a GitHub remote is configured.
9. Review workflow results, GitHub status, patch proposals, and diffs in a local browser UI.
10. Apply LLM-generated file edits after explicit user approval.
11. Generate a final engineering summary.

## Usage

Run the local workflow from the project root:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior"
```

Run with validation:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --validate "python -m unittest discover -s tests"
```

The report includes ranked relevant files, an implementation plan, proposed file-level changes, risk notes, validation suggestions, validation results, and a final summary.

Start the local web UI:

```bash
python repopilot.py serve
```

Then open:

```text
http://127.0.0.1:8765
```

The web UI includes LLM model selection, API base URL and API key inputs, task input, workflow output, standalone patch proposal generation, proposed diff preview, approved patch application, GitHub issue/PR/review/check display, and working tree or staged diff display. API keys entered in the UI are sent only to the local server for that workflow request and are not written to disk.

Use the LLM planner and patch proposal generator:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm
```

Use a specific model:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm --model gpt-4o-mini
```

Disable fallback when debugging LLM output:

```bash
python repopilot.py run --repo . --task "fix search relevance for login behavior" --use-llm --no-llm-fallback
```

The LLM planner and patch proposal generator read these environment variables:

- `OPENAI_API_KEY`: API key for the OpenAI-compatible provider.
- `OPENAI_BASE_URL`: Optional API base URL. Defaults to `https://api.openai.com/v1`.
- `REPOPILOT_MODEL`: Optional default model name.

Print JSON output:

```bash
python repopilot.py run --repo . --task "inspect validation workflow" --json
```

Inspect local Git state:

```bash
python repopilot.py git status --repo .
```

Generate a commit and workflow summary:

```bash
python repopilot.py git summary --repo . --validation "python -m unittest discover -s tests"
```

Generate a pull request draft:

```bash
python repopilot.py git pr-draft --repo . --validation "python -m unittest discover -s tests"
```

Inspect GitHub issue, pull request, review, and CI state:

```bash
python repopilot.py github status --repo .
```

Print GitHub state as JSON:

```bash
python repopilot.py github status --repo . --limit 10 --json
```

The GitHub command resolves the repository from the local `origin` remote. It can read public repositories without a token, but `GITHUB_TOKEN` or `GH_TOKEN` is recommended for private repositories and higher rate limits.

Run tests:

```bash
python -m unittest discover -s tests
```

Compile-check Python files:

```bash
python -m py_compile repopilot.py src/repopilot_agent/*.py tests/test_workflow.py
```

## Engineering Value

This project combines modern AI engineering with real software development workflows:

- LLM tool use and agent orchestration
- Retrieval-augmented generation for codebases
- Workflow automation with human approval
- Backend API design
- Frontend product design for developer tools
- Git and GitHub workflow integration
- Test execution and result interpretation
- Security-aware agent design

## Future Extensions

- LLM-backed review generation
- GitHub issue import into RepoPilot tasks
- GitHub pull request creation after user approval
- FastAPI service for workflow execution
- Rich React or Next.js dashboard for execution traces, diffs, approvals, and results
- Multi-agent collaboration between planner, implementer, tester, and reviewer
- Persistent memory per repository
- Automatic benchmark suite using real open-source issues
- Browser-based investigation for documentation and dependency research
- Team workspace support with task history and approval logs

## Current Status

Local MVP implementation is in progress. The CLI workflow, repository scanner, search layer, deterministic planner, optional LLM planner, deterministic and optional LLM patch proposal modules, validation runner, Git workflow awareness commands, GitHub workflow awareness command, local web UI, root launcher, and unit tests are implemented.
