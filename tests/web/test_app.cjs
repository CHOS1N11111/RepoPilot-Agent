const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const source = readFileSync(path.join(__dirname, "../../src/repopilot_agent/web/static/app.js"), "utf8");
const diffSource = readFileSync(path.join(__dirname, "../../src/repopilot_agent/web/static/diff-view.js"), "utf8");
const diffLibrary = require("../../src/repopilot_agent/web/static/vendor/diff.min.js");

function createApp() {
  const elements = new Map();
  const listeners = new Map();
  const timers = new Map();
  const requests = [];
  let timerId = 0;
  const element = (id) => {
    if (!elements.has(id)) {
      const classes = new Set();
      const events = new Map();
      const attributes = new Map();
      elements.set(id, {
        value: "", textContent: "", innerHTML: "", disabled: false, hidden: false,
        checked: false, dataset: {}, events,
        classList: {
          add: (name) => classes.add(name),
          remove: (name) => classes.delete(name),
          contains: (name) => classes.has(name),
          toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
        },
        addEventListener: (name, callback) => events.set(name, callback),
        setAttribute: (name, value) => attributes.set(name, value),
        getAttribute: (name) => attributes.get(name),
        appendChild() {}, focus() {},
        scrollIntoView() {},
        replaceChildren() { this.textContent = ""; this.innerHTML = ""; },
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {
      getElementById: element,
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: () => element(Symbol()),
      addEventListener: (name, callback) => listeners.set(name, callback),
    },
    window: {
      confirm: () => true,
      setInterval: (callback) => { timers.set(++timerId, callback); return timerId; },
      clearInterval: (id) => timers.delete(id),
    },
    URLSearchParams,
    Diff: diffLibrary,
    fetch: async (url, options) => {
      requests.push({ url, payload: options?.body ? JSON.parse(options.body) : null });
      const response = await context.respond(url, options);
      return { json: async () => response };
    },
    respond: () => { throw new Error("Unexpected request"); },
  });
  element("repoSource").value = "local";
  element("repoPath").value = "/repo-A";
  element("modelSelect").value = "test-model";
  element("useMemory").checked = true;
  element("allowFallback").checked = true;
  vm.runInContext(diffSource, context, { filename: "diff-view.js" });
  vm.runInContext(source, context, { filename: "app.js" });
  return {
    app: context,
    state: vm.runInContext("state", context),
    diffView: vm.runInContext("DiffView", context),
    element, requests, timers,
    change(id, value) {
      element(id).value = value;
      element(id).events.get(id === "repoSource" ? "change" : "input")();
    },
    approve(filePath, checked) {
      listeners.get("change")({ target: { dataset: { approvalPath: filePath }, checked } });
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}

function report(proposalId = "proposal-A") {
  return {
    proposal_id: proposalId, plan: [], validation: [], relevant_files: [],
    patch_proposal: {
      objective: "Update validation", apply_ready: true, risks: [],
      files: ["a.py", "b.py"].map((filePath) => ({ path: filePath, suggested_actions: [] })),
      file_edits: ["a.py", "b.py"].map((filePath) => ({ path: filePath, new_content: "updated\n" })),
      proposed_diff: "+updated\n",
    },
  };
}

function delivery(ready) {
  return {
    pr_readiness: { ready, branch: "feature/A", head_branch: "feature/A", base_branch: "main" },
    pull_request: { title: "Repository A changes", body: "Changes made in repository A." },
  };
}

test("repository fields invalidate cached views, drafts, approvals and task controls", async (t) => {
  for (const [id, value] of [["repoPath", "/repo-B"], ["repoSource", "github"], ["githubUrl", "https://github.com/example/B"], ["repoBranch", "feature/B"]]) {
    await t.test(id, async () => {
      const { app, state, element, timers, change } = createApp();
      app.respond = () => delivery(true);
      await app.generateDelivery();
      state.lastReport = report();
      app.renderReport(state.lastReport, app.buildWorkflowPayload());
      state.taskRun = { run_id: "old-run", source_repo: "/repo-A", can_cancel: true };
      state.sandbox = { path: "/sandbox-A", source_repo: "/repo-A" };
      state.sandboxesLoaded = true;
      state.loadedViews.add("diff");
      element("diffOutput").textContent = "Old diff";
      app.startTaskRunPolling();
      change(id, value);
      assert.equal(state.delivery, null);
      assert.equal(state.lastReport, null);
      assert.equal(state.taskRun, null);
      assert.equal(state.sandbox, null);
      assert.equal(state.sandboxesLoaded, false);
      assert.equal(state.loadedViews.size, 0);
      assert.equal(state.approvedPaths.size, 0);
      assert.equal(timers.size, 0);
      assert.equal(element("diffOutput").textContent, "");
      for (const button of ["createPullRequest", "applyProposal", "cancelTaskRun", "approveRuntimeWrite"]) {
        assert.equal(element(button).disabled, true, button);
      }
    });
  }
});

test("a late diff response cannot replace the new repository or its cached view", async () => {
  const { app, state, element, requests, change } = createApp();
  const oldResponse = deferred();
  app.respond = () => oldResponse.promise;
  const oldLoad = app.loadViewData("diff");
  change("repoPath", "/repo-B");
  app.respond = () => ({ diff: "B diff" });
  await app.loadViewData("diff");
  oldResponse.resolve({ diff: "A diff", repository_source: { source: "local", local_path: "/repo-A" } });
  await oldLoad;
  assert.equal(requests.length, 2);
  assert.equal(new URL(requests[1].url, "http://localhost").searchParams.get("repo"), "/repo-B");
  assert.equal(element("diffOutput").textContent, "B diff");
  assert.equal(element("repoPath").value, "/repo-B");
  assert.equal(state.loadedViews.has("diff"), true);
});

test("a late request failure cannot overwrite the new repository status", async () => {
  const { app, element, change } = createApp();
  const response = deferred();
  app.respond = () => response.promise;
  const loading = app.loadViewData("diff");
  change("repoPath", "/repo-B");
  response.reject(new Error("Old repository failed"));
  await loading;
  assert.equal(element("diffOutput").textContent, "");
  assert.equal(element("statusLine").textContent, "Repository selection changed.");
});

test("a late delivery response cannot restore a draft after a repository switch", async () => {
  const { app, state, element, change } = createApp();
  const response = deferred();
  app.respond = () => response.promise;
  const generating = app.generateDelivery();
  change("repoPath", "/repo-B");
  response.resolve(delivery(true));
  await generating;
  assert.equal(state.delivery, null);
  assert.equal(element("createPullRequest").disabled, true);
});

test("LLM connection tests remain independent of the repository selection", async () => {
  const { app, element, change } = createApp();
  const response = deferred();
  app.respond = () => response.promise;
  const testing = app.testLlmConnection();
  change("repoPath", "/repo-B");
  response.resolve({ model: "test-model", response_preview: "OK" });
  await testing;
  assert.match(element("llmTestLine").textContent, /^OK: test-model responded/);
});

test("PR creation refuses a draft bound to another repository even without an input event", async () => {
  const { app, element, requests } = createApp();
  app.respond = () => delivery(true);
  await app.generateDelivery();
  element("repoPath").value = "/repo-B";
  await app.createPullRequest();
  assert.equal(requests.length, 1);
  assert.match(element("statusLine").textContent, /current repository/);
});

test("selecting a sandbox detaches the old task while automatic adoption preserves its task", () => {
  const { app, state, element } = createApp();
  state.taskRun = { run_id: "old-run" };
  app.activateSandbox({ path: "/sandbox-B", source_repo: "/repo-B" });
  assert.equal(state.taskRun, null);
  assert.equal(element("repoPath").value, "/sandbox-B");
  const task = { run_id: "new-run", source_repo: "/repo-B", sandbox_path: "/sandbox-C", status: "exploring" };
  app.updateTaskRun(task);
  assert.equal(state.taskRun.run_id, "new-run");
  assert.equal(state.taskRunSandboxPath, "/sandbox-C");
  assert.equal(element("repoPath").value, "/sandbox-C");
});

test("unchanged proposal content preserves file choices when task timestamps change", () => {
  const { app, state, approve } = createApp();
  const run = { run_id: "run-A", proposal_id: "proposal-A", status: "awaiting_approval", updated_at: "1", result: report() };
  app.updateTaskRun(run);
  approve("b.py", false);
  app.updateTaskRun({ ...run, updated_at: "2" });
  assert.deepEqual([...state.approvedPaths], ["a.py"]);
  state.proposalApplied = true;
  state.rollbackAvailable = true;
  app.updateTaskRun({ ...run, updated_at: "3" });
  assert.equal(state.proposalApplied, true);
  assert.equal(state.rollbackAvailable, true);
});

test("changed proposal contents require renewed file choices", () => {
  const { app, state } = createApp();
  const original = report();
  state.lastReport = original;
  app.renderReport(original, app.buildWorkflowPayload());
  const revision = report();
  revision.patch_proposal.file_edits[0].new_content = "different\n";
  app.renderReport(revision, app.buildWorkflowPayload());
  assert.equal(state.approvedPaths.size, 0);
  app.renderReport(report("proposal-B"), app.buildWorkflowPayload());
  assert.deepEqual([...state.approvedPaths], ["a.py", "b.py"]);
});

test("GitHub error and unavailable text is escaped before rendering", () => {
  const { app, element } = createApp();
  for (const field of ["error", "unavailable_reason"]) {
    app.renderGithub({ [field]: '<img src=x onerror="probe()"> & failure' });
    assert.equal(element("githubContent").innerHTML, '<div class="item">&lt;img src=x onerror=&quot;probe()&quot;&gt; &amp; failure</div>');
  }
});

test("fresh PR readiness is used by both the button and the submitted draft", async () => {
  const { app, state, element, requests } = createApp();
  app.respond = () => delivery(false);
  await app.generateDelivery();
  assert.equal(element("createPullRequest").disabled, true);
  app.respond = () => ({ pr_readiness: delivery(true).pr_readiness });
  await app.loadPrReadiness();
  assert.equal(state.delivery.pr_readiness.ready, true);
  assert.equal(element("createPullRequest").disabled, false);
  app.respond = () => ({ pull_request: { number: 1 }, pr_readiness: delivery(true).pr_readiness });
  await app.createPullRequest();
  assert.equal(requests[2].url, "/api/github/pr/create");
  assert.equal(requests[2].payload.repo, "/repo-A");
  assert.equal(requests[2].payload.title, "Repository A changes");
  assert.equal(state.delivery.pr_readiness, null);
  assert.equal(element("createPullRequest").disabled, true);
});

test("a failed readiness refresh invalidates a previously ready draft", async () => {
  const { app, state, element, requests } = createApp();
  app.respond = () => delivery(true);
  await app.generateDelivery();
  app.respond = () => { throw new Error("Unavailable"); };
  await app.loadPrReadiness();
  assert.equal(state.delivery.pr_readiness, null);
  assert.equal(element("createPullRequest").disabled, true);
  await app.createPullRequest();
  assert.equal(requests.length, 2);
});

test("PR creation rejection replaces cached readiness with server blockers", async () => {
  const { app, state, element } = createApp();
  app.respond = () => delivery(true);
  await app.generateDelivery();
  app.respond = () => ({ error: "Branch is no longer ready", pr_readiness: delivery(false).pr_readiness });
  await app.createPullRequest();
  assert.equal(state.delivery.pr_readiness.ready, false);
  assert.equal(element("createPullRequest").disabled, true);
});

test("a static summary cannot report a successful refresh without loading data", async () => {
  const { app, element, requests } = createApp();
  await app.refreshCurrentView();
  assert.equal(requests.length, 0);
  assert.equal(element("refreshAll").disabled, true);
  assert.match(element("viewStatus").textContent, /no live data/);
});

test("failed history refresh stays failed and can be retried", async () => {
  const { app, state, element } = createApp();
  state.activeView = "history";
  app.respond = () => { throw new Error("History offline"); };
  await assert.rejects(app.loadViewData("history"), /History offline/);
  assert.equal(element("viewStatus").dataset.tone, "danger");
  assert.equal(state.loadedViews.has("history"), false);
  assert.equal(element("retryView").hidden, false);
  app.respond = () => ({ runs: [] });
  await state.retryAction();
  assert.equal(element("viewStatus").textContent, "View updated.");
  assert.equal(element("retryView").hidden, true);
});

test("rapid working and staged loads keep the newest selection and response", async () => {
  const { app, state, element } = createApp();
  const working = deferred();
  app.respond = () => working.promise;
  const oldLoad = app.loadDiff(false);
  app.respond = () => ({ diff: "Staged diff" });
  await app.loadDiff(true);
  working.resolve({ diff: "Older working diff" });
  await oldLoad;
  assert.equal(state.diffStaged, true);
  assert.equal(element("loadStagedDiff").getAttribute("aria-pressed"), "true");
  assert.equal(element("diffOutput").textContent, "Staged diff");
});

test("connection testing prevents duplicate requests and exposes retry after failure", async () => {
  const { app, element, requests } = createApp();
  const response = deferred();
  app.respond = () => response.promise;
  const first = app.testLlmConnection();
  await app.testLlmConnection();
  assert.equal(requests.length, 1);
  assert.equal(element("testLlm").disabled, true);
  response.resolve({ error: "Endpoint offline" });
  await first;
  assert.equal(element("testLlm").disabled, false);
  assert.equal(element("llmTestLine").dataset.tone, "danger");
  assert.equal(element("retryView").hidden, false);
});

test("new sandbox runs re-enable the launch button after resetting repository context", async () => {
  const { app, state, element } = createApp();
  element("taskInput").value = "Review task";
  app.selectRunMode("task");
  app.respond = () => ({ task_run: { run_id: "run-B", source_repo: "/repo-A", status: "awaiting_approval", result: report() } });
  await app.runSelectedMode();
  assert.equal(state.taskRun.run_id, "run-B");
  assert.equal(element("runPrimary").disabled, false);
});

test("positive settings preserve payload meaning and ignore hidden GitHub URLs", () => {
  const { app, element } = createApp();
  element("githubUrl").value = "https://github.com/other/repo";
  element("useMemory").checked = false;
  element("allowFallback").checked = false;
  element("iterativeAgent").checked = true;
  let payload = app.buildWorkflowPayload();
  assert.equal(payload.use_memory, false);
  assert.equal(payload.no_llm_fallback, true);
  assert.equal(payload.iterative_agent, false);
  assert.equal(payload.github_url, "");
  element("repoSource").value = "github";
  element("useLlm").checked = true;
  payload = app.buildWorkflowPayload();
  assert.equal(payload.github_url, "https://github.com/other/repo");
  assert.equal(payload.iterative_agent, true);
});

test("pending exact approval hides compatible apply controls while preserving selection", () => {
  const { app, state, element } = createApp();
  const pending = report();
  pending.agent_pending_approval = { checkpoint: "checkpoint-A", action_kind: "write", file_scope: ["a.py"] };
  state.lastReport = pending;
  state.taskRun = { status: "awaiting_approval", can_approve_runtime: true };
  app.renderReport(pending, app.buildWorkflowPayload());
  assert.equal(element("proposalReview").hidden, true);
  assert.equal(element("runtimeDetails").hidden, false);
  assert.equal(element("applyProposal").disabled, true);
  assert.equal(element("approveRuntimeWrite").disabled, false);
  assert.deepEqual([...state.approvedPaths], ["a.py", "b.py"]);
});

test("executing an approval disables both grant and rejection until it finishes", async () => {
  const { app, state, element } = createApp();
  state.lastReport = { ...report(), agent_pending_approval: { checkpoint: "pending", action_kind: "write" } };
  state.taskRun = { can_approve_runtime: true };
  const completion = deferred();
  const action = app.withBusy("approveRuntimeWrite", "Executing...", () => completion.promise);
  assert.equal(element("approveRuntimeWrite").disabled, true);
  assert.equal(element("rejectRuntimeWrite").disabled, true);
  assert.match(element("runtimeApprovalStatus").textContent, /Executing/);
  completion.resolve();
  await action;
  assert.equal(element("approveRuntimeWrite").disabled, false);
  assert.equal(element("rejectRuntimeWrite").disabled, false);
});

test("diff review attaches selection to unified patch files and escapes code", () => {
  const { diffView } = createApp();
  const patch = diffLibrary.createTwoFilesPatch("a/a.py", "b/a.py", "old\n", '<img src=x onerror="attack()">\n');
  const html = diffView.render(patch, { files: [{ path: "a.py" }], edits: [{ path: "a.py" }], selected: new Set(["a.py"]) });
  assert.equal((html.match(/class="diff-file"/g) || []).length, 1);
  assert.match(html, /data-approval-path="a.py" checked/);
  assert.match(html, /&lt;img src=x onerror=&quot;attack\(\)&quot;&gt;/);
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /class="diff-line addition"/);
  assert.match(html, /class="diff-line deletion"/);
});

test("diff review preserves binary, rename and file mode information", () => {
  const { diffView } = createApp();
  const binary = 'diff --git a/image.png b/image.png\nindex 1111111..2222222 100644\nBinary files a/image.png and b/image.png differ\n';
  const rename = 'diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt\n';
  const mode = 'diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n';
  const html = diffView.render(binary + rename + mode);
  assert.match(html, /Binary change/);
  assert.match(html, /Renamed/);
  assert.match(html, /From a\/old.txt/);
  assert.match(html, /Mode 100644 to 100755/);
});

test("malformed diffs remain readable as escaped raw text", () => {
  const { diffView } = createApp();
  const malformed = '@@ invalid patch <script>attack()</script>';
  const html = diffView.render(malformed);
  assert.match(html, /diff-fallback/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});
