/* The designer's own tools on the workbench page: which ones are listed, and the import
   that asks before it replaces one. */

import type { ImportResult, WorkbenchTool, workbenchApi } from '../../api/workbench'
import { BridgeApiError } from '../shared/http'

/* The designer's own tools: built-in packs are read-only and stay on the Packs page. */
export const ownTools = (tools: WorkbenchTool[]) => tools.filter(t => !t.builtin)

/* Import a pack file; when a tool of that name is yours already, ask before replacing it.
   null: the designer kept the one there. */
export async function importPack(api: Pick<typeof workbenchApi, 'importYaml'>, text: string,
                                 confirmReplace: (name: string) => boolean): Promise<ImportResult | null> {
  try {
    return await api.importYaml(text)
  } catch (e: unknown) {
    if (!(e instanceof BridgeApiError) || e.code !== 'pack_exists') throw e
    const name = typeof e.body?.tool === 'string' ? e.body.tool : 'this tool'
    if (!confirmReplace(name)) return null
    return api.importYaml(text, { overwrite: true })
  }
}
