/**
 * supplier-assessment.js
 *
 * Auto-save form for the user's primary decision data on a supplier.
 * URL pattern: /supplier/{id}/edit
 */

const SUPPLIER_ID = (() => {
  const m = window.location.pathname.match(/\/supplier\/(\d+)\/edit/);
  return m ? parseInt(m[1], 10) : null;
})();

const status = document.getElementById("assess-save-status");

function setStatus(state, text) {
  status.dataset.state = state;
  status.textContent = text;
}

// --- Chip (tag) helpers --------------------------------------------------

function renderChips(container, values, onChange) {
  container.innerHTML = "";
  (values || []).forEach((val, idx) => {
    const chip = document.createElement("span");
    chip.className = "assess-chip";
    chip.innerHTML = `${val} <button type="button" class="assess-chip-x" aria-label="Remove">×</button>`;
    chip.querySelector(".assess-chip-x").addEventListener("click", () => {
      const next = [...values];
      next.splice(idx, 1);
      onChange(next);
    });
    container.appendChild(chip);
  });
}

function wireChipInput(inputId, containerId, fieldName, getValues, setValues) {
  const input = document.getElementById(inputId);
  const container = document.getElementById(containerId);

  const rerender = () => renderChips(container, getValues(), (next) => {
    setValues(next);
    rerender();
    saveField(fieldName, next);
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && input.value.trim()) {
      e.preventDefault();
      const val = input.value.trim();
      const current = getValues();
      if (!current.includes(val)) {
        const next = [...current, val];
        setValues(next);
        rerender();
        saveField(fieldName, next);
      }
      input.value = "";
    }
  });

  return rerender;
}

// --- Star rating --------------------------------------------------------

function renderStars(container, value, onChange) {
  const max = parseInt(container.dataset.max || "5", 10);
  container.innerHTML = "";
  for (let i = 1; i <= max; i++) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "assess-star" + (i <= (value || 0) ? " is-filled" : "");
    btn.textContent = "★";
    btn.addEventListener("click", () => {
      // click same star twice = clear
      const next = value === i ? null : i;
      onChange(next);
    });
    container.appendChild(btn);
  }
  // "Clear" button
  if (value) {
    const clr = document.createElement("button");
    clr.type = "button";
    clr.className = "assess-star-clear";
    clr.textContent = "Clear";
    clr.addEventListener("click", () => onChange(null));
    container.appendChild(clr);
  }
}

// --- Auto-save ----------------------------------------------------------

let saveTimer = null;
let pendingPayload = {};

function saveField(field, value) {
  pendingPayload[field] = value;
  setStatus("saving", "Saving…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, 400);
}

async function flushSave() {
  if (Object.keys(pendingPayload).length === 0) return;
  const body = pendingPayload;
  pendingPayload = {};
  try {
    const resp = await fetch(`/api/saved-supplier/${SUPPLIER_ID}/assessment`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    setStatus("saved", "Saved ✓");
  } catch (e) {
    setStatus("error", "Save failed — retry");
    // Roll pending back so a later change flushes everything
    pendingPayload = { ...body, ...pendingPayload };
  }
}

// --- Wire inputs --------------------------------------------------------

function wireSimpleInput(id, field, transform = (v) => v) {
  const el = document.getElementById(id);
  el.addEventListener("change", () => {
    let v = el.value;
    if (v === "") v = null;
    else v = transform(v);
    saveField(field, v);
  });
}

function wireCheckGroup(containerId, field) {
  const container = document.getElementById(containerId);
  const boxes = container.querySelectorAll('input[type="checkbox"]');
  const emit = () => {
    const values = [...boxes].filter(b => b.checked).map(b => b.value);
    saveField(field, values);
  };
  boxes.forEach(b => b.addEventListener("change", emit));
}

// --- Load existing data -------------------------------------------------

async function loadSupplier() {
  if (!SUPPLIER_ID) {
    setStatus("error", "Invalid URL");
    return;
  }
  const resp = await fetch(`/api/saved-suppliers`);
  if (!resp.ok) {
    setStatus("error", "Failed to load");
    return;
  }
  const all = await resp.json();
  const s = all.find(x => x.id === SUPPLIER_ID);
  if (!s) {
    setStatus("error", "Supplier not found");
    return;
  }

  // Header
  document.getElementById("assess-supplier-name").textContent = s.supplier_name;
  const countryEl = document.getElementById("assess-country");
  if (s.country) countryEl.textContent = s.country;
  const urlEl = document.getElementById("assess-url");
  if (s.url) {
    const a = document.createElement("a");
    a.href = s.url; a.target = "_blank"; a.rel = "noopener";
    a.textContent = s.url;
    urlEl.innerHTML = "";
    urlEl.appendChild(a);
  }
  document.title = `${s.supplier_name} — Assessment`;

  // Tier 1
  document.getElementById("decision_stage").value = s.decision_stage || "";

  let ratingValue = s.rating;
  const ratingEl = document.getElementById("rating-stars");
  const redrawRating = () => renderStars(ratingEl, ratingValue, (next) => {
    ratingValue = next;
    redrawRating();
    saveField("rating", next);
  });
  redrawRating();

  let tagsValue = Array.isArray(s.tags) ? [...s.tags] : [];
  const tagsRender = wireChipInput("tags-input", "tags-chips", "tags",
    () => tagsValue, (v) => { tagsValue = v; });
  tagsRender();

  // Tier 2
  document.getElementById("quoted_price").value = s.quoted_price ?? "";
  document.getElementById("quoted_currency").value = s.quoted_currency || "";
  document.getElementById("quoted_unit").value = s.quoted_unit || "";
  document.getElementById("moq").value = s.moq ?? "";
  document.getElementById("lead_time_days").value = s.lead_time_days ?? "";
  document.getElementById("payment_terms").value = s.payment_terms || "";
  document.getElementById("incoterms").value = s.incoterms || "";

  // Tier 3
  document.getElementById("sample_status").value = s.sample_status || "";

  let sampleQualValue = s.sample_quality;
  const sqEl = document.getElementById("sample-quality-stars");
  const redrawSQ = () => renderStars(sqEl, sampleQualValue, (next) => {
    sampleQualValue = next;
    redrawSQ();
    saveField("sample_quality", next);
  });
  redrawSQ();

  const fvs = Array.isArray(s.factory_verified_via) ? s.factory_verified_via : [];
  document.querySelectorAll('#factory-verified input[type="checkbox"]').forEach(b => {
    b.checked = fvs.includes(b.value);
  });

  document.getElementById("coating_confirmed").value = s.coating_confirmed || "";
  document.getElementById("core_material_confirmed").value = s.core_material_confirmed || "";
  document.getElementById("fire_rating_confirmed").value = s.fire_rating_confirmed || "";
  document.getElementById("warranty_years").value = s.warranty_years ?? "";
  document.getElementById("next_action_date").value = s.next_action_date || "";

  // Pros / Cons
  let prosValue = Array.isArray(s.pros) ? [...s.pros] : [];
  const prosRender = wireChipInput("pros-input", "pros-chips", "pros",
    () => prosValue, (v) => { prosValue = v; });
  prosRender();

  let consValue = Array.isArray(s.cons) ? [...s.cons] : [];
  const consRender = wireChipInput("cons-input", "cons-chips", "cons",
    () => consValue, (v) => { consValue = v; });
  consRender();

  // Notes
  document.getElementById("notes").value = s.notes || "";

  setStatus("saved", "Saved ✓");
}

// Wire all the simple change handlers after DOM is ready.
document.addEventListener("DOMContentLoaded", () => {
  wireSimpleInput("decision_stage", "decision_stage");
  wireSimpleInput("quoted_price", "quoted_price", (v) => parseFloat(v));
  wireSimpleInput("quoted_currency", "quoted_currency");
  wireSimpleInput("quoted_unit", "quoted_unit");
  wireSimpleInput("moq", "moq", (v) => parseInt(v, 10));
  wireSimpleInput("lead_time_days", "lead_time_days", (v) => parseInt(v, 10));
  wireSimpleInput("payment_terms", "payment_terms");
  wireSimpleInput("incoterms", "incoterms");
  wireSimpleInput("sample_status", "sample_status");
  wireCheckGroup("factory-verified", "factory_verified_via");
  wireSimpleInput("coating_confirmed", "coating_confirmed");
  wireSimpleInput("core_material_confirmed", "core_material_confirmed");
  wireSimpleInput("fire_rating_confirmed", "fire_rating_confirmed");
  wireSimpleInput("warranty_years", "warranty_years", (v) => parseInt(v, 10));
  wireSimpleInput("next_action_date", "next_action_date");

  // Notes: debounce on input so we don't save per keystroke
  const notesEl = document.getElementById("notes");
  let notesTimer = null;
  notesEl.addEventListener("input", () => {
    clearTimeout(notesTimer);
    notesTimer = setTimeout(() => saveField("notes", notesEl.value), 600);
  });

  wireAttachments();
  loadSupplier();
  loadAttachments();
});


// ---------------------------------------------------------------------------
// Attachments — upload, list, delete files for this supplier
// ---------------------------------------------------------------------------

function _fmtBytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function _attachmentIcon(mime, filename) {
  const name = (filename || "").toLowerCase();
  const m = (mime || "").toLowerCase();
  if (m.startsWith("image/"))                     return "🖼️";
  if (m === "application/pdf" || name.endsWith(".pdf")) return "📄";
  if (m.includes("spreadsheet") || /\.(xlsx?|csv)$/.test(name)) return "📊";
  if (m.includes("word") || /\.(docx?)$/.test(name)) return "📝";
  if (m.startsWith("video/"))                     return "🎬";
  if (m.startsWith("audio/"))                     return "🎵";
  if (/\.(zip|rar|7z|tar|gz)$/.test(name))        return "🗜️";
  return "📎";
}

async function loadAttachments() {
  if (SUPPLIER_ID == null) return;
  try {
    const resp = await fetch(`/api/suppliers/${SUPPLIER_ID}/attachments`);
    if (!resp.ok) return;
    const items = await resp.json();
    renderAttachments(items);
  } catch (e) {
    console.error("Failed to load attachments:", e);
  }
}

function renderAttachments(items) {
  const list = document.getElementById("attachments-list");
  if (!list) return;
  list.innerHTML = "";

  if (!items || items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "attachments-empty";
    empty.textContent = "No attachments yet.";
    list.appendChild(empty);
    return;
  }

  items.forEach(att => {
    const li = document.createElement("li");
    li.className = "attachment-item";

    const downloadUrl = `/api/suppliers/${SUPPLIER_ID}/attachments/${att.id}`;
    const filename = att.filename || "file";
    const icon = _attachmentIcon(att.mime_type, filename);

    li.innerHTML = `
      <span class="attachment-icon">${icon}</span>
      <a class="attachment-name" href="${downloadUrl}" target="_blank" rel="noopener">${_escapeHtml(filename)}</a>
      <span class="attachment-size">${_fmtBytes(att.size_bytes)}</span>
      <button type="button" class="attachment-delete" aria-label="Delete">×</button>
    `;

    li.querySelector(".attachment-delete").addEventListener("click", async () => {
      if (!confirm(`Delete "${filename}"?`)) return;
      try {
        const resp = await fetch(downloadUrl, { method: "DELETE" });
        if (!resp.ok) throw new Error(`Delete failed: ${resp.status}`);
        await loadAttachments();
      } catch (e) {
        alert("Could not delete: " + e.message);
      }
    });

    list.appendChild(li);
  });
}

function _escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function wireAttachments() {
  const btn      = document.getElementById("add-attachment-btn");
  const input    = document.getElementById("attachment-file-input");
  const statusEl = document.getElementById("attachments-status");
  if (!btn || !input) return;

  btn.addEventListener("click", () => input.click());

  input.addEventListener("change", async () => {
    const files = Array.from(input.files || []);
    if (!files.length || SUPPLIER_ID == null) return;

    btn.disabled = true;
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      statusEl.textContent = `Uploading ${i + 1}/${files.length}: ${f.name}`;
      try {
        const form = new FormData();
        form.append("file", f);
        const resp = await fetch(`/api/suppliers/${SUPPLIER_ID}/attachments`, {
          method: "POST",
          body:   form,
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || `Upload failed (${resp.status})`);
        }
      } catch (e) {
        alert(`Could not upload ${f.name}: ${e.message}`);
      }
    }
    statusEl.textContent = `Uploaded ${files.length} file${files.length === 1 ? "" : "s"}`;
    setTimeout(() => { statusEl.textContent = ""; }, 2500);

    input.value = "";       // reset so selecting the same file again re-triggers change
    btn.disabled = false;
    await loadAttachments();
  });
}
