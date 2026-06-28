"""Command-line interface for RepoPilot Agent."""

from __future__ import annotations

import argparse
import json

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

    args = parser.parse_args()
    if args.command == "run":
        report = run_workflow(args.repo, args.task, args.validate)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report)
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


if __name__ == "__main__":
    raise SystemExit(main())
