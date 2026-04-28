# Email Log Feature — Design Lock

**Date locked:** 2026-04-28
**Status:** scope-locked, not yet implemented
**Estimated work:** ~6–8 hours total, split across two phases

---

## 🎯 Goal

Add an **Email Log** module to each supplier to support RFQ workflows
(AI draft → user sends manually → user logs replies). Replaces the
desire for an automated SMTP-based RFQ system that was previously
built and deleted for being too complex.

---

## 🧱 Data Model (final)

New table `supplier_emails`:

```python
class SupplierEmail(Base):
    __tablename__ = "supplier_emails"

    id            = Column(Integer, primary_key=True, index=True)
    supplier_id   = Column(Integer, ForeignKey("saved_suppliers.id",
                                               ondelete="CASCADE"),
                           nullable=False, index=True)

    subject       = Column(String(500), nullable=False)
    body          = Column(Text,        nullable=False)

    direction     = Column(String(20),  nullable=False)  # "outbound" | "inbound"

    # processing time (auto, immutable)
    created_at    = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # event time — when the email actually went out / came in
    sent_at       = Column(DateTime, nullable=True)   # outbound only
    received_at   = Column(DateTime, nullable=True)   # inbound only, USER-EDITABLE

    thread_id     = Column(String(50), nullable=True, index=True)  # v2

    ai_generated  = Column(Boolean, default=False)
```

**Design principle: Event Time ≠ Processing Time.**

- `created_at` = processing time. When the row hit the DB. Auto, immutable.
- `sent_at` / `received_at` = event time. When the email was *actually*
  sent or received in the real world. User-editable for inbound (so
  pasting a 2-day-old reply doesn't pollute supplier-responsiveness
  metrics with our paste delay).

Adding this distinction now (not later) protects future analytics
(supplier responsiveness, lead-time accuracy, follow-up reminders).

**Field integrity rules (enforce in API layer, not just DB):**

- outbound rows: `sent_at` may be NULL (= draft) or a datetime (= sent).
  `received_at` MUST be NULL.
- inbound rows: `received_at` MUST be a datetime (default = now,
  user-editable). `sent_at` MUST be NULL.
- Mixing (e.g. an inbound row with `sent_at`) is invalid and the API
  must reject it. Otherwise downstream state derivation breaks.

---

## 🚦 State Model (derived, not stored)

The email "state" the user sees in the UI is **derived** from
`(direction, sent_at)` — there is NO `status` column. Single source of
truth: the existing fields.

| State    | Derivation                                          | Meaning                          |
|----------|-----------------------------------------------------|----------------------------------|
| draft    | `direction='outbound' AND sent_at IS NULL`          | saved but not sent yet           |
| sent     | `direction='outbound' AND sent_at IS NOT NULL`      | user confirmed sent              |
| inbound  | `direction='inbound'`                               | supplier reply (received_at set) |

**Do NOT add a `status` column.** It would create two sources of truth
(status + sent_at) and they will drift. State queries use the rules
above directly, or a SQL `CASE` expression at read time.

---

## 🧩 UI Structure

In `frontend/supplier-detail-page` add a new section between Chat and
Attachments:

```
Supplier Detail Page
  ├─ Quick Assessment
  ├─ Commercial Facts
  ├─ Notes
  ├─ Chat with Supplier Assistant
  ├─ 📧 Email Log              ← NEW
  └─ Attachments
```

Email Log section:
- List view, sorted by `sent_at`/`received_at` desc (drafts use `created_at`
  as the timeline anchor since they have no event time yet)
- Each row: `[state badge] subject · timestamp · ai_generated badge`
- Click row → expand to show body
- Two action buttons at bottom:
  - `+ Compose new email` → opens AI-draft modal
  - `+ Log inbound reply`  → opens textarea modal

**State badge colours (Phase 2):**

| State    | Badge colour | Rationale                                       |
|----------|--------------|-------------------------------------------------|
| draft    | grey         | neutral / not-yet-actioned                      |
| sent     | blue         | user took an action                             |
| inbound  | green        | supplier responded — most important progress    |

A user scanning a supplier's email log should see at a glance how many
greens (= replies) vs blues (= waiting on response) vs greys (= still
need to send). Costs almost nothing in CSS, big readability win.

---

## ✉️ Outbound Flow (AI-drafted email)

```
User in Chat → "给 China Copper Sheet 写询价邮件"
  ↓
intent classifier → EMAIL_DRAFT
  ↓
LLM returns FORMAT D
  ↓
Frontend renders as Email Card (NOT a chat bubble)
  ↓
3 action buttons:
  [Open in Email]  (mailto: link)
  [Copy]           (clipboard)
  [Save to Log]    (POST to /api/supplier-emails, direction="outbound")
```

The reason for two send paths:
- `mailto:` is fast for users with desktop email clients, but breaks on
  long bodies (URL length limits) and Chinese encoding edge cases.
- `Copy` always works — paste into Gmail Web / Outlook 365 / company CRM.

`Save to Log` records the email even if the user hasn't actually sent
it yet (treated as `direction="outbound"` with `sent_at=null` until
user marks it sent).

**Persistence rule (critical):**

- AI-generated drafts are **NOT** automatically persisted to the DB.
- They live in frontend state ONLY until the user clicks `[Save to Log]`.
- Otherwise, every "let me try one more prompt" iteration would write
  a row, polluting the supplier's email history with discarded drafts.
- POST `/api/supplier-emails` is therefore the product of an explicit
  user action, never a side effect of LLM completion.

After a draft is saved, two further state transitions:
1. User clicks `[Mark as sent]` (after sending in their email client)
   → PATCH sets `sent_at` to now (or user-edited datetime).
2. User edits subject / body before sending
   → PATCH updates those fields, leaves `sent_at` alone.

---

## 🧾 FORMAT D — `email_draft`

New JSON shape returned by the chat endpoint when intent is
`EMAIL_DRAFT`:

```json
{
  "type": "email_draft",
  "subject": "RFQ: Copper Sheet 1mm — quote request",
  "body": "Dear Supplier,\n\n...",
  "highlights": ["MOQ missing", "ask for lead time", "request samples"]
}
```

**`highlights` is an actionable checklist, not decoration.** The LLM
uses it to record "what's missing or what should be asked" — points the
draft already covers OR points the user might want to add.

User follow-ups like "加上样品要求 / add sample request / make the MOQ
explicit" should map onto an existing highlight item, and the LLM
updates the body to address that specific item. Without `highlights`
the LLM has no anchor for "which part of the draft to revise".

**Constraints to enforce in the prompt:**

- maximum 3–5 items
- each item is a SHORT phrase (≤ 8 words), not a full sentence
- items describe the *thing*, not the rationale (good: "ask for lead
  time"; bad: "We should ask about lead time because it's important")

Frontend rendering rule: **email_draft type renders as an email card,
NEVER as a chat bubble**. Otherwise users get confused (sometimes the
chat answers, sometimes it writes them an email — visual distinction
is required).

---

## 📥 Inbound Flow (logging supplier replies)

```
User clicks "+ Log inbound reply"
  ↓
Modal:
  Subject     [_____________________]
  Body        [
                paste supplier reply
              ]
  Received at [2026-04-28 19:42] (default = now, EDITABLE)
  ↓
POST /api/supplier-emails  direction="inbound"
```

The `received_at` field is editable so a user pasting a reply from
2 days ago can correct the timestamp. This is the key data-integrity
guarantee that lets future "supplier responsiveness" analytics work.

---

## 🤖 AI Integration (reuses last night's chat infrastructure)

`EMAIL_DRAFT` is a new intent in `_INTENT_KEYWORDS`:

```python
("EMAIL_DRAFT", ["写邮件", "起草邮件", "询价邮件", "draft email",
                 "RFQ", "quote request", "compose email"]),
```

Reuses, unchanged:
- `_detect_query_language` → email written in user's language
- `_filter_by_supplier_name` → "给 China Copper Sheet 写..." auto-narrows
- `_FOLLOWUP_SUBSTRINGS` → "再加一句强调样品要求" follow-up auto-modifies
- `_user_wants_web` → if user asks "用网上信息加点细节", web data folded in

New: a per-intent prompt template in `_INTENT_PROMPT_OVERRIDE["EMAIL_DRAFT"]`
that instructs the LLM to produce FORMAT D and use a specific tone
(business formal, signature placeholder, supplier name in greeting).

Verdict layer (RECOMMEND / HEDGE / INSUFFICIENT_DATA / SINGLE) does NOT
fire for EMAIL_DRAFT — that machinery is for OPINION queries, not
email composition.

---

## ❌ Out of Scope (v1)

Hard line — these are explicitly NOT in this feature:

- ❌ SMTP integration (auto-send)
- ❌ IMAP integration (auto-pull supplier replies)
- ❌ Auto email parsing / price extraction from inbound bodies
- ❌ Auto-categorisation of replies
- ❌ Thread grouping UI (data model has `thread_id`, but v1 list is flat)
- ❌ Gmail / Outlook OAuth integration
- ❌ Follow-up reminder system

If the customer asks for any of these, treat as separate v2 features
with their own scope conversation.

---

## ⚙️ Implementation Plan

### Phase 1 — Backend (~2–3 hours)

- [x] `SupplierEmail` model in `models.py` (commit `08eba3a`)
- [x] Schema deployed via `Base.metadata.create_all` — no Alembic
      (project doesn't use Alembic; `init_db()` creates tables on import)
- [ ] API endpoints in `routes/`:
  - `GET  /api/suppliers/{id}/emails` — list emails for a supplier,
    ordered by event time desc (drafts use `created_at` as anchor)
  - `POST /api/supplier-emails` — create. Triggered ONLY by user
    action (`[Save to Log]` for outbound, `[Log inbound reply]` for
    inbound). Validates field integrity rules (no `sent_at` on
    inbound, no `received_at` on outbound).
  - `PATCH /api/supplier-emails/{id}` — **primary purpose: state
    transition draft → sent** (set `sent_at`). Secondary purpose:
    edit subject/body or correct `received_at`. Both supported but
    state transition is the dominant use case.
  - `DELETE /api/supplier-emails/{id}` — remove a row (soft-delete
    not needed for v1; emails are user-controlled records, hard
    delete is fine).
- [ ] Pydantic request/response models
- [ ] Manual smoke test via curl

### Phase 2 — Frontend + AI (~3–4 hours)

- [ ] Email Log section component in supplier detail page
  - List rendering with expand-on-click
  - "+ Compose" / "+ Log inbound" buttons
- [ ] Inbound log modal (textarea + editable datetime)
- [ ] Compose modal — wraps AI chat with FORMAT D rendering
- [ ] Email Card component (FORMAT D renderer, distinct from chat bubble)
- [ ] mailto: link generation + Copy button + Save to Log
- [ ] `EMAIL_DRAFT` intent in `_INTENT_KEYWORDS`
- [ ] `_INTENT_PROMPT_OVERRIDE["EMAIL_DRAFT"]` template (bilingual)
- [ ] FORMAT D in the OUTPUT FORMAT section of the chat prompt
- [ ] Smoke test: 3 cases
  - Chinese query "给 China Copper Sheet 写询价邮件" → email card in
    Chinese, mailto link works
  - English query "draft an RFQ to JINBAICHENG" → English email
  - Follow-up "加上 MOQ 1000 的要求" → modifies the draft, doesn't
    start a new chat answer

---

## ⚠️ Key Constraints (do-not-break list)

When implementing, none of the following may be modified:

1. The chat endpoint's existing intent flow (FIND / OPINION / RANK /
   LOOKUP / COMPARE) must keep its current behaviour. EMAIL_DRAFT is
   added alongside, not on top of, existing intents.
2. `_user_wants_web` and the WEB_SUMMARY override stay as-is.
3. Verdict layer (RECOMMEND / HEDGE / INSUFFICIENT_DATA) is OPINION-only.
4. Follow-up inheritance (`_is_followup`, `effective_query`) stays as-is —
   email drafts should ALSO support follow-up modifications.
5. Email draft renders as an email card. **Never** as a regular chat
   bubble or as plain markdown in the chat answer field.

---

## 🧠 Why this design

Two principles encoded:

1. **Email belongs to the supplier, not to a global inbox.** That's why
   it's a sibling section in the supplier detail page, not a separate
   top-level page. If a feature can't exist without a specific supplier,
   it lives on the supplier card.
2. **Event time and processing time are different.** Auto-stamping
   `received_at = now()` on paste is the wrong call — it pollutes
   responsiveness data with our paste delay. The 5-minute cost of
   adding an editable datetime is the price of keeping future analytics
   honest.

---

## ✅ Done definition

Phase 1 done = backend tests pass, can POST/GET via curl.
Phase 2 done = three smoke-test scenarios above pass in browser.
Feature done = customer sends one real RFQ end-to-end through the
system and it's logged correctly.
