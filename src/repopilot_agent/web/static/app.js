const state = {
  lastReport: null,
  github: null,
  proposalId: null,
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

$("modelSelect").addEventListener("change", () => {
  $("customModelWrap").classList.toggle("hidden", $("modelSelect").value !== "custom");
});

$("runWorkflow").addEventListener("click", runWorkflow);
$("generateProposal").addEventListener("click", generateProposal);
$("applyProposal").addEventListener("click", applyProposal);
$("loadGithub").addEventListener("click", loadGithub);
$("loadDiff").addEventListener("click", () => loadDiff(false));
$("loadStagedDiff").addEventListener("click", () => loadDiff(true));
$("refreshAll").addEventListener("click", async () => {
  await Promise.allSettled([loadGithub(), loadDiff(false)]);
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

async function applyProposal() {
  const proposal = state.lastReport?.patch_proposal;
  if (!state.proposalId || !proposal?.apply_ready) {
    setStatus("No apply-ready proposal is available.");
    return;
  }
  const fileCount = (proposal.file_edits || []).length;
  const confirmed = window.confirm(`Apply proposal ${state.proposalId} with ${fileCount} file edit(s) to the working tree?`);
  if (!confirmed) {
    return;
  }

  setStatus("Applying proposal...");
  try {
    const result = await postJson("/api/apply", {
      proposal_id: state.proposalId,
    });
    if (result.error) {
      throw new Error(result.error);
    }
    $("diffOutput").textContent = result.diff || "No diff.";
    $("validationList").innerHTML = renderValidation(result.validation || []);
    renderTimeline(result.timeline || []);
    $("applyProposal").disabled = true;
    setStatus(result.message || "Proposal applied.");
    await loadDiff(false);
  } catch (error) {
    setStatus(`Error: ${error.message}`);
  }
}

function buildWorkflowPayload() {
  const validation = $("validationInput").value.trim();
  return {
    repo: $("repoPath").value.trim() || ".",
    task: $("taskInput").value.trim(),
    validation: validation ? [validation] : [],
    use_llm: $("useLlm").checked,
    model: selectedModel(),
    base_url: $("baseUrl").value.trim(),
    api_key: $("apiKey").value,
    no_llm_fallback: $("disableFallback").checked,
  };
}

async function loadGithub() {
  const repo = encodeURIComponent($("repoPath").value.trim() || ".");
  $("githubContent").innerHTML = item("Loading GitHub status...");
  const data = await getJson(`/api/github/status?repo=${repo}&limit=8`);
  state.github = data;
  renderGithub(data);
}

async function loadDiff(staged) {
  const repo = encodeURIComponent($("repoPath").value.trim() || ".");
  const data = await getJson(`/api/git/diff?repo=${repo}&staged=${staged ? "true" : "false"}`);
  $("diffOutput").textContent = data.diff || data.error || "No diff.";
}

function renderReport(report, payload) {
  state.proposalId = report.proposal_id || null;
  $("filesScanned").textContent = report.files_scanned;
  $("planSource").textContent = sourceLabel(report.plan_metadata);
  $("proposalSource").textContent = sourceLabel(report.patch_proposal_metadata);
  renderTimeline(report.timeline || []);
  $("planList").innerHTML = report.plan.map((step) => `<li class="item"><div class="item-title">${escapeHtml(step.title)}</div>${escapeHtml(step.detail)}</li>`).join("");
  $("proposalList").innerHTML = renderProposals(report.patch_proposal);
  $("proposalOutput").textContent = JSON.stringify(
    {
      metadata: report.patch_proposal_metadata,
      proposal: report.patch_proposal,
    },
    null,
    2
  );
  $("proposedDiffOutput").textContent = report.patch_proposal?.proposed_diff || "No proposed diff. Use LLM proposal generation for apply-ready edits.";
  $("applyProposal").disabled = !state.proposalId || !report.patch_proposal?.apply_ready || !(report.patch_proposal.file_edits || []).length;
  $("validationList").innerHTML = renderValidation(report.validation);
  $("llmInput").textContent = buildLlmInputPreview(report, payload);
  $("llmOutput").textContent = buildLlmOutputPreview(report);
  $("jsonOutput").textContent = JSON.stringify(report, null, 2);
}

function renderProposals(proposal) {
  if (!proposal || !proposal.files || proposal.files.length === 0) {
    return item("No proposed changes.");
  }
  const files = proposal.files
    .map((file) => {
      const actions = file.suggested_actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("");
      return `<div class="item">
        <div class="item-title">${escapeHtml(file.path)}
          <span class="tag">${escapeHtml(file.change_type)}</span>
          <span class="tag ${file.confidence === "high" ? "ok" : "warn"}">${escapeHtml(file.confidence)}</span>
        </div>
        <p>${escapeHtml(file.rationale)}</p>
        <ul>${actions}</ul>
      </div>`;
    })
    .join("");
  const risks = proposal.risks
    .map((risk) => `<div class="item"><div class="item-title">Risk <span class="tag ${risk.level === "high" ? "danger" : "warn"}">${escapeHtml(risk.level)}</span></div><p>${escapeHtml(risk.message)}</p><p>${escapeHtml(risk.mitigation)}</p></div>`)
    .join("");
  return `<div class="item"><div class="item-title">${escapeHtml(proposal.objective)}</div></div>${files}${risks}`;
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

function renderGithub(data) {
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
  return `<div class="item">
    <div class="item-title">#${issue.number} ${escapeHtml(issue.title)}</div>
    <p><small>${escapeHtml(issue.author)} updated ${escapeHtml(issue.updated_at)}</small></p>
    <div class="issue-actions">
      <button class="secondary" data-task="${escapeHtml(taskText)}">Use as task</button>
    </div>
  </div>`;
}

function buildIssueTask(issue) {
  const labels = issue.labels && issue.labels.length ? `\nLabels: ${issue.labels.join(", ")}` : "";
  return `GitHub issue #${issue.number}: ${issue.title}${labels}\nURL: ${issue.html_url}`;
}

function renderPullRequest(pr) {
  const reviews = pr.reviews.length ? pr.reviews.map((review) => `<li>${escapeHtml(review.state)} by ${escapeHtml(review.reviewer)}</li>`).join("") : "<li>No reviews returned</li>";
  const checks = pr.checks.length ? pr.checks.map((check) => `<li>${escapeHtml(check.name)}: ${escapeHtml(check.status)}${check.conclusion ? `/${escapeHtml(check.conclusion)}` : ""}</li>`).join("") : "<li>No checks returned</li>";
  return `<div class="item">
    <div class="item-title">#${pr.number} ${escapeHtml(pr.title)}</div>
    <p>${escapeHtml(pr.source_branch)} -> ${escapeHtml(pr.target_branch)}</p>
    <p><small>${escapeHtml(pr.author)} updated ${escapeHtml(pr.updated_at)}</small></p>
    <strong>Reviews</strong><ul>${reviews}</ul>
    <strong>Checks</strong><ul>${checks}</ul>
  </div>`;
}

document.addEventListener("click", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLElement) || !target.matches("[data-task]")) {
    return;
  }
  $("taskInput").value = target.dataset.task || "";
  setStatus("GitHub issue loaded into task input.");
});

function buildLlmInputPreview(report, payload) {
  const context = report.relevant_files
    .slice(0, 5)
    .map((hit) => `Path: ${hit.path}\nScore: ${hit.score}\nReasons: ${hit.reasons.join(", ")}\nPreview:\n${hit.preview}`)
    .join("\n\n---\n\n");
  return `Use LLM: ${payload.use_llm}\nModel: ${payload.model || "(default)"}\nTask: ${payload.task}\n\nRelevant context:\n${context || "No context selected."}`;
}

function buildLlmOutputPreview(report) {
  return JSON.stringify(
    {
      plan_metadata: report.plan_metadata,
      plan: report.plan,
      patch_proposal_metadata: report.patch_proposal_metadata,
      patch_proposal: report.patch_proposal,
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

loadGithub().catch((error) => {
  $("githubContent").innerHTML = item(`GitHub status unavailable: ${escapeHtml(error.message)}`);
});
loadDiff(false).catch((error) => {
  $("diffOutput").textContent = `Diff unavailable: ${error.message}`;
});
