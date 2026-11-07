/**
 * Registry types — shared shapes for the samcloud registry API.
 *
 * Data fetching lives in the pages/components themselves, which call the
 * registry through the `/api/registry` proxy (it forwards the user's session
 * token; the registry enforces scope). The tokenless / gateway-token-file
 * server-side fetchers that used to live here were removed — they bypassed the
 * auth proxy and silently 401'd against the hardened registry. Import the
 * shapes below with `import type`.
 */

export interface Device {
  id: string;
  hostname: string;
  context: string;
  type: string;
  ip: string | null;
  ssh_user: string | null;
  ssh_port: number;
  tunnel_port: number | null;
  lifeline_port: number | null;
  monitor_port: number | null;
  enrolled: number;
  enrolled_at: string | null;
  startup_method: string | null;
  recovery_method: string | null;
  status: string;
  last_seen: string | null;
}

export interface Service {
  id: string;
  name: string;
  device_id: string;
  port: number;
  slice_port: number | null;
  protocol: string;
  health_endpoint: string | null;
  subdomain: string | null;
  domain: string | null;
  external_url: string | null;
  status: string;
  version: string | null;
  capabilities: string | null;
  last_health: string | null;
  last_health_status: number | null;
}

export interface Port {
  port: number;
  type: string;
  owner_id: string;
  allocated_at: string | null;
}

export interface Route {
  id: number;
  subdomain: string;
  domain: string;
  target: string;
  service_id: string | null;
  created_at: string;
}

export interface Ticket {
  id: number;
  source: string;
  type: string;
  priority: string;
  status: string;
  summary: string;
  body: string | null;
  service_id: string | null;
  device_id: string | null;
  assignee: string | null;
  created_at: string;
  updated_at: string | null;
  resolved_at: string | null;
}

export interface TicketWithComments extends Ticket {
  comments: TicketComment[];
}

export interface TicketComment {
  session_fp?: string | null;
  home_thread_id?: number | null;
  id: number;
  ticket_id: number;
  author: string;
  body: string;
  created_at: string;
}

export interface AuditEntry {
  id: number;
  actor: string;
  action: string;
  target: string | null;
  detail: string | null;
  created_at: string;
}

// ── Docs plane (RFC #105) — versioned, contributor-directed knowledge ──
export interface DocLink {
  id?: number;
  target_type: string;   // ticket | service | device | doc
  target_id: string;
  created_by?: string;
  created_at?: string;
}

// A row from GET /api/v1/docs (no body).
export interface Doc {
  id: number;
  slug: string;
  title: string;
  kind: string;          // doc | rfc | runbook | note
  status: string;        // draft | active | superseded | archived
  group_scope: string | null;
  source: string;        // owner (bearer-derived)
  supersedes: number | null;
  parent_doc_id?: number | null;  // doc hierarchy (H3): nested under this doc
  current_rev: number;
  created_at: string;
  updated_at: string;
  links?: DocLink[];
}

// Compact reference to a related doc (parent/children in the hierarchy).
export interface DocRef {
  id: number;
  slug: string;
  title: string;
  kind: string;
  status: string;
}

export interface DocRevisionMeta {
  rev: number;
  title: string;
  author: string;
  note: string | null;
  created_at: string;
}

// The discussion layer — distinct from the versioned body.
export interface DocComment {
  session_fp?: string | null;
  home_thread_id?: number | null;
  id: number;
  author: string;
  body: string;
  created_at: string;
}

// GET /api/v1/docs/{id} — head (or ?rev=N) body + links + comments + counts.
export interface DocDetail extends Doc {
  rev: number;           // the revision currently being viewed
  body: string;
  rev_author: string;
  rev_note: string | null;
  rev_created_at: string;
  revision_count: number;
  links: DocLink[];
  comments: DocComment[];
  parent?: DocRef | null;   // H3 breadcrumb without a second fetch
  children?: DocRef[];      // sub-docs nested under this one
}

// ── Task plane (doc #13, #148) — projects / tasks / subtasks ──
export interface ProjectRollup {
  task_count: number;
  by_status: Record<string, number>;
  progress_pct: number;
  progress_basis: string;   // estimate-weighted | count | empty
  estimate_total_min: number | null;
}

export interface Project {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  status: string;           // planning | active | paused | done | archived
  group_scope: string | null;
  source: string;           // owner (bearer-derived)
  created_at: string;
  updated_at: string;
  rollup?: ProjectRollup;
}

export interface ProjectTask {
  id: number;
  project_id: number;
  parent_task_id: number | null;
  title: string;
  detail: string | null;
  status: string;           // todo | in_progress | blocked | done | cancelled
  priority: string | null;
  estimate_min: number | null;
  actual_min: number | null;
  assignee: string | null;
  due: string | null;
  position: number | null;
  source_ref: string | null;
  delegated_ticket_id: number | null;
  source: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  subtasks?: ProjectTask[];
}

export interface ProjectDetail extends Project {
  rollup: ProjectRollup;
  tasks: ProjectTask[];
}

export interface RegistryStatus {
  devices: Device[];
  services: Service[];
  ports: Port[];
  routes: Route[];
  open_tickets: Ticket[];
  counts: {
    devices: number;
    services: number;
    ports: number;
    routes: number;
    open_tickets: number;
  };
}

export interface ProbeResult {
  probed: number;
  active: number;
  stopped: number;
  degraded: number;
  results: {
    service: string;
    status: string;
    http_status?: number;
    error?: string;
    url?: string;
  }[];
}

export interface Group {
  id: string;
  name: string;
  description: string | null;
  route_path: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface GroupWithDetails extends Group {
  services: Service[];
  members: GroupMember[];
}

export interface GroupMember {
  id: number;
  username: string;
  user_role: string;
  group_role: string;
  added_at: string;
}

export interface GroupStatus {
  group: {
    id: string;
    name: string;
    description: string | null;
  };
  devices: Device[];
  services: Service[];
  routes: Route[];
  counts: {
    devices: number;
    services: number;
    active_services: number;
    online_devices: number;
    routes: number;
  };
}

export interface User {
  id: number;
  username: string;
  role: string;
  scopes: string | null;
  created_at: string;
  updated_at: string;
  groups?: { id: string; name: string; description: string | null; route_path: string | null; member_role: string }[];
}
