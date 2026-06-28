"""Command-line interface for RepoPilot Agent."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .git_summary import build_git_workflow_summary
from .git_tools import inspect_repository
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
    run_parser.add_argument("--json", action="store_true", help="Print the workflow report as JSON.")

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

    args = parser.parse_args()
    if args.command == "run":
        report = run_workflow(args.repo, args.task, args.validate)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report)
        return 0
    if args.command == "git":
        return _handle_git_command(args)
    return 1


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


def _print_report(report) -> None:
    print("RepoPilot Agent Report")
    print("=" * 22)
    print(f"Task: {report.task}")
    print(f"Repository: {report.repo_path}")
    print(f"Files scanned: {report.files_scanned}")
    print()

    print("Relevant files")
    if report.relevant_files:
        for hit in report.relevant_files:
            print(f"- {hit.path} (score: {hit.score})")
            print(f"  Reasons: {', '.join(hit.reasons)}")
    else:
        print("- No relevant files found from the current query.")
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
        if report.patch_proposal.validation_suggestions:
            print("Validation suggestions")
            for suggestion in report.patch_proposal.validation_suggestions:
                print(f"- {suggestion}")
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


if __name__ == "__main__":
    raise SystemExit(main())
