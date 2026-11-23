export interface ToolParamDraft {
  name: string
  type: 'string' | 'double' | 'int' | 'bool'
  description: string
  source: 'designer' | 'levels' | 'family_types' | 'elements'
  category?: string
  unit?: 'mm' | 'm' | 'feet'
  required: boolean
  default?: unknown
}

export interface ToolDraft {
  name: string
  description: string
  code: string
  parameters: ToolParamDraft[]
  validator?: string
}

export interface WorkbenchApi {
  check(code: string, parameters: ToolParamDraft[]): Promise<unknown>
  dryRun(code: string, parameters: ToolParamDraft[], values: Record<string, unknown>): Promise<unknown>
  choices(param: ToolParamDraft): Promise<{ label: string; value: unknown }[]>
  save(draft: ToolDraft): Promise<{ name: string }>
  list(): Promise<{ name: string; description: string; builtin: boolean; origin?: string }[]>
  load(name: string): Promise<ToolDraft>
  remove(name: string): Promise<void>
  exportYaml(name: string): Promise<string>
  importYaml(text: string): Promise<{ name: string }>
}
