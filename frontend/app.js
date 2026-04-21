/**
 * frontend/app.js — MetalMind Unified UI
 *
 * Single flow: Run → See results → Trust signal → Decide
 * AI Insight is on-demand (click to load per supplier).
 */

// ---------------------------------------------------------------------------
// Save supplier to database
// ---------------------------------------------------------------------------

async function syncSavedButtonStates() {
  try {
    const resp = await fetch("/api/saved-suppliers");
    if (!resp.ok) return;
    const saved = await resp.json();
    // Key → DB id so the button can issue DELETE when toggled off.
    const savedMap = new Map(saved.map(s => [`${s.supplier_name}|${s.url || ""}`, s.id]));
    document.querySelectorAll(".save-btn").forEach(btn => {
      const key = `${btn.dataset.supplierName || ""}|${btn.dataset.supplierUrl || ""}`;
      const id  = savedMap.get(key);
      const isRow = btn.classList.contains("save-btn-row");
      if (id !== undefined) {
        btn.textContent = "✓ Saved";
        btn.disabled = false;
        btn.classList.add("saved");
        btn.dataset.supplierId = String(id);
      } else {
        btn.textContent = isRow ? "Save" : "Save to My Suppliers";
        btn.disabled = false;
        btn.classList.remove("saved");
        delete btn.dataset.supplierId;
      }
    });
  } catch (_) {}
}

// Restore cached analysis + resync save buttons on page show (covers back button / bfcache)
function restoreCachedAnalysis() {
  // One-time skip: when logo was clicked from another page, skip restore once
  try {
    if (sessionStorage.getItem("metalmind_show_idle_once") === "1") {
      sessionStorage.removeItem("metalmind_show_idle_once");
      showIdle();
      return false;
    }
  } catch (_) {}
  try {
    const cached = localStorage.getItem("metalmind_last_analysis");
    if (!cached) return false;
    const parsed = JSON.parse(cached);
    if (!parsed || parsed.version !== 1 || !parsed.data || !parsed.data.winner) return false;
    renderResults(parsed.data);
    showResults();
    return true;
  } catch (_) { return false; }
}

// Logo click on same-page just shows idle without losing cache
function goToIdle() {
  showIdle();
}

window.addEventListener("pageshow", (e) => {
  // Only restore from cache when page was restored from bfcache
  // (fresh page loads are handled by _init)
  if (e.persisted) {
    const idle = document.getElementById("idle-state");
    const idleVisible = idle && idle.style.display !== "none";
    if (idleVisible) restoreCachedAnalysis();
  }
  syncSavedButtonStates();
});

async function saveSupplier(supplier, btnEl) {
  // One-click toggle: if already saved, unsave and flip back to "Save".
  if (btnEl.classList.contains("saved") && btnEl.dataset.supplierId) {
    const id = btnEl.dataset.supplierId;
    btnEl.disabled = true;
    try {
      await fetch(`/api/saved-supplier/${id}`, { method: "DELETE" });
    } catch (_) {}
    await syncSavedButtonStates();
    return;
  }

  const isRow = btnEl.classList.contains("save-btn-row");
  btnEl.disabled = true;
  btnEl.textContent = "Saving...";
  try {
    const resp = await fetch("/api/save-supplier", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        supplier_name: supplier.name,
        country: supplier.country,
        price_display: supplier.price_usd ? formatPrice(supplier.price_usd, _lastSymbol, _lastFx) : null,
        price_usd: supplier.price_usd,
        risk_level: supplier.risk_level,
        risk_score: supplier.risk_score,
        risk_reasons: supplier.risk_reasons,
        value_score: supplier.value_score,
        url: supplier.url,
        description: supplier.description,
        trust: supplier.trust,
        anomalies: supplier.anomalies,
        ai_adjustment: supplier.ai_adjustment,
      }),
    });
    if (resp.status === 409) {
      btnEl.textContent = "Saved";
      btnEl.classList.add("saved");
      syncSavedButtonStates();
      return;
    }
    if (!resp.ok) {
      btnEl.textContent = "Save";
      btnEl.disabled = false;
      return;
    }
    btnEl.textContent = "Saved";
    btnEl.classList.add("saved");
    syncSavedButtonStates();
  } catch (_) {
    btnEl.textContent = "Save";
    btnEl.disabled = false;
  }
}

let _lastSymbol = "A$", _lastFx = 1.58;

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

function _hideAllStates() {
  document.getElementById("idle-state").style.display    = "none";
  document.getElementById("loading-state").style.display = "none";
  document.getElementById("error-state").style.display   = "none";
  document.getElementById("results-state").style.display = "none";
}

function showIdle() {
  _hideAllStates();
  document.getElementById("idle-state").style.display = "flex";
  // Show "Previous Analysis Result" button only if we have cached data
  const prevBtn = document.getElementById("previous-analysis-btn");
  if (prevBtn) {
    try {
      prevBtn.style.display = localStorage.getItem("metalmind_last_analysis") ? "" : "none";
    } catch (_) { prevBtn.style.display = "none"; }
  }
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
  const prevBtn = document.getElementById("previous-analysis-btn");
  if (prevBtn) prevBtn.style.display = "none";
}


// ---------------------------------------------------------------------------
// Loading animation — simple spinner + rotating message
// ---------------------------------------------------------------------------

const _LOADING_MSGS = [
  "Searching suppliers...",
  "Analyzing data...",
  "Scoring and ranking...",
  "Almost there...",
];

function animateProgress(onDone) {
  const msgEl = document.getElementById("loading-msg");
  let i = 0;
  const interval = setInterval(() => {
    i++;
    if (i < _LOADING_MSGS.length) {
      msgEl.textContent = _LOADING_MSGS[i];
    } else {
      clearInterval(interval);
      onDone();
    }
  }, 1500);
}


// ---------------------------------------------------------------------------
// Render helpers
// ---------------------------------------------------------------------------

function countryTag(country) {
  const cls = country === "India" ? "tag-india" : country === "China" ? "tag-china" : "tag-unknown";
  return `<span class="country-tag ${cls}">${country}</span>`;
}

function riskClass(level) {
  return level === "Low" ? "risk-low" : level === "Medium" ? "risk-medium" : "risk-high";
}

function formatPrice(usd, symbol, fx) {
  return `${symbol}${(usd * fx).toFixed(2)}/sqm`;
}

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#039;");
}

function riskReasonsList(reasons) {
  if (!reasons || !reasons.length) return '<li class="risk-reason-empty">No risk signals detected.</li>';
  return reasons.map(r => `<li>${escapeHtml(r)}</li>`).join("");
}

// Anomaly block
function anomaliesHTML(anomalies) {
  if (!anomalies || !anomalies.anomalies || !anomalies.anomalies.length || anomalies.severity === "none") return "";
  const sev = anomalies.severity || "low";
  const label = sev === "high" ? "HIGH" : sev === "medium" ? "MEDIUM" : "LOW";
  const items = anomalies.anomalies.map(a => `<li>${escapeHtml(a)}</li>`).join("");
  return `<div class="anomaly-block anomaly-${sev}">
    <div class="anomaly-head"><span class="anomaly-icon">⚠</span><span class="anomaly-title">Anomalies</span><span class="anomaly-sev">${label}</span></div>
    <ul class="anomaly-list">${items}</ul></div>`;
}

// AI adjustment badge
function aiAdjBadge(supplier) {
  const adj = supplier.ai_adjustment;
  if (!adj || !adj.adjustment) return "";
  const val = adj.adjustment;
  const sign = val > 0 ? "+" : "";
  const cls = val > 0 ? "ai-adj-pos" : "ai-adj-neg";
  const tip = escapeHtml(adj.reason || "AI adjustment");
  return `<span class="ai-adj-badge ${cls}" title="${tip}">${sign}${val} AI</span>`;
}

function valueScoreHTML(supplier) {
  return `${supplier.value_score}/100 ${aiAdjBadge(supplier)}`;
}

// Trust signal — authoritative verdict labels
const TRUST_META = {
  safe:    { icon: "🛡", label: "AI VERIFIED — SAFE",    sub: "Rules and AI agree — no additional concerns" },
  warning: { icon: "🛡", label: "AI VERIFIED — WARNING", sub: "AI raised warnings not captured by rules" },
  risk:    { icon: "🛡", label: "AI VERIFIED — RISK",    sub: "AI flagged a serious concern — review alternatives" },
};

function trustBadgeHTML(trust) {
  if (!trust || !TRUST_META[trust]) return "";
  const m = TRUST_META[trust];
  return `<span class="trust-badge trust-${trust}" title="${m.sub}">${m.icon} ${m.label}</span>`;
}

// Data confidence — derived from supplier quality signals (not AI)
function dataConfidence(supplier) {
  let signals = 0;
  if (supplier.price_usd)                          signals++;
  if (supplier.url && supplier.url !== "#")         signals++;
  if (supplier.description && supplier.description.length > 80) signals++;
  if (supplier.risk_level === "Low")               signals++;
  if (supplier.risk_reasons && supplier.risk_reasons.length > 0
      && supplier.risk_reasons[0] !== "No risk signals detected") signals++;
  if (signals >= 4) return { level: "High",   cls: "conf-high" };
  if (signals >= 2) return { level: "Medium", cls: "conf-med" };
  return                    { level: "Low",    cls: "conf-low" };
}

function confidenceHTML(supplier) {
  const c = dataConfidence(supplier);
  return `<span class="data-conf ${c.cls}">Confidence: ${c.level}</span>`;
}


// ---------------------------------------------------------------------------
// On-demand AI Insight (loaded per supplier on click)
// ---------------------------------------------------------------------------

// Per-supplier cache of AI insight payloads, keyed by "name|url".
// Lives in localStorage so it survives refreshes and server restarts —
// the same supplier re-opened won't trigger another Gemma call until
// the user explicitly hits "Regenerate".
const _INSIGHT_CACHE_PREFIX = "metalmind_insight|";

function _insightCacheKey(supplier) {
  const name = (typeof supplier === "object" ? supplier?.name : supplier) || "";
  const url  = (typeof supplier === "object" ? supplier?.url  : "")       || "";
  return _INSIGHT_CACHE_PREFIX + name + "|" + url;
}

function _insightCacheGet(supplier) {
  try {
    const raw = localStorage.getItem(_insightCacheKey(supplier));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || !parsed.data) return null;
    return parsed;   // {data, generatedAt}
  } catch (_) { return null; }
}

function _insightCacheSet(supplier, data) {
  try {
    localStorage.setItem(_insightCacheKey(supplier), JSON.stringify({
      data,
      generatedAt: new Date().toISOString(),
    }));
  } catch (_) {}
}

function _insightCacheClear(supplier) {
  try { localStorage.removeItem(_insightCacheKey(supplier)); } catch (_) {}
}

function _renderInsight(data, containerEl, supplier, cached, generatedAt) {
  const section = (label, items) => {
    if (!items || !items.length) return "";
    return `<div class="ai-insight-section"><div class="ai-insight-label">${label}</div><ul>${items.map(s => `<li>${escapeHtml(s)}</li>`).join("")}</ul></div>`;
  };

  const topStrength = data.key_strengths?.[0];
  const topRisk     = data.key_risks?.[0];
  const quickLines  = [
    topStrength ? `<div class="insight-quick-line insight-quick-pos">+ ${escapeHtml(topStrength)}</div>` : "",
    topRisk     ? `<div class="insight-quick-line insight-quick-neg">- ${escapeHtml(topRisk)}</div>` : "",
  ].filter(Boolean).join("");

  const fullSection = [
    section("Key Strengths", data.key_strengths),
    section("Key Risks", data.key_risks),
    section("Hidden Signals", data.hidden_signals),
  ].filter(Boolean).join("");

  const metaLine = cached && generatedAt
    ? `<span class="insight-cache-note">Cached ${_fmtRelTime(generatedAt)}</span>`
    : "";

  containerEl.innerHTML = `
    <div class="ai-insight-body">
      <p class="ai-insight-summary">${escapeHtml(data.summary)}</p>
      ${quickLines}
      ${fullSection ? `<details class="insight-full"><summary class="insight-full-toggle">Show full analysis</summary>${fullSection}</details>` : ""}
      <div class="ai-insight-foot">
        <span class="ai-insight-conf">AI Confidence: ${(data.confidence * 100).toFixed(0)}%</span>
        ${metaLine}
        <button type="button" class="insight-regen-btn" title="Re-run Gemma for a fresh insight (uses API call)">↻ Regenerate</button>
      </div>
    </div>
  `;

  const regenBtn = containerEl.querySelector(".insight-regen-btn");
  if (regenBtn) {
    regenBtn.addEventListener("click", () => {
      _insightCacheClear(supplier);
      fetchInsight(supplier, containerEl, { forceRefresh: true });
    });
  }
}

function _fmtRelTime(iso) {
  try {
    const diffSec = (Date.now() - new Date(iso).getTime()) / 1000;
    if (diffSec < 60)       return "just now";
    if (diffSec < 3600)     return Math.floor(diffSec / 60) + "m ago";
    if (diffSec < 86400)    return Math.floor(diffSec / 3600) + "h ago";
    return Math.floor(diffSec / 86400) + "d ago";
  } catch (_) { return "recently"; }
}

// supplier = the full SupplierOut object (not just the name).
// Passing extra fields so the backend can reconstruct context when its
// in-memory _last_ranked cache has been cleared (server restart, or the
// user is viewing a restored-from-localStorage analysis).
//
// Cache flow:
//   - First call → hits /api/insight, stores response in localStorage
//   - Subsequent calls for the same supplier → served from localStorage
//   - User clicks "↻ Regenerate" → clears cache and re-fetches
async function fetchInsight(supplier, containerEl, opts = {}) {
  const { forceRefresh = false } = opts;
  const name = typeof supplier === "string" ? supplier : supplier?.name;

  // Cache hit → render immediately, no API call
  if (!forceRefresh) {
    const cached = _insightCacheGet(supplier);
    if (cached) {
      _renderInsight(cached.data, containerEl, supplier, true, cached.generatedAt);
      return;
    }
  }

  containerEl.innerHTML = '<div class="insight-loading">Loading AI insight...</div>';

  const body = { name };
  if (typeof supplier === "object" && supplier) {
    if (supplier.country)     body.country     = supplier.country;
    if (supplier.url)         body.url         = supplier.url;
    if (supplier.description) body.description = supplier.description;
    if (supplier.price_usd != null) body.price_usd = supplier.price_usd;
  }

  try {
    const resp = await fetch("/api/insight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      containerEl.innerHTML = `<div class="insight-error">${escapeHtml(err.detail || "Failed to load insight.")}</div>`;
      return;
    }
    const data = await resp.json();
    if (data.source === "fallback") {
      containerEl.innerHTML = '<div class="insight-error">AI insight unavailable for this supplier.</div>';
      return;
    }

    _insightCacheSet(supplier, data);
    _renderInsight(data, containerEl, supplier, false);
  } catch (_) {
    containerEl.innerHTML = '<div class="insight-error">Could not reach the server.</div>';
  }
}


// ---------------------------------------------------------------------------
// Render results
// ---------------------------------------------------------------------------

function renderResults(data) {
  const { winner, top3, all_suppliers, explanation, risk_note, symbol, fx, decision, trust } = data;
  _lastSymbol = symbol; _lastFx = fx;

  document.getElementById("all-suppliers-count").textContent = `${all_suppliers.length}`;

  // Winner badge
  const badge = document.getElementById("winner-badge");
  const isRec = (decision || "recommended") === "recommended";
  badge.textContent = isRec ? "Recommended" : "Not Recommended";
  badge.classList.toggle("winner-badge-not-recommended", !isRec);

  // Trust signal banner — authoritative verdict
  const trustBanner = document.getElementById("trust-banner");
  if (trust && TRUST_META[trust]) {
    const m = TRUST_META[trust];
    const conf = dataConfidence(winner);
    trustBanner.style.display = "";
    trustBanner.className = `trust-banner trust-banner-${trust}`;
    document.getElementById("trust-icon").textContent  = m.icon;
    document.getElementById("trust-title").textContent = m.label;
    document.getElementById("trust-sub").innerHTML     =
      `${escapeHtml(m.sub)} <span class="data-conf ${conf.cls}">Confidence: ${conf.level}</span>`;
  } else {
    trustBanner.style.display = "none";
  }

  // Winner card
  document.getElementById("winner-name").textContent       = winner.name;
  const winnerUrlEl = document.getElementById("winner-url");
  if (winnerUrlEl) {
    winnerUrlEl.innerHTML = winner.url ? `<a href="${winner.url}" target="_blank" rel="noopener">${winner.url}</a>` : "";
  }
  document.getElementById("winner-explanation").textContent = explanation;
  document.getElementById("winner-risk-note").textContent   = risk_note;
  document.getElementById("winner-price").textContent = winner.price_usd ? formatPrice(winner.price_usd, symbol, fx) : "—";
  const winnerScoreEl = document.getElementById("winner-score");
  winnerScoreEl.innerHTML = valueScoreHTML(winner);
  document.getElementById("winner-country").outerHTML =
    `<span class="country-tag ${winner.country === "India" ? "tag-india" : winner.country === "China" ? "tag-china" : "tag-unknown"}" id="winner-country">${winner.country}</span>`;

  const riskEl = document.getElementById("winner-risk");
  riskEl.textContent = winner.risk_level;
  riskEl.className   = `kpi-value ${riskClass(winner.risk_level)}`;

  const winnerAnomaliesEl = document.getElementById("winner-anomalies");
  if (winnerAnomaliesEl) winnerAnomaliesEl.innerHTML = anomaliesHTML(winner.anomalies);

  const winnerSaveBtn = document.getElementById("winner-save-btn");
  if (winnerSaveBtn) {
    winnerSaveBtn.textContent = "Save to My Suppliers";
    winnerSaveBtn.disabled = false;
    winnerSaveBtn.classList.remove("saved");
    winnerSaveBtn.dataset.supplierName = winner.name;
    winnerSaveBtn.dataset.supplierUrl = winner.url || "";
    winnerSaveBtn.onclick = () => saveSupplier(winner, winnerSaveBtn);
  }

  // Winner AI Insight (on-demand)
  const winnerInsight = document.getElementById("winner-ai-insight");
  if (winnerInsight) {
    winnerInsight.open = false;
    const container = winnerInsight.querySelector(".ai-insight-container");
    container.innerHTML = "";
    let winnerInsightLoaded = false;
    winnerInsight.ontoggle = () => {
      if (winnerInsight.open && !winnerInsightLoaded) {
        winnerInsightLoaded = true;
        fetchInsight(winner, container);
      }
    };
  }

  // Top 3 cards
  const top3Container = document.getElementById("top3-cards");
  top3Container.innerHTML = "";
  top3.forEach((s, idx) => {
    const rankClass = idx === 0 ? "rank-1" : idx === 1 ? "rank-2" : "";
    const card = document.createElement("div");
    card.className = `supplier-card ${rankClass}`;
    card.innerHTML = `
      <div class="card-rank">${idx + 1}</div>
      <div class="card-name">${s.name}</div>
      <div class="card-meta">
        ${countryTag(s.country)}
        ${trustBadgeHTML(s.trust)}
        ${confidenceHTML(s)}
      </div>
      <div class="card-stats">
        <div><div class="card-stat-label">Est. Price</div><div class="card-stat-value">${s.price_usd ? formatPrice(s.price_usd, symbol, fx) : "—"}</div></div>
        <div><div class="card-stat-label">Risk</div><div class="card-stat-value ${riskClass(s.risk_level)}">${s.risk_level}</div></div>
        <div><div class="card-stat-label">Value Score</div><div class="card-stat-value">${valueScoreHTML(s)}</div></div>
      </div>
      <div class="card-url"><a href="${s.url}" target="_blank" rel="noopener">${s.url}</a></div>
      ${anomaliesHTML(s.anomalies)}
      <details class="card-risk-details"><summary>Risk Details</summary><ul class="risk-reasons">${riskReasonsList(s.risk_reasons)}</ul></details>
      <details class="ai-insight" data-supplier="${escapeHtml(s.name)}">
        <summary>AI Insight</summary>
        <div class="ai-insight-container"></div>
      </details>
      <button type="button" class="save-btn save-btn-card" data-supplier-name="${escapeHtml(s.name)}" data-supplier-url="${escapeHtml(s.url)}">Save to My Suppliers</button>
    `;
    const saveBtn = card.querySelector(".save-btn");
    saveBtn.addEventListener("click", () => saveSupplier(s, saveBtn));
    // Wire on-demand insight loading
    const insightDetails = card.querySelector(".ai-insight");
    let insightLoaded = false;
    insightDetails.addEventListener("toggle", () => {
      if (insightDetails.open && !insightLoaded) {
        insightLoaded = true;
        fetchInsight(s, insightDetails.querySelector(".ai-insight-container"));
      }
    });
    top3Container.appendChild(card);
  });

  // All suppliers table
  const tbody = document.getElementById("all-tbody");
  tbody.innerHTML = "";
  all_suppliers.forEach((s, idx) => {
    const tr = document.createElement("tr");
    tr.className = `supplier-row ${idx === 0 ? "winner-row" : ""}`;
    tr.innerHTML = `
      <td><span class="rank-badge ${idx === 0 ? "r1" : idx === 1 ? "r2" : ""}">${s.rank}</span></td>
      <td>${s.name} ${trustBadgeHTML(s.trust)}</td>
      <td>${countryTag(s.country)}</td>
      <td>${s.price_usd ? formatPrice(s.price_usd, symbol, fx) : "—"}</td>
      <td class="${riskClass(s.risk_level)}">${s.risk_level}</td>
      <td>${valueScoreHTML(s)}</td>
      <td><a href="${s.url}" target="_blank" rel="noopener" style="font-size:12px" onclick="event.stopPropagation()">${new URL(s.url).hostname}</a></td>
      <td class="row-actions-cell"><div class="row-actions-inner">
        <span class="row-details-btn" data-action="details"><span class="row-details-btn-text">View</span><span class="row-details-btn-chevron">▸</span></span>
        <button class="row-insight-btn" data-action="insight" onclick="event.stopPropagation()">AI Insight</button>
        <button class="save-btn save-btn-row" data-action="save" data-supplier-name="${escapeHtml(s.name)}" data-supplier-url="${escapeHtml(s.url)}" onclick="event.stopPropagation()">Save</button>
      </div></td>
    `;
    tbody.appendChild(tr);

    const detailsRow = document.createElement("tr");
    detailsRow.className = "supplier-row-details";
    detailsRow.innerHTML = `<td colspan="8"><div class="row-details-inner">
      ${anomaliesHTML(s.anomalies)}
      <div class="row-details-header">Risk Details</div>
      <ul class="risk-reasons">${riskReasonsList(s.risk_reasons)}</ul>
      <div class="row-insight-panel" style="display:none;"></div>
    </div></td>`;
    tbody.appendChild(detailsRow);

    // "View" — toggle risk details
    const btnText = tr.querySelector(".row-details-btn-text");
    tr.querySelector('[data-action="details"]').addEventListener("click", (e) => {
      e.stopPropagation();
      const expanded = tr.classList.toggle("is-expanded");
      detailsRow.classList.toggle("is-visible", expanded);
      if (btnText) btnText.textContent = expanded ? "Hide" : "View";
    });

    // "Save" button
    const saveRowBtn = tr.querySelector('[data-action="save"]');
    saveRowBtn.addEventListener("click", () => saveSupplier(s, saveRowBtn));

    // "🔍 AI Insight" — expand row + load insight on demand
    let insightLoaded = false;
    tr.querySelector('[data-action="insight"]').addEventListener("click", () => {
      // Ensure row is expanded
      if (!tr.classList.contains("is-expanded")) {
        tr.classList.add("is-expanded");
        detailsRow.classList.add("is-visible");
        if (btnText) btnText.textContent = "Hide";
      }

      const panel = detailsRow.querySelector(".row-insight-panel");
      panel.style.display = "";

      if (!insightLoaded) {
        insightLoaded = true;
        fetchInsight(s, panel);
      }

      // Scroll the insight into view
      setTimeout(() => panel.scrollIntoView({ behavior: "smooth", block: "nearest" }), 100);
    });
  });

  // After all supplier cards + rows are in the DOM, cross-check against
  // /api/saved-suppliers so the Save buttons show the correct "✓ Saved"
  // state. Without this, a page refresh or cache-restore would always
  // render every button as "Save" even when the supplier is already saved.
  syncSavedButtonStates();
}


// ---------------------------------------------------------------------------
// Main: single unified analysis
// ---------------------------------------------------------------------------

async function runAnalysis() {
  const btn = document.getElementById("run-btn");
  if (btn) btn.classList.add("btn-running");

  showLoading();

  let apiDone = false, animDone = false, apiResult = null, apiError = null;

  animateProgress(() => {
    animDone = true;
    if (apiDone) finalize(apiResult, apiError);
  });

  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_results: 10 }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      apiError = err.detail || `Server error (${resp.status})`;
    } else {
      apiResult = await resp.json();
    }
  } catch (_) {
    apiError = "Could not reach the server. Make sure the backend is running.";
  }

  apiDone = true;
  if (animDone) finalize(apiResult, apiError);
}

function finalize(data, error) {
  const btn = document.getElementById("run-btn");
  if (btn) btn.classList.remove("btn-running");

  if (error || !data) {
    showError(error || "No data returned.");
    return;
  }

  // Cache the result so Back-to-Analysis-Result restores it without re-running
  // Use localStorage so cache survives browser close/reopen (saves API cost)
  try { localStorage.setItem("metalmind_last_analysis", JSON.stringify({ version: 1, data: data })); } catch (_) {}

  renderResults(data);
  showResults();
}


// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(function _init() {
  // Show "View Last Analysis Result" button if we have a cache (but don't auto-restore)
  const prevBtn = document.getElementById("previous-analysis-btn");
  if (prevBtn) {
    try {
      prevBtn.style.display = localStorage.getItem("metalmind_last_analysis") ? "" : "none";
    } catch (_) {}
  }

  // ?restore=1 — coming back from My Suppliers or a saved-supplier page.
  // Skip the idle UI and drop the user straight back on the last analysis
  // result. Uses the same cache reader as the "View Last Analysis" button.
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("restore") === "1") {
      const ok = restoreCachedAnalysis();
      // Clean the URL so a refresh later doesn't keep auto-restoring if
      // the cache is intentionally cleared.
      if (ok) history.replaceState({}, "", window.location.pathname);
    }
  } catch (_) {}

  // Wire principle modal
  const badge   = document.getElementById("principle-badge");
  const overlay = document.getElementById("principle-modal");
  const closeBtn = document.getElementById("principle-modal-close");
  if (badge && overlay) {
    const open  = () => { overlay.hidden = false; document.body.classList.add("modal-open"); };
    const close = () => { overlay.hidden = true;  document.body.classList.remove("modal-open"); };
    badge.addEventListener("click", open);
    closeBtn?.addEventListener("click", close);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !overlay.hidden) close(); });
  }
})();
