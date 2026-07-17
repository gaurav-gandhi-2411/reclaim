import { renderTreemap } from "./treemap.js";
import { categoryColorVar } from "./categories.js";

const THEME_KEY = "reclaim-theme";

// --- Theme toggle -----------------------------------------------------------------------------

function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "light" || stored === "dark") {
    document.documentElement.setAttribute("data-theme", stored);
  }
  updateThemeButtonLabel();
}

function toggleTheme() {
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const current =
    document.documentElement.getAttribute("data-theme") ?? (prefersDark ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem(THEME_KEY, next);
  updateThemeButtonLabel();
}

function updateThemeButtonLabel() {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const current =
    document.documentElement.getAttribute("data-theme") ?? (prefersDark ? "dark" : "light");
  btn.textContent = current === "dark" ? "Switch to light" : "Switch to dark";
}

// --- Fetch helper -------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers ?? {}) },
    ...options,
  });
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const body = isJson ? await response.json() : null;
  if (!response.ok) {
    const detail = body?.detail ?? response.statusText;
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), response.status);
  }
  return body;
}

// --- Generic loading / empty / error state panel ------------------------------------------------

function renderState(container, kind, { title, message, actionLabel, onAction } = {}) {
  container.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "rc-state-panel";
  panel.dataset.kind = kind;
  panel.setAttribute("role", kind === "error" ? "alert" : "status");

  if (kind === "loading") {
    const spinner = document.createElement("span");
    spinner.className = "rc-spinner";
    spinner.setAttribute("aria-hidden", "true");
    panel.appendChild(spinner);
  }

  const strong = document.createElement("strong");
  strong.textContent = title;
  panel.appendChild(strong);

  if (message) {
    const p = document.createElement("p");
    p.textContent = message;
    p.style.margin = "0";
    panel.appendChild(p);
  }

  if (actionLabel && onAction) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rc-btn rc-btn-primary";
    btn.textContent = actionLabel;
    btn.addEventListener("click", onAction);
    panel.appendChild(btn);
  }

  container.appendChild(panel);
}

// --- Tabs --------------------------------------------------------------------------------------

const VIEW_LOADERS = {
  overview: loadOverview,
  treemap: loadTreemapView,
  review: loadReviewQueue,
  quarantine: loadQuarantineView,
};

function initTabs() {
  const tabs = document.querySelectorAll(".rc-tab");
  for (const tab of tabs) {
    tab.addEventListener("click", () => activateTab(tab.dataset.view));
  }
}

function activateTab(viewName) {
  for (const tab of document.querySelectorAll(".rc-tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.view === viewName));
  }
  for (const section of document.querySelectorAll(".rc-view")) {
    section.dataset.active = String(section.id === `view-${viewName}`);
  }
  VIEW_LOADERS[viewName]?.();
}

// --- Scan bar ------------------------------------------------------------------------------------

let pollHandle = null;

function initScanBar() {
  const form = document.getElementById("scan-form");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = document.getElementById("scan-path");
    const statusEl = document.getElementById("scan-status");
    const submitBtn = form.querySelector("button[type=submit]");
    const path = input.value.trim();
    if (!path) return;

    submitBtn.disabled = true;
    statusEl.dataset.tone = "";
    statusEl.textContent = "Starting scan…";
    try {
      await api("/api/scan", { method: "POST", body: JSON.stringify({ path }) });
      // refreshScanStatus (not pollScanStatus) — it's the one that arms the repeating
      // setInterval when it observes "running"; pollScanStatus alone only ever checks once,
      // so a scan caught mid-flight here would otherwise freeze the UI on "Scanning…" forever.
      refreshScanStatus();
    } catch (err) {
      statusEl.dataset.tone = "error";
      statusEl.textContent = `Scan failed to start: ${err.message}`;
      submitBtn.disabled = false;
    }
  });
  refreshScanStatus();
}

async function refreshScanStatus() {
  const statusEl = document.getElementById("scan-status");
  const submitBtn = document.querySelector("#scan-form button[type=submit]");
  try {
    const status = await api("/api/scan/status");
    renderScanStatus(status);
    if (status.status === "running") {
      submitBtn.disabled = true;
      if (!pollHandle) pollHandle = setInterval(pollScanStatus, 1500);
    } else {
      submitBtn.disabled = false;
    }
  } catch (err) {
    statusEl.dataset.tone = "error";
    statusEl.textContent = `Could not reach the Reclaim server: ${err.message}`;
  }
}

function renderScanStatus(status) {
  const statusEl = document.getElementById("scan-status");
  if (status.status === "idle") {
    statusEl.dataset.tone = "";
    statusEl.textContent = "No scan yet — enter a path and click Scan to begin.";
  } else if (status.status === "running") {
    statusEl.dataset.tone = "";
    statusEl.textContent = `Scanning ${status.root}…`;
  } else if (status.status === "completed") {
    statusEl.dataset.tone = "success";
    statusEl.textContent =
      `Scan of ${status.root} complete: ${status.entries_total} entries ` +
      `(${status.files_written} written, ${status.files_unchanged} unchanged, ` +
      `${status.files_pruned} pruned) in ${status.elapsed_seconds?.toFixed(2)}s.`;
  } else if (status.status === "failed") {
    statusEl.dataset.tone = "error";
    statusEl.textContent = `Scan of ${status.root} failed: ${status.error}`;
  }
}

async function pollScanStatus() {
  const status = await api("/api/scan/status").catch(() => null);
  if (!status) return;
  renderScanStatus(status);
  const submitBtn = document.querySelector("#scan-form button[type=submit]");
  if (status.status !== "running") {
    clearInterval(pollHandle);
    pollHandle = null;
    submitBtn.disabled = false;
    const activeView = document.querySelector('.rc-tab[aria-selected="true"]')?.dataset.view;
    if (activeView) VIEW_LOADERS[activeView]?.();
  }
}

// --- Overview: summary stats + category cards -----------------------------------------------------

async function loadOverview() {
  const stateEl = document.getElementById("overview-state");
  const contentEl = document.getElementById("overview-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Loading summary…" });

  try {
    const summary = await api("/api/summary");
    if (!summary.has_scan) {
      renderState(stateEl, "empty", {
        title: "No scan yet",
        message: "Run a scan from the bar above to see reclaimable space here.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderSummaryStats(summary);
    renderCategoryCards(summary.categories);
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not load summary",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadOverview,
    });
  }
}

function renderSummaryStats(summary) {
  const row = document.getElementById("stat-row");
  row.innerHTML = "";
  const stats = [
    ["Total indexed", summary.total_indexed_human, `${summary.total_indexed_bytes.toLocaleString()} bytes`],
    [
      "Tier A — auto-quarantine eligible",
      `${summary.tier_a_bytes ? formatFromBytes(summary.tier_a_bytes) : "0 B"}`,
      `${summary.tier_a_count.toLocaleString()} items`,
    ],
    [
      "Tier B — review queue",
      `${summary.tier_b_bytes ? formatFromBytes(summary.tier_b_bytes) : "0 B"}`,
      `${summary.tier_b_count.toLocaleString()} items`,
    ],
  ];
  for (const [label, big, small] of stats) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.innerHTML = `${big} <small>${small}</small>`;
    const stat = document.createElement("div");
    stat.className = "rc-stat";
    stat.appendChild(dt);
    stat.appendChild(dd);
    row.appendChild(stat);
  }
}

function formatFromBytes(bytes) {
  // Mirrors the server's format_bytes rounding rules for locally-derived numbers (e.g. summed
  // display strings) that don't already come pre-formatted from the API.
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  for (const unit of units) {
    if (Math.abs(value) < 1024 || unit === units[units.length - 1]) {
      return unit === "B" ? `${value.toFixed(0)} ${unit}` : `${value.toFixed(1)} ${unit}`;
    }
    value /= 1024;
  }
  return `${value.toFixed(1)} PB`;
}

function renderCategoryCards(categories) {
  const grid = document.getElementById("category-grid");
  grid.innerHTML = "";
  if (categories.length === 0) {
    const p = document.createElement("p");
    p.className = "rc-scan-status";
    p.textContent = "No candidate categories found in this scan.";
    grid.appendChild(p);
    return;
  }
  for (const card of categories) {
    const el = document.createElement("article");
    el.className = "rc-category-card";
    el.style.borderLeftColor = categoryColorVar(card.category_group);
    el.innerHTML = `
      <h3>${card.category_label}</h3>
      <div class="rc-bytes">${card.total_bytes_human}</div>
      <div class="rc-meta">${card.file_count.toLocaleString()} item(s) exactly measured</div>
      <span class="rc-badge" data-tier="${card.tier}">Tier ${card.tier}</span>
    `;
    grid.appendChild(el);
  }
}

// --- Treemap -----------------------------------------------------------------------------------

async function loadTreemapView() {
  const stateEl = document.getElementById("treemap-state");
  const contentEl = document.getElementById("treemap-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Loading treemap…" });

  try {
    const data = await api("/api/treemap");
    if (!data.has_scan) {
      renderState(stateEl, "empty", {
        title: "No scan yet",
        message: "Run a scan from the bar above to see the storage treemap here.",
      });
      return;
    }
    if (data.nodes.length === 0) {
      renderState(stateEl, "empty", {
        title: "No directory data for this session",
        message: data.root
          ? `No sized entries found directly under ${data.root}.`
          : "This server process hasn't recorded a scan root yet this session — run a new scan.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    document.getElementById("treemap-root-label").textContent =
      `${data.root} — ${data.total_bytes_human} total`;
    const svg = document.getElementById("treemap-svg");
    const tooltip = document.getElementById("treemap-tooltip");
    renderTreemap(svg, tooltip, data.nodes);
    renderTreemapLegend(data.nodes);
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not load treemap",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadTreemapView,
    });
  }
}

function renderTreemapLegend(nodes) {
  const legend = document.getElementById("treemap-legend");
  legend.innerHTML = "";
  const seen = new Map();
  for (const node of nodes) {
    if (!seen.has(node.category_group)) seen.set(node.category_group, node.category_label);
  }
  for (const [group, label] of seen) {
    const span = document.createElement("span");
    span.className = "rc-legend-swatch";
    span.style.setProperty("--dot-color", categoryColorVar(group));
    span.textContent = label;
    legend.appendChild(span);
  }
}

// --- Review queue --------------------------------------------------------------------------------

const selectedPaths = new Set();
let lastCandidates = [];

async function loadReviewQueue() {
  const stateEl = document.getElementById("review-state");
  const contentEl = document.getElementById("review-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Loading review queue…" });

  const tier = document.getElementById("review-tier-filter").value;
  const category = document.getElementById("review-category-filter").value;
  const params = new URLSearchParams({ tier });
  if (category) params.set("category", category);

  try {
    const data = await api(`/api/candidates?${params.toString()}`);
    if (!data.has_scan) {
      renderState(stateEl, "empty", {
        title: "No scan yet",
        message: "Run a scan from the bar above to populate the review queue.",
      });
      return;
    }
    lastCandidates = data.candidates;
    if (data.candidates.length === 0) {
      renderState(stateEl, "empty", {
        title: "Nothing matches this filter",
        message: "No candidates in this tier/category. Try “both” tiers or clear the category filter.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderCandidateList(data.candidates);
    updateApplyBar();
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not load the review queue",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadReviewQueue,
    });
  }
}

function renderCandidateList(candidates) {
  const list = document.getElementById("candidate-list");
  list.innerHTML = "";
  for (const candidate of candidates) {
    list.appendChild(renderCandidateCard(candidate));
  }
}

function renderCandidateCard(candidate) {
  const card = document.createElement("article");
  card.className = "rc-candidate-card";

  const head = document.createElement("div");
  head.className = "rc-candidate-card-head";

  const label = document.createElement("label");
  label.className = "rc-checkbox-row";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = selectedPaths.has(candidate.path);
  checkbox.setAttribute("aria-label", `Select ${candidate.path} for apply`);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) selectedPaths.add(candidate.path);
    else selectedPaths.delete(candidate.path);
    updateApplyBar();
  });
  const pathSpan = document.createElement("span");
  pathSpan.className = "rc-candidate-path";
  pathSpan.textContent = candidate.path;
  label.appendChild(checkbox);
  label.appendChild(pathSpan);
  head.appendChild(label);

  const badges = document.createElement("div");
  badges.innerHTML = `
    <span class="rc-badge" data-tier="${candidate.tier}">Tier ${candidate.tier}</span>
    <span class="rc-badge" data-kind="heuristic" title="Not a probability — a category label only">
      ${candidate.category_label}
    </span>
  `;
  head.appendChild(badges);
  card.appendChild(head);

  const meta = document.createElement("p");
  meta.className = "rc-candidate-rationale";
  meta.textContent = `${candidate.size_human} (${candidate.size_bytes.toLocaleString()} bytes) — ${candidate.rationale}`;
  card.appendChild(meta);

  if (candidate.rebuild_instruction) {
    const rebuild = document.createElement("p");
    rebuild.className = "rc-candidate-rationale";
    rebuild.textContent = `Rebuild: ${candidate.rebuild_instruction}`;
    card.appendChild(rebuild);
  }

  if (candidate.recovery_cost_note) {
    const cost = document.createElement("p");
    cost.className = "rc-candidate-rationale rc-candidate-recovery-cost";
    cost.textContent = `Recovery cost: ${candidate.recovery_cost_note}`;
    card.appendChild(cost);
  }

  if (candidate.duplicate_cluster) {
    card.appendChild(renderClusterTable(candidate.duplicate_cluster));
  }

  return card;
}

function renderClusterTable(cluster) {
  const table = document.createElement("table");
  table.className = "rc-cluster-table";
  table.innerHTML = `
    <caption>Duplicate cluster (BLAKE3 full-hash match, exact byte-identical) — hash ${cluster.full_hash.slice(0, 12)}…</caption>
    <thead>
      <tr><th scope="col">Path</th><th scope="col">Size</th><th scope="col">Created</th><th scope="col">Status</th></tr>
    </thead>
  `;
  const tbody = document.createElement("tbody");
  for (const member of cluster.members) {
    const row = document.createElement("tr");
    row.dataset.keep = String(member.is_keep);
    row.innerHTML = `
      <td class="rc-candidate-path">${member.path}</td>
      <td>${member.size_human}</td>
      <td>${new Date(member.ctime * 1000).toLocaleString()}</td>
      <td>${member.is_keep ? '<span class="rc-badge" data-kind="heuristic">Kept — heuristic pick</span>' : "Proposed for removal"}</td>
    `;
    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  return table;
}

function updateApplyBar() {
  const countEl = document.getElementById("apply-selected-count");
  const bytesEl = document.getElementById("apply-selected-bytes");
  const selected = lastCandidates.filter((c) => selectedPaths.has(c.path));
  const totalBytes = selected.reduce((sum, c) => sum + c.size_bytes, 0);
  countEl.textContent = String(selected.length);
  bytesEl.textContent = formatFromBytes(totalBytes);
  const previewBtn = document.getElementById("apply-preview-btn");
  const realBtn = document.getElementById("apply-real-btn");
  previewBtn.disabled = selected.length === 0;
  realBtn.disabled = true; // re-enabled only after a fresh preview of the current selection
}

async function runApply(dryRun) {
  const resultEl = document.getElementById("apply-result");
  const method = document.getElementById("apply-method").value;
  const paths = [...selectedPaths];
  if (paths.length === 0) return;

  if (!dryRun) {
    const confirmed = window.confirm(
      `This will really quarantine ${paths.length} item(s) via ${method}. Continue?`
    );
    if (!confirmed) return;
  }

  renderState(resultEl, "loading", { title: dryRun ? "Running dry-run preview…" : "Applying…" });
  try {
    const report = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify({ tier: "both", paths, method, dry_run: dryRun }),
    });
    renderApplyReport(resultEl, report);
    if (dryRun) document.getElementById("apply-real-btn").disabled = false;
    if (!dryRun) {
      selectedPaths.clear();
      loadReviewQueue();
    }
  } catch (err) {
    renderState(resultEl, "error", { title: "Apply failed", message: err.message });
  }
}

function renderApplyReport(container, report) {
  container.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "rc-state-panel";
  panel.dataset.kind = report.apply ? "success" : "info";
  panel.setAttribute("role", "status");

  const heading = document.createElement("strong");
  heading.textContent = report.apply
    ? `Applied — batch ${report.batch_id}`
    : `Dry-run preview — batch ${report.batch_id} (nothing on disk was touched)`;
  panel.appendChild(heading);

  const summary = document.createElement("p");
  summary.style.margin = "0";
  summary.textContent =
    `${report.files_succeeded}/${report.files_processed} succeeded, ` +
    `${report.files_failed} failed — ${report.bytes_freed_human} ` +
    `(${report.bytes_freed.toLocaleString()} bytes) ${report.apply ? "freed" : "would be freed"}.`;
  panel.appendChild(summary);

  if (report.category_breakdown.length > 0) {
    const list = document.createElement("ul");
    for (const entry of report.category_breakdown) {
      const li = document.createElement("li");
      li.textContent = `${entry.category_label}: ${entry.count} item(s), ${entry.bytes_freed_human}`;
      list.appendChild(li);
    }
    panel.appendChild(list);
  }

  if (report.disk_free_delta_bytes !== null && report.disk_free_delta_bytes !== undefined) {
    const disk = document.createElement("p");
    disk.textContent = `Measured disk free: before ${report.disk_free_before_bytes.toLocaleString()} bytes, after ${report.disk_free_after_bytes.toLocaleString()} bytes, delta ${report.disk_free_delta_bytes.toLocaleString()} bytes.`;
    panel.appendChild(disk);
  }

  const failures = report.items.filter((item) => !item.succeeded);
  if (failures.length > 0) {
    const failList = document.createElement("ul");
    for (const item of failures) {
      const li = document.createElement("li");
      li.textContent = `FAILED: ${item.path} — ${item.error}`;
      failList.appendChild(li);
    }
    panel.appendChild(failList);
  }

  container.appendChild(panel);
}

function initReviewQueue() {
  document.getElementById("review-tier-filter").addEventListener("change", loadReviewQueue);
  document.getElementById("review-category-filter").addEventListener("change", loadReviewQueue);
  document.getElementById("apply-preview-btn").addEventListener("click", () => runApply(true));
  document.getElementById("apply-real-btn").addEventListener("click", () => runApply(false));
}

// --- Quarantine / restore ------------------------------------------------------------------------

async function loadQuarantineView() {
  const stateEl = document.getElementById("quarantine-state");
  const contentEl = document.getElementById("quarantine-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Loading quarantine batches…" });

  try {
    const data = await api("/api/quarantine");
    if (data.batches.length === 0) {
      renderState(stateEl, "empty", {
        title: "No quarantined batches yet",
        message: "Apply Tier A/B candidates from the Review Queue to see batches here.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderBatchList(data.batches);
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not load quarantine batches",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadQuarantineView,
    });
  }
}

function renderBatchList(batches) {
  const list = document.getElementById("batch-list");
  list.innerHTML = "";
  for (const batch of batches) {
    const card = document.createElement("article");
    card.className = "rc-batch-card";

    const head = document.createElement("div");
    head.className = "rc-batch-head";
    head.innerHTML = `
      <div>
        <span class="rc-batch-id">${batch.batch_id}</span> — ${batch.method} —
        ${new Date(batch.quarantined_at * 1000).toLocaleString()}
      </div>
      <div>${batch.item_count} item(s), ${batch.bytes_total_human}, ${batch.restored_count} restored</div>
    `;
    card.appendChild(head);

    const restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.className = "rc-btn rc-btn-secondary";
    restoreBtn.textContent = "Restore batch";
    restoreBtn.disabled = !batch.can_restore || batch.restored_count === batch.item_count;
    restoreBtn.addEventListener("click", () => restoreBatch(batch.batch_id));
    card.appendChild(restoreBtn);

    if (!batch.can_restore) {
      const blocked = document.createElement("p");
      blocked.className = "rc-restore-blocked";
      blocked.textContent = batch.restore_blocked_reason;
      card.appendChild(blocked);
    }

    list.appendChild(card);
  }
}

async function restoreBatch(batchId) {
  const stateEl = document.getElementById("quarantine-state");
  try {
    await api(`/api/restore/${encodeURIComponent(batchId)}`, { method: "POST" });
    loadQuarantineView();
  } catch (err) {
    renderState(stateEl, "error", {
      title: `Restore failed for ${batchId}`,
      message: err.message,
      actionLabel: "Back to quarantine",
      onAction: loadQuarantineView,
    });
    document.getElementById("quarantine-content").hidden = true;
  }
}

// --- Boot ------------------------------------------------------------------------------------

function init() {
  initTheme();
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  initTabs();
  initScanBar();
  initReviewQueue();
  activateTab("overview");
}

document.addEventListener("DOMContentLoaded", init);
