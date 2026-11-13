'use client';

import { Markdown } from '@/components/Markdown';

// Card previews render markdown, soft-clamped
// by height since -webkit-line-clamp can't survive nested block tags.
export function MdSnippet({ text, strong = false }: { text: string; strong?: boolean }) {
  if (!text) return null;
  return (
    <div className={`mt-1 max-h-11 overflow-hidden ${strong ? '' : 'opacity-80'} [&_*]:!text-xs [&_*]:!leading-snug [&_p]:!my-0.5 [&_pre]:!my-0.5 [&_ul]:!my-0.5 [&_ol]:!my-0.5 [&_img]:!max-h-8 [&_img]:!my-0`}>
      <Markdown muted={!strong} flatLinks>{text}</Markdown>
    </div>
  );
}
