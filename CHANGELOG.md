# Changelog

## 0.14.1 – 2026-10-19

- Projects: the "Aktuell"/"Current" badge (and the highlighted card border) for the project preselected in quick capture is no longer shown in the list and cards view.
- Workflows: a template's pages (detail, runs, schedules, event triggers) now use template-scoped tabs that open the template's own views, with a breadcrumb back to the global Workflows list; the global pages keep their global tabs.
- Workflow schedules: the template's schedule page lists existing schedules and opens the first/selected one for editing; the "New schedule" form only appears on demand, and the workday-rule fields are hidden for a plain cron rule.
- Inbox: reviews by reviewer agents (running, waiting or sent back) move out of "Needs your action" into their own section "Reviews by agents" below a divider – one compact, left-aligned row per task in one card (status, key, prio, age; title; reviewer · "Review running since …"; "Open task", "Open diff ↗"). The owner/admin overrule (approve & merge, request changes) stays as small secondary buttons. The section is hidden when no agent review is pending.
- Inbox: the subtitle ("x items need you"), the "Needs your action" count and the phone tab badge no longer count agent reviews; the "Reviews" filter still counts both and shows both sections.
- Inbox: the notices panel no longer repeats agent reviews as a "Review by agents" table with explanation text; it only shows a one-line link "n reviews by agents ↓" to the new section, which now carries a short explanation under its heading.

## 0.14.0 – 2026-10-18

- UI: tab bar and quick-capture icons are inline SVG, and symbols missing from IBM Plex (arrows, ✓, ⚠, ▲, ⋯ …) come
  from a small self-hosted DejaVu Sans subset – pages no longer look different depending on the fonts installed on
  the viewer's system.
- Task detail: the sidebar shows the resolved model again (e.g. `haiku` for tier "simple") and notes when the agent
  ignores it.
- Runner: the Docker launcher client reads the launcher's refusal even when the launcher closes the socket before the
  request is sent (was reported as "not reachable: Broken pipe").
- Release: the public export also excludes `docs/ui-redesign/` (internal redesign mockups and review screenshots).
- Fix DB integration tests after parallel migration merges: compare schema tables with ORM metadata and use the final output-budget migration revisions.
- Web UI: foundation for the redesign – `--ui-*` design tokens (colours, status/zone/priority colours, type scale,
  radii, spacing), self-hosted IBM Plex Sans/Mono (no external font requests, system fallbacks) and shared
  components in `static/ui.css` with Jinja macros in `templates/_ui.html` (buttons, pills, priority badge, task key,
  actor chip, segmented control, filter chips, toggle switch, select/search, card, table with phone card layout,
  page header, tabs, empty state).
- Admin → UI kit (`/admin/ui-kit`, admins only) shows all components. Existing pages keep their layout and now use
  IBM Plex.
- Replace the app header with inline quick capture, active navigation and an avatar menu for profile, language and logout.
- Add tablet navigation, a safe-area-aware phone tab bar with Inbox action count, and quick capture/More sheets; press N to capture a task.
- Redesign the Backlog with counted status chips, URL-backed filters, sortable task rows, pagination, and shared UI components.
- Add bulk assignment, sprint, priority, and status actions with ordinary permissions, concurrency checks, and per-task results.
- Add phone task cards, a filter sheet, and a sticky selection toolbar; translate the new controls into German.
- `tbr update --depends-on <NR> [--depends-on …]` replaces a task's dependencies (`none` removes all); MCP `create_task` and `update_task` take `depends_on` (list or comma-separated string). Docs, skills and MCP instructions mention it.
- Dependency circles (A → B → A) are refused with `422` instead of leaving tasks that can never be claimed.
- Task page: read view by default (result, description, acceptance criteria, activity) with an explicit edit mode, inline property edits, and separate Discussion, History and Attachments tabs.
- Sprints: a closed sprint can be reopened (`POST /api/v1/projects/{key}/sprints/{id}/reopen`, button "Reopen sprint" in the web UI, `tbr sprint reopen`) – the status goes back to `released` and the tasks that were moved out when it was closed return on request, their status unchanged.
- Inbox redesign: action cards (review/question) with type pill, task key, priority, actor, detail and age, plus inline actions "Approve & merge", "Request changes" and question "Send".
- Segmented filter "All | Reviews | Questions" and the sprint filter now work together as URL query params (`tab`, `sprint`).
- New notices panel with colored dots per event, "Mark all read" and a "Notification settings" link; read state stored in `notifications.read_at`.
- Mobile (< 640 px): single column, scrollable segmented control and full-width inline answer.
- Project page redesigned: breadcrumb, header with key badge, owner/status/repo subtitle and Backlog/Kanban/“+ New task”
  actions; tabs Settings, Members, Gitea, Email recipients and Data & output gate (deep links `#settings`, `#members`,
  `#gitea`, `#recipients`, `#data`); settings grouped in “General” and “Agents & review” cards with an unsaved-changes
  indicator, “Discard” and “Save changes”; right column with Gitea (status pill, clone URL with “Copy”, “Repeat setup”),
  members, email recipients, tasks and sprints. On phones the tabs scroll, the right column moves below the form and
  Backlog/Kanban collapse into a “⋯” menu.
- Kanban: redesigned board – always all 7 columns (Draft, Defined, In progress, Questions/Aborted, Review, Rework, Done), equal width, min 1260 px with horizontal scroll on smaller screens, column backgrounds stretched to the tallest column.
- Kanban: card filter search added to the filter row; segmented control "Board | Swimlanes by agent" (grouped per assignee).
- Kanban cards: mono task key, model/runner tags and the priority badge on the right; sprint line; assignee chip and project key; the data-zone badge only for restricted classes (local only / sealed).
- Kanban: "Draft" column ends with "+ Add task", "Done" column ends with "Show all N in Backlog"; the done-range filter lives in the filter row and the done header shows only the count.
- Kanban: drag & drop kept (invalid drops still revert with feedback); the "Move ›" text link is replaced by a card menu (⋮). Phones get snap-scrolling columns, a sticky row of status tabs and a "Move to …" bottom sheet.
- Admin → Settings redesigned: settings are grouped (General, Gitea, Agents & leases, Telegram, Email (SMTP), Data
  store, Data runner, Output gate, SFTP, Workflows) with a group navigation ("off" pill for disabled areas; a select on
  phones), one card per group with fields side by side, and a sticky save bar with an unsaved-changes counter,
  "Discard" and a warning when leaving with unsaved changes.
- Secret settings show only whether they are set and where from ("stored in DB" / "from .env" / "not set"), with
  "Replace" and "Reset to .env"; the page no longer shows the last four characters. The API still returns the masked
  value.
- Gitea "Test connection" moved into the Gitea card and never shows the token, even if Gitea echoes it in an error.
- Redesign Admin → Users & agents with a compact seven-column table grouped by owner, searchable type/role/kind filters, presence indicators, relative last-seen times, and phone cards.
- Add an editable detail panel (full-screen on phones) for account settings, confirmed API-token rotation/revocation, masked one-time token copying, and Gitea connection checks and credential editing. Keep advanced settings accessible and provide separate Add human and New agent actions.
- Planning (`/planning`): the cramped column-per-sprint board is now a sprint accordion list – one full-width card
  per sprint with expand/collapse (state kept per user), status pill, progress bar, done count and a primary
  "+ Task" button; the segmented filter (Active/Planned/Closed/All) and the "Show closed sprints" toggle narrow the
  list (closed sprints hidden by default; the default tab shows planned and released sprints, as in the mockup).
  Sprint actions (edit/release/close, plus reopen for closed sprints) and moving tasks between sprints (drag & drop
  and the "Move to sprint …" row menu) are unchanged; phone layout wraps the header and shows task rows as two-line
  items.
- Profile: redesigned page – page header with name/email and Language/Log out actions, sticky section nav (Display, Channels, Notifications, Quiet hours & mute, Delivery log), channel cards (Telegram link/unlink, Email address), and a delivery log.
- Profile: notification matrix grouped into Tasks / Workflows / Data with a Telegram/Email checkbox per event and an "All projects" checkbox in the header; one "Save all changes" button now saves display switches, matrix, bundling, quiet hours and mute together.
- Profile: phone layout – section nav becomes a select, the matrix keeps three columns without horizontal scroll, and the save button is sticky at the bottom.
- Kanban: fixed a crash when opening `/kanban` (a duplicated `view` keyword argument in the render call).
- Projects overview (`/projects`): list view as the default (always used on the phone) plus a card view via the
  List/Cards switch, remembered per user. Mine/All, search (key, name, repo), status and sort are in the URL and
  the project preselected in quick capture is marked as "Current". Task counts, agent count and last activity are
  real values from a few aggregated queries (no query per project).
- Redesign the Workflows page: underline tabs with counts (Templates, Runs, Schedules, Event triggers), a search box plus project filter, the shared table style with the status legend, an empty state, and phone-friendly scrolling tabs and stacked cards.
- Add "Start from a template" with three cards (Task chain, Scheduled report, Event-triggered); "Use template" opens the new-workflow form prefilled and creates a draft with valid example steps. No new engine features.
- `tbr sprint update` and MCP `update_sprint` edit a sprint's name, goal and dates (only the given fields; `--start none` / `--end none` or `"none"` clear a date, an empty goal clears it; the status stays unchanged).
- `tbr sprint reopen --no-restore` and MCP `reopen_sprint` reopen a closed sprint, with or without moving the tasks from the close back, and list restored and skipped tasks.
- UI redesign QA pass: phones (< 640 px) get 44 px touch targets for buttons, fields, segments, tabs, chips, switches and
  stand-alone links, and 16 px form text so iOS no longer zooms in on focus; task detail and inbox cards use the full
  width on phones; the inbox notices panel no longer leaves an empty block.
- Admin › Database, the login page, error pages and the new-task / new-project forms use the new page header, cards
  and controls; all admin pages share one tab navigation (Database, Users & agents, Settings, UI kit).
- Inbox, kanban and planning use the shared segmented control (contrast fixed); the header uses only design tokens;
  keyboard focus on form fields is always visible; the attachments field of the new-task form has a label.
- Screenshot and layout tests (`tests/test_ui_screens.py`, Playwright) for all main pages at 390, 768 and 1440 px plus
  overflow/tap-target checks at 360 px; baselines in `tests/screenshots/`, CI installs Chromium for them.
- Web UI: every page now sits on the grey page background (`--ui-bg`) in one shared container (max. 1400 px,
  24 px padding, 16 px on phones); the grey wrapper boxes and page-specific widths/paddings are gone, so page titles
  and content line up across all pages. Task details and the new-task form use the full container width (task
  details: main column plus a 380 px sidebar).
- Fix the Projects ownership notice so it appears only when no project is owned and the profile limits the view to owned projects.
- Align Projects toolbar controls, keep accessible select names, and preserve German capitalization in sort labels.
- Show empty grey progress tracks for projects without tasks and green/amber/navy segments for done, review and in-progress tasks.
- Inbox notices: "Mark all read" is a one-line extra-small secondary button (German "Alle gelesen"); the link next to it is "Settings" / "Einstellungen" so the header stays inside the panel.
- Backlog task titles are 14px/500. The 24px task-page heading style is limited to `h1.task-title`, so it no longer leaks into table cells.
- Planning: the segmented filter counts now cover all sprints of the selected project (closed ones included), so they agree with the subtitle total and the "All" tab.
- Planning: the sprint expand/collapse chevron is now a larger, clearer 32px button with higher contrast.
- Task detail: the Details / Discussion / History / Attachments tabs are left-aligned underline tabs (shared
  `ui-tabs`); the Properties sidebar is a 110 px / 1fr grid with label and value centred per row, the project shows
  its key (full name as tooltip), long sprint names wrap to at most two lines, and assignee/reviewer/activity use the
  shared actor chip (20 px avatar, 14 px/500 text, avatar colour by agent kind).
- Long actor names no longer overflow their card or cell: the actor chip and the agent labels on Kanban cards,
  Planning rows, Backlog and Inbox end in an ellipsis with the full name as tooltip; the same for the name in the
  Users & agents table and detail panel.
- Admin → Users & agents: agent kind badges are light tints with dark text (`ui.kind`, tokens `--ui-kind-*`);
  "Runs in" shows "Data: Cloud ok" as plain text and a zone badge only for local-only/sealed; the "N of M" count stays
  at the end of the toolbar row.
- Admin → Settings: "Reset to .env default" / "Reset to .env" are extra-small secondary buttons (visually hidden
  checkbox, check mark while queued); the reset behaviour on save is unchanged.
- Add a Default language option in the header and profile, mark personal versus default selections, and explain personal overrides in Admin settings.
- UI: phone and tablet review of all redesigned pages with screenshots at 390/768/1440 px.
- Kanban on phones opens on the column its status tabs mark ("In progress"), and the "Move to …" sheet is no longer
  hidden behind the bottom tab bar.
- Sticky save bars (Admin › Settings, workflow editor) stop above the bottom tab bar on phones.
- Tablet header no longer shows the phone-only quick-capture toggle and close button.
- Inbox question cards: the answer field takes the row again and "Send" stays inside the card.
- Admin section tabs scroll in one row on phones; tab counts keep their spacing; consistent "Admin" kicker; the
  settings "Reset to .env default" button sits next to its label; project card list without a double frame.
- New Playwright test `tests/test_ui_mobile_flows.py` for the phone sheets, kanban tabs and save bars.
- Quick capture: the target project is now its own select next to the title field (border, chevron, hover and focus
  ring; key in mono, "KEY – name" in the list, the name as tooltip). On phones the quick-capture sheet shows title,
  a labelled "Project" row and the create button below each other.
- Fix Inbox “Mark all read” to cover every unread notice in the selected sprint up to the page timestamp, preserving later arrivals and other users’ notices.
- Add individual, swipe and checkbox selection read actions with six-second undo; keep read notices in “Show older” and the profile delivery log.
- Keep the Inbox sprint filter within the mobile viewport and translate the new controls into German.
- UI: Backlog, Planning and Kanban share one task-title style (`.ui-task-title`, 14 px / 600, shown on `/admin/ui-kit`); Planning titles are now bold and Kanban card titles 14 px instead of 15 px.
- UI: automatic hyphenation is off app-wide (`hyphens: manual`); long words break only as a last resort.
- Task detail: Markdown headings in description, acceptance criteria and result are scaled down (h1/h2 16 px, h3–h6 14 px) so they no longer compete with the page title.
- Kanban: the filter row stays on one line from 1200 px (search shrinks, selects take the width of their text) and
  wraps cleanly below.
- Kanban cards: the stray "⋮ ˎ" characters are replaced by a 28 px "⋯" menu button ("Actions") at the right of the
  card footer; the menu now also offers "Open task" next to the allowed moves.
- Kanban on phones opens on the first column ("Draft") or the column given by `?col=`; filter changes keep the column
  in view.
- Planning: the expand/collapse button is a 32 px square with a 16 px chevron (▸ collapsed, ▾ expanded); on phones
  the sprint filter "Active / Planned / Closed / All" scrolls sideways in one line instead of wrapping.
- Backlog filter row: the search field now shrinks (`flex: 1 1 220px; min-width: 0`) and the filter selects keep
  their full label width (`width: auto`), so the last select ("Workflow-Tasks ausblenden") is no longer clipped at
  desktop width; the row only wraps below ~1200 px.
- Profile: the language choice is a compact segmented control ("Standard (EN) | EN | DE") left-aligned under the
  label instead of three buttons spread across the card.
- Admin → Settings on phones: a setting's label takes the full width and its "Reset to .env default" button sits on
  its own line below, so long labels no longer break mid-word; the save-bar buttons share the width and wrap their
  text instead of clipping.
- Task detail links box: the branch name is shown on one line with an ellipsis (full name as tooltip) and the ↗ icon
  inline at the end instead of wrapping onto its own line.
- Projects: the cards view shows the project status as a translated pill (Active/Paused/Archived, never the raw
  value), the progress bar spans the full card width (empty grey track for projects without tasks) and cards in a row
  are equal height with the footer at the bottom. The List/Cards toggle is right-aligned in the toolbar, flush with
  the "+ New project" button.
- Fix Inbox notice read and undo actions behind HTTPS proxies such as Tailscale; retain session CSRF and cross-origin protection.
- Show translated reload guidance when marking notices read or undoing fails, including network errors.
- Planning: tighter spacing as in the mockup – 10px between sprint cards, 14px 16px sprint header, one-line task rows of about 40px (8px padding, `--ui-divider` line); long titles are cut with an ellipsis and shown in full as a tooltip. On phones the rows are two-line items (key, assignee, status and menu, then the title).
- Inbox: reviews by reviewer agents appear again under "Needs your action" (filter "Reviews") as action cards – running or waiting reviews with reviewer, age and owner/admin overrule (approve & merge, request changes), and reviews the agent sent back (changes requested/conflict) until the assignee resumes. The "Reviews" count and the phone tab badge include them.
- Fixed: the profile switches "Show all projects" and "Show all agents" are saved as soon as they are toggled
  (before, they were only saved by the distant "Save all changes" button and a toggle was easily lost).
- Changed: with "Show all agents" off, admins see only their own agents in assignee/reviewer choices, agent filters
  and Admin → Users & agents – also when other owners' agents are public. The users page names the hidden agents
  with a "Show all agents" link; humans are always listed, and agents already assigned to a task stay visible.
- Projects overview (`/projects`): the card view is no longer remembered – the list stays the default for everyone.
  "Cards" now applies only while `?view=cards` is in the URL; the `tm_projects_view` cookie is no longer written,
  an old value is ignored and deleted, and the toolbar no longer emits a duplicate `view` parameter when switching
  back to the list. On the phone (< 640 px) the list is always shown, also with a `?view=cards` link.
- Add workflow overview actions to start published runs, rename display names, duplicate the latest version into a draft, and archive workflows with confirmation while preserving run history.
- Add parameter and budget dialogs, permission checks, English/German labels, and a mobile action sheet.
- Admin pages show one title line "Admin: Database", "Admin: Users & agents", "Admin: Settings" and "Admin: UI kit"
  (German "Admin: Datenbank" …) instead of a separate "Admin" label; browser tab titles follow.
- Inbox, Kanban, Planning, project detail and the workflow trigger/schedule pages use the shared page header
  (`ui.page_header`, `.ui-page-title`: 26 px / 700, 22 px on phones), so all page titles have the same size.
- README rewritten for the current state: new UI (header, Planning, Projects, Profile, Admin, phone tab bar),
  configuration table with the relevant environment variables, upgrade notes, Workflows with a minimal YAML example,
  Telegram and email notifications, result mails, agent kinds, roles and reviewer agents, and a separate data-zones
  section marked “Under development – untested”.
- Screenshot script: new desktop shots of Planning, Projects, Workflows (templates, editor, run) and the profile's
  notification settings; the demo seed imports and publishes two workflows from YAML, starts a run and enables the
  email channel with a placeholder SMTP server; token prefixes are masked; phone shots are no longer cut above the
  bottom tab bar.

- CI: GitHub Actions moved to Node 24 versions (`actions/checkout` v7, `astral-sh/setup-uv` v10.3.0, Docker actions v4/v7/v6), removing the Node 20 deprecation warning.
- Internal product-video package in `Produktvideo/` (excluded from the public export) — click path, instructions for a recording agent, AI speaker script in German and English, and a suggested way to produce the LinkedIn and X cut from the demo stack.
- Runner: an idle waiting session is no longer parked (ended) while processes the agent started are still running in
  it – ending it would kill them; the session stays open and one thread comment names the processes.
- Agent instructions (TASK.md, Hermes/OpenClaw skills): start long background jobs detached
  (`setsid nohup <cmd> > <log> 2>&1 < /dev/null &`), not with the agent CLI's own background feature.
- Docs: awareness plan (`docs/awareness-plan.md`) for a first public introduction — channels, posting times, who to tag or message on X, which Reddit communities, and sample posts in English and German.
- Add a four-week TaskBrigade promotion plan with channel priorities, posting times, X outreach targets, Reddit community guidance and sample posts.
- Internal `Marketing/` folder with a consolidated promotion plan; the public export excludes it.
- Docs: removed the promotion and awareness plans from `docs/` and the README; they are internal material now.
- Docs: concept for data zones and a "code instead of data" workflow (`docs/konzept-datenzonen.md`, German) – local analysis of confidential Excel/CSV data by a sandboxed runner, with cloud models seeing only schema, synthetic samples and released aggregates. Concept only, no code changes.
- Docs: concept for repeatable workflows (`docs/konzept-workflows.md`, German) – versioned task-chain templates, runs with output contracts and placeholders, "agent once, then runner" pattern, MCP tools, and a scheduler (systemd timer tick, workday/holiday rules, overlap and misfire policies). Cross-referenced with the data-zones concept. Concept only, no code changes.
- Agents have a **model location** (`cloud`, `local`; `none` reserved for the coming runner) next to the runtime:
  where content goes for inference. Claude Code and Codex are always `cloud`; “local model” needs a confirmation in
  *Admin → Users & agents*. Existing agents are migrated to `cloud`.
- New setting “local_only only for agents with a local model” (`privacy_enforce_model_location`): when on,
  assignment, project access, reviewers, review jobs and claim for `local_only` projects require runtime **and**
  model location `local`. On by default for new installations, off after the update of existing ones – check
  *Admin → Settings → Model location findings* first, then switch it on. A later release will always enforce it.
- API: actors carry `model_location` (`GET /actors`, admin create/patch); MCP `list_agents` shows it.
- Add the stage-1 workflow schema and SQLAlchemy models for templates, versions, steps, parameters, role bindings, runs and step attempts, with task links and the shared `work`/`profile`/`run`/`approval` task kinds.
- Record the actual Gitea merge commit when approving a task and return it separately from the submitted commit in task API responses.
- Local-only projects reject spreadsheet and archive task attachments (`.xlsx`, `.xls`, `.xlsm`, `.csv`, `.tsv`, `.ods`, `.zip`) in the web UI, API, CLI and MCP (`422 use_dataset_upload`). The message points at the coming data store. `tbr-admin data scan-attachments` lists existing ones in `local_only` projects and does not delete them.
- Workflow stage-1 schema is Alembic revision `0021` (revises model-location `0020`), so `upgrade head` has a single linear head.
- Add the transactional workflow run engine with pinned versions, ranked role assignment, ordered agent/approval/notify steps, rejection rework and pause/resume/cancel controls.
- Connect task completion, cancellation, aborts, questions and review decisions to workflow progress; preserve task and attempt history and prevent sprint assignment for workflow tasks.
- Add workflow notification events with existing delivery preferences and conservative masking, plus runtime guards against automatic zone releases and unsupported step types.
- Data class per task (`tasks.data_class`, migration 0022): `cloud_ok`, `local_only` or `sealed`, inheriting the
  project's privacy by default. Rules: stricter is always allowed, looser only for the project owner and admins
  (audit log), runner tasks (`profile`/`run`) are `sealed`, and after work started a class can only be made stricter.
  Assignment, reviewers, claim, Telegram texts and the spreadsheet attachment lock use the task's class.
- Field “Data class” in the task forms, `data_class` in the API, `tbr create|update --data-class`, MCP
  `create_task`/`update_task`; TASK.md names the task's data class.
- Data class badges on kanban cards, task detail, projects (with “mixed”) and a “Zone” column in
  *Admin → Users & agents*.
- Workflow steps can declare output contracts and `{{placeholders}}` (no Jinja). `tbr submit` accepts `--output` and `--output-json`; MCP `submit_task` accepts `outputs`; `.tm-submit.md` accepts an `## Outputs` JSON block. A workflow task's TASK.md lists the required outputs.
- Add workflow template/version reads, draft publication, run creation/history and run controls through REST, with project access checks and data-class masking.
- Add `tbr workflows`, `tbr workflow` commands and eight workflow MCP tools, including read-only previews with resolved parameters, first-step placeholders and eligible roles.
- Connect workflow activation to typed parameters and the placeholder resolver; prepare branchless approval review jobs without Git credentials or a pull request.
- Document the workflow API/CLI/MCP and provide German translations; reserve scheduler interfaces with explicit unavailable errors until the scheduler stage.
- Email as a second notification channel (outbound SMTP only – TaskBrigade never reads mail): SMTP settings and
  “Send test mail” under *Admin → Settings → Email*, address confirmation by one-time link in the profile, channel
  choice per event (default: email for review requests and completed sprints), email bundling (immediately or every
  15/60 minutes); mutes and quiet hours apply to both channels.
- Scheduler job `email_deliver` with retries and back-off (max. 8 attempts), throttle below “Maximum mails per hour”,
  one sending process (`email-sender.lock`) and a delivery log without contents; `local_only` mails carry task
  number, event and link only.
- The notification delivery log in *Admin → Settings* and the profile shows the channel. Concept:
  `docs/konzept-email.md`.
- Data store for confidential datasets outside Gitea (menu *Data*, off by default): web upload that encrypts while
  streaming (one key per dataset, wrapped with a separate data key from `tbr-admin init-data-key`), dataset list and
  detail with rights per project, retention with reminder and crypto-shredding (`tbr-admin data retention`), and an
  append-only, hash-chained data audit (`tbr-admin data audit verify`). Guide: `docs/data-store.md`.
- Humans get a data right (*Admin → Users & agents*); `sealed` tasks now need it. New settings `TBR_DATA_DIR`,
  `TBR_DATA_KEY_FILE`, `data_enabled`, `data_max_upload_mb`, `data_retention_days`; migration `0024`.
- Work results by email: agents file send requests (`tbr mail send|list|recipients`, MCP `send_email`, `list_emails`,
  `list_mail_recipients`, `POST /api/v1/tasks/<nr>/emails`); the server checks the project's recipient list
  (*Projects → <key> → Email recipients*, mode with/without approval; humans with a confirmed address need no
  approval), data class (`cloud_ok` only), attachment types, size (`mail_max_mb`) and the daily limit per project
  (`mail_daily_limit_per_project`), then sends after a human's approval – in the task's “Emails” section, the inbox
  group “Email approvals”, or together with the task approval (`--on-approve`). Agents never see SMTP credentials.
- Attachments come from task attachments, the task branch or the merge commit in Gitea, recorded with SHA-256 and
  re-checked when sending; `Reply-To` is the approving human (else the project owner); results are system comments
  and audit events (`mail_requested/approved/rejected/sent/failed`, without the text). Text and attachment references
  are cleared after 30 days. Migration `0025` (`outgoing_mails`, `project_mail_recipients`).
- TASK.md gets the section “Sending results by email” when the project has recipients.
- Add an optional native Gitea pre-receive guard rejecting spreadsheet/archive files in data-zone repositories, with `tbr-admin data gitea-hook install/uninstall/status/payload`, dry runs, and operations documentation.
- Data zones: opt-in installation routines (`TBR_DATA_ZONES=1` for `deploy/install.sh`/`deploy/update.sh`) create
  the system users/groups (`tbr-data`, `tbr-runner`, `tbr-job`), the encrypted store folder layout, the sandbox job
  user, the runner virtualenv and the polkit rule (`deploy/polkit/60-taskbrigade-runner.rules`); without the switch
  nothing changes on the system.
- `deploy/prereq.sh` gains the data-zone helpers and checks (systemd ≥ 249, polkit; optional OpenSSH for the SFTP
  inbox); `deploy/backup.sh` can archive the encrypted store with `TBR_BACKUP_DATA=1` (never the KEK).
- `docs/operations.md`: documented the data-zone install/migration path and the off switch; `tbr-admin config`
  knows `TBR_DATA_ZONES`, `TBR_DATA_SFTP`, `TBR_RUNNER_VENV` and `TBR_BACKUP_DATA`.
- Email: `tbr doctor` checks the email channel via `GET /agents/me/email-status` (SMTP probe with login, no credentials) and reports the last successful delivery; it warns when deliveries keep failing.
- Docs: operations guide gains email provider choice (EU provider, own domain, DKIM/SPF/DMARC, data processing agreement, app password, rate limit) and SMTP troubleshooting.
- Email: a failing SMTP probe (server unreachable, TLS or login refused) now answers `502 bad_gateway` with the plain SMTP reason instead of a hidden 500, so `tbr doctor` shows the real cause.
- Add workflow schedules, holiday calendars and slot history with MariaDB locking, overlap protection, bounded queues and misfire recovery.
- Add cron and workday rules with explicit Europe/Berlin DST handling, schedule editing and next-run previews, plus `tbr-admin workflows tick/schedules/calendar`.
- Web UI: new menu **Workflows** with the template list (steps, version, last runs), template page with versions,
  diff and “Start a run”, an editor with a sortable step list, parameters, placeholder insertion, live publish checks,
  save/publish/discard, a pipeline run history, a run list and a run page with chain, attempts, outputs and
  pause/resume/cancel. Guide: `docs/workflows-ui.md`.
- Kanban and Backlog hide workflow tasks by default; the new filter (hide / with / only / one template) is
  remembered, and a hint shows how many are hidden. Workflow tasks carry a `⟳ key #run` badge, the Inbox groups them
  under “Workflow steps”, and the task page shows the run's chain.
- API: task responses include `workflow` (`run_id`, `seq`, `key`, `name`) for tasks created by a workflow run.
- Workflow scheduler installation: systemd units `taskbrigade-workflows.{service,timer}` run `tbr-admin workflows tick` every minute; `deploy/install.sh` and `deploy/update.sh` install the units and stop/start the timer around the schema upgrade.
- `tbr doctor` and `tbr-admin db-status` warn when the last workflow tick is older than five minutes while workflows are enabled.
- Track workflow working time across claims and human waits; release timed-out leases and pause after final failure.
- Retry failed role-assigned agent steps with exponential backoff while preserving their task and discussion; retain separate attempts and assignee history.
- Add configurable wait/claim reminders, optional idle reassignment, long-wait warnings, recovery timing in run details, and notification preferences with privacy-safe run links.
- Preserve resume-from-step, optional skip and cancellation behavior during recovery, including clearing pending retries on cancellation.
- Data-zone runner (DZ 6): new agent kind `runner` (no model, model location `none`) and task kinds `run`/`profile`
  with run fields (`run.commit_sha`, `run.script_path`, `run.dataset`); runners only get runner tasks, LLM agents
  never. `taskbrigade-runner.service` (`tbr-admin runner join|serve|once|check|selftest|limits`) claims run tasks,
  decrypts the input into a job directory and starts the sandbox `taskbrigade-job@.service` (no network, only the job
  directory, memory/CPU/time limits from *Admin → Settings → Data runner*, stdout/stderr to files). Outputs and logs
  go encrypted into `TBR_DATA_DIR/quarantine` (`run_results`, migration `0028`); exit 0 → *For review*, exit code 3
  (schema mismatch) or any failure → *Aborted*. Only merge commits of approved tasks run; a static pre-check refuses
  network, subprocess and dynamic-code constructs first. Approving a run task waits for the output gate.
- TASK.md shows the script contract (input `TBR_INPUT`, output `TBR_OUTPUT_DIR/result.json` in `tbr-aggregate/1`,
  no network, aggregates only, exit code 3 = schema mismatch) for code tasks in projects with datasets.
- Runner virtualenv packages are pinned in `deploy/runner-requirements.txt`; `deploy/sandbox-test/run.sh` runs the
  sandbox isolation test in a throwaway systemd container. Guide: *docs/operations.md → Data-zone runner*.
- Native SFTP inbox (data zones, `TBR_DATA_SFTP=1`): OpenSSH `internal-sftp` with a per-user chroot
  (`/srv/taskbrigade-sftp/<user>/upload`), sshd drop-in `deploy/ssh/60-taskbrigade-sftp.conf` (SFTP only, no
  shell/forwarding, keys only), installed by `install.sh`/`update.sh` after `sshd -t` passes.
- `tbr-admin data sftp-user add|remove|list|key add|key remove|key list|provision|import` manages deliverers and
  their public keys (project per user) and imports finished uploads.
- systemd path unit `taskbrigade-data-import.path` + `taskbrigade-data-import.service` encrypt new SFTP uploads
  into the data store (`upload_channel=sftp`), write the `dataset_uploaded` audit entry, remove the plaintext and
  notify the project.
- Data store: local-model agents (`model_location=local`) may now read `local_only` datasets over `GET /api/v1/data/datasets/<key>/content`; cloud-model agents and `sealed` datasets stay blocked, and every read is written to the data audit.
- Profile CSV/TSV/XLSX uploads and SFTP imports in the local runner sandbox; generate deterministic synthetic samples and stable schema fingerprints.
- Add human profile release/rejection with independent approval for sealed uploads, column and sheet aliases, audited draft code handoffs, scoped profile reads and a dataset profile review UI.
- Workflows: token/cost usage tracking (`task_usage`) and per-run budgets – agents report usage via `tbr submit`/`tbr heartbeat --usage` or MCP `submit_task`/`report_waiting`; the runner reads it from agent output. At 80 % the owner is notified, at 100 % the run pauses with `budget_exceeded` and resuming requires a higher budget. Run detail shows "usage unknown" until the first report.
- Planning board (`/planning`, menu "Planning" for admins, orchestrators and project owners): all open sprints of a
  project side by side with a "No sprint" column; drag tasks between sprints, reorder a planned sprint (sets rank and
  draft prio like the sprint editor), release with preview, close with "move open tasks to", edit and create sprints
  in dialogs, "+ Task" quick add (one draft per pasted line) and "waits for …" hints for unfinished dependencies.
  One column at a time on phones.
- Output gate for data-zone runner results: automatic checks 1–9 (format, size, minimum group size k = 5 by default
  and per dataset, no identifier values, no raw rows, free-text, dominance and differencing warnings, rounding to two
  decimals); checks 4 and 5 run in a separate sandbox job against the input. Results that pass wait in a new release
  view (*Inbox → Data releases*, task and dataset pages) with full output, logs and script diff link; humans with the
  right release, reject or keep them in zone R (`kept_sealed`). Four-eyes rule on by default (per project
  switchable), every check and decision in the hash-chained data audit. Only released aggregates are readable by
  agents (`GET /api/v1/data/results/<id>`). New settings `gate_min_group_size`, `gate_max_rows`,
  `gate_dominance_pct`, `gate_dominance_hard`, `data_four_eyes`; migration `0032`.
- Add email delivery requests to workflow notify steps, with attachments from earlier file or result outputs and the existing recipient, approval, size, daily-limit and audit rules.
- Allow local-only result mail exclusively for output-gate released aggregates, with separate human mail approval; refuse sealed mail and recheck release status and hashes before delivery.
- Data zones: task chain across zones (DZ 9). New table `task_data_refs` (migration `0034`, existing run inputs and
  profile handoffs carried over) – tasks refer to datasets (input), released profiles (schema) and released results
  (output) instead of carrying their content; rule 3 (input dataset ⇒ `sealed`, profiles/results in zone C/L only
  when released) applies to references and class changes.
- Automatic follow-up drafts at each zone change: profile released → "Write analysis script" (C), script merged →
  "Run script" (R, merge commit, script path from the pull request, input dataset), result released → "Interpret
  result" (C). Run drafts can be completed under *Data → Run fields*; a run task is published only with commit,
  script path and dataset.
- Task page: chain view above the description (the workflow run's chain component) and a *Data* box with the
  references.
- Agents read released profiles/results only through an open task of their own that references them (before: any
  open task in the project). TASK.md gets the section "Approved data artifacts" with the released schema/sample or
  aggregate inline (`GET /api/v1/tasks/<nr>/artifacts`, audited).
- Execute workflow runner steps through sealed runner tasks and map mandatory manual gate steps to the existing output release, four-eyes and audit lifecycle.
- Validate consumed output classes at activation, require gates at publication, and keep results sealed when workflows have no release step.
- Validate dataset parameters, support runner input/script bindings, remind waiting gates, and stop cancelled or superseded jobs at the next heartbeat.
- Sprints: the release preview warns about tasks that would be released although they wait for a task that is not
  done and has no sprint, sits in a planned sprint or in a later one (`waiting` in `sprints.release_preview` and in
  the response of `POST …/sprints/{id}/release`; `tbr sprint release` and MCP `release_sprint` print it). The
  planning board's release dialog and the sprint page show the list with links and the count in the confirmation;
  releasing stays possible. The sprint page's task table has a *Waits for* column and its headline links to the
  planning board.
- Project page: the sprint table and the *New sprint* form are replaced by a compact list (name, status, progress)
  with *Open in planning*; sprints are created on the planning board or in the sprint editor.
- Add reviewed workflow script pins: write a script once, reuse it locally across runs with matching schemas, and preserve each run's executed merge commit.
- Request bounded repair tasks for schema changes, exit code 3 and failed gate checks, with released profile references, structural diffs and human recreate/manual repair controls.
- Fix workflow publication and editor checks to respect enabled data zones.
- Workflows: canonical YAML export/import of template versions (`GET /workflows/{project}/{key}/export`, `PUT …/import`, `tbr workflow export|import`, editor buttons). Import creates or replaces the unpublished draft only (never publishes) and runs the same publish checks as the editor; the export → import round trip is lossless – a non-default `retry_backoff_s` (including 0) and a step's `max_retries` are written whenever they differ from the editor/API defaults, so no value falls back to a default on import.
- Workflows: new MCP tool `draft_workflow(workflow, yaml_document)` – an orchestrator agent drafts a version, a human publishes it in the web UI.
- Dependency: `pyyaml` is now an explicit runtime dependency (was already pulled in by `uvicorn[standard]`).
- Docker: data zones for Compose installations. `TBR_DATA_ZONES=1` with `COMPOSE_PROFILES=data` starts `data-init`
  (folder layout and data key in their own volumes), the data-zone runner and a job launcher; every sandbox job runs
  in its own container with `network_mode: none`, a read-only root, no capabilities and only its own job directory.
  The runner never gets the Docker socket and reaches only the TaskBrigade API. Guide and design:
  *docs/docker.md → Data zones*.
- Docker: SFTP inbox as the profile `data-sftp` (OpenSSH `internal-sftp` with the native drop-in, `TBR_DATA_SFTP=1`,
  port `TBR_SFTP_PUBLISH`, default `127.0.0.1:2223`); deliverers are managed with
  `docker compose exec taskbrigade tbr-admin data sftp-user …` and imported by the app every 30 seconds.
- Runner: `tbr-admin runner join`, `check` and `selftest` work inside the runner container; the self-test also
  checks that a job sees only its own four directories.
- Docker: `deploy/docker/data-zones-test.sh` – isolation and end-to-end test of both profiles in a throwaway stack.
- New settings for Docker: `TBR_RUNNER_NETWORKS` (runner network may use the API and redeem join codes, not trusted
  like loopback), `TBR_SFTP_PROVISION=spool`/`TBR_SFTP_SPOOL`, `TBR_DATA_IMPORT=app`. The app container now mounts the
  data volumes and joins the internal runner network; both stay unused without the profiles.
- Workflow event triggers: an outbox starts a template once when a dataset becomes ready or another workflow succeeds or fails. Configure the trigger on the template; the scheduler tick delivers it.
- Add workflow step conditions (`when:`): a step is skipped (with a recorded reason) when its condition resolves empty or false, and unresolved placeholders fail closed.
- Add parallel step groups: consecutive steps sharing a group name activate together and complete the group only when every member is done.
- Editor fields for the condition and the group name, run detail shows the skipped reason, and publish checks reject conditions on decision steps and invalid parallel groups.
- Add optional per-dataset Laplace noise with documented sensitivity and human query review, plus release-count and cumulative epsilon budgets enforced transactionally across concurrent releases.
- Show budget usage and noise policies in dataset and output-approval views, preserve release policy audit records, and warn when noisy history prevents a differencing comparison.
- Docs: English overviews of the implemented data zones (`docs/data-zones.md`: zones, agent model, parts, walk-through, rights, admin settings, security model, deviations from the concept) and workflows (`docs/workflows.md`: terms, step types, starting runs, errors and budgets, zone boundaries, "agent once, then runner", operations, examples, scope, deviations); both linked from the README.
- Docs: the German concepts moved to `research/konzept-datenzonen.md` and `research/konzept-workflows.md` (design records, not in the public export); code comments and docs now point there or to the English guides. Stale "later stage" passages in the architecture, operations, data-store and workflow guides describe the built state.

## 0.13.4 – 2026-10-14

- `tbr-admin config` no longer fails with a traceback when the env file's directory (`/etc/taskbrigade`) is not readable for the current user; it asks for `sudo` instead.
- README: new screenshots including the mobile UI (`*-mobile.png`, generated by `deploy/release/screenshots.py`); intro mentions Gitea and SFTP storage.
- Docs: `llm-install-prompt.md`, quickstart, Docker guide and the other guides name the English UI labels (default language) instead of the German ones; `UI_LANGUAGE=de` is mentioned as the switch.
- Docs: the Docker quickstart and the install prompt recommend the public image `ghcr.io/taskbrigade/taskbrigade` (`TBR_IMAGE` + `TBR_PULL_POLICY=always`); local build stays as the alternative. `.env.example` shows the GHCR example.
- Install prompt: raw URL for agents in an empty directory; optional clone of the release tag.
- README: the Features list mentions the phone layout of the web UI; two typos in the intro fixed.
- README: the Development section notes that much of the code and documentation is written by AI coding agents and reviewed by the maintainer.
- Tests: the tmux test for stopping a returned task's window counts an exited but not yet reaped process (zombie) as stopped; it failed on slow CI runners.

## 0.13.3 – 2026-10-14 – first public release

TaskBrigade is a task manager for humans and AI coding agents: humans create tasks and review results, agents claim
tasks through the API, CLI or MCP, work on their own Git branch, ask questions or submit a pull request in Gitea.
This is the first public version. Changes since the internal 0.13.1:

- Package delivery: a server booted less than five minutes ago now builds a missing `tbr` wheel on the first request instead of answering 503 until the build back-off has passed (the back-off treated "never failed" as "failed at boot").
- Tests: the tmux test for stopping a returned task's window reports the old process's state and `runner.log` when it fails.
- Agent setup page: a fresh one-time join code is now also shown on its own in an orange box ("Temporary sign-in token:") with a copy button, above the `tbr join` command.
- README rewritten: positioning as agile project management for AI agent teams, supported-agents table, Taskmeister
  rename section removed.
- Docs: `docs/website/usp.md` is English source copy for a product page (one-liner, USPs, other features, sourced comparison, screenshot suggestions, limits). It does not add a website.
- Refresh the README with badges, navigation, GitHub alerts, a central documentation table and clearer setup and workflow descriptions.
- README: remove the introductory Taskmeister migration note and CI badge, feature the kanban screenshot, and add installation guidance for Claude Code and Codex CLI with the forthcoming installation prompt.
- `llm-install-prompt.md`: prompt for Claude Code or Codex that sets up TaskBrigade on a fresh Ubuntu machine with the
  user (machine check, native or Docker path, first agent and task), answers installation questions from the docs and
  adapts the setup; linked from the README's "Getting started".
- Web UI: the "New task" form follows the agreed layout – project and status in the first row, then title,
  description and acceptance criteria (Markdown with "Edit / Preview" tabs); below, "Planning & assignment"
  (priority, sprint, assignee, reviewer, due date) with folded sections "Model & execution" and "Task links" (they
  open when they hold values or a validation error), compact attachments and "Cancel" / "Create task". Priorities
  show a label ("3 – normal"), the spec-file checkbox survives a validation error, and every text is available in
  English and German.
- README: new section "Who it's for" with three setups – one workstation, an always-on machine with tasks from
  anywhere, and several machines with local LLMs and central orchestration.
- MCP server (`tbr mcp`): attachments from desktop apps – new tools `attach_files(task_nr, paths)` and
  `attach_text(task_nr, filename, content)` (UTF-8, max. 1 MB), and `create_task(…, attachments=[…])`. If attaching
  fails after creating, the task stays and the reply names the error. Upload through the existing
  `POST /tasks/{nr}/attachments`, shared with `tbr attach` (`cli/attachments.py`).
- `attach_files` reads only from allowed folders (work folder, `~/Downloads`, `~/Documents`, `~/Desktop`; configurable
  with `[mcp] attach_roots` and `max_attachment_mb` in `.taskbrigade.toml`), resolves symlinks and refuses hidden
  paths, key/secret files, folders, devices, empty or too large files and more than 20 files, each with a reason.
- Web UI on phones ("compact header"): small header with "+ Task" (quick capture sheet) and a menu with the full
  navigation; inbox shows only needed actions ("All done" when empty) and folded notices with counts; backlog as
  cards with visible search, folded filters (active count, reset) and sort fields; task details read first with
  "Edit", "Description / Thread" tabs, "More actions" and folded links/attachments/structure/history.
- Kanban on phones: one column with a "Status column" picker in portrait, a sideways-scrolling board (three columns
  at 844 px) in landscape, and a "Move" menu per card offering only transitions the server allows.
- Task details keep entered values after a failed save or comment.
- API: database changes are committed before the response is sent (`SessionDep`, `Depends(get_session,
  scope="function")`). With FastAPI ≥ 0.121 the commit of the request's session ran only after the response had gone
  out, so a client acting on it at once could read the old state – e.g. `tbr join` reported "tbr whoami fails: 401"
  for the token it had just received. Version 0.13.2.
- The installation prompt for coding agents is now `llm-install-prompt.md` (was `llm-installpromt.md`); README no
  longer calls it "being prepared".
- Public export: `tasks/` (attachments of TaskBrigade's own tasks) is excluded like the other internal material.
- Task detail page in the card layout of the new-task form: project and status (read-only) above title,
  description and acceptance criteria with "Edit / Preview" tabs; "Planning & assignment" with folded
  "Model & execution" and "Task links"; "Discard changes" / "Save changes"; discussion with folded history below.
  Right column: actions with a compact release check (what is still missing, updated live from the editor),
  follow-up and duplicate, "More actions", attachments, structure, project & links. German and English.
- Task edit errors keep sprint, reviewer, model and link input and open the folded section with the faulty field.
- Managers without the reassign right can save a running task again (the locked assignee is sent as a hidden field
  instead of being cleared).

## 0.13.2 – 2026-10-14

Internal release, not published; its changes are listed under 0.13.3.

## 0.13.1 – 2026-10-13

Internal release, not published.

- README and docs show screenshots of the inbox, kanban, backlog, task detail, sprint editor and user/agent admin
  (`docs/screenshots/`), taken from a Docker demo with invented data.
- `deploy/release/screenshots.py` regenerates them: throwaway Compose stack with the Gitea profile, demo data via
  `deploy/release/screenshot_seed.py`, Playwright in a container; nothing is installed on the host.
- Security: Telegram bot tokens no longer appear in the server log – httpx/httpcore request lines are logged only from WARNING up, and every log line (message, extra fields, tracebacks) has bot tokens redacted. After updating, rotate the bot token via @BotFather (`/revoke`) because older log entries still contain it.
- Duplicate tasks: „Duplizieren“ (duplicate) in the task detail view, `POST /api/v1/tasks/{nr}/duplicate`
  (`assign`, `title` optional), `tbr duplicate <NR> [--assign <agent>] [--title …]` and the MCP tool
  `duplicate_task`. The copy is a draft in the same project titled "(duplicate) …" (German UI: "(dupliziert) …",
  never stacked) with description, acceptance criteria, prio, due date, model tier/model, dependencies, parent task
  and the sprint unless it is closed; thread, questions, review, branch, pull request, lease, history, result and
  uploads stay behind. Unassigned by default; both tasks link to each other, the original gets a system comment and
  the action is audited (`task_duplicated`). Admins, orchestrators and the project owner only.
- Drafts: parent task and dependencies can now be edited in the task form (also `PATCH /tasks/{nr}` with
  `parent_task_nr`/`depends_on`).
- Migration 0016 adds `tasks.duplicate_of_id`. Version 0.10.0.
- `tbr prepare`/`tbr run`/MCP `start_task`: a reused worktree is now brought up to the current `origin/<base>` even
  when the task branch was never pushed (e.g. the first run only asked a question) – fast-forward without own commits,
  merge with own commits (conflicts as markers, as before). A worktree with uncommitted changes is left alone and
  TASK.md warns that the base is outdated (number of missing commits).
- The agent clone's local `<base>` branch now follows `origin/<base>` after each fetch (fast-forward only; diverged or
  dirty branches are left alone).
- Reviewer agents: new role `reviewer` for agents (*Admin → Benutzer & Agenten*). A reviewer agent reviews the tasks
  it is entered for as reviewer and works on its own tasks like a worker.
- Reviewer per task (optional, a human or a reviewer agent, never the assignee): task form, task detail (changeable
  until approval), sprint editor, batch capture (`Title | Agent | Prio | Reviewer`), API `reviewer` in
  `POST`/`PATCH /tasks` and `GET /tasks?reviewer=`, `tbr create/update --reviewer`, `tbr list --reviewer`, MCP
  `create_task`/`update_task`. Default reviewer per project (*Standard-Reviewer*, `PATCH /projects/{key}`
  `default_reviewer`) for new tasks. The project owner and admins can always approve, reject or return a conflict.
- Review jobs: a task in review with a reviewer agent is claimed by that agent (`POST /agents/me/claim` with
  `"reviews": true`, `GET /agents/me/reviews`, `tbr claim`/`tbr list` mark it) with its own lease (heartbeat,
  expiry returns it to the reviewer's queue; counts towards the parallel limit and "one review at a time"). The
  runner prepares a read-only checkout with `REVIEW.md` and starts a review session; Hermes/OpenClaw skills, the
  OpenClaw trigger and the Cowork skill know review jobs.
- `tbr review <NR> approve|reject|conflict [--comment …]` and `release --reason …`, MCP `review_task`, API
  `POST /tasks/{nr}/review/release`: same path as a human approval (merge, brain-changes, changelog.d,
  notifications). The thread shows „Abgenommen von <agent> (Reviewer-Agent)“; Telegram reports the agent's decision
  to the owner (event „Vom Reviewer-Agenten entschieden“), the inbox lists such tasks under „Review durch Agenten“.
- Migration 0017 (role `reviewer`, `tasks.reviewer_id`/`review_claimed_at`/`review_lease_expires_at`,
  `projects.default_reviewer_id`). Version 0.11.0.
- Agent owner and public agents: every agent has an owner (a human) and/or is public. New agents belong to the human
  who creates them and are private; migration 0018 gives existing agents their creator (else the bootstrap admin)
  as owner and makes them public, so nothing changes after the update. Owner and „Öffentlich“ (public) are columns
  and form fields under *Admin → Benutzer & Agenten* and in the agent wizard (admins only), API `owner`/`is_public`
  in `/admin/actors`.
- Assignment and reviewer choices (task form, task detail, duplicate, follow-up, sprint editor and batch, agent
  filters in backlog and kanban, project default reviewer) show only own and public agents; an already assigned
  agent stays visible. Admins: profile switch „Alle Agenten anzeigen“ (show all agents, off by default).
- Enforced on the server for UI, API, CLI and MCP: non-admins cannot assign another human's private agent as
  assignee or reviewer (`403 agent_not_visible`); agents (e.g. orchestrators) may assign public agents and those of
  their owner. `tbr agents`/MCP `list_agents`/`GET /actors` list only those, with owner or public; admins get all
  with `tbr agents --all` (`?all=true`).
- *Admin → Benutzer & Agenten* always lists all actors, with combinable filters Owner, Role and Kind in the URL and
  the number of matches.
- Conscious waiting for long background jobs: `tbr heartbeat <NR> --waiting "<reason>" [--for 2h] [--watch-file <log>]`
  (MCP `report_waiting`, `POST /tasks/{nr}/heartbeat` with `waiting`, `for_minutes`, `watch_file`, `done_waiting`).
  `--for` defaults to 2 h and is capped at 24 h; repeating the call extends it, `--done-waiting`, submit, ask, abort and
  release end it. Meanwhile the runner reports no stall and keeps the lease alive (also past `max_session_hours`).
- With `--watch-file` a change of the log (modified time or size) counts as activity; if it stays unchanged for
  `stall_minutes`, the runner writes „Background run is no longer writing: <path>“ with the file's last lines and
  notifies as for a stall. An expired waiting state falls back to normal stall detection.
- The task detail and the kanban card show „waiting for a background run: <reason> (until <time>)“.
- TASK.md instructions, the Hermes/OpenClaw skills, `docs/agent-skill.md` and `docs/cli-agents.md` describe it.
- Migration 0019 adds `tasks.waiting_reason`, `waiting_until`, `waiting_file`. Version 0.13.0.
- `tbr doctor` and `tbr cleanup` (also `--stale`) only judge tmux windows of their own agent when several runner
  agents share one tmux session: windows are tagged with the opening agent's work root, older untagged windows are
  matched by their directory. Foreign windows are no longer reported as stale or closed; doctor only counts them.
- Agent setup page: the install hint names `tbr` instead of `tm`; the Gitea user commands are shown for a native
  Gitea (`sudo -u git gitea --config … admin user …`), Docker Compose only as the variant for a containerised Gitea;
  without a Gitea admin token the page points to the one-click creation via *Admin → Settings*.
- Runner: an agent waiting for a long background job (`tbr heartbeat --waiting`) ends its session without `release`
  (new step 5 in TASK.md; an idle interactive agent is stopped after `park_idle_seconds`, default 120). The task stays
  in progress, the runner keeps its lease and starts a new session only when the waiting ends or the watched log stops
  changing – instead of a release/claim loop every 10 minutes that showed the task as defined. `tbr doctor` shows
  such a task as waiting.
- Version 0.13.1.

## 0.9.0 – 2026-10-10

Internal release, not published. Feature overview of the first public version (0.13.3) up to that point:

- **Web UI** in English (default) or German: inbox (answer questions, approve or reject inline), backlog with filters
  and sorting, kanban with drag & drop and a period filter for done tasks, task detail with Markdown preview, thread,
  history and attachments, quick capture, follow-up tasks from open points. Owner filter and "show all projects"
  switch per human.
- **Tasks and projects:** project-scoped task numbers, priorities, due dates, dependencies and subtasks, drafts,
  optimistic locking (`If-Match`). Projects create or link a Gitea repository, have an owner (role `member` for
  humans who manage their own projects), sync collaborators and keep project knowledge in `brain.md`.
- **Sprints:** optional level between project and task with a sprint editor, batch capture, release preview and
  close dialog; also from the CLI (`tbr sprint …`) and MCP.
- **Review workflow:** state machine with questions, aborts, changes requested and approval; on approval
  TaskBrigade merges the pull request and applies `brain-changes/<TASK-NR>.md` and `changelog.d/<TASK-NR>.md`, so
  parallel tasks do not conflict on `brain.md` and `CHANGELOG.md`. Optional "one review at a time per agent".
- **Agents:** API tokens shown once and stored hashed, Gitea and SFTP credentials stored encrypted, parallel-task
  limit, model tier and concrete model per task with per-agent allow lists. Onboarding with a one-time join code:
  `curl <server>/install.sh | sh`, `tbr join … --setup <kind>`, `tbr doctor`.
- **CLI `tbr`** for agents and orchestrators, `tbr-admin` for operators (`tbr-admin config get|set|list`), MCP server
  `tbr mcp` for desktop apps. `tbr self-update` follows the server's version and build.
- **Integrations:** runner for Claude Code, Codex and OpenCode (tmux window per task, heartbeats, start check, stall
  detection, automatic wrap-up); Claude Desktop and Codex Desktop via MCP; Hermes and OpenClaw with skills and
  check scripts, including an OpenClaw orchestrator that plans tasks from Telegram.
- **Notifications** through an own Telegram bot: questions, reviews, conflicts, stalled agents; approve, reject and
  answer from the chat; quiet hours and muting (also `tbr notify` and MCP `notify`).
- **SFTP storage** for agent results (`tbr sftp put|get|ls`, MCP `sftp_put`/`sftp_ls`) with pinned host key.
- **Deployment** without Docker (install/update/backup scripts, systemd units, daily backups) or with Docker Compose
  (app + MariaDB, amd64 and arm64, optional Gitea profile and Tailscale sidecar, backup/restore/update scripts).
  Security model and trust boundaries: `SECURITY.md`.
- **CI:** GitHub Actions workflow (lint, tests against MariaDB, multi-arch image, Compose smoke test); tags `v*`
  publish `ghcr.io/taskbrigade/taskbrigade`.
- Licensed under Apache-2.0; bundled third-party assets are listed in `THIRD_PARTY_NOTICES.md`.
