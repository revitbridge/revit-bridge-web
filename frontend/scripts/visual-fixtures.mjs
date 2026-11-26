// In-memory responses for visual-check.mjs. Strings in the model/project are fixture data.
export function fixtures(zh, png) {
  const t = (en, cn) => zh ? cn : en
  const view = t('Ground floor', '首层平面')
  const floor = t('Level 01', '一层')
  const model = 'studio-planner'
  const profile = { model, base_url: 'http://127.0.0.1/mock', key_set: false, key_tail: null }
  const profiles = Object.fromEntries(['analyst', 'planner', 'supervisor'].map(r => [r, profile]))
  const parameter = { name: 'height', type: 'double', unit: 'mm', required: true, description: t('Wall height', '墙体高度') }
  const packs = [
    ['list_levels', t('List levels', '列出标高'), t('Read the levels and their elevations.', '读取项目标高及其高程。')],
    ['show_floor', t('Open floor plan', '打开平面视图'), t('Open the selected level in plan.', '打开所选标高的平面视图。')],
    ['create_wall', t('Create a wall', '创建墙体'), t('Place a straight wall on the specified level.', '在指定标高上创建一道直墙。')],
    ['room_schedule', t('Room schedule', '房间明细表'), t('Summarise the rooms on this level.', '汇总本层的房间信息。')],
  ].map(([name, display_name, description], i) => ({ name, display_name, description, origin: i === 3 ? 'user' : 'builtin',
    builtin: i !== 3, read_only: i === 0, ui_only: i === 1, used: [26, 9, 14, 3][i], revit_versions: ['2025', '2026'],
    version: '1.0.0', parameters: i === 2 ? [parameter] : [], validator: null, preconditions: [] }))
  const steps = [{ id: 's1', title: packs[0].display_name, tool: 'list_levels', params: {} },
    { id: 's2', title: packs[2].display_name, tool: 'create_wall', params: { height: 3200 } }]
  const flow = { kind: 'workflow', schema_version: 1, name: 'level_wall', display_name: t('Ground floor partition', '首层隔墙'),
    summary: t('Check the levels and place a partition.', '确认标高并创建隔墙。'), description: t('Ground floor coordination study.', '首层协调设计。'),
    version: '1.0.0', steps, inputs: [] }
  const snapshot = { document: { title: t('North Court Studio', '北庭工作室'), revit_version: '2026', is_workshared: false },
    units: { raw: t('Metric', '公制'), length: 'mm' }, levels: [floor, t('Level 02', '二层')].map((name, i) => ({ id: i+1, name, elevation_mm: 3600*i })),
    grids: { count: 3, names: ['A','B','C'] }, family_types: [{ category: 'OST_Walls', count: 4, names: ['Interior 120'] }],
    selection_count: 2, selection: [{ category: 'Wall', name: 'Interior 120', id: 2108 }, { category: 'Wall', name: 'Core 200', id: 2109 }],
    active_view: { name: view, view_type: 'FloorPlan', level: floor }, warnings: [], fingerprint: 'ab74e31c',
    duration_ms: 42, taken_at: '2026-11-24 10:42' }
  const ts = '2026-11-24 10:42'
  const record = { id: 'ev_1042', ts, host: 'web', action: 'run_tool', tool: 'create_wall', confirmed_by: 'designer',
    success: true, duration_ms: 148, params: { height: 3200 }, validation: { validator: 'created_ids', passed: true, checks: [] } }
  const routes = {
    '/config.json': { features: { mode: 'local', serverModel: true, authoring: true, roles: true, models: { planner: model },
      chatModes: ['auto','single','roles','roles_analyst'], chatModeDefault: 'roles', runModes: ['confirm','auto'] } },
    '/api/v1/settings/models': { configured: true, models: profiles, saved: { shared: profile } },
    '/api/v1/settings/remote-mcp': { enabled: true, device_id: 'studio-01', tokens: [{ id: 'sample', label: 'Studio',
      created_at: ts, last_used_at: null }], calls: [] },
    '/api/usage/runs': { items: [{ run_label: 'ground-floor', mode: 'roles', first_at: ts, last_at: ts,
      calls: 3, input: 1654, output: 318, ms: 2050, cache_read: 740, cache_write: 0 }] },
  }
  const bridge = { 'revit-health': { revit_connected: true, user_tools_allowed: true, selection_state_supported: true },
    'tools/draft': { pack: { schema_version: 1, version: '1.0.0', name: '', description: t('Read the active view name.', '读取当前视图名称。'),
      parameters: [], code_template: 'var view = doc.ActiveView;\nreturn new { Name = view.Name };' },
      problems: ['name: required, non-empty string'], reference: 'query_levels', repair_rounds: 2, compiled: null, diagnostics: [] },
    check: body => ({ checked: true, compiled: body.pack.code_template.includes('document.ActiveView'),
      diagnostics: body.pack.code_template.includes('document.ActiveView') ? [] : [
        { severity: 'error', id: 'CS0103', line: 1, column: 12, message: "The name 'doc' does not exist in the current context." }] }),
    'dry-run': { success: true, rolled_back: true, result: { Name: view }, diagnostics: [], failures: [],
      changes: { added: { count: 0 }, modified: { count: 0 }, deleted: { count: 0 } } },
    snapshot, 'selection-state': { count: 2, stamp: 'selected', document_id: 'studio' }, tools: packs,
    'host-functions': [{ name: 'get_project_snapshot', description: t('Read project context.', '读取项目上下文。'), class: 'read_only', parameters: [] }],
    workflows: [flow], 'workflows/validate': { errors: [] }, evidence: [record, { ...record, id: 'ev_1031', tool: 'list_levels' }],
    'remote-trials': { trials: [{ time: ts, evidence_id: 'ev_trial', code_sha256: '6c79d96a3fa7'.repeat(5) }] },
  }
  for (const pack of packs) bridge['tools/' + pack.name] = { ...pack, tags: [], code_template: '', source_query: pack.description }
  for (const [key, value] of Object.entries(bridge)) routes['/api/v1/bridge/' + key] = value
  const spec = { task: flow.display_name, action: { kind: 'run_tool', tool: 'create_wall' },
    parameters: [{ name: 'height', value: 3200, unit: 'mm', source: 'answer', evidence: t('Designer brief', '设计要求') }] }
  const events = [
    ['stage', { role: 'planner', model, status: 'start' }],
    ['', t('I checked the selected walls. Both are on **Level 01**.', '已检查所选墙体，两者均位于**一层**。')],
    ['plan', { plan_id: 'plan_A', steps: steps.map((s, i) => ({ ...s, status: i ? 'proposed' : 'done' })) }],
    ['view_image', { capture_id: 'floor', width: 320, height: 180, view_name: view, png_base64: png }],
    ['spec', { spec, card: flow.summary, errors: [], reconcile: { ready: true, conflicts: [], questions: [], interpretations_required: [] } }],
    ['questions', { ask_id: 'direction', questions: [{ id: 'face', text: t('Which face should align?', '需要对齐哪一侧？'),
      options: [{ label: t('Core face', '核心层表面'), value: 'core' }, { label: t('Finish face', '饰面层表面'), value: 'finish' }] }] }],
    ['usage', { role: 'planner', model, input: 1654, output: 318, round: 1 }],
    ['stage', { role: 'planner', model, status: 'end' }],
  ]
  return { routes, chat: events.map(([name, data]) => (name ? 'event: ' + name + '\n' : '') +
    'data: ' + JSON.stringify(data) + '\n\n').join('') + 'data: [DONE]\n\n' }
}
