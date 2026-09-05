const state = {
  lastReport: null,
  github: null,
  delivery: null,
  deliveryRepository: null,
  repositoryGeneration: 0,
  proposalId: null,
  proposalReviewKey: null,
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
  trajectory: null,
  trajectoryIndex: 0,
  trajectoryTimer: null,
  historyTrajectory: null,
  runMode: "workflow",
  loadedViews: new Set(),
  sandboxesLoaded: false,
  activeView: "summary",
  controlsCollapsed: false,
  diffStaged: false,
  diffRequest: 0,
  busyActions: new Set(),
  feedbackRevision: 0,
  feedbackTone: "info",
  retryAction: null,
  reportPayload: null,
};

const TASK_RUN_PHASES = ["Sandbox", "Explore", "Approval", "Apply", "Validate", "Complete"];

const $ = (id) => document.getElementById(id);

function setBrandMenuOpen(open) {
  $("brandMenu").classList.toggle("open", open);
  $("brandMenuToggle").setAttribute("aria-expanded", open ? "true" : "false");
}

$("brandMenuToggle").addEventListener("click", () => {
  setBrandMenuOpen(!$("brandMenu").classList.contains("open"));
});

$("brandLinks").addEventListener("click", (event) => {
  if (event.target.closest?.("a")) {
    setBrandMenuOpen(false);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("brandMenu").classList.contains("open")) {
    setBrandMenuOpen(false);
    $("brandMenuToggle").focus();
  }
});

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    activateTab(button.dataset.tab || "summary");
  });
});

document.querySelectorAll("[data-run-mode]").forEach((button) => {
  button.addEventListener("click", () => selectRunMode(button.dataset.runMode || "workflow"));
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
  updateLlmSettingsUi();
});
$("customModel").addEventListener("input", updateLlmSettingsUi);
$("useLlm").addEventListener("change", () => {
  updateLlmSettingsUi();
  if ($("useLlm").checked) {
    $("llmSettings").open = true;
  }
});
$("iterativeAgent").addEventListener("change", updateLlmSettingsUi);
$("autoRepair").addEventListener("change", updateLlmSettingsUi);
$("toggleControls").addEventListener("click", () => setControlsCollapsed(!state.controlsCollapsed));
$("workspaceAction").addEventListener("click", openWorkspaceAction);
$("openChangesDelivery").addEventListener("click", () => {
  activateTab("delivery");
  revealWorkspace("deliveryTab");
});
$("retryView").addEventListener("click", () => state.retryAction?.());
$("repoSource").addEventListener("change", handleRepositoryChange);
["repoPath", "githubUrl", "repoBranch"].forEach((id) => {
  $(id).addEventListener("input", handleRepositoryChange);
});
$("sandboxSelect").addEventListener("change", selectSandbox);
$("repositorySettings").addEventListener("toggle", () => {
  if ($("repositorySettings").open && !state.sandboxesLoaded) {
    refreshSandboxes().catch((error) => {
      $("sandboxLine").textContent = `Sandbox status unavailable: ${error.message}`;
    });
  }
});
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

$("runPrimary").addEventListener("click", runSelectedMode);
bindAction("pauseTaskRun", pauseTaskRun, "Pausing...");
bindAction("checkTaskRunReadiness", checkTaskRunRecoveryReadiness, "Checking...");
bindAction("resumeTaskRun", resumeTaskRun, "Resuming...");
bindAction("cancelTaskRun", cancelTaskRun, "Cancelling...");
$("submitTaskRunInput").addEventListener("click", submitTaskRunInput);
bindAction("createTaskBranch", createTaskBranch, "Creating branch...");
$("openTaskAttention").addEventListener("click", openTaskAttention);
bindAction("approveRuntimeWrite", approveRuntimeWrite, "Executing...");
bindAction("rejectRuntimeWrite", rejectRuntimeWrite, "Rejecting...");
$("testLlm").addEventListener("click", testLlmConnection);
bindAction("applyProposal", applyProposal, "Applying...");
bindAction("revertProposal", revertProposal, "Reverting...");
bindAction("syncRepository", syncRepository, "Syncing...");
bindAction("createSandbox", createSandbox, "Creating...");
bindAction("refreshSandboxes", refreshSandboxes);
bindAction("removeSandbox", removeSandbox);
bindAction("loadGithub", loadGithub, "Loading...");
bindAction("loadDiff", () => loadDiff(false));
bindAction("loadStagedDiff", () => loadDiff(true));
bindAction("loadPrReadiness", loadPrReadiness, "Checking...");
bindAction("generateDelivery", generateDelivery, "Generating...");
bindAction("createPullRequest", createPullRequest, "Creating...");
bindAction("generateRepairProposal", generateRepairProposal, "Generating...");
bindAction("loadHistory", loadHistory);
bindAction("clearHistory", clearHistory, "Clearing...");
$("trajectoryFirst").addEventListener("click", () => moveTrajectory("first"));
$("trajectoryPrevious").addEventListener("click", () => moveTrajectory(-1));
$("trajectoryPlay").addEventListener("click", toggleTrajectoryPlayback);
$("trajectoryNext").addEventListener("click", () => moveTrajectory(1));
$("trajectoryLast").addEventListener("click", () => moveTrajectory("last"));
$("trajectoryCursor").addEventListener("input", () => {
  stopTrajectoryPlayback();
  state.trajectoryIndex = Number.parseInt($("trajectoryCursor").value, 10) || 0;
  renderTrajectoryFrame();
});
bindAction("refreshAll", refreshCurrentView);

function icon(name) {
  return `<svg class="ui-icon" aria-hidden="true"><use href="/vendor/icons.svg#${escapeHtml(name)}" /></svg>`;
}

function bindAction(id, action, label = "") {
  $(id).addEventListener("click", () => withBusy(id, label, action));
}

async function withBusy(id, label, action) {
  if (state.busyActions.has(id)) return;
  const button = $(id);
  const text = button.textContent;
  const disabled = button.disabled;
  const generation = state.repositoryGeneration;
  const revision = state.feedbackRevision;
  state.busyActions.add(id);
  button.disabled = true;
  button.classList.add("is-busy");
  button.setAttribute("aria-busy", "true");
  if (label) button.textContent = label;
  updateApprovalState();
  updateRuntimeApprovalControls();
  try {
    return await action();
  } catch (error) {
    setStatus(`Error: ${error.message}`, "danger");
  } finally {
    state.busyActions.delete(id);
    button.classList.remove("is-busy");
    button.setAttribute("aria-busy", "false");
    if (label && button.textContent === label) button.textContent = text;
    button.disabled = disabled;
    updateApprovalState();
    updateRuntimeApprovalControls();
    updateTaskControlState(state.taskRun || {});
    updateCreatePullRequestState(state.delivery?.pr_readiness);
    $("runPrimary").disabled = state.busyActions.has("runPrimary");
    $("generateRepairProposal").disabled = !state.repairParentId || state.busyActions.has("generateRepairProposal");
    $("removeSandbox").disabled = !state.sandbox || state.busyActions.has("removeSandbox");
    updateRefreshState();
    if ((generation === state.repositoryGeneration || id === "testLlm") && state.feedbackRevision > revision && state.feedbackTone === "danger") {
      state.retryAction = () => withBusy(id, label, action);
      $("retryView").hidden = false;
    }
  }
}

function setControlsCollapsed(collapsed) {
  state.controlsCollapsed = collapsed;
  document.querySelector(".layout")?.classList.toggle("controls-collapsed", collapsed);
  $("toggleControls").setAttribute("aria-expanded", String(!collapsed));
  const label = collapsed ? "Show run settings" : "Hide run settings";
  $("toggleControls").title = label;
  $("toggleControls").setAttribute("aria-label", label);
  if (!collapsed && window.matchMedia?.("(max-width: 980px)").matches) {
    $("runControls").scrollIntoView({ block: "start" });
  }
}

function revealWorkspace(id = "workspaceContext") {
  if (window.matchMedia?.("(max-width: 980px)").matches) setControlsCollapsed(true);
  const target = $(id);
  target.scrollIntoView?.({ block: "start", behavior: "instant" });
  target.focus({ preventScroll: true });
}

function activateTab(name) {
  const panel = $(`${name}Tab`);
  const button = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!panel || !button) {
    return;
  }
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.classList.remove("active");
    tab.setAttribute("aria-selected", "false");
  });
  document.querySelectorAll(".tab-content").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  button.setAttribute("aria-selected", "true");
  panel.classList.add("active");
  state.activeView = name;
  const secondary = button.classList.contains("secondary-tab");
  $("viewMenu").classList.toggle("active", secondary);
  $("secondaryViewLabel").textContent = secondary ? button.textContent.trim() : "More";
  $("viewMenu").open = false;
  if (name !== "trajectory") {
    stopTrajectoryPlayback();
  }
  loadViewData(name).catch(() => {});
  updateRefreshState();
  updateWorkspaceContext();
  if (name === "history") revealWorkspace("historyTab");
}

function selectedModel() {
  return $("modelSelect").value === "custom" ? $("customModel").value.trim() : $("modelSelect").value;
}

const RUN_MODES = {
  workflow: { label: "Run analysis", status: "Analysis", action: runWorkflow },
  proposal: { label: "Generate proposal", status: "Proposal", action: generateProposal },
  task: { label: "Start sandbox task", status: "Sandbox", action: startTaskRun },
};

function selectRunMode(mode) {
  state.runMode = RUN_MODES[mode] ? mode : "workflow";
  document.querySelectorAll("[data-run-mode]").forEach((button) => {
    const active = button.dataset.runMode === state.runMode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
  $("runPrimary").textContent = RUN_MODES[state.runMode].label;
  updateLlmSettingsUi();
}

async function runSelectedMode() {
  if (!$("taskInput").value.trim()) {
    setStatus("Task is required.", "danger");
    $("taskInput").focus();
    return;
  }
  const invalid = [...document.querySelectorAll("#runControls input[type='number']")]
    .find((input) => !input.disabled && !input.closest("[hidden]") && !input.checkValidity());
  if (invalid) {
    const section = invalid.closest("details");
    if (section) section.open = true;
    setStatus(invalid.validationMessage, "danger");
    invalid.reportValidity();
    return;
  }
  await withBusy("runPrimary", "Running...", RUN_MODES[state.runMode].action);
}

function updateLlmSettingsUi() {
  const enabled = $("useLlm").checked;
  const model = selectedModel() || "custom model";
  $("llmSettingsStatus").textContent = enabled ? model : "Rules only";
  $("llmOptions").hidden = !enabled;
  document.querySelectorAll(".agent-option").forEach((field) => {
    field.hidden = !enabled || !$("iterativeAgent").checked;
  });
  $("repairLimitField").hidden = !$("autoRepair").checked;
}

function viewLoader(name) {
  const loaders = {
    diff: () => loadDiff(state.diffStaged),
    github: loadGithub,
    history: loadHistory,
    taskRun: () => state.taskRun ? pollTaskRun() : loadLatestTaskRun(),
  };
  if (["summary", "trajectory", "llm", "json"].includes(name) && state.taskRun?.source_repo) return pollTaskRun;
  return loaders[name];
}

function updateRefreshState() {
  const available = Boolean(viewLoader(state.activeView));
  $("refreshAll").disabled = !available || state.busyActions.has("refreshAll");
  $("refreshAll").title = available ? "Refresh current view" : "This view has no live data to refresh";
}

async function loadViewData(name, { force = false } = {}) {
  const loader = viewLoader(name);
  if (!loader || (!force && state.loadedViews.has(name))) {
    return false;
  }
  const generation = state.repositoryGeneration;
  state.loadedViews.add(name);
  if (state.activeView === name) setStatus("Loading current view...", "busy");
  try {
    const loaded = await loader();
    if (loaded === false || generation !== state.repositoryGeneration) return false;
    if (state.activeView === name) setStatus("View updated.", "ok");
    return true;
  } catch (error) {
    state.loadedViews.delete(name);
    if (generation !== state.repositoryGeneration) return false;
    if (name === "diff") {
      $("diffOutput").textContent = `Diff unavailable: ${error.message}`;
    } else if (name === "github") {
      $("githubContent").innerHTML = item(`GitHub status unavailable: ${escapeHtml(error.message)}`);
    }
    if (state.activeView === name) {
      setStatus(`Error: ${error.message}`, "danger");
      state.retryAction = () => withBusy("refreshAll", "", () => loadViewData(name, { force: true }));
      $("retryView").hidden = false;
    }
    throw error;
  }
}

async function refreshCurrentView() {
  const refreshed = await loadViewData(state.activeView, { force: true });
  if (!refreshed && !viewLoader(state.activeView)) setStatus("This view has no live data to refresh.", "info");
}

async function runWorkflow() {
  resetRepositoryContext();
  setStatus("Running RepoPilot...");
  const payload = buildWorkflowPayload();

  try {
    const report = await postJson("/api/run", payload);
    if (!report) return;
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, payload);
    activateTab("summary");
    revealWorkspace();
    setStatus("Workflow complete.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function generateProposal() {
  resetRepositoryContext();
  setStatus("Generating patch proposal...");
  const payload = buildWorkflowPayload();

  try {
    const report = await postJson("/api/propose", payload);
    if (!report) return;
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, payload);
    activateTab("summary");
    revealWorkspace();
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
  resetRepositoryContext();
  state.loadedViews.add("taskRun");
  activateTab("taskRun");
  revealWorkspace("taskRunTab");
  try {
    const data = await postJson("/api/task-runs/start", payload);
    if (!data) return;
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
    if (!data) return;
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
    if (!data) return;
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
    if (!data) return null;
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
    if (!data) return;
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
    if (!data) return;
    if (data.error) throw new Error(data.error);
    updateTaskRun(data.task_run);
    setStatus(`Created local branch ${data.branch}.`);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function openTaskAttention() {
  const status = String(state.taskRun?.status || "");
  const target = status === "awaiting_approval" ? "runtimeDetails"
    : status === "repair_pending" ? "repairDetails" : "proposalReview";
  if (status === "awaiting_approval") {
    $("runtimeDetails").open = true;
  } else if (status === "repair_pending") {
    $("repairDetails").open = true;
  }
  activateTab("summary");
  revealWorkspace(target);
}

function statusPresentation(status) {
  const values = {
    completed: ["Completed", "ok"], applied: ["Applied", "ok"], analyzed: ["Analysis complete", "info"],
    failed: ["Failed", "danger"], validation_failed: ["Validation failed", "danger"],
    blocked: ["Needs attention", "warn"],
    interrupted: ["Interrupted", "warn"], paused: ["Paused", "warn"], cancelled: ["Cancelled", "neutral"],
    awaiting_approval: ["Approval needed", "warn"], awaiting_input: ["Answer needed", "warn"],
    review_pending: ["Ready for review", "info"], repair_pending: ["Repair needed", "warn"],
    idle: ["Not started", "neutral"],
  };
  const [label, tone] = values[status] || [capitalize(String(status || "Running").replaceAll("_", " ")), "info"];
  return { label, tone };
}

function reportBlockers(report) {
  const blockers = [...(report?.agent_completion_blockers || [])];
  const failures = (report?.validation || []).filter((result) => result.exit_code !== 0);
  if (failures.length) blockers.push(`${failures.length} validation command(s) failed or were rejected.`);
  if (report?.patch_proposal?.safety_check?.ok === false) {
    blockers.push(...(report.patch_proposal.safety_check.findings || []).map((finding) => finding.message));
    if (!report.patch_proposal.safety_check.findings?.length) blockers.push("The proposal safety check is blocked.");
  }
  if (report?.repair_stop_message) blockers.push(report.repair_stop_message);
  return [...new Set(blockers.filter(Boolean).map(String))];
}

function updateWorkspaceContext() {
  const report = state.lastReport;
  const run = state.taskRun;
  const visible = Boolean(report || run);
  $("workspaceContext").hidden = !visible || state.activeView === "history";
  if (!visible) return;
  const payload = state.reportPayload || state.taskRunPayload || {};
  $("workspaceTask").textContent = run?.task || payload.task || report?.patch_proposal?.objective || "Repository task";
  const repo = run?.sandbox_path || report?.repository_source?.local_path || payload.repo || $("repoPath").value;
  const branch = run?.delivery_branch || report?.repository_source?.branch || payload.branch || $("repoBranch").value;
  $("workspaceRepository").textContent = `${repo}${branch ? ` | ${branch}` : ""}${run?.sandbox_path ? " | Managed worktree" : ""}`;
  $("workspaceRepository").title = $("workspaceRepository").textContent;
  const status = run?.status || ((report?.validation || []).some((result) => result.exit_code !== 0) ? "validation_failed"
    : reportBlockers(report).length ? "blocked"
    : state.proposalApplied ? "applied" : report?.patch_proposal?.files?.length ? "review_pending" : "analyzed");
  const presentation = statusPresentation(status);
  $("workspaceStatus").className = `status-badge ${presentation.tone}`;
  $("workspaceStatus").innerHTML = icon(presentation.tone === "danger" ? "circle-x" : presentation.tone === "warn" ? "circle-alert" : "circle-check") + escapeHtml(presentation.label);
  const action = status === "awaiting_input" ? "Answer agent"
    : status === "awaiting_approval" ? "Review approval"
    : status === "repair_pending" || report?.validation_feedback ? "Review repair"
    : status === "review_pending" ? "Review changes" : "";
  $("workspaceAction").textContent = action;
  $("workspaceAction").hidden = !action;
  $("openDelivery").hidden = !(state.proposalApplied || run?.can_create_branch || run?.delivery_branch || status === "completed" || state.delivery);
}

function openWorkspaceAction() {
  if (state.taskRun?.status === "awaiting_input") {
    activateTab("taskRun");
    revealWorkspace("taskRunInputAnswer");
  } else if (["awaiting_approval", "review_pending", "repair_pending"].includes(state.taskRun?.status)) {
    openTaskAttention();
  } else {
    const target = state.lastReport?.validation_feedback ? "repairDetails" : "proposalReview";
    if (target === "repairDetails") $(target).open = true;
    activateTab("summary");
    revealWorkspace(target);
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
      use_memory: $("useMemory").checked,
      agent_max_steps: $("agentMaxSteps").value.trim(),
      checkpoint: request.checkpoint,
      payload_hash: request.payload_hash,
      file_scope: request.file_scope || [],
      command_allowlist: request.command_allowlist || [],
    });
    if (!data) return;
    if (data.error) {
      if (data.task_run) updateTaskRun(data.task_run);
      throw new Error(data.error);
    }
    state.taskRunRenderedProposalId = null;
    updateTaskRun(data.task_run);
    if (data.write_result) {
      renderWorkingDiff(data.write_result.resulting_diff || "");
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
    if (!data) return;
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
  if (!current?.run_id || !current.source_repo) return false;
  const params = new URLSearchParams({
    run_id: current.run_id,
    source_repo: current.source_repo,
  });
  const data = await getJson(`/api/task-runs/status?${params.toString()}`);
  if (!data) return false;
  if (data.error) throw new Error(data.error);
  if (state.taskRun?.run_id !== data.task_run?.run_id) return false;
  updateTaskRun(data.task_run);
  return true;
}

async function loadLatestTaskRun() {
  const data = await getJson(`/api/task-runs?${repositoryQuery()}&limit=1`);
  if (!data) return false;
  if (data.error) throw new Error(data.error);
  const latest = (data.task_runs || [])[0];
  if (!latest) {
    $("taskRunPhases").innerHTML = TASK_RUN_PHASES.map(
      (phase, index) => `<div class="task-run-phase">${index + 1}. ${escapeHtml(phase)}</div>`
    ).join("");
    return true;
  }
  state.taskRunPayload = buildWorkflowPayload();
  state.taskRunRenderedProposalId = null;
  updateTaskRun(latest);
  const shortId = String(latest.run_id || "task").slice(0, 8);
  $("taskRunBranch").value = latest.delivery_branch || `feature/repopilot-${shortId}`;
  return true;
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
  updateWorkspaceContext();
  updateRefreshState();
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
  resetRepositoryContext({ preserveTaskRun: true });
}

function renderTaskRun(taskRun) {
  const status = String(taskRun.status || "unknown");
  const presentation = statusPresentation(status);
  $("taskRunTitle").textContent = taskRun.task || state.taskRunPayload?.task || "Sandbox task";
  $("taskRunId").textContent = taskRun.run_id ? `Run ${String(taskRun.run_id).slice(0, 8)}` : "Not started";
  $("taskRunId").title = taskRun.run_id || "";
  $("taskRunStatus").textContent = presentation.label;
  $("taskRunStatus").className = `task-run-status ${presentation.tone}`;
  $("taskRunMessage").textContent = taskRun.message || "";
  renderTaskRunInterruption(taskRun);
  $("taskRunSandbox").textContent = taskRun.sandbox_path
    ? `${taskRun.sandbox_path}\nHEAD ${taskRun.sandbox_head || "unknown"}`
    : "Not created";
  updateTaskControlState(taskRun);
  const attentionLabels = {
    awaiting_approval: "Review approval",
    review_pending: "Review changes",
    repair_pending: "Review repair",
  };
  $("openTaskAttention").hidden = !attentionLabels[status];
  $("openTaskAttention").textContent = attentionLabels[status] || "Review action";
  if (status === "interrupted") {
    $("taskRunRecoveryDetails").open = true;
  }
  if (["failed", "repair_pending"].includes(status)) {
    $("taskRunEvidenceDetails").open = true;
  }
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
  if (hasTrajectory(taskRun.result?.agent_trajectory)) {
    setTrajectory(taskRun.result.agent_trajectory, { preserveTail: true });
  }
}

function updateTaskControlState(taskRun) {
  const available = {
    pauseTaskRun: taskRun.can_pause,
    checkTaskRunReadiness: ["paused", "cancelled", "failed", "interrupted"].includes(taskRun.status),
    resumeTaskRun: taskRun.can_resume,
    cancelTaskRun: taskRun.can_cancel,
    createTaskBranch: taskRun.can_create_branch,
  };
  Object.entries(available).forEach(([id, enabled]) => {
    $(id).disabled = !enabled || state.busyActions.has(id);
  });
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
    if (!data) return;
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

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}

async function testLlmConnection() {
  return withBusy("testLlm", "Testing...", async () => {
  setStatus("Testing LLM connection...");
  $("llmTestLine").textContent = "Testing model endpoint...";
  $("llmTestLine").dataset.tone = "busy";
  try {
    const data = await postJson("/api/llm/test", buildLlmPayload());
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    const model = data.model || selectedModel() || "configured model";
    const preview = data.response_preview ? ` Response: ${data.response_preview}` : "";
    $("llmTestLine").textContent = `OK: ${model} responded.${preview}`;
    $("llmTestLine").dataset.tone = "ok";
    setStatus("LLM connection OK.", "ok");
  } catch (error) {
    $("llmTestLine").textContent = `LLM test failed: ${error.message}`;
    $("llmTestLine").dataset.tone = "danger";
    setStatus(`Error: ${error.message}`);
  }
  });
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
  const confirmed = window.confirm(`Apply ${approvedPaths.length} selected file edit(s) to ${$("repoPath").value}?\n\nProposal ${state.proposalId}`);
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
    if (!result) return;
    if (result.error) {
      if (result.safety_check) {
        $("proposalList").innerHTML += renderSafetyCheck(result.safety_check);
        renderTimeline(result.timeline || []);
      }
      throw new Error(result.error);
    }
    renderWorkingDiff(result.diff || "");
    $("validationList").innerHTML = renderValidation(result.validation || []);
    $("validationFeedbackList").innerHTML = renderValidationFeedback(result.validation_feedback, result);
    if (result.validation_feedback) {
      $("repairDetails").open = true;
    }
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
    if (!result) return;
    if (result.error) {
      if (result.timeline) {
        renderTimeline(result.timeline);
      }
      throw new Error(result.error);
    }
    renderWorkingDiff(result.diff || "");
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
    if (!report) return;
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, buildWorkflowPayload());
    activateTab("summary");
    if (report.task_run) updateTaskRun(report.task_run);
    setStatus("Repair proposal ready for review.");
    revealWorkspace("proposalReview");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function buildWorkflowPayload() {
  const validation = $("validationInput").value.trim();
  return {
    repo: $("repoPath").value.trim() || ".",
    repo_source: $("repoSource").value,
    github_url: $("repoSource").value === "github" ? $("githubUrl").value.trim() : "",
    branch: $("repoBranch").value.trim(),
    task: $("taskInput").value.trim(),
    validation: validation ? [validation] : [],
    use_llm: $("useLlm").checked,
    ...buildLlmPayload(),
    no_llm_fallback: !$("allowFallback").checked,
    use_memory: $("useMemory").checked,
    iterative_agent: $("useLlm").checked && $("iterativeAgent").checked,
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
    no_llm_fallback: !$("allowFallback").checked,
    use_memory: $("useMemory").checked,
    iterative_agent: $("useLlm").checked && $("iterativeAgent").checked,
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
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    resetRepositoryContext();
    state.sandboxesLoaded = false;
    setStatus(data.repository_source?.message || "Repository synced.");
    await Promise.allSettled([
      loadViewData(
        document.querySelector(".tab-content.active")?.id?.replace(/Tab$/, "") || "summary",
        { force: true }
      ),
    ]);
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
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    state.sandboxes = data.sandboxes || [data.sandbox];
    renderSandboxOptions(data.sandbox?.path || "");
    activateSandbox(data.sandbox);
    state.sandboxesLoaded = true;
    setStatus("Worktree sandbox created and selected.");
    await Promise.allSettled([
      loadViewData(
        document.querySelector(".tab-content.active")?.id?.replace(/Tab$/, "") || "summary",
        { force: true }
      ),
    ]);
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
  if (!data) return;
  if (data.error) {
    throw new Error(data.error);
  }
  state.sandboxes = data.sandboxes || [];
  state.sandboxesLoaded = true;
  const currentPath = state.sandbox?.path || $("repoPath").value.trim();
  const selected = state.sandboxes.find((sandbox) => sandbox.path === currentPath) || null;
  state.sandbox = selected;
  if (previousSandbox && !selected) {
    restoreSandboxSource(previousSandbox);
    resetRepositoryContext();
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
    resetRepositoryContext();
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
  resetRepositoryContext();
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
    if (!data) return;
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
      if (!data) return;
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
    resetRepositoryContext();
    setStatus("Worktree sandbox removed.");
    await Promise.allSettled([
      loadViewData(
        document.querySelector(".tab-content.active")?.id?.replace(/Tab$/, "") || "summary",
        { force: true }
      ),
    ]);
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

function handleRepositoryChange() {
  state.sandbox = null;
  state.sandboxes = [];
  state.sandboxesLoaded = false;
  renderSandboxOptions("");
  renderSandboxStatus(null);
  updateRepositorySourceUi();
  resetRepositoryContext();
  setStatus("Repository selection changed.");
}

function resetRepositoryContext({ preserveTaskRun = false } = {}) {
  state.repositoryGeneration += 1;
  state.lastReport = null;
  state.reportPayload = null;
  state.diffRequest += 1;
  state.diffStaged = false;
  state.github = null;
  state.delivery = null;
  state.deliveryRepository = null;
  state.proposalId = null;
  state.proposalReviewKey = null;
  state.repairParentId = null;
  state.rollbackAvailable = false;
  state.proposalApplied = false;
  state.approvedPaths = new Set();
  state.loadedViews.clear();
  state.historyTrajectory = null;
  stopTrajectoryPlayback();
  setTrajectory(null);
  if (!preserveTaskRun) {
    stopTaskRunPolling();
    state.taskRun = null;
    state.taskRunPayload = null;
    state.taskRunSandboxPath = null;
    state.taskRunRenderedProposalId = null;
    state.taskRunRecoveryKey = null;
    state.taskRunRecoveryReadiness = null;
    renderTaskRun({ status: "idle", message: "No sandboxed task is active." });
    $("taskRunPhases").replaceChildren();
    $("taskRunBranch").value = "";
    $("taskRunInputAnswer").value = "";
  }
  [
    "diffOutput", "workingDiff", "githubContent", "prReadinessContent", "deliveryContent",
    "historyContent", "historyDetail", "llmInput", "llmOutput", "llmReview",
    "llmTraceList", "jsonOutput",
  ].forEach((id) => $(id).replaceChildren());
  $("summaryEmpty").classList.remove("hidden");
  $("summaryResults").classList.add("hidden");
  $("applyProposal").disabled = true;
  $("revertProposal").disabled = true;
  $("generateRepairProposal").disabled = true;
  updateCreatePullRequestState(null);
  updateRuntimeApprovalControls(null);
  updateApprovalState();
  $("rawWorkingDiff").hidden = true;
  $("runtimeDetails").hidden = true;
  updateDiffMode();
  updateWorkspaceContext();
  updateRefreshState();
}

async function loadGithub() {
  $("githubContent").innerHTML = item("Loading GitHub status...");
  const data = await getJson(`/api/github/status?${repositoryQuery()}&limit=8`);
  if (!data) return false;
  if (data.error) throw new Error(data.error);
  state.github = data;
  renderGithub(data);
  return true;
}

async function loadDiff(staged) {
  const request = ++state.diffRequest;
  state.diffStaged = Boolean(staged);
  updateDiffMode();
  $("workingDiff").innerHTML = '<p class="empty-inline">Loading changes...</p>';
  $("rawWorkingDiff").hidden = true;
  try {
    const data = await getJson(`/api/git/diff?${repositoryQuery()}&staged=${staged ? "true" : "false"}`);
    if (!data || request !== state.diffRequest) return false;
    if (data.error) throw new Error(data.error);
    renderWorkingDiff(data.diff || "");
    updateRepositorySourceStatus(data.repository_source);
    return true;
  } catch (error) {
    if (request !== state.diffRequest) return false;
    $("workingDiff").innerHTML = `<p class="empty-inline" data-tone="danger">${escapeHtml(error.message)}</p>`;
    throw error;
  }
}

function updateDiffMode() {
  [["loadDiff", !state.diffStaged], ["loadStagedDiff", state.diffStaged]].forEach(([id, selected]) => {
    $(id).classList.toggle("active", selected);
    $(id).setAttribute("aria-pressed", String(selected));
  });
}

function renderWorkingDiff(diff) {
  $("diffOutput").textContent = diff || "No diff.";
  $("rawWorkingDiff").hidden = !diff;
  $("workingDiff").innerHTML = DiffView.render(diff, { id: "working" });
}

async function generateDelivery() {
  setStatus("Generating delivery draft...");
  try {
    const data = await postJson("/api/git/summary", {
      ...buildRepositoryPayload(),
      validation_notes: buildValidationNotes(),
    });
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    state.delivery = data;
    renderDelivery(data);
    state.deliveryRepository = buildRepositoryPayload();
    setStatus("Delivery draft ready.");
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

async function loadPrReadiness() {
  setStatus("Checking PR readiness...");
  updateCreatePullRequestState(null);
  try {
    const data = await postJson("/api/github/pr/readiness", buildRepositoryPayload());
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    $("prReadinessContent").innerHTML = renderPrReadiness(data.pr_readiness);
    updateCreatePullRequestState(data.pr_readiness);
    setStatus(data.pr_readiness?.ready ? "PR readiness passed." : "PR readiness needs attention.");
  } catch (error) {
    updateCreatePullRequestState(null);
    $("prReadinessContent").innerHTML = item(`PR readiness unavailable: ${escapeHtml(error.message)}`);
    setStatus(`Error: ${error.message}`);
  }
}

async function createPullRequest() {
  const delivery = state.delivery;
  const readiness = delivery?.pr_readiness;
  const draft = delivery?.pull_request;
  if (!state.deliveryRepository || repositoryKey(state.deliveryRepository) !== repositoryKey()) {
    setStatus("Generate a delivery draft for the current repository selection.");
    return;
  }
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
      ...state.deliveryRepository,
      confirm_create: true,
      title: draft.title,
      body: draft.body,
      base_branch: readiness.base_branch,
    });
    if (!data) return;
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
    if (!data) return false;
    if (data.error) {
      throw new Error(data.error);
    }
    updateRepositorySourceStatus(data.repository_source);
    renderHistory(data.runs || []);
    return true;
  } catch (error) {
    $("historyContent").innerHTML = item(`History unavailable: ${escapeHtml(error.message)}`);
    throw error;
  }
}

async function loadHistoryDetail(runId) {
  $("historyDetail").innerHTML = item("Loading run detail...");
  state.historyTrajectory = null;
  try {
    const data = await getJson(`/api/history/run?${repositoryQuery()}&id=${encodeURIComponent(runId)}`);
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    renderHistoryDetail(data);
    $("historyDetail").setAttribute("tabindex", "-1");
    revealWorkspace("historyDetail");
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
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    $("historyDetail").innerHTML = "";
    state.historyTrajectory = null;
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
    if (!data) return;
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
    if (!data) return;
    if (data.error) {
      throw new Error(data.error);
    }
    $("historyDetail").innerHTML = "";
    state.historyTrajectory = null;
    await loadHistory();
    setStatus(`Cleared ${data.deleted || 0} saved run(s).`);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function renderReport(report, payload) {
  state.reportPayload = payload;
  const proposal = report.patch_proposal;
  const reviewKey = JSON.stringify([
    report.proposal_id || null,
    (proposal?.file_edits || []).map((edit) => [edit.path, edit.new_content]),
    editableProposalPaths(proposal),
    proposal?.proposed_diff || "",
  ]);
  if (reviewKey !== state.proposalReviewKey) {
    const revisedProposal = state.proposalId && state.proposalId === report.proposal_id;
    state.approvedPaths = revisedProposal ? new Set() : new Set(editableProposalPaths(proposal));
    state.proposalApplied = false;
    state.rollbackAvailable = false;
    state.proposalReviewKey = reviewKey;
  }
  state.proposalId = report.proposal_id || null;
  const repairInProgress = ["diagnosing", "replanning"].includes(report.task_run?.status);
  state.repairParentId = report.validation_feedback && state.proposalId
    && !report.repair_budget_exhausted && !report.repair_stop_reason && !repairInProgress
    ? state.proposalId : null;
  if (typeof report.rollback_available === "boolean") {
    state.rollbackAvailable = report.rollback_available;
  }
  $("summaryEmpty").classList.add("hidden");
  $("summaryResults").classList.remove("hidden");
  $("runSummary").textContent = report.summary || "Repository analysis complete.";
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
  $("runtimeDetails").hidden = !runtimeApproval;
  if (runtimeApproval) {
    $("runtimeDetails").open = true;
  }
  updateRuntimeApprovalControls(runtimeApproval);
  $("proposalReview").hidden = Boolean(runtimeApproval);
  $("runtimeResultsDetails").hidden = !report.agent_write_result && !report.agent_validation_cycle;
  $("runtimeWriteResult").innerHTML = renderRuntimeWriteResult(report.agent_write_result);
  $("agentValidationCycle").innerHTML = renderAgentValidationCycle(
    report.agent_validation_cycle,
    report.agent_validation_results
  );
  $("repositoryMapList").innerHTML = renderRepositoryMap(report.repository_map);
  $("repositoryInstructionsList").innerHTML = renderRepositoryInstructions(
    report.repository_instructions
  );
  $("acceptanceCriteriaList").innerHTML = renderAcceptanceCriteria(
    report.acceptance_criteria || [],
    report.completion_evidence
  );
  $("executionBudgetList").innerHTML = renderExecutionBudget(report.execution_budget);
  $("completionEvidenceList").innerHTML = renderCompletionEvidence(report.completion_evidence);
  $("repairLoopList").innerHTML = renderRepairLoop(report);
  $("planList").innerHTML = (report.plan || []).map((step) => `<li><strong>${escapeHtml(step.title)}</strong><p>${escapeHtml(step.detail)}</p></li>`).join("");
  $("planDetails").hidden = !report.plan?.length;
  $("proposalObjective").textContent = proposal?.objective || "";
  const closedFiles = new Set([...($("proposalList").querySelectorAll?.(".diff-file:not([open])") || [])].map((file) => file.dataset.filePath));
  $("proposalList").innerHTML = renderProposals(proposal);
  $("proposalList").querySelectorAll?.(".diff-file").forEach((file) => {
    if (closedFiles.has(file.dataset.filePath)) file.open = false;
  });
  $("proposalChecks").innerHTML = (proposal?.risks || []).map((risk) => `<div class="review-risk ${risk.level === "high" ? "danger" : "warn"}"><strong>${escapeHtml(capitalize(risk.level))} risk</strong><p>${escapeHtml(risk.message)}</p><p class="section-note">${escapeHtml(risk.mitigation)}</p></div>`).join("")
    + (proposal?.validation_plan ? `<details class="review-notes"><summary>Validation plan</summary>${renderValidationPlan(proposal.validation_plan)}</details>` : "")
    + (proposal?.safety_check ? `<details class="review-notes"><summary>Safety check: ${proposal.safety_check.ok ? "passed" : "blocked"}</summary>${renderSafetyCheck(proposal.safety_check)}</details>` : "");
  $("memoryContext").innerHTML = report.memory_context?.length ? renderMemoryContext(report.memory_context) : "";
  setApprovalInputsDisabled(state.proposalApplied);
  $("proposalOutput").textContent = JSON.stringify(
    {
      memory_context: report.memory_context,
      metadata: report.patch_proposal_metadata,
      proposal: report.patch_proposal,
    },
    null,
    2
  );
  $("proposedDiffOutput").textContent = report.patch_proposal?.proposed_diff || "No proposed diff.";
  updateApprovalState();
  $("revertProposal").disabled = !state.rollbackAvailable;
  $("rollbackStatus").textContent = state.rollbackAvailable
    ? "Rollback snapshot available for this applied proposal."
    : state.proposalId
      ? "Proposal is stored server-side; rollback becomes available after apply."
      : "No rollback snapshot available.";
  $("validationList").innerHTML = renderValidation(report.validation);
  const blockers = reportBlockers(report);
  const validation = report.validation || [];
  const passed = validation.filter((result) => result.exit_code === 0).length;
  $("changedFileCount").textContent = new Set(runtimeApproval?.file_scope || proposal?.files?.map((file) => file.path) || []).size;
  $("validationCount").textContent = validation.length ? `${passed}/${validation.length} passed` : "Not run";
  $("validationCount").dataset.tone = validation.length ? passed === validation.length ? "ok" : "danger" : "neutral";
  $("blockerCount").textContent = blockers.length;
  $("blockerCount").dataset.tone = blockers.length ? "danger" : "neutral";
  $("summaryBlockers").hidden = !blockers.length;
  $("summaryBlockers").innerHTML = `<strong>Needs attention</strong><ul>${blockers.map((blocker) => `<li>${escapeHtml(blocker)}</li>`).join("")}</ul>`;
  $("validationFeedbackList").innerHTML = renderValidationFeedback(report.validation_feedback, report);
  if (report.validation_feedback || report.repair_stop_reason || report.repair_budget_exhausted) {
    $("repairDetails").open = true;
  }
  $("generateRepairProposal").disabled = !state.repairParentId;
  $("llmInput").textContent = buildLlmInputPreview(report, payload);
  $("llmOutput").textContent = buildLlmOutputPreview(report);
  $("llmReview").textContent = JSON.stringify(report.patch_review || {}, null, 2);
  $("llmTraceList").innerHTML = renderLlmTraces(report.llm_traces || []);
  $("jsonOutput").textContent = JSON.stringify(report, null, 2);
  setTrajectory(
    hasTrajectory(report.agent_trajectory)
      ? report.agent_trajectory
      : trajectoryFromEvents(
          report.agent_events || [],
          report.agent_run_id || "",
          report.agent_stop_reason || ""
        )
  );
  updateWorkspaceContext();
  updateRefreshState();
}

function hasTrajectory(value) {
  return Boolean(value && typeof value === "object" && Array.isArray(value.frames));
}

function setTrajectory(trajectory, { preserveTail = false } = {}) {
  const next = hasTrajectory(trajectory) ? trajectory : trajectoryFromEvents([], "", "");
  const previousFrames = state.trajectory?.frames || [];
  const wasAtTail = !previousFrames.length || state.trajectoryIndex >= previousFrames.length - 1;
  const sameFingerprint = Boolean(
    state.trajectory
      && state.trajectory.fingerprint
      && state.trajectory.fingerprint === next.fingerprint
  );
  state.trajectory = next;
  const frameCount = next.frames.length;
  if (sameFingerprint && !(preserveTail && wasAtTail)) {
    state.trajectoryIndex = Math.min(state.trajectoryIndex, Math.max(frameCount - 1, 0));
  } else {
    state.trajectoryIndex = Math.max(frameCount - 1, 0);
  }
  renderTrajectory();
}

function renderTrajectory() {
  const trajectory = state.trajectory || trajectoryFromEvents([], "", "");
  const metrics = trajectory.metrics || {};
  const llm = metrics.llm || {};
  const frames = trajectory.frames || [];
  const evidenceCoverage = Number.isFinite(metrics.evidence_coverage)
    ? `${Math.round(metrics.evidence_coverage * 100)}%`
    : "n/a";
  $("trajectoryEvents").textContent = trajectory.event_count ?? frames.length;
  $("trajectoryTools").textContent = metrics.tool_calls ?? 0;
  $("trajectoryEvidence").textContent = evidenceCoverage;
  $("trajectoryTokens").textContent = Number(llm.total_tokens || 0).toLocaleString();
  $("trajectoryTokens").title = `Token source: ${llm.token_source || "none"}`;
  $("trajectoryTokenSource").textContent = llm.token_source && llm.token_source !== "none"
    ? `tokens (${llm.token_source})`
    : "tokens";
  $("trajectoryRecovery").textContent = metrics.recovery_events ?? 0;
  $("trajectoryStop").textContent = metrics.stop_reason || (frames.length ? "in progress" : "not recorded");
  $("trajectoryIntegrity").innerHTML = renderTrajectoryIntegrity(trajectory);
  $("trajectoryActionList").innerHTML = renderTrajectoryActions(metrics.action_sequence || []);

  const hasFrames = frames.length > 0;
  $("trajectoryCursor").disabled = !hasFrames;
  $("trajectoryCursor").max = String(Math.max(frames.length - 1, 0));
  $("trajectoryCursor").value = String(Math.min(state.trajectoryIndex, Math.max(frames.length - 1, 0)));
  ["trajectoryFirst", "trajectoryPrevious", "trajectoryPlay", "trajectoryNext", "trajectoryLast"]
    .forEach((id) => {
      $(id).disabled = !hasFrames;
    });
  renderTrajectoryFrame();
}

function renderTrajectoryIntegrity(trajectory) {
  const integrity = trajectory.integrity || {};
  const valid = integrity.valid !== false;
  const gaps = (integrity.sequence_gaps || [])
    .map((gap) => `${gap.after}-${gap.before}`)
    .join(", ");
  const duplicates = (integrity.duplicate_sequences || []).join(", ");
  const omitted = Number(trajectory.omitted_frames || 0);
  const fingerprint = trajectory.fingerprint || "unavailable";
  return `<div class="trajectory-integrity-row">
    <span class="tag ${valid ? "ok" : "danger"}">${valid ? "verified" : "invalid sequence"}</span>
    <span>Schema v${escapeHtml(trajectory.schema_version ?? "legacy")}</span>
    <span>${escapeHtml(trajectory.frame_count ?? 0)} replay frame(s)${omitted ? `, ${escapeHtml(omitted)} omitted` : ""}</span>
    <code title="${escapeHtml(fingerprint)}">${escapeHtml(shortFingerprint(fingerprint))}</code>
    ${gaps ? `<span>Gaps: ${escapeHtml(gaps)}</span>` : ""}
    ${duplicates ? `<span>Duplicates: ${escapeHtml(duplicates)}</span>` : ""}
  </div>`;
}

function renderTrajectoryActions(actions) {
  if (!actions.length) {
    return item("No Agent decisions were recorded.");
  }
  return actions
    .map((action, index) => `<div class="trajectory-action">
      <span>${escapeHtml(index + 1)}</span>
      <strong>${escapeHtml(action)}</strong>
    </div>`)
    .join("");
}

function renderTrajectoryFrame() {
  const frames = state.trajectory?.frames || [];
  if (!frames.length) {
    $("trajectoryPosition").textContent = "0 / 0";
    $("trajectoryFrame").innerHTML = item("No trajectory frames are available.");
    return;
  }
  state.trajectoryIndex = Math.min(Math.max(state.trajectoryIndex, 0), frames.length - 1);
  const frame = frames[state.trajectoryIndex];
  $("trajectoryCursor").value = String(state.trajectoryIndex);
  $("trajectoryPosition").textContent = `${state.trajectoryIndex + 1} / ${frames.length}`;
  const statusClass = ["completed", "applied", "passed", "finished"].includes(frame.status)
    ? "ok"
    : ["failed", "conflict", "policy_denied"].includes(frame.status)
      ? "danger"
      : "warn";
  const elapsed = Number.isInteger(frame.elapsed_ms) ? `${frame.elapsed_ms} ms` : "n/a";
  $("trajectoryFrame").innerHTML = `<div class="trajectory-frame-header">
      <div>
        <strong>#${escapeHtml(frame.sequence)} ${escapeHtml(frame.event_type || "event")}</strong>
        <span class="tag">${escapeHtml(frame.category || "runtime")}</span>
        <span class="tag ${statusClass}">${escapeHtml(frame.status || "recorded")}</span>
      </div>
      <small>+${escapeHtml(elapsed)}</small>
    </div>
    <dl class="trajectory-frame-grid">
      <div><dt>Action</dt><dd>${escapeHtml(frame.action_kind || "none")}</dd></div>
      <div><dt>Action ID</dt><dd>${escapeHtml(frame.action_id || "none")}</dd></div>
      <div><dt>Tool cost</dt><dd>${escapeHtml(frame.tool_call_cost || 0)}</dd></div>
      <div><dt>Replay</dt><dd>${frame.replayed ? "yes" : "no"}</dd></div>
    </dl>
    <p>${escapeHtml(frame.summary || "No bounded summary was recorded.")}</p>`;
}

function moveTrajectory(target) {
  const frames = state.trajectory?.frames || [];
  if (!frames.length) {
    return;
  }
  if (target === "first") {
    state.trajectoryIndex = 0;
  } else if (target === "last") {
    state.trajectoryIndex = frames.length - 1;
  } else {
    state.trajectoryIndex = Math.min(
      Math.max(state.trajectoryIndex + Number(target || 0), 0),
      frames.length - 1
    );
  }
  renderTrajectoryFrame();
  if (state.trajectoryTimer && state.trajectoryIndex >= frames.length - 1) {
    stopTrajectoryPlayback();
  }
}

function toggleTrajectoryPlayback() {
  if (state.trajectoryTimer) {
    stopTrajectoryPlayback();
    return;
  }
  const frames = state.trajectory?.frames || [];
  if (!frames.length) {
    return;
  }
  if (state.trajectoryIndex >= frames.length - 1) {
    state.trajectoryIndex = 0;
    renderTrajectoryFrame();
  }
  $("trajectoryPlay").textContent = "||";
  $("trajectoryPlay").title = "Pause trajectory";
  $("trajectoryPlay").setAttribute("aria-label", "Pause trajectory");
  state.trajectoryTimer = window.setInterval(() => moveTrajectory(1), 700);
}

function stopTrajectoryPlayback() {
  if (state.trajectoryTimer) {
    window.clearInterval(state.trajectoryTimer);
    state.trajectoryTimer = null;
  }
  const button = $("trajectoryPlay");
  if (button) {
    button.textContent = ">";
    button.title = "Play trajectory";
    button.setAttribute("aria-label", "Play trajectory");
  }
}

function trajectoryFromEvents(events, runId, stopReason) {
  const records = Array.isArray(events) ? events : [];
  const frames = records.map((event, index) => {
    const payload = event.payload || {};
    const observation = payload.observation || {};
    const action = payload.action || {};
    const eventType = event.event_type || "unknown";
    return {
      sequence: Number.isInteger(event.sequence) ? event.sequence : index + 1,
      elapsed_ms: null,
      category: trajectoryEventCategory(eventType),
      event_type: eventType,
      action_id: event.action_id || observation.action_id || "",
      action_kind: observation.action_kind || action.kind || "",
      status: observation.status || payload.status || "recorded",
      summary: observation.summary || payload.summary || payload.reason || eventType.replaceAll("_", " "),
      tool_call_cost: ["action_started", "action_recovery_started"].includes(eventType)
        ? Math.max(Number(payload.tool_call_cost || 1), 1)
        : 0,
      replayed: Boolean(observation.replayed) || eventType === "action_replayed",
    };
  });
  const actionSequence = frames
    .filter((frame) => frame.event_type === "decision_recorded" && frame.action_kind)
    .map((frame) => frame.action_kind);
  const sequences = frames.map((frame) => frame.sequence);
  const sequenceValid = sequences.every(
    (sequence, index) => sequence === index + 1
  );
  const eventCounts = records.reduce((counts, event) => {
    const name = event.event_type || "unknown";
    counts[name] = (counts[name] || 0) + 1;
    return counts;
  }, {});
  return {
    schema_version: 0,
    run_id: runId || "",
    fingerprint: frames.length ? `legacy-${runId || "run"}-${frames.length}` : "unavailable",
    event_count: frames.length,
    frame_count: frames.length,
    omitted_frames: 0,
    integrity: {
      valid: sequenceValid,
      ordered: sequenceValid,
      starts_at_one: !frames.length || frames[0].sequence === 1,
      duplicate_sequences: [],
      sequence_gaps: [],
    },
    metrics: {
      stop_reason: stopReason || "",
      action_sequence: actionSequence,
      event_counts: eventCounts,
      tool_calls: frames.reduce((total, frame) => total + frame.tool_call_cost, 0),
      recovery_events: frames.filter((frame) => frame.category === "recovery").length,
      evidence_coverage: null,
      llm: { total_tokens: 0, token_source: "none" },
    },
    frames,
  };
}

function trajectoryEventCategory(eventType) {
  if (eventType === "decision_recorded") return "decision";
  if (eventType.startsWith("approval_")) return "approval";
  if (eventType.startsWith("input_")) return "interaction";
  if (eventType.includes("recovery") || eventType === "action_replayed") return "recovery";
  if (eventType.startsWith("working_state_")) return "state";
  if (eventType.startsWith("repair_")) return "repair";
  if (eventType.startsWith("run_")) return "lifecycle";
  if (eventType.startsWith("action_")) return "execution";
  return "runtime";
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

function renderRepositoryInstructions(repositoryInstructions) {
  const value = repositoryInstructions || {};
  const files = Array.isArray(value.files) ? value.files : [];
  const issues = Array.isArray(value.issues) ? value.issues : [];
  if (!files.length && !issues.length) {
    return item("No applicable repository AGENTS.md instructions were found.");
  }
  const summary = value.summary
    ? `<div class="item"><p>${escapeHtml(value.summary)}</p></div>`
    : "";
  const sources = files.map((file) => {
    const scope = file.scope === "." ? "repository-wide" : `${file.scope}/**`;
    const hash = shortFingerprint(String(file.content_sha256 || ""));
    return `<div class="item">
      <div class="item-title">${escapeHtml(file.path || "AGENTS.md")}
        <span class="tag">scope ${escapeHtml(scope)}</span>
        <span class="tag">precedence ${escapeHtml(file.precedence || "?")}</span>
        ${file.truncated ? '<span class="tag warn">truncated</span>' : ""}
      </div>
      <p><small>SHA-256 ${escapeHtml(hash)}</small></p>
    </div>`;
  }).join("");
  const skipped = issues.map((issue) => `<li>${escapeHtml(issue.path || "AGENTS.md")}: ${escapeHtml(issue.reason || "skipped")}</li>`).join("");
  const guidance = value.text
    ? `<div class="item repository-instruction-guidance">
        <details><summary>Bounded redacted guidance</summary><pre>${escapeHtml(value.text)}</pre></details>
      </div>`
    : "";
  return `${summary}${sources}${guidance}${skipped ? `<div class="item"><strong>Skipped files</strong><ul>${skipped}</ul></div>` : ""}`;
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
      const toolCallCount = Number.isInteger(step.tool_call_count)
        ? Math.max(step.tool_call_count, 1)
        : 1;
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
      <div class="item-title">Step ${escapeHtml(step.order)}: ${escapeHtml(step.action)}${toolCallCount > 1 ? ` <span class="tag">${escapeHtml(toolCallCount)} read tools</span>` : ""}</div>
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
      <span class="timeline-step">Virtual edit</span>
      <span class="timeline-status">revision ${escapeHtml(value.revision ?? 0)} | ${escapeHtml(value.status || "proposed")}</span>
      <span><code>${escapeHtml(value.path || "file")}</code><br>${value.inspected ? "Inspected" : "Inspection required"} | ${escapeHtml(value.hunk_count ?? 0)} cumulative hunk(s) | SHA-256 ${escapeHtml(value.current_sha256 || "unknown")}</span>
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
    <div class="timeline-event timeline-property">
      <span class="timeline-step">Objective</span>
      <span>${escapeHtml(agentState.objective)}</span>
    </div>
    <div class="timeline-event timeline-property">
      <span class="timeline-step">Focus</span>
      <span>${escapeHtml(agentState.focus || "none")}</span>
    </div>
    <div class="timeline-event timeline-property">
      <span class="timeline-step">Selected paths</span>
      <span>${escapeHtml(selectedPaths.join(", ") || "none")}</span>
    </div>
    <div class="timeline-event timeline-property">
      <span class="timeline-step">Findings</span>
      <span>${escapeHtml(findings.join(" | ") || "none")}</span>
    </div>
    <div class="timeline-event timeline-property">
      <span class="timeline-step">Open questions</span>
      <span>${escapeHtml(openQuestions.join(" | ") || "none")}</span>
    </div>
    <div class="timeline-event timeline-property">
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
    ${proposedDiff ? `<div class="timeline-event timeline-property">
      <span class="timeline-step">Cumulative virtual diff</span>
      <pre>${escapeHtml(proposedDiff)}</pre>
    </div>` : ""}
    ${pendingQuestion ? `<div class="timeline-event timeline-property">
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
      const toolCallCost = Number.isInteger(event.payload?.tool_call_cost)
        ? event.payload.tool_call_cost
        : 0;
      const costLabel = toolCallCost > 1 ? ` | ${toolCallCost} read tools` : "";
      return `<div class="timeline-event runtime-event">
        <span class="timeline-step">#${escapeHtml(event.sequence)}</span>
        <span class="timeline-status">${escapeHtml(event.event_type || "event")}</span>
        <span>${escapeHtml(event.action_id || detail || "runtime")}${event.action_id && detail ? ` - ${escapeHtml(detail)}` : ""}${escapeHtml(costLabel)}</span>
      </div>`;
    })
    .join("");
  return `${header}${rows}`;
}

function renderRuntimeApproval(request, diffId = "runtime") {
  if (!request || !request.checkpoint) {
    return item("No runtime side effect is waiting for approval.");
  }
  const fileScope = (request.file_scope || []).map((path) => `<li>${escapeHtml(path)}</li>`).join("");
  const commandScope = (request.command_allowlist || []).map((command) => `<li><code>${escapeHtml(command)}</code></li>`).join("");
  return `<div class="runtime-approval">
    <div class="item-title">${request.action_kind === "validate" ? "Validation command" : "Pending write"}</div>
    ${fileScope ? `<strong>File scope</strong><ul>${fileScope}</ul>` : ""}
    ${commandScope ? `<strong>Command scope</strong><ul>${commandScope}</ul>` : ""}
    ${request.diff ? `<h2>${request.diff_truncated ? "Bounded diff (truncated)" : "Exact diff"}</h2>${DiffView.render(request.diff, { id: diffId })}<details class="raw-diff"><summary>Raw exact diff</summary><pre>${escapeHtml(request.diff)}</pre></details>` : ""}
    <details class="review-notes">
      <summary>Approval record and exact action</summary>
      <p><strong>Checkpoint</strong> <code>${escapeHtml(request.checkpoint)}</code></p>
      <p><strong>Payload SHA-256</strong> <code>${escapeHtml(request.payload_hash || "")}</code></p>
      <p><strong>Action</strong> ${escapeHtml(request.action_kind || "action")} ${escapeHtml(request.action_id || "")}</p>
      <pre>${escapeHtml(JSON.stringify(request.action || {}, null, 2))}</pre>
    </details>
  </div>`;
}

function updateRuntimeApprovalControls(request = currentRuntimeApproval()) {
  const busy = state.busyActions.has("approveRuntimeWrite") || state.busyActions.has("rejectRuntimeWrite");
  const available = Boolean(request?.checkpoint && state.taskRun?.can_approve_runtime)
    && !busy;
  $("approveRuntimeWrite").disabled = !available;
  $("rejectRuntimeWrite").disabled = !available;
  if (!state.busyActions.has("approveRuntimeWrite")) $("approveRuntimeWrite").textContent = request?.action_kind === "validate"
    ? "Run Exact Validation"
    : "Approve Exact Write";
  $("runtimeApprovalStatus").textContent = busy
    ? state.busyActions.has("rejectRuntimeWrite") ? "Rejecting the exact action..." : "Executing the exact approved action..."
    : available
    ? request.action_kind === "validate"
      ? "One exact validation command in the managed worktree."
      : `${request.file_scope?.length || 0} file(s) in the managed worktree.`
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
  $("applyProposal").disabled = !state.proposalId || state.proposalApplied || !state.lastReport?.patch_proposal?.apply_ready || total === 0 || selected === 0
    || Boolean(currentRuntimeApproval()) || state.busyActions.has("applyProposal") || state.busyActions.has("revertProposal");
  if (!state.busyActions.has("applyProposal")) $("applyProposal").textContent = state.proposalApplied ? "Changes applied" : `Apply ${selected} file${selected === 1 ? "" : "s"}`;
  $("revertProposal").disabled = !state.rollbackAvailable || state.busyActions.has("applyProposal") || state.busyActions.has("revertProposal");
  $("revertProposal").hidden = !state.rollbackAvailable;
  $("rollbackStatus").hidden = !state.rollbackAvailable;
  setApprovalInputsDisabled(state.proposalApplied || state.busyActions.has("applyProposal") || state.busyActions.has("revertProposal"));
  if ($("approvalStatus")) {
    $("approvalStatus").textContent = total
      ? state.proposalApplied ? `${selected} file(s) applied.` : `${selected} of ${total} apply-ready file(s) selected.`
      : "No apply-ready file edits.";
  }
  updateWorkspaceContext();
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
  return DiffView.render(proposal.proposed_diff || "", {
    id: "proposal", files: proposal.files, edits: proposal.file_edits || [],
    selected: state.approvedPaths, disabled: state.proposalApplied,
  });
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
    return '<p class="empty-inline">No validation commands were run.</p>';
  }
  return results
    .map((result) => `<details class="validation-result" ${result.exit_code !== 0 ? "open" : ""}>
      <summary><code>${escapeHtml(result.command)}</code><span class="status-badge ${result.exit_code === 0 ? "ok" : "danger"}">${icon(result.exit_code === 0 ? "circle-check" : "circle-x")}${result.exit_code === 0 ? "Passed" : result.exit_code == null ? "Rejected" : `Failed (${escapeHtml(result.exit_code)})`}</span></summary>
      <pre>${escapeHtml([result.stdout, result.stderr].filter(Boolean).join("\n") || "No output.")}</pre>
    </details>`)
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
  if (state.delivery) {
    state.delivery.pr_readiness = readiness;
  }
  $("createPullRequest").disabled = !readiness?.ready || !draft?.title || !draft?.body || state.busyActions.has("createPullRequest");
  const hasDraft = Boolean(draft?.title && draft?.body);
  $("generateDelivery").classList.toggle("secondary", hasDraft);
  $("loadPrReadiness").classList.toggle("secondary", !hasDraft || Boolean(readiness?.ready));
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
    .map((run) => `<div class="item history-row">
      <div class="history-heading">
        <div><div class="item-title">${escapeHtml(run.task)}</div><small>${escapeHtml(formatTimestamp(run.created_at))}</small></div>
        <div class="toolbar">
          <button id="historyPin-${escapeHtml(run.id)}" class="secondary icon-button" data-history-pin="${escapeHtml(run.id)}" data-history-pinned="${run.pinned ? "false" : "true"}" title="${run.pinned ? "Unpin" : "Pin"} run" aria-label="${run.pinned ? "Unpin" : "Pin"} run">${icon(run.pinned ? "pin-off" : "pin")}</button>
          <details class="action-menu"><summary title="Run actions" aria-label="Run actions">${icon("ellipsis")}</summary><div>
            <button class="secondary" data-task="${escapeHtml(run.task)}">Use as task</button>
            <button id="historyDelete-${escapeHtml(run.id)}" class="secondary danger-button" data-history-delete="${escapeHtml(run.id)}">Delete</button>
          </div></details>
        </div>
      </div>
      <p>${escapeHtml(run.summary || "")}</p>
      <div class="toolbar"><button class="secondary" data-history-id="${escapeHtml(run.id)}">Open run</button><span class="tag">${escapeHtml(run.mode)}</span><span class="tag ${run.applied ? "ok" : ""}">${run.applied ? "applied" : "open"}</span>${run.pinned ? '<span class="tag">pinned</span>' : ""}</div>
    </div>`)
    .join("");
}

function formatTimestamp(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value || "" : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
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
  state.historyTrajectory = hasTrajectory(run.agent_trajectory)
    ? run.agent_trajectory
    : trajectoryFromEvents(
        run.agent_events || [],
        run.agent_runtime_run_id || "",
        agentStopReason
      );
  const pinnedTag = run.pinned ? ' <span class="tag ok">pinned</span>' : "";
  $("historyDetail").innerHTML = `
    <div class="item">
      <div class="item-title">${escapeHtml(run.task)}${pinnedTag}</div>
      <p>${escapeHtml(run.summary || "")}</p>
      <p><small>${escapeHtml(run.created_at)} | ${escapeHtml(run.mode)} | ${escapeHtml(run.id)}</small></p>
      <div class="toolbar">
        <button class="secondary" data-open-trajectory="history">Open Trajectory</button>
      </div>
    </div>
    <div class="item">
      <div class="item-title">Timeline</div>
      <ul>${timeline || "<li>No timeline saved.</li>"}</ul>
    </div>
    <div class="item">
      <div class="item-title">Proposal Diff</div>
      <pre>${escapeHtml(run.proposal?.proposed_diff || "No proposed diff saved.")}</pre>
    </div>
    <h3>Repository Instructions</h3>
    ${renderRepositoryInstructions(run.repository_instructions)}
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
    ${renderRuntimeApproval(agentPendingApproval, "history-runtime")}
    <div class="item">
      <div class="item-title">Validation</div>
      <ul>${validation || "<li>No validation saved.</li>"}</ul>
    </div>
  `;
}

function renderSavedTrace(trace) {
  const usage = formatTraceUsage(trace);
  return `<details class="trace-details">
    <summary>${escapeHtml(trace.name || "trace")} ${trace.model ? `<span class="tag">${escapeHtml(trace.model)}</span>` : ""} <span class="tag ${trace.parsed ? "ok" : "danger"}">${trace.parsed ? "parsed" : "failed"}</span></summary>
    <p>${escapeHtml(trace.error || `Latency: ${trace.latency_ms ?? 0} ms`)}</p>
    ${usage ? `<p><small>${escapeHtml(usage)}</small></p>` : ""}
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
      ${formatTraceUsage(trace) ? `<p><small>${escapeHtml(formatTraceUsage(trace))}</small></p>` : ""}
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

function formatTraceUsage(trace) {
  if (!Number.isInteger(trace?.total_tokens)) {
    return "";
  }
  return `Tokens: ${trace.total_tokens} total (${trace.input_tokens || 0} input, ${trace.output_tokens || 0} output)`;
}

function renderGithub(data) {
  updateRepositorySourceStatus(data.repository_source);
  if (data.error || data.unavailable_reason) {
    $("githubContent").innerHTML = item(escapeHtml(data.error || data.unavailable_reason));
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
  const target = event.target.closest?.("button") || event.target;
  if (!target.matches) return;
  if ($("viewMenu").open && !$("viewMenu").contains(target)) {
    $("viewMenu").open = false;
  }
  if ($("brandMenu").classList.contains("open") && !$("brandMenu").contains(target)) {
    setBrandMenuOpen(false);
  }
  if (target.matches("[data-task]")) {
    $("taskInput").value = target.dataset.task || "";
    setControlsCollapsed(false);
    $("taskInput").focus();
    setStatus("Task loaded into input.");
    return;
  }
  if (target.matches("[data-history-delete]")) {
    target.closest("details").open = false;
    withBusy(target.id, "Deleting...", () => deleteHistoryRun(target.dataset.historyDelete || ""));
    return;
  }
  if (target.matches("[data-history-pin]")) {
    withBusy(target.id, "", () => toggleHistoryPin(target.dataset.historyPin || "", target.dataset.historyPinned === "true"));
    return;
  }
  if (target.matches("[data-history-id]")) {
    loadHistoryDetail(target.dataset.historyId || "");
    return;
  }
  if (target.matches('[data-open-trajectory="history"]') && state.historyTrajectory) {
    setTrajectory(state.historyTrajectory);
    activateTab("trajectory");
  }
  if (target.matches("[data-diff-target]")) {
    target.closest("nav").querySelectorAll("[data-diff-target]").forEach((button) => button.setAttribute("aria-current", String(button === target)));
    const file = $(target.dataset.diffTarget);
    file.open = true;
    file.scrollIntoView({ block: "start" });
  }
  if (target.matches("[data-copy-path]")) {
    if (!navigator.clipboard) {
      setStatus("Error: Clipboard access is unavailable.", "danger");
      return;
    }
    navigator.clipboard.writeText(target.dataset.copyPath)
      .then(() => setStatus("File path copied.", "ok"))
      .catch(() => setStatus("Error: Could not copy the file path.", "danger"));
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
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function getJson(url) {
  return requestJson(url);
}

async function requestJson(url, options) {
  const generation = state.repositoryGeneration;
  const isCurrent = () => url === "/api/llm/test" || generation === state.repositoryGeneration;
  try {
    const response = await fetch(url, options);
    const data = await response.json();
    // Repository responses from a previous selection must not restore its controls.
    return isCurrent() ? data : null;
  } catch (error) {
    if (!isCurrent()) return null;
    throw error;
  }
}

function repositoryKey(repository = buildRepositoryPayload()) {
  return JSON.stringify([repository.repo_source, repository.repo, repository.github_url, repository.branch]);
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
  const githubUrl = $("repoSource").value === "github" ? $("githubUrl").value.trim() : "";
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
    github_url: $("repoSource").value === "github" ? $("githubUrl").value.trim() : "",
    branch: $("repoBranch").value.trim(),
  };
}

function updateRepositorySourceUi() {
  const source = $("repoSource").value;
  $("githubUrlWrap").classList.toggle("hidden", source !== "github");
  $("repoPathField").classList.toggle("hidden", source === "github");
  $("repoPathLabel").textContent = source === "auto" ? "Repository path or URL" : "Repository path";
  $("repoPath").placeholder = source === "auto" ? "Path or GitHub URL" : ".";
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

function setStatus(message, tone = "") {
  const resolved = tone || (message.startsWith("Error:") || message.startsWith("Task status error:") ? "danger" : message.endsWith("...") ? "busy" : "info");
  state.feedbackRevision += 1;
  state.feedbackTone = resolved;
  state.retryAction = null;
  $("statusLine").textContent = message;
  $("statusLine").dataset.tone = resolved;
  $("viewStatus").textContent = message;
  $("viewStatus").dataset.tone = resolved;
  $("viewFeedback").hidden = !message || message === "Ready.";
  $("retryView").hidden = true;
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

setTrajectory(null);
updateRepositorySourceUi();
selectRunMode("workflow");
updateLlmSettingsUi();
updateRefreshState();
