/* Save an exported pack file in the browser. */

export const REVOKE_AFTER_MS = 60_000

export function downloadYaml(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/yaml;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${name.replace(/[^A-Za-z0-9_.-]/g, '_') || 'tool'}.yaml`
  document.body.appendChild(anchor)
  try { anchor.click() }
  // Revoked once the browser has taken the download, not before (some start it asynchronously).
  finally { anchor.remove(); setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS) }
}
