import type { ReactNode } from 'react'
import { uiText } from '../../utils/strings'
import Icon from '../shared/Icon'
import { usePanelDialog, type useContextLayout } from '../shared/usePanelLayout'
import SnapshotPanel from './SnapshotPanel'

export default function ProjectContext({ layout, selection, children }: {
  layout: ReturnType<typeof useContextLayout>; selection?: number; children: ReactNode
}) {
  const { docked, open, close } = layout
  const modal = usePanelDialog(docked, open)
  return <dialog ref={modal} id="project-context" className={'context-panel ' + (docked ? 'context-rail' : 'drawer-shell')}
    aria-label={uiText('Project context')}
    onCancel={e => { e.preventDefault(); close() }}>
    <header className="panel-head"><h2><Icon name="panel" />{uiText('Project context')}</h2>
      <button className="icon-button btn-ghost" aria-label={uiText('Close')} onClick={close}><Icon name="close" /></button></header>
    <SnapshotPanel />
    {selection !== undefined && <div className="context-section"><h3><Icon name="select" />{uiText('Current selection')}</h3>
      <span className="chip">{uiText('{0} selected in Revit', selection)}</span></div>}
    <div className="context-section">{children}</div>
  </dialog>
}
