# Contributing to RepoPilot Agent

Thank you for helping improve RepoPilot Agent. Contributions should preserve its local-first, human-in-the-loop workflow and keep repository writes explicit and reviewable.

## Before You Start

- Search existing issues and pull requests before starting overlapping work.
- Open an issue before a large behavioral or architectural change.
- Keep each pull request focused on one problem.
- Never include API keys, access tokens, private repository content, or local RepoPilot history.

## Development Setup

RepoPilot requires Python 3.10 or newer and currently has no required third-party runtime dependencies.

```bash
git clone https://github.com/CHOS1N11111/RepoPilot-Agent.git
cd RepoPilot-Agent
python -m venv .venv
```

Activate the virtual environment on Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

Activate it on macOS or Linux:

```bash
source .venv/bin/activate
```

Install the project in editable mode:

```bash
python -m pip install -e .
```

Confirm that the CLI is available:

```bash
repopilot --help
```

## Environment Variables

Use [.env.example](.env.example) as a reference for supported environment variables. RepoPilot does not load `.env` files automatically, so export the values in the terminal that starts the CLI or web server.

Never commit a populated `.env` file. Do not include secrets in tests, screenshots, issues, pull requests, logs, or LLM traces.

## Making Changes

- Follow the existing module boundaries and standard-library-first approach.
- Use four spaces for Python and two spaces for web assets and YAML.
- Add type hints where they make public behavior clearer.
- Keep file writes behind proposal approval and existing safety checks.
- Add or update tests for behavior changes and regressions.
- Update `README.md` or `docs/tutorial.md` when user-facing behavior changes.
- Keep `log.md` and `.repopilot/` local; they must not be committed.

## Validation

Run the complete test suite:

```bash
python -m unittest discover -s tests
```

Compile-check the launcher, package, and tests:

```bash
python -m compileall -q repopilot.py src tests
```

Run the deterministic Agent evaluation baseline:

```bash
python repopilot.py eval --suite evals/cases
```

For a narrow change, run the directly related test module first, then run the complete suite before opening a pull request.

GitHub Actions runs the same compile, unit-test, and deterministic evaluation checks on Python 3.10, 3.11, and 3.12.

## Pull Requests

A pull request should include:

- A concise explanation of the problem and the chosen solution.
- The affected user workflow or API behavior.
- Tests added or updated.
- Commands run and their results.
- Any safety, compatibility, migration, or follow-up considerations.

Use an imperative, focused commit subject, such as `Add validation retry limits`. Avoid mixing formatting-only changes with behavior changes unless they are required by the same task.

By contributing, you agree that your contribution will be licensed under the repository's MIT License.
