# RepoPilot Documentation

Use these documents according to what you are trying to do:

| Document | Purpose |
| --- | --- |
| [简体中文 README](README.zh-CN.md) | Read the concise project overview and quick start in Simplified Chinese. |
| [Tutorial](tutorial.md) | Install RepoPilot, configure an LLM, use the CLI and Web UI, run sandboxed tasks, review diffs, validate changes, and troubleshoot failures. |
| [Architecture](architecture.md) | Understand the Agent loop, repository instructions, trajectories, context, virtual patches, approval boundaries, recovery, validation, persistence, and safety. |
| [Evaluation Guide](../evals/README.md) | Run deterministic or opt-in LLM trajectory evaluations and add reproducible cases. |
| [Contributing Guide](../CONTRIBUTING.md) | Set up a development environment, follow repository conventions, test changes, and prepare contributions. |
| [Environment Reference](../.env.example) | See supported environment variables without storing real credentials. |

For a first run, start with the [Tutorial](tutorial.md). For implementation work, read the [Architecture](architecture.md) before changing runtime, approval, persistence, or recovery behavior.
