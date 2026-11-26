/* Capabilities: packs (parameterised C# that worked before), the workbench (where code
   becomes a tool) and skills (protocol text). */

import { uiText } from '../../utils/strings'
import { useState } from 'react'
import type { CapabilitiesView } from '../../route'
import SkillsView from '../capabilities/SkillsView'
import AuthoringPage from '../capabilities/AuthoringPage'
import useHostFeatures from '../shared/useHostFeatures'
import ToolLibrary from '../capabilities/ToolLibrary'
import WorkbenchPage from '../capabilities/WorkbenchPage'

const VIEWS: { id: CapabilitiesView; label: string }[] = [
  { id: 'packs', label: 'Packs' }, { id: 'workbench', label: 'Workbench' }, { id: 'skills', label: 'Skills' },
]

/* `view` / `onView`: the sub-page, held by App so /capabilities/workbench can open it; on its
   own the page keeps it itself. */
export default function CapabilitiesPage({ view: shown, onView, onRemote, brief }: { onRemote?: () => void; brief?: string; view?: CapabilitiesView; onView?: (view: CapabilitiesView) => void }) {
  const [own, setOwn] = useState<CapabilitiesView>(shown ?? 'packs')
  const features = useHostFeatures()
  const view = shown ?? own
  const select = (next: CapabilitiesView) => { setOwn(next); onView?.(next) }
  return (
    <div className="page">
      <div className="subtab-bar">
        {(features.authoring ? [...VIEWS, { id: 'authoring' as const, label: uiText("Author tools") }] : VIEWS).map(v => (
          <button key={v.id} className={`subtab${view === v.id ? ' is-active' : ''}`} onClick={() => select(v.id)}>{uiText(v.label)}</button>
        ))}
        <button className="subtab" onClick={onRemote}>{uiText("Remote MCP")}</button>
      </div>
      {view === 'packs' && <ToolLibrary />}
      {view === 'workbench' && <WorkbenchPage />}
      {view === 'skills' && <SkillsView />}
      {view === 'authoring' && (features.authoring ? <AuthoringPage brief={brief} /> : <p role="status">{uiText("Tool authoring requires a local host or a verified administrator.")}</p>)}
    </div>
  )
}
