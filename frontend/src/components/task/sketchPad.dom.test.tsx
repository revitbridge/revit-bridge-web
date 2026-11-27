// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ResolvedSketch, SketchCapture, SketchDraft } from '../../types/api'
import SketchPad from './SketchPad'
import { MAX_IMAGE_CHARS, capturePixel, lineText, sketchRequest, thinned } from './sketch'

const container = document.createElement('div')
let root: ReturnType<typeof createRoot>
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('navigator', { languages: ['en-US'] })
  // jsdom has no canvas and no modal dialog.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
  document.body.append(container)
  root = createRoot(container)
})
afterEach(() => {
  act(() => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const shot: SketchCapture = { capture_id: 'ab'.repeat(16), png_base64: 'AAAA', width: 1600, height: 800, view_name: 'L1', level: 'L1' }
const reading = (count: number): ResolvedSketch => ({
  view_name: 'L1', view_type: 'FloorPlan', level: 'L1', elevation_mm: 0, tolerance_mm: 250, warnings: [],
  lines: Array.from({ length: count }, (_, index) => ({ line: index + 1, length_mm: 3000, pixels: [[300, 100], [600, 100]],
    points: [{ x: -3000, y: 5000, snap: { kind: 'wall_end' as const, wall_id: 11 } }, { x: 0, y: 5000, snap: { kind: 'on_wall' as const, wall_id: 12 } }] })),
})
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })
const button = (text: string) => [...document.querySelectorAll('button')].find(b => b.textContent === text) as HTMLButtonElement

function draw(points: number[][]) {
  const surface = document.querySelector('.sketch-pad-surface')!
  const fire = (type: string, [x, y]: number[]) => act(() => {
    const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 })
    Object.defineProperty(event, 'pointerId', { value: 1 })
    surface.dispatchEvent(event)
  })
  fire('pointerdown', points[0])
  points.slice(1).forEach(point => fire('pointermove', point))
  fire('pointerup', points[points.length - 1])
}

it('captures the view, shows what the host read of each stroke, and hands the sketch over', async () => {
  const capture = vi.fn(async () => shot)
  const resolve = vi.fn(async (_id: string, strokes: number[][][]) => reading(strokes.length))
  const used = vi.fn<(draft: SketchDraft) => void>()
  act(() => root.render(<SketchPad onUse={used} onClose={() => {}} capture={capture} resolve={resolve} />))
  expect(document.body.textContent).toContain('Capturing the view from Revit...')
  await flush()
  expect(capture).toHaveBeenCalledTimes(1)
  expect(document.querySelector('.sketch-pad-view')?.textContent).toBe('L1 · L1')
  expect(button('Use this sketch').disabled).toBe(true)
  // The picture is shown at half size: a pointer at (155, 54) is pixel (310, 108) of the capture.
  vi.spyOn(HTMLImageElement.prototype, 'getBoundingClientRect').mockReturnValue(
    { left: 0, top: 0, width: 800, height: 400, right: 800, bottom: 400, x: 0, y: 0, toJSON: () => ({}) })
  draw([[155, 54], [200, 50], [295, 45]])
  await flush()
  expect(resolve).toHaveBeenLastCalledWith(shot.capture_id, [[[310, 108], [400, 100], [590, 90]]])
  expect(document.body.textContent).toContain('Line 1: 3000 mm, from the end of wall 11 to wall 12')
  expect(document.querySelectorAll('.sketch-read')).toHaveLength(1)
  expect(document.querySelectorAll('.sketch-snap')).toHaveLength(2)
  draw([[100, 300], [300, 300]])
  await flush()
  expect(resolve.mock.calls.at(-1)![1]).toHaveLength(2)
  act(() => button('Undo').click())
  await flush()
  expect(resolve.mock.calls.at(-1)![1]).toHaveLength(1)
  act(() => button('Use this sketch').click())
  expect(used).toHaveBeenCalledTimes(1)
  const draft = used.mock.calls[0][0]
  expect(draft).toMatchObject({ capture_id: shot.capture_id, view_name: 'L1', image: '' })
  expect(draft.strokes).toEqual([[[310, 108], [400, 100], [590, 90]]])
  expect(draft.lines).toHaveLength(1)
})

it('takes back a stroke the host could not read, and says why a view cannot be captured', async () => {
  const resolve = vi.fn(async () => { throw new Error('A line is too short or too tangled to read; draw it again') })
  act(() => root.render(<SketchPad onUse={() => {}} onClose={() => {}} capture={async () => shot} resolve={resolve} />))
  await flush()
  vi.spyOn(HTMLImageElement.prototype, 'getBoundingClientRect').mockReturnValue(
    { left: 0, top: 0, width: 1600, height: 800, right: 1600, bottom: 800, x: 0, y: 0, toJSON: () => ({}) })
  draw([[10, 10], [12, 11]])
  await flush()
  expect(document.querySelector('[role=alert]')?.textContent).toContain('too short or too tangled')
  expect(document.querySelectorAll('.sketch-stroke')).toHaveLength(0)
  expect(button('Use this sketch').disabled).toBe(true)

  act(() => root.unmount())
  root = createRoot(container)
  const refused = vi.fn(async () => { throw new Error('Sketching needs a plan view') })
  act(() => root.render(<SketchPad onUse={() => {}} onClose={() => {}} capture={refused} resolve={resolve} />))
  await flush()
  expect(document.querySelector('[role=alert]')?.textContent).toContain('Sketching needs a plan view')
  expect(document.querySelector('.sketch-pad-surface')).toBeNull()
})

it('converts pointers to capture pixels, thins long strokes, and words a line', () => {
  expect(capturePixel(400, 200, { left: 0, top: 0, width: 800, height: 400 }, 1600, 800)).toEqual([800, 400])
  expect(capturePixel(-5, 999, { left: 0, top: 0, width: 800, height: 400 }, 1600, 800)).toEqual([0, 800])
  const long = Array.from({ length: 5000 }, (_, index) => [index / 10 + 0.04, 7])
  const thin = thinned(long)
  expect(thin.length).toBeLessThanOrEqual(601)
  expect(thin[0]).toEqual([0, 7])
  expect(thin.at(-1)).toEqual([499.9, 7])
  const words = (key: string, ...values: unknown[]) => key.replace(/\{(\d)\}/g, (_, index) => String(values[Number(index)]))
  expect(lineText({ line: 2, length_mm: 1200, pixels: [], points: [{ x: 10, y: 20 }, { x: 1210, y: 20, snap: { kind: 'on_wall', wall_id: 7 } }] }, words))
    .toBe('Line 2: 1200 mm, from (10, 20) to wall 7')
  expect(sketchRequest({ capture_id: 'c', strokes: [[[1, 2], [3, 4]]], image: '', view_name: 'L1', lines: [] }))
    .toEqual({ capture_id: 'c', strokes: [[[1, 2], [3, 4]]] })
  expect(sketchRequest({ capture_id: 'c', strokes: [], image: 'AAAA', view_name: 'L1', lines: [] }).image).toBe('AAAA')
})

it('keeps the strokes as they were when an undo cannot be read, and leaves a too large picture out', async () => {
  let fail = false
  const resolve = vi.fn(async (_id: string, strokes: number[][][]) => {
    if (fail) throw new Error('Revit is busy')
    return reading(strokes.length)
  })
  act(() => root.render(<SketchPad onUse={() => {}} onClose={() => {}} capture={async () => shot} resolve={resolve} />))
  await flush()
  vi.spyOn(HTMLImageElement.prototype, 'getBoundingClientRect').mockReturnValue(
    { left: 0, top: 0, width: 1600, height: 800, right: 1600, bottom: 800, x: 0, y: 0, toJSON: () => ({}) })
  draw([[310, 108], [590, 90]])
  await flush()
  draw([[100, 300], [300, 300]])
  await flush()
  expect(document.querySelectorAll('.sketch-stroke')).toHaveLength(2)
  fail = true
  act(() => button('Undo').click())
  await flush()
  expect(document.querySelector('[role=alert]')?.textContent).toContain('Revit is busy')
  expect(document.querySelectorAll('.sketch-stroke')).toHaveLength(2)      // both strokes are still there
  expect(document.querySelectorAll('.sketch-read')).toHaveLength(2)        // and so is what was read of them

  const draft = { capture_id: 'c', strokes: [[[1, 2], [3, 4]]], view_name: 'L1', lines: [] }
  expect(sketchRequest({ ...draft, image: 'A'.repeat(MAX_IMAGE_CHARS) }).image).toHaveLength(MAX_IMAGE_CHARS)
  expect('image' in sketchRequest({ ...draft, image: 'A'.repeat(MAX_IMAGE_CHARS + 1) })).toBe(false)
})
