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

Run the opt-in trajectory and patch-quality gate:

```bash
python repopilot.py eval --suite evals/llm_cases --use-llm --iterative-agent --agent-max-steps 6 --no-llm-fallback
```

`evals/llm_cases/` is deliberately outside the default suite because it makes real provider calls. Its fixture starts with a known failing test; RepoPilot inspects but never applies the generated proposal, so `validation_passed: false` describes the unchanged fixture while patch expectations score the proposed fix.

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
- Ordered Agent actions, stop reason, required Runtime events, recovery events, and repair cycles.
- Evidence coverage: completed plan items and passed acceptance criteria that cite a successful evidence-producing action.
- Tool-call count, including every member of a parallel read batch.
- Unauthorized side effects: write or command starts without a preceding authorization event for the same action id.
- Proposed edit paths, apply readiness, and required fragments in the generated diff.
- Provider token usage when supplied; otherwise a clearly marked estimate from the bounded trace text.

The aggregate report includes suite pass rate, average score, retrieval/proposal recall, evidence coverage, duration, tool/recovery/repair/safety totals, token usage, and summed LLM metrics. Each case also records a trajectory fingerprint without embedding raw Runtime payloads.

Token totals are not dollar-cost estimates. A report labels usage as `provider`, `estimated`, `mixed`, or `none`, because OpenAI-compatible gateways do not all return the same usage fields. The current fixtures are regression tests, not a claim of SWE-bench performance.

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
        "min_agent_steps": 1,
        "required_agent_actions": ["finish"],
        "required_runtime_events": ["decision_recorded", "run_stopped"],
        "expected_stop_reason": "finished",
        "min_evidence_coverage": 1.0,
        "edit_files": ["src/auth.py"],
        "patch_contains": ["expired"],
        "proposal_apply_ready": true,
        "max_tool_calls": 8,
        "max_unauthorized_side_effects": 0,
        "max_repair_cycles": 0,
        "max_llm_latency_ms": 600000,
        "max_total_tokens": 100000,
        "max_duration_ms": 900000
      }
    }
  ]
}
```

`expected_agent_actions` can enforce an exact ordered action list; `required_agent_actions` checks only membership. `min_recovery_events` and `expected_repair_stop_reason` are available for recovery and repair-specific suites. Paths are resolved relative to the JSON case file. Unknown fields, duplicate IDs, missing repositories, invalid types, fractions outside `0..1`, and expectations without validation commands are rejected before the suite runs.

## Adding A Case

1. Add a small repository under `evals/fixtures/` or point the case at another stable local fixture.
2. Give the case a task that identifies the behavior under evaluation.
3. Add only expectations that can be verified from `WorkflowReport`.
4. Keep API-backed cases under `evals/llm_cases/`, not the default `evals/cases/` directory.
5. Inspect every failed criterion instead of weakening a threshold to hide a regression.
6. Run the deterministic baseline and complete unit test suite before committing.

Keep fixtures deterministic and free of credentials, network requirements, generated histories, and large dependencies.
