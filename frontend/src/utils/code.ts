/* Pull the C# the model proposed out of its reply. */

const FENCE = /```(?:csharp|cs|c#)?\s*\n([\s\S]*?)```/gi

/* Last fenced C# block; a still-open fence during streaming is returned as-is. */
export function extractCSharp(markdown: string): string {
  let last = ''
  for (const match of markdown.matchAll(FENCE)) last = match[1]
  if (last) return last.trim()
  const open = markdown.match(/```(?:csharp|cs|c#)?\s*\n([\s\S]*)$/i)
  return open ? open[1].trim() : ''
}
