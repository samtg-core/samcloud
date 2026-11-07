"""Registry database layer — SQLite with WAL mode."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("REGISTRY_DB_PATH", "/var/lib/samcloud/registry.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate(conn)
    conn.close()


def _migrate(conn):
    """Run schema migrations that can't be expressed as CREATE IF NOT EXISTS."""
    # RFC 386: legacy participation has no explicit-subscription provenance.
    # Keep conversation audiences; record updates require a new opt-in.
    for table in ("ticket_watchers", "doc_watchers"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "updates" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN updates INTEGER NOT NULL DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(docs)")}
    if "owner" not in cols:
        conn.execute("ALTER TABLE docs ADD COLUMN owner TEXT")
        conn.execute("UPDATE docs SET owner = source")
    # #932: immutable writer provenance across every comment plane. Old rows
    # stay unknown; today's holder is not evidence of yesterday's author.
    for table in ("ticket_comments", "doc_comments", "task_comments"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, typ in (("session_fp", "TEXT"), ("home_thread_id", "INTEGER")):
            if name not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    conn.commit()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)").fetchall()}

    # v2 schema: inbound/outbound connection model
    for col, ctype in [
        ("name", "TEXT"),
        ("owner_id", "INTEGER"),
        ("inbound_method", "TEXT"),
        ("inbound_endpoint", "TEXT"),
        ("outbound_method", "TEXT"),
        ("outbound_ip", "TEXT"),
        ("outbound_ssh_user", "TEXT"),
        ("outbound_ssh_port", "INTEGER DEFAULT 22"),
        ("ssh_pubkey", "TEXT"),
        ("ssh_user_warning", "TEXT"),
        # v5: shared-transport — secondary user borrows another device's tunnel
        ("transport_owner", "TEXT"),
        # v7: the host key the gateway pins a hop against (#818)
        ("host_pubkey", "TEXT"),
        ("host_pubkey_recorded_at", "TEXT"),
    ]:
        if col not in columns:
            conn.execute(f"ALTER TABLE devices ADD COLUMN {col} {ctype}")

    # `workcloud` was the early label for a device reached by reverse tunnel;
    # it is `tunnel` now (#1010). Behaviour keys on outbound_method, not this.
    # A legacy row that named no method was a reverse tunnel by its label.
    conn.execute("UPDATE devices SET outbound_method = 'reverse-tunnel' "
                 "WHERE context = 'workcloud' AND outbound_method IS NULL")
    conn.execute("UPDATE devices SET context = 'tunnel' WHERE context = 'workcloud'")

    # Rename hostname → name for existing rows (if hostname exists but name is null)
    if "hostname" in columns and "name" in columns:
        conn.execute("UPDATE devices SET name = hostname WHERE name IS NULL AND hostname IS NOT NULL")

    # v3: services.created_by — track who created each service
    svc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(services)").fetchall()}
    if "created_by" not in svc_columns:
        conn.execute("ALTER TABLE services ADD COLUMN created_by TEXT")
    # v4: services.description + docs_endpoint
    if "description" not in svc_columns:
        conn.execute("ALTER TABLE services ADD COLUMN description TEXT")
    if "docs_endpoint" not in svc_columns:
        conn.execute("ALTER TABLE services ADD COLUMN docs_endpoint TEXT")

    conn.commit()

    # Add password_hash column to users if missing
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "password_hash" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")

    # v0.7.71: docs.parent_doc_id — doc hierarchy (overview parents / gotcha sub-docs)
    doc_columns = {row["name"] for row in conn.execute("PRAGMA table_info(docs)").fetchall()}
    if "parent_doc_id" not in doc_columns:
        conn.execute("ALTER TABLE docs ADD COLUMN parent_doc_id INTEGER REFERENCES docs(id)")
        conn.commit()

    # v6: docs plane (RFC #105) — doc events ride the same durable inbox as
    # ticket events, so the existing notifications table grows a nullable doc_id
    # alongside ticket_id (one of the two is set per row).
    notif_columns = {row["name"] for row in conn.execute("PRAGMA table_info(notifications)").fetchall()}
    if "doc_id" not in notif_columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN doc_id INTEGER REFERENCES docs(id)")
        conn.commit()

    # v7: task plane (doc #13, #148) — task-assignment notifications ride the
    # same durable inbox; nullable task_id alongside ticket_id / doc_id.
    if "task_id" not in notif_columns:
        conn.execute("ALTER TABLE notifications ADD COLUMN task_id INTEGER REFERENCES tasks(id)")
        conn.commit()

    # v8: wake rail (RFC doc #17) — users.last_seen stamped on inbox reads; a
    # stale value means the agent's wake rail is dead (the observability line
    # between "async rail" and "async rail you can trust").
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "last_seen" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
        conn.commit()

    # v9: foreground presence (doc #17) — live sessions stamp presence_until;
    # the identity's own wake rail defers dispatch while it is fresh.
    if "presence_until" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN presence_until TEXT")
        conn.commit()

    # v10: interactive-surface observability (#198) — the listener reports the
    # installed hook versions alongside its own, so the fleet view covers the
    # chat surface too, not just the rail.
    wake_reg_columns = {row["name"] for row in
                        conn.execute("PRAGMA table_info(wake_registrations)").fetchall()}
    if wake_reg_columns:
        for col in ("notif_hook_version", "stop_hook_version"):
            if col not in wake_reg_columns:
                conn.execute(f"ALTER TABLE wake_registrations ADD COLUMN {col} TEXT")
        # v12 (#472): seat's reconnect bound, so the dark-seat threshold is
        # derived per row instead of assuming the 6h client default.
        if "max_conn_age" not in wake_reg_columns:
            conn.execute("ALTER TABLE wake_registrations ADD COLUMN max_conn_age INTEGER")
        # v13 (#897): a registration could be created by anyone and retired by
        # nobody — GET and POST /wake/register were the whole surface, for admin
        # as much as for a seat — so the one table feeding the fleet's health view
        # could only ever grow. Additive and nullable: every existing row reads
        # NULL = not retired, so the default view is unchanged on the day it ships
        # and nothing needs backfilling.
        if "ended_at" not in wake_reg_columns:
            conn.execute("ALTER TABLE wake_registrations ADD COLUMN ended_at TEXT")
        conn.commit()

    # v11: notification lifecycle (#227) — per-consumer ack attribution
    # (acked_via), soft row-level "seen" leases (seen_until: a live surface
    # has eyes on the row, the cold rail skips it while fresh — ack remains
    # the only "handled"), and sticky claims (explicitly asserted engagement
    # that identity-wide releases must not clobber, #229 twin race).
    notif_columns = {row["name"] for row in
                     conn.execute("PRAGMA table_info(notifications)").fetchall()}
    if notif_columns:
        # delivered_at / delivered_to (doc 174): the delivery receipt — the
        # rail wrote the row into a live conversation. A transport fact,
        # distinct from `seen` (a lease) and `read` (the reader's flag).
        for col in ("acked_via", "seen_until", "delivered_at", "delivered_to"):
            if col not in notif_columns:
                conn.execute(f"ALTER TABLE notifications ADD COLUMN {col} TEXT")
        conn.commit()
    claim_columns = {row["name"] for row in
                     conn.execute("PRAGMA table_info(thread_claims)").fetchall()}
    if claim_columns and "sticky" not in claim_columns:
        conn.execute("ALTER TABLE thread_claims ADD COLUMN sticky INTEGER DEFAULT 0")
        conn.commit()
    # #264: session dimension on claims — which live surface of an identity
    # owns the thread, so parallel sessions (tmux, terminal, wake) exclude
    # each other instead of silently sharing one row. NULL = legacy claim.
    if claim_columns and "session_id" not in claim_columns:
        conn.execute("ALTER TABLE thread_claims ADD COLUMN session_id TEXT")
        conn.commit()
    # #264 differentiation: comments carry a session FINGERPRINT (hash, not
    # the raw nonce) so two surfaces of one identity writing the same thread
    # are tellable apart in the record. NULL = legacy/no-nonce writer.
    cmt_columns = {row["name"] for row in
                   conn.execute("PRAGMA table_info(ticket_comments)").fetchall()}
    if cmt_columns and "session_fp" not in cmt_columns:
        conn.execute("ALTER TABLE ticket_comments ADD COLUMN session_fp TEXT")
        conn.commit()

    # #383: one-level reply threading — a reply anchors to a top-level comment
    # on the same ticket (enforced in add_comment; NULL = top-level).
    if cmt_columns and "parent_comment_id" not in cmt_columns:
        conn.execute("ALTER TABLE ticket_comments ADD COLUMN parent_comment_id "
                     "INTEGER REFERENCES ticket_comments(id)")
        conn.commit()

    # Backfill for the comment->updated_at fix (#470): shipped in v0.7.82
    # without touching existing rows, leaving ~115 open threads reading
    # quieter than reality. Naturally idempotent: lifts updated_at only
    # where a comment is newer.
    for _table, _fk, _ctable in (("tickets", "ticket_id", "ticket_comments"),
                                 ("docs", "doc_id", "doc_comments"),
                                 ("tasks", "task_id", "task_comments")):
        conn.execute(
            f"UPDATE {_table} SET updated_at = "
            f"(SELECT MAX(c.created_at) FROM {_ctable} c WHERE c.{_fk} = {_table}.id) "
            f"WHERE EXISTS (SELECT 1 FROM {_ctable} c WHERE c.{_fk} = {_table}.id "
            f"AND c.created_at > {_table}.updated_at)")
    conn.commit()

    # v12: first-class tags on tickets + tasks (#203: views
    # over data, never hardcoded sections — 'assigned to me and tag (x)').
    # JSON string array, normalized lowercase. Backfill: the DECISION-title
    # convention becomes tag 'decision' as data, retiring the title hack.
    tk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    if tk_columns and "tags" not in tk_columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN tags TEXT")
        conn.commit()
    ta_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if ta_columns and "tags" not in ta_columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN tags TEXT")
        conn.execute("UPDATE tasks SET tags = '[\"decision\"]' "
                     "WHERE title LIKE 'DECISION%' AND tags IS NULL")
        conn.commit()

    # v13: explicit group_scope on tickets (#330). Docs/files already carry it;
    # tickets derived visibility only from source/assignee/device/service, so
    # "share this ticket with my group" had no expression. Nullable and
    # additive — a set scope is a UNION with the existing rules, never a
    # restriction, so no existing ticket changes visibility. (Tasks ride their
    # project's group_scope, so they need no column of their own.)
    tk_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)").fetchall()}
    if tk_columns and "group_scope" not in tk_columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN group_scope TEXT")
        conn.commit()

    # v14 (#392): claims widen to doc/task threads. The #199/#264 machinery was
    # ticket-scoped only, so a live surface working a DOC or TASK was invisible
    # to its own cold rail — two agent surfaces double-wrote the same
    # deliverables. ticket_id also drops NOT NULL (exactly one of the three ids
    # per row), which SQLite can't ALTER — rebuild. Claims are ephemeral
    # (minutes-scale TTL), so only live rows with a valid ticket are carried.
    claim_columns = {row["name"] for row in
                     conn.execute("PRAGMA table_info(thread_claims)").fetchall()}
    if claim_columns and "doc_id" not in claim_columns:
        conn.executescript("""
            ALTER TABLE thread_claims RENAME TO thread_claims_v0;
            CREATE TABLE thread_claims (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                identity        TEXT NOT NULL,
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
            INSERT INTO thread_claims (id, identity, ticket_id, claimed_until,
                                       created_at, updated_at, sticky, session_id)
                SELECT id, identity, ticket_id, claimed_until, created_at,
                       updated_at, COALESCE(sticky, 0), session_id
                FROM thread_claims_v0
                WHERE claimed_until > datetime('now')
                  AND ticket_id IN (SELECT id FROM tickets);
            DROP TABLE thread_claims_v0;
            CREATE INDEX IF NOT EXISTS idx_thread_claims_identity
                ON thread_claims(identity, claimed_until);
            CREATE INDEX IF NOT EXISTS idx_thread_claims_ticket
                ON thread_claims(ticket_id);
        """)
        conn.commit()
    # Partial uniques for the new planes — here, not schema.sql, so they never
    # run against a pre-rebuild table that lacks the columns.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_claims_uq_doc "
                 "ON thread_claims(identity, doc_id) WHERE doc_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_thread_claims_uq_task "
                 "ON thread_claims(identity, task_id) WHERE task_id IS NOT NULL")
    conn.commit()

    # v22 (#827): a lease carries the longest it could legitimately be held, so a
    # renewal cannot walk it past what its holder originally justified. It has to be
    # stored rather than derived: `expires_at - granted_at` is the original span only
    # until the first renewal, after which it ratchets and the ceiling becomes
    # unbounded. NULL means "use the registry default", which is what every row
    # granted before this column says.
    lease_columns = {row["name"] for row in conn.execute("PRAGMA table_info(leases)").fetchall()}
    if "max_total_s" not in lease_columns:
        conn.execute("ALTER TABLE leases ADD COLUMN max_total_s INTEGER")
        conn.commit()
    # 0.12.98: the queue. A queued row keeps the TTL it asked for, so promotion
    # can grant it the lease it requested, not a default; and an expired row says
    # why (`wait_expired` is a waiter that ran out of patience, not a dead holder).
    if "ttl_s" not in lease_columns:
        conn.execute("ALTER TABLE leases ADD COLUMN ttl_s INTEGER")
        conn.execute("ALTER TABLE leases ADD COLUMN expiry_reason TEXT")
        conn.commit()
    # 0.12.99 (task 868): an expired holder on a `hold` resource keeps charging the
    # ledger until its holder or an admin settles it — `settled_at` is that act.
    if "settled_at" not in lease_columns:
        conn.execute("ALTER TABLE leases ADD COLUMN settled_at TEXT")
        conn.commit()
    resource_columns = {row["name"] for row in conn.execute("PRAGMA table_info(resources)").fetchall()}
    if "expiry_policy" not in resource_columns:
        conn.execute("ALTER TABLE resources ADD COLUMN expiry_policy TEXT NOT NULL DEFAULT 'free'")
        conn.commit()

    # v21 (#806): at most ONE active exclusive lease per resource, enforced by the
    # database rather than by a SELECT that a concurrent request can step over.
    # `claude-services/gpu-pool` is declared exclusive and granted TWO leases
    # 20 ms apart on 2026-09-22, putting two generations into a single-slot pool for
    # 53 seconds — while a request 53 s later was correctly refused with 409, which
    # is what a check-then-act race looks like from the outside. The gateway's whole
    # serialisation, including its wedge guard ("we hold the only lease, so a runner
    # still Running is not another holder"), rests on this being true.
    dupes = conn.execute(
        "SELECT resource_id, COUNT(*) n FROM leases "
        "WHERE exclusive = 1 AND status = 'active' GROUP BY resource_id HAVING n > 1"
    ).fetchall()
    for row in dupes:
        # Keep the oldest; the later grant was the one that should never have been.
        conn.execute(
            "UPDATE leases SET status = 'expired' WHERE resource_id = ? AND exclusive = 1 "
            "AND status = 'active' AND id != ("
            "  SELECT id FROM leases WHERE resource_id = ? AND exclusive = 1 "
            "  AND status = 'active' ORDER BY created_at, id LIMIT 1)",
            (row["resource_id"], row["resource_id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_uq_exclusive "
                 "ON leases(resource_id) WHERE exclusive = 1 AND status = 'active'")
    conn.commit()

    # v15 (#521): cause class on session outcomes. The capability leg shipped
    # counting `rc`, and `rc` cannot split the failure bucket -- 4,284 auth
    # failures on one seat and 463 quota failures on another emit byte-
    # identical `exit=1`. Additive columns so a registry that already carries
    # the unclassed table picks up the split without losing its counters.
    stat_columns = {row["name"] for row in
                    conn.execute("PRAGMA table_info(wake_session_stats)").fetchall()}
    if stat_columns:
        for col in ("last_cause", "last_error", "last_launch_ok_at"):
            if col not in stat_columns:
                conn.execute(f"ALTER TABLE wake_session_stats ADD COLUMN {col} TEXT")
        # v16 (#521): `watchdog` moved OUT of the capability counter onto its
        # own axis. A session killed at the cap is the one outcome where the
        # seat is provably alive and working (29 kills, three rails, zero
        # stalled), so it measures the session BUDGET, not incapacity.
        if "truncations" not in stat_columns:
            conn.execute("ALTER TABLE wake_session_stats "
                         "ADD COLUMN truncations INTEGER NOT NULL DEFAULT 0")
        if "last_truncated_at" not in stat_columns:
            conn.execute("ALTER TABLE wake_session_stats "
                         "ADD COLUMN last_truncated_at TEXT")
        # Any counter already accrued under the old disposition is a false
        # latch waiting to fire: a registry that recorded watchdog kills as
        # capability failures may be holding a seat at or near the threshold
        # for sessions that were working. Clear the ones whose LAST attempt
        # was a kill — the rest will clear themselves on their next ok/quota.
        conn.execute("UPDATE wake_session_stats SET consecutive_failures = 0 "
                     "WHERE last_cause = 'watchdog' AND consecutive_failures > 0")
        conn.commit()

    # v17 (doc 161 §5, task 596): plane-native scheduling on tasks — the
    # timer half of #303 (AgentWake/schedule.fired was the delivery half).
    # Additive and nullable; nothing changes for unscheduled tasks.
    ta_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    if ta_columns:
        for col in ("scheduled_at", "recurrence", "last_fired_at"):
            if col not in ta_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        conn.commit()

    # v19 (doc 239): the universal thread. Additive: a table, and a nullable
    # thread_id on tickets, tasks, docs, notifications and the binding table
    # (`threads`, whose kind/object_id becomes the thread's first member). A
    # fence, not a lifecycle. Threads are created lazily by the registry the first
    # time anything touches an object without one, so no backfill runs here.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_threads (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT,
            root_kind   TEXT NOT NULL,
            root_id     INTEGER NOT NULL,
            state       TEXT NOT NULL DEFAULT 'open',
            created_by  TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            closed_at   TEXT,
            UNIQUE(root_kind, root_id)
        )
    """)
    for tbl in ("tickets", "tasks", "docs", "notifications", "threads"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if cols and "thread_id" not in cols:
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN thread_id INTEGER")
    # Backfill: every existing object starts its own thread (its first member is
    # itself); bindings and old notification rows follow their object. Idempotent —
    # only rows with no thread_id are touched.
    for kind, tbl, title_col, by_col in (("ticket", "tickets", "summary", "source"),
                                         ("doc", "docs", "title", "source"),
                                         ("task", "tasks", "title", "source")):
        if not {r["name"] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}:
            continue
        conn.execute(f"INSERT OR IGNORE INTO work_threads (title, root_kind, root_id, created_by) "
                     f"SELECT {title_col}, '{kind}', id, {by_col} FROM {tbl} WHERE thread_id IS NULL")
        conn.execute(f"UPDATE {tbl} SET thread_id = (SELECT w.id FROM work_threads w "
                     f"WHERE w.root_kind = '{kind}' AND w.root_id = {tbl}.id) WHERE thread_id IS NULL")
        conn.execute(f"UPDATE threads SET thread_id = (SELECT thread_id FROM {tbl} WHERE {tbl}.id = threads.object_id) "
                     f"WHERE kind = '{kind}' AND thread_id IS NULL")
        col = {"ticket": "ticket_id", "doc": "doc_id", "task": "task_id"}[kind]
        conn.execute(f"UPDATE notifications SET thread_id = (SELECT thread_id FROM {tbl} WHERE {tbl}.id = notifications.{col}) "
                     f"WHERE {col} IS NOT NULL AND thread_id IS NULL")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS notifications_thread_ai AFTER INSERT ON notifications
        BEGIN
            UPDATE notifications SET thread_id = COALESCE(NEW.thread_id,
                (SELECT thread_id FROM tickets WHERE id = NEW.ticket_id),
                (SELECT thread_id FROM docs WHERE id = NEW.doc_id),
                (SELECT thread_id FROM tasks WHERE id = NEW.task_id))
            WHERE id = NEW.id;
        END
    """)
    conn.commit()

    # v23 (#886): a conversation's own thread. What a conversation makes
    # without naming a thread joins the thread its first create started —
    # there is no create that starts a second one. `conversation_id` '' is the
    # identity's own thread: where an unstamped write lands. Created lazily by
    # the registry; no backfill.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversation_threads (
            identity        TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            thread_id       INTEGER NOT NULL REFERENCES work_threads(id),
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (identity, conversation_id)
        )""")
    conn.commit()

    # v18 (rail 3.10): `surfaces` — a conversation that can receive, declared
    # rather than inferred. Created by schema.sql on a fresh DB; this arm is
    # for a live one. Additive; no existing row is touched, and nothing reads
    # it until a surface registers.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS surfaces (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            identity        TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            surface         TEXT NOT NULL DEFAULT 'interactive',
            host            TEXT,
            client          TEXT,
            registered_at   TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
            ended_at        TEXT,
            UNIQUE(identity, conversation_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_surfaces_identity "
                 "ON surfaces(identity, ended_at)")
    conn.commit()

    # v21 (0.12.20): the thread joins the conversation-scoped read makes. Six
    # correlated EXISTS per call, all of them on unindexed thread_id columns.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_notifications_thread ON notifications(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_tickets_thread ON tickets(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_docs_thread ON docs(thread_id)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_thread ON tasks(thread_id)",
    ):
        try:
            conn.execute(stmt)
        except Exception:
            pass          # the column arrives with v19; a DB older than that has no work here

    # v20 (scope v1, doc 261): nothing unscoped is shared. Every ticket, doc,
    # project and file with no group_scope gets its creator's first group,
    # where it has one; a machine
    # identity (`<device>/<service>`) the first group holding its device, and
    # anything else `core`, the plane's own group (seeded by schema.sql). The
    # role vocabulary becomes viewer/editor/owner (`admin` rows were scoring as
    # viewer). Idempotent: only NULL rows are touched, and the counts are
    # written to the audit log once per run that changed anything.
    conn.execute("INSERT OR IGNORE INTO groups (id, name, description) VALUES "
                 "('core', 'core', 'The inner city — administration and the gateway. Least access.')")
    conn.execute("INSERT OR IGNORE INTO groups (id, name, description) VALUES "
                 "('samcloud-contributors', 'samcloud contributors', 'Work on samcloud itself — its records, "
                 "boards, briefs and, later, its PRs. The admin writes here by default; seats that contribute are editors.')")
    conn.execute("UPDATE group_members SET role = 'owner' WHERE role = 'admin'")
    gcols = {row["name"] for row in conn.execute("PRAGMA table_info(groups)").fetchall()}
    if gcols and "doc_id" not in gcols:
        conn.execute("ALTER TABLE groups ADD COLUMN doc_id INTEGER")   # the working doc (scope v1, train 2)
    admins = {r["username"] for r in conn.execute("SELECT username FROM users WHERE role = 'admin'").fetchall()}
    first_group = {}
    for r in conn.execute("SELECT u.username, gm.group_id FROM group_members gm "
                          "JOIN users u ON u.id = gm.user_id ORDER BY gm.rowid").fetchall():
        first_group.setdefault(r["username"], r["group_id"])
    device_group = {}
    for r in conn.execute("SELECT device_id, group_id FROM group_devices ORDER BY rowid").fetchall():
        device_group.setdefault(r["device_id"], r["group_id"])
    groups = {r["id"] for r in conn.execute("SELECT id FROM groups").fetchall()}

    def _home(creator):
        creator = creator or ""
        g = first_group.get(creator)
        if not g and creator in admins:
            g = "samcloud-contributors"      # the admin's records: the plane's own work, not the inner city
        if not g and "/" in creator:
            g = device_group.get(creator.split("/", 1)[0])
        if not g and creator in device_group:
            g = device_group[creator]
        return g if g in groups else "core"

    def _has_home(creator):
        creator = creator or ""
        return creator in first_group or (("/" in creator and creator.split("/", 1)[0] in device_group)
                                          or creator in device_group)

    counts = {}
    for tbl, creator_col in (("tickets", "source"), ("docs", "source"), ("projects", "source"), ("files", "owner")):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if not cols or "group_scope" not in cols:
            continue
        extra = ", assignee" if tbl == "tickets" else ""
        rows = conn.execute(f"SELECT id, {creator_col} AS who{extra} FROM {tbl} "
                            f"WHERE group_scope IS NULL OR group_scope = ''").fetchall()
        for r in rows:
            # A ticket filed by someone with no group of their own (the admin)
            # to someone who has one goes to the assignee's — the shape of an
            # admin filing work to a seat, which is what most such rows are.
            if tbl == "tickets" and (r["who"] or "") in admins and _has_home(r["assignee"]):
                g = _home(r["assignee"])
            else:
                g = _home(r["who"])
            conn.execute(f"UPDATE {tbl} SET group_scope = ? WHERE id = ?", (g, r["id"]))
        if rows:
            counts[tbl] = len(rows)
    if counts:
        conn.execute("INSERT INTO audit_log (actor, action, target, detail) VALUES (?, ?, ?, ?)",
                     ("system:migration", "scope.backfill", "v20", json.dumps(counts)))
    conn.commit()


@contextmanager
def transaction():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def query(sql: str, params: tuple = ()) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    with transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid


def execute_count(sql: str, params: tuple = ()) -> int:
    """Like execute() but returns the AFFECTED ROW COUNT — for DELETE/UPDATE
    callers. execute()'s lastrowid is meaningless for those (#564 round 2:
    /wake/claims/release reported released:0 on every call, including the
    ones that demonstrably deleted a claim)."""
    with transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.rowcount


def audit(actor: str, action: str, target: str = None, detail: dict = None, conn=None):
    sql = "INSERT INTO audit_log (actor, action, target, detail) VALUES (?, ?, ?, ?)"
    params = (actor, action, target, json.dumps(detail) if detail else None)
    if conn:
        conn.execute(sql, params)
    else:
        execute(sql, params)
