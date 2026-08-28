# Engine 1 Command Center (Multi-POD, SQLite Version)

Deal Execution Tracker & Strategy Dashboard for **Engine 1**, covering three PODS — **PODS 1**,
**PODS 2**, and **PODS 3** — in one shared app. Flask + SQLite backend, single-file frontend
(Tailwind + vanilla JS), Google Cloud Platform–style UI with a light/dark theme, an interactive
analytics workspace, per-Account-Manager row-level access control, and PDF report export.

This app began life as a single-team tracker built for PODS 2. It has since been generalized so
the same Tracker, Analytics, Execution Framework, Action Plan, and Team Tasks features every PODS
2 user already knows can be rolled out to PODS 1 and PODS 3 as well — with each POD's data
completely walled off from the other two.

## 1. Multi-POD Architecture

- **One shared database, one `pod` column everywhere it matters.** Every opportunity, task, login
  log, and performance snapshot belongs to exactly one POD (`pods1` / `pods2` / `pods3`). Every
  pod-scoped role — admin, account_manager, management, solution, project, product — only ever
  sees, queries, exports, or edits their own POD's rows; this is enforced **server-side**, not just
  hidden in the UI, including against a user directly guessing another POD's opportunity/task/user
  ID.
- **Two Engine-1-wide roles, belonging to no single POD, both using the same POD selector** in
  the top header to choose which POD's data they're looking at (or, for the read-only one, to
  combine all three):
  - **`engine1_exec`** — **read-only** across all three PODS combined by default (deals, tasks,
    KPIs summed and merged), or drilled into just one POD. Can never create, edit, or delete an
    opportunity, task, or user in any POD. The one thing it *can* edit is the shared Stages/
    Pillars taxonomy (see below).
  - **`super_admin`** — **full read/write everywhere**: create/edit/delete any opportunity, task,
    or user in any POD (selecting which POD to act in via the same header POD selector), plus
    everything `engine1_exec` can do. It's the only role that can create or manage another
    `engine1_exec` or `super_admin` account — a POD's own admin can never do that.
- **Shared vs. per-POD configuration.** **Deal Stages** and **Strategic Pillars** are one
  Engine-1-wide list (Configuration / "Lists & Automation" tab) so terminology stays consistent
  division-wide — editable only by `engine1_exec` or `super_admin`. Everything money-related — the
  full-year **target**, **YTD achieved**, **recurring revenue**, and each **AM's individual
  target/actual/recurring** — is tracked **per POD**, editable by that POD's own admin in Settings,
  or by `super_admin` acting on whichever POD is currently selected.
- **PODS 1 carries the real, original pipeline** this app was built from (31 opportunities across
  4 real Account Managers — Anisa Rahmy, Arie Prabowo, Ashari, Dimas). **PODS 2 and PODS 3 start
  empty**, each with one bootstrap admin account, ready for those teams to configure and populate.

## 2. Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

App runs at `http://localhost:5000`. A `db.sqlite3` file is created and seeded automatically on
first run (users, config, and PODS 1's 31 opportunities). Set `PORT` to change the port.

## 3. Accounts

| Username        | Password      | Role            | POD    | Access                                              |
|-----------------|---------------|-----------------|--------|------------------------------------------------------|
| `admin`         | `admin123`    | admin           | PODS 1 | Full access within PODS 1: Tracker, Analytics, Settings; edits all of PODS 1 |
| `anisa`         | `anisa123`    | account_manager | PODS 1 | Edits only opportunities assigned to **Anisa Rahmy** |
| `arie`          | `arie123`     | account_manager | PODS 1 | Edits only opportunities assigned to **Arie Prabowo**|
| `ashari`        | `ashari123`   | account_manager | PODS 1 | Edits only opportunities assigned to **Ashari**      |
| `dimas`         | `dimas123`    | account_manager | PODS 1 | Edits only opportunities assigned to **Dimas**       |
| `exec`          | `exec123`     | management      | PODS 1 | View-only across PODS 1's Tracker and Analytics       |
| `pods2_admin`   | `changeme123` | admin           | PODS 2 | Full access within PODS 2 (starts empty)              |
| `pods3_admin`   | `changeme123` | admin           | PODS 3 | Full access within PODS 3 (starts empty)              |
| `engine1_exec`  | `changeme123` | engine1_exec    | *(none)* | Read-only across all 3 PODS combined, or drilled into one; exclusively manages the shared Stages/Pillars |
| `super_admin`   | `changeme123` | super_admin     | *(none)* | Full read/write in any POD (pick which via the header POD selector); the only role that can create other engine1_exec/super_admin accounts |

> **Change every one of these passwords before a real rollout** — especially the four
> `changeme123` bootstrap accounts — via Settings → User Management (each POD's own admin manages
> their own POD's users; there is no cross-POD user management anywhere in the app).

No `solution` / `project` / `product` accounts are seeded for any POD — each POD's admin creates
them from Settings → User Management → Add User when that POD is ready to bring those teams on.

## 4. Role-Based & Row-Level Access Control

- **admin** (per POD) — full CRUD on opportunities, users, and that POD's own settings/targets —
  never another POD's.
- **account_manager** (per POD) — can view **all** opportunities in their own POD but can only
  **create/edit/delete/update** the ones assigned to their own name. The UI shows a 🔒 lock on
  opportunities owned by other AMs, and the server independently rejects cross-AM (and cross-POD)
  edits with `403`. New opportunities an AM creates are automatically assigned to them, in their
  own POD (they cannot assign to someone else or to another POD).
- **management** (per POD) — read-only within their own POD; no Settings tab, no edit controls.
- **solution / project / product** (per POD) — cross-functional roles that bridge Sales with
  delivery, scoped to their own POD. They can browse that POD's **Tracker** read-only (every
  opportunity, customer, AM, TCV, stage in their POD, for context) plus their own **My Team Tasks**
  inbox (every task assigned to their team, across every opportunity in their POD). From either
  place they can open an opportunity's **Team Tasks** panel to file a new follow-up under their own
  team, and update the **status** and **note** on their own team's tasks — nothing else. They can't
  edit any opportunity field, reassign a task to a different team, touch another team's tasks, or
  reach into another POD, and have no access to Calendar, Analytics, or Performance; both the nav
  and the API independently enforce this.
- **engine1_exec** (Engine-1-wide, no POD) — read-only across all three PODS' Tracker, Calendar,
  Analytics, Performance, and Weekly Meeting; the **POD selector** in the header switches between
  "All PODS combined" and drilling into one specific POD. Cannot create, edit, or delete any
  opportunity, task, or user anywhere. The one thing it *can* edit is the Engine-1-wide **Deal
  Stages / Strategic Pillars** list under "Lists & Automation" — the one piece of configuration
  meant to stay consistent across all three PODS rather than be owned by any single one.
- **super_admin** (Engine-1-wide, no POD) — everything `engine1_exec` can do, plus full read/write:
  create, edit, and delete opportunities, tasks, and users in **any** POD, acting on whichever POD
  is picked via the same header **POD selector** (creating an opportunity, importing a backup, or
  editing a POD's target figures all require a specific POD to be selected first — "All PODS
  combined" only makes sense for reading). It's the only role that can create or edit another
  `engine1_exec` or `super_admin` account, or move a user between PODS; a POD's own admin is
  permanently confined to managing users within that one POD.

Enforced both in the UI and server-side (`can_edit_deal()`, `deal_visible_to()`, `query_pod_for()`,
`mutation_pod_for()` in `app.py`).

## 5. Feature Highlights

- **Theme** — light/dark toggle in the top app bar (persists in `localStorage`, follows the OS
  preference on first load).
- **AWS-inspired design** — a fixed dark-navy top app bar (stays dark in both light and dark theme,
  like aws.amazon.com), left nav rail, card-based layout, an AWS Cloudscape-style blue accent
  (no orange), and Inter/Amazon Ember-style typography. The sign-in page matches: it's built from the
  same theme tokens as the rest of the app (so it follows your light/dark preference too) with a
  fixed dark-navy brand panel on the right, the same as the app's header.
- **Feature search (Ctrl/Cmd+K)** — click the search bar in the top app bar (or press **Ctrl+K** /
  **Cmd+K**) to open a command palette that searches every section of the dashboard (Tracker,
  Calendar, Pipeline Analytics, Performance, Login Logs, Settings, etc.) plus a few quick actions
  (Add Opportunity, Export PDF, Toggle theme). Type to filter, use the arrow keys + Enter, or click
  a result to jump straight there. Only shows what your role can access.
- **Full-year coverage header** — Full-Year 2026 Target, Achieved (YTD), 2026 Pipeline + Recurring,
  and Remaining Gap as icon-badged metric cards, plus a stacked **coverage bar** (Achieved /
  Recurring / 2026 Pipeline / Gap) with pill-style legend chips, so you can see how much of the
  full-year target is already covered and what's left to close.
- **Tracker** — search, filter by AM / Pillar / Stage, **sort** (defaults to Progress
  High → Low, so opportunities closest to 100% surface first; also TCV, Rev 2026, name), and
  **pagination** (10/25/50 rows per page). Inline progress sliders, blocker flags, and
  next-action checklists. **Double-click any row** to open a full opportunity detail drawer.
- **Bulk delete** — admin, account_manager, and super_admin only (never management, the
  cross-functional roles, or the read-only engine1_exec). A checkbox column and a header
  "select all" appear on the Tracker; only rows that role could delete one at a time are
  selectable (an account_manager still only ever sees a checkbox on their own opportunities), and
  "Delete Selected (N)" asks for one confirmation before removing all of them. The server rechecks
  the same per-opportunity permission for every id in the request — a row that isn't actually yours
  is silently skipped and reported back rather than deleting anything it shouldn't.
- **Strategy per opportunity** — every deal has a free-text **Strategy** ("how you'll win & close"),
  editable by the owning AM. It shows in the detail drawer and drives the next-action plan, and is
  summarized for management in a **Strategy Playbook** card on the Analytics tab (grouped by AM,
  respecting the analytics filters).
- **Achievement & recurring** — in Settings, admins enter Achieved-YTD and expected recurring revenue.
  The target stays the full-year 2026 goal; the dashboard then shows the true remaining gap
  (Target − Achieved − Recurring − 2026 Pipeline).
- **Thousand separators** — all large IDR inputs (target, achievement, recurring, per-AM targets, TCV,
  Rev 2026) format as you type: `163000000000` → `163.000.000.000`, so you never miscount a zero.
- **Calendar** — shows every scheduled item: every evidence entry logged against any of the 8
  Enterprise Proofs (whatever stage it's in — planned, in progress or done) plus every ad-hoc next
  action. Toggles between **Week** (default) and **Month** view. Multi-day entries (see Execution
  Framework below) render on every day they span. Item labels show only the AM's own wording — no
  "Proof of X" prefix. The KPI tiles (Scheduled / Overdue / In Progress / Done / No date) are
  **clickable filters** — click one to narrow the grid and list to that category, click again to
  clear it. Clicking an item opens a small detail popup for just that item (text, status, dates,
  which opportunity it belongs to) with a **View opportunity** button, rather than jumping straight
  into the full opportunity. Admin and management see everyone with an AM filter; an **Account
  Manager only ever sees their own** action plan.
- **Configuration tab** (admin) — manage the **Deal Stages** and **Strategic Pillars** lists.
  Stages are fully configurable: add/rename/delete, and every dropdown, filter and chart follows.
  Deleting a value that is still in use asks for confirmation and never rewrites existing deals.
  (The Squad feature is hidden for now — the underlying data isn't deleted, so it can come back later.)
- **Pipeline vs Account Manager Gap** — per AM: `Gap = Target − YTD actual − FY26 recurring (no
  churn) − 2026 pipeline`, with a stacked coverage bar so you instantly see who is short. Enter each
  AM's Target / YTD / Recurring under Settings → Account Manager Targets, or use the **Import
  Target/Actual/Recurring** button (admin only) right on this Gap & Targets sub-tab to bulk-update
  them from the same monthly "PODS (2)" performance workbook already used for Performance import —
  only account managers matched by name in the file are changed, everyone else's figures are left
  untouched, and any unmatched names are reported back so nothing is silently skipped.
- **Strategy Coverage** — replaces the old wall-of-text playbook: per-AM coverage bars, a call-out
  listing opportunities that still have no strategy, and one collapsed line per documented deal that
  expands to read the full strategy.
- **Backup & restore (XLSX)** — admin-only, in Settings → Data Backup, scoped to that admin's own
  POD. Export everything in that POD (opportunities, strategies, next actions with dates, the
  8-proof framework, that POD's own target/AM figures, that POD's users) to one Excel workbook; the
  same file is the import template for restoring or migrating that POD to another host. Re-uploading
  it can never touch another POD's data even if a row's ID or a username happens to collide with one
  from a different POD — a mismatch is treated as new rather than overwritten.
- **Execution Framework — the 8 Enterprise Proofs.** Every opportunity has a wider *Execution
  Framework* tab in its edit modal implementing the shared Engine 1 execution standard used across
  all three PODS: **1 Qualification, 2 Engagement,
  3 Concept, 4 Value, 5 Contract, 6 Delivery, 7 Operation, 8 CLM**. Each proof carries an overall
  status (Not started / In progress / Done / N/A), a target date, and **an unlimited list of evidence
  entries** — each with its own **status** (Not started / Planned / In progress / Done, clickable
  directly on the entry) and its own **start date and optional end date**. Leaving the end date blank
  means a single-day activity; setting it schedules a multi-day activity that shows on every day it
  spans on the Calendar. This is how an AM records everything they have done — or plan to do — to
  fulfil that proof, building a real audit trail rather than a single note. Existing entries without
  their own status/dates inherit sensible defaults automatically, so nothing already recorded needs
  to be re-entered. N/A proofs are excluded from completion maths.
- **Autosave + unsaved-changes warning.** While editing an existing opportunity, any change on the
  Opportunity Details, Execution Framework, or Action Plan tabs (Team Tasks excepted — those already
  save immediately per-task) is auto-saved to the server about 1.5 seconds after you stop typing, with
  a small status label next to the Save button ("Unsaved changes" → "Saving…" → "All changes saved").
  If you close or cancel the modal in that short window before autosave catches up (or a new,
  not-yet-created opportunity, which can't autosave until it exists), you get a confirmation prompt
  first so nothing typed is silently lost. The **Save** button still works as before and closes the
  modal immediately.
- **Action Plan tab** — a third tab in the opportunity edit modal, alongside Opportunity Details and
  Execution Framework. It auto-collects every evidence entry across the 8 Enterprise Proofs that's
  currently marked **Planned** — the team's to-do list for that opportunity — with its own status
  selector so you can bump a step to In progress/Done right there (it disappears from this list the
  moment it's no longer Planned). Below it, **Next actions** (ad-hoc items that don't belong to a
  specific proof) now lives in its own tab too, instead of being tucked away in a collapsible on the
  Execution Framework tab.
- **Stage — manual** — an opportunity's Stage is set directly by the AM/admin in its edit form.
  (Earlier builds could auto-derive it from execution-framework progress; that automation has been
  removed so the team controls Stage explicitly.)
- **Team Tasks — the Sales ↔ Solution/Project/Product bridge.** A fourth tab in the opportunity edit
  modal where anyone who can touch it — Sales (AM/admin), or Solution/Project/Product themselves —
  files a follow-up task, deciding its **status right away** (Not started / In progress / **Blocked**
  / **Needs discussion** / Done — not stuck defaulting to Not started) and an optional **target date**
  it needs to be resolved by. Sales sees the full opportunity and can assign a task to any of the
  three teams, reassign or delete any task, and edit anything else on the deal as usual. A
  cross-functional user instead gets a stripped-down version of the same modal (title "Team Tasks",
  no other tabs, no Save button — just a read-only summary of the opportunity for context): they can
  only file tasks under their *own* team and can only edit/delete their own team's tasks; every other
  team's tasks on that opportunity show up locked (status, note and date disabled, no delete). Every
  change saves immediately (not tied to a Save button), so it stays in sync in real time. Each
  cross-functional team also gets its own **My Team Tasks** inbox — a focused worklist with no FY
  target/gap/coverage noise — listing every task assigned to them across *all* opportunities, sorted
  by **closest target date first** (done tasks sink to the bottom); tick a task done, change its
  status, edit the note, or adjust the date, right there. Admin and management get a separate
  **Weekly Meeting** board with the same closest-date-first sorting — every Blocked/Needs-discussion
  item across the whole portfolio in one place, filterable by team and status, built specifically to
  run the weekly cross-team sync and see what's most urgent to resolve. Unlike My Team Tasks, Weekly
  Meeting is visible to **every role** (Sales and admin/management included, not just Solution/
  Project/Product) so everyone can see the same picture ahead of the meeting; it's read-only there.
  Like My Team Tasks, it hides the FY target/gap/coverage strip too — both are focused worklists.
- **Action Plan timeline** — in the same Action Plan tab, Sales can set two **milestones** per
  opportunity: the **expected PO date** and the **expected revenue booking date**. A **horizontal**
  timeline right below plots those milestones together with every dated Planned execution step along
  one dated axis, from **today** (marked with a red reference line) through to the end of the
  project — regular steps as small dots, the PO/Revenue milestones as larger rings, overdue steps in
  red, with a small legend underneath. It updates live as you edit either milestone date or a step's
  date.
- **Framework analytics** — Analytics shows a proof-by-proof funnel (done / in progress / not
  started across the filtered deals), completion by Account Manager, and a click-to-drill list of the
  deals stuck at any given proof. The PDF report includes the same breakdown.
- **Account Manager focus** — when an AM signs in, the Tracker is pre-filtered to their own
  opportunities (clearly flagged, and they can widen it to the whole team at any time).
- **Management View** (admin + management only) — a simple executive briefing that management lands
  on by default. A **"Needs your attention"** headline states whether anything is wrong, then three
  widgets: **Blockers** and **Action points** (collapsed, one-line summary until expanded), and
  **Account Manager performance** (open by default) — a per-AM card showing Target vs Covered
  (Achieved + Recurring + 2026 Pipeline, the same formula as the Analytics Gap section), an
  attainment bar, and a warning badge (On target / Behind target / Significantly behind) so you can
  see at a glance who isn't covering their number. Beneath it, an **Insight** panel explains *why*:
  for every AM short of target it checks whether they have blocked deals and calls that out as a
  likely contributor, or — if there are no blockers on file — says the gap more likely needs pipeline
  generation than escalation. Click or double-click anything to open the full opportunity detail.
- **Performance tab** — upload the monthly **PODS 2 - ACH** workbook from the performance team
  (admin only) and the dashboard reads the `PODS (2)` and `byAccount (BP)` sheets to show:
  team scorecard (Target FY, Actual YTD, MRC, PO on Hand, Forecast, Gap, attainment);
  **pipeline cover** against the conservative 3× rule per AM; monthly target vs actual/forecast;
  **MRC run-rate with next-quarter and full-year projection** (no-churn assumption);
  **churn watch** and **growing accounts** compared to the previous month or quarter; and
  **top revenue accounts cross-checked against pipeline in this dashboard** so you can see which
  big earners have no opportunity attached (upsell blind spots). The last 12 uploads are retained.
- **Performance PDF export** — *Export PDF* on the Performance tab downloads the current snapshot
  (optionally scoped to one AM) as a report: team scorecard, gap-to-target, pipeline cover, per-AM
  breakdown, churn watch and growing accounts (vs previous month), and revenue by product pillar —
  the gap comes first since that's the number management needs immediately, saved as
  `performance_summary_<date>.pdf`.
- **Login Logs** (admin only) — every successful sign-in is recorded with a timestamp (Jakarta time,
  GMT+7), the user, their role and IP address, so an admin can see exactly when each Account Manager
  last used the dashboard.
  Only the most recent logs are kept — the limit defaults to **100** and is adjustable (10–2000) right
  on the Login Logs page — so storage never grows unbounded. Logs can be exported to Excel at any time
  (`login_logs_<date>.xlsx`). This page and its API are strictly admin-only; it does not appear in the
  sidebar for any other role.
- **Tracker sheet export** — on Analytics, *Export Tracker sheet* produces one row per activity in
  the exact column order of the shared **H2 2026 Sales Activity Tracker** (`No. / PODS / Opportunity
  Name / Customer / Account Manager / TCV / Rev 2026 / Target Quarter / Pillar / Activity / Status /
  Due Date / Completed Date / Notes`), ready to paste into the OneDrive **Tracker** sheet. Each proof's
  evidence entries become activity rows; add `?include_empty=1` to also emit not-yet-started proofs.
- **Rev 2026 column** — every opportunity carries a `revenue_2026` value: the revenue realizable in
  the remaining H2 2026 (vs. the full multi-year TCV). This is the number that counts toward the
  2026 result and is editable per deal. Seeded equal to TCV; refine per deal in the edit modal.
- **Analytics** — organized into five sub-tabs so each view stays focused: **1. Gap & Targets**
  (the default landing tab — Pipeline vs Account Manager Gap comes first, since that's what
  management needs to see immediately), **2. Pipeline Breakdown** (charts for pipeline by AM,
  pillar, target quarter, stage distribution and top-8 opportunities), **3. Execution
  Framework** (the 8-proof funnel and completion by AM), **4. Strategy** (Strategy Coverage), and
  **5. Leaderboard & Summary** (AM leaderboard + management summary). 7 KPI cards (incl. 2026 Rev)
  and the **filter bar** (AM / Pillar / Stage / Quarter) stay visible across every sub-tab,
  and **clicking any chart bar/segment drills down** into the same filters.
- **AM target vs pipeline** — admins set a per-AM 2026 revenue target in Settings; the Analytics tab
  shows each AM's attainment (2026 revenue ÷ target) with a color-coded progress bar, gap, and TCV
  pipeline, plus a combined-team roll-up. Quickly spots which AMs are on/behind target.
- **PDF export** — professional multi-section report (ReportLab) including 2026 revenue, an AM
  target-attainment table, and per-opportunity detail, downloaded as `deal_tracker_report_<date>.pdf`.

## 6. API Reference

**POD scoping, in one place:** almost every GET endpoint below is scoped by `query_pod_for(user)` -
a pod-scoped user always gets their own POD's data, ignoring any `?pod=` they pass; `engine1_exec`
and `super_admin` (both podless) get all three PODS combined unless `?pod=pods1|pods2|pods3` is
given, in which case it's scoped to just that one. Mutations (create/update/delete/import) use
`mutation_pod_for(user, data)` instead: a pod-scoped user's own POD is forced server-side, never a
client-supplied one; a podless `super_admin` must supply the target POD itself (JSON `pod` field,
`?pod=`, or a multipart form field, in that priority) or the request 400s - `engine1_exec` never
reaches a mutation endpoint at all except the shared-taxonomy config PUT. Every mutation also
independently verifies the target row's POD matches before allowing an edit/delete, so no role can
affect another POD's data even by guessing an ID.

**Auth** — `POST /api/login` → `{token, role, pod, pod_label, username, full_name}`, `POST /api/logout`
**Account managers** — `GET /api/account_managers` (any authenticated user; POD-scoped, see above)
**Deals** — `GET /api/deals?am=&pillar=&stage=&quarter=&pod=`, `POST /api/deals` (admin/account_manager
in their own POD, or `super_admin` with an explicit `pod`), `PUT /api/deals/<id>`,
`DELETE /api/deals/<id>`, `PUT /api/deals/<id>/progress`, `PUT /api/deals/<id>/blocker` (mutations
require admin, the owning AM, or super_admin). `POST /api/deals/bulk_delete` (admin/account_manager/
super_admin) takes `{"ids": [...]}` and applies the exact same per-id permission check as single
delete — ids that don't exist or aren't editable by the caller are skipped and returned in a
`skipped` list (with a reason) rather than failing the whole request; `deleted`/`deleted_ids` report
what actually went through. Each deal includes `pod` and a display `pod_label`.
**Users** — `GET/POST /api/users`, `PUT/DELETE /api/users/<id>`. A POD's own admin is confined to
that POD and to pod-scoped roles (`admin`, `account_manager`, `management`, `solution`, `project`,
`product`) - it can never create, promote to, or manage an `engine1_exec`/`super_admin` account.
`super_admin` manages users in **any** POD (via a `pod` field in the request body) and is the only
role that can create/edit another `engine1_exec` or `super_admin` account, or move a user's POD.
**Team Tasks** — `GET/POST /api/deals/<id>/tasks` (list: any authenticated role, POD-checked against
the deal; create: admin/owning AM/super_admin choosing any team, or a cross-functional role creating
only under its own team — the `team` field is ignored and forced server-side for them; both may set
the starting `status` and an optional `due` date at creation time), `PUT/DELETE /api/tasks/<id>`
(cross-functional roles may edit/delete only their own team's tasks in their own POD, and can change
`text`/`status`/`note`/`due` but never `team`; admin/owning AM/super_admin can edit or delete any
field on any task on their deals), `GET /api/tasks?team=&status=&scope=&pod=` (cross-opportunity
list, any authenticated role — by default a cross-functional role only ever sees its own team's
tasks; pass `scope=all` to see every team's tasks instead, which is what the shared Weekly Meeting
board uses).
**Config** — `GET /api/config?pod=` (all authenticated roles), `PUT /api/config`: admin edits their
own POD's `target_amount`/`am_targets`/`am_achievements`/`am_recurring`/`current_achievement`/
`recurring_revenue` only; `engine1_exec` edits only the shared `strategic_pillars`/`stages`/
`max_login_logs`; `super_admin` can send either or both in the same request (a chosen POD's money
fields need that POD resolved via `mutation_pod_for`, taxonomy fields don't) - every role's PUT
silently ignores fields it doesn't own. The GET response merges the shared taxonomy with either one
POD's figures (`pod`/`pod_label` reflect which) or, for a podless role with no `?pod=`, all three
summed plus a `pods_breakdown` array of each POD's own figures.
Deals carry `estimated_value` (TCV), `revenue_2026`, and the two Action Plan milestones
`expected_po_date` / `expected_revenue_date`.
**Data Backup** — `GET /api/export/xlsx`, `POST /api/import/xlsx` (admin or super_admin; a podless
super_admin must resolve a POD via `mutation_pod_for` first or gets a 400) - both POD-scoped, same
guarantee as deals: an ID or username collision with another POD is treated as new, never merged in.
**Reports** — `GET /api/export/pdf`, `GET /api/performance/export_pdf?am=` (both POD-scoped; a
podless role with no POD selected gets an all-PODS combined report where that makes sense, or an
error where a single POD must be chosen, e.g. the performance PDF).
**Login Logs** — `GET /api/login_logs`, `GET /api/login_logs/export_xlsx` (admin: own POD only;
super_admin: any POD, or all combined). The shared `max_login_logs` setting (default 100, range
10–2000, managed by `engine1_exec`/`super_admin`) controls how many of the most recent logs are kept
**per POD** — one POD's sign-in volume can never crowd out another's history.

All endpoints except `/api/login` require an `Authorization: Bearer <token>` header.

## 7. Deployment (make it accessible to your whole team)

Deploy **this** (SQLite) version for a team — it keeps one shared database on the server so
everyone sees and edits the same data. (The `deal_tracker_no_sql_pro` edition stores data in each
person's own browser and cannot be shared.)

**Before you deploy — do these two things:**
1. **Change the demo passwords.** After first login as `admin`, go to Settings → User Management and
   reset every account. The app ships with publicly known credentials.
2. **Run a single worker.** Auth tokens are held in memory, so the included `Procfile`, `Dockerfile`,
   and `render.yaml` all use `--workers 1`. Don't raise this, or users will get random logouts.

### Option A — PythonAnywhere (recommended: free *and* keeps your data)
Best free choice for a small team: no credit card, and the SQLite file persists.
1. Create a free "Beginner" account at pythonanywhere.com.
2. Open a **Bash console** and get the code onto the server, e.g.
   `git clone <your-repo-url>` (push this folder to GitHub first) — or upload a zip via the **Files** tab.
3. Install dependencies: `pip install --user Flask reportlab gunicorn`
4. **Web** tab → *Add a new web app* → *Manual configuration* → *Python 3.10+*.
5. Edit the WSGI file it created (link on the Web tab) so it points at this app:
   ```python
   import sys
   path = "/home/<youruser>/deal_tracker_sqlite_pro"   # folder containing app.py
   if path not in sys.path:
       sys.path.insert(0, path)
   from app import app as application                    # app.py exposes `app`
   ```
6. Click **Reload**. Your team visits `https://<youruser>.pythonanywhere.com` and logs in.
   `db.sqlite3` is created next to `app.py` on first load and persists across reloads.
   *(Free accounts click a "run for 3 more months" button occasionally to stay active.)*

### Option B — Render (easiest Git deploy; free tier does NOT keep data)
1. Push this folder to GitHub, then on render.com create a **New → Web Service** from the repo.
2. Build: `pip install -r requirements.txt` — Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1`.
3. You get a free `https://…onrender.com` URL with HTTPS.
   ⚠️ **Free-tier caveat:** the disk is ephemeral, so `db.sqlite3` is wiped on every restart/redeploy,
   and the service sleeps after ~15 min idle (first request then takes ~50s). To keep data on Render
   you need a **paid** plan + persistent disk (see `render.yaml`), or switch storage to a free managed
   Postgres (e.g. Neon/Supabase) — that requires code changes. For free + durable, use Option A.

### Option C — Docker (any host / your own server)
```bash
docker build -t deal-tracker-pods2 .
docker run -p 8000:8000 -v "$PWD/data:/app/data" -e DB_PATH=/app/data/db.sqlite3 deal-tracker-pods2
```
The `-v` volume + `DB_PATH` keep the database on the host so it survives container restarts.

### Option D — Heroku / Railway / any Procfile host
`git push heroku main` — uses `Procfile`, `runtime.txt`, `requirements.txt`. Note these platforms
also have ephemeral filesystems; attach a managed database or volume for durable storage.

## 8. Updating a running deployment (without losing data)

New columns are added **non-destructively**: on startup `migrate_db()` runs `ALTER TABLE ADD COLUMN`
only for columns that don't yet exist, so your existing `db.sqlite3` keeps all its rows. To update a
live PythonAnywhere instance:

1. Replace `app.py` and `templates/index.html` with the new versions (git pull in a Bash console, or
   re-upload via the **Files** tab). **Do not delete `db.sqlite3`.**
2. **If this update adds a new library, install it first.** The XLSX backup feature needs `openpyxl`:
   ```bash
   pip3.10 install --user openpyxl        # use the pip matching your web app's Python version
   ```
   (Everything else keeps working without it; only Export/Import will report that it's missing.)
3. Go to the **Web** tab and click **Reload**. On reload the app auto-migrates the database — it adds
   any missing columns (`deals.strategy`, `deals.proofs`, `config.current_achievement`,
   `config.recurring_revenue`, `config.stages`, `config.am_achievements`, `config.am_recurring`) and
   preserves every existing deal, user, password and setting. A proof's older single note is folded
   into its evidence list automatically. (`config.auto_stage`/`config.stage_rules` may still exist
   from older releases but are no longer read — Stage is manual only.) It also adds the new
   `deal_tasks` table, and — only if your `users` table predates the `solution`/`project`/`product`
   roles — rebuilds `users` to widen its role constraint, copying every existing user row across
   unchanged (usernames, password hashes, everything) rather than touching any data.
4. Recommended right after reloading: **Settings → Data Backup → Export all data** so you have a
   restore point, then fill in the per-AM Target / YTD / Recurring figures.

## 9. One-time onboarding: bulk import from a Sales Activity Tracker workbook

When a POD's real pipeline already lives in a spreadsheet in the shared "Sales Activity Tracker"
layout (the same one-row-per-activity shape `Analytics → Export Tracker sheet` produces — a "PODS "
column, Opportunity Name, Customer, Account Manager, TCV, Rev 2026, Target Quarter, an
8-Enterprise-Proof "Pillar" column, Activity/Action, Status, Due Date, Completed Date, Notes),
**Settings → Bulk Import from Sales Activity Tracker** (super_admin only) turns it straight into
opportunities:

- Rows are grouped back into one deal per (POD, Opportunity Name, Customer), with every row's
  activity becoming an evidence entry under the matching one of the 8 Enterprise Proofs.
- Each row's own **"PODS "** column decides which app POD it lands in (matched against the POD
  labels case-insensitively) — so **one upload can populate multiple PODS at once**, which is why
  this import is restricted to `super_admin` and no other role can reach it.
- A new Account Manager name found in the file gets an `account_manager` account created
  automatically (temporary password `changeme123`) so per-AM filtering and targets work right away.
- This workbook shape doesn't carry a Strategic Pillar (business category) or Stage, so every
  imported deal defaults to the first configured Strategic Pillar and "Prospecting" — go through and
  correct those per opportunity afterward.
- **Additive only**: it only ever creates new opportunities, never updates or deletes existing ones.
  Re-uploading the same file twice will create duplicates — there's no ID column in this format to
  match against, unlike `Settings → Data Backup → Import`.
- Rows with a blank or unrecognised POD label are skipped and reported back in the result, not
  silently dropped.

**If your existing seeded team turns out to be the wrong POD number** (e.g. you seeded "PODS 1" with
a team that turns out to actually be the real PODS 2 in your org, and now need to bring in the real
PODS 1's data), correct that *before* running the bulk import: `scripts/swap_pod1_pod2.py` swaps
every deal, task, user, login log, performance snapshot, and each POD's own target/AM figures
between PODS 1 and PODS 2 in one non-destructive pass (the shared Stages/Pillars taxonomy is
untouched, since it isn't POD-specific). Run it once against your live database:

```bash
cd ~/engine1_dashboard      # or wherever app.py lives
python3 scripts/swap_pod1_pod2.py
```

It prints a before/after row count per POD so you can confirm the swap did what you expected, and
also renames the bootstrap `pods1_admin`/`pods2_admin` accounts to match wherever they ended up. It's
safe to re-run — running it twice just swaps back. Take a `Settings → Data Backup → Export` first if
you want an extra safety net, and restart/reload the web app afterward so it picks up the change.

## 10. Notes

- Session tokens are held in-memory (`TOKENS` in `app.py`); a server restart requires users to log
  in again. Passwords are hashed with PBKDF2 (`werkzeug.security`).
- Charts are lightweight inline SVG — no chart-library dependency beyond the Tailwind/FontAwesome CDNs.
- Branding lives in `static/img/`: `pods2-logo.png` is the master file; `pods2-logo-256.png` is what
  the header and sign-in page actually load, and `favicon-16/32.png` + `apple-touch-icon.png` are the
  browser-tab/bookmark icons. Regenerate the smaller sizes from the master with Pillow if you ever
  swap the logo — see the resize snippet used when these were first generated (any 1:1 PNG works).
# pods2_focused_oppty_dashboard
# pods2_focused_oppty_dashboard_sql
# pods2_focused_oppty_dashboard_sql
