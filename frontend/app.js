/**
 * frontend/app.js — MetalMind UI logic
 *
 * Calls POST /api/compare and renders the results.
 */

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

function _hideAllStates() {
  document.getElementById("idle-state").style.display    = "none";
  document.getElementById("loading-state").style.display = "none";
  document.getElementById("error-state").style.display   = "none";
  document.getElementById("results-state").style.display = "none";
}

// ---------------------------------------------------------------------------
// Run-state — legacy sidebar run buttons were removed; these are now no-ops
// but kept so the runner functions below don't need conditional logic.
// ---------------------------------------------------------------------------

function _setRunning(_activeId)  { /* no sidebar run buttons on analysis page */ }
function _resetRunButtons()      { /* no sidebar run buttons on analysis page */ }

function showIdle() {
  _hideAllStates();
  document.getElementById("idle-state").style.display = "flex";
}

function showLoading() {
  _hideAllStates();
  document.getElementById("loading-state").style.display = "flex";
}

function showError(msg) {
  _hideAllStates();
  document.getElementById("error-state").style.display = "flex";
  document.getElementById("error-msg").textContent = msg;
}

function showResults() {
  _hideAllStates();
  document.getElementById("results-state").style.display = "block";
}


// ---------------------------------------------------------------------------
// Progress animation
// ---------------------------------------------------------------------------

const STEPS = [
  "Searching India suppliers via Tavily...",
  "Searching China suppliers via Tavily...",
  "Cleaning and extracting data...",
  "Scoring risk for each supplier...",
  "Computing value scores...",
  "Ranking and selecting Top 3...",
  "Generating recommendation...",
];

function animateProgress(onDone) {
  const container = document.getElementById("progress-steps");
  const msgEl     = document.getElementById("loading-msg");
  container.innerHTML = "";

  // Create all step elements
  const stepEls = STEPS.map(text => {
    const el = document.createElement("div");
    el.className = "progress-step";
    el.textContent = text;
    container.appendChild(el);
    return el;
  });

  let i = 0;
  const interval = setInterval(() => {
    if (i > 0) stepEls[i - 1].classList.replace("active", "done");
    if (i < stepEls.length) {
      stepEls[i].classList.add("active");
      msgEl.textContent = STEPS[i];
      i++;
    } else {
      clearInterval(interval);
      onDone();
    }
  }, 400);
}

// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

function countryTag(country) {
  const cls = country === "India" ? "tag-india"
            : country === "China" ? "tag-china"
            : "tag-unknown";
  return `<span class="country-tag ${cls}">${country}</span>`;
}

function riskClass(level) {
  return level === "Low" ? "risk-low" : level === "Medium" ? "risk-medium" : "risk-high";
}

function formatPrice(usd, symbol, fx) {
  return `${symbol}${(usd * fx).toFixed(2)}/sqm`;
}

// ---------------------------------------------------------------------------
// Render results
// ---------------------------------------------------------------------------

const TIER_META = {
  green:  { icon: "🟢", title: "Strongly Recommended",       subtitle: "Approved by both rule-based scoring and AI" },
  yellow: { icon: "🟡", title: "Recommended with Caution",   subtitle: "AI detected additional risks — review before choosing" },
  red:    { icon: "🔴", title: "Not Recommended",            subtitle: "AI rejects this supplier — consider the next option" },
};

function tierBadgeHTML(tier) {
  if (!tier || !TIER_META[tier]) return "";
  const m = TIER_META[tier];
  return `<span class="tier-badge tier-${tier}" title="${m.title}">${m.icon} ${m.title}</span>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function riskReasonsList(reasons) {
  if (!reasons || !reasons.length) {
    return '<li class="risk-reason-empty">No risk signals detected.</li>';
  }
  return reasons.map(r => `<li>${escapeHtml(r)}</li>`).join("");
}

function renderResults(data) {
  const { winner, top3, all_suppliers, explanation, risk_note, symbol, fx, decision } = data;

  const countEl = document.getElementById("all-suppliers-count");
  if (countEl) countEl.textContent = `${all_suppliers.length}`;

  // Winner badge — reflects backend decision (recommended vs not_recommended)
  const badge = document.getElementById("winner-badge");
  if (badge) {
    const isRec = (decision || "recommended") === "recommended";
    badge.textContent = isRec ? "Recommended" : "Not Recommended";
    badge.classList.toggle("winner-badge-not-recommended", !isRec);
  }

  // Winner decision-tier banner (AI Comparison mode only)
  const tierBanner = document.getElementById("winner-decision-banner");
  if (tierBanner) {
    if (winner.decision_tier && TIER_META[winner.decision_tier]) {
      const m = TIER_META[winner.decision_tier];
      tierBanner.style.display = "";
      tierBanner.className = `decision-banner decision-banner-${winner.decision_tier}`;
      document.getElementById("winner-decision-icon").textContent     = m.icon;
      document.getElementById("winner-decision-title").textContent    = m.title;
      document.getElementById("winner-decision-subtitle").textContent = m.subtitle;
    } else {
      tierBanner.style.display = "none";
    }
  }

  // Winner card
  document.getElementById("winner-name").textContent    = winner.name;
  document.getElementById("winner-explanation").textContent = explanation;
  document.getElementById("winner-risk-note").textContent   = risk_note;
  document.getElementById("winner-price").textContent   = winner.price_usd ? formatPrice(winner.price_usd, symbol, fx) : "—";
  document.getElementById("winner-score").textContent   = `${winner.value_score}/100`;
  document.getElementById("winner-country").outerHTML   =
    `<span class="country-tag ${winner.country === "India" ? "tag-india" : winner.country === "China" ? "tag-china" : "tag-unknown"}" id="winner-country">${winner.country}</span>`;

  const riskEl = document.getElementById("winner-risk");
  riskEl.textContent  = winner.risk_level;
  riskEl.className    = `kpi-value ${riskClass(winner.risk_level)}`;

  // Top 3 cards
  const top3Container = document.getElementById("top3-cards");
  top3Container.innerHTML = "";
  top3.forEach((s, idx) => {
    const rankClass = idx === 0 ? "rank-1" : idx === 1 ? "rank-2" : "";
    const rankLabel = String(idx + 1);
    const card = document.createElement("div");
    card.className = `supplier-card ${rankClass}`;
    card.innerHTML = `
      <div class="card-rank">${rankLabel}</div>
      <div class="card-name">${s.name}</div>
      <div class="card-meta">
        ${countryTag(s.country)}
        ${tierBadgeHTML(s.decision_tier)}
      </div>
      <div class="card-stats">
        <div>
          <div class="card-stat-label">Est. Price</div>
          <div class="card-stat-value">${s.price_usd ? formatPrice(s.price_usd, symbol, fx) : "—"}</div>
        </div>
        <div>
          <div class="card-stat-label">Risk</div>
          <div class="card-stat-value ${riskClass(s.risk_level)}">${s.risk_level}</div>
        </div>
        <div>
          <div class="card-stat-label">Value Score</div>
          <div class="card-stat-value">${s.value_score}/100</div>
        </div>
        <div>
          <div class="card-stat-label">Price Found</div>
          <div class="card-stat-value" style="font-size:12px;color:var(--muted)">${s.price_raw}</div>
        </div>
      </div>
      <div class="card-url">
        <a href="${s.url}" target="_blank" rel="noopener">${s.url}</a>
      </div>
      <details class="card-risk-details">
        <summary>Risk Details</summary>
        <ul class="risk-reasons">${riskReasonsList(s.risk_reasons)}</ul>
      </details>
    `;
    top3Container.appendChild(card);
  });

  // All suppliers table — each supplier renders as a main row + a hidden
  // details row. Clicking the main row (or its chevron) toggles expansion.
  const tbody = document.getElementById("all-tbody");
  tbody.innerHTML = "";
  all_suppliers.forEach((s, idx) => {
    const rankBadgeClass = idx === 0 ? "r1" : idx === 1 ? "r2" : "";
    const rowClass = idx === 0 ? "winner-row" : "";

    const tr = document.createElement("tr");
    tr.className = `supplier-row ${rowClass}`;
    tr.innerHTML = `
      <td><span class="rank-badge ${rankBadgeClass}">${s.rank}</span></td>
      <td>${s.name}</td>
      <td>${countryTag(s.country)}</td>
      <td>${s.price_usd ? formatPrice(s.price_usd, symbol, fx) : "—"}</td>
      <td class="${riskClass(s.risk_level)}">${s.risk_level}</td>
      <td>${s.value_score}/100</td>
      <td><a href="${s.url}" target="_blank" rel="noopener" style="font-size:12px" onclick="event.stopPropagation()">${new URL(s.url).hostname}</a></td>
      <td class="row-details-cell">
        <span class="row-details-btn">
          <span class="row-details-btn-text">View</span>
          <span class="row-details-btn-chevron">▸</span>
        </span>
      </td>
    `;
    tbody.appendChild(tr);

    const detailsRow = document.createElement("tr");
    detailsRow.className = "supplier-row-details";
    detailsRow.innerHTML = `
      <td colspan="8">
        <div class="row-details-inner">
          <div class="row-details-header">Risk Details</div>
          <ul class="risk-reasons">${riskReasonsList(s.risk_reasons)}</ul>
        </div>
      </td>
    `;
    tbody.appendChild(detailsRow);

    const btnText = tr.querySelector(".row-details-btn-text");
    tr.addEventListener("click", () => {
      const expanded = tr.classList.toggle("is-expanded");
      detailsRow.classList.toggle("is-visible", expanded);
      if (btnText) btnText.textContent = expanded ? "Hide" : "View";
    });
  });
}

// ---------------------------------------------------------------------------
// Main: run comparison
// ---------------------------------------------------------------------------

async function runComparison() {
  const maxResults = 8;
  const priority   = _currentPriority();

  showLoading();
  _setRunning("run-btn");

  let apiDone   = false;
  let animDone  = false;
  let apiResult = null;
  let apiError  = null;

  animateProgress(() => {
    animDone = true;
    if (apiDone) finalize(apiResult, apiError);
  });

  try {
    const resp = await fetch("/api/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        max_results: maxResults,
        priority,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      apiError = err.detail || "Unknown error from server.";
    } else {
      apiResult = await resp.json();
    }
  } catch (e) {
    apiError = "Could not reach the server. Make sure the backend is running.";
  }

  apiDone = true;
  if (animDone) finalize(apiResult, apiError);
}

function finalize(data, error) {
  _resetRunButtons();

  if (error || !data) {
    showError(error || "No data returned.");
    return;
  }

  renderResults(data);
  showResults();
}

// ---------------------------------------------------------------------------
// Phase 2 — Expert vs AI Comparison
// ---------------------------------------------------------------------------

const AI_STEPS = [
  "Searching suppliers via Tavily...",
  "Cleaning and extracting data...",
  "Running rule-based expert scoring...",
  "Querying AI (Gemma) for each supplier...",
  "Comparing Expert vs AI decisions...",
];

function animateAiProgress(onDone) {
  const container = document.getElementById("progress-steps");
  const msgEl     = document.getElementById("loading-msg");
  container.innerHTML = "";

  const stepEls = AI_STEPS.map(text => {
    const el = document.createElement("div");
    el.className = "progress-step";
    el.textContent = text;
    container.appendChild(el);
    return el;
  });

  let i = 0;
  const interval = setInterval(() => {
    if (i > 0) stepEls[i - 1].classList.replace("active", "done");
    if (i < stepEls.length) {
      stepEls[i].classList.add("active");
      msgEl.textContent = AI_STEPS[i];
      i++;
    } else {
      // Loop the last step while AI is still running
      msgEl.textContent = "Still querying AI — this may take a minute...";
      clearInterval(interval);
      onDone();
    }
  }, 900);
}

async function runAiComparison() {
  const priority = _currentPriority();

  showLoading();
  _setRunning("run-ai-btn");

  let apiDone = false, animDone = false, apiResult = null, apiError = null;

  animateAiProgress(() => { animDone = true; if (apiDone) finalizeAi(apiResult, apiError); });

  try {
    const resp = await fetch("/api/compare-with-ai", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ max_results: 8, priority }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      apiError = err.detail || `Server error (${resp.status})`;
    } else {
      apiResult = await resp.json();
    }
  } catch (_) {
    apiError = "Could not reach the server.";
  }

  apiDone = true;
  if (animDone) finalizeAi(apiResult, apiError);
}

function finalizeAi(data, error) {
  _resetRunButtons();

  if (error || !data) {
    showError(error || "No AI comparison data returned.");
    return;
  }

  // /api/compare-with-ai now returns the SAME shape as /api/compare,
  // with per-supplier decision_tier added. Reuse the standard renderer.
  renderResults(data);
  showResults();
}

// ---------------------------------------------------------------------------
// Phase 2.5 — AI-Only Analysis
// ---------------------------------------------------------------------------

const AI_ONLY_STEPS = [
  "Searching suppliers via Tavily...",
  "Cleaning and extracting data...",
  "Selecting top candidates by relevance...",
  "Querying AI (Gemma) for each supplier...",
  "Sorting by AI score...",
];

function animateAiOnlyProgress(onDone) {
  const container = document.getElementById("progress-steps");
  const msgEl     = document.getElementById("loading-msg");
  container.innerHTML = "";

  const stepEls = AI_ONLY_STEPS.map(text => {
    const el = document.createElement("div");
    el.className = "progress-step";
    el.textContent = text;
    container.appendChild(el);
    return el;
  });

  let i = 0;
  const interval = setInterval(() => {
    if (i > 0) stepEls[i - 1].classList.replace("active", "done");
    if (i < stepEls.length) {
      stepEls[i].classList.add("active");
      msgEl.textContent = AI_ONLY_STEPS[i];
      i++;
    } else {
      msgEl.textContent = "Still querying AI — this may take a minute...";
      clearInterval(interval);
      onDone();
    }
  }, 900);
}

async function runAiOnly() {
  const priority = _currentPriority();

  showLoading();
  _setRunning("run-ai-only-btn");

  let apiDone = false, animDone = false, apiResult = null, apiError = null;

  animateAiOnlyProgress(() => { animDone = true; if (apiDone) finalizeAiOnly(apiResult, apiError); });

  try {
    const resp = await fetch("/api/ai-only", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ max_results: 8, priority }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      apiError = err.detail || `Server error (${resp.status})`;
    } else {
      apiResult = await resp.json();
    }
  } catch (_) {
    apiError = "Could not reach the server.";
  }

  apiDone = true;
  if (animDone) finalizeAiOnly(apiResult, apiError);
}

function finalizeAiOnly(data, error) {
  _resetRunButtons();

  if (error || !data) {
    showError(error || "No AI results returned.");
    return;
  }

  // AI-only results use the SAME response shape as /api/compare,
  // so we reuse the exact same renderer + state panel.
  renderResults(data);
  showResults();
}

// ---------------------------------------------------------------------------
// URL-based navigation — each mode is a dedicated "page" under /analysis
// ---------------------------------------------------------------------------

const _MODE_TO_RUNNER = {
  rule:       () => runComparison(),
  ai:         () => runAiOnly(),
  comparison: () => runAiComparison(),
};

const _MODE_TO_TITLE = {
  rule:       "Calculation-Based Analysis (No AI)",
  ai:         "AI Analysis",
  comparison: "AI Comparison (Expert vs AI)",
};

// Country priority source of truth:
//   - Landing page: the active .priority-pill
//   - Analysis page: the ?priority URL param (set when the user came from landing)
// Defaults to "Both Equal" if neither is present.
function _currentPriority() {
  const active = document.querySelector(".priority-pill.is-active");
  if (active) return active.dataset.value;
  const param = new URLSearchParams(window.location.search).get("priority");
  return param || "Both Equal";
}

// Risk > Price explainer modal — opens on badge click, closes on backdrop
// click, the × button, or ESC. Locks body scroll while open.
function _wirePrincipleModal() {
  const badge   = document.getElementById("principle-badge");
  const overlay = document.getElementById("principle-modal");
  const closeBtn = document.getElementById("principle-modal-close");
  if (!badge || !overlay) return;

  const open = () => {
    overlay.hidden = false;
    document.body.classList.add("modal-open");
  };
  const close = () => {
    overlay.hidden = true;
    document.body.classList.remove("modal-open");
  };

  badge.addEventListener("click", open);
  closeBtn?.addEventListener("click", close);
  overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !overlay.hidden) close();
  });
}

function _wireLandingPriorityPills() {
  const pills = document.querySelectorAll(".priority-pill");
  pills.forEach(pill => {
    pill.addEventListener("click", () => {
      pills.forEach(p => {
        p.classList.remove("is-active");
        p.setAttribute("aria-checked", "false");
      });
      pill.classList.add("is-active");
      pill.setAttribute("aria-checked", "true");
    });
  });
}

function goToAnalysis(mode) {
  const priority = _currentPriority();
  const url = `/analysis?mode=${encodeURIComponent(mode)}&priority=${encodeURIComponent(priority)}`;
  window.open(url, "_blank", "noopener");
}

// On page load:
//   - No ?mode    → landing page (wire up priority pills)
//   - ?mode=X[&priority] → analysis page (apply mode theme, run)
(function _bootFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const mode   = params.get("mode");
  const runner = _MODE_TO_RUNNER[mode];

  if (!runner) {
    document.body.classList.add("landing-mode");
    _wireLandingPriorityPills();
    _wirePrincipleModal();
    return;
  }

  // Analysis page
  document.body.classList.add("analysis-mode");

  const titleEl = document.getElementById("results-mode-name");
  if (titleEl) titleEl.textContent = _MODE_TO_TITLE[mode] || "";

  // Apply mode theme to results container — drives accent colors + gradient border
  const resultsEl = document.getElementById("results-state");
  if (resultsEl) {
    resultsEl.classList.remove("mode-rule", "mode-ai", "mode-compare");
    const modeClass = { rule: "mode-rule", ai: "mode-ai", comparison: "mode-compare" }[mode];
    if (modeClass) resultsEl.classList.add(modeClass);
  }

  runner();
})();
