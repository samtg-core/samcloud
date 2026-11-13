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
 */

const KIND_PATH: Record<string, string> = { ticket: 'tickets', doc: 'docs', task: 'tasks' };

export const REF_RE = /\b(ticket|doc|task)s?\s+#?(\d+)\b|(?<![\w/&#])(?<!\b(?:pr|pull)\s)#(\d{2,})\b/gi;

export interface RefMatch { index: number; length: number; text: string; href: string }

export function findRefs(text: string): RefMatch[] {
  const out: RefMatch[] = [];
  for (const m of text.matchAll(REF_RE)) {
    const kind = m[1] ? m[1].toLowerCase() : 'ticket';
    const id = m[2] ?? m[3];
    out.push({ index: m.index ?? 0, length: m[0].length, text: m[0], href: `/${KIND_PATH[kind]}/${id}` });
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
