/* Models by role, read-only: the model name each role runs on (features.models) and where
   the names come from - the host's models file, or the environment and this browser. The
   host publishes names only: no address, no key, no path of its file system. */

import type { RoleModels as Models } from '../../config'
import { MODELS_FROM_ENV, MODELS_FROM_FILE, roleRows } from './models'

export default function RoleModels({ models, modelsFile }: { models: Models; modelsFile: boolean }) {
  return (
    <section className="card section">
      <h3 className="heading-display section-title">Models by role</h3>
      <p className="section-copy">{modelsFile ? MODELS_FROM_FILE : MODELS_FROM_ENV}</p>
      <div className="table-scroll">
        <table className="evidence-table role-models">
          <thead><tr><th>Role</th><th>Model</th></tr></thead>
          <tbody>
            {roleRows(models).map(row => (
              <tr key={row.role} data-role={row.role}>
                <td>{row.label} <span className="section-copy small">({row.does})</span></td>
                <td className="mono">{row.model}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="section-copy small">
        Only the planner runs in this version; the analyst and supervisor rows are what the roles split will use.
        Model settings filled in below replace the planner's model for your own requests.
      </p>
    </section>
  )
}
