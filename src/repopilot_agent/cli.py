"""Command-line interface for RepoPilot Agent."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from .evaluation import EvalConfigurationError, run_eval_suite, write_eval_report
from .git_summary import build_git_workflow_summary
from .git_tools import inspect_repository
from .github_tools import inspect_github_repository
from .web_server import run_web_server
from .worktree_sandbox import (
    WorktreeSandboxError,
    create_worktree_sandbox,
    list_worktree_sandboxes,
    remove_worktree_sandbox,
)
from .workflow import run_workflow


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the RepoPilot local workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Analyze a repository task.")
    run_parser.add_argument("--repo", required=True, help="Path to the repository to analyze.")
    run_parser.add_argument("--task", required=True, help="Bug report, issue, or feature request.")
    run_parser.add_argument(
        "--validate",
        action="append",
        default=[],
        help="Allowlisted validation command to run. Can be provided multiple times.",
    )
    run_parser.add_argument("--use-llm", action="store_true", help="Use an LLM for planning when configured.")
    run_parser.add_argument("--model", help="Override the model used by the LLM planner.")
    run_parser.add_argument(
        "--llm-timeout",
        type=int,
        help="LLM request timeout in seconds. Defaults to REPOPILOT_LLM_TIMEOUT_SECONDS or 120.",
    )
    run_parser.add_argument(
        "--iterative-agent",
        action="store_true",
        help="Run a read-only multi-step LLM exploration loop before planning and proposal.",
    )
    run_parser.add_argument(
        "--agent-max-steps",
        type=int,
        default=6,
        help="Maximum read-only iterative agent steps when --iterative-agent is enabled.",
    )
    run_parser.add_argument(
        "--no-json-mode",
        action="store_true",
        help="Do not send response_format=json_object to the OpenAI-compatible provider.",
    )
    run_parser.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="Fail instead of falling back to the rule-based planner when LLM planning fails.",
    )
    run_parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable related local memory lookup for this run.",
    )
    run_parser.add_argument("--json", action="store_true", help="Print the workflow report as JSON.")

    eval_parser = subparsers.add_parser("eval", help="Run a reproducible RepoPilot evaluation suite.")
    eval_parser.add_argument(
        "--suite",
        default="evals/cases",
        help="Evaluation JSON file or directory. Defaults to evals/cases.",
    )
    eval_parser.add_argument("--use-llm", action="store_true", help="Evaluate LLM-backed planning and proposals.")
    eval_parser.add_argument("--model", help="Override the model used during LLM evaluation.")
    eval_parser.add_argument(
        "--llm-timeout",
        type=int,
        help="LLM request timeout in seconds. Defaults to REPOPILOT_LLM_TIMEOUT_SECONDS or 120.",
    )
    eval_parser.add_argument(
        "--iterative-agent",
        action="store_true",
        help="Evaluate the read-only multi-step exploration loop before planning.",
    )
    eval_parser.add_argument(
        "--agent-max-steps",
        type=int,
        default=6,
        help="Maximum iterative agent steps. Defaults to 6.",
    )
    eval_parser.add_argument(
        "--search-limit",
        type=int,
        default=8,
        help="Maximum relevant files passed into the workflow. Defaults to 8.",
    )
    eval_parser.add_argument(
        "--no-json-mode",
        action="store_true",
        help="Do not send response_format=json_object to the OpenAI-compatible provider.",
    )
    eval_parser.add_argument(
        "--no-llm-fallback",
        action="store_true",
        help="Fail a case instead of using deterministic fallback after an LLM error.",
    )
    eval_parser.add_argument("--output", help="Write the aggregate JSON report to this path.")
    eval_parser.add_argument("--json", action="store_true", help="Print the aggregate report as JSON.")

    sandbox_parser = subparsers.add_parser("sandbox", help="Manage isolated Git worktree sandboxes.")
    sandbox_subparsers = sandbox_parser.add_subparsers(dest="sandbox_command", required=True)

    sandbox_create_parser = sandbox_subparsers.add_parser("create", help="Create a detached worktree sandbox.")
    sandbox_create_parser.add_argument("--repo", default=".", help="Path to a clean Git repository.")
    sandbox_create_parser.add_argument("--ref", default="HEAD", help="Commit or ref used as the sandbox base.")
    sandbox_create_parser.add_argument("--name", help="Optional unique sandbox directory name.")
    sandbox_create_parser.add_argument("--worktree-root", help="Override the managed sandbox root directory.")
    sandbox_create_parser.add_argument("--json", action="store_true", help="Print sandbox metadata as JSON.")

    sandbox_list_parser = sandbox_subparsers.add_parser("list", help="List managed worktree sandboxes.")
    sandbox_list_parser.add_argument("--repo", default=".", help="Path to any linked repository worktree.")
    sandbox_list_parser.add_argument("--worktree-root", help="Override the managed sandbox root directory.")
    sandbox_list_parser.add_argument("--json", action="store_true", help="Print sandbox metadata as JSON.")

    sandbox_remove_parser = sandbox_subparsers.add_parser("remove", help="Remove a managed worktree sandbox.")
    sandbox_remove_parser.add_argument("--repo", default=".", help="Path to any linked repository worktree.")
    sandbox_remove_parser.add_argument("--path", required=True, help="Managed worktree path to remove.")
    sandbox_remove_parser.add_argument("--worktree-root", help="Override the managed sandbox root directory.")
    sandbox_remove_parser.add_argument(
        "--force",
        action="store_true",
        help="Explicitly discard uncommitted changes in the sandbox.",
    )
    sandbox_remove_parser.add_argument("--json", action="store_true", help="Print removal metadata as JSON.")

    git_parser = subparsers.add_parser("git", help="Inspect local Git workflow state.")
    git_subparsers = git_parser.add_subparsers(dest="git_command", required=True)

    git_status_parser = git_subparsers.add_parser("status", help="Show branch, remote, commit, and file state.")
    git_status_parser.add_argument("--repo", default=".", help="Path to the Git repository.")
    git_status_parser.add_argument("--json", action="store_true", help="Print Git state as JSON.")

    git_summary_parser = git_subparsers.add_parser("summary", help="Generate commit and workflow summary text.")
    git_summary_parser.add_argument("--repo", default=".", help="Path to the Git repository.")
    git_summary_parser.add_argument(
        "--validation",
        action="append",
        default=[],
        help="Validation note to include in the generated PR draft. Can be repeated.",
    )
    git_summary_parser.add_argument("--json", action="store_true", help="Print Git summary as JSON.")

    git_pr_parser = git_subparsers.add_parser("pr-draft", help="Generate a pull request title and body draft.")
    git_pr_parser.add_argument("--repo", default=".", help="Path to the Git repository.")
    git_pr_parser.add_argument(
        "--validation",
        action="append",
        default=[],
        help="Validation note to include in the generated PR draft. Can be repeated.",
    )
    git_pr_parser.add_argument("--json", action="store_true", help="Print PR draft as JSON.")

    github_parser = subparsers.add_parser("github", help="Inspect GitHub issue, PR, review, and CI state.")
    github_subparsers = github_parser.add_subparsers(dest="github_command", required=True)

    github_status_parser = github_subparsers.add_parser("status", help="Show GitHub repository collaboration state.")
    github_status_parser.add_argument("--repo", default=".", help="Path to the local Git repository.")
    github_status_parser.add_argument("--limit", type=int, default=5, help="Maximum number of issues and PRs to read.")
    github_status_parser.add_argument("--json", action="store_true", help="Print GitHub state as JSON.")

    serve_parser = subparsers.add_parser("serve", help="Start the local RepoPilot web UI.")
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host for the local web server.")
    serve_parser.add_argument("--port", type=int, default=8765, help="Port for the local web server.")

    args = parser.parse_args()
    if args.command == "run":
        report = run_workflow(
            args.repo,
            args.task,
            args.validate,
            use_llm=args.use_llm,
            llm_model=args.model,
            allow_llm_fallback=not args.no_llm_fallback,
            llm_json_mode=False if args.no_json_mode else None,
            llm_timeout_seconds=args.llm_timeout,
            iterative_agent=args.iterative_agent,
            agent_max_steps=args.agent_max_steps,
            use_memory=not args.no_memory,
        )
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report)
        return 0
    if args.command == "eval":
        return _handle_eval_command(args)
    if args.command == "sandbox":
        return _handle_sandbox_command(args)
    if args.command == "git":
        return _handle_git_command(args)
    if args.command == "github":
        return _handle_github_command(args)
    if args.command == "serve":
        run_web_server(args.host, args.port)
        return 0
    return 1


def _handle_eval_command(args) -> int:
    try:
        result = run_eval_suite(
            args.suite,
            use_llm=args.use_llm,
            llm_model=args.model,
            allow_llm_fallback=not args.no_llm_fallback,
            llm_json_mode=False if args.no_json_mode else None,
            llm_timeout_seconds=args.llm_timeout,
            iterative_agent=args.iterative_agent,
            agent_max_steps=args.agent_max_steps,
            search_limit=args.search_limit,
        )
    except EvalConfigurationError as exc:
        print(f"Evaluation configuration error: {exc}", file=sys.stderr)
        return 2

    output_path = None
    if args.output:
        try:
            output_path = write_eval_report(result, args.output)
        except EvalConfigurationError as exc:
            print(f"Evaluation output error: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_eval_report(result)
        if output_path:
            print(f"\nJSON report: {output_path}")
    return 0 if result.passed else 1


def _print_eval_report(result) -> None:
    print("RepoPilot Evaluation Report")
    print("===========================")
    print(f"Suite: {result.suite_path}")
    print(f"Mode: {result.mode}")
    if result.model:
        print(f"Model: {result.model}")
    print(f"Cases: {result.passed_cases}/{result.total_cases} passed")
    print(f"Pass rate: {result.pass_rate:.2f}%")
    print(f"Average score: {result.average_score:.2f}%")
    if result.average_relevant_file_recall is not None:
        print(f"Relevant-file recall: {result.average_relevant_file_recall * 100:.2f}%")
    if result.average_proposal_file_recall is not None:
        print(f"Proposal-file recall: {result.average_proposal_file_recall * 100:.2f}%")
    print(f"Duration: {result.duration_ms} ms")
    print(
        "LLM: "
        f"{result.total_llm_calls} traced call(s), "
        f"{result.total_llm_failures} failure(s), "
        f"{result.total_fallbacks} fallback stage(s), "
        f"{result.total_llm_latency_ms} ms provider latency"
    )
    print()
    print("Cases")
    for case in result.cases:
        status = "PASS" if case.passed else "FAIL"
        print(f"- [{status}] {case.case_id}: {case.score:.2f}% ({case.duration_ms} ms)")
        if case.error:
            print(f"  Error: {case.error}")
        failed_criteria = [criterion for criterion in case.criteria if not criterion.passed]
        for criterion in failed_criteria:
            print(
                f"  - {criterion.name}: expected {criterion.expected!r}, "
                f"got {criterion.actual!r}"
            )
            if criterion.detail:
                print(f"    {criterion.detail}")


def _handle_sandbox_command(args) -> int:
    try:
        if args.sandbox_command == "create":
            sandbox = create_worktree_sandbox(
                args.repo,
                base_ref=args.ref,
                name=args.name,
                worktree_root=args.worktree_root,
            )
            if args.json:
                print(json.dumps(sandbox.to_dict(), indent=2))
            else:
                _print_sandbox(sandbox, heading="Created RepoPilot worktree sandbox")
            return 0
        if args.sandbox_command == "list":
            sandboxes = list_worktree_sandboxes(args.repo, worktree_root=args.worktree_root)
            if args.json:
                print(json.dumps({"sandboxes": [item.to_dict() for item in sandboxes]}, indent=2))
            else:
                print("RepoPilot Worktree Sandboxes")
                print("===========================")
                if not sandboxes:
                    print("No managed sandboxes found.")
                for sandbox in sandboxes:
                    _print_sandbox(sandbox)
            return 0
        if args.sandbox_command == "remove":
            removal = remove_worktree_sandbox(
                args.repo,
                args.path,
                force=args.force,
                worktree_root=args.worktree_root,
            )
            if args.json:
                print(json.dumps(removal.to_dict(), indent=2))
            else:
                print(f"Removed worktree sandbox: {removal.path}")
                print(f"Forced: {'yes' if removal.forced else 'no'}")
            return 0
    except WorktreeSandboxError as exc:
        print(f"Sandbox error: {exc}", file=sys.stderr)
        return 2
    return 1


def _print_sandbox(sandbox, heading: str | None = None) -> None:
    if heading:
        print(heading)
        print("=" * len(heading))
    print(f"Path: {sandbox.path}")
    print(f"Source: {sandbox.source_repo}")
    print(f"HEAD: {sandbox.head}")
    print(f"Branch: {sandbox.branch or 'detached'}")
    clean_label = "unknown" if sandbox.clean is None else ("yes" if sandbox.clean else "no")
    print(f"Clean: {clean_label}")
    if sandbox.base_ref:
        print(f"Base ref: {sandbox.base_ref}")
    if not heading:
        print()


def _handle_git_command(args) -> int:
    if args.git_command == "status":
        state = inspect_repository(args.repo)
        if args.json:
            print(json.dumps(asdict(state), indent=2))
        else:
            _print_git_status(state)
        return 0

    summary = build_git_workflow_summary(args.repo, args.validation)
    if args.git_command == "summary":
        if args.json:
            print(json.dumps(summary.to_dict(), indent=2))
        else:
            _print_git_summary(summary)
        return 0
    if args.git_command == "pr-draft":
        if args.json:
            print(json.dumps(asdict(summary.pull_request), indent=2))
        else:
            print(summary.pull_request.title)
            print()
            print(summary.pull_request.body)
        return 0
    return 1


def _handle_github_command(args) -> int:
    if args.github_command == "status":
        snapshot = inspect_github_repository(args.repo, limit=args.limit)
        if args.json:
            print(json.dumps(snapshot.to_dict(), indent=2))
        else:
            _print_github_status(snapshot)
        return 0
    return 1


def _print_report(report) -> None:
    print("RepoPilot Agent Report")
    print("=" * 22)
    print(f"Task: {report.task}")
    print(f"Repository: {report.repo_path}")
    print(f"Files scanned: {report.files_scanned}")
    print(f"Plan source: {report.plan_metadata.source}")
    if report.plan_metadata.model:
        print(f"Plan model: {report.plan_metadata.model}")
    if report.plan_metadata.fallback_used:
        print("Plan fallback: used")
    if report.plan_metadata.error:
        print(f"Plan error: {report.plan_metadata.error}")
    print(f"Patch proposal source: {report.patch_proposal_metadata.source}")
    if report.patch_proposal_metadata.model:
        print(f"Patch proposal model: {report.patch_proposal_metadata.model}")
    if report.patch_proposal_metadata.fallback_used:
        print("Patch proposal fallback: used")
    if report.patch_proposal_metadata.error:
        print(f"Patch proposal error: {report.patch_proposal_metadata.error}")
    print()

    print("Relevant files")
    if report.relevant_files:
        for hit in report.relevant_files:
            print(f"- {hit.path} (score: {hit.score})")
            print(f"  Reasons: {', '.join(hit.reasons)}")
    else:
        print("- No relevant files found from the current query.")
    print()

    print("Related memory")
    if report.memory_context:
        for memory in report.memory_context:
            status_parts = []
            if memory.pinned:
                status_parts.append("pinned")
            status_parts.append("applied" if memory.applied else "open")
            status = ", ".join(status_parts)
            print(f"- {memory.task} [{memory.mode}, {status}, score: {memory.score}]")
            print(f"  Summary: {memory.summary}")
            print(f"  Reasons: {', '.join(memory.reasons)}")
            if memory.validation:
                print(f"  Validation: {'; '.join(memory.validation)}")
    else:
        print("- No related previous runs found.")
    print()

    print("Agent steps")
    if report.agent_steps:
        for step in report.agent_steps:
            print(f"{step.order}. {step.action}: {step.thought}")
            if step.tool_input:
                print(f"  Input: {step.tool_input}")
            if step.selected_paths:
                print(f"  Selected: {', '.join(step.selected_paths)}")
    else:
        print("- Iterative agent was not run.")
    print()

    print("Plan")
    for step in report.plan:
        print(f"{step.order}. {step.title}: {step.detail}")
    print()

    print("Proposed changes")
    if report.patch_proposal and report.patch_proposal.files:
        print(f"Objective: {report.patch_proposal.objective}")
        for proposal in report.patch_proposal.files:
            print(f"- {proposal.path} [{proposal.change_type}, confidence: {proposal.confidence}]")
            print(f"  Rationale: {proposal.rationale}")
            for action in proposal.suggested_actions:
                print(f"  - {action}")
        if report.patch_proposal.risks:
            print("Risks")
            for risk in report.patch_proposal.risks:
                print(f"- {risk.level}: {risk.message}")
                print(f"  Mitigation: {risk.mitigation}")
        validation_plan_items = set()
        if report.patch_proposal.validation_plan:
            validation_plan_items.update(report.patch_proposal.validation_plan.commands)
            validation_plan_items.update(report.patch_proposal.validation_plan.notes)
        extra_suggestions = [
            suggestion
            for suggestion in report.patch_proposal.validation_suggestions
            if suggestion not in validation_plan_items
        ]
        if extra_suggestions:
            print("Validation suggestions")
            for suggestion in extra_suggestions:
                print(f"- {suggestion}")
        if report.patch_proposal.validation_plan:
            print("Recommended validation")
            for command in report.patch_proposal.validation_plan.commands:
                print(f"- {command}")
            for note in report.patch_proposal.validation_plan.notes:
                print(f"- Note: {note}")
    else:
        print("- No patch proposal was prepared from the current query.")
    print()

    if report.validation:
        print("Validation")
        for result in report.validation:
            status = "allowed" if result.allowed else "rejected"
            exit_code = "n/a" if result.exit_code is None else result.exit_code
            print(f"- {result.command} [{status}, exit code: {exit_code}]")
    else:
        print("Validation")
        print("- No validation commands were provided.")
    print()

    print("Validation feedback")
    if report.validation_feedback:
        feedback = report.validation_feedback
        print(feedback.summary)
        if feedback.suspected_files:
            print(f"Suspected files: {', '.join(feedback.suspected_files)}")
        for step in feedback.repair_steps:
            print(f"- {step}")
    else:
        print("- No validation failures detected.")
    print()

    print("Summary")
    print(report.summary)


def _print_git_status(state) -> None:
    print("RepoPilot Git Status")
    print("====================")
    print(f"Repository: {state.repo_path}")
    print(f"Branch: {state.branch}")
    print(f"Upstream: {state.upstream or 'none'}")
    print(f"Ahead/behind: +{state.ahead}/-{state.behind}")
    print()

    print("Remotes")
    if state.remotes:
        for remote in state.remotes:
            print(f"- {remote.name} {remote.url} ({remote.kind})")
    else:
        print("- No remotes configured.")
    print()

    print("Latest commit")
    if state.latest_commit:
        commit = state.latest_commit
        print(f"- {commit.short_hash} {commit.subject} ({commit.author}, {commit.date})")
    else:
        print("- No commits found.")
    print()

    print("Working tree")
    if state.changes:
        for change in state.changes:
            print(f"- {change.path}: {change.description}")
    else:
        print("- Clean")


def _print_git_summary(summary) -> None:
    _print_git_status(summary.state)
    print()
    readiness = summary.pr_readiness
    print("PR readiness")
    print(f"Ready: {'yes' if readiness.ready else 'no'}")
    print(f"Base: {readiness.base_branch}")
    if readiness.blockers:
        print("Blockers")
        for blocker in readiness.blockers:
            print(f"- {blocker}")
    if readiness.warnings:
        print("Warnings")
        for warning in readiness.warnings:
            print(f"- {warning}")
    print()
    print("Suggested commit")
    print(summary.suggested_commit_message)
    print()
    print("Change summary")
    for line in summary.change_summary:
        print(f"- {line}")
    print()
    print("PR draft")
    print(f"Title: {summary.pull_request.title}")
    print(summary.pull_request.body)


def _print_github_status(snapshot) -> None:
    print("RepoPilot GitHub Status")
    print("=======================")
    if snapshot.repository:
        repo = snapshot.repository
        print(f"Repository: {repo.owner}/{repo.repo}")
        print(f"URL: {repo.html_url}")
    else:
        print("Repository: unavailable")
    if snapshot.unavailable_reason:
        print(f"Unavailable: {snapshot.unavailable_reason}")
        return
    print()

    print("Open issues")
    if snapshot.issues:
        for issue in snapshot.issues:
            labels = f" [{', '.join(issue.labels)}]" if issue.labels else ""
            print(f"- #{issue.number} {issue.title}{labels}")
            print(f"  Author: {issue.author}; Updated: {issue.updated_at}")
            print(f"  {issue.html_url}")
            if issue.body_preview:
                print(f"  Body: {issue.body_preview}")
            if issue.comments:
                print("  Comments")
                for comment in issue.comments:
                    print(f"  - {comment.author}: {comment.body_preview}")
    else:
        print("- No open issues returned.")
    print()

    print("Open pull requests")
    if snapshot.pull_requests:
        for pull_request in snapshot.pull_requests:
            print(f"- #{pull_request.number} {pull_request.title}")
            print(
                f"  {pull_request.source_branch} -> {pull_request.target_branch}; "
                f"Author: {pull_request.author}; Updated: {pull_request.updated_at}"
            )
            print(f"  {pull_request.html_url}")
            if pull_request.body_preview:
                print(f"  Body: {pull_request.body_preview}")
            if pull_request.files:
                print("  Files")
                for file in pull_request.files:
                    print(
                        f"  - {file.filename}: {file.status}, "
                        f"+{file.additions}/-{file.deletions} ({file.changes} changes)"
                    )
            if pull_request.comments:
                print("  Conversation")
                for comment in pull_request.comments:
                    print(f"  - {comment.author}: {comment.body_preview}")
            if pull_request.review_comments:
                print("  Review comments")
                for comment in pull_request.review_comments:
                    line = f":{comment.line}" if comment.line else ""
                    print(f"  - {comment.path}{line} by {comment.reviewer}: {comment.body_preview}")
            if pull_request.reviews:
                print("  Reviews")
                for review in pull_request.reviews:
                    print(f"  - {review.state} by {review.reviewer} at {review.submitted_at or 'unknown time'}")
                    if review.body_preview:
                        print(f"    {review.body_preview}")
            else:
                print("  Reviews: none returned")
            if pull_request.checks:
                print("  Checks")
                for check in pull_request.checks:
                    conclusion = f"/{check.conclusion}" if check.conclusion else ""
                    print(f"  - {check.name}: {check.status}{conclusion}")
                    if check.output_summary_preview:
                        print(f"    {check.output_summary_preview}")
            else:
                print("  Checks: none returned")
    else:
        print("- No open pull requests returned.")


if __name__ == "__main__":
    raise SystemExit(main())
