/**
 * frontend/app.js — MetalMind UI logic
 *
 * Calls POST /api/compare and renders the results.
 */

// ---------------------------------------------------------------------------
// State helpers
// ---------------------------------------------------------------------------

function showIdle() {
  document.getElementById("idle-state").style.display    = "flex";
  document.getElementById("loading-state").style.display = "none";
  document.getElementById("error-state").style.display   = "none";
  document.getElementById("results-state").style.display = "none";
}

function showLoading() {
  document.getElementById("idle-state").style.display    = "none";
  document.getElementById("loading-state").style.display = "flex";
  document.getElementById("error-state").style.display   = "none";
  document.getElementById("results-state").style.display = "none";
}

function showError(msg) {
  document.getElementById("idle-state").style.display    = "none";
  document.getElementById("loading-state").style.display = "none";
  document.getElementById("error-state").style.display   = "flex";
  document.getElementById("results-state").style.display = "none";
  document.getElementById("error-msg").textContent = msg;
}

function showResults() {
  document.getElementById("idle-state").style.display    = "none";
  document.getElementById("loading-state").style.display = "none";
  document.getElementById("error-state").style.display   = "none";
  document.getElementById("results-state").style.display = "block";
}

// ---------------------------------------------------------------------------
// Currency toggle
// ---------------------------------------------------------------------------

function toggleFxRow() {
  const currency = document.getElementById("currency").value;
  document.getElementById("fx-row").style.display = currency === "AUD" ? "block" : "none";
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

function renderResults(data) {
  const { winner, top3, all_suppliers, explanation, risk_note, symbol, fx } = data;

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
  riskEl.className    = `metric-value ${riskClass(winner.risk_level)}`;

  // Top 3 cards
  const top3Container = document.getElementById("top3-cards");
  top3Container.innerHTML = "";
  top3.forEach((s, idx) => {
    const rankClass = idx === 0 ? "rank-1" : idx === 1 ? "rank-2" : "";
    const rankLabel = ["1st", "2nd", "3rd"][idx];
    const card = document.createElement("div");
    card.className = `supplier-card ${rankClass}`;
    card.innerHTML = `
      <div class="card-rank">${rankLabel}</div>
      <div class="card-name">${s.name}</div>
      <div class="card-meta">
        ${countryTag(s.country)}
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
    `;
    top3Container.appendChild(card);
  });

  // All suppliers table
  const tbody = document.getElementById("all-tbody");
  tbody.innerHTML = "";
  all_suppliers.forEach((s, idx) => {
    const rankBadgeClass = idx === 0 ? "r1" : idx === 1 ? "r2" : "";
    const rowClass = idx === 0 ? "winner-row" : "";
    const tr = document.createElement("tr");
    tr.className = rowClass;
    tr.innerHTML = `
      <td><span class="rank-badge ${rankBadgeClass}">${s.rank}</span></td>
      <td>${s.name}</td>
      <td>${countryTag(s.country)}</td>
      <td>${s.price_usd ? formatPrice(s.price_usd, symbol, fx) : "—"}</td>
      <td class="${riskClass(s.risk_level)}">${s.risk_level}</td>
      <td>${s.value_score}/100</td>
      <td><a href="${s.url}" target="_blank" rel="noopener" style="font-size:12px">${new URL(s.url).hostname}</a></td>
    `;
    tbody.appendChild(tr);
  });
}

// ---------------------------------------------------------------------------
// Main: run comparison
// ---------------------------------------------------------------------------

async function runComparison() {
  const maxResults = parseInt(document.getElementById("max-results").value);
  const priority   = document.getElementById("priority").value;
  const currency   = document.getElementById("currency").value;
  const usdToAud   = parseFloat(document.getElementById("usd-to-aud").value) || 1.58;

  showLoading();
  document.getElementById("run-btn").disabled = true;

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
        currency,
        usd_to_aud: usdToAud,
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
  document.getElementById("run-btn").disabled = false;

  if (error || !data) {
    showError(error || "No data returned.");
    return;
  }

  renderResults(data);
  showResults();
}
