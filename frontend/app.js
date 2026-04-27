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
        price_display: supplier.price_usd ? formatPrice(supplier.price_usd, _lastSymbol, _lastFx, supplier.price_unit, supplier.price_unit_source) : null,
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
  // Reset chat UI to its default state (visible form, hidden preview/clarification).
  hide("parse-preview");
  hide("clarification");
  show("chat-form");
  _pendingParsed = null;
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

// Multi-unit aware price formatting. unit defaults to "sqm" for backward
// compat with the legacy ACP flow. unitSource lets us flag estimates with
// "(est.)" so users know when the unit was inferred vs scraped explicitly.
function formatPrice(usd, symbol, fx, unit, unitSource) {
  if (usd == null) return "—";
  const u = escapeHtml(unit && unit !== "unknown" ? unit : "unit");
  const head = `${symbol}${(usd * fx).toFixed(2)}/${u}`;
  if (unitSource && unitSource !== "regex") {
    const tag = unitSource === "keyword" ? "est." : unitSource === "category" ? "est." : "?";
    return `${head} <span class="unit-est unit-est-${unitSource}">(${tag})</span>`;
  }
  return head;
}

// ---------------------------------------------------------------------------
// Hybrid pricing helpers (path C): per-supplier model estimate + market
// range status badges.
// ---------------------------------------------------------------------------

// Badge shown next to a real extracted price, based on its ratio to the
// per-country market midpoint (backend classifies into 4 buckets).
function priceRangeBadge(status) {
  if (!status) return "";
  const meta = {
    within:           { label: "✓ Within market range",    cls: "pr-within" },
    above:            { label: "⚠ Above market",           cls: "pr-above" },
    far_above:        { label: "🚨 Far above market",      cls: "pr-far-above" },
    suspicious_low:   { label: "🚨 Suspiciously low",      cls: "pr-low" },
  }[status];
  if (!meta) return "";
  return `<span class="price-range-badge ${meta.cls}">${meta.label}</span>`;
}

// Sub-line shown UNDER "Quote on request" when we have a per-supplier
// model estimate. Visually secondary (small + grey + ⚠) per product rules.
// `fallbackUnit` comes from market_reference so estimates for no-price
// suppliers still show a sensible unit (e.g. "sqm") instead of "unknown".
function priceEstimateLine(supplier, symbol, fx, fallbackUnit) {
  const lo = supplier.price_estimated_low_usd;
  const hi = supplier.price_estimated_high_usd;
  if (lo == null || hi == null) return "";
  const raw = escapeHtml(supplier.price_unit && supplier.price_unit !== "unknown"
    ? supplier.price_unit
    : (fallbackUnit || "unit"));
  return `<div class="price-estimate">
    <span class="price-estimate-label">Estimated: ${symbol}${(lo * fx).toFixed(2)}–${symbol}${(hi * fx).toFixed(2)}/${raw}</span>
    <span class="price-estimate-warn" title="Estimated from country, supplier type, finish and scale signals — not a quoted price.">⚠ model</span>
  </div>`;
}

// Main price cell: prefers real price + status badge, falls back to
// "Quote on request" + estimate range sub-line. Estimates are NEVER
// shown alongside a real price (product rule: one source of truth).
function renderPriceCell(s, symbol, fx, fallbackUnit) {
  if (s.price_usd != null) {
    return `${formatPrice(s.price_usd, symbol, fx, s.price_unit, s.price_unit_source)}
            ${priceRangeBadge(s.price_range_status)}`;
  }
  const rfq = `<span class="price-rfq" title="Supplier does not publish prices online — contact for a quote">Quote on request</span>`;
  const est = priceEstimateLine(s, symbol, fx, fallbackUnit);
  return est ? `${rfq}${est}` : rfq;
}

// Top-of-results market reference banner. Hidden (display:none) when the
// backend couldn't produce a reference (e.g. category=unknown).
function renderMarketReferenceBanner(ref, symbol) {
  const el = document.getElementById("market-reference");
  const samplesEl = document.getElementById("market-reference-samples");
  if (!el) return;
  if (!ref) { el.style.display = "none"; return; }

  const catLabel = _humanCategory(ref.category);
  const scope    = (ref.country_scope && ref.country_scope.length)
    ? ref.country_scope.join(", ")
    : "Global";
  const labelEl = document.getElementById("market-reference-label");
  const rangeEl = document.getElementById("market-reference-range");
  if (labelEl) labelEl.textContent = `${catLabel} market reference (${scope})`;
  if (rangeEl) {
    rangeEl.innerHTML = `<strong>${symbol}${ref.low_aud.toFixed(2)} – ${symbol}${ref.high_aud.toFixed(2)}</strong> / ${escapeHtml(ref.unit)}`;
  }

  if (samplesEl) {
    if (ref.samples_from_search > 0 && ref.samples_low_aud != null) {
      const n = ref.samples_from_search;
      const lo = ref.samples_low_aud.toFixed(2);
      const hi = ref.samples_high_aud.toFixed(2);
      samplesEl.textContent = `Your search: ${n} supplier${n === 1 ? "" : "s"} quoted ${symbol}${lo}${lo !== hi ? `–${symbol}${hi}` : ""} / ${ref.unit}`;
      samplesEl.style.display = "";
    } else {
      samplesEl.textContent = "No suppliers in this search published a price — model estimates shown below.";
      samplesEl.style.display = "";
    }
  }

  el.style.display = "";
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
  // Score-breakdown hover: shows price/risk/bucket so the user can see *why*
  // the number is what it is. Hidden behind a title attribute for now.
  const breakdown = scoreBreakdownTip(supplier);
  return `<span class="value-score" title="${escapeHtml(breakdown)}">${supplier.value_score}/100</span> ${aiAdjBadge(supplier)}`;
}

function scoreBreakdownTip(s) {
  const parts = [];
  parts.push(`Risk: ${s.risk_level} (${(s.risk_score || 0).toFixed(2)})`);
  if (s.bucket_key) {
    const bs = s.bucket_size != null ? ` • ${s.bucket_size} samples` : "";
    parts.push(`Bucket: ${s.bucket_key}${bs}`);
  }
  if (s.angle_count) {
    parts.push(`${s.angle_count} search angle${s.angle_count > 1 ? "s" : ""} matched`);
  }
  if (s.price_unit_source && s.price_unit_source !== "regex" && s.price_unit) {
    parts.push(`Unit "${s.price_unit}" inferred from ${s.price_unit_source}`);
  }
  return parts.join(" | ");
}

// Multi-angle trust chip: more angles matched = stronger search signal.
function angleChipHTML(s) {
  const n = s.angle_count || 0;
  if (!n) return "";
  const cls = n >= 3 ? "angle-chip-high" : n === 2 ? "angle-chip-med" : "angle-chip-low";
  const angles = (s.angles_matched || []).join(", ");
  const tip = `Matched search angles: ${angles}`;
  return `<span class="angle-chip ${cls}" title="${escapeHtml(tip)}">${n}-angle</span>`;
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

const _INSIGHT_CACHE_MAX = 50;
const _INSIGHT_CACHE_TRACKING_KEY = "metalmind_insight_keys";

function _insightCacheSet(supplier, data) {
  const key = _insightCacheKey(supplier);
  try {
    localStorage.setItem(key, JSON.stringify({ data, generatedAt: new Date().toISOString() }));
  } catch (e) {
    if (e instanceof DOMException && e.name === "QuotaExceededError") {
      // Evict oldest entry and retry once
      try {
        const tracked = JSON.parse(localStorage.getItem(_INSIGHT_CACHE_TRACKING_KEY) || "[]");
        if (tracked.length) { localStorage.removeItem(tracked.shift()); localStorage.setItem(_INSIGHT_CACHE_TRACKING_KEY, JSON.stringify(tracked)); }
        localStorage.setItem(key, JSON.stringify({ data, generatedAt: new Date().toISOString() }));
      } catch (_) {}
      return;
    }
  }
  try {
    const tracked = JSON.parse(localStorage.getItem(_INSIGHT_CACHE_TRACKING_KEY) || "[]");
    const updated = [...tracked.filter(k => k !== key), key];
    if (updated.length > _INSIGHT_CACHE_MAX) { localStorage.removeItem(updated.shift()); }
    localStorage.setItem(_INSIGHT_CACHE_TRACKING_KEY, JSON.stringify(updated));
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

// ---------------------------------------------------------------------------
// Search details (replaces the former winner card)
// ---------------------------------------------------------------------------

function _capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function _humanCategory(cat) {
  if (!cat || cat === "unknown") return "Any metal";
  const map = {
    acp: "ACP (aluminium composite panel)",
    aluminum: "Aluminium",
    steel: "Carbon steel",
    stainless_steel: "Stainless steel",
    copper: "Copper",
    brass: "Brass",
    zinc: "Zinc",
    titanium: "Titanium",
    tube: "Metal tube",
    pipe: "Metal pipe",
  };
  return map[cat] || _capitalize(cat);
}

function _humanVariant(v) {
  if (!v) return "";
  return v.replace(/_/g, " ");    // "pvdf_coated" -> "pvdf coated"
}

// Called by the search-details "Edit & re-search" link.
// For now we go back to the homepage chat box — inline editing is a later
// enhancement (user explicitly chose the simpler A path).
function goEditSearch() {
  showIdle();
}

function renderSearchDetails(parsed, data) {
  const body   = document.getElementById("search-details-body");
  const footer = document.getElementById("search-details-footer");
  if (!body || !footer) return;

  // --- Body: natural-language summary of parsed query -------------------
  // Shape looks like a 4-line chat response, not a filter form.
  const cat     = _humanCategory(parsed && parsed.category);
  const variant = parsed && _humanVariant(parsed.variant);
  const material = parsed && parsed.material;

  // "Searching for: ACP · Marble finish · PVDF coated"
  const productParts = [cat];
  if (variant && variant !== "solid") productParts.push(variant + " finish");
  if (material) productParts.push("grade " + material);
  const product = productParts.join(" · ");

  const countries = parsed && parsed.countries && parsed.countries.length
    ? parsed.countries.join(", ")
    : "Any country (global search)";

  const spec = (parsed && parsed.spec) ? parsed.spec : "Not specified";

  let priceBand = "Any price";
  const pr = (parsed && parsed.price_range) || {};
  if (pr.min || pr.max) {
    const cur = pr.currency || "USD";
    const range = (pr.min && pr.max) ? `${pr.min}-${pr.max}` : (pr.min || pr.max);
    const u = pr.unit || "unit";
    priceBand = `${cur} ${range}/${u}`;
  }

  body.innerHTML = `
    <div class="sd-line"><span class="sd-label">Searching for:</span> <span class="sd-value">${escapeHtml(product)}</span></div>
    <div class="sd-line"><span class="sd-label">In:</span> <span class="sd-value">${escapeHtml(countries)}</span></div>
    <div class="sd-line"><span class="sd-label">Spec:</span> <span class="sd-value">${escapeHtml(spec)}</span></div>
    <div class="sd-line"><span class="sd-label">Price band:</span> <span class="sd-value">${escapeHtml(priceBand)}</span></div>
  `;

  // --- Footer: count · trust · top pick ---------------------------------
  const count  = data.all_suppliers.length;
  const winner = data.winner;
  const trust  = data.trust || "safe";
  const tm     = TRUST_META[trust];

  const trustHtml = tm
    ? `<span class="sd-trust sd-trust-${trust}" title="${escapeHtml(tm.sub)}">${tm.icon} ${escapeHtml(tm.label)}</span>`
    : "";

  // Respect the recommendation verdict in the top-pick line
  const decision = data.decision || "recommended";
  const isRec = decision === "recommended";
  const topPickLabel = isRec ? "Top pick" : "Closest match";
  const topPickHtml = winner
    ? `<span class="sd-toppick">${topPickLabel}: <strong>${escapeHtml(winner.name)}</strong> <span class="sd-toppick-country">(${escapeHtml(winner.country)})</span></span>`
    : "";

  const verdictBadge = isRec
    ? ""
    : `<span class="sd-verdict sd-verdict-warn">Not recommended — review alternatives</span>`;

  const partialBadge = data.partial
    ? `<span class="sd-partial" title="Partial result: AI cross-check was skipped to stay within the time budget.">⚠ Partial</span>`
    : "";

  footer.innerHTML = `
    <span class="sd-count"><strong>${count}</strong> suppliers found</span>
    <span class="sd-sep">·</span>
    ${trustHtml}
    <span class="sd-sep">·</span>
    ${topPickHtml}
    ${verdictBadge ? `<span class="sd-sep">·</span>${verdictBadge}` : ""}
    ${partialBadge ? `<span class="sd-sep">·</span>${partialBadge}` : ""}
  `;
}


// ---------------------------------------------------------------------------
// Main results renderer — search details + Top 3 + All Suppliers
// ---------------------------------------------------------------------------

function renderResults(data) {
  const { winner, top3, all_suppliers, explanation, risk_note, symbol, fx, decision, trust, partial, trace, parsed } = data;
  _lastSymbol = symbol; _lastFx = fx;

  document.getElementById("all-suppliers-count").textContent = `${all_suppliers.length}`;

  // Debug trace: surface via window for console inspection when ?debug=1.
  if (trace) {
    window._mmLastTrace = trace;
    console.info("[mm] trace:", trace);
  }

  // Search details panel — chat-thread style recap of what the user asked.
  // Also renders the count + global trust verdict + top-pick mention, plus
  // the partial-results warning (which used to sit on the winner card).
  renderSearchDetails(parsed, data);

  // Market reference banner — macro price context above All Suppliers.
  renderMarketReferenceBanner(data.market_reference, symbol);

  // Canonical unit from the market reference (used to label per-supplier
  // estimates when the supplier had no extracted price/unit of its own).
  const fallbackUnit = data.market_reference ? data.market_reference.unit : null;

  // Top 3 cards — #1 gets the 🏆 Top Pick treatment (highlighted border +
  // badge) and a "Why this pick?" collapsible that carries the explanation
  // narrative that used to live on the now-deleted winner card.
  const top3Container = document.getElementById("top3-cards");
  top3Container.innerHTML = "";
  top3.forEach((s, idx) => {
    const rankClass = idx === 0 ? "rank-1 top-pick" : idx === 1 ? "rank-2" : "";
    const isTopPick = (idx === 0);
    const decision  = data.decision || "recommended";

    const rankBadge = isTopPick
      ? `<div class="card-top-pick-badge">🏆 ${decision === "recommended" ? "TOP PICK" : "CLOSEST MATCH"}</div>`
      : `<div class="card-rank">${idx + 1}</div>`;

    const whyThisPick = isTopPick && explanation
      ? `<details class="why-this-pick">
           <summary>Why this pick?</summary>
           <div class="why-this-pick-body">${escapeHtml(explanation).replace(/\*\*/g, '')}</div>
         </details>`
      : "";

    const card = document.createElement("div");
    card.className = `supplier-card ${rankClass}`;
    card.innerHTML = `
      ${rankBadge}
      <div class="card-name">${escapeHtml(s.name)}</div>
      <div class="card-meta">
        ${countryTag(s.country)}
        ${trustBadgeHTML(s.trust)}
        ${confidenceHTML(s)}
        ${angleChipHTML(s)}
      </div>
      <div class="card-stats">
        <div><div class="card-stat-label">Est. Price</div><div class="card-stat-value">${renderPriceCell(s, symbol, fx, fallbackUnit)}</div></div>
        <div><div class="card-stat-label">Risk</div><div class="card-stat-value ${riskClass(s.risk_level)}">${s.risk_level}</div></div>
        <div><div class="card-stat-label">Value Score</div><div class="card-stat-value">${valueScoreHTML(s)}</div></div>
      </div>
      <div class="card-url"><a href="${/^https?:/i.test(s.url) ? escapeHtml(s.url) : '#'}" target="_blank" rel="noopener">${escapeHtml(s.url || '')}</a></div>
      ${anomaliesHTML(s.anomalies)}
      ${whyThisPick}
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
      <td>${escapeHtml(s.name)} ${trustBadgeHTML(s.trust)} ${angleChipHTML(s)}</td>
      <td>${countryTag(s.country)}</td>
      <td>${renderPriceCell(s, symbol, fx, fallbackUnit)}</td>
      <td class="${riskClass(s.risk_level)}">${s.risk_level}</td>
      <td>${valueScoreHTML(s)}</td>
      <td><a href="${/^https?:/i.test(s.url) ? escapeHtml(s.url) : '#'}" target="_blank" rel="noopener" style="font-size:12px" onclick="event.stopPropagation()">${escapeHtml((() => { try { return new URL(s.url).hostname; } catch(_) { return s.url || ''; } })())}</a></td>
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
// Chat-driven flow: parse query → preview → confirm → run analysis
// ---------------------------------------------------------------------------

// Holds the most-recently-parsed query while the parse-preview UI is visible.
let _pendingParsed = null;

function fillChatExample(btn) {
  const input = document.getElementById("chat-input");
  if (input && btn) {
    input.value = btn.textContent.trim();
    input.focus();
  }
}

function cancelParse() {
  _pendingParsed = null;
  hide("parse-preview");
  hide("clarification");
  show("chat-form");
  show("chat-examples-row");
}

function show(id) { const el = document.getElementById(id); if (el) el.style.display = ""; }
function hide(id) { const el = document.getElementById(id); if (el) el.style.display = "none"; }

async function runChatSearch(ev) {
  if (ev && ev.preventDefault) ev.preventDefault();
  const inp = document.getElementById("chat-input");
  const q   = (inp && inp.value || "").trim();
  if (!q) return;

  // Step 1: parse-only call. Cheap (no Serper), shows what the model heard.
  const sendBtn = document.getElementById("chat-send");
  if (sendBtn) sendBtn.disabled = true;
  try {
    const resp = await fetch("/api/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: q }),
    });
    if (!resp.ok) {
      showError("Could not parse the request. Please try again.");
      return;
    }
    const data = await resp.json();
    const parsed = data.parsed || {};
    if (parsed.needs_clarification && parsed.clarification_question) {
      _pendingParsed = parsed;
      const cEl = document.getElementById("clarification-text");
      if (cEl) cEl.textContent = parsed.clarification_question;
      hide("chat-form");
      show("clarification");
      return;
    }
    _pendingParsed = parsed;
    renderParsePreview(parsed);
    hide("chat-form");
    show("parse-preview");
  } catch (e) {
    showError("Service temporarily unavailable. Please try again.");
  } finally {
    if (sendBtn) sendBtn.disabled = false;
  }
}

function renderParsePreview(parsed) {
  const src = document.getElementById("parse-source");
  if (src) src.textContent = `parsed by ${parsed.source || "rules"}`;

  const grid = document.getElementById("parse-fields");
  if (!grid) return;

  const fields = [
    ["Category",   parsed.category || "any",  "category"],
    ["Material",   parsed.material || "—",    "material"],
    ["Variant",    parsed.variant  || "—",    "variant"],
    ["Countries",  (parsed.countries && parsed.countries.length) ? parsed.countries.join(", ") : "global", "countries"],
    ["Spec",       parsed.spec     || "—",    "spec"],
  ];
  const pr = parsed.price_range || {};
  if (pr.min || pr.max || pr.currency || pr.unit) {
    const cur = pr.currency || "USD";
    const amt = pr.min && pr.max ? `${pr.min}-${pr.max}` : (pr.min || pr.max || "?");
    const u   = pr.unit || "unit";
    fields.push(["Price band", `${cur} ${amt}/${u}`, "price"]);
  }
  grid.innerHTML = fields.map(([label, val, key]) =>
    `<div class="parse-field">
       <span class="parse-field-label">${escapeHtml(label)}</span>
       <span class="parse-field-val" data-key="${key}" contenteditable="true"
             onblur="updateParseField('${key}', this.textContent)">${escapeHtml(val)}</span>
     </div>`
  ).join("");
}

function updateParseField(key, value) {
  if (!_pendingParsed) return;
  const v = String(value || "").trim();
  if (key === "category") {
    _pendingParsed.category = v.toLowerCase();
  } else if (key === "material" || key === "variant" || key === "spec") {
    _pendingParsed[key] = v;
  } else if (key === "countries") {
    _pendingParsed.countries = v && v !== "global"
      ? v.split(",").map(s => s.trim()).filter(Boolean)
      : [];
  } else if (key === "price") {
    // Best-effort parse of "USD 800-900/ton"
    const m = v.match(/([A-Z]{3})\s*(\d+(?:\.\d+)?)(?:[-–to](\d+(?:\.\d+)?))?\s*\/?\s*(\w+)?/i);
    if (m) {
      _pendingParsed.price_range = {
        currency: (m[1] || "USD").toUpperCase(),
        min:      parseFloat(m[2]),
        max:      m[3] ? parseFloat(m[3]) : null,
        unit:     (m[4] || "").toLowerCase() || null,
      };
    }
  }
}

let _confirmSearchInFlight = false;

async function confirmParseAndSearch() {
  if (!_pendingParsed || _confirmSearchInFlight) return;
  _confirmSearchInFlight = true;

  const searchBtn = document.getElementById("confirm-search-btn");
  if (searchBtn) searchBtn.disabled = true;

  hide("parse-preview");
  hide("clarification");
  hide("chat-form");
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
      body: JSON.stringify({
        max_results: 10,
        parsed: _pendingParsed,
        debug:  isDebugMode(),
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      apiError = err.detail || `Server error (${resp.status})`;
    } else {
      apiResult = await resp.json();
    }
  } catch (_) {
    apiError = "Service temporarily unavailable. Please try again.";
  } finally {
    _confirmSearchInFlight = false;
    if (searchBtn) searchBtn.disabled = false;
  }
  apiDone = true;
  if (animDone) finalize(apiResult, apiError);
}

function isDebugMode() {
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.get("debug") === "1") return true;
  } catch (_) {}
  return !!window._mmDebug;
}


// ---------------------------------------------------------------------------
// Main: single unified analysis (legacy ACP button, kept for back-compat)
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
    apiError = "Service temporarily unavailable. Please try again.";
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

  // Cache the result so Back-to-Analysis-Result restores it without re-running.
  // Use localStorage so cache survives browser close/reopen (saves API cost).
  // On QuotaExceededError, evict the stale analysis and retry once.
  const _cachePayload = JSON.stringify({ version: 1, data: data });
  try {
    localStorage.setItem("metalmind_last_analysis", _cachePayload);
  } catch (e) {
    if (e instanceof DOMException && e.name === "QuotaExceededError") {
      try { localStorage.removeItem("metalmind_last_analysis"); localStorage.setItem("metalmind_last_analysis", _cachePayload); } catch (_) {}
    }
  }

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
