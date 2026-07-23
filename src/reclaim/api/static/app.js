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

// Read once at module load from the <meta> tag reclaim.api.app's index() route renders — a
// cross-origin page can't read this tag (same-origin policy), so it can't forge the header
// reclaim.api.security requires on every mutating request either. See CSRF_HEADER_NAME there.
const CSRF_HEADER_NAME = "X-Reclaim-CSRF-Token";
const CSRF_TOKEN = document.querySelector('meta[name="reclaim-csrf-token"]')?.content ?? "";

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {
      "Content-Type": "application/json",
      [CSRF_HEADER_NAME]: CSRF_TOKEN,
      ...(options.headers ?? {}),
    },
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

async function loadQuickRoots() {
  // Server-resolved default scan-root suggestions (Downloads, home folder) for non-technical
  // users who can't be expected to type a path — the free-text input right below stays
  // available regardless. Non-fatal on failure: same "fail open, advanced path always works"
  // posture as loadModeStatus below.
  const container = document.getElementById("quick-roots");
  const list = document.getElementById("quick-roots-list");
  try {
    const data = await api("/api/scan/suggested-roots");
    if (!data.roots || data.roots.length === 0) {
      container.hidden = true;
      return;
    }
    list.innerHTML = "";
    for (const root of data.roots) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rc-btn rc-btn-secondary rc-quick-root-btn";
      btn.textContent = root.label;
      btn.dataset.path = root.path;
      btn.addEventListener("click", () => {
        document.getElementById("scan-path").value = root.path;
        document.getElementById("scan-form").requestSubmit();
      });
      list.appendChild(btn);
    }
    container.hidden = false;
  } catch {
    container.hidden = true;
  }
}

function initScanBar() {
  const form = document.getElementById("scan-form");
  loadQuickRoots();
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
    loadQuickClean();
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

// --- Quick Clean (one-click, categorically-safe groups only) -----------------------------------

// Populated by the last successful `/api/clean/one-click-summary` fetch — the confirm dialog
// and the apply call both read from this rather than re-deriving group -> paths themselves, so
// there is exactly one source for "what one-click clean is about to touch."
let lastQuickCleanGroups = [];

async function loadQuickClean() {
  const stateEl = document.getElementById("quick-clean-state");
  const contentEl = document.getElementById("quick-clean-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Checking for safe-to-clean items…" });

  try {
    const data = await api("/api/clean/one-click-summary");
    if (!data.has_scan) {
      renderState(stateEl, "empty", {
        title: "No scan yet",
        message: "Run a scan from the bar above to see what's safe to clean automatically.",
      });
      return;
    }
    lastQuickCleanGroups = data.groups;
    if (data.groups.length === 0) {
      renderState(stateEl, "empty", {
        title: "Nothing categorically safe to clean yet",
        message:
          "No package caches, temp/browser caches, crash reports, or rebuildable developer " +
          "files were found in this scan. Check the Review Queue for everything else.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderQuickCleanGroups(data.groups);
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not check for safe-to-clean items",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadQuickClean,
    });
  }
}

// `group.plain_label`/`safety_reason`/`total_bytes_human` are all server-formatted strings from
// a fixed lookup table (schemas.py::_PLAIN_LANGUAGE_CATEGORY), never a raw filesystem path —
// safe to template into innerHTML the same way renderCategoryCards does above. `group.paths`
// (raw filesystem paths) is NEVER rendered here at all — it only ever feeds the apply request
// body (see confirmQuickClean) and the dialog's group-name list (openQuickCleanDialog, which
// uses plain_label/counts via textContent, not paths).
function renderQuickCleanGroups(groups) {
  const container = document.getElementById("quick-clean-groups");
  container.innerHTML = "";
  for (const group of groups) {
    const card = document.createElement("article");
    card.className = "rc-quick-clean-card";
    card.style.borderLeftColor = categoryColorVar(group.category_group);
    card.innerHTML = `
      <div class="rc-quick-clean-card-head">
        <h3>${group.plain_label}</h3>
        <span class="rc-bytes">${group.total_bytes_human}</span>
      </div>
      <div class="rc-meta">${group.file_count.toLocaleString()} item(s)</div>
      ${group.safety_reason ? `<p class="rc-candidate-rationale">${group.safety_reason}</p>` : ""}
    `;
    container.appendChild(card);
  }

  const totalCount = groups.reduce((sum, g) => sum + g.file_count, 0);
  const totalBytes = groups.reduce((sum, g) => sum + g.total_bytes, 0);
  document.getElementById("quick-clean-total-count").textContent = String(totalCount);
  document.getElementById("quick-clean-total-bytes").textContent = formatFromBytes(totalBytes);
  document.getElementById("quick-clean-btn").disabled = groups.length === 0;
}

function openQuickCleanDialog() {
  const list = document.getElementById("quick-clean-dialog-groups");
  list.innerHTML = "";
  let totalBytes = 0;
  for (const group of lastQuickCleanGroups) {
    const li = document.createElement("li");
    li.textContent =
      `${group.plain_label}: ${group.file_count.toLocaleString()} item(s), ` +
      `${group.total_bytes_human}`;
    list.appendChild(li);
    totalBytes += group.total_bytes;
  }
  document.getElementById("quick-clean-dialog-total").textContent = formatFromBytes(totalBytes);
  document.getElementById("quick-clean-dialog").hidden = false;
}

function closeQuickCleanDialog() {
  document.getElementById("quick-clean-dialog").hidden = true;
}

async function confirmQuickClean() {
  closeQuickCleanDialog();
  const resultEl = document.getElementById("quick-clean-result");
  const paths = lastQuickCleanGroups.flatMap((group) => group.paths);
  if (paths.length === 0) return;

  renderState(resultEl, "loading", { title: "Cleaning…" });
  try {
    // tier: "both" — safe mode forces every candidate's tier to B (ADR-0023 guarantee 3), and
    // an explicit `paths` list is what makes this a valid safe-mode apply at all (apply_
    // selection refuses a blanket tier/category-group selection with no paths regardless of
    // this call's tier value). method: "vault" only matters in power mode — safe mode's
    // apply_batch forces recycle_bin unconditionally no matter what's requested here.
    const report = await api("/api/apply", {
      method: "POST",
      body: JSON.stringify({ tier: "both", paths, method: "vault", dry_run: false }),
    });
    renderQuickCleanResult(resultEl, report);
    loadQuickClean();
  } catch (err) {
    renderState(resultEl, "error", { title: "Clean failed", message: err.message });
  }
}

function renderQuickCleanResult(container, report) {
  container.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "rc-state-panel";
  panel.dataset.kind = "success";
  panel.setAttribute("role", "status");

  const heading = document.createElement("strong");
  heading.textContent = `Cleaned — batch ${report.batch_id}`;
  panel.appendChild(heading);

  // Every branch below states what ACTUALLY happened to the bytes — recycle_bin/vault are both
  // moves (recoverable), never described as "freed"; only direct_delete really frees the space
  // immediately. See house rule: never claim space was freed when it was only moved.
  const summary = document.createElement("p");
  summary.style.margin = "0";
  if (report.method === "recycle_bin") {
    summary.textContent =
      `${report.bytes_freed_human} (${report.bytes_freed.toLocaleString()} bytes) moved to ` +
      "the Recycle Bin — empty the Recycle Bin to free the space.";
  } else if (report.method === "vault") {
    summary.textContent =
      `${report.bytes_freed_human} (${report.bytes_freed.toLocaleString()} bytes) moved to ` +
      "the Reclaim vault — restorable from the Quarantine & Restore tab; the space is held " +
      "until purged.";
  } else {
    summary.textContent =
      `${report.bytes_freed_human} (${report.bytes_freed.toLocaleString()} bytes) permanently ` +
      "freed.";
  }
  panel.appendChild(summary);

  const detail = document.createElement("p");
  detail.style.margin = "0";
  detail.textContent =
    `${report.files_succeeded}/${report.files_processed} item(s) succeeded, ` +
    `${report.files_failed} failed.`;
  panel.appendChild(detail);

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

function initQuickClean() {
  document.getElementById("quick-clean-btn").addEventListener("click", openQuickCleanDialog);
  document.getElementById("quick-clean-cancel").addEventListener("click", closeQuickCleanDialog);
  document.getElementById("quick-clean-confirm").addEventListener("click", confirmQuickClean);
}

// --- "How this works" ----------------------------------------------------------------------------

function initHowItWorks() {
  document.getElementById("how-it-works-btn").addEventListener("click", () => {
    document.getElementById("how-it-works-dialog").hidden = false;
  });
  document.getElementById("how-it-works-close").addEventListener("click", () => {
    document.getElementById("how-it-works-dialog").hidden = true;
  });
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
  loadDuplicateClusterReview();

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

async function loadDuplicateClusterReview() {
  const stateEl = document.getElementById("duplicate-review-state");
  const contentEl = document.getElementById("duplicate-review-content");
  contentEl.hidden = true;
  renderState(stateEl, "loading", { title: "Loading largest duplicate clusters…" });

  try {
    const data = await api("/api/duplicate-clusters/review");
    if (!data.has_scan) {
      renderState(stateEl, "empty", {
        title: "No scan yet",
        message: "Run a scan from the bar above to see the largest duplicate clusters.",
      });
      return;
    }
    if (data.clusters.length === 0) {
      renderState(stateEl, "empty", {
        title: "No duplicate clusters to review",
        message: "Either no exact duplicates were found, or every cluster was excluded because a member sits under a protected path.",
      });
      return;
    }
    stateEl.innerHTML = "";
    contentEl.hidden = false;
    renderDuplicateClusterReview(contentEl, data.clusters);
  } catch (err) {
    renderState(stateEl, "error", {
      title: "Could not load the largest duplicate clusters",
      message: err.message,
      actionLabel: "Retry",
      onAction: loadDuplicateClusterReview,
    });
  }
}

function renderDuplicateClusterReview(container, rows) {
  container.innerHTML = "";
  for (const row of rows) {
    const card = document.createElement("article");
    card.className = "rc-candidate-card";

    const head = document.createElement("div");
    head.className = "rc-candidate-card-head";
    head.innerHTML = `
      <span class="rc-candidate-path">${row.reclaimable_bytes_human} reclaimable (${row.reclaimable_bytes.toLocaleString()} bytes)</span>
      ${
        row.needs_review
          ? '<span class="rc-badge" data-tier="B" title="The kept copy sits in a less durable location than a copy being deleted">Flagged for review</span>'
          : ""
      }
    `;
    card.appendChild(head);

    const rationale = document.createElement("p");
    rationale.className = "rc-candidate-rationale";
    rationale.textContent = row.rationale;
    card.appendChild(rationale);

    card.appendChild(renderClusterTable(row.cluster));
    container.appendChild(card);
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

// `cluster.full_hash` is a BLAKE3 hex digest and every other field on this path is a
// server-formatted number/enum — safe to template into innerHTML. `member.path` is a raw
// filesystem path (attacker-controllable: this tool's whole job is walking a real disk, so a
// file/directory literally named e.g. `<img src=x onerror=...>` is real, reachable input) and
// MUST NEVER be interpolated into innerHTML — it goes through `textContent` only, below.
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

    const pathCell = document.createElement("td");
    pathCell.className = "rc-candidate-path";
    pathCell.textContent = member.path;
    row.appendChild(pathCell);

    const sizeCell = document.createElement("td");
    sizeCell.textContent = member.size_human;
    row.appendChild(sizeCell);

    const createdCell = document.createElement("td");
    createdCell.textContent = new Date(member.ctime * 1000).toLocaleString();
    row.appendChild(createdCell);

    const statusCell = document.createElement("td");
    if (member.is_keep) {
      const badge = document.createElement("span");
      badge.className = "rc-badge";
      badge.dataset.kind = "heuristic";
      badge.textContent = "Kept — heuristic pick";
      statusCell.appendChild(badge);
    } else {
      statusCell.textContent = "Proposed for removal";
    }
    row.appendChild(statusCell);

    tbody.appendChild(row);
  }
  table.appendChild(tbody);
  return table;
}

export { renderClusterTable };

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

// --- Stage 2: mode badge + power-mode dialog --------------------------------------------------

function renderModeBadge(mode) {
  const badge = document.getElementById("mode-badge");
  if (!badge) return;
  badge.dataset.mode = mode;
  badge.textContent = mode === "power" ? "Power mode" : "Safe mode";
}

async function loadModeStatus() {
  try {
    const status = await api("/api/mode");
    renderModeBadge(status.mode);
    document.getElementById("power-mode-phrase").textContent = status.required_power_confirmation;
  } catch {
    // Non-fatal: the badge just stays in its loading state if this fails.
  }
}

async function switchToSafeMode() {
  try {
    const status = await api("/api/mode/safe", { method: "POST" });
    renderModeBadge(status.mode);
  } catch {
    // Reverting to safe mode never requires confirmation and should never fail in practice;
    // if it does, the badge simply stays on whatever it last successfully rendered.
  }
}

function openPowerModeDialog() {
  const dialog = document.getElementById("power-mode-dialog");
  const input = document.getElementById("power-mode-input");
  document.getElementById("power-mode-error").textContent = "";
  input.value = "";
  dialog.hidden = false;
  input.focus();
}

function closePowerModeDialog() {
  document.getElementById("power-mode-dialog").hidden = true;
}

async function confirmPowerMode() {
  const input = document.getElementById("power-mode-input");
  const error = document.getElementById("power-mode-error");
  try {
    const status = await api("/api/mode/power", {
      method: "POST",
      body: JSON.stringify({ confirmation_text: input.value }),
    });
    renderModeBadge(status.mode);
    closePowerModeDialog();
  } catch (err) {
    error.textContent = err.message || "That didn't match the required phrase exactly.";
  }
}

function initModeControls() {
  const badge = document.getElementById("mode-badge");
  if (badge) {
    badge.addEventListener("click", () => {
      if (badge.dataset.mode === "power") {
        switchToSafeMode();
      } else {
        openPowerModeDialog();
      }
    });
  }
  document.getElementById("power-mode-cancel").addEventListener("click", closePowerModeDialog);
  document.getElementById("power-mode-confirm").addEventListener("click", confirmPowerMode);
  loadModeStatus();
}

// --- Stage 2: first-run screen -----------------------------------------------------------------

async function initFirstRun() {
  const overlay = document.getElementById("first-run-overlay");
  try {
    const status = await api("/api/first-run");
    if (!status.acknowledged) {
      overlay.hidden = false;
    }
  } catch {
    // Fail open: a broken status check must never trap the user behind an overlay they can't
    // dismiss — the acknowledge button below still works regardless.
  }
  document.getElementById("first-run-acknowledge").addEventListener("click", async () => {
    try {
      await api("/api/first-run/acknowledge", { method: "POST" });
    } catch {
      // Non-fatal — the overlay still closes; worst case it reappears next launch.
    }
    overlay.hidden = true;
  });
}

// --- Boot ------------------------------------------------------------------------------------

function init() {
  initTheme();
  document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
  initTabs();
  initScanBar();
  initReviewQueue();
  initQuickClean();
  initHowItWorks();
  initModeControls();
  initFirstRun();
  activateTab("overview");
}

document.addEventListener("DOMContentLoaded", init);
