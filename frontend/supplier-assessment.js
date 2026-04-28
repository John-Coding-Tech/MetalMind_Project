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
    a.href = /^https?:/i.test(s.url) ? s.url : "#";
    a.target = "_blank"; a.rel = "noopener";
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

  document.getElementById("reference_1").value = s.reference_1 || "";
  document.getElementById("reference_2").value = s.reference_2 || "";
  document.getElementById("reference_3").value = s.reference_3 || "";
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
  wireSimpleInput("reference_1", "reference_1");
  wireSimpleInput("reference_2", "reference_2");
  wireSimpleInput("reference_3", "reference_3");
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
  wireEmailLog();
  loadSupplier();
  loadAttachments();
  loadEmails();
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

  const MAX_UPLOAD_MB = 25;
  const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

  input.addEventListener("change", async () => {
    const files = Array.from(input.files || []);
    if (!files.length || SUPPLIER_ID == null) return;

    btn.disabled = true;
    let uploadedCount = 0;
    for (let i = 0; i < files.length; i++) {
      const f = files[i];
      if (f.size > MAX_UPLOAD_BYTES) {
        statusEl.textContent = `Skipped ${f.name}: exceeds ${MAX_UPLOAD_MB} MB limit.`;
        continue;
      }
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
          statusEl.textContent = `Could not upload ${f.name}: ${err.detail || `error ${resp.status}`}`;
          continue;
        }
        uploadedCount++;
      } catch (e) {
        statusEl.textContent = `Could not upload ${f.name}: ${e.message}`;
        continue;
      }
    }
    if (uploadedCount > 0) {
      statusEl.textContent = `Uploaded ${uploadedCount} file${uploadedCount === 1 ? "" : "s"}`;
    }
    setTimeout(() => { statusEl.textContent = ""; }, 2500);

    input.value = "";       // reset so selecting the same file again re-triggers change
    btn.disabled = false;
    await loadAttachments();
  });
}


// ---------------------------------------------------------------------------
// Email Log — RFQ outbound + supplier reply tracking
// Mirrors the loadX → renderX → wireX pattern used by Attachments.
// ---------------------------------------------------------------------------

// Held in module scope so optimistic-update handlers can mutate without
// re-fetching from the server every keystroke. Refreshed by loadEmails().
let _emailRows = [];
let _composeDraft = null;   // last AI-generated FORMAT D payload, NOT yet saved

function _fmtEmailTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  // Local time, short: "2026-04-28 20:30"
  const yyyy = d.getFullYear();
  const mm   = String(d.getMonth() + 1).padStart(2, "0");
  const dd   = String(d.getDate()).padStart(2, "0");
  const hh   = String(d.getHours()).padStart(2, "0");
  const mi   = String(d.getMinutes()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}`;
}

async function loadEmails() {
  try {
    const resp = await fetch(`/api/suppliers/${SUPPLIER_ID}/emails`);
    if (!resp.ok) throw new Error(`status=${resp.status}`);
    _emailRows = await resp.json();
    renderEmails(_emailRows);
  } catch (e) {
    console.error("Failed to load emails:", e);
    const list = document.getElementById("email-log-list");
    if (list) list.innerHTML = `<li class="email-log-empty">Failed to load emails.</li>`;
  }
}

function renderEmails(items) {
  const list = document.getElementById("email-log-list");
  if (!list) return;
  list.innerHTML = "";

  if (!items || items.length === 0) {
    const empty = document.createElement("li");
    empty.className = "email-log-empty";
    empty.textContent = "No emails yet. Click Compose new email or Log inbound reply to start.";
    list.appendChild(empty);
    return;
  }

  for (const e of items) {
    list.appendChild(_buildEmailCard(e));
  }
}

function _buildEmailCard(e) {
  const li = document.createElement("li");
  li.className = `email-card email-card--${e.state}`;
  li.dataset.emailId = e.id;

  // Time displayed depends on state
  let timeStr = "";
  if (e.state === "draft")   timeStr = `Draft · created ${_fmtEmailTime(e.created_at)}`;
  if (e.state === "sent")    timeStr = `Sent ${_fmtEmailTime(e.sent_at)}`;
  if (e.state === "inbound") timeStr = `Received ${_fmtEmailTime(e.received_at)}`;
  const aiTag = e.ai_generated ? " · AI-drafted" : "";

  // State badge text
  const badgeLabel = e.state.toUpperCase();

  // Action buttons — only Mark sent for drafts; everything has Delete
  let actionsHtml = "";
  if (e.state === "draft") {
    actionsHtml += `<button type="button" class="email-mark-sent-btn">Mark sent</button>`;
  }
  actionsHtml += `<button type="button" class="email-delete-btn" aria-label="Delete">×</button>`;

  li.innerHTML = `
    <div class="email-card-row">
      <div class="email-card-header">
        <div class="email-card-title">${_escapeHtml(e.subject || "(no subject)")}</div>
        <div class="email-card-meta">
          <span class="email-state-badge email-state-badge--${e.state}">${badgeLabel}</span>
          <span>${_escapeHtml(timeStr)}${aiTag}</span>
        </div>
      </div>
      <div class="email-card-actions">${actionsHtml}</div>
    </div>
    <div class="email-card-body" hidden>${_escapeHtml(e.body || "")}</div>
  `;

  // Click on header (only) toggles body
  const header = li.querySelector(".email-card-header");
  const body   = li.querySelector(".email-card-body");
  header.addEventListener("click", () => {
    body.hidden = !body.hidden;
  });

  // Mark sent (optimistic — UI updates immediately, then PATCH; revert on failure)
  const markBtn = li.querySelector(".email-mark-sent-btn");
  if (markBtn) {
    markBtn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const sentAt = new Date().toISOString();
      // Optimistic: rewrite the local row + re-render this card
      const idx = _emailRows.findIndex(r => r.id === e.id);
      if (idx >= 0) {
        const updated = { ..._emailRows[idx], state: "sent", sent_at: sentAt };
        _emailRows[idx] = updated;
        const newCard = _buildEmailCard(updated);
        li.replaceWith(newCard);
      }
      try {
        const resp = await fetch(`/api/supplier-emails/${e.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sent_at: sentAt }),
        });
        if (!resp.ok) throw new Error(`status=${resp.status}`);
        // Sync from server (catch any clock drift / format normalisation)
        await loadEmails();
      } catch (err) {
        console.error("Mark sent failed:", err);
        alert("Failed to mark email as sent. Reloading list.");
        await loadEmails();
      }
    });
  }

  // Delete (with confirm — destructive)
  const delBtn = li.querySelector(".email-delete-btn");
  if (delBtn) {
    delBtn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      if (!confirm(`Delete email "${e.subject}"? This cannot be undone.`)) return;
      try {
        const resp = await fetch(`/api/supplier-emails/${e.id}`, { method: "DELETE" });
        if (!resp.ok && resp.status !== 204) throw new Error(`status=${resp.status}`);
        await loadEmails();
      } catch (err) {
        console.error("Delete failed:", err);
        alert("Failed to delete email.");
      }
    });
  }

  return li;
}

// ---------------------------------------------------------------------------
// Compose modal — AI draft via /api/ai-search EMAIL_DRAFT intent
// ---------------------------------------------------------------------------

function _openComposeModal() {
  document.getElementById("compose-prompt").value = "";
  document.getElementById("compose-status").textContent = "";
  _resetComposeOutput();
  _composeDraft = null;
  document.getElementById("compose-email-modal").hidden = false;
  document.body.classList.add("modal-open");
}

function _closeComposeModal() {
  document.getElementById("compose-email-modal").hidden = true;
  document.body.classList.remove("modal-open");
}

function _resetComposeOutput() {
  const out = document.getElementById("compose-draft-card");
  out.className = "compose-draft-empty";
  out.innerHTML = "AI-drafted email will appear here.";
}

function _renderComposeDraft(draft) {
  const out = document.getElementById("compose-draft-card");
  out.className = "compose-draft-card";

  const subjectEl = document.createElement("div");
  subjectEl.className = "compose-draft-subject";
  subjectEl.textContent = draft.subject || "(no subject)";

  const bodyEl = document.createElement("div");
  bodyEl.className = "compose-draft-body";
  bodyEl.textContent = draft.body || "";

  out.innerHTML = "";
  out.appendChild(subjectEl);
  out.appendChild(bodyEl);

  if (Array.isArray(draft.highlights) && draft.highlights.length > 0) {
    const ul = document.createElement("ul");
    ul.className = "compose-draft-highlights";
    for (const h of draft.highlights) {
      const li = document.createElement("li");
      li.textContent = h;
      ul.appendChild(li);
    }
    out.appendChild(ul);
  }

  // 3 action buttons
  const buttonsRow = document.createElement("div");
  buttonsRow.className = "compose-draft-buttons";
  buttonsRow.innerHTML = `
    <button type="button" class="compose-mailto-btn">Open in Email</button>
    <button type="button" class="compose-copy-btn">Copy</button>
    <button type="button" class="compose-save-btn">Save to Log</button>
  `;
  out.appendChild(buttonsRow);

  buttonsRow.querySelector(".compose-mailto-btn").addEventListener("click", () => {
    const subject = encodeURIComponent(draft.subject || "");
    const body    = encodeURIComponent(draft.body || "");
    // No supplier email address yet — user pastes To:
    window.location.href = `mailto:?subject=${subject}&body=${body}`;
  });

  buttonsRow.querySelector(".compose-copy-btn").addEventListener("click", async () => {
    const text = `Subject: ${draft.subject || ""}\n\n${draft.body || ""}`;
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById("compose-status").textContent = "Copied to clipboard.";
    } catch (err) {
      console.error("Clipboard write failed:", err);
      // Fallback: select text in a hidden textarea + execCommand
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch (_) { /* nothing more we can do */ }
      ta.remove();
      document.getElementById("compose-status").textContent = "Copied (fallback).";
    }
  });

  buttonsRow.querySelector(".compose-save-btn").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    document.getElementById("compose-status").textContent = "Saving…";
    try {
      const resp = await fetch("/api/supplier-emails", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          supplier_id: Number(SUPPLIER_ID),
          subject: draft.subject || "",
          body:    draft.body || "",
          direction: "outbound",
          ai_generated: true,
          // sent_at intentionally omitted — saved as draft until user marks sent
        }),
      });
      if (!resp.ok) {
        const errText = await resp.text();
        throw new Error(`status=${resp.status} ${errText}`);
      }
      _closeComposeModal();
      await loadEmails();
    } catch (err) {
      console.error("Save draft failed:", err);
      btn.disabled = false;
      document.getElementById("compose-status").textContent = "Save failed — see console.";
    }
  });
}

async function _generateDraft() {
  const promptText = document.getElementById("compose-prompt").value.trim();
  if (!promptText) {
    document.getElementById("compose-status").textContent = "Type what kind of email you need.";
    return;
  }
  const btn = document.getElementById("compose-generate-btn");
  btn.disabled = true;
  document.getElementById("compose-status").textContent = "Drafting…";

  // Detect the user's input language so the wrapper trigger phrase
  // matches it. Otherwise mixing Chinese + English in the prefix
  // pollutes the backend's language-lock detection (which counts Chinese
  // chars in the whole query) and makes English prompts produce Chinese
  // emails (and vice versa). Both wrapper phrases ("起草邮件" / "draft
  // email") are EMAIL_DRAFT trigger keywords, so intent classification
  // still works either way.
  const cnChars = (promptText.match(/[一-鿿]/g) || []).length;
  const isUserChinese = cnChars >= 2;
  const triggerPhrase = isUserChinese
    ? (_composeDraft ? "继续起草邮件" : "起草邮件")
    : (_composeDraft ? "refine email draft" : "draft email");
  const wrappedQuery = `${triggerPhrase}: ${promptText}`;

  try {
    const resp = await fetch("/api/ai-search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: wrappedQuery,
        selected_ids: [Number(SUPPLIER_ID)],
        // History carries the last draft as context for the LLM.
        history: _composeDraft ? [
          { role: "user",      content: wrappedQuery },
          { role: "assistant", content: JSON.stringify(_composeDraft) },
        ] : [],
      }),
    });
    if (!resp.ok) throw new Error(`status=${resp.status}`);
    const data = await resp.json();
    const struct = data.structured || data.raw_structured;
    if (!struct || struct.type !== "email_draft") {
      throw new Error(`expected email_draft, got ${struct && struct.type}`);
    }
    _composeDraft = {
      subject:    struct.subject    || "",
      body:       struct.body       || "",
      highlights: struct.highlights || [],
    };
    _renderComposeDraft(_composeDraft);
    document.getElementById("compose-status").textContent = "";
  } catch (err) {
    console.error("Draft generation failed:", err);
    document.getElementById("compose-status").textContent = "Draft failed — see console.";
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Inbound modal — paste a supplier reply with editable timestamp
// ---------------------------------------------------------------------------

function _openInboundModal() {
  document.getElementById("inbound-subject").value = "";
  document.getElementById("inbound-body").value = "";
  // datetime-local needs YYYY-MM-DDTHH:MM in the user's local zone
  const now = new Date();
  const tz = now.getTimezoneOffset() * 60000;
  const localIso = new Date(now.getTime() - tz).toISOString().slice(0, 16);
  document.getElementById("inbound-received-at").value = localIso;
  document.getElementById("inbound-status").textContent = "";
  document.getElementById("inbound-modal").hidden = false;
  document.body.classList.add("modal-open");
}

function _closeInboundModal() {
  document.getElementById("inbound-modal").hidden = true;
  document.body.classList.remove("modal-open");
}

async function _saveInbound() {
  const subject     = document.getElementById("inbound-subject").value.trim();
  const body        = document.getElementById("inbound-body").value.trim();
  const receivedRaw = document.getElementById("inbound-received-at").value;
  const statusEl    = document.getElementById("inbound-status");

  if (!subject || !body || !receivedRaw) {
    statusEl.textContent = "Subject, body, and received_at are all required.";
    return;
  }

  const btn = document.getElementById("inbound-save-btn");
  btn.disabled = true;
  statusEl.textContent = "Saving…";

  try {
    const receivedIso = new Date(receivedRaw).toISOString();
    const resp = await fetch("/api/supplier-emails", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        supplier_id: Number(SUPPLIER_ID),
        subject, body,
        direction: "inbound",
        received_at: receivedIso,
      }),
    });
    if (!resp.ok) {
      const errText = await resp.text();
      throw new Error(`status=${resp.status} ${errText}`);
    }
    _closeInboundModal();
    await loadEmails();
  } catch (err) {
    console.error("Inbound save failed:", err);
    statusEl.textContent = "Save failed — see console.";
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Wire-up entry point — called once on DOMContentLoaded
// ---------------------------------------------------------------------------

function wireEmailLog() {
  const composeOpenBtn = document.getElementById("compose-email-btn");
  const inboundOpenBtn = document.getElementById("log-inbound-btn");
  if (composeOpenBtn) composeOpenBtn.addEventListener("click", _openComposeModal);
  if (inboundOpenBtn) inboundOpenBtn.addEventListener("click", _openInboundModal);

  // Compose modal close + generate
  const composeOverlay  = document.getElementById("compose-email-modal");
  const composeCloseBtn = document.getElementById("compose-modal-close");
  if (composeCloseBtn) composeCloseBtn.addEventListener("click", _closeComposeModal);
  if (composeOverlay) {
    composeOverlay.addEventListener("click", (e) => {
      if (e.target === composeOverlay) _closeComposeModal();
    });
  }
  const generateBtn = document.getElementById("compose-generate-btn");
  if (generateBtn) generateBtn.addEventListener("click", _generateDraft);

  // Inbound modal close + save
  const inboundOverlay  = document.getElementById("inbound-modal");
  const inboundCloseBtn = document.getElementById("inbound-modal-close");
  if (inboundCloseBtn) inboundCloseBtn.addEventListener("click", _closeInboundModal);
  if (inboundOverlay) {
    inboundOverlay.addEventListener("click", (e) => {
      if (e.target === inboundOverlay) _closeInboundModal();
    });
  }
  const inboundSaveBtn = document.getElementById("inbound-save-btn");
  if (inboundSaveBtn) inboundSaveBtn.addEventListener("click", _saveInbound);

  // Escape closes any open modal
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!document.getElementById("compose-email-modal").hidden) _closeComposeModal();
    if (!document.getElementById("inbound-modal").hidden)        _closeInboundModal();
  });
}
