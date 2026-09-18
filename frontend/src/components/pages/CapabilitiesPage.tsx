/* Capabilities: packs (parameterised C# that worked before) and skills (protocol text). */

import { useState } from 'react'
import SkillsView from '../capabilities/SkillsView'
import ToolLibrary from '../capabilities/ToolLibrary'

export default function CapabilitiesPage() {
  const [view, setView] = useState<'packs' | 'skills'>('packs')
  return (
    <div className="page">
      <div className="subtab-bar">
        <button className={`subtab${view === 'packs' ? ' is-active' : ''}`} onClick={() => setView('packs')}>Packs</button>
        <button className={`subtab${view === 'skills' ? ' is-active' : ''}`} onClick={() => setView('skills')}>Skills</button>
      </div>
      {view === 'packs' ? <ToolLibrary /> : <SkillsView />}
    </div>
  )
}
