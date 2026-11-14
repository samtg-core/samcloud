-- samcloud registry schema
-- SQLite with WAL mode for concurrent readers

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ===== Devices =====
-- A device is anything on the network: phone, server, laptop, IoT
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,           -- e.g. 'phone-1', 'mediahost'
    name        TEXT NOT NULL,             -- display name
    hostname    TEXT,                      -- compat alias for name
    owner_id    INTEGER REFERENCES users(id), -- who owns this device
    type        TEXT NOT NULL DEFAULT 'device', -- freeform: device, server, phone, bot, etc.
    context     TEXT NOT NULL DEFAULT 'samcloud', -- logical group: samcloud, tunnel (reached by reverse tunnel; once `workcloud`)

    -- Inbound: how this device reaches the plane host
    inbound_method  TEXT,                  -- 'ssh-tunnel', 'lan', 'browser-only', null
    inbound_endpoint TEXT,                 -- what they connect to (e.g. 'tunnel.example.org:2222')

    -- Outbound: how the plane host reaches this device
    outbound_method TEXT,                  -- 'direct-ssh', 'reverse-tunnel', 'http', null
    outbound_ip     TEXT,                  -- LAN IP for direct access (null for NAT)
    outbound_ssh_user TEXT,                -- SSH user for the host to connect
    outbound_ssh_port INTEGER DEFAULT 22,
    tunnel_port     INTEGER,              -- reverse tunnel port on the host (e.g. 10003)
    lifeline_port   INTEGER,              -- Windows lifeline tunnel port
    transport_owner TEXT,                 -- device whose reverse tunnel this device borrows (shared-transport secondary user); null = owns its own transport

    -- Identity
    ssh_pubkey      TEXT,                  -- SSH public key (stored on activation)
    ssh_user_warning TEXT,                 -- non-null if device-reported username failed SSH-safe validation
    host_pubkey     TEXT,                  -- the key this device's sshd presents; the gateway pins hops against it
    host_pubkey_recorded_at TEXT,          -- when host_pubkey was pinned

    -- Operational
    startup_method  TEXT,                  -- 'systemd', 'launchd', 'scheduled-task', 'docker'
    recovery_method TEXT,                  -- 'event-driven', 'polling', 'restart-policy', 'none'
    monitor_port    INTEGER,              -- GPU/health monitor port

    -- State
    enrolled    INTEGER NOT NULL DEFAULT 0,
    enrolled_at TEXT,
    status      TEXT NOT NULL DEFAULT 'unknown',  -- online, offline, degraded, unknown, pending
    last_seen   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Services =====
CREATE TABLE IF NOT EXISTS services (
    id              TEXT PRIMARY KEY,       -- e.g. 'gpu-1/model-gateway'
    name            TEXT NOT NULL,          -- e.g. 'model-gateway'
    device_id       TEXT NOT NULL REFERENCES devices(id),
    port            INTEGER,                -- port on the device (null when pending)
    slice_port      INTEGER,               -- forwarded port on the host (null if LAN direct)
    protocol        TEXT NOT NULL DEFAULT 'http',  -- http, https, ws, tcp
    health_endpoint TEXT,                  -- e.g. '/health'
    subdomain       TEXT,                  -- e.g. 'mdl' (null if internal-only)
    domain          TEXT,                  -- set by the registry (SAMCLOUD_DOMAIN / PUBLIC_URL)
    external_url    TEXT,                  -- full URL e.g. 'https://mdl.example.org'
    status          TEXT NOT NULL DEFAULT 'unknown',  -- active, degraded, stopped, unknown
    version         TEXT,
    capabilities    TEXT,                  -- JSON array
    description     TEXT,                  -- human/agent-readable description
    docs_endpoint   TEXT,                  -- e.g. '/service-docs' (for service discovery)
    last_health     TEXT,                  -- ISO timestamp
    last_health_status INTEGER,            -- HTTP status code
    created_by      TEXT,                  -- user/agent who created this service (owner_id)
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(device_id, port)
);

-- ===== Ports =====
-- Enforces uniqueness across the entire system
CREATE TABLE IF NOT EXISTS ports (
    port        INTEGER NOT NULL,
    type        TEXT NOT NULL,              -- 'tunnel', 'lifeline', 'forward', 'local', 'monitor'
    owner_id    TEXT NOT NULL,              -- device_id or service_id
    allocated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (port, type)
);

-- ===== Routes =====
-- Caddy reverse proxy routes
CREATE TABLE IF NOT EXISTS routes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subdomain   TEXT NOT NULL,
    domain      TEXT NOT NULL,
    target      TEXT NOT NULL,             -- e.g. 'localhost:18008'
    service_id  TEXT REFERENCES services(id),
    caddy_extra TEXT,                      -- additional Caddy directives (JSON)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(subdomain, domain)
);

-- ===== Users =====
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL DEFAULT 'user',  -- admin, user, agent
    scopes      TEXT,                      -- JSON array of scopes
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT,                      -- stamped on inbox reads (wake rail, doc #17)
    presence_until TEXT                    -- live-session presence; wake rail defers while fresh (doc #17)
);

-- ===== Tokens =====
-- Device tokens, service tokens, agent tokens
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,           -- token ID (not the token itself)
    token_hash  TEXT NOT NULL,             -- SHA-256 hash of token
    type        TEXT NOT NULL,             -- 'device', 'service', 'agent', 'enrollment'
    owner_id    TEXT NOT NULL,             -- device_id, service_id, or user_id
    scopes      TEXT,                      -- JSON array
    expires_at  TEXT,                      -- ISO timestamp (null = never)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    last_used   TEXT
);

-- ===== Tickets =====
CREATE TABLE IF NOT EXISTS tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,             -- 'user:alice', 'agent:watchdog', 'service:nerfstudio'
    type        TEXT NOT NULL DEFAULT 'bug', -- bug, feature, question, incident
    priority    TEXT NOT NULL DEFAULT 'medium', -- low, medium, high, critical
    status      TEXT NOT NULL DEFAULT 'open',   -- open, triaged, in_progress, review, resolved, closed
    summary     TEXT NOT NULL,
    body        TEXT,
    service_id  TEXT REFERENCES services(id),
    device_id   TEXT REFERENCES devices(id),
    assignee    TEXT,                      -- user or agent ID
    group_scope TEXT,                      -- #330: explicit group visibility (union w/ assignee/device); null = existing rules only
    resolved_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    , thread_id   INTEGER                    -- v19: the universal thread this ticket belongs to (doc 239)
);

CREATE TABLE IF NOT EXISTS ticket_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    session_fp  TEXT,
    home_thread_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Ticket watchers =====
-- Per-ticket subscriptions. Identity strings match tickets.source /
-- ticket_comments.author (i.e. AuthContext.identity()): username for
-- user/agent tokens, owner_id (device or service path) otherwise.
-- Filer + assignee + commenters are auto-subscribed; DELETE to opt out.
CREATE TABLE IF NOT EXISTS ticket_watchers (
    ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticket_id, identity)
);

-- ===== Notifications (durable inbox, RFC #37) =====
-- One row per (watcher, ticket event), written at emit time so delivery
-- survives restarts and isn't subject to the in-memory event-history cap.
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL,              -- identity string (see ticket_watchers)
    ticket_id   INTEGER REFERENCES tickets(id),
    event_type  TEXT NOT NULL,              -- ticket.created / ticket.updated / ticket.commented
    actor       TEXT NOT NULL,              -- identity that triggered the event
    payload     TEXT,                       -- JSON summary (body snippet, status, etc.)
    read        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    , thread_id   INTEGER                    -- v19: the universal thread this row belongs to
);

-- ===== Docs plane (RFC #105, parent principle #86) =====
-- A registry surface that is a *sibling to tickets* — versioned,
-- contributor-directed knowledge / understanding / planning memory for
-- on-cloud agents. Tickets are append-only *event* memory; docs are
-- *understanding* memory that evolves in place via immutable revisions.
--
-- Provenance is the substrate of agent continuity (#86): every doc's owner
-- (docs.source) and every revision's author are bearer-derived and never
-- trusted from the body, using the same identity namespace as tickets so an
-- identity's contributions are *contiguous* across surfaces. Revisions are
-- append-only — an edit writes a new row, never rewrites an old one — so a
-- contribution can't be silently overwritten or forged (*safe*).
CREATE TABLE IF NOT EXISTS docs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT UNIQUE NOT NULL,            -- stable reference, e.g. 'bootstrap-compiler'
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'doc',     -- doc | rfc | runbook | note
    status       TEXT NOT NULL DEFAULT 'active',  -- draft | active | superseded | archived
    group_scope  TEXT,                            -- null = shared commons (all authenticated agents); else group id → scoped visibility
    source       TEXT NOT NULL,                   -- creator/owner identity (bearer-derived, immutable)
    supersedes   INTEGER REFERENCES docs(id),     -- optional: this doc replaces an older one (supersession chain)
    parent_doc_id INTEGER REFERENCES docs(id),    -- doc hierarchy (#384-era structure directive): overview parents point to fiddly sub-docs
    current_rev  INTEGER NOT NULL DEFAULT 1,      -- head revision number
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
    , thread_id    INTEGER                    -- v19: the universal thread this doc belongs to
);

-- One immutable row per edit. The head is docs.current_rev; older revs are
-- kept verbatim for history/diff. title is snapshotted per-rev so a rename is
-- itself a tracked edit.
CREATE TABLE IF NOT EXISTS doc_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES docs(id),
    rev         INTEGER NOT NULL,                 -- monotonic per doc: 1, 2, 3…
    title       TEXT NOT NULL,                    -- title at this revision
    body        TEXT NOT NULL,                    -- markdown snapshot (immutable)
    author      TEXT NOT NULL,                    -- bearer-derived, immutable
    note        TEXT,                             -- edit summary ("why")
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(doc_id, rev)
);

-- Threads a doc back to the tickets/services/devices/docs it concerns — so a
-- design doc and the tickets that drove it are discoverable from either side.
CREATE TABLE IF NOT EXISTS doc_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES docs(id),
    target_type TEXT NOT NULL,                    -- ticket | service | device | doc
    target_id   TEXT NOT NULL,
    created_by  TEXT NOT NULL,                    -- who added the link (bearer-derived)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(doc_id, target_type, target_id)
);

-- Per-doc subscriptions (mirrors ticket_watchers). Creator + contributors are
-- auto-subscribed; DELETE to opt out. Fan-out reuses the #37 notifications inbox.
CREATE TABLE IF NOT EXISTS doc_watchers (
    doc_id      INTEGER NOT NULL REFERENCES docs(id),
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (doc_id, identity)
);

-- Saved views (#203 v2, doc #34): per-user filter definitions rendered as
-- the inbox's section stack. Views are DATA — the four default sections are
-- seeded as editable rows on first list, never baked into code. Personal
-- (owner = bearer identity) in v1; group sharing later.
CREATE TABLE IF NOT EXISTS views (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    owner            TEXT NOT NULL,
    name             TEXT NOT NULL,
    entity           TEXT NOT NULL,      -- tickets | tasks | notifications
    filters          TEXT,               -- JSON object, whitelisted keys
    sort             TEXT,               -- "field" or "field:desc" (whitelisted)
    group_by         TEXT,               -- whitelisted per entity
    limit_per_group  INTEGER,
    position         INTEGER,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The inbox feed (#1082): one card per ticket, task or doc, with a person's
-- read and star state kept per card. read_at is when they last read it; NULL
-- means "never marked", so the epoch decides. The epoch is when their feed
-- began, or their last "mark all read": activity before it counts as read.
CREATE TABLE IF NOT EXISTS inbox_marks (
    identity    TEXT NOT NULL,
    kind        TEXT NOT NULL,      -- ticket | task | doc
    object_id   INTEGER NOT NULL,
    read_at     TEXT,
    starred     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, kind, object_id)
);

CREATE TABLE IF NOT EXISTS inbox_state (
    identity    TEXT PRIMARY KEY,
    epoch       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Task comments (#203 riff 2026-07-25: tasks get the same comment/metadata
-- treatment as tickets/docs). Author is bearer-derived, immutable. Fan-out
-- stays quiet-plane-consistent: assignee + project watchers only.
CREATE TABLE IF NOT EXISTS task_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id),
    author      TEXT NOT NULL,
    body        TEXT NOT NULL,
    session_fp  TEXT,
    home_thread_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Project watchers (#232): OPT-IN fan-out of task events within a project.
-- The task plane stays quiet by default (only task.assigned notifies);
-- watching a project is an explicit choice — what a producer/orchestrator
-- surface needs to hold the reins without per-task subscriptions.
CREATE TABLE IF NOT EXISTS project_watchers (
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    identity    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, identity)
);

-- The discussion layer — commentary DISTINCT from the versioned body (a twin of
-- ticket_comments). Edits to a doc are revisions; talking about a doc is comments.
CREATE TABLE IF NOT EXISTS doc_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES docs(id),
    author      TEXT NOT NULL,                   -- bearer-derived, immutable
    body        TEXT NOT NULL,
    session_fp  TEXT,
    home_thread_id INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Task plane (doc #13, ticket #148) =====
-- The third primitive of the trichotomy:
--   doc    → canonical record of ways of working / design / notes (not necessarily actionable)
--   ticket → an issue or request raised, usually needing another's attention
--   task   → an actionable item with specific direction, for self or other agent(s)
-- Tasks nest from day one (parent_task_id) and group under projects; project
-- access rides group scopes exactly like docs.group_scope. Progress/rollups are
-- computed on read, never stored.
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT UNIQUE NOT NULL,             -- stable reference, e.g. 'samcloud'
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- planning | active | paused | done | archived
    group_scope TEXT,                             -- null = shared commons; else group id → scoped visibility
    source      TEXT NOT NULL,                    -- owner identity (bearer-derived, immutable)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    parent_task_id  INTEGER REFERENCES tasks(id), -- nesting (a subtask is just a task with a parent)
    title           TEXT NOT NULL,
    detail          TEXT,
    status          TEXT NOT NULL DEFAULT 'todo', -- todo | in_progress | blocked | done | cancelled
    priority        TEXT,                         -- low | medium | high (optional)
    estimate_min    INTEGER,                      -- effort estimate (minutes)
    actual_min      INTEGER,                      -- optional actuals
    assignee        TEXT,                         -- username (validated) — self or another agent
    due             TEXT,                         -- ISO date
    position        INTEGER,                      -- manual ordering among siblings
    source_ref      TEXT,                         -- provenance: tape-capture span | ticket:# | doc:#
    delegated_ticket_id INTEGER REFERENCES tickets(id), -- set by /delegate — the ONE bridge to tickets
    source          TEXT NOT NULL,                -- creator identity (bearer-derived, immutable)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT,
    -- Plane-native scheduling (doc 161 §5 — the timer half of #303): at
    -- scheduled_at the registry surfaces the task in the assignee's inbox as
    -- a schedule.fired notification; recurrence re-arms it per calendar.
    scheduled_at    TEXT,                         -- UTC ISO; NULL = not scheduled
    recurrence      TEXT,                         -- daily | weekdays | weekly | monthly
    last_fired_at   TEXT
    , thread_id    INTEGER                    -- v19: the universal thread this task belongs to
);

-- ===== Groups =====
-- Groups define scoped views of services
CREATE TABLE IF NOT EXISTS groups (
    id          TEXT PRIMARY KEY,           -- e.g. 'home', 'work', 'admin'
    name        TEXT NOT NULL,             -- display name
    description TEXT,
    route_path  TEXT,                      -- e.g. '/home', '/work' (console route)
    doc_id      INTEGER,                   -- the working doc: the group's knowledge base entry (scope v1)
    created_by  TEXT REFERENCES users(id), -- the user id that made it (its first owner)
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which devices belong to which groups
CREATE TABLE IF NOT EXISTS group_devices (
    group_id    TEXT NOT NULL REFERENCES groups(id),
    device_id   TEXT NOT NULL REFERENCES devices(id),
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, device_id)
);

-- Which services are in which groups
CREATE TABLE IF NOT EXISTS group_services (
    group_id    TEXT NOT NULL REFERENCES groups(id),
    service_id  TEXT NOT NULL REFERENCES services(id),
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, service_id)
);

-- Which users belong to which groups and with what role
CREATE TABLE IF NOT EXISTS group_members (
    group_id    TEXT NOT NULL REFERENCES groups(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    role        TEXT NOT NULL DEFAULT 'viewer',  -- viewer, editor, owner (scope v1, doc 261)
    added_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, user_id)
);

-- Two groups the plane always has (scope v1, doc 261). `core` is the inner
-- city — administration, the gateway — with the least access: an orphan row
-- (a creator with no group at all) lands there, seen by few. The admin's own
-- unnamed writes — the plane's records, boards, briefs — go to
-- `samcloud-contributors`, the shared scope for work on samcloud itself.
INSERT OR IGNORE INTO groups (id, name, description) VALUES ('core', 'core', 'The inner city — administration and the gateway. Least access.');
INSERT OR IGNORE INTO groups (id, name, description) VALUES ('samcloud-contributors', 'samcloud contributors', 'Work on samcloud itself — its records, boards, briefs and, later, its PRs. The admin writes here by default; seats that contribute are editors.');

-- ===== Audit Log =====
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT NOT NULL,             -- 'user:alice', 'agent:turbo', 'system:watchdog'
    action      TEXT NOT NULL,             -- 'device.enroll', 'service.register', 'port.allocate'
    target      TEXT,                      -- what was acted on
    detail      TEXT,                      -- JSON detail
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Resources =====
-- Compute resources: GPUs, CPU pools, memory, accelerators
CREATE TABLE IF NOT EXISTS resources (
    id              TEXT PRIMARY KEY,       -- e.g. 'sim-gpu-1/gpu-0'
    device_id       TEXT NOT NULL REFERENCES devices(id),
    name            TEXT NOT NULL,          -- e.g. 'gpu-0'
    type            TEXT NOT NULL,          -- gpu, cpu, memory, storage, accelerator
    provider        TEXT NOT NULL,          -- nvidia, apple, amd, intel
    model           TEXT,                   -- e.g. 'RTX 3090', 'M2 Pro'
    allocation_mode TEXT NOT NULL DEFAULT 'shared', -- shared, exclusive
    expiry_policy   TEXT NOT NULL DEFAULT 'free',   -- free: an expired holder stops charging at once; hold: it charges until settled (DELETE by holder/admin)

    -- Static capacity (JSON)
    specs           TEXT,                   -- JSON: vram_mb, cuda_cores, unified_memory_mb, etc.

    -- Live utilisation (JSON, updated via stats push)
    utilisation     TEXT,                   -- JSON: memory_used_mb, compute_pct, temperature_c, etc.

    -- State
    status          TEXT NOT NULL DEFAULT 'unknown',
    last_stats      TEXT,                  -- ISO timestamp
    created_by      TEXT,                  -- user/agent who registered
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ===== Leases =====
-- Resource allocation: request/grant/release
CREATE TABLE IF NOT EXISTS leases (
    id              TEXT PRIMARY KEY,       -- e.g. 'lease_abc123'
    resource_id     TEXT NOT NULL REFERENCES resources(id),
    service_id      TEXT REFERENCES services(id),
    agent_id        TEXT,                  -- user/agent requesting

    memory_mb       INTEGER,               -- amount of memory leased
    exclusive       INTEGER NOT NULL DEFAULT 0, -- 1 = exclusive (passthrough)
    priority        TEXT NOT NULL DEFAULT 'normal', -- low, normal, high, critical

    status          TEXT NOT NULL DEFAULT 'active', -- active, queued, expired, released, cancelled (a waiter that left)
    queue_position  INTEGER,
    granted_at      TEXT,
    expires_at      TEXT,
    released_at     TEXT,
    settled_at      TEXT,                       -- an expired holder on a `hold` resource, settled by its holder or an admin
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Explicit, idempotent continuation of a shared-memory allocation. The linked
-- replacement and predecessor retirement commit together; never two byte claims.
CREATE TABLE IF NOT EXISTS lease_rollovers (
    previous_id TEXT PRIMARY KEY REFERENCES leases(id) ON DELETE CASCADE,
    next_id TEXT NOT NULL UNIQUE REFERENCES leases(id) ON DELETE CASCADE,
    key_hash TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL CHECK(ttl_seconds > 0),
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- One durable operation spanning ordinary per-resource queues. No TTL on the
-- group: member expiry does not prove that its provider stopped using memory.
CREATE TABLE IF NOT EXISTS lease_groups (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    intent_sha256 TEXT NOT NULL,
    intent_json TEXT NOT NULL,
    phase TEXT NOT NULL CHECK(phase IN
        ('pending','acquiring','admitted','serving','releasing','uncertain','released','cancelled')),
    generation INTEGER NOT NULL DEFAULT 1 CHECK(generation > 0),
    release_requested INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    serving_evidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lease_groups_one_acquiring
    ON lease_groups((1))
    WHERE phase IN ('acquiring','admitted','serving','releasing','uncertain');

CREATE TABLE IF NOT EXISTS lease_group_members (
    group_id TEXT NOT NULL REFERENCES lease_groups(id),
    ordinal INTEGER NOT NULL,
    resource_id TEXT NOT NULL,
    -- Keep the binding even if an administrator deletes a resource/lease. A
    -- missing receipt becomes uncertain; it must never become a fresh request.
    lease_id TEXT UNIQUE,
    cleanup_evidence TEXT,
    release_receipt TEXT,
    PRIMARY KEY(group_id, ordinal),
    UNIQUE(group_id, resource_id)
);
CREATE TABLE IF NOT EXISTS lease_group_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT NOT NULL REFERENCES lease_groups(id),
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    phase TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lease_group_events_group
    ON lease_group_events(group_id, sequence);

-- ===== Indexes =====
CREATE INDEX IF NOT EXISTS idx_resources_device ON resources(device_id);
CREATE INDEX IF NOT EXISTS idx_resources_type ON resources(type);
CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
CREATE INDEX IF NOT EXISTS idx_leases_resource ON leases(resource_id);
CREATE INDEX IF NOT EXISTS idx_leases_service ON leases(service_id);
CREATE INDEX IF NOT EXISTS idx_leases_status ON leases(status);
CREATE INDEX IF NOT EXISTS idx_services_device ON services(device_id);
CREATE INDEX IF NOT EXISTS idx_services_status ON services(status);
CREATE INDEX IF NOT EXISTS idx_ports_owner ON ports(owner_id);
CREATE INDEX IF NOT EXISTS idx_routes_service ON routes(service_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_ticket_watchers_identity ON ticket_watchers(identity);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient, read);
-- The conversation-scoped read (`?for_conversation=`) joins notifications to
-- bindings on thread_id and runs six correlated EXISTS per call; with the feed
-- now woken by the stream rather than a 2s poll it runs on arrival instead of
-- on a clock, which makes it latency rather than load (0.12.20).
CREATE INDEX IF NOT EXISTS idx_notifications_thread ON notifications(thread_id);
CREATE INDEX IF NOT EXISTS idx_tickets_thread ON tickets(thread_id);
CREATE INDEX IF NOT EXISTS idx_docs_thread ON docs(thread_id);
CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_id);
CREATE INDEX IF NOT EXISTS idx_tickets_service ON tickets(service_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_tokens_owner ON tokens(owner_id);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_group_devices_device ON group_devices(device_id);
CREATE INDEX IF NOT EXISTS idx_group_services_service ON group_services(service_id);
CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id);
CREATE INDEX IF NOT EXISTS idx_docs_status ON docs(status);
CREATE INDEX IF NOT EXISTS idx_docs_slug ON docs(slug);
CREATE INDEX IF NOT EXISTS idx_doc_revisions_doc ON doc_revisions(doc_id, rev);
CREATE INDEX IF NOT EXISTS idx_doc_links_doc ON doc_links(doc_id);
CREATE INDEX IF NOT EXISTS idx_doc_links_target ON doc_links(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_doc_watchers_identity ON doc_watchers(identity);
CREATE INDEX IF NOT EXISTS idx_doc_comments_doc ON doc_comments(doc_id);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_slug ON projects(slug);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ===== Wake-rail registrations (RFC doc #17; tape 2026-07-11, doc #22) =====
-- One row per (user, host): the registry's live view of the wake-rail fleet.
-- The listener upserts on every SSE connect, reporting its versions; the
-- registry answers with the currently-served versions so a stale listener
-- can self-update. This is the "here's my wake-up command" registration.
CREATE TABLE IF NOT EXISTS wake_registrations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    host              TEXT NOT NULL DEFAULT '',   -- hostname the listener runs on
    wake_cmd          TEXT,                        -- the registered wake command
    listener_version  TEXT,
    handler_version   TEXT,                        -- null when WAKE_CMD is a custom harness
    api_env           TEXT,                        -- API base the listener points at (prod/staging)
    notif_hook_version TEXT,                       -- interactive-surface hooks (#198); null = dark
    stop_hook_version  TEXT,
    registered_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_connect_at   TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at          TEXT,                        -- retired (#897); NULL = live. Cleared by a real register, so a returning seat comes back
    UNIQUE(user_id, host)
);
CREATE INDEX IF NOT EXISTS idx_wake_reg_user ON wake_registrations(user_id);

-- ===== Thread claims (doc #17 / #199 — the full form) =====
-- A claim = "a warm surface of this identity is currently engaged with this
-- thread." Created by writing to a thread, kept alive by holding the watch,
-- released explicitly at surface teardown, TTL-expired if the surface dies.
-- The wake rail's predicate becomes per-thread: cold sessions handle only
-- unclaimed unread, so a live chat owns ITS threads while the rail works
-- everything else in parallel — asynchronous multi-threaded intra-instance.
-- v0.7.72 (#392): a thread is a ticket, doc, OR task — exactly one of the
-- three ids is set per row (task/doc writes raced with live sessions before
-- this because claims were ticket-scoped only). sticky (#229) / session_id
-- (#264) predate this shape as migrations, folded into the base DDL here.
-- Doc/task uniqueness rides partial indexes created in db._migrate (they
-- would fail here against a pre-v0.7.72 table awaiting rebuild).
CREATE TABLE IF NOT EXISTS thread_claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identity        TEXT NOT NULL,               -- matches notifications.recipient
    ticket_id       INTEGER REFERENCES tickets(id),
    doc_id          INTEGER REFERENCES docs(id),
    task_id         INTEGER REFERENCES tasks(id),
    claimed_until   TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sticky          INTEGER DEFAULT 0,
    session_id      TEXT,
    UNIQUE(identity, ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_claims_identity ON thread_claims(identity, claimed_until);
CREATE INDEX IF NOT EXISTS idx_thread_claims_ticket ON thread_claims(ticket_id);

-- ===== Threads (doc 174 / rail 3.9) — the conversation <-> object <-> identity
-- binding. A claim (above) is a short lease guessing whether a reader is
-- still there; a thread row is the RECORD of which conversation (harness
-- session id) holds an object for an identity, from open to closed, however
-- long that takes. One instance per (kind, object, identity). Written by the
-- rail at birth / resume / close, and by any write carrying X-Session-Id when
-- the identity has no open instance on the object ("the conversation that
-- wrote owns the thread"). Raw conversation ids are returned only to the
-- owning identity; every other reader sees a fingerprint.
CREATE TABLE IF NOT EXISTS threads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,                 -- ticket | doc | task
    object_id        INTEGER NOT NULL,
    identity         TEXT NOT NULL,                 -- matches notifications.recipient
    conversation_id  TEXT NOT NULL,                 -- harness session id (X-Session-Id)
    host             TEXT,
    holder           TEXT NOT NULL DEFAULT 'conversation',  -- stream | tmux | oneshot | conversation
    pid              INTEGER,
    state            TEXT NOT NULL DEFAULT 'open',  -- open | closing | closed
    rail_version     TEXT,
    opened_at        TEXT NOT NULL DEFAULT (datetime('now')),
    last_delivery_at TEXT,
    closed_at        TEXT,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    thread_id        INTEGER REFERENCES work_threads(id),   -- v19: the universal thread this binding holds; kind/object_id = its first member
    UNIQUE(kind, object_id, identity)
);
CREATE INDEX IF NOT EXISTS idx_threads_identity ON threads(identity, state);
CREATE INDEX IF NOT EXISTS idx_threads_object ON threads(kind, object_id);

-- ===== Surfaces (doc 174 / rail 3.10) — a live conversation that can RECEIVE.
-- A thread row above says which conversation holds an object; a surface row
-- says that a conversation exists at all, and what kind of thing it is.
--
-- Headless holders never needed this: the rail creates them, so it knows them.
-- A human's interactive session is the opposite — the plane did not create it
-- and cannot see it, which is why it was the last consumer still polling an
-- identity-wide inbox and hand-acking its own cursor: it got every message and
-- had to pull them. Registering
-- the harness session id once gives that session the same three properties a
-- holder has: the plane knows it, delivery can target it, and its scope is the
-- one rule (deliverable(), rail 3.9.18) rather than "everything this identity
-- watches".
--
-- Not a lease and not presence: `last_seen_at` is observability, never a gate.
-- An interactive surface is NOT rail-resumable — when it ends,
-- its threads are simply unheld.
-- v19 (doc 239): the universal thread — the unit of work. Objects belong to one
-- (tickets/tasks/docs.thread_id), instances are bound to one (threads.thread_id),
-- every notification row is stamped with one. A fence, not a lifecycle: created
-- lazily the first time anything touches an object that has none; only a person
-- closes it (PATCH /threads/{id}). root_* is the first member — provenance only.
CREATE TABLE IF NOT EXISTS work_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    root_kind   TEXT NOT NULL,                -- ticket | doc | task
    root_id     INTEGER NOT NULL,
    state       TEXT NOT NULL DEFAULT 'open', -- open | closed
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT,
    UNIQUE(root_kind, root_id)
);

-- v23 (#886): a conversation's own thread — what it makes without naming a
-- thread joins this one. conversation_id '' is the identity's own thread, where
-- an unstamped write lands. Created lazily by the first create; never a second.
CREATE TABLE IF NOT EXISTS conversation_threads (
    identity        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    thread_id       INTEGER NOT NULL REFERENCES work_threads(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (identity, conversation_id)
);

-- #932: provenance survives rebinding, object moves and holder replacement.
-- This is a label record, not a routing binding or a create destination.
CREATE TABLE IF NOT EXISTS conversation_origins (
    identity TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    home_thread_id INTEGER NOT NULL,
    PRIMARY KEY (identity, conversation_id)
);

-- v19: every notification row is stamped with its object's thread at insert
-- time, so no fan-out site has to know about threads. Objects always have one
-- (created with the object; backfilled at migration).
CREATE TRIGGER IF NOT EXISTS notifications_thread_ai AFTER INSERT ON notifications
BEGIN
    UPDATE notifications SET thread_id = COALESCE(NEW.thread_id,
        (SELECT thread_id FROM tickets WHERE id = NEW.ticket_id),
        (SELECT thread_id FROM docs WHERE id = NEW.doc_id),
        (SELECT thread_id FROM tasks WHERE id = NEW.task_id))
    WHERE id = NEW.id;
END;

CREATE TABLE IF NOT EXISTS surfaces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    identity        TEXT NOT NULL,
    conversation_id TEXT NOT NULL,                 -- harness session id (X-Session-Id)
    surface         TEXT NOT NULL DEFAULT 'interactive',  -- interactive | stream | tmux | oneshot
    host            TEXT,
    client          TEXT,                          -- free-text: what is at the other end
    registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at        TEXT,
    UNIQUE(identity, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_surfaces_identity ON surfaces(identity, ended_at);

-- ---------------------------------------------------------------------------
-- Files (#305) — a top-level primitive, a flat sibling to docs/tickets/tasks.
--
-- Deliberately NOT childed to whatever created them: a file can be uploaded
-- standalone, live unassociated, be shared directly, and be attached to several
-- docs or tickets at once without belonging to any of them. Deleting a doc must
-- not delete its images.
--
-- Content-addressed: the blob lives at <sha256> on disk and the display name
-- lives here, so identical uploads de-duplicate and a crafted filename never
-- touches a path. Storage is behind a small interface so the backing store can
-- move (NAS/object store) without a schema change.
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256       TEXT NOT NULL,                   -- content address; blob path is derived from this
    name         TEXT NOT NULL,                   -- display name as uploaded (never used as a path)
    content_type TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    group_scope  TEXT,                            -- null = shared commons, else that group's holders
    owner        TEXT NOT NULL,                   -- bearer-derived, immutable
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_files_owner ON files(owner);
CREATE INDEX IF NOT EXISTS idx_files_sha ON files(sha256);

-- A relation, not ownership. Visibility is the UNION of the file's own scope and
-- anything it is linked to, so attaching a commons image to a scoped doc cannot
-- leak that doc, and a scoped file stays scoped inside a commons doc.
CREATE TABLE IF NOT EXISTS file_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files(id),
    target_type TEXT NOT NULL,                    -- doc | ticket | task
    target_id   TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(file_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_file_links_file ON file_links(file_id);
CREATE INDEX IF NOT EXISTS idx_file_links_target ON file_links(target_type, target_id);

-- Thread mutes (#83): an explicit unsubscribe that outlives your next comment.
-- Ambient auto-subscribes (commenting on the thread) skip muted identities;
-- addressed acts (assignment, @mention, an explicit POST /subscribe) clear the
-- mute — being summoned is not ambient traffic.
CREATE TABLE IF NOT EXISTS thread_mutes (
    identity    TEXT NOT NULL,
    ticket_id   INTEGER REFERENCES tickets(id),
    doc_id      INTEGER REFERENCES docs(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mutes_ticket ON thread_mutes(identity, ticket_id) WHERE ticket_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mutes_doc ON thread_mutes(identity, doc_id) WHERE doc_id IS NOT NULL;

-- Auth-refused observations (#503 shape (c)): the registry itself serves the
-- 401s a locked-out listener retries against — record them, attributed by
-- token-hash resolution (an expired/rotated token still names its owner even
-- when it no longer verifies). The sharpest dark signal there is: a seat that
-- is alive but cannot get in.
CREATE TABLE IF NOT EXISTS wake_auth_refusals (
    identity    TEXT PRIMARY KEY,
    first_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_at     TEXT NOT NULL DEFAULT (datetime('now')),
    count       INTEGER NOT NULL DEFAULT 0
);

-- Last evaluated dark state per identity (#503): the dark flag was computed
-- at read time only, so a fleet outage was visible solely to someone polling
-- the endpoint — detection without consumption. The evaluator diffs against
-- this and pushes a notification on transitions.
CREATE TABLE IF NOT EXISTS wake_dark_state (
    identity    TEXT PRIMARY KEY,
    dark        INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    since       TEXT
);

-- Session outcomes per identity (#521): dark-watch measured whether a seat
-- CONNECTS, never whether it can WORK. claude-gpu-1 connected healthily for 23
-- days -- listener_version current, stale:false, dark:false, auth_refused:0 --
-- while every single launch died on `Not logged in`. The handler has computed
-- the verdict since 2.4.5 (`session outcome=FAILED rc=1 (tombstone)`) and then
-- dropped it in a local logfile: the one place nobody reads until an incident.
--
-- The seat now reports every ATTEMPT with its outcome, which is what makes
-- three states separable instead of two (#521 c3742):
--   attempts=0              -> idle OR not reporting: the capability leg abstains
--   attempts>0, successes=0 -> failing-launches
--   attempts>0, successes>0 -> healthy
-- Without the attempt count rows 1 and 2 are the same row, and that is exactly
-- the row claude-gpu-1 sat in for 23 days. A fault UPSTREAM of the reporter (no
-- token to authenticate with, no launcher binary to exec) still emits nothing
-- at all -- that seat is unmeasurable here by construction, and `probe_at`
-- exists so silence gets interrogated rather than assumed green.
--
-- The report carries a CAUSE CLASS, not just `rc`, because the failure bucket
-- is not homogeneous and `rc` cannot split it. Two seats, measured the same
-- night, both emitting identical `exit=1` + identical tombstone lines:
--   seat A    4,284 auth       / 0 quota   (c3751)
--   seat B    0 auth / 463 of 464 quota    (c3757)
-- One needs an operator tonight; the other clears itself at 7am and its seat
-- is perfectly healthy -- seat B's longest quota run is 234
-- consecutive failures over ~70h, which would trip any threshold built on
-- `rc` alone. A counter keyed on `rc` pages on the healthy one.
--
-- Per-class counter semantics (converged c3749/c3751/c3752/c3753, and the
-- watchdog row settled c3757/c3759/c3760/c3761):
--   ok        exit 0            -> resets. Launcher ran, session completed.
--   quota     rate/usage limit  -> RESETS. Being told you are rate-limited is
--                                  POSITIVE capability evidence: the launcher
--                                  ran, the broker minted, the API
--                                  authenticated you. Stale failures must not
--                                  survive a proof of capability.
--   auth      credential dead   -> latches. Needs an operator.
--   launcher  rc=127, exec fail -> latches. Needs an operator.
--   watchdog  killed at the cap -> NEITHER; counts on `truncations` instead.
--                                  The one outcome where the seat is provably
--                                  ALIVE AND WORKING at the moment of the
--                                  verdict: 29 kills across three rails, zero
--                                  stalled, median 102-121 tool calls against
--                                  5-45 for a clean exit. Counting it toward
--                                  incapacity inverts this ticket's own bug --
--                                  one rail's longest run is 5, so the default
--                                  threshold of 3 would have paged it across
--                                  the most productive stretch in its log.
--                                  Do NOT read this class as "the seat hung":
--                                  that is false on all 29.
--   api       anything else     -> NEITHER. Under-firing is the safe direction
--                                  for a detector whose premise is that people
--                                  stopped trusting a noisy channel.
CREATE TABLE IF NOT EXISTS wake_session_stats (
    identity             TEXT PRIMARY KEY,
    attempts             INTEGER NOT NULL DEFAULT 0,
    successes            INTEGER NOT NULL DEFAULT 0,
    failures             INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,  -- LATCHING classes only
    first_attempt_at     TEXT,
    last_attempt_at      TEXT,
    last_ok_at           TEXT,     -- session exited 0. NOT that work happened.
    last_launch_ok_at    TEXT,     -- launcher reached the API (ok OR quota)
    last_failed_at       TEXT,
    last_rc              INTEGER,
    last_cause           TEXT,     -- ok|auth|quota|launcher|watchdog|api
    last_error           TEXT,     -- FIRST error line, truncated; never the tail
    last_host            TEXT,
    probe_at             TEXT,     -- last synthetic capability probe (idle seats produce no attempts)
    truncations          INTEGER NOT NULL DEFAULT 0,  -- killed at the session cap
    last_truncated_at    TEXT      -- session-BUDGET signal, never a capability one
);
