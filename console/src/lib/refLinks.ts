/**
 * Plane references in prose become links (#1082: "everyone uses doc and ticket
 * ids … i can't remember, but at least using hyperlinks").
 *
 *   ticket 1082 · ticket #1082 · tickets 1082 · #1082      → /tickets/1082
 *   doc 470 · doc #470                                     → /docs/470
 *   task 869 · task #869                                   → /tasks/869
 *
 * A bare #N needs two or more digits and is never read after "PR"/"pull", so
 * "step #1" and "PR #104" stay text. Text inside links and code is untouched.
 *
 * A pull request names its repo, and links to GitHub (#1144: "unity-service #16"
 * linked to ticket 16):
 *
 *   samtg-projects/unity-service#16                        → github.com/samtg-projects/unity-service/pull/16
 *   unity-service#16 · unity-service #16 · unity-service PR 16 · unity-service PR #16
 *   samcloud#49 · samcloud PR 49                           → github.com/samtg-core/samcloud/pull/49
 *
 * "samcloud #N", with a space, stays a ticket: the plane's tickets are samcloud's.
 */

const KIND_PATH: Record<string, string> = { ticket: 'tickets', doc: 'docs', task: 'tasks' };

/** Repos a short name may stand for. Anything else is written owner/repo#N. */
export const REPO_OWNER: Record<string, string> = {
  'samcloud': 'samtg-core', 'samcloud-services': 'samtg-core', 'samcloud-evals': 'samtg-core',
  'image-service': 'samtg-core', 'compute-watch': 'samtg-core',
  'unity-service': 'samtg-projects', 'tsplat-service': 'samtg-projects', 'scene-render': 'samtg-projects',
  'asset-registry': 'samtg-projects', 'compute-jobs': 'samtg-projects', 'nerfstudio-api': 'samtg-projects',
  '4dgs-pipeline': 'samtg-projects', 'gsplat-unity': 'samtg-projects', 'unity-helper-tools': 'samtg-projects',
  'immersive-asset-services': 'samtg-projects', 'asset-hosting-client': 'samtg-projects',
  'radiance-playground': 'samtg-projects', 'flowers-samtg': 'samtg-projects', 'MetalSplatter': 'samtg-projects',
};

const REPO_ALT = Object.keys(REPO_OWNER).sort((a, b) => b.length - a.length).join('|');

export const REF_RE = new RegExp(
  String.raw`(?<![\w./-])(?<full>[\w.-]+\/[\w.-]+)#(?<fullN>\d+)\b` +
  String.raw`|(?<![\w/.-])(?<repo>${REPO_ALT})(?<sep>#|\s+#|\s+(?:pr|pull(?:\s+request)?)\s+#?)(?<repoN>\d+)\b` +
  String.raw`|\b(?<kind>ticket|doc|task)s?\s+#?(?<kindN>\d+)\b` +
  String.raw`|(?<![\w/&#])(?<!\b(?:pr|pull)\s)#(?<bareN>\d{2,})\b`,
  'gi');

export interface RefMatch { index: number; length: number; text: string; href: string }

function repoKey(name: string): string | undefined {
  return Object.keys(REPO_OWNER).find((k) => k.toLowerCase() === name.toLowerCase());
}

export function findRefs(text: string): RefMatch[] {
  const out: RefMatch[] = [];
  for (const m of text.matchAll(REF_RE)) {
    const g = m.groups ?? {};
    const at = { index: m.index ?? 0, length: m[0].length, text: m[0] };
    if (g.full) {
      out.push({ ...at, href: `https://github.com/${g.full}/pull/${g.fullN}` });
    } else if (g.repo) {
      const key = repoKey(g.repo);
      if (!key || (key === 'samcloud' && /^\s+#$/.test(g.sep))) {
        // "samcloud #N" is a ticket: link just the #N, as a bare reference would.
        if (g.repoN.length < 2) continue;
        const hash = m[0].lastIndexOf('#');
        out.push({ index: at.index + hash, length: m[0].length - hash, text: m[0].slice(hash), href: `/tickets/${g.repoN}` });
        continue;
      }
      out.push({ ...at, href: `https://github.com/${REPO_OWNER[key]}/${key}/pull/${g.repoN}` });
    } else {
      const kind = g.kind ? g.kind.toLowerCase() : 'ticket';
      out.push({ ...at, href: `/${KIND_PATH[kind]}/${g.kindN ?? g.bareN}` });
    }
  }
  return out;
}

// Minimal mdast shapes — enough to walk and splice without a types dependency.
interface MdNode { type: string; value?: string; url?: string; children?: MdNode[] }

const SKIP = new Set(['link', 'linkReference', 'inlineCode', 'code', 'definition', 'html']);

function splitText(node: MdNode): MdNode[] {
  const value = node.value ?? '';
  const refs = findRefs(value);
  if (!refs.length) return [node];
  const parts: MdNode[] = [];
  let at = 0;
  for (const r of refs) {
    if (r.index > at) parts.push({ type: 'text', value: value.slice(at, r.index) });
    parts.push({ type: 'link', url: r.href, children: [{ type: 'text', value: r.text }] });
    at = r.index + r.length;
  }
  if (at < value.length) parts.push({ type: 'text', value: value.slice(at) });
  return parts;
}

function walk(node: MdNode) {
  if (!node.children || SKIP.has(node.type)) return;
  const next: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === 'text') next.push(...splitText(child));
    else { walk(child); next.push(child); }
  }
  node.children = next;
}

/** remark plugin: link plane references in text nodes. */
export function remarkRefLinks() {
  return (tree: MdNode) => walk(tree);
}
