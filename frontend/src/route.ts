/* The page's addresses. The tabs live at '/'; the workbench also has a path of its own,
   /capabilities/workbench, so a link opens it - the one the chat model writes when no saved
   tool fits (a relative Markdown link, opened in a new tab), a bookmark, a typed address. The
   Token page is /token. The server answers every such path with the page itself. */

export const WORKBENCH_PATH = '/capabilities/workbench'
export const TOKEN_PATH = '/token'

export const TABS = ['Connect', 'Task', 'Capabilities', 'Evidence', 'Token'] as const
export const CAPABILITIES_TAB = 2
export const TOKEN_TAB = 4

export type CapabilitiesView = 'packs' | 'workbench' | 'skills'

export interface View {
  tab: number
  capabilities: CapabilitiesView
}

/* What a path opens: the workbench or the Token page for its path (a trailing slash or any
   case too), the first tab for everything else, as before. */
export function viewOf(pathname: string): View {
  const path = pathname.replace(/\/+$/, '').toLowerCase()
  if (path === WORKBENCH_PATH) return { tab: CAPABILITIES_TAB, capabilities: 'workbench' }
  if (path === TOKEN_PATH) return { tab: TOKEN_TAB, capabilities: 'packs' }
  return { tab: 0, capabilities: 'packs' }
}

/* The address a view is shown at: the workbench's and the Token page's own paths, '/' for the rest. */
export function pathOf(view: View): string {
  if (view.tab === CAPABILITIES_TAB && view.capabilities === 'workbench') return WORKBENCH_PATH
  if (view.tab === TOKEN_TAB) return TOKEN_PATH
  return '/'
}

/* The address bar's path; '/' where there is none (server rendering in tests). */
export function currentPath(): string {
  return typeof window === 'undefined' ? '/' : window.location.pathname
}

/* Show the view's address without a new history entry or a reload. */
export function showPath(view: View): void {
  if (typeof window === 'undefined') return
  const path = pathOf(view)
  if (window.location.pathname !== path) window.history.replaceState(window.history.state, '', path)
}
