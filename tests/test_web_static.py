from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from repopilot_agent.web_server import STATIC_DIR


class WebApprovalUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        cls.app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        cls.app_css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")

    def test_apply_toolbar_exposes_approval_status(self) -> None:
        self.assertIn('id="applyProposal"', self.index_html)
        self.assertIn('id="approvalStatus"', self.index_html)
        self.assertIn("No apply-ready file edits.", self.index_html)

    def test_proposal_rendering_exposes_per_file_approval_controls(self) -> None:
        self.assertIn("function renderProposals", self.app_js)
        self.assertIn("data-approval-path", self.app_js)
        self.assertIn("Approve this file for apply", self.app_js)
        self.assertIn("apply-ready", self.app_js)
        self.assertIn("No direct file edit was generated for this file.", self.app_js)

    def test_checkbox_changes_update_approved_paths_and_button_state(self) -> None:
        self.assertIn('document.addEventListener("change"', self.app_js)
        self.assertIn("target?.dataset?.approvalPath", self.app_js)
        self.assertIn("state.approvedPaths.add(target.dataset.approvalPath)", self.app_js)
        self.assertIn("state.approvedPaths.delete(target.dataset.approvalPath)", self.app_js)
        self.assertIn("updateApprovalState();", self.app_js)

    def test_apply_request_sends_only_selected_approved_paths(self) -> None:
        self.assertIn("const approvedPaths = approvedFilePaths();", self.app_js)
        self.assertIn("...buildRepositoryPayload(),", self.app_js)
        self.assertIn("approved_paths: approvedPaths", self.app_js)
        self.assertIn("with ${approvedPaths.length} approved file edit(s)", self.app_js)

    def test_revert_request_sends_repository_payload_for_session_restore(self) -> None:
        self.assertIn("async function revertProposal", self.app_js)
        self.assertIn("...buildRepositoryPayload(),", self.app_js)
        self.assertIn("proposal_id: state.proposalId", self.app_js)

    def test_apply_button_is_disabled_without_selection_or_after_apply(self) -> None:
        self.assertIn("function updateApprovalState", self.app_js)
        self.assertIn("state.proposalApplied", self.app_js)
        self.assertIn("total === 0", self.app_js)
        self.assertIn("selected === 0", self.app_js)
        self.assertIn("setApprovalInputsDisabled(true)", self.app_js)
        self.assertIn("setApprovalInputsDisabled(false)", self.app_js)

    def test_default_approval_is_limited_to_visible_apply_ready_files(self) -> None:
        self.assertIn("function editableProposalPaths", self.app_js)
        self.assertIn("visiblePaths", self.app_js)
        self.assertIn(".filter((path) => !visiblePaths.size || visiblePaths.has(path));", self.app_js)
        self.assertIn("state.approvedPaths = new Set(editableProposalPaths(report.patch_proposal));", self.app_js)

    def test_approval_controls_have_dedicated_styles(self) -> None:
        self.assertIn(".approval-row", self.app_css)
        self.assertIn(".approval-row input", self.app_css)

    def test_history_detail_renders_persisted_trace_history(self) -> None:
        self.assertIn("function renderSavedTrace", self.app_js)
        self.assertIn("LLM Trace History", self.app_js)
        self.assertIn("trace.prompt_preview", self.app_js)
        self.assertIn("trace.raw_output", self.app_js)

    def test_repair_budget_controls_are_sent_and_rendered(self) -> None:
        self.assertIn('id="repairMaxAttempts"', self.index_html)
        self.assertIn("max_repair_attempts: $(\"repairMaxAttempts\").value.trim()", self.app_js)
        self.assertIn("repair_budget_exhausted", self.app_js)
        self.assertIn("next attempt", self.app_js)
        self.assertIn("function renderRepairBudget", self.app_js)

    def test_delivery_tab_exposes_pr_readiness_controls(self) -> None:
        self.assertIn('id="loadPrReadiness"', self.index_html)
        self.assertIn('id="createPullRequest"', self.index_html)
        self.assertIn('id="prReadinessContent"', self.index_html)
        self.assertIn("async function loadPrReadiness", self.app_js)
        self.assertIn("async function createPullRequest", self.app_js)
        self.assertIn("/api/github/pr/readiness", self.app_js)
        self.assertIn("/api/github/pr/create", self.app_js)
        self.assertIn("function renderPrReadiness", self.app_js)
        self.assertIn("function updateCreatePullRequestState", self.app_js)
        self.assertIn("create_pr_command", self.app_js)

    def test_repository_controls_expose_worktree_sandbox_lifecycle(self) -> None:
        self.assertIn('id="sandboxSelect"', self.index_html)
        self.assertIn('id="createSandbox"', self.index_html)
        self.assertIn('id="refreshSandboxes"', self.index_html)
        self.assertIn('id="removeSandbox"', self.index_html)
        self.assertIn('id="sandboxLine"', self.index_html)
        self.assertIn("async function createSandbox", self.app_js)
        self.assertIn("async function refreshSandboxes", self.app_js)
        self.assertIn("async function removeSandbox", self.app_js)
        self.assertIn("/api/sandbox/create", self.app_js)
        self.assertIn("/api/sandbox/list", self.app_js)
        self.assertIn("/api/sandbox/remove", self.app_js)
        self.assertIn("Permanently discard them", self.app_js)
        self.assertIn("resetProposalForRepositoryChange", self.app_js)

    def test_task_run_ui_exposes_orchestration_and_delivery_controls(self) -> None:
        for element_id in [
            "startTaskRun",
            "taskRunStatus",
            "taskRunPhases",
            "pauseTaskRun",
            "checkTaskRunReadiness",
            "resumeTaskRun",
            "cancelTaskRun",
            "taskRunRecoveryReadiness",
            "taskRunExecutionProfile",
            "taskRunLatestCheckpoint",
            "taskRunCheckpoints",
            "taskRunEvents",
            "taskRunBranch",
            "createTaskBranch",
        ]:
            self.assertIn(f'id="{element_id}"', self.index_html)
        for endpoint in [
            "/api/task-runs/start",
            "/api/task-runs/status",
            "/api/task-runs/pause",
            "/api/task-runs/recovery/readiness",
            "/api/task-runs/resume",
            "/api/task-runs/cancel",
            "/api/task-runs/branch",
        ]:
            self.assertIn(endpoint, self.app_js)
        self.assertIn("startTaskRunPolling", self.app_js)
        self.assertIn("function renderTaskRunCheckpoints", self.app_js)
        self.assertIn("async function checkTaskRunRecoveryReadiness", self.app_js)
        self.assertIn("function renderTaskRunRecoveryReadiness", self.app_js)
        self.assertIn("function renderExecutionProfileComparison", self.app_js)
        self.assertIn("readiness.execution_profile_comparison", self.app_js)
        self.assertIn("formatExecutionProfileValue", self.app_js)
        self.assertIn("...buildWorkflowPayload(),", self.app_js)
        self.assertIn("function renderTaskRunExecutionProfile", self.app_js)
        self.assertIn("taskRun.execution_profile", self.app_js)
        self.assertIn("profile.endpoint_fingerprint", self.app_js)
        self.assertIn("state.taskRunRecoveryReadiness", self.app_js)
        self.assertIn("readiness?.ready", self.app_js)
        self.assertIn("taskRun.latest_checkpoint", self.app_js)
        self.assertIn("taskRun.checkpoints", self.app_js)
        self.assertIn("task_run_id: state.taskRun.run_id", self.app_js)
        self.assertIn("RepoPilot will not commit or push", self.app_js)
        self.assertIn(".task-run-phases", self.app_css)

    def test_summary_exposes_typed_runtime_events(self) -> None:
        self.assertIn('id="runtimeEventList"', self.index_html)
        self.assertIn('id="runtimeApproval"', self.index_html)
        self.assertIn('id="agentWorkingState"', self.index_html)
        self.assertIn("function renderRuntimeEvents", self.app_js)
        self.assertIn("function renderRuntimeApproval", self.app_js)
        self.assertIn("function pendingApprovalFromEvents", self.app_js)
        self.assertIn("function renderAgentWorkingState", self.app_js)
        self.assertIn("report.agent_stop_reason", self.app_js)
        self.assertIn("report.agent_pending_question", self.app_js)
        self.assertIn("report.agent_pending_approval", self.app_js)
        self.assertIn("run.agent_stop_reason", self.app_js)
        self.assertIn("run.agent_pending_question", self.app_js)
        self.assertIn("run.agent_pending_approval", self.app_js)
        self.assertIn('event.event_type === "input_required"', self.app_js)
        self.assertIn("latestInputEvent?.payload?.observation?.data?.question", self.app_js)
        self.assertIn("Pending question", self.app_js)
        self.assertIn("Pending Runtime Approval", self.app_js)
        self.assertIn("Payload SHA-256", self.app_js)
        self.assertIn("Exact action", self.app_js)
        self.assertIn("Exact diff", self.app_js)
        self.assertIn("Bounded diff (truncated)", self.app_js)
        self.assertIn("report.agent_completion_ready", self.app_js)
        self.assertIn("report.agent_completion_blockers", self.app_js)
        self.assertIn("report.agent_proposed_edits", self.app_js)
        self.assertIn("report.agent_proposed_diff", self.app_js)
        self.assertIn("agentState.proposed_edits", self.app_js)
        self.assertIn("Cumulative virtual diff", self.app_js)
        self.assertIn("agentState.plan", self.app_js)
        self.assertIn("agentState.acceptance_criteria", self.app_js)
        self.assertIn("Completion ready", self.app_js)
        self.assertIn("value.evidence_action_ids", self.app_js)
        self.assertIn("observation.action_id", self.app_js)
        self.assertIn('event.event_type === "working_state_updated"', self.app_js)
        self.assertIn("step.expected_evidence", self.app_js)
        self.assertIn("step.state_update", self.app_js)
        self.assertIn("step.finish_reason", self.app_js)
        self.assertIn("agentState.focus", self.app_js)
        self.assertIn("agentState.findings", self.app_js)
        self.assertIn("agentState.open_questions", self.app_js)
        self.assertIn("agentState.expected_evidence", self.app_js)
        self.assertIn("report.agent_events || []", self.app_js)
        self.assertIn("run.agent_events || []", self.app_js)
        self.assertIn(".runtime-event", self.app_css)

    def test_runtime_write_approval_controls_are_exact_and_task_scoped(self) -> None:
        for element_id in [
            "approveRuntimeWrite",
            "rejectRuntimeWrite",
            "runtimeApprovalStatus",
            "runtimeWriteResult",
        ]:
            self.assertIn(f'id="{element_id}"', self.index_html)
        for endpoint in [
            "/api/task-runs/runtime-approval/grant",
            "/api/task-runs/runtime-approval/reject",
        ]:
            self.assertIn(endpoint, self.app_js)
        self.assertIn("async function approveRuntimeWrite", self.app_js)
        self.assertIn("async function rejectRuntimeWrite", self.app_js)
        self.assertIn("payload_hash: request.payload_hash", self.app_js)
        self.assertIn("file_scope: request.file_scope || []", self.app_js)
        self.assertIn("function renderRuntimeWriteResult", self.app_js)
        self.assertIn("Before ${escapeHtml(entry.before_sha256", self.app_js)
        self.assertIn("Rollback snapshot", self.app_js)

    def test_summary_exposes_repository_map_metrics_and_entries(self) -> None:
        self.assertIn('id="symbolsIndexed"', self.index_html)
        self.assertIn('id="repositoryMapList"', self.index_html)
        self.assertIn("function renderRepositoryMap", self.app_js)
        self.assertIn("report.repository_map?.symbols_indexed", self.app_js)
        self.assertIn("repositoryMap.relevant_entries", self.app_js)

    def test_validation_aware_execution_controls_and_evidence_are_visible(self) -> None:
        for element_id in [
            "agentMaxToolCalls",
            "maxValidationCommands",
            "executionTimeoutSeconds",
            "acceptanceCriteriaList",
            "executionBudgetList",
            "completionEvidenceList",
            "taskRunCriteria",
            "taskRunBudget",
            "taskRunEvidence",
        ]:
            self.assertIn(f'id="{element_id}"', self.index_html)
        for payload_key in [
            "agent_max_tool_calls",
            "max_validation_commands",
            "execution_timeout_seconds",
        ]:
            self.assertIn(payload_key, self.app_js)
        self.assertIn("function renderAcceptanceCriteria", self.app_js)
        self.assertIn("function renderExecutionBudget", self.app_js)
        self.assertIn("function renderCompletionEvidence", self.app_js)

    def test_autonomous_repair_controls_and_progress_are_visible(self) -> None:
        for element_id in ["autoRepair", "repairLoopList", "taskRunRepairLoop"]:
            self.assertIn(f'id="{element_id}"', self.index_html)
        self.assertIn("auto_repair: $(\"autoRepair\").checked", self.app_js)
        self.assertIn("function buildRepairAutomationPayload", self.app_js)
        self.assertIn("function renderRepairLoop", self.app_js)
        self.assertIn("repair_stop_reason", self.app_js)
        self.assertIn('"diagnosing", "replanning"', self.app_js)
        self.assertIn('taskRun.status === "repair_pending"', self.app_js)
        self.assertIn('status === "failed" && taskRun.repair_stop_reason', self.app_js)

    def test_interrupted_task_state_has_a_dedicated_notice(self) -> None:
        self.assertIn('id="taskRunInterruption"', self.index_html)
        self.assertIn('id="taskRunResumeLine"', self.index_html)
        self.assertIn("function renderTaskRunInterruption", self.app_js)
        self.assertIn("function renderTaskRunResumePlan", self.app_js)
        self.assertIn("taskRun.interrupted_from", self.app_js)
        self.assertIn("taskRun.interrupted_at", self.app_js)
        self.assertIn("taskRun.interruption_reason", self.app_js)
        self.assertIn("resume_checkpoint: checkpoint", self.app_js)
        self.assertIn("confirm_resume: true", self.app_js)
        self.assertIn("window.confirm", self.app_js)
        self.assertIn('"interrupted", "awaiting_approval"', self.app_js)
        self.assertIn(".interruption-notice", self.app_css)


if __name__ == "__main__":
    unittest.main()
