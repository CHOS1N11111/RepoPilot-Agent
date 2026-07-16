# RepoPilot Evaluations

This directory contains reproducible workflow evaluations for RepoPilot Agent. The initial suite uses small, self-contained fixture repositories so retrieval, planning, proposal selection, and validation regressions are easy to diagnose.

## Run The Baseline

From the repository root:

```bash
python repopilot.py eval
```

The default command loads every JSON file under `evals/cases/`, disables memory, runs in deterministic mode, and exits with status `1` if any case fails.

Print the complete structured report:

```bash
python repopilot.py eval --json
```

Write a local report:

```bash
python repopilot.py eval --output evals/results/baseline.json
```

Generated reports under `evals/results/` are ignored by Git.

## Evaluate An LLM

Set the provider configuration in the process environment, then run:

```bash
python repopilot.py eval --use-llm --model gpt-4o-mini --no-llm-fallback
```

Evaluate the read-only iterative agent loop as well:

```bash
python repopilot.py eval --use-llm --iterative-agent --agent-max-steps 6 --no-llm-fallback
```

The evaluation runner never applies proposals. API keys, raw prompts, and raw model outputs are excluded from saved evaluation reports.

## Metrics

Each case reports:

- Pass/fail status and a percentage score across explicit expectations.
- Relevant-file and proposal-file recall.
- Ranked relevant files and proposed files.
- Plan step count and validation status.
- End-to-end duration.
- Iterative agent step count.
- Traced workflow LLM-call count, failed calls, fallback stages, and provider latency. A provider-internal retry remains part of one traced call.

The aggregate report includes suite pass rate, average score, average retrieval/proposal recall, total duration, and summed LLM metrics.

RepoPilot does not currently record provider token usage, so token counts and estimated API cost are not included. The current fixtures are regression tests, not a claim of SWE-bench performance.

## Case Schema

Each JSON suite contains a non-empty `cases` list:

```json
{
  "suite": "example",
  "description": "Example suite",
  "cases": [
    {
      "id": "unique-case-id",
      "description": "What this case measures",
      "repo": "../fixtures/example_repo",
      "task": "fix authentication token validation",
      "validation_commands": [
        "python -m unittest discover -s tests"
      ],
      "expect": {
        "relevant_files": ["src/auth.py"],
        "top_relevant_file": "src/auth.py",
        "proposal_files": ["src/auth.py", "tests/test_auth.py"],
        "proposal_ready": true,
        "min_plan_steps": 5,
        "validation_passed": true,
        "max_llm_failures": 0,
        "max_fallbacks": 0,
        "min_agent_steps": 1
      }
    }
  ]
}
```

Paths are resolved relative to the JSON case file. Unknown fields, duplicate IDs, missing repositories, invalid types, and expectations without validation commands are rejected before the suite runs.

## Adding A Case

1. Add a small repository under `evals/fixtures/` or point the case at another stable local fixture.
2. Give the case a task that identifies the behavior under evaluation.
3. Add only expectations that can be verified from `WorkflowReport`.
4. Run the case in deterministic mode and inspect every failed criterion.
5. Run the complete unit test suite before committing.

Keep fixtures deterministic and free of credentials, network requirements, generated histories, and large dependencies.
