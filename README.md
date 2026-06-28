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
- Generate a deterministic engineering plan from the task and retrieved context.
- Run validation commands through an allowlist.
- Print a human-readable report or JSON report.
- Provide unit tests for scanner, search, and workflow behavior.

This MVP intentionally uses deterministic local logic first. LLM providers, patch application, persistent storage, and web UI features will be added after the core workflow is stable.

## Core Features

- **Repository Understanding**: Index source code, README files, configuration files, and project documentation.
- **Task Planning**: Convert a user request into a clear engineering plan with actionable steps.
- **Code Search and Context Retrieval**: Combine semantic search with precise keyword search to locate relevant files and functions.
- **Patch Generation**: Propose focused code changes as readable diffs before applying them.
- **Human-in-the-Loop Approval**: Require user confirmation before applying file edits, running risky commands, or creating pull requests.
- **Test and Validation Runner**: Run project-specific tests, linters, or type checks and summarize the results.
- **Execution Trace**: Show each agent step, tool call, result, retry, and decision in a transparent timeline.
- **PR Summary Generation**: Generate concise pull request descriptions, risk notes, and test evidence.
- **Safety Controls**: Use command allowlists, sensitive file protection, and clear approval boundaries.

## Planned Architecture

- **Frontend**: React or Next.js dashboard for task input, execution timeline, diffs, approvals, and results.
- **Backend**: FastAPI service for project management, agent orchestration, file access, and tool execution.
- **Agent Orchestration**: LangGraph-style state machine with planner, coder, tester, and reviewer nodes.
- **Retrieval Layer**: LlamaIndex or a lightweight custom index for repository search and documentation retrieval.
- **Storage**: SQLite for task history, execution traces, repository metadata, and validation results.
- **Sandboxing**: Docker-based execution environment for safer command execution.
- **Model Layer**: Pluggable LLM provider interface for OpenAI-compatible APIs and local models.

## MVP Scope

The first version focuses on a complete local workflow:

1. Select or register a local repository.
2. Submit a bug report or feature request.
3. Generate an implementation plan.
4. Search and display relevant files.
5. Run allowlisted validation commands.
6. Generate a final engineering summary.

Patch generation and human-approved patch application are planned next.

## Usage

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

- LLM-backed planning, review, and patch generation
- Human-approved patch application
- FastAPI service for workflow execution
- React or Next.js dashboard for execution traces, diffs, approvals, and results
- GitHub issue import and pull request creation
- Multi-agent collaboration between planner, implementer, tester, and reviewer
- Persistent memory per repository
- Automatic benchmark suite using real open-source issues
- Browser-based investigation for documentation and dependency research
- Team workspace support with task history and approval logs

## Current Status

Local MVP implementation is in progress. The CLI workflow, repository scanner, search layer, deterministic planner, validation runner, root launcher, and unit tests are implemented.
