const state = {
  lastReport: null,
  github: null,
  delivery: null,
  proposalId: null,
  repairParentId: null,
  rollbackAvailable: false,
  proposalApplied: false,
  approvedPaths: new Set(),
};

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
$("testLlm").addEventListener("click", testLlmConnection);
$("applyProposal").addEventListener("click", applyProposal);
$("revertProposal").addEventListener("click", revertProposal);
$("syncRepository").addEventListener("click", syncRepository);
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
  await Promise.allSettled([loadGithub(), loadDiff(false), loadHistory()]);
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
  try {
    const result = await postJson("/api/apply", {
      ...buildRepositoryPayload(),
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
    state.repairParentId = result.validation_feedback && !result.repair_budget_exhausted ? state.proposalId : null;
    state.rollbackAvailable = Boolean(result.rollback_available);
    state.proposalApplied = true;
    $("generateRepairProposal").disabled = !state.repairParentId;
    $("revertProposal").disabled = !state.rollbackAvailable;
    $("rollbackStatus").textContent = state.rollbackAvailable
      ? "Rollback snapshot available for this applied proposal."
      : "No rollback snapshot available.";
    renderTimeline(result.timeline || []);
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
  try {
    const report = await postJson("/api/repair/propose", {
      ...buildWorkflowPayload(),
      proposal_id: state.repairParentId,
    });
    if (report.error) {
      throw new Error(report.error);
    }
    state.lastReport = report;
    renderReport(report, buildWorkflowPayload());
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
    max_repair_attempts: $("repairMaxAttempts").value.trim(),
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
  state.repairParentId = report.validation_feedback && state.proposalId && !report.repair_budget_exhausted ? state.proposalId : null;
  state.rollbackAvailable = Boolean(report.rollback_available);
  state.proposalApplied = false;
  state.approvedPaths = new Set(editableProposalPaths(report.patch_proposal));
  updateRepositorySourceStatus(report.repository_source);
  $("filesScanned").textContent = report.files_scanned;
  $("planSource").textContent = sourceLabel(report.plan_metadata);
  $("proposalSource").textContent = sourceLabel(report.patch_proposal_metadata);
  renderTimeline(report.timeline || []);
  $("agentStepList").innerHTML = renderAgentSteps(report.agent_steps || []);
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
    .map((step) => `<div class="item">
      <div class="item-title">Step ${escapeHtml(step.order)}: ${escapeHtml(step.action)}</div>
      <p>${escapeHtml(step.thought || "")}</p>
      <p><small>Input: ${escapeHtml(step.tool_input || "(none)")}</small></p>
      <pre>${escapeHtml(step.observation || "")}</pre>
      ${(step.selected_paths || []).length ? `<p><small>Selected: ${escapeHtml(step.selected_paths.join(", "))}</small></p>` : ""}
    </div>`)
    .join("");
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
  const repairTag = repairState.repair_budget_exhausted
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
  if (maxAttempts <= 0 || repairState.repair_budget_exhausted) {
    return `<p><strong>Repair budget:</strong> exhausted (${escapeHtml(currentAttempt)}/${escapeHtml(maxAttempts)}).</p>`;
  }
  const nextAttempt = repairState.next_repair_attempt ?? currentAttempt + 1;
  return `<p><strong>Repair budget:</strong> next attempt ${escapeHtml(nextAttempt)}/${escapeHtml(maxAttempts)}; ${escapeHtml(remaining)} remaining.</p>`;
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
  return `Repository source: ${payload.repo_source}\nRepository input: ${payload.repo}\nGitHub URL: ${payload.github_url || "(none)"}\nBranch: ${payload.branch || "(default)"}\nUse LLM: ${payload.use_llm}\nUse memory: ${payload.use_memory}\nIterative agent: ${payload.iterative_agent}\nAgent max steps: ${payload.agent_max_steps || "(default)"}\nModel: ${payload.model || "(default)"}\nTimeout: ${payload.timeout_seconds || "(default)"} seconds\nTask: ${payload.task}\n\nRelevant context:\n${context || "No context selected."}`;
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
loadHistory().catch(() => {});
