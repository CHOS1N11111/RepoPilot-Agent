const DiffView = (() => {
  const escape = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  function filePath(patch) {
    const name = patch.newFileName && patch.newFileName !== "/dev/null"
      ? patch.newFileName : patch.oldFileName || "Unnamed patch";
    return patch.isGit ? name.replace(/^[ab]\//, "") : name;
  }

  function parse(text) {
    if (text?.length > 1_000_000) return [];
    try {
      const patches = Diff.parsePatch(text || "");
      let lines = 0;
      for (const patch of patches) {
        if (!patch.oldFileName && !patch.newFileName) return [];
        for (const hunk of patch.hunks || []) {
          if (![hunk.oldStart, hunk.newStart, hunk.oldLines, hunk.newLines].every((value) => Number.isInteger(value) && value >= 0)) return [];
          lines += hunk.lines.length;
        }
      }
      return lines > 10_000 ? [] : patches;
    } catch {
      return [];
    }
  }

  function stats(patch) {
    return (patch.hunks || []).reduce((counts, hunk) => {
      for (const line of hunk.lines) {
        if (line.startsWith("+")) counts.added += 1;
        if (line.startsWith("-")) counts.removed += 1;
      }
      return counts;
    }, { added: 0, removed: 0 });
  }

  function renderHunks(patch) {
    return (patch.hunks || []).map((hunk) => {
      let oldLine = hunk.oldStart;
      let newLine = hunk.newStart;
      const rows = hunk.lines.map((line) => {
        const prefix = line[0];
        const kind = prefix === "+" ? "addition" : prefix === "-" ? "deletion" : "context";
        const oldNumber = prefix === " " || prefix === "-" ? oldLine++ : "";
        const newNumber = prefix === " " || prefix === "+" ? newLine++ : "";
        return `<div class="diff-line ${kind}"><span class="line-number">${oldNumber}</span><span class="line-number">${newNumber}</span><code>${escape(line)}</code></div>`;
      }).join("");
      return `<div class="diff-hunk">@@ -${hunk.oldStart},${hunk.oldLines} +${hunk.newStart},${hunk.newLines} @@</div>${rows}`;
    }).join("");
  }

  function render(text, { id = "diff", files = [], edits = [], selected = new Set(), disabled = false } = {}) {
    const patches = parse(text);
    const metadata = new Map(files.map((file) => [file.path, file]));
    const editable = new Map(edits.map((edit) => [edit.path, edit]));
    const entries = patches.map((patch) => {
      const path = filePath(patch);
      const unprefixed = path.replace(/^[ab]\//, "");
      return { path: !metadata.has(path) && metadata.has(unprefixed) ? unprefixed : path, patch };
    });
    const displayed = new Set(entries.map((entry) => entry.path));
    for (const file of files) {
      if (!displayed.has(file.path)) entries.push({ path: file.path, patch: null });
    }
    if (!entries.length) {
      return text?.trim()
        ? `<pre class="diff-fallback">${escape(text)}</pre>`
        : '<p class="empty-inline">No changes.</p>';
    }
    const total = patches.reduce((counts, patch) => {
      const next = stats(patch);
      return { added: counts.added + next.added, removed: counts.removed + next.removed };
    }, { added: 0, removed: 0 });
    const nav = entries.map((entry, index) => {
      const parts = entry.path.split(/[\\/]/);
      const name = parts.pop();
      return `<button type="button" class="file-link" data-diff-target="${id}-file-${index}" aria-current="${index === 0}" title="${escape(entry.path)}"><span>${escape(name)}</span><small>${escape(parts.join("/"))}</small></button>`;
    }).join("");
    const content = entries.map((entry, index) => {
      const { path, patch } = entry;
      const file = metadata.get(path);
      const edit = editable.get(path);
      const counts = patch ? stats(patch) : { added: 0, removed: 0 };
      const flags = patch ? [patch.isBinary ? "Binary change" : "", patch.isRename ? "Renamed" : "", patch.isCopy ? "Copied" : "",
        patch.isCreate ? "Added" : "", patch.isDelete ? "Deleted" : "",
        patch.oldMode !== patch.newMode && patch.newMode ? `Mode ${patch.oldMode || "new"} to ${patch.newMode}` : ""] : [];
      const previous = patch?.isRename || patch?.isCopy ? `<p class="section-note">From ${escape(patch.oldFileName)}</p>` : "";
      const selection = edit ? `<label class="approval-row"><input type="checkbox" data-approval-path="${escape(path)}" ${selected.has(path) ? "checked" : ""} ${disabled ? "disabled" : ""} />Selected for apply</label>` : "";
      const rationale = file?.rationale || edit?.rationale;
      const actions = (file?.suggested_actions || []).map((action) => `<li>${escape(action)}</li>`).join("");
      const notes = rationale || actions ? `<details class="file-notes"><summary>Change rationale${file?.confidence ? ` <span class="tag">${escape(file.confidence)} confidence</span>` : ""}</summary>${rationale ? `<p>${escape(rationale)}</p>` : ""}${actions ? `<ul>${actions}</ul>` : ""}</details>` : "";
      const lines = patch ? renderHunks(patch) : "";
      return `<details id="${id}-file-${index}" class="diff-file" data-file-path="${escape(path)}" open>
        <summary><span class="diff-path">${escape(path)}</span><span class="diff-counts"><span class="added">+${counts.added}</span><span class="removed">-${counts.removed}</span></span></summary>
        <div class="diff-file-tools">${selection}<button class="secondary icon-button" type="button" data-copy-path="${escape(path)}" title="Copy file path" aria-label="Copy file path"><svg class="ui-icon" aria-hidden="true"><use href="/vendor/icons.svg#copy" /></svg></button></div>
        ${previous}${flags.filter(Boolean).map((flag) => `<p class="section-note">${escape(flag)}</p>`).join("")}${notes}
        ${lines ? `<div class="diff-lines" tabindex="0" aria-label="Changes to ${escape(path)}">${lines}</div>` : `<p class="empty-inline">${patch?.isBinary ? "Binary content is not displayed." : patch ? "No line changes." : edit ? "No structured diff for this file." : "No direct file edit was generated for this file."}</p>`}
      </details>`;
    }).join("");
    const fallback = !patches.length && text?.trim() ? `<pre class="diff-fallback">${escape(text)}</pre>` : "";
    return `<div class="diff-overview"><span>${entries.length} file(s)</span><span class="diff-counts"><span class="added">+${total.added}</span><span class="removed">-${total.removed}</span></span></div>
      <div class="diff-layout"><nav class="diff-file-nav" aria-label="Changed files">${nav}</nav><div class="diff-files">${content}${fallback}</div></div>`;
  }

  return { parse, filePath, stats, render };
})();
