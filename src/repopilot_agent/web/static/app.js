const state = {
  lastReport: null,
  github: null,
  delivery: null,
  proposalId: null,
  repairParentId: null,
  rollbackAvailable: false,
  proposalApplied: false,
  approvedPaths: new Set(),
  sandbox: null,
  sandboxes: [],
  taskRun: null,
  taskRunPayload: null,
  taskRunPoll: null,
  taskRunSandboxPath: null,
  taskRunRenderedProposalId: null,
  taskRunRecoveryKey: null,
  taskRunRecoveryReadiness: null,
};

const TASK_RUN_PHASES = ["Sandbox", "Explore", "Approval", "Apply", "Validate", "Complete"];

const $ = (id) => document.getElementById(id);

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((tab) => tab.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach((panel) => panel.classList.remove("active"));
    button.classList.add("active");
    $(`${button.dataset.tab}Tab`).classList.add("active");
  });
});

document.querySelectorAll(".subtab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".subtab").forEach((tab) => tab.classList.remove("active"));
    document.querySelectorAll(".llm-panel").forEach((panel) => panel.classList.remove("active"));
    button.classList.add("active");
    $(`llm${capitalize(button.dataset.llmPanel || "input")}Panel`).classList.add("active");
  });
});

$("modelSelect").addEventListener("change", () => {
  $("customModelWrap").classList.toggle("hidden", $("modelSelect").value !== "custom");
});
$("repoSource").addEventListener("change", updateRepositorySourceUi);
$("sandboxSelect").addEventListener("change", selectSandbox);
document.addEventListener("change", (event) => {
  const target = event.target;
  if (!target?.dataset?.approvalPath) {
    return;
  }
  if (target.checked) {
    state.approvedPaths.add(target.dataset.approvalPath);
  } else {
    state.approvedPaths.delete(target.dataset.approvalPath);
  }
  updateApprovalState();
});

$("runWorkflow").addEventListener("click", runWorkflow);
$("generateProposal").addEventListener("click", generateProposal);
$("startTaskRun").addEventListener("click", startTaskRun);
$("pauseTaskRun").addEventListener("click", pauseTaskRun);
$("checkTaskRunReadiness").addEventListener("click", checkTaskRunRecoveryReadiness);
$("resumeTaskRun").addEventListener("click", resumeTaskRun);
$("cancelTaskRun").addEventListener("click", cancelTaskRun);
$("submitTaskRunInput").addEventListener("click", submitTaskRunInput);
$("createTaskBranch").addEventListener("click", createTaskBranch);
$("approveRuntimeWrite").addEventListener("click", approveRuntimeWrite);
$("rejectRuntimeWrite").addEventListener("click", rejectRuntimeWrite);
$("testLlm").addEventListener("click", testLlmConnection);
$("applyProposal").addEventListener("click", applyProposal);
$("revertProposal").addEventListener("click", revertProposal);
$("syncRepository").addEventListener("click", syncRepository);
$("createSandbox").addEventListener("click", createSandbox);
$("refreshSandboxes").addEventListener("click", refreshSandboxes);
$("removeSandbox").addEventListener("click", removeSandbox);
$("loadGithub").addEventListener("click", loadGithub);
$("loadDiff").addEventListener("click", () => loadDiff(false));
$("loadStagedDiff").addEventListener("click", () => loadDiff(true));
$("loadPrReadiness").addEventListener("click", loadPrReadiness);
$("generateDelivery").addEventListener("click", generateDelivery);
$("createPullRequest").addEventListener("click", createPullRequest);
$("generateRepairProposal").addEventListener("click", generateRepairProposal);
$("loadHistory").addEventListener("click", loadHistory);
$("clearHistory").addEventListener("click", clearHistory);
$("refreshAll").addEventListener("click", async () => {
  await Promise.allSettled([loadGithub(), loadDiff(false), loadHistory(), pollTaskRun()]);
});

function selectedModel() {
  return $("modelSelect").value === "custom" ? $("customModel").value.trim() : $("modelSelect").value;
}

async function runWorkflow() {
  setStatus("Running RepoPilot...");
  const payload = buildWorkflowPayload();

  try {
    const report = await postJson("/api/run", payload);
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, payload);
    setStatus("Workflow complete.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function generateProposal() {
  setStatus("Generating patch proposal...");
  const payload = buildWorkflowPayload();

  try {
    const report = await postJson("/api/propose", payload);
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, payload);
    setStatus("Proposal ready for review.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function startTaskRun() {
  const payload = buildWorkflowPayload();
  if (!payload.task) {
    setStatus("Task is required.");
    return;
  }
  setStatus("Starting sandboxed task run...");
  activateTab("taskRun");
  try {
    const data = await postJson("/api/task-runs/start", payload);
    if (data.error) {
      throw new Error(data.error);
    }
    state.taskRunPayload = payload;
    state.taskRunSandboxPath = null;
    state.taskRunRenderedProposalId = null;
    updateTaskRun(data.task_run);
    const shortId = String(data.task_run?.run_id || "task").slice(0, 8);
    $("taskRunBranch").value = `feature/repopilot-${shortId}`;
    startTaskRunPolling();
    setStatus("Sandboxed task run started.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function pauseTaskRun() {
  if (!state.taskRun) return;
  setStatus("Requesting task pause...");
  try {
    const data = await postJson("/api/task-runs/pause", taskRunControlPayload());
    if (data.error) throw new Error(data.error);
    updateTaskRun(data.task_run);
    startTaskRunPolling();
    setStatus(data.task_run.message || "Pause requested.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function resumeTaskRun() {
  if (!state.taskRun) return;
  const checkpoint = String(state.taskRun.resume_checkpoint || "").trim();
  if (!checkpoint || !state.taskRun.can_resume) {
    setStatus(state.taskRun.resume_blocked_reason || "No safe resume checkpoint is available.");
    return;
  }
  const readiness = await checkTaskRunRecoveryReadiness();
  if (!readiness?.ready) {
    setStatus(readiness?.summary || "Recovery readiness could not be verified.");
    return;
  }
  if (readiness.checkpoint !== checkpoint || state.taskRun.resume_checkpoint !== checkpoint) {
    setStatus("The recovery checkpoint changed during preflight. Review the updated task state.");
    return;
  }
  const checkpointLabel = checkpoint.replaceAll("_", " ");
  const runtimeRecovery = readiness.runtime_recovery || {};
  const pendingAction = runtimeRecovery.pending_action || null;
  const exactConfirmation = {};
  const confirmed = runtimeRecovery.requires_confirmation && pendingAction
    ? window.confirm(
        `Action ${pendingAction.action_id} (${pendingAction.action_kind}) started before interruption, but its outcome was not recorded. Continue from ${checkpointLabel} without replaying it? The Agent will inspect current repository evidence before deciding again.`
      )
    : window.confirm(
        `Resume this task from the ${checkpointLabel} checkpoint? RepoPilot will run the saved safety preflight first.`
      );
  if (!confirmed) return;
  if (runtimeRecovery.requires_confirmation && pendingAction) {
    exactConfirmation.confirm_ambiguous_action = true;
    exactConfirmation.runtime_recovery_action_id = pendingAction.action_id;
    exactConfirmation.runtime_recovery_token = pendingAction.confirmation_token;
  }
  setStatus("Resuming task run...");
  try {
    const data = await postJson("/api/task-runs/resume", {
      ...buildWorkflowPayload(),
      ...taskRunControlPayload(),
      resume_checkpoint: checkpoint,
      confirm_resume: true,
      ...exactConfirmation,
    });
    if (data.recovery_readiness) {
      state.taskRunRecoveryReadiness = data.recovery_readiness;
    }
    if (data.error) {
      if (data.task_run) updateTaskRun(data.task_run);
      renderTaskRunRecoveryReadiness(state.taskRunRecoveryReadiness);
      throw new Error(data.error);
    }
    state.taskRunPayload = buildWorkflowPayload();
    updateTaskRun(data.task_run);
    if (data.task_run.can_pause) startTaskRunPolling();
    setStatus(data.task_run.message || "Task run resumed.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function checkTaskRunRecoveryReadiness() {
  if (!state.taskRun) return null;
  setStatus("Checking recovery readiness...");
  try {
    const data = await postJson(
      "/api/task-runs/recovery/readiness",
      {
        ...buildWorkflowPayload(),
        ...taskRunControlPayload(),
      }
    );
    if (data.error) throw new Error(data.error);
    if (data.task_run) updateTaskRun(data.task_run);
    state.taskRunRecoveryReadiness = data.recovery_readiness || null;
    renderTaskRunRecoveryReadiness(state.taskRunRecoveryReadiness);
    setStatus(data.recovery_readiness?.summary || "Recovery readiness checked.");
    return state.taskRunRecoveryReadiness;
  } catch (error) {
    state.taskRunRecoveryReadiness = null;
    renderTaskRunRecoveryReadiness(null);
    setStatus(`Error: ${error.message}`);
    return null;
  }
}

async function cancelTaskRun() {
  if (!state.taskRun) return;
  const confirmed = window.confirm("Cancel this task run and preserve its sandbox for inspection?");
  if (!confirmed) return;
  setStatus("Requesting task cancellation...");
  try {
    const data = await postJson("/api/task-runs/cancel", taskRunControlPayload());
    if (data.error) throw new Error(data.error);
    updateTaskRun(data.task_run);
    startTaskRunPolling();
    setStatus(data.task_run.message || "Cancellation requested.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function createTaskBranch() {
  if (!state.taskRun) return;
  const branchName = $("taskRunBranch").value.trim();
  if (!branchName) {
    setStatus("Feature branch name is required.");
    return;
  }
  const confirmed = window.confirm(
    `Create local branch ${branchName} in the task sandbox? RepoPilot will not commit or push.`
  );
  if (!confirmed) return;
  setStatus("Creating local feature branch...");
  try {
    const data = await postJson("/api/task-runs/branch", {
      ...taskRunControlPayload(),
      branch_name: branchName,
      confirm_create: true,
    });
    if (data.error) throw new Error(data.error);
    updateTaskRun(data.task_run);
    setStatus(`Created local branch ${data.branch}.`);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function currentRuntimeApproval() {
  const report = state.lastReport || currentTaskRunReport(state.taskRun);
  const directRequest = report?.agent_pending_approval;
  const request = directRequest?.checkpoint
    ? directRequest
    : pendingApprovalFromEvents(report?.agent_events || []);
  return request?.checkpoint ? request : null;
}

async function approveRuntimeWrite() {
  const request = currentRuntimeApproval();
  if (!request || !state.taskRun?.can_approve_runtime) {
    setStatus("No exact managed Runtime action is waiting for approval.");
    return;
  }
  const isValidation = request.action_kind === "validate";
  const command = request.action?.arguments?.command || (request.command_allowlist || [])[0] || "";
  const scope = isValidation
    ? command
    : (request.file_scope || []).join(", ") || request.action_kind || "action";
  const confirmation = isValidation
    ? `Run this exact validation command inside the managed task worktree?\n\n${scope}`
    : `Approve the exact ${request.action_kind || "write"} action for ${scope}? Only the managed task worktree will be modified.`;
  const confirmed = window.confirm(
    confirmation
  );
  if (!confirmed) return;
  setStatus(isValidation ? "Running the exact approved validation command..." : "Executing the exact approved Runtime write...");
  try {
    const data = await postJson("/api/task-runs/runtime-approval/grant", {
      ...taskRunControlPayload(),
      ...buildLlmPayload(),
      use_llm: $("useLlm").checked,
      use_memory: !$("disableMemory").checked,
      agent_max_steps: $("agentMaxSteps").value.trim(),
      checkpoint: request.checkpoint,
      payload_hash: request.payload_hash,
      file_scope: request.file_scope || [],
      command_allowlist: request.command_allowlist || [],
    });
    if (data.error) {
      if (data.task_run) updateTaskRun(data.task_run);
      throw new Error(data.error);
    }
    state.taskRunRenderedProposalId = null;
    updateTaskRun(data.task_run);
    if (data.write_result) {
      $("diffOutput").textContent = data.write_result.resulting_diff || "No resulting diff.";
    }
    setStatus(data.task_run?.message || "Approved Runtime action completed.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function rejectRuntimeWrite() {
  const request = currentRuntimeApproval();
  if (!request || !state.taskRun?.can_approve_runtime) {
    setStatus("No exact managed Runtime action is waiting for rejection.");
    return;
  }
  const isValidation = request.action_kind === "validate";
  const confirmed = window.confirm(
    isValidation
      ? "Reject this exact validation command? It will not run, and the current managed worktree will be preserved."
      : "Reject this exact Runtime write? The managed worktree will remain unchanged."
  );
  if (!confirmed) return;
  setStatus(isValidation ? "Rejecting the pending validation command..." : "Rejecting the pending Runtime write...");
  try {
    const data = await postJson("/api/task-runs/runtime-approval/reject", {
      ...taskRunControlPayload(),
      checkpoint: request.checkpoint,
      reason: "Rejected in the RepoPilot Web UI.",
    });
    if (data.error) {
      if (data.task_run) updateTaskRun(data.task_run);
      throw new Error(data.error);
    }
    state.taskRunRenderedProposalId = null;
    updateTaskRun(data.task_run);
    setStatus(data.task_run?.message || "Runtime action rejected.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function startTaskRunPolling() {
  stopTaskRunPolling();
  state.taskRunPoll = window.setInterval(() => {
    pollTaskRun().catch((error) => setStatus(`Task status error: ${error.message}`));
  }, 1000);
}

function stopTaskRunPolling() {
  if (state.taskRunPoll) {
    window.clearInterval(state.taskRunPoll);
    state.taskRunPoll = null;
  }
}

async function pollTaskRun() {
  const current = state.taskRun;
  if (!current?.run_id || !current.source_repo) return;
  const params = new URLSearchParams({
    run_id: current.run_id,
    source_repo: current.source_repo,
  });
  const data = await getJson(`/api/task-runs/status?${params.toString()}`);
  if (data.error) throw new Error(data.error);
  if (state.taskRun?.run_id !== data.task_run?.run_id) return;
  updateTaskRun(data.task_run);
}

async function loadLatestTaskRun() {
  const data = await getJson(`/api/task-runs?${repositoryQuery()}&limit=1`);
  if (data.error) throw new Error(data.error);
  const latest = (data.task_runs || [])[0];
  if (!latest) {
    $("taskRunPhases").innerHTML = TASK_RUN_PHASES.map(
      (phase, index) => `<div class="task-run-phase">${index + 1}. ${escapeHtml(phase)}</div>`
    ).join("");
    return;
  }
  state.taskRunPayload = buildWorkflowPayload();
  state.taskRunRenderedProposalId = null;
  updateTaskRun(latest);
  const shortId = String(latest.run_id || "task").slice(0, 8);
  $("taskRunBranch").value = latest.delivery_branch || `feature/repopilot-${shortId}`;
}

function updateTaskRun(taskRun) {
  if (!taskRun) return;
  const recoveryKey = [
    taskRun.run_id,
    taskRun.status,
    taskRun.resume_checkpoint,
    taskRun.latest_checkpoint?.sequence,
  ].join(":");
  if (recoveryKey !== state.taskRunRecoveryKey) {
    state.taskRunRecoveryKey = recoveryKey;
    state.taskRunRecoveryReadiness = null;
  }
  state.taskRun = taskRun;
  renderTaskRun(taskRun);
  adoptTaskRunSandbox(taskRun);
  const report = currentTaskRunReport(taskRun);
  const manualRepairAvailable = taskRun.status === "repair_pending"
    && taskRun.proposal_id && report?.validation_feedback && !taskRun.repair_stop_reason;
  if (["diagnosing", "replanning", "review_pending", "failed", "completed", "cancelled", "interrupted", "awaiting_input"].includes(taskRun.status)) {
    state.repairParentId = null;
  } else if (manualRepairAvailable) {
    state.repairParentId = taskRun.proposal_id;
  }
  $("generateRepairProposal").disabled = !state.repairParentId;
  const reportKey = report
    ? `${taskRun.proposal_id || "none"}:${taskRun.history_run_id || "analysis"}:${taskRun.updated_at || "unknown"}`
    : null;
  if (report && reportKey !== state.taskRunRenderedProposalId) {
    state.taskRunRenderedProposalId = reportKey;
    state.lastReport = report;
    renderReport(report, state.taskRunPayload || buildWorkflowPayload());
  }
  if (["completed", "review_pending", "cancelled", "failed", "paused", "interrupted", "awaiting_approval", "awaiting_input", "repair_pending"].includes(taskRun.status)) {
    stopTaskRunPolling();
  }
}

function currentTaskRunReport(taskRun) {
  const result = taskRun?.result;
  if (!result) return null;
  if (result.repair_report && result.repair_report.proposal_id === taskRun.proposal_id) {
    return result.repair_report;
  }
  return result;
}

function adoptTaskRunSandbox(taskRun) {
  if (!taskRun.sandbox_path || state.taskRunSandboxPath === taskRun.sandbox_path) return;
  state.taskRunSandboxPath = taskRun.sandbox_path;
  const sandbox = {
    source_repo: taskRun.source_repo,
    path: taskRun.sandbox_path,
    head: taskRun.sandbox_head,
    branch: taskRun.delivery_branch,
    detached: !taskRun.delivery_branch,
    clean: taskRun.status === "awaiting_approval",
    managed: true,
    primary: false,
  };
  if (!state.sandboxes.some((item) => item.path === sandbox.path)) {
    state.sandboxes = [sandbox, ...state.sandboxes];
  }
  state.sandbox = sandbox;
  $("repoSource").value = "local";
  $("repoPath").value = sandbox.path;
  $("repoBranch").value = taskRun.delivery_branch || "";
  renderSandboxOptions(sandbox.path);
  renderSandboxStatus(sandbox);
  updateRepositorySourceUi();
  resetProposalForRepositoryChange();
}

function renderTaskRun(taskRun) {
  const status = String(taskRun.status || "unknown");
  $("taskRunId").textContent = taskRun.run_id || "Not started";
  $("taskRunStatus").textContent = status.replaceAll("_", " ");
  $("taskRunStatus").classList.toggle("warning", status === "interrupted");
  $("taskRunMessage").textContent = taskRun.message || "";
  renderTaskRunInterruption(taskRun);
  $("taskRunSandbox").textContent = taskRun.sandbox_path
    ? `${taskRun.sandbox_path}\nHEAD ${taskRun.sandbox_head || "unknown"}`
    : "Not created";
  $("pauseTaskRun").disabled = !taskRun.can_pause;
  $("checkTaskRunReadiness").disabled = !["paused", "cancelled", "failed", "interrupted"].includes(status);
  $("resumeTaskRun").disabled = !taskRun.can_resume;
  $("cancelTaskRun").disabled = !taskRun.can_cancel;
  $("createTaskBranch").disabled = !taskRun.can_create_branch;
  renderTaskRunInput(taskRun);
  $("taskRunDelivery").textContent = taskRun.delivery_branch
    ? `Local branch ${taskRun.delivery_branch} is ready for manual review, commit, and push.`
    : "No delivery branch created.";
  renderTaskRunResumePlan(taskRun);
  renderTaskRunRecoveryReadiness(state.taskRunRecoveryReadiness);
  renderTaskRunExecutionProfile(taskRun.execution_profile);
  renderTaskRunCheckpoints(taskRun);
  renderTaskRunPhases(taskRun);
  $("taskRunCriteria").innerHTML = renderAcceptanceCriteria(
    taskRun.acceptance_criteria || [],
    taskRun.completion_evidence
  );
  $("taskRunBudget").innerHTML = renderExecutionBudget(taskRun.execution_budget);
  $("taskRunEvidence").innerHTML = renderCompletionEvidence(taskRun.completion_evidence);
  $("taskRunRepairLoop").innerHTML = renderRepairLoop({
    ...(taskRun.result || {}),
    ...taskRun,
  });
  const events = taskRun.events || [];
  $("taskRunEvents").innerHTML = events.length
    ? events
        .map(
          (event) => `<div class="timeline-event">
            <div class="timeline-step">${escapeHtml(String(event.status || "").replaceAll("_", " "))}</div>
            <div class="timeline-status">${escapeHtml(formatTime(event.created_at))}</div>
            <div>${escapeHtml(event.detail || "")}</div>
          </div>`
        )
        .join("")
    : item("No task-run events yet.");
}

function renderTaskRunExecutionProfile(profile) {
  if (!profile) {
    $("taskRunExecutionProfile").innerHTML = item("No saved execution profile for this legacy task.");
    return;
  }
  const budget = profile.execution_budget || {};
  const jsonMode = profile.json_mode === null || profile.json_mode === undefined
    ? "automatic"
    : profile.json_mode
      ? "enabled"
      : "disabled";
  const endpoint = profile.endpoint_configured
    ? profile.endpoint_fingerprint
      ? `${String(profile.endpoint_fingerprint).slice(0, 12)}...`
      : "configured"
    : "not configured";
  const modeTag = profile.use_llm
    ? '<span class="tag ok">LLM enabled</span>'
    : '<span class="tag">rules only</span>';
  $("taskRunExecutionProfile").innerHTML = `
    <div class="timeline-event">
      <div class="timeline-step">Profile v${escapeHtml(profile.version || 1)} ${modeTag}</div>
      <div class="timeline-status">${escapeHtml(formatTime(profile.captured_at))}</div>
      <div>Model: ${escapeHtml(profile.model || "default")} | Endpoint: ${escapeHtml(endpoint)} | LLM timeout: ${escapeHtml(profile.llm_timeout_seconds || "default")}s</div>
    </div>
    <div class="timeline-event">
      <div class="timeline-step">Agent behavior</div>
      <div class="timeline-status">saved</div>
      <div>JSON mode: ${escapeHtml(jsonMode)} | Fallback: ${profile.allow_llm_fallback ? "enabled" : "disabled"} | Memory: ${profile.use_memory ? "enabled" : "disabled"} | Iterative: ${profile.iterative_agent ? "enabled" : "disabled"} | Auto repair: ${profile.auto_repair_enabled ? "enabled" : "disabled"} | Repair attempts: ${escapeHtml(profile.max_repair_attempts ?? 0)}</div>
    </div>
    <div class="timeline-event">
      <div class="timeline-step">Execution limits</div>
      <div class="timeline-status">maximum</div>
      <div>Steps: ${escapeHtml(budget.max_agent_steps ?? "n/a")} | Tools: ${escapeHtml(budget.max_tool_calls ?? "n/a")} | Validation: ${escapeHtml(budget.max_validation_commands ?? "n/a")} | Elapsed: ${escapeHtml(budget.max_elapsed_seconds ?? "n/a")}s</div>
    </div>
  `;
}

function renderTaskRunRecoveryReadiness(readiness) {
  if (!readiness) {
    $("taskRunRecoveryReadiness").innerHTML = item("Readiness has not been checked for this task state.");
    return;
  }
  const resultTag = readiness.ready
    ? '<span class="tag ok">ready</span>'
    : '<span class="tag danger">blocked</span>';
  const checks = (readiness.checks || []).map((check) => {
    const tagClass = check.status === "passed"
      ? "ok"
      : check.status === "failed"
        ? "danger"
        : "warn";
    const name = String(check.name || "check").replaceAll("_", " ");
    return `<div class="timeline-event">
      <div class="timeline-step">${escapeHtml(name)} <span class="tag ${tagClass}">${escapeHtml(check.status)}</span></div>
      <div>${escapeHtml(check.detail || "")}</div>
    </div>`;
  }).join("");
  const profileComparison = renderExecutionProfileComparison(
    readiness.execution_profile_comparison
  );
  const runtimeRecovery = renderRuntimeRecoveryPlan(readiness.runtime_recovery);
  $("taskRunRecoveryReadiness").innerHTML = `
    <div class="timeline-event">
      <div class="timeline-step">Recovery ${resultTag}</div>
      <div class="timeline-status">${escapeHtml(formatTime(readiness.checked_at))}</div>
      <div>${escapeHtml(readiness.summary || "")}</div>
    </div>
    ${checks}
    ${runtimeRecovery}
    ${profileComparison}
  `;
}

function renderRuntimeRecoveryPlan(recovery) {
  if (!recovery) return "";
  const pending = recovery.pending_action;
  const statusClass = recovery.requires_confirmation
    ? "warn"
    : recovery.can_continue
      ? "ok"
      : "danger";
  const pendingDetail = pending
    ? `<div class="timeline-event">
        <div class="timeline-step">Exact action <span class="tag ${statusClass}">${escapeHtml(String(pending.classification || "pending").replaceAll("_", " "))}</span></div>
        <div class="timeline-status">${escapeHtml(pending.action_kind || "action")}</div>
        <div>Action ${escapeHtml(pending.action_id || "unknown")} | Payload ${escapeHtml(String(pending.payload_hash || "").slice(0, 12))}...</div>
        <div>${escapeHtml(pending.summary || "")}</div>
        ${pending.input_request?.question ? `<div><strong>Question:</strong> ${escapeHtml(pending.input_request.question)}</div>` : ""}
        ${pending.input_request?.checkpoint ? `<div><small>Input checkpoint ${escapeHtml(pending.input_request.checkpoint)}</small></div>` : ""}
        ${renderRuntimeRecoveryArguments(pending.arguments)}
      </div>`
    : "";
  const replayed = Array.isArray(recovery.replayed_observations)
    ? recovery.replayed_observations.length
    : 0;
  return `
    <div class="timeline-event">
      <div class="timeline-step">Runtime resume <span class="tag ${statusClass}">${escapeHtml(String(recovery.status || "unknown").replaceAll("_", " "))}</span></div>
      <div class="timeline-status">${escapeHtml(String(recovery.next_step || "unknown").replaceAll("_", " "))}</div>
      <div>${escapeHtml(recovery.summary || "")}</div>
      <div><small>Working State iteration ${escapeHtml(recovery.working_state_iteration ?? 0)} | Replayed read-only observations ${escapeHtml(replayed)}</small></div>
    </div>
    ${pendingDetail}
  `;
}

function renderRuntimeRecoveryArguments(argumentsValue) {
  if (!argumentsValue || typeof argumentsValue !== "object") return "";
  const entries = Object.entries(argumentsValue).filter(([, value]) => value !== "");
  if (!entries.length) return "";
  const text = entries
    .map(([name, value]) => `${name.replaceAll("_", " ")}: ${Array.isArray(value) ? value.join(", ") : value}`)
    .join(" | ");
  return `<div><small>${escapeHtml(text)}</small></div>`;
}

function renderExecutionProfileComparison(comparison) {
  if (!comparison) return "";
  const status = String(comparison.status || "legacy");
  const tagClass = status === "matched" ? "ok" : "warn";
  const differences = Array.isArray(comparison.differences)
    ? comparison.differences
    : [];
  const rows = differences.map((difference) => {
    const field = String(difference.field || "setting").replaceAll("_", " ");
    return `<div class="timeline-event">
      <div class="timeline-step">${escapeHtml(field)}</div>
      <div><small>Saved</small> ${escapeHtml(formatExecutionProfileValue(difference.field, difference.saved))}</div>
      <div><small>Current</small> ${escapeHtml(formatExecutionProfileValue(difference.field, difference.current))}</div>
    </div>`;
  }).join("");
  return `
    <div class="timeline-event">
      <div class="timeline-step">Execution profile <span class="tag ${tagClass}">${escapeHtml(status)}</span></div>
      <div class="timeline-status">${escapeHtml(formatTime(comparison.compared_at))}</div>
      <div>${escapeHtml(comparison.summary || "")}</div>
    </div>
    ${rows}
  `;
}

function formatExecutionProfileValue(field, value) {
  if (field === "endpoint_fingerprint" && value) {
    return `${String(value).slice(0, 12)}...`;
  }
  if (value === null || value === undefined || value === "") return "not set";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderTaskRunCheckpoints(taskRun) {
  const checkpoints = Array.isArray(taskRun.checkpoints) ? taskRun.checkpoints : [];
  const latest = taskRun.latest_checkpoint || checkpoints[checkpoints.length - 1];
  $("taskRunLatestCheckpoint").innerHTML = latest
    ? renderTaskRunCheckpoint(latest, true)
    : item("No execution checkpoint has been recorded.");
  $("taskRunCheckpoints").innerHTML = checkpoints.length
    ? checkpoints
        .slice(-20)
        .reverse()
        .map((checkpoint) => renderTaskRunCheckpoint(checkpoint, false))
        .join("")
    : item("No checkpoint history yet.");
}

function renderTaskRunCheckpoint(checkpoint, compact) {
  const sequence = Number(checkpoint.sequence || 0);
  const phase = String(checkpoint.phase || "unknown").replaceAll("_", " ");
  const status = String(checkpoint.status || "unknown").replaceAll("_", " ");
  const nextAction = String(checkpoint.next_action || "none").replaceAll("_", " ");
  const usage = checkpoint.execution_usage || {};
  const remaining = checkpoint.execution_remaining || {};
  const budget = `Steps ${usage.agent_steps || 0} used / ${remaining.agent_steps ?? "n/a"} left | `
    + `tools ${usage.tool_calls || 0} used / ${remaining.tool_calls ?? "n/a"} left | `
    + `validation ${usage.validation_commands || 0} used / ${remaining.validation_commands ?? "n/a"} left`;
  const references = [
    checkpoint.proposal_id ? `Proposal ${checkpoint.proposal_id}` : "",
    checkpoint.sandbox_path ? `Sandbox ${checkpoint.sandbox_path}` : "",
    Number(checkpoint.repair_attempt || 0) > 0 ? `Repair attempt ${checkpoint.repair_attempt}` : "",
  ].filter(Boolean);
  return `<div class="${compact ? "item" : "timeline-event"}">
    <div class="timeline-step">#${escapeHtml(sequence)} ${escapeHtml(phase)}</div>
    <div class="timeline-status">${escapeHtml(status)} | ${escapeHtml(formatTime(checkpoint.created_at))}</div>
    <div>${escapeHtml(checkpoint.detail || "No detail recorded.")}</div>
    <small>Next: ${escapeHtml(nextAction)}</small>
    <small>${escapeHtml(budget)}</small>
    ${references.length ? `<small>${escapeHtml(references.join(" | "))}</small>` : ""}
  </div>`;
}

function renderTaskRunInterruption(taskRun) {
  const notice = $("taskRunInterruption");
  if (taskRun.status !== "interrupted") {
    notice.hidden = true;
    notice.innerHTML = "";
    return;
  }
  const previous = String(taskRun.interrupted_from || "unknown").replaceAll("_", " ");
  const reason = String(taskRun.interruption_reason || "server restart").replaceAll("_", " ");
  const detected = formatTime(taskRun.interrupted_at) || "unknown time";
  const checkpoint = String(taskRun.resume_checkpoint || "blocked").replaceAll("_", " ");
  notice.innerHTML = `
    <strong>Execution interrupted</strong>
    <span>Previous state: ${escapeHtml(previous)} | Detected: ${escapeHtml(detected)} | Reason: ${escapeHtml(reason)}</span>
    <span>Resume checkpoint: ${escapeHtml(checkpoint)}</span>
    <span>No work resumed automatically. Inspect the preserved sandbox before choosing Resume or Cancel.</span>
  `;
  notice.hidden = false;
}

function renderTaskRunResumePlan(taskRun) {
  const line = $("taskRunResumeLine");
  const plan = taskRun.resume_plan || {};
  if (!taskRun.can_resume && !taskRun.resume_blocked_reason) {
    line.textContent = "";
    return;
  }
  if (taskRun.resume_blocked_reason) {
    line.textContent = `Resume blocked: ${taskRun.resume_blocked_reason}`;
    return;
  }
  const checkpoint = String(plan.checkpoint || taskRun.resume_checkpoint || "unknown").replaceAll("_", " ");
  const requirement = plan.requires_clean_sandbox ? " Clean sandbox required." : "";
  line.textContent = `Resume checkpoint: ${checkpoint}.${requirement}`;
}

function renderTaskRunPhases(taskRun) {
  const phaseIndex = taskRunPhaseIndex(taskRun);
  const completed = taskRun.status === "completed";
  const warning = ["failed", "cancelled", "repair_pending", "interrupted"].includes(taskRun.status);
  $("taskRunPhases").innerHTML = TASK_RUN_PHASES.map((phase, index) => {
    let className = "task-run-phase";
    if (completed || index < phaseIndex) className += " done";
    else if (index === phaseIndex) className += warning ? " warning" : " active";
    return `<div class="${className}">${index + 1}. ${escapeHtml(phase)}</div>`;
  }).join("");
}

function taskRunPhaseIndex(taskRun) {
  const status = taskRun.status === "paused" ? taskRun.resume_status : taskRun.status;
  const runtimeActionKind = taskRun.result?.agent_pending_approval?.action_kind;
  if (["queued", "creating_sandbox"].includes(status)) return 0;
  if (status === "failed" && taskRun.repair_stop_reason) return 4;
  if (["exploring", "pausing", "cancelling", "interrupted", "failed"].includes(status)) return 1;
  if (status === "awaiting_input") {
    const resumePhase = taskRun.result?.agent_pending_input?.resume_phase;
    return resumePhase === "validation" ? 4 : 1;
  }
  if (status === "awaiting_approval") return runtimeActionKind === "validate" ? 4 : 2;
  if (status === "review_pending" && taskRun.result?.agent_validation_cycle) return 4;
  if (["applying", "review_pending"].includes(status)) return 3;
  if (["validating", "diagnosing", "replanning", "repair_pending"].includes(status)) return 4;
  return 5;
}

function taskRunControlPayload() {
  return {
    run_id: state.taskRun?.run_id,
    source_repo: state.taskRun?.source_repo,
  };
}

function currentRuntimeInput(taskRun = state.taskRun) {
  const request = taskRun?.result?.agent_pending_input;
  return request?.checkpoint ? request : null;
}

function renderTaskRunInput(taskRun) {
  const section = $("taskRunInput");
  const request = currentRuntimeInput(taskRun);
  const userInputs = Array.isArray(taskRun.result?.agent_state?.user_inputs)
    ? taskRun.result.agent_state.user_inputs
    : [];
  section.hidden = !request && userInputs.length === 0;
  if (section.hidden) {
    $("taskRunInputRequest").innerHTML = "";
    $("submitTaskRunInput").disabled = true;
    $("taskRunInputAnswer").disabled = true;
    $("taskRunInputStatus").textContent = "";
    return;
  }

  const answeredRows = userInputs.slice(-3).reverse().map((value) => `
    <div class="timeline-event">
      <div class="timeline-step">Answered</div>
      <div class="timeline-status">not evidence</div>
      <div>
        <strong>${escapeHtml(value.question || "Agent question")}</strong>
        <p>${escapeHtml(value.answer || "")}</p>
        <small>Action ${escapeHtml(value.action_id || "unknown")} | ${escapeHtml(formatTime(value.answered_at))}</small>
      </div>
    </div>
  `).join("");
  const pendingRow = request ? `
    <div class="timeline-event">
      <div class="timeline-step">Pending question</div>
      <div class="timeline-status">${escapeHtml(request.input_type || "text")}</div>
      <div>
        <strong>${escapeHtml(request.question || "Agent input required")}</strong>
        <p><small>Checkpoint ${escapeHtml(request.checkpoint)} | Action ${escapeHtml(request.action_id || "unknown")}</small></p>
        <p><small>Question SHA-256 ${escapeHtml(request.question_hash || "unknown")}</small></p>
      </div>
    </div>
  ` : "";
  $("taskRunInputRequest").innerHTML = pendingRow + answeredRows;
  const available = Boolean(request && taskRun.can_answer_input);
  $("taskRunInputAnswer").disabled = !available;
  $("submitTaskRunInput").disabled = !available;
  $("taskRunInputStatus").textContent = available
    ? "The answer will continue this exact Runtime run."
    : "No Agent question is currently waiting for an answer.";
}

async function submitTaskRunInput() {
  const request = currentRuntimeInput();
  const answer = $("taskRunInputAnswer").value.trim();
  if (!request || !state.taskRun?.can_answer_input) {
    setStatus("No exact Agent question is waiting for an answer.");
    return;
  }
  if (!answer) {
    setStatus("Answer is required.");
    return;
  }

  $("submitTaskRunInput").disabled = true;
  $("taskRunInputStatus").textContent = "Saving answer and continuing the Agent...";
  setStatus("Continuing the sandboxed Agent run...");
  try {
    const data = await postJson("/api/task-runs/runtime-input/answer", {
      ...taskRunControlPayload(),
      ...buildRepairAutomationPayload(),
      checkpoint: request.checkpoint,
      action_id: request.action_id,
      question_hash: request.question_hash,
      answer,
    });
    if (data.error) {
      if (data.task_run) updateTaskRun(data.task_run);
      throw new Error(data.error);
    }
    $("taskRunInputAnswer").value = "";
    updateTaskRun(data.task_run);
    setStatus(data.task_run?.message || "Agent answer accepted.");
    if (!["awaiting_input", "awaiting_approval", "review_pending", "completed", "failed", "cancelled"].includes(data.task_run?.status)) {
      startTaskRunPolling();
    }
  } catch (error) {
    $("submitTaskRunInput").disabled = !state.taskRun?.can_answer_input;
    $("taskRunInputStatus").textContent = `Answer failed: ${error.message}`;
    setStatus(`Error: ${error.message}`);
  }
}

function taskRunLinkPayload() {
  if (!state.taskRun?.run_id) return {};
  return {
    task_run_id: state.taskRun.run_id,
    source_repo: state.taskRun.source_repo,
  };
}

function activateTab(name) {
  document.querySelector(`.tab[data-tab="${name}"]`)?.click();
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}

async function testLlmConnection() {
  setStatus("Testing LLM connection...");
  $("llmTestLine").textContent = "Testing model endpoint...";
  try {
    const data = await postJson("/api/llm/test", buildLlmPayload());
    if (data.error) {
      throw new Error(data.error);
    }
    const model = data.model || selectedModel() || "configured model";
    const preview = data.response_preview ? ` Response: ${data.response_preview}` : "";
    $("llmTestLine").textContent = `OK: ${model} responded.${preview}`;
    setStatus("LLM connection OK.");
  } catch (error) {
    $("llmTestLine").textContent = `LLM test failed: ${error.message}`;
    setStatus(`Error: ${error.message}`);
  }
}

async function applyProposal() {
  const proposal = state.lastReport?.patch_proposal;
  if (!state.proposalId || !proposal?.apply_ready) {
    setStatus("No apply-ready proposal is available.");
    return;
  }
  const approvedPaths = approvedFilePaths();
  if (!approvedPaths.length) {
    setStatus("Select at least one proposed file before applying.");
    return;
  }
  const confirmed = window.confirm(`Apply proposal ${state.proposalId} with ${approvedPaths.length} approved file edit(s) to the working tree?`);
  if (!confirmed) {
    return;
  }

  setStatus("Applying proposal...");
  if (state.taskRun) startTaskRunPolling();
  try {
    const result = await postJson("/api/apply", {
      ...buildRepositoryPayload(),
      ...taskRunLinkPayload(),
      ...buildRepairAutomationPayload(),
      proposal_id: state.proposalId,
      approved_paths: approvedPaths,
    });
    if (result.error) {
      if (result.safety_check) {
        $("proposalList").innerHTML += renderSafetyCheck(result.safety_check);
        renderTimeline(result.timeline || []);
      }
      throw new Error(result.error);
    }
    $("diffOutput").textContent = result.diff || "No diff.";
    $("validationList").innerHTML = renderValidation(result.validation || []);
    $("validationFeedbackList").innerHTML = renderValidationFeedback(result.validation_feedback, result);
    const autoRepairRunning = ["diagnosing", "replanning"].includes(result.task_run?.status);
    state.repairParentId = result.validation_feedback && !result.repair_budget_exhausted
      && !result.repair_stop_reason && !autoRepairRunning ? state.proposalId : null;
    state.rollbackAvailable = Boolean(result.rollback_available);
    state.proposalApplied = true;
    $("generateRepairProposal").disabled = !state.repairParentId;
    $("revertProposal").disabled = !state.rollbackAvailable;
    $("rollbackStatus").textContent = state.rollbackAvailable
      ? "Rollback snapshot available for this applied proposal."
      : "No rollback snapshot available.";
    renderTimeline(result.timeline || []);
    if (result.task_run) updateTaskRun(result.task_run);
    setApprovalInputsDisabled(true);
    updateApprovalState();
    setStatus(result.message || "Proposal applied.");
    await loadDiff(false);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function revertProposal() {
  if (!state.proposalId || !state.rollbackAvailable) {
    setStatus("No applied proposal can be reverted.");
    return;
  }
  const confirmed = window.confirm(`Revert applied proposal ${state.proposalId} from its rollback snapshot?`);
  if (!confirmed) {
    return;
  }

  setStatus("Reverting applied proposal...");
  try {
    const result = await postJson("/api/revert", {
      ...buildRepositoryPayload(),
      ...taskRunLinkPayload(),
      proposal_id: state.proposalId,
    });
    if (result.error) {
      if (result.timeline) {
        renderTimeline(result.timeline);
      }
      throw new Error(result.error);
    }
    $("diffOutput").textContent = result.diff || "No diff.";
    state.rollbackAvailable = Boolean(result.rollback_available);
    state.proposalApplied = false;
    state.repairParentId = null;
    $("revertProposal").disabled = true;
    $("generateRepairProposal").disabled = true;
    setApprovalInputsDisabled(false);
    updateApprovalState();
    $("rollbackStatus").textContent = "Applied proposal was reverted from its rollback snapshot.";
    $("validationFeedbackList").innerHTML = renderValidationFeedback(null);
    renderTimeline(result.timeline || []);
    if (result.task_run) updateTaskRun(result.task_run);
    setStatus(result.message || "Proposal reverted.");
    await loadDiff(false);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function generateRepairProposal() {
  if (!state.repairParentId) {
    setStatus("No validation feedback is available for repair.");
    return;
  }
  setStatus("Generating repair proposal...");
  if (state.taskRun) startTaskRunPolling();
  try {
    const report = await postJson("/api/repair/propose", {
      ...buildWorkflowPayload(),
      ...taskRunLinkPayload(),
      proposal_id: state.repairParentId,
    });
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, buildWorkflowPayload());
    if (report.task_run) updateTaskRun(report.task_run);
    setStatus("Repair proposal ready for review.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function buildWorkflowPayload() {
  const validation = $("validationInput").value.trim();
  return {
    repo: $("repoPath").value.trim() || ".",
    repo_source: $("repoSource").value,
    github_url: $("githubUrl").value.trim(),
    branch: $("repoBranch").value.trim(),
    task: $("taskInput").value.trim(),
    validation: validation ? [validation] : [],
    use_llm: $("useLlm").checked,
    ...buildLlmPayload(),
    no_llm_fallback: $("disableFallback").checked,
    use_memory: !$("disableMemory").checked,
    iterative_agent: $("iterativeAgent").checked,
    agent_max_steps: $("agentMaxSteps").value.trim(),
    agent_max_tool_calls: $("agentMaxToolCalls").value.trim(),
    max_validation_commands: $("maxValidationCommands").value.trim(),
    execution_timeout_seconds: $("executionTimeoutSeconds").value.trim(),
    max_repair_attempts: $("repairMaxAttempts").value.trim(),
    auto_repair: $("autoRepair").checked,
  };
}

function buildRepairAutomationPayload() {
  return {
    auto_repair: $("autoRepair").checked,
    use_llm: $("useLlm").checked,
    ...buildLlmPayload(),
    no_llm_fallback: $("disableFallback").checked,
    use_memory: !$("disableMemory").checked,
    iterative_agent: $("iterativeAgent").checked,
    agent_max_steps: $("agentMaxSteps").value.trim(),
  };
}

function buildLlmPayload() {
  return {
    model: selectedModel(),
    base_url: $("baseUrl").value.trim(),
    api_key: $("apiKey").value,
    timeout_seconds: $("timeoutSeconds").value.trim(),
  };
}

async function syncRepository() {
  setStatus("Syncing repository...");
  try {
    const data = await postJson("/api/repository/sync", buildRepositoryPayload());
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    setStatus(data.repository_source?.message || "Repository synced.");
    await Promise.allSettled([loadGithub(), loadDiff(false), loadHistory()]);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function createSandbox() {
  setStatus("Creating isolated worktree sandbox...");
  try {
    const data = await postJson("/api/sandbox/create", {
      ...buildRepositoryPayload(),
      ref: "HEAD",
    });
    if (data.error) {
      throw new Error(data.error);
    }
    state.sandboxes = data.sandboxes || [data.sandbox];
    renderSandboxOptions(data.sandbox?.path || "");
    activateSandbox(data.sandbox);
    setStatus("Worktree sandbox created and selected.");
    await Promise.allSettled([loadGithub(), loadDiff(false), loadHistory()]);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function refreshSandboxes() {
  const previousSandbox = state.sandbox;
  const params = state.sandbox
    ? new URLSearchParams({ repo: state.sandbox.source_repo, repo_source: "local" })
    : new URLSearchParams(repositoryQuery());
  const data = await getJson(`/api/sandbox/list?${params.toString()}`);
  if (data.error) {
    throw new Error(data.error);
  }
  state.sandboxes = data.sandboxes || [];
  const currentPath = state.sandbox?.path || $("repoPath").value.trim();
  const selected = state.sandboxes.find((sandbox) => sandbox.path === currentPath) || null;
  state.sandbox = selected;
  if (previousSandbox && !selected) {
    restoreSandboxSource(previousSandbox);
    resetProposalForRepositoryChange();
  }
  renderSandboxOptions(selected?.path || "");
  renderSandboxStatus(selected);
}

function selectSandbox() {
  const selectedPath = $("sandboxSelect").value;
  const sandbox = state.sandboxes.find((item) => item.path === selectedPath) || null;
  if (!sandbox) {
    const previousSandbox = state.sandbox;
    state.sandbox = null;
    if (previousSandbox) {
      restoreSandboxSource(previousSandbox);
    }
    renderSandboxStatus(null);
    resetProposalForRepositoryChange();
    return;
  }
  activateSandbox(sandbox);
  setStatus("Worktree sandbox selected.");
}

function activateSandbox(sandbox) {
  if (!sandbox) {
    return;
  }
  state.sandbox = sandbox;
  $("repoSource").value = "local";
  $("repoPath").value = sandbox.path;
  $("repoBranch").value = "";
  updateRepositorySourceUi();
  renderSandboxStatus(sandbox);
  resetProposalForRepositoryChange();
}

function restoreSandboxSource(sandbox) {
  $("repoSource").value = "local";
  $("repoPath").value = sandbox.source_repo;
  $("repoBranch").value = "";
  updateRepositorySourceUi();
}

async function removeSandbox() {
  const sandbox = state.sandbox;
  if (!sandbox) {
    setStatus("Select a managed sandbox first.");
    return;
  }
  const confirmed = window.confirm(`Remove worktree sandbox ${sandbox.path}?`);
  if (!confirmed) {
    return;
  }

  setStatus("Removing worktree sandbox...");
  try {
    let data = await postJson("/api/sandbox/remove", {
      source_repo: sandbox.source_repo,
      path: sandbox.path,
      confirm_remove: true,
      force: false,
    });
    if (data.error && data.dirty) {
      const discard = window.confirm(
        "This sandbox has uncommitted changes. Permanently discard them and remove the sandbox?"
      );
      if (!discard) {
        setStatus("Sandbox removal cancelled; changes were preserved.");
        return;
      }
      data = await postJson("/api/sandbox/remove", {
        source_repo: sandbox.source_repo,
        path: sandbox.path,
        confirm_remove: true,
        force: true,
      });
    }
    if (data.error) {
      throw new Error(data.error);
    }

    const sourceRepo = data.removed?.source_repo || sandbox.source_repo;
    state.sandbox = null;
    state.sandboxes = data.sandboxes || [];
    $("repoSource").value = "local";
    $("repoPath").value = sourceRepo;
    $("repoBranch").value = "";
    updateRepositorySourceUi();
    renderSandboxOptions("");
    renderSandboxStatus(null);
    resetProposalForRepositoryChange();
    setStatus("Worktree sandbox removed.");
    await Promise.allSettled([loadGithub(), loadDiff(false), loadHistory()]);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function renderSandboxOptions(selectedPath) {
  const select = $("sandboxSelect");
  select.replaceChildren();
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = "No managed sandbox";
  select.appendChild(empty);
  state.sandboxes.forEach((sandbox) => {
    const option = document.createElement("option");
    option.value = sandbox.path;
    const name = sandbox.path.split(/[\\/]/).pop() || sandbox.path;
    const stateLabel = sandbox.clean == null ? "unknown" : sandbox.clean ? "clean" : "changes";
    option.textContent = `${name} | ${String(sandbox.head || "").slice(0, 8)} | ${stateLabel}`;
    select.appendChild(option);
  });
  select.value = selectedPath || "";
}

function renderSandboxStatus(sandbox) {
  $("removeSandbox").disabled = !sandbox;
  if (!sandbox) {
    $("sandboxLine").textContent = "No managed worktree sandbox selected.";
    return;
  }
  const stateLabel = sandbox.clean == null ? "state unknown" : sandbox.clean ? "clean" : "local changes present";
  $("sandboxLine").textContent = `Sandbox: ${sandbox.path}. Detached at ${String(
    sandbox.head || "unknown"
  ).slice(0, 12)}; ${stateLabel}.`;
}

function resetProposalForRepositoryChange() {
  state.lastReport = null;
  state.proposalId = null;
  state.repairParentId = null;
  state.rollbackAvailable = false;
  state.proposalApplied = false;
  state.approvedPaths = new Set();
  $("applyProposal").disabled = true;
  $("revertProposal").disabled = true;
  $("generateRepairProposal").disabled = true;
  updateApprovalState();
}

async function loadGithub() {
  $("githubContent").innerHTML = item("Loading GitHub status...");
  const data = await getJson(`/api/github/status?${repositoryQuery()}&limit=8`);
  state.github = data;
  renderGithub(data);
}

async function loadDiff(staged) {
  const data = await getJson(`/api/git/diff?${repositoryQuery()}&staged=${staged ? "true" : "false"}`);
  $("diffOutput").textContent = data.diff || data.error || "No diff.";
  updateRepositorySourceStatus(data.repository_source);
}

async function generateDelivery() {
  setStatus("Generating delivery draft...");
  try {
    const data = await postJson("/api/git/summary", {
      ...buildRepositoryPayload(),
      validation_notes: buildValidationNotes(),
    });
    if (data.error) {
      throw new Error(data.error);
    }
    state.delivery = data;
    renderDelivery(data);
    setStatus("Delivery draft ready.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function loadPrReadiness() {
  setStatus("Checking PR readiness...");
  try {
    const data = await postJson("/api/github/pr/readiness", buildRepositoryPayload());
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    $("prReadinessContent").innerHTML = renderPrReadiness(data.pr_readiness);
    updateCreatePullRequestState(data.pr_readiness);
    setStatus(data.pr_readiness?.ready ? "PR readiness passed." : "PR readiness needs attention.");
  } catch (error) {
    $("prReadinessContent").innerHTML = item(`PR readiness unavailable: ${escapeHtml(error.message)}`);
    setStatus(`Error: ${error.message}`);
  }
}

async function createPullRequest() {
  const delivery = state.delivery;
  const readiness = delivery?.pr_readiness;
  const draft = delivery?.pull_request;
  if (!readiness?.ready || !draft?.title || !draft?.body) {
    setStatus("Generate a ready PR draft before creating a pull request.");
    return;
  }
  const confirmed = window.confirm(`Create a GitHub pull request from ${readiness.head_branch} to ${readiness.base_branch}?`);
  if (!confirmed) {
    return;
  }
  setStatus("Creating pull request...");
  try {
    const data = await postJson("/api/github/pr/create", {
      ...buildRepositoryPayload(),
      confirm_create: true,
      title: draft.title,
      body: draft.body,
      base_branch: readiness.base_branch,
    });
    if (data.error) {
      if (data.pr_readiness) {
        $("prReadinessContent").innerHTML = renderPrReadiness(data.pr_readiness);
        updateCreatePullRequestState(data.pr_readiness);
      }
      throw new Error(data.error);
    }
    $("prReadinessContent").innerHTML = renderPrCreated(data.pull_request) + renderPrReadiness(data.pr_readiness);
    updateCreatePullRequestState(null);
    setStatus(`Pull request created: ${data.pull_request?.html_url || data.pull_request?.number || "done"}.`);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function loadHistory() {
  $("historyContent").innerHTML = item("Loading history...");
  try {
    const data = await getJson(`/api/history?${repositoryQuery()}&limit=20`);
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    renderHistory(data.runs || []);
  } catch (error) {
    $("historyContent").innerHTML = item(`History unavailable: ${escapeHtml(error.message)}`);
  }
}

async function loadHistoryDetail(runId) {
  $("historyDetail").innerHTML = item("Loading run detail...");
  try {
    const data = await getJson(`/api/history/run?${repositoryQuery()}&id=${encodeURIComponent(runId)}`);
    if (data.error) {
      throw new Error(data.error);
    }
    renderHistoryDetail(data);
  } catch (error) {
    $("historyDetail").innerHTML = item(`Run detail unavailable: ${escapeHtml(error.message)}`);
  }
}

async function deleteHistoryRun(runId) {
  const confirmed = window.confirm("Delete this saved run from local memory?");
  if (!confirmed) {
    return;
  }
  setStatus("Deleting saved run...");
  try {
    const data = await postJson("/api/history/delete", {
      ...buildRepositoryPayload(),
      id: runId,
    });
    if (data.error) {
      throw new Error(data.error);
    }
    $("historyDetail").innerHTML = "";
    await loadHistory();
    setStatus("Saved run deleted.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function toggleHistoryPin(runId, pinned) {
  setStatus(pinned ? "Pinning saved run..." : "Unpinning saved run...");
  try {
    const data = await postJson("/api/history/pin", {
      ...buildRepositoryPayload(),
      id: runId,
      pinned,
    });
    if (data.error) {
      throw new Error(data.error);
    }
    await loadHistory();
    setStatus(pinned ? "Saved run pinned." : "Saved run unpinned.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function clearHistory() {
  const confirmed = window.confirm("Clear all saved runs for this repository?");
  if (!confirmed) {
    return;
  }
  setStatus("Clearing history...");
  try {
    const data = await postJson("/api/history/clear", buildRepositoryPayload());
    if (data.error) {
      throw new Error(data.error);
    }
    $("historyDetail").innerHTML = "";
    await loadHistory();
    setStatus(`Cleared ${data.deleted || 0} saved run(s).`);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function renderReport(report, payload) {
  state.proposalId = report.proposal_id || null;
  const repairInProgress = ["diagnosing", "replanning"].includes(report.task_run?.status);
  state.repairParentId = report.validation_feedback && state.proposalId
    && !report.repair_budget_exhausted && !report.repair_stop_reason && !repairInProgress
    ? state.proposalId : null;
  state.rollbackAvailable = Boolean(report.rollback_available);
  state.proposalApplied = false;
  state.approvedPaths = new Set(editableProposalPaths(report.patch_proposal));
  updateRepositorySourceStatus(report.repository_source);
  $("filesScanned").textContent = report.files_scanned;
  $("symbolsIndexed").textContent = report.repository_map?.symbols_indexed || 0;
  $("planSource").textContent = sourceLabel(report.plan_metadata);
  $("proposalSource").textContent = sourceLabel(report.patch_proposal_metadata);
  renderTimeline(report.timeline || []);
  $("agentStepList").innerHTML = renderAgentSteps(report.agent_steps || []);
  $("agentWorkingState").innerHTML = renderAgentWorkingState(
    report.agent_state,
    report.agent_stop_reason,
    report.agent_pending_question,
    report.agent_completion_ready,
    report.agent_completion_blockers,
    report.agent_proposed_edits,
    report.agent_proposed_diff
  );
  $("runtimeEventList").innerHTML = renderRuntimeEvents(report.agent_events || [], report.agent_run_id);
  const runtimeApproval = report.agent_pending_approval?.checkpoint
    ? report.agent_pending_approval
    : pendingApprovalFromEvents(report.agent_events || []);
  $("runtimeApproval").innerHTML = renderRuntimeApproval(runtimeApproval);
  updateRuntimeApprovalControls(runtimeApproval);
  $("runtimeWriteResult").innerHTML = renderRuntimeWriteResult(report.agent_write_result);
  $("agentValidationCycle").innerHTML = renderAgentValidationCycle(
    report.agent_validation_cycle,
    report.agent_validation_results
  );
  $("repositoryMapList").innerHTML = renderRepositoryMap(report.repository_map);
  $("acceptanceCriteriaList").innerHTML = renderAcceptanceCriteria(
    report.acceptance_criteria || [],
    report.completion_evidence
  );
  $("executionBudgetList").innerHTML = renderExecutionBudget(report.execution_budget);
  $("completionEvidenceList").innerHTML = renderCompletionEvidence(report.completion_evidence);
  $("repairLoopList").innerHTML = renderRepairLoop(report);
  $("planList").innerHTML = report.plan.map((step) => `<li class="item"><div class="item-title">${escapeHtml(step.title)}</div>${escapeHtml(step.detail)}</li>`).join("");
  $("proposalList").innerHTML = renderMemoryContext(report.memory_context || []) + renderProposals(report.patch_proposal);
  $("proposalOutput").textContent = JSON.stringify(
    {
      memory_context: report.memory_context,
      metadata: report.patch_proposal_metadata,
      proposal: report.patch_proposal,
    },
    null,
    2
  );
  $("proposedDiffOutput").textContent = report.patch_proposal?.proposed_diff || "No proposed diff. Use LLM proposal generation for apply-ready edits.";
  updateApprovalState();
  $("revertProposal").disabled = !state.rollbackAvailable;
  $("rollbackStatus").textContent = state.proposalId
    ? "Proposal is stored server-side; rollback becomes available after apply."
    : "No rollback snapshot available.";
  $("validationList").innerHTML = renderValidation(report.validation);
  $("validationFeedbackList").innerHTML = renderValidationFeedback(report.validation_feedback, report);
  $("generateRepairProposal").disabled = !state.repairParentId;
  $("llmInput").textContent = buildLlmInputPreview(report, payload);
  $("llmOutput").textContent = buildLlmOutputPreview(report);
  $("llmReview").textContent = JSON.stringify(report.patch_review || {}, null, 2);
  $("llmTraceList").innerHTML = renderLlmTraces(report.llm_traces || []);
  $("jsonOutput").textContent = JSON.stringify(report, null, 2);
  loadHistory().catch(() => {});
}

function renderRepositoryMap(repositoryMap) {
  if (!repositoryMap || !repositoryMap.files_indexed) {
    return item("No repository map was generated for this run.");
  }
  const metrics = `<div class="item">
    <div class="item-title">Indexed Structure</div>
    <p>${escapeHtml(repositoryMap.files_indexed)} files, ${escapeHtml(repositoryMap.symbols_indexed || 0)} symbols, ${escapeHtml(repositoryMap.relations_indexed || 0)} relations.</p>
    ${repositoryMap.parse_errors ? `<p><small>${escapeHtml(repositoryMap.parse_errors)} file(s) could not be parsed completely.</small></p>` : ""}
  </div>`;
  const entries = (repositoryMap.relevant_entries || []).map((entry) => {
    const symbols = (entry.symbols || []).map((symbol) => `<li><code>${escapeHtml(symbol)}</code></li>`).join("");
    const related = (entry.related_paths || []).map((path) => `<li>${escapeHtml(path)}</li>`).join("");
    const reasons = (entry.reasons || []).join("; ");
    return `<div class="item">
      <div class="item-title">${escapeHtml(entry.path)} <span class="tag">score ${escapeHtml(entry.score)}</span></div>
      <p><small>${escapeHtml(reasons || "Task-relevant repository structure")}</small></p>
      <strong>Symbols</strong>
      <ul>${symbols || "<li>No indexed symbols.</li>"}</ul>
      <strong>Related files</strong>
      <ul>${related || "<li>No resolved relations.</li>"}</ul>
    </div>`;
  }).join("");
  return metrics + (entries || item("No task-specific map entries were ranked."));
}

function renderAcceptanceCriteria(criteria, completionEvidence) {
  if (!criteria || criteria.length === 0) {
    return item("No explicit acceptance criteria were generated for this run.");
  }
  const evidenceById = new Map(
    (completionEvidence?.criteria || []).map((entry) => [entry.criterion_id, entry])
  );
  return criteria.map((criterion) => {
    const evidence = evidenceById.get(criterion.criterion_id);
    const status = evidence?.status || "pending";
    const tagClass = status === "passed" ? "ok" : status === "failed" ? "danger" : "warn";
    return `<div class="item">
      <div class="item-title">${escapeHtml(criterion.description)}
        <span class="tag">${escapeHtml(criterion.kind || "criterion")}</span>
        <span class="tag ${tagClass}">${escapeHtml(status)}</span>
      </div>
      <p><small>${criterion.required ? "Required" : "Advisory"}${evidence?.summary ? ` - ${escapeHtml(evidence.summary)}` : ""}</small></p>
    </div>`;
  }).join("");
}

function renderExecutionBudget(budgetState) {
  if (!budgetState?.limits || !budgetState?.usage) {
    return item("No execution budget data is available.");
  }
  const limits = budgetState.limits;
  const usage = budgetState.usage;
  const reasons = (budgetState.exhausted_reasons || [])
    .map((reason) => `<li>${escapeHtml(reason)}</li>`)
    .join("");
  return `<div class="item">
    <div class="item-title">Execution Budget
      <span class="tag ${budgetState.exhausted ? "danger" : "ok"}">${budgetState.exhausted ? "exhausted" : "within budget"}</span>
    </div>
    <p>Agent steps ${escapeHtml(usage.agent_steps)}/${escapeHtml(limits.max_agent_steps)}; tool calls ${escapeHtml(usage.tool_calls)}/${escapeHtml(limits.max_tool_calls)}.</p>
    <p>Validation ${escapeHtml(usage.validation_commands)}/${escapeHtml(limits.max_validation_commands)}; active time ${escapeHtml(Math.round((usage.elapsed_ms || 0) / 1000))}/${escapeHtml(limits.max_elapsed_seconds)} seconds.</p>
    ${reasons ? `<ul>${reasons}</ul>` : ""}
  </div>`;
}

function renderCompletionEvidence(evidence) {
  if (!evidence) {
    return item("Completion evidence has not been collected.");
  }
  const statusClass = evidence.status === "passed" ? "ok" : evidence.status === "failed" ? "danger" : "warn";
  const files = renderList(evidence.changed_files, "No changed files recorded.");
  const validation = renderList(evidence.validation_commands, "No automated validation command recorded.");
  return `<div class="item">
    <div class="item-title">${escapeHtml(evidence.summary || "Completion evidence")}
      <span class="tag ${statusClass}">${escapeHtml(evidence.status || "pending")}</span>
    </div>
    <p><small>Diff evidence: ${evidence.diff_available ? "available" : "not available"}</small></p>
    <strong>Changed files</strong><ul>${files}</ul>
    <strong>Validation commands</strong><ul>${validation}</ul>
  </div>`;
}

function renderMemoryContext(memory) {
  if (!memory || memory.length === 0) {
    return item("No related memory found for this task.");
  }
  const rows = memory
    .map((entry) => {
      const reasons = (entry.reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("");
      const validation = (entry.validation || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
      return `<div class="item">
        <div class="item-title">${escapeHtml(entry.task)}
          <span class="tag">${escapeHtml(entry.mode)}</span>
          ${entry.pinned ? '<span class="tag ok">pinned</span>' : ""}
          <span class="tag ${entry.applied ? "ok" : "warn"}">${entry.applied ? "applied" : "open"}</span>
          <span class="tag">score ${escapeHtml(entry.score)}</span>
        </div>
        <p>${escapeHtml(entry.summary || "")}</p>
        <p><small>${escapeHtml(entry.created_at || "")}</small></p>
        <strong>Why matched</strong>
        <ul>${reasons || "<li>No match reason saved.</li>"}</ul>
        <strong>Saved validation</strong>
        <ul>${validation || "<li>No validation saved.</li>"}</ul>
      </div>`;
    })
    .join("");
  return `<div class="item"><div class="item-title">Related Memory</div><p>${memory.length} previous run(s) may help this task.</p></div>${rows}`;
}

function renderAgentSteps(steps) {
  if (!steps || steps.length === 0) {
    return item("Iterative agent was not run for this workflow.");
  }
  return steps
    .map((step) => {
      const update = step.state_update || {};
      const stateChanges = [
        update.focus ? `focus: ${update.focus}` : "",
        ...(update.add_findings || []).map((value) => `finding +: ${value}`),
        ...(update.add_open_questions || []).map((value) => `question +: ${value}`),
        ...(update.resolve_open_questions || []).map((value) => `question resolved: ${value}`),
        ...(update.plan_updates || []).map((value) => `plan ${value.step_id}: ${value.status}`),
        ...(update.acceptance_updates || []).map((value) => (
          `acceptance ${value.criterion_id}: ${(value.evidence_action_ids || []).length ? "evidence attached" : "pending"}`
        )),
      ].filter(Boolean);
      return `<div class="item">
      <div class="item-title">Step ${escapeHtml(step.order)}: ${escapeHtml(step.action)}</div>
      <p>${escapeHtml(step.thought || "")}</p>
      <p><small>Input: ${escapeHtml(step.tool_input || "(none)")}</small></p>
      <p><small>Expected evidence: ${escapeHtml(step.expected_evidence || "(none)")}</small></p>
      ${stateChanges.length ? `<p><small>State update: ${escapeHtml(stateChanges.join(" | "))}</small></p>` : ""}
      <pre>${escapeHtml(step.observation || "")}</pre>
      ${(step.selected_paths || []).length ? `<p><small>Selected: ${escapeHtml(step.selected_paths.join(", "))}</small></p>` : ""}
      ${step.finish_reason ? `<p><small>Finish reason: ${escapeHtml(step.finish_reason)}</small></p>` : ""}
      ${step.user_question ? `<p><small>User question: ${escapeHtml(step.user_question)}</small></p>` : ""}
    </div>`;
    })
    .join("");
}

function renderAgentWorkingState(
  agentState,
  stopReason = "",
  pendingQuestion = "",
  completionReady = null,
  completionBlockers = [],
  proposedEdits = [],
  proposedDiff = ""
) {
  if (!agentState || !agentState.objective) {
    return item("No Agent working state was recorded for this workflow.");
  }
  const resolvedStopReason = stopReason || agentState.stop_reason || "";
  const selectedPaths = Array.isArray(agentState.selected_paths)
    ? agentState.selected_paths
    : [];
  const observations = Array.isArray(agentState.recent_observations)
    ? agentState.recent_observations
    : [];
  const findings = Array.isArray(agentState.findings) ? agentState.findings : [];
  const openQuestions = Array.isArray(agentState.open_questions)
    ? agentState.open_questions
    : [];
  const plan = Array.isArray(agentState.plan) ? agentState.plan : [];
  const acceptance = Array.isArray(agentState.acceptance_criteria)
    ? agentState.acceptance_criteria
    : [];
  const userInputs = Array.isArray(agentState.user_inputs)
    ? agentState.user_inputs
    : [];
  const virtualEdits = Array.isArray(proposedEdits) && proposedEdits.length
    ? proposedEdits
    : Array.isArray(agentState.proposed_edits) ? agentState.proposed_edits : [];
  const inferredBlockers = [
    ...plan
      .filter((value) => value.status !== "completed")
      .map((value) => `plan:${value.step_id}`),
    ...acceptance
      .filter((value) => value.required !== false && value.status !== "passed")
      .map((value) => `acceptance:${value.criterion_id}`),
    ...virtualEdits
      .filter((value) => value.status === "conflict" || !value.inspected)
      .map((value) => `proposal:${value.path}:${value.status === "conflict" ? "conflict" : "uninspected"}`),
  ];
  const resolvedBlockers = Array.isArray(completionBlockers) && completionBlockers.length
    ? completionBlockers
    : inferredBlockers;
  const resolvedCompletionReady = typeof completionReady === "boolean"
    ? completionReady
    : plan.length > 0 && acceptance.length > 0 && resolvedBlockers.length === 0;
  const planRows = plan.map((value) => `
    <div class="timeline-event">
      <span class="timeline-step">Plan ${escapeHtml(value.step_id || "step")}</span>
      <span class="timeline-status">${escapeHtml(value.status || "pending")}</span>
      <span>${escapeHtml(value.title || "Untitled step")}${value.evidence_action_ids?.length ? ` | evidence ${escapeHtml(value.evidence_action_ids.join(", "))}` : ""}</span>
    </div>
  `).join("");
  const acceptanceRows = acceptance.map((value) => `
    <div class="timeline-event">
      <span class="timeline-step">Acceptance ${escapeHtml(value.criterion_id || "criterion")}</span>
      <span class="timeline-status">${escapeHtml(value.status || "pending")}</span>
      <span>${escapeHtml(value.description || "No description")}${value.evidence_action_ids?.length ? ` | evidence ${escapeHtml(value.evidence_action_ids.join(", "))}` : ""}</span>
    </div>
  `).join("");
  const proposalRows = virtualEdits.map((value) => `
    <div class="timeline-event">
      <span class="timeline-step">Virtual edit ${escapeHtml(value.path || "file")}</span>
      <span class="timeline-status">revision ${escapeHtml(value.revision ?? 0)} | ${escapeHtml(value.status || "proposed")}</span>
      <span>${value.inspected ? "Inspected" : "Inspection required"} | ${escapeHtml(value.hunk_count ?? 0)} cumulative hunk(s) | SHA-256 ${escapeHtml(value.current_sha256 || "unknown")}</span>
    </div>
  `).join("");
  const observationRows = observations.map((observation) => `
    <div class="timeline-event">
      <span class="timeline-step">#${escapeHtml(observation.iteration ?? 0)} ${escapeHtml(observation.action_id || "action")} ${escapeHtml(observation.action_kind || "action")}</span>
      <span class="timeline-status">${escapeHtml(observation.status || "unknown")}</span>
      <span>${escapeHtml(observation.summary || "No summary recorded.")}</span>
    </div>
  `).join("");
  const userInputRows = userInputs.map((value) => `
    <div class="timeline-event">
      <span class="timeline-step">User input</span>
      <span class="timeline-status">not evidence</span>
      <span><strong>${escapeHtml(value.question || "Agent question")}</strong><br>${escapeHtml(value.answer || "")}</span>
    </div>
  `).join("");
  return `
    <div class="timeline-event">
      <span class="timeline-step">${escapeHtml(agentState.phase || "unknown")}</span>
      <span class="timeline-status">${escapeHtml(agentState.status || "unknown")}</span>
      <span>Iteration ${escapeHtml(agentState.iteration ?? 0)}${resolvedStopReason ? ` | ${escapeHtml(resolvedStopReason)}` : ""}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Objective</span>
      <span>${escapeHtml(agentState.objective)}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Focus</span>
      <span>${escapeHtml(agentState.focus || "none")}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Selected paths</span>
      <span>${escapeHtml(selectedPaths.join(", ") || "none")}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Findings</span>
      <span>${escapeHtml(findings.join(" | ") || "none")}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Open questions</span>
      <span>${escapeHtml(openQuestions.join(" | ") || "none")}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Expected evidence</span>
      <span>${escapeHtml(agentState.expected_evidence || "none")}</span>
    </div>
    <div class="timeline-event">
      <span class="timeline-step">Completion ready</span>
      <span class="timeline-status">${resolvedCompletionReady ? "yes" : "no"}</span>
      <span>${escapeHtml(resolvedBlockers.join(" | ") || "No blockers")}</span>
    </div>
    ${planRows}
    ${acceptanceRows}
    ${proposalRows}
    ${proposedDiff ? `<div class="timeline-event">
      <span class="timeline-step">Cumulative virtual diff</span>
      <pre>${escapeHtml(proposedDiff)}</pre>
    </div>` : ""}
    ${pendingQuestion ? `<div class="timeline-event">
      <span class="timeline-step">Pending question</span>
      <span>${escapeHtml(pendingQuestion)}</span>
    </div>` : ""}
    ${userInputRows}
    ${observationRows}
  `;
}

function renderRuntimeEvents(events, runId = "") {
  if (!events || events.length === 0) {
    return item("No typed runtime events were recorded for this workflow.");
  }
  const header = runId
    ? `<p class="runtime-run-id"><small>Run ${escapeHtml(runId)}</small></p>`
    : "";
  const rows = events
    .map((event) => {
      const observation = event.payload?.observation || {};
      const detail = observation.summary || event.payload?.summary || event.payload?.reason || "";
      return `<div class="timeline-event runtime-event">
        <span class="timeline-step">#${escapeHtml(event.sequence)}</span>
        <span class="timeline-status">${escapeHtml(event.event_type || "event")}</span>
        <span>${escapeHtml(event.action_id || detail || "runtime")}${event.action_id && detail ? ` - ${escapeHtml(detail)}` : ""}</span>
      </div>`;
    })
    .join("");
  return `${header}${rows}`;
}

function renderRuntimeApproval(request) {
  if (!request || !request.checkpoint) {
    return item("No runtime side effect is waiting for approval.");
  }
  const fileScope = (request.file_scope || []).map((path) => `<li>${escapeHtml(path)}</li>`).join("");
  const commandScope = (request.command_allowlist || []).map((command) => `<li><code>${escapeHtml(command)}</code></li>`).join("");
  return `<div class="item runtime-approval">
    <div class="item-title">Pending Runtime Approval: ${escapeHtml(request.action_kind || "action")} ${escapeHtml(request.action_id || "")}</div>
    <p><strong>Checkpoint</strong> <code>${escapeHtml(request.checkpoint)}</code></p>
    <p><strong>Payload SHA-256</strong> <code>${escapeHtml(request.payload_hash || "")}</code></p>
    ${fileScope ? `<strong>File scope</strong><ul>${fileScope}</ul>` : ""}
    ${commandScope ? `<strong>Command scope</strong><ul>${commandScope}</ul>` : ""}
    <details open>
      <summary>Exact action</summary>
      <pre>${escapeHtml(JSON.stringify(request.action || {}, null, 2))}</pre>
    </details>
    ${request.diff ? `<details open><summary>${request.diff_truncated ? "Bounded diff (truncated)" : "Exact diff"}</summary><pre>${escapeHtml(request.diff)}</pre></details>` : ""}
  </div>`;
}

function updateRuntimeApprovalControls(request = currentRuntimeApproval()) {
  const available = Boolean(request?.checkpoint && state.taskRun?.can_approve_runtime);
  $("approveRuntimeWrite").disabled = !available;
  $("rejectRuntimeWrite").disabled = !available;
  $("approveRuntimeWrite").textContent = request?.action_kind === "validate"
    ? "Run Exact Validation"
    : "Approve Exact Write";
  $("runtimeApprovalStatus").textContent = available
    ? request.action_kind === "validate"
      ? "Review the exact command before allowing it to run in the managed worktree."
      : "Review the exact action and diff before approving or rejecting it."
    : "No managed Runtime action is waiting.";
}

function renderRuntimeWriteResult(result) {
  if (!result || !result.write_observation) {
    return item("No approved managed-worktree write has executed.");
  }
  const observation = result.write_observation;
  const evidence = (observation.data?.write_evidence || []).map((entry) => `
    <div class="timeline-event">
      <span class="timeline-step">${escapeHtml(entry.path || "file")}</span>
      <span class="timeline-status">${escapeHtml(observation.status || result.status || "unknown")}</span>
      <span>Before ${escapeHtml(entry.before_sha256 || "missing")} | After ${escapeHtml(entry.after_sha256 || "missing")}</span>
    </div>
  `).join("");
  const diff = result.resulting_diff || observation.data?.resulting_diff || "";
  return `
    <div class="item runtime-write-result">
      <div class="item-title">Managed-worktree write ${escapeHtml(result.status || observation.status || "completed")}</div>
      <p><small>Action ${escapeHtml(result.action_id || observation.action_id || "unknown")} | Rollback snapshot ${result.rollback_available ? "recorded" : "not available"}</small></p>
      ${evidence || "<p>No file hash evidence was recorded.</p>"}
      <details open><summary>Resulting Git diff</summary><pre>${escapeHtml(diff || "No diff.")}</pre></details>
    </div>
  `;
}

function renderAgentValidationCycle(cycle, savedResults = []) {
  if (!cycle || !Array.isArray(cycle.commands) || cycle.commands.length === 0) {
    return item("No approval-gated Agent validation cycle has started.");
  }
  const results = Array.isArray(cycle.results) && cycle.results.length
    ? cycle.results
    : Array.isArray(savedResults) ? savedResults : [];
  const rows = cycle.commands.map((command, index) => {
    const result = results.find((entry) => Number(entry?.command_index) === index);
    const validation = result?.validation || result?.observation?.data || {};
    const status = result?.status || (index === cycle.next_index ? "awaiting approval" : "pending");
    const statusClass = status === "passed" ? "ok" : status === "failed" ? "danger" : "warn";
    const output = [
      validation.stdout ? `stdout\n${validation.stdout}` : "",
      validation.stderr ? `stderr\n${validation.stderr}` : "",
    ].filter(Boolean).join("\n\n");
    const truncation = validation.stdout_truncated || validation.stderr_truncated
      ? " | bounded output truncated"
      : "";
    return `<div class="item">
      <div class="item-title">${index + 1}. <code>${escapeHtml(command)}</code> <span class="tag ${statusClass}">${escapeHtml(status)}</span></div>
      ${result ? `<p><small>Exit ${escapeHtml(validation.exit_code ?? "n/a")}${escapeHtml(truncation)} | Evidence ${escapeHtml(result.observation?.action_id || "recorded")}</small></p>` : ""}
      ${output ? `<details><summary>Bounded command output</summary><pre>${escapeHtml(output)}</pre></details>` : ""}
    </div>`;
  }).join("");
  return `<div class="item">
    <div class="item-title">Cycle ${escapeHtml(cycle.cycle_id || "unknown")}</div>
    <p>${escapeHtml(Math.min(Number(cycle.next_index) || 0, cycle.commands.length))} of ${escapeHtml(cycle.commands.length)} command(s) observed.</p>
  </div>${rows}`;
}

function pendingApprovalFromEvents(events = []) {
  const requestEvent = [...events]
    .reverse()
    .find((event) => event.event_type === "approval_required" && event.payload?.approval_request);
  const request = requestEvent?.payload?.approval_request;
  if (!request?.checkpoint) {
    return null;
  }
  const resolved = events.some((event) =>
    event.sequence > requestEvent.sequence
    && ["approval_granted", "approval_rejected"].includes(event.event_type)
    && event.payload?.checkpoint === request.checkpoint
  );
  return resolved ? null : request;
}

function editableProposalPaths(proposal = state.lastReport?.patch_proposal) {
  const visiblePaths = new Set((proposal?.files || []).map((file) => file.path));
  return (proposal?.file_edits || [])
    .map((edit) => edit.path)
    .filter((path) => !visiblePaths.size || visiblePaths.has(path));
}

function approvedFilePaths() {
  const available = new Set(editableProposalPaths());
  return Array.from(state.approvedPaths).filter((path) => available.has(path));
}

function updateApprovalState() {
  const total = editableProposalPaths().length;
  const selected = approvedFilePaths().length;
  $("applyProposal").disabled = !state.proposalId || state.proposalApplied || !state.lastReport?.patch_proposal?.apply_ready || total === 0 || selected === 0;
  if ($("approvalStatus")) {
    $("approvalStatus").textContent = total
      ? `${selected} of ${total} apply-ready file(s) approved.`
      : "No apply-ready file edits.";
  }
}

function setApprovalInputsDisabled(disabled) {
  document.querySelectorAll("[data-approval-path]").forEach((input) => {
    input.disabled = disabled;
  });
}

function renderProposals(proposal) {
  if (!proposal || !proposal.files || proposal.files.length === 0) {
    return item("No proposed changes.");
  }
  const editsByPath = new Map((proposal.file_edits || []).map((edit) => [edit.path, edit]));
  const files = proposal.files
    .map((file) => {
      const actions = file.suggested_actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("");
      const edit = editsByPath.get(file.path);
      const approved = state.approvedPaths.has(file.path);
      const approval = edit
        ? `<label class="approval-row">
            <input type="checkbox" data-approval-path="${escapeHtml(file.path)}" ${approved ? "checked" : ""} />
            Approve this file for apply
          </label>
          <p><small>${escapeHtml(edit.rationale || "Direct replacement edit available.")}</small></p>`
        : `<p><small>No direct file edit was generated for this file.</small></p>`;
      return `<div class="item">
        <div class="item-title">${escapeHtml(file.path)}
          <span class="tag">${escapeHtml(file.change_type)}</span>
          <span class="tag ${file.confidence === "high" ? "ok" : "warn"}">${escapeHtml(file.confidence)}</span>
          ${edit ? '<span class="tag ok">apply-ready</span>' : ""}
        </div>
        ${approval}
        <p>${escapeHtml(file.rationale)}</p>
        <ul>${actions}</ul>
      </div>`;
    })
    .join("");
  const risks = proposal.risks
    .map((risk) => `<div class="item"><div class="item-title">Risk <span class="tag ${risk.level === "high" ? "danger" : "warn"}">${escapeHtml(risk.level)}</span></div><p>${escapeHtml(risk.message)}</p><p>${escapeHtml(risk.mitigation)}</p></div>`)
    .join("");
  return `<div class="item"><div class="item-title">${escapeHtml(proposal.objective)}</div></div>${files}${risks}${renderValidationPlan(proposal.validation_plan)}${renderSafetyCheck(proposal.safety_check)}`;
}

function renderValidationPlan(plan) {
  if (!plan) {
    return "";
  }
  const commands = plan.commands && plan.commands.length
    ? plan.commands.map((command) => `<li><code>${escapeHtml(command)}</code></li>`).join("")
    : "<li>No command inferred.</li>";
  const notes = plan.notes && plan.notes.length
    ? plan.notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")
    : "<li>No notes.</li>";
  return `<div class="item">
    <div class="item-title">Recommended Validation <span class="tag">${escapeHtml(plan.source || "rules")}</span></div>
    <strong>Commands</strong>
    <ul>${commands}</ul>
    <strong>Notes</strong>
    <ul>${notes}</ul>
  </div>`;
}

function renderSafetyCheck(safety) {
  if (!safety) {
    return "";
  }
  const status = safety.ok ? "ok" : "danger";
  const findings = safety.findings && safety.findings.length
    ? safety.findings.map((finding) => `<li><strong>${escapeHtml(finding.code)}</strong>${finding.path ? ` (${escapeHtml(finding.path)})` : ""}: ${escapeHtml(finding.message)} ${escapeHtml(finding.mitigation)}</li>`).join("")
    : "<li>No safety findings.</li>";
  return `<div class="item">
    <div class="item-title">Safety Check <span class="tag ${status}">${safety.ok ? "passed" : "blocked"}</span></div>
    <p>Checked ${(safety.checked_files || []).length} file(s).</p>
    <ul>${findings}</ul>
  </div>`;
}

function renderValidation(results) {
  if (!results || results.length === 0) {
    return item("No validation commands were run.");
  }
  return results
    .map((result) => `<div class="item">
      <div class="item-title">${escapeHtml(result.command)} <span class="tag ${result.exit_code === 0 ? "ok" : "danger"}">${result.exit_code ?? "rejected"}</span></div>
      <pre>${escapeHtml(result.stdout || result.stderr || "")}</pre>
    </div>`)
    .join("");
}

function renderValidationFeedback(feedback, repairState = {}) {
  if (!feedback) {
    return item("No validation failures detected.");
  }
  const budget = renderRepairBudget(repairState);
  const files = feedback.suspected_files && feedback.suspected_files.length
    ? feedback.suspected_files.map((path) => `<li>${escapeHtml(path)}</li>`).join("")
    : "<li>No specific file extracted.</li>";
  const steps = (feedback.repair_steps || []).map((step) => `<li>${escapeHtml(step)}</li>`).join("");
  const failures = (feedback.failures || [])
    .map((failure) => `<div class="item">
      <div class="item-title">${escapeHtml(failure.command)}
        <span class="tag danger">${escapeHtml(failure.exit_code ?? "rejected")}</span>
      </div>
      <p>${escapeHtml((failure.signals || []).join(", ") || "No signals extracted.")}</p>
      <pre>${escapeHtml(failure.output_excerpt || "")}</pre>
    </div>`)
    .join("");
  const repairTag = repairState.repair_stop_reason
    ? `<span class="tag danger">stopped: ${escapeHtml(repairState.repair_stop_reason)}</span>`
    : repairState.repair_budget_exhausted
      ? '<span class="tag danger">budget exhausted</span>'
      : '<span class="tag danger">repair available</span>';
  return `<div class="item">
    <div class="item-title">Failure Analysis ${repairTag}</div>
    <p>${escapeHtml(feedback.summary || "")}</p>
    ${budget}
    <strong>Suspected Files</strong>
    <ul>${files}</ul>
    <strong>Repair Steps</strong>
    <ul>${steps || "<li>No repair steps available.</li>"}</ul>
  </div>${failures}`;
}

function renderRepairBudget(repairState = {}) {
  if (repairState.max_repair_attempts === undefined || repairState.repair_attempt === undefined) {
    return "";
  }
  const maxAttempts = Number(repairState.max_repair_attempts) || 0;
  const currentAttempt = Number(repairState.repair_attempt) || 0;
  const remaining = Number(repairState.repair_budget_remaining ?? Math.max(maxAttempts - currentAttempt, 0));
  if (repairState.repair_stop_reason) {
    return `<p><strong>Repair stopped:</strong> ${escapeHtml(repairState.repair_stop_message || repairState.repair_stop_reason)}</p>`;
  }
  if (maxAttempts <= 0 || repairState.repair_budget_exhausted) {
    return `<p><strong>Repair budget:</strong> exhausted (${escapeHtml(currentAttempt)}/${escapeHtml(maxAttempts)}).</p>`;
  }
  const nextAttempt = repairState.next_repair_attempt ?? currentAttempt + 1;
  return `<p><strong>Repair budget:</strong> next attempt ${escapeHtml(nextAttempt)}/${escapeHtml(maxAttempts)}; ${escapeHtml(remaining)} remaining.</p>`;
}

function renderRepairLoop(repairState = {}) {
  const history = repairState.repair_history || [];
  const automation = repairState.agent_repair_mode === "unified_controller"
    ? '<span class="tag ok">same-controller repair</span>'
    : repairState.auto_repair_enabled
      ? '<span class="tag ok">automatic generation enabled</span>'
      : '<span class="tag">manual generation</span>';
  const stopped = repairState.repair_stop_reason
    ? `<div class="item">
        <div class="item-title">Loop stopped <span class="tag danger">${escapeHtml(repairState.repair_stop_reason)}</span></div>
        <p>${escapeHtml(repairState.repair_stop_message || "No stop detail was recorded.")}</p>
      </div>`
    : "";
  const attempts = history.map((attempt) => {
    const trigger = shortFingerprint(attempt.trigger_failure_fingerprint);
    const proposal = shortFingerprint(attempt.proposal_fingerprint);
    const result = shortFingerprint(attempt.result_failure_fingerprint);
    const paths = renderList(attempt.proposal_paths, "No proposal paths recorded.");
    const statusClass = attempt.status === "completed"
      ? "ok"
      : attempt.status === "stopped" || attempt.status === "validation_failed" ? "danger" : "warn";
    const attemptLabel = Number(attempt.attempt) === 0
      ? "Baseline validation"
      : `Repair attempt ${attempt.attempt}`;
    return `<div class="item">
      <div class="item-title">${escapeHtml(attemptLabel)}
        <span class="tag ${statusClass}">${escapeHtml(attempt.status || "unknown")}</span>
      </div>
      <p>${escapeHtml(attempt.summary || "No summary recorded.")}</p>
      <p><small>Trigger ${escapeHtml(trigger)}; proposal ${escapeHtml(proposal)}; result ${escapeHtml(result)}</small></p>
      <strong>Proposal paths</strong><ul>${paths}</ul>
    </div>`;
  }).join("");
  return `<div class="item"><div class="item-title">Repair Control ${automation}</div>${renderRepairBudget(repairState)}</div>`
    + stopped
    + (attempts || item("No repair attempt has run."));
}

function shortFingerprint(value) {
  return value ? String(value).slice(0, 12) : "none";
}

function renderDelivery(data) {
  updateRepositorySourceStatus(data.repository_source);
  const state = data.state || {};
  const changes = state.changes || [];
  const changedFiles = changes.length
    ? changes.map((change) => `<li>${escapeHtml(change.path)} <span class="tag">${escapeHtml(change.description)}</span></li>`).join("")
    : "<li>No changed files detected.</li>";
  const summaries = (data.change_summary || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  const validation = (data.validation_notes || []).map((line) => `<li>${escapeHtml(line)}</li>`).join("");
  $("prReadinessContent").innerHTML = renderPrReadiness(data.pr_readiness);
  updateCreatePullRequestState(data.pr_readiness, data.pull_request);
  $("deliveryContent").innerHTML = `
    <div class="metrics">
      <div><span>${escapeHtml(state.branch || "unknown")}</span><small>branch</small></div>
      <div><span>${escapeHtml(String(state.ahead ?? 0))}</span><small>ahead</small></div>
      <div><span>${escapeHtml(String(state.behind ?? 0))}</span><small>behind</small></div>
    </div>
    <div class="item">
      <div class="item-title">Suggested Commit Message</div>
      <pre>${escapeHtml(data.suggested_commit_message || "")}</pre>
    </div>
    <div class="item">
      <div class="item-title">Changed Files</div>
      <ul>${changedFiles}</ul>
    </div>
    <div class="item">
      <div class="item-title">Change Summary</div>
      <ul>${summaries || "<li>No summary available.</li>"}</ul>
    </div>
    <div class="item">
      <div class="item-title">Validation Notes</div>
      <ul>${validation || "<li>Validation not provided.</li>"}</ul>
    </div>
    <div class="item">
      <div class="item-title">PR Draft Title</div>
      <pre>${escapeHtml(data.pull_request?.title || "")}</pre>
    </div>
    <div class="item">
      <div class="item-title">PR Draft Body</div>
      <pre>${escapeHtml(data.pull_request?.body || "")}</pre>
    </div>
    <div class="item">
      <div class="item-title">Diff Stat</div>
      <pre>${escapeHtml(state.diff_stat || state.staged_diff_stat || "No diff stat.")}</pre>
    </div>
  `;
}

function updateCreatePullRequestState(readiness, draft = state.delivery?.pull_request) {
  $("createPullRequest").disabled = !readiness?.ready || !draft?.title || !draft?.body;
}

function renderPrCreated(pr) {
  if (!pr) {
    return "";
  }
  const link = pr.html_url
    ? `<p><a href="${escapeHtml(pr.html_url)}" target="_blank" rel="noreferrer">${escapeHtml(pr.html_url)}</a></p>`
    : "";
  return `<div class="item">
    <div class="item-title">Pull Request Created <span class="tag ok">created</span></div>
    <p>#${escapeHtml(pr.number || "")} ${escapeHtml(pr.title || "")}</p>
    ${link}
  </div>`;
}

function renderPrReadiness(readiness) {
  if (!readiness) {
    return item("No PR readiness data yet.");
  }
  const status = readiness.ready ? "ok" : "warn";
  const repo = readiness.repository
    ? `${readiness.repository.owner}/${readiness.repository.repo}`
    : "No GitHub remote";
  const blockers = renderList(readiness.blockers, "No blockers detected.");
  const warnings = renderList(readiness.warnings, "No warnings.");
  const steps = renderList(readiness.next_steps, "No next steps.");
  const commands = (readiness.suggested_commands || []).length
    ? readiness.suggested_commands.map((command) => `<li><code>${escapeHtml(command)}</code></li>`).join("")
    : "<li>No command suggestions.</li>";
  const createCommand = readiness.create_pr_command
    ? `<strong>PR Command</strong><pre>${escapeHtml(readiness.create_pr_command)}</pre>`
    : "";
  return `<div class="item">
    <div class="item-title">PR Readiness <span class="tag ${status}">${readiness.ready ? "ready" : "attention needed"}</span></div>
    <p>${escapeHtml(repo)} - ${escapeHtml(readiness.branch || "unknown")} -> ${escapeHtml(readiness.base_branch || "main")}</p>
    <p><small>Upstream: ${escapeHtml(readiness.upstream || "none")}; clean worktree: ${readiness.clean_worktree ? "yes" : "no"}; pushed: ${readiness.upstream_pushed ? "yes" : "no"}.</small></p>
    <strong>Blockers</strong>
    <ul>${blockers}</ul>
    <strong>Warnings</strong>
    <ul>${warnings}</ul>
    <strong>Next Steps</strong>
    <ul>${steps}</ul>
    <strong>Suggested Commands</strong>
    <ul>${commands}</ul>
    ${createCommand}
  </div>`;
}

function renderList(values, emptyMessage) {
  return values && values.length
    ? values.map((value) => `<li>${escapeHtml(value)}</li>`).join("")
    : `<li>${escapeHtml(emptyMessage)}</li>`;
}

function renderHistory(runs) {
  if (!runs.length) {
    $("historyContent").innerHTML = item("No saved runs yet.");
    return;
  }
  $("historyContent").innerHTML = runs
    .map((run) => `<div class="item">
      <div class="item-title">${escapeHtml(run.task)}
        <span class="tag">${escapeHtml(run.mode)}</span>
        ${run.pinned ? '<span class="tag ok">pinned</span>' : ""}
        <span class="tag ${run.applied ? "ok" : "warn"}">${run.applied ? "applied" : "open"}</span>
      </div>
      <p><small>${escapeHtml(run.created_at)}</small></p>
      <p>${escapeHtml(run.summary || "")}</p>
      <div class="toolbar">
        <button class="secondary" data-history-id="${escapeHtml(run.id)}">Open</button>
        <button class="secondary" data-task="${escapeHtml(run.task)}">Use as task</button>
        <button class="secondary" data-history-pin="${escapeHtml(run.id)}" data-history-pinned="${run.pinned ? "false" : "true"}">${run.pinned ? "Unpin" : "Pin"}</button>
        <button class="secondary danger-button" data-history-delete="${escapeHtml(run.id)}">Delete</button>
      </div>
    </div>`)
    .join("");
}

function renderHistoryDetail(run) {
  const timeline = (run.timeline || [])
    .map((event) => `<li>${escapeHtml(event.step)}: ${escapeHtml(event.status)} - ${escapeHtml(event.detail)}</li>`)
    .join("");
  const validation = (run.validation || [])
    .map((result) => `<li>${escapeHtml(result.command)}: ${result.allowed ? `exit ${result.exit_code}` : "rejected"}</li>`)
    .join("");
  const traces = (run.llm_traces || []).map(renderSavedTrace).join("");
  const runtimeEvents = (run.agent_events || [])
    .map((event) => `<li>#${escapeHtml(event.sequence)} ${escapeHtml(event.event_type || "event")}${event.action_id ? ` - ${escapeHtml(event.action_id)}` : ""}</li>`)
    .join("");
  const latestStateEvent = [...(run.agent_events || [])]
    .reverse()
    .find((event) => event.event_type === "working_state_updated");
  const latestInputEvent = [...(run.agent_events || [])]
    .reverse()
    .find((event) => event.event_type === "input_required");
  const latestVirtualDiffEvent = [...(run.agent_events || [])]
    .reverse()
    .find((event) => {
      const observation = event.payload?.observation;
      return observation?.status === "completed"
        && ["inspect_proposed_diff", "propose_patch"].includes(observation.action_kind)
        && typeof observation.data?.diff === "string";
    });
  const agentState = run.agent_state || latestStateEvent?.payload?.working_state;
  const agentStopReason = run.agent_stop_reason || agentState?.stop_reason || "";
  const agentPendingQuestion = run.agent_pending_question
    || latestInputEvent?.payload?.observation?.data?.question
    || "";
  const agentPendingApproval = run.agent_pending_approval
    || pendingApprovalFromEvents(run.agent_events || []);
  const agentProposedEdits = run.agent_proposed_edits || agentState?.proposed_edits || [];
  const agentProposedDiff = run.agent_proposed_diff
    || latestVirtualDiffEvent?.payload?.observation?.data?.diff
    || "";
  const pinnedTag = run.pinned ? ' <span class="tag ok">pinned</span>' : "";
  $("historyDetail").innerHTML = `
    <div class="item">
      <div class="item-title">${escapeHtml(run.task)}${pinnedTag}</div>
      <p>${escapeHtml(run.summary || "")}</p>
      <p><small>${escapeHtml(run.created_at)} | ${escapeHtml(run.mode)} | ${escapeHtml(run.id)}</small></p>
    </div>
    <div class="item">
      <div class="item-title">Timeline</div>
      <ul>${timeline || "<li>No timeline saved.</li>"}</ul>
    </div>
    <div class="item">
      <div class="item-title">Proposal Diff</div>
      <pre>${escapeHtml(run.proposal?.proposed_diff || "No proposed diff saved.")}</pre>
    </div>
    <div class="item">
      <div class="item-title">LLM Trace History</div>
      ${traces || "<p>No LLM traces saved.</p>"}
    </div>
    <div class="item">
      <div class="item-title">Agent Working State</div>
      ${renderAgentWorkingState(
        agentState,
        agentStopReason,
        agentPendingQuestion,
        null,
        [],
        agentProposedEdits,
        agentProposedDiff
      )}
    </div>
    <div class="item">
      <div class="item-title">Runtime Events</div>
      <p><small>${escapeHtml(run.agent_runtime_run_id || "No runtime run id")}</small></p>
      <ul>${runtimeEvents || "<li>No typed runtime events saved.</li>"}</ul>
    </div>
    ${renderRuntimeApproval(agentPendingApproval)}
    <div class="item">
      <div class="item-title">Validation</div>
      <ul>${validation || "<li>No validation saved.</li>"}</ul>
    </div>
  `;
}

function renderSavedTrace(trace) {
  return `<details class="trace-details">
    <summary>${escapeHtml(trace.name || "trace")} ${trace.model ? `<span class="tag">${escapeHtml(trace.model)}</span>` : ""} <span class="tag ${trace.parsed ? "ok" : "danger"}">${trace.parsed ? "parsed" : "failed"}</span></summary>
    <p>${escapeHtml(trace.error || `Latency: ${trace.latency_ms ?? 0} ms`)}</p>
    ${trace.context_summary ? `<strong>Context Budget</strong><p>${escapeHtml(trace.context_summary)}</p>` : ""}
    <strong>Prompt</strong>
    <pre>${escapeHtml(trace.prompt_preview || "")}</pre>
    <strong>Raw Output</strong>
    <pre>${escapeHtml(trace.raw_output || "")}</pre>
  </details>`;
}

function buildValidationNotes() {
  const validation = state.lastReport?.validation || [];
  if (!validation.length) {
    return [];
  }
  return validation.map((result) => {
    if (!result.allowed) {
      return `${result.command}: rejected`;
    }
    return `${result.command}: exit ${result.exit_code}`;
  });
}

function renderLlmTraces(traces) {
  if (!traces || traces.length === 0) {
    return item("No LLM calls were recorded for this run.");
  }
  return traces
    .map((trace, index) => `<div class="item">
      <div class="item-title">${escapeHtml(trace.name)}
        <span class="tag">${escapeHtml(trace.model)}</span>
        <span class="tag ${trace.parsed ? "ok" : "danger"}">${trace.parsed ? "parsed" : "failed"}</span>
      </div>
      <p>${escapeHtml(trace.error || `Latency: ${trace.latency_ms ?? 0} ms`)}</p>
      ${trace.context_summary ? `<strong>Context Budget</strong><p>${escapeHtml(trace.context_summary)}</p>` : ""}
      <details class="trace-details">
        <summary>Prompt ${index + 1}</summary>
        <pre>${escapeHtml(trace.prompt_preview || "")}</pre>
      </details>
      <details class="trace-details">
        <summary>Raw Output ${index + 1}</summary>
        <pre>${escapeHtml(trace.raw_output || "")}</pre>
      </details>
    </div>`)
    .join("");
}

function renderGithub(data) {
  updateRepositorySourceStatus(data.repository_source);
  if (data.error || data.unavailable_reason) {
    $("githubContent").innerHTML = item(data.error || data.unavailable_reason);
    return;
  }
  const repo = data.repository ? item(`<strong>${escapeHtml(data.repository.owner)}/${escapeHtml(data.repository.repo)}</strong><br>${escapeHtml(data.repository.html_url)}`) : item("Repository unavailable.");
  const issues = data.issues.length
    ? data.issues.map(renderIssue).join("")
    : item("No open issues returned.");
  const prs = data.pull_requests.length
    ? data.pull_requests.map(renderPullRequest).join("")
    : item("No open pull requests returned.");
  $("githubContent").innerHTML = `<h2>Repository</h2>${repo}<h2>Open Issues</h2>${issues}<h2>Open Pull Requests</h2>${prs}`;
}

function renderTimeline(events) {
  if (!events || events.length === 0) {
    $("timelineList").innerHTML = item("No timeline events yet.");
    return;
  }
  $("timelineList").innerHTML = events
    .map((event) => `<div class="timeline-event">
      <div class="timeline-step">${escapeHtml(event.step)}</div>
      <div class="timeline-status">${escapeHtml(event.status)}</div>
      <div>${escapeHtml(event.detail)}</div>
    </div>`)
    .join("");
}

function renderIssue(issue) {
  const taskText = buildIssueTask(issue);
  const comments = issue.comments && issue.comments.length
    ? `<strong>Comments</strong><ul>${issue.comments.map((comment) => `<li>${escapeHtml(comment.author)}: ${escapeHtml(comment.body_preview || "")}</li>`).join("")}</ul>`
    : "";
  return `<div class="item">
    <div class="item-title">#${issue.number} ${escapeHtml(issue.title)}</div>
    <p><small>${escapeHtml(issue.author)} updated ${escapeHtml(issue.updated_at)}</small></p>
    ${issue.body_preview ? `<p>${escapeHtml(issue.body_preview)}</p>` : ""}
    ${comments}
    <div class="issue-actions">
      <button class="secondary" data-task="${escapeHtml(taskText)}">Use as task</button>
    </div>
  </div>`;
}

function buildIssueTask(issue) {
  const labels = issue.labels && issue.labels.length ? `\nLabels: ${issue.labels.join(", ")}` : "";
  const body = issue.body_preview ? `\n\nBody:\n${issue.body_preview}` : "";
  const comments = issue.comments && issue.comments.length
    ? `\n\nRecent comments:\n${issue.comments.map((comment) => `- ${comment.author}: ${comment.body_preview}`).join("\n")}`
    : "";
  return `GitHub issue #${issue.number}: ${issue.title}${labels}\nURL: ${issue.html_url}${body}${comments}`;
}

function renderPullRequest(pr) {
  const reviews = pr.reviews.length ? pr.reviews.map((review) => `<li>${escapeHtml(review.state)} by ${escapeHtml(review.reviewer)}</li>`).join("") : "<li>No reviews returned</li>";
  const checks = pr.checks.length ? pr.checks.map((check) => `<li>${escapeHtml(check.name)}: ${escapeHtml(check.status)}${check.conclusion ? `/${escapeHtml(check.conclusion)}` : ""}</li>`).join("") : "<li>No checks returned</li>";
  const files = pr.files && pr.files.length
    ? pr.files.map((file) => `<li>${escapeHtml(file.filename)} <span class="tag">${escapeHtml(file.status)}</span> +${escapeHtml(file.additions)} / -${escapeHtml(file.deletions)}</li>`).join("")
    : "<li>No files returned</li>";
  const comments = pr.comments && pr.comments.length
    ? pr.comments.map((comment) => `<li>${escapeHtml(comment.author)}: ${escapeHtml(comment.body_preview || "")}</li>`).join("")
    : "<li>No conversation comments returned</li>";
  const reviewComments = pr.review_comments && pr.review_comments.length
    ? pr.review_comments.map((comment) => `<li>${escapeHtml(comment.path)}${comment.line ? `:${escapeHtml(comment.line)}` : ""} by ${escapeHtml(comment.reviewer)}: ${escapeHtml(comment.body_preview || "")}</li>`).join("")
    : "<li>No review comments returned</li>";
  const taskText = buildPullRequestTask(pr);
  return `<div class="item">
    <div class="item-title">#${pr.number} ${escapeHtml(pr.title)}</div>
    <p>${escapeHtml(pr.source_branch)} -> ${escapeHtml(pr.target_branch)}</p>
    <p><small>${escapeHtml(pr.author)} updated ${escapeHtml(pr.updated_at)}</small></p>
    ${pr.body_preview ? `<p>${escapeHtml(pr.body_preview)}</p>` : ""}
    <strong>Files</strong><ul>${files}</ul>
    <strong>Conversation</strong><ul>${comments}</ul>
    <strong>Review Comments</strong><ul>${reviewComments}</ul>
    <strong>Reviews</strong><ul>${reviews}</ul>
    <strong>Checks</strong><ul>${checks}</ul>
    <div class="issue-actions">
      <button class="secondary" data-task="${escapeHtml(taskText)}">Use as task</button>
    </div>
  </div>`;
}

function buildPullRequestTask(pr) {
  const files = pr.files && pr.files.length
    ? `\n\nChanged files:\n${pr.files.map((file) => `- ${file.filename}: ${file.status}, +${file.additions}/-${file.deletions}`).join("\n")}`
    : "";
  const reviewComments = pr.review_comments && pr.review_comments.length
    ? `\n\nReview comments:\n${pr.review_comments.map((comment) => `- ${comment.path}${comment.line ? `:${comment.line}` : ""} by ${comment.reviewer}: ${comment.body_preview}`).join("\n")}`
    : "";
  const checks = pr.checks && pr.checks.length
    ? `\n\nChecks:\n${pr.checks.map((check) => `- ${check.name}: ${check.status}${check.conclusion ? `/${check.conclusion}` : ""}${check.output_summary_preview ? ` - ${check.output_summary_preview}` : ""}`).join("\n")}`
    : "";
  return `GitHub PR #${pr.number}: ${pr.title}\nURL: ${pr.html_url}\nBranch: ${pr.source_branch} -> ${pr.target_branch}\nAuthor: ${pr.author}\n\nBody:\n${pr.body_preview || "(none)"}${files}${reviewComments}${checks}`;
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement)) {
    return;
  }
  if (target.matches("[data-task]")) {
    $("taskInput").value = target.dataset.task || "";
    setStatus("Task loaded into input.");
    return;
  }
  if (target.matches("[data-history-delete]")) {
    deleteHistoryRun(target.dataset.historyDelete || "");
    return;
  }
  if (target.matches("[data-history-pin]")) {
    toggleHistoryPin(target.dataset.historyPin || "", target.dataset.historyPinned === "true");
    return;
  }
  if (target.matches("[data-history-id]")) {
    loadHistoryDetail(target.dataset.historyId || "");
  }
});

function buildLlmInputPreview(report, payload) {
  if (report.llm_traces && report.llm_traces.length) {
    return report.llm_traces.map((trace) => `# ${trace.name}\n${trace.prompt_preview || ""}`).join("\n\n---\n\n");
  }
  const context = report.relevant_files
    .slice(0, 5)
    .map((hit) => `Path: ${hit.path}\nScore: ${hit.score}\nReasons: ${hit.reasons.join(", ")}\nPreview:\n${hit.preview}`)
    .join("\n\n---\n\n");
  return `Repository source: ${payload.repo_source}\nRepository input: ${payload.repo}\nGitHub URL: ${payload.github_url || "(none)"}\nBranch: ${payload.branch || "(default)"}\nUse LLM: ${payload.use_llm}\nUse memory: ${payload.use_memory}\nIterative agent: ${payload.iterative_agent}\nAgent max steps: ${payload.agent_max_steps || "(default)"}\nAgent max tool calls: ${payload.agent_max_tool_calls || "(default)"}\nValidation command limit: ${payload.max_validation_commands || "(default)"}\nExecution timeout: ${payload.execution_timeout_seconds || "(default)"} seconds\nModel: ${payload.model || "(default)"}\nLLM timeout: ${payload.timeout_seconds || "(default)"} seconds\nTask: ${payload.task}\n\nRelevant context:\n${context || "No context selected."}`;
}

function buildLlmOutputPreview(report) {
  if (report.llm_traces && report.llm_traces.length) {
    return report.llm_traces.map((trace) => `# ${trace.name}\n${trace.raw_output || ""}`).join("\n\n---\n\n");
  }
  return JSON.stringify(
    {
      plan_metadata: report.plan_metadata,
      plan: report.plan,
      patch_proposal_metadata: report.patch_proposal_metadata,
      patch_proposal: report.patch_proposal,
      patch_review: report.patch_review,
    },
    null,
    2
  );
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return response.json();
}

async function getJson(url) {
  const response = await fetch(url);
  return response.json();
}

function sourceLabel(metadata) {
  if (!metadata) return "unknown";
  return metadata.fallback_used ? `${metadata.source} fallback` : metadata.source;
}

function repositoryQuery() {
  const params = new URLSearchParams({
    repo: $("repoPath").value.trim() || ".",
    repo_source: $("repoSource").value,
  });
  const githubUrl = $("githubUrl").value.trim();
  if (githubUrl) {
    params.set("github_url", githubUrl);
  }
  const branch = $("repoBranch").value.trim();
  if (branch) {
    params.set("branch", branch);
  }
  return params.toString();
}

function buildRepositoryPayload() {
  return {
    repo: $("repoPath").value.trim() || ".",
    repo_source: $("repoSource").value,
    github_url: $("githubUrl").value.trim(),
    branch: $("repoBranch").value.trim(),
  };
}

function updateRepositorySourceUi() {
  const source = $("repoSource").value;
  $("githubUrlWrap").classList.toggle("hidden", source === "local");
  $("repoPath").placeholder = source === "github" ? "Optional cache context; GitHub URL is used" : ".";
  if (source === "github") {
    $("repoSourceLine").textContent = "GitHub repositories are cloned into .repopilot/repos before analysis.";
  } else if (source === "auto") {
    $("repoSourceLine").textContent = "Auto detects GitHub URLs or local paths from the repository input.";
  } else {
    $("repoSourceLine").textContent = "Using local repository path.";
  }
}

function updateRepositorySourceStatus(source) {
  if (!source) {
    return;
  }
  const label = source.source === "github" ? `GitHub ${source.owner}/${source.repo}` : "Local path";
  const details = [
    `${label}: ${source.local_path}.`,
    source.branch ? `Branch: ${source.branch}.` : "",
    source.latest_commit ? `Commit: ${source.latest_commit}.` : "",
    source.dirty ? "Local changes present." : "",
    source.synced ? "Synced." : "",
    source.message || "",
  ].filter(Boolean);
  $("repoSourceLine").textContent = details.join(" ").trim();
  if (source.branch && !$("repoBranch").value.trim()) {
    $("repoBranch").value = source.branch;
  }
  if (source.local_path) {
    $("repoPath").value = source.source === "github" ? $("repoPath").value : source.local_path;
  }
}

function item(content) {
  return `<div class="item">${content}</div>`;
}

function setStatus(message) {
  $("statusLine").textContent = message;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function capitalize(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : value;
}

loadGithub().catch((error) => {
  $("githubContent").innerHTML = item(`GitHub status unavailable: ${escapeHtml(error.message)}`);
});
loadDiff(false).catch((error) => {
  $("diffOutput").textContent = `Diff unavailable: ${error.message}`;
});
updateRepositorySourceUi();
refreshSandboxes().catch((error) => {
  $("sandboxLine").textContent = `Sandbox status unavailable: ${error.message}`;
});
loadLatestTaskRun().catch(() => {
  $("taskRunMessage").textContent = "Saved task-run state is unavailable for this repository.";
});
loadHistory().catch(() => {});
