import { uiText } from '../../../utils/strings'
import { useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import type { ToolParamDraft, WorkbenchApi, WorkbenchTool } from '../../../types/hostv2'
import { defaultOf, fieldValue, saveDisabledReason, WorkbenchModel, type Action, type ActionState, type OnRun } from './model'
import { diagnosticRange, dryRunNotSent, indentSelection, type Diagnostic, type DryRunReply } from './results'
import { downloadYaml } from './download'
import './workbench.css'

/* canEdit / editNote: whether this host lets the page save, delete and import tools (a cloud
   host wants the admin password) and why not. runPanel: what the host shows for a run it was
   asked for (its spec card, confirmation, result), placed under the action buttons.
   onDraftChange: called on every change to the draft or its values, so the host can drop what
   it built from the draft as it was. runLocked: the host is running something Run started;
   Run is off until it has its result. */
interface Props {
  api: WorkbenchApi
  onRun: OnRun
  initialDescription?: string
  canEdit?: boolean
  editNote?: string
  runPanel?: ReactNode
  onDraftChange?: () => void
  runLocked?: boolean
}

// Text fields keep what is typed; the model reads numbers from it when the value is used.
const boolChoice = (text: string) => text === '' ? undefined : text === 'true'
const shown = (value: unknown) => value === undefined || value === null ? '' : String(value)
const RUN_LOCKED = 'A run is in progress; wait for its result.'

const actionLabels: Record<Action, string> = {
  check: 'Check', dryRun: 'Dry run', run: 'Run', save: 'Save', list: 'Tool list', load: 'Open',
  choices: 'Model options', remove: 'Delete', export: 'Export', import: 'Import',
}

function ActionStatus({ action, state }: { action: Action; state?: ActionState }) {
  if (!state || state.status === 'idle') return null
  return <div className={`hostv2-workbench-status is-${state.status}`} role={state.status === 'error' ? 'alert' : 'status'}>
    <p><strong>{uiText(actionLabels[action])}: </strong>{uiText(state.message)}</p>
    {state.details && <ul>{state.details.map((line, index) => <li key={index}>{line}</li>)}</ul>}
  </div>
}

function Confirm({ label, question, yes, onYes, onNo }: { label: string; question: string; yes: string; onYes: () => void; onNo: () => void }) {
  return <div className="hostv2-workbench-delete" role="group" aria-label={label}>
    <p>{question}</p>
    <button type="button" className="btn-primary" onClick={onYes}>{yes}</button>
    <button type="button" className="btn-ghost" onClick={onNo}>{uiText("Cancel")}</button>
  </div>
}

export function DiagnosticList({ diagnostics, code, locate }: { diagnostics: Diagnostic[]; code: string; locate: (diagnostic: Diagnostic) => void }) {
  if (!diagnostics.length) return null
  return <ul className="hostv2-workbench-diagnostics">
    {diagnostics.map((diagnostic, index) => <li key={index} className={`is-${diagnostic.severity}`}>
      <button type="button" className="btn-ghost" disabled={!diagnosticRange(code, diagnostic)} onClick={() => locate(diagnostic)}>
        {diagnostic.file ? `${diagnostic.file}: ` : ''}
        {diagnostic.line === null ? uiText("Location unavailable") : uiText("Line {0}, column {1}", diagnostic.line, diagnostic.column ?? '?')}
        {diagnostic.in_wrapper ? uiText(" (wrapper)") : ''}
      </button>
      <span>{diagnostic.severity} {diagnostic.id}: {diagnostic.message}</span>
    </li>)}
  </ul>
}

function DryRunResult({ result }: { result: DryRunReply }) {
  const notSent = dryRunNotSent(result)
  return <section className="hostv2-workbench-result" aria-label={uiText("Dry run result")}>
    <p className={`hostv2-workbench-rollback ${result.rolled_back === true ? 'is-safe' : notSent ? 'is-not-sent' : 'is-unknown'}`}>
      {result.rolled_back === true ? uiText("Model was not changed. The dry run was rolled back.")
        : notSent ? uiText("Nothing was sent to Revit ({0}): the code did not run, so the model is as it was.", result.error)
        : 'Rollback is not confirmed. Check the model before continuing.'}
    </p>
    <h3>{uiText("What would change")}</h3>
    <dl className="hostv2-workbench-changes">
      {(['added', 'modified', 'deleted'] as const).map(kind => {
        const part = result.changes?.[kind]
        const count = part?.count
        return <div key={kind}><dt>{uiText(kind)}</dt><dd>{typeof count === 'number' && Number.isFinite(count) ? count : uiText("unknown")}</dd></div>
      })}
    </dl>
    {result.changes?.note && <p className="hostv2-workbench-help">{result.changes.note}</p>}
    {result.changes && <details><summary>{uiText("Changed categories and element IDs")}</summary><pre>{JSON.stringify(result.changes, null, 2)}</pre></details>}
    <h3>{uiText("Return value")}</h3><pre>{JSON.stringify(result.result, null, 2)}</pre>
    {result.hint && <p>{result.hint}</p>}
    {result.failures.length > 0 && <details open><summary>{uiText("Revit failures")}</summary><pre>{JSON.stringify(result.failures, null, 2)}</pre></details>}
    {result.warnings.length > 0 && <ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
  </section>
}

export function WorkbenchView({ model, onRun, runPanel, runLocked = false }: { model: WorkbenchModel; onRun: OnRun; runPanel?: ReactNode; runLocked?: boolean }) {
  const state = useSyncExternalStore(model.subscribe, model.getSnapshot, model.getSnapshot)
  const { draft, builtin, actions } = state
  const own = state.tools.filter(tool => !tool.builtin)
  const builtins = state.tools.filter(tool => tool.builtin)
  const codeRef = useRef<HTMLTextAreaElement>(null)
  const linesRef = useRef<HTMLDivElement>(null)
  const id = useId()
  const busy = (action: Action) => actions[action]?.status === 'running'
  const saveReason = saveDisabledReason(state)
  const editParam = (index: number, patch: Partial<ToolParamDraft>) => model.edit({ parameters: draft.parameters.map((param, i) => i === index ? { ...param, ...patch } : param) })
  const setDefault = (index: number, value: unknown) => model.setDefault(index, value)
  const locate = (diagnostic: Diagnostic) => {
    const range = diagnosticRange(draft.code, diagnostic)
    const editor = codeRef.current
    if (!range || !editor) return
    editor.focus()
    editor.setSelectionRange(...range)
    editor.scrollTop = Math.max(0, (diagnostic.line! - 2) * 20)
    if (linesRef.current) linesRef.current.scrollTop = editor.scrollTop
  }

  return <section className="hostv2-workbench" aria-label={uiText("Tool workbench")}>
    <div className="hostv2-workbench-heading">
      <h2>{uiText("Tool workbench")}</h2>
      <button type="button" className="btn-secondary" onClick={() => model.newDraft()}>{uiText("New tool")}</button>
      <button type="button" className="btn-secondary" onClick={() => model.copyAsMine()} disabled={!draft.code && !draft.name}>{uiText("Copy as mine")}</button>
    </div>
    {builtin && <p className="hostv2-workbench-readonly">{uiText("Built-in tool: read-only. Choose Copy as mine to edit it.")}</p>}
    <fieldset disabled={builtin} className="hostv2-workbench-metadata">
      <legend>{uiText("Tool details")}</legend>
      <label>{uiText("Name")}<input className="input-field" value={draft.name} onChange={event => model.edit({ name: event.target.value })} /></label>
      <label>{uiText("Description")}<input className="input-field" value={draft.description} onChange={event => model.edit({ description: event.target.value })} /></label>
      <label>{uiText("Validator (JSON object, optional)")}<input className="input-field" placeholder={uiText('Example validator: {"kind":"created_ids","category":"OST_Walls"}')} value={draft.validator ?? ''} onChange={event => model.edit({ validator: event.target.value || undefined })} /></label>
    </fieldset>

    <label htmlFor={`${id}-code`} className="label-text">{uiText("C# code")}</label>
    <div className="hostv2-workbench-editor">
      <div className="hostv2-workbench-lines" aria-hidden="true" ref={linesRef}>{draft.code.split('\n').map((_, index) => index + 1).join('\n')}</div>
      <textarea id={`${id}-code`} ref={codeRef} value={draft.code} readOnly={builtin} spellCheck={false}
        aria-describedby={`${id}-editor-help`} wrap="off" onChange={event => model.edit({ code: event.target.value })}
        onScroll={event => { if (linesRef.current) linesRef.current.scrollTop = event.currentTarget.scrollTop }}
        onKeyDown={event => {
          if (event.key !== 'Tab' || event.shiftKey || event.nativeEvent.isComposing || builtin) return
          event.preventDefault()
          const editor = event.currentTarget
          const next = indentSelection(draft.code, editor.selectionStart, editor.selectionEnd)
          model.edit({ code: next.code })
          requestAnimationFrame(() => editor.setSelectionRange(next.start, next.end))
        }} />
    </div>
    <p id={`${id}-editor-help`} className="hostv2-workbench-help">{uiText("Tab inserts two spaces. Shift+Tab moves focus out of the editor.")}</p>

    <fieldset disabled={builtin} className="hostv2-workbench-parameters">
      <legend>{uiText("Parameter declarations")}</legend>
      {draft.parameters.map((param, index) => <div className="hostv2-workbench-param" key={index}>
        <label>{uiText("Name")}<input className="input-field" value={param.name} onChange={event => editParam(index, { name: event.target.value })} /></label>
        <label>{uiText("Type")}<select className="input-field" value={param.type} onChange={event => editParam(index, { type: event.target.value as ToolParamDraft['type'] })}>
          {['string', 'double', 'int', 'bool'].map(type => <option key={type}>{type}</option>)}
        </select></label>
        <label>{uiText("Description")}<input className="input-field" value={param.description} onChange={event => editParam(index, { description: event.target.value })} /></label>
        <label>{uiText("Source")}<select className="input-field" value={param.source} onChange={event => editParam(index, { source: event.target.value as ToolParamDraft['source'], category: undefined })}>
          {['designer', 'levels', 'family_types', 'elements'].map(source => <option key={source}>{source}</option>)}
        </select></label>
        {(param.source === 'family_types' || param.source === 'elements') && <label>{uiText("Category")}<input className="input-field" placeholder="OST_Walls" value={param.category ?? ''} onChange={event => editParam(index, { category: event.target.value })} /></label>}
        <label>{uiText("Unit")}<select className="input-field" value={param.unit ?? ''} onChange={event => editParam(index, { unit: event.target.value ? event.target.value as ToolParamDraft['unit'] : undefined })}>
          <option value="">{uiText("None")}</option>{['mm', 'm', 'feet'].map(unit => <option key={unit}>{unit}</option>)}
        </select></label>
        {/* A default makes the parameter optional (the package refuses a required one with a default). */}
        <label>{uiText("Default")}{param.type === 'bool'
          ? <select className="input-field" value={shown(param.default)} onChange={event => setDefault(index, boolChoice(event.target.value))}>
              <option value="">{param.default === null ? uiText("null (the saved default)") : uiText("No default")}</option><option value="true">true</option><option value="false">false</option>
            </select>
          : <input className="input-field" value={shown(param.default)} placeholder={param.default === null ? uiText("null (the saved default)") : undefined}
              onChange={event => setDefault(index, event.target.value === '' ? undefined : event.target.value)} />}
        </label>
        <label className="hostv2-workbench-checkbox"><input type="checkbox" checked={param.required} onChange={event => editParam(index, { required: event.target.checked })} />{uiText("Required")}</label>
        <button type="button" className="btn-ghost danger" aria-label={uiText("Remove parameter {0}", param.name || index + 1)} onClick={() => model.edit({ parameters: draft.parameters.filter((_, i) => i !== index) })}>{uiText("Remove")}</button>
      </div>)}
      <button type="button" className="btn-secondary" onClick={() => model.edit({ parameters: [...draft.parameters, { name: '', type: 'string', description: '', source: 'designer', required: true }] })}>{uiText("Add parameter")}</button>
    </fieldset>

    <fieldset className="hostv2-workbench-values">
      <legend>{uiText("Values for dry run and run")}</legend>
      {draft.parameters.some(param => param.source !== 'designer') && <button type="button" className="btn-secondary" disabled={busy('choices')} onClick={() => { void model.loadChoices() }}>{uiText("Load model options")}</button>}
      {draft.parameters.length === 0 && <p>{uiText("No parameters.")}</p>}
      {draft.parameters.map((param, index) => {
        // The text the field shows: the designer's as typed, else the default.
        const value = Object.hasOwn(state.values, param.name) ? state.values[param.name] : param.default
        const choices = Object.hasOwn(state.choices, param.name) ? state.choices[param.name] : []
        // Options compare by typed value: a default typed as "3" selects the option 3.
        const typed = JSON.stringify(fieldValue(param, state.values))
        const selected = choices.findIndex(choice => JSON.stringify(choice.value) === typed)
        const fallback = defaultOf(param)
        const usesDefault = param.source === 'designer' && fallback.has && shown(value).trim() === ''
        return <label key={index}>{param.name || uiText("Parameter {0}", index + 1)}{param.required ? uiText(" (required)") : ''}{param.unit ? ` (${param.unit})` : ''}
          {param.source !== 'designer'
            ? <select className="input-field" value={selected < 0 ? '' : String(selected)} onChange={event => model.setValue(param.name, event.target.value === '' ? undefined : choices[Number(event.target.value)].value)}>
                <option value="">{choices.length ? uiText("Choose a model option") : uiText("Load model options first")}</option>
                {choices.map((choice, i) => <option key={i} value={String(i)}>{choice.label}</option>)}
              </select>
            : param.type === 'bool'
              ? <select className="input-field" value={shown(value)} onChange={event => model.setValue(param.name, boolChoice(event.target.value))}>
                  <option value="">{uiText("Choose a value")}</option><option value="true">true</option><option value="false">false</option>
                </select>
              : <input className="input-field" value={shown(value)} onChange={event => model.setValue(param.name, event.target.value)} />}
          {usesDefault && <small className="hostv2-workbench-help">{uiText("default")} {JSON.stringify(fallback.value)} {uiText("will be used")}</small>}
        </label>
      })}
      <ActionStatus action="choices" state={actions.choices} />
    </fieldset>

    <div className="hostv2-workbench-actions">
      <button type="button" className="btn-secondary" disabled={busy('check')} onClick={() => { void model.check() }}>{busy('check') ? uiText("Checking...") : uiText("Check")}</button>
      <button type="button" className="btn-secondary" disabled={busy('dryRun')} onClick={() => { void model.dryRun() }}>{busy('dryRun') ? uiText("Dry-running...") : uiText("Dry run")}</button>
      <button type="button" className="btn-primary" disabled={runLocked || busy('run') || busy('dryRun')} title={runLocked ? RUN_LOCKED : undefined} onClick={() => { void model.run(onRun) }}>{busy('run') ? uiText("Requesting run...") : uiText("Run")}</button>
      <button type="button" className="btn-primary" disabled={!!saveReason || busy('save') || busy('dryRun')} title={saveReason ? uiText(saveReason) : undefined} onClick={() => { void model.save() }}>{busy('save') ? uiText("Saving...") : uiText("Save as tool")}</button>
    </div>
    {runLocked && <p className="hostv2-workbench-help">{RUN_LOCKED}</p>}
    {saveReason && <p className="hostv2-workbench-help">{uiText(saveReason)}</p>}
    {state.replace?.step === 'replace' && <Confirm label={uiText("Confirm replacing a tool")} yes={uiText("Replace {0}", state.replace.name)}
      question={uiText("A tool named {0} exists. Replace it with this draft?{1}", state.replace.name, state.replace.unseen.length
        ? uiText(" A save writes the whole file again; this page cannot see {0}, which are not kept if the saved file has them.", state.replace.unseen.join(', ')) : '')}
      onYes={() => { void model.confirmReplace() }} onNo={() => model.cancelReplace()} />}
    {state.replace?.step === 'drop' && <Confirm label={uiText("Confirm dropping parts of the saved tool")} yes="Replace and drop them"
      question={uiText("These parts of the saved {0} will not be kept: {1}.", state.replace.name, state.replace.dropped.join('; '))}
      onYes={() => { void model.confirmReplace() }} onNo={() => model.cancelReplace()} />}
    {(['check', 'dryRun', 'run', 'save'] as const).map(action => <ActionStatus key={action} action={action} state={actions[action]} />)}
    {runPanel}
    {state.check && <section aria-label={uiText("Check result")}>
      <DiagnosticList diagnostics={state.check.diagnostics} code={draft.code} locate={locate} />
      {state.check.truncated > 0 && <p>{state.check.truncated} {uiText("additional diagnostics were omitted.")}</p>}
      {[...state.check.warnings, ...state.check.hints.map(hint => hint.text)].length > 0 && <ul>{[...state.check.warnings, ...state.check.hints.map(hint => hint.text)].map((text, i) => <li key={i}>{text}</li>)}</ul>}
    </section>}
    {state.dryRun && <>
      <DiagnosticList diagnostics={state.dryRun.diagnostics} code={draft.code} locate={locate} />
      <DryRunResult result={state.dryRun} />
    </>}

    <section className="hostv2-workbench-library" aria-label={uiText("My tools")}>
      <div className="hostv2-workbench-heading"><h3>{uiText("My tools")}</h3>
        <button type="button" className="btn-secondary" disabled={busy('list')} onClick={() => { void model.refresh() }}>{uiText("Refresh")}</button>
        {state.canEdit && <label>{uiText("Import YAML")}<input type="file" accept=".yaml,.yml,application/yaml,text/yaml" disabled={busy('import')} onChange={event => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file) void model.importTool(file.text())
        }} /></label>}
      </div>
      {!state.canEdit && state.editNote && <p className="hostv2-workbench-help">{state.editNote}</p>}
      {(['list', 'load', 'remove', 'export', 'import'] as const).map(action => <ActionStatus key={action} action={action} state={actions[action]} />)}
      {state.importReplace && <Confirm label={uiText("Confirm replacing a tool")} yes={uiText("Replace {0}", state.importReplace.name)}
        question={uiText("A tool named {0} exists. Replace it with the imported file?", state.importReplace.name)}
        onYes={() => { void model.confirmImport() }} onNo={() => model.cancelImport()} />}
      {state.deleteName && <div className="hostv2-workbench-delete" role="group" aria-label={uiText("Confirm deletion")}>
        <p>{uiText("Delete")} {state.deleteName}?</p>
        <button type="button" className="btn-primary" disabled={busy('remove')} onClick={() => { void model.confirmDelete() }}>{uiText("Confirm delete")}</button>
        <button type="button" className="btn-ghost" disabled={busy('remove')} onClick={() => model.cancelDelete()}>{uiText("Cancel")}</button>
      </div>}
      {own.length === 0 && !busy('list') && <p>{uiText("No tools of your own yet.")}</p>}
      <ToolList tools={own} model={model} busy={busy} canDelete={state.canEdit} />
      {builtins.length > 0 && <details className="hostv2-workbench-builtins">
        <summary>{uiText("Built-in tools (")}{builtins.length}{uiText(", read-only: open to read or copy, export)")}</summary>
        <ToolList tools={builtins} model={model} busy={busy} canDelete={false} />
      </details>}
    </section>
  </section>
}

function ToolList({ tools, model, busy, canDelete }: { tools: WorkbenchTool[]; model: WorkbenchModel; busy: (action: Action) => boolean; canDelete: boolean }) {
  if (!tools.length) return null
  return <ul className="hostv2-workbench-tools">{tools.map(tool => <li key={tool.name}>
    <div><strong>{tool.name}</strong>{tool.builtin && <span className="hostv2-workbench-badge">{uiText("Built-in")}</span>}
      <p>{tool.description}</p>{(tool.directory || tool.origin) && <small>{tool.directory ?? tool.origin}</small>}
    </div>
    <div className="hostv2-workbench-actions">
      <button type="button" className="btn-secondary" onClick={() => { void model.open(tool.name) }}>{uiText("Open")}</button>
      <button type="button" className="btn-secondary" disabled={busy('export')} onClick={() => { void model.exportTool(tool.name, downloadYaml) }}>{uiText("Export YAML")}</button>
      {canDelete && !tool.builtin && <button type="button" className="btn-ghost danger" disabled={busy('remove')} onClick={() => model.requestDelete(tool.name)}>{uiText("Delete")}</button>}
    </div>
  </li>)}</ul>
}

export function Workbench({ api, onRun, initialDescription, canEdit = true, editNote = '', runPanel, onDraftChange, runLocked = false }: Props) {
  const [model] = useState(() => {
    const created = new WorkbenchModel(api, initialDescription)
    created.setGate(canEdit, editNote)
    return created
  })
  useEffect(() => { model.setApi(api) }, [model, api])
  useEffect(() => { model.setGate(canEdit, editNote) }, [model, canEdit, editNote])
  useEffect(() => { model.setDraftListener(onDraftChange ?? null); return () => model.setDraftListener(null) }, [model, onDraftChange])
  useEffect(() => { void model.refresh() }, [model])
  return <WorkbenchView model={model} onRun={onRun} runPanel={runPanel} runLocked={runLocked} />
}

export default Workbench
