/* Models by role, read-only: the model name each role runs on (features.models), where
   the names come from - the host's models file, or the environment and this browser - and
   what the chat runs by default (features.roles, features.chatModeDefault): planner +
   supervisor, the analyst too, or one loop. The host publishes names only: no address, no
   key, no path of its file system. */

import { uiText } from '../../utils/strings'
import type { RoleModels as Models } from '../../config'
import { MODELS_FROM_ENV, MODELS_FROM_FILE, roleRows, rolesLine } from './models'

interface Props {
  models: Models
  modelsFile: boolean
  roles: boolean
  chatModeDefault?: string | null
}

export default function RoleModels({ models, modelsFile, roles, chatModeDefault }: Props) {
  return (
    <section className="card section">
      <h3 className="heading-display section-title">{uiText("Models by role")}</h3>
      <p className="section-copy">{uiText(modelsFile ? MODELS_FROM_FILE : MODELS_FROM_ENV)}</p>
      <div className="table-scroll">
        <table className="evidence-table role-models">
          <thead><tr><th>{uiText("Role")}</th><th>{uiText("Model")}</th></tr></thead>
          <tbody>
            {roleRows(models).map(row => (
              <tr key={row.role} data-role={row.role}>
                <td>{uiText(row.label)} <span className="section-copy small">({uiText(row.does)})</span></td>
                <td className="mono">{row.model === 'not set' ? uiText('not set') : row.model}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="section-copy small">{uiText(rolesLine(roles, chatModeDefault))}</p>
    </section>
  )
}
