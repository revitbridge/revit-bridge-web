/* Pixel strokes go to the host, where the package reads them using the capture's frame. */

import type { ResolvedSketch, SketchCapture, SketchDraft, SketchLine, SketchPoint } from '../../types/api'
import { post } from '../shared/http'

const B = '/api/v1/bridge'

export const SKETCH_COLOUR = '#e11d2e'
export const MAX_STROKES = 8
const MAX_POINTS = 600           // a stroke is thinned to this many points before it is sent

export const captureSketchView = () => post<SketchCapture>(`${B}/sketch/capture`, {})

export const resolveSketch = (captureId: string, strokes: number[][][], level?: string) =>
  post<ResolvedSketch>(`${B}/sketch/resolve`, { capture_id: captureId, strokes, level })

/* The stroke with at most MAX_POINTS points, the first and the last kept, rounded to a tenth. */
export function thinned(stroke: number[][]): number[][] {
  const kept = stroke.length <= MAX_POINTS ? stroke : Array.from({ length: MAX_POINTS },
    (_, index) => stroke[Math.round(index * (stroke.length - 1) / (MAX_POINTS - 1))])
  return kept.map(([x, y]) => [Math.round(x * 10) / 10, Math.round(y * 10) / 10])
}

/* The pointer position as a pixel of the capture, whatever size the picture is shown at. */
export function capturePixel(clientX: number, clientY: number, box: { left: number; top: number; width: number; height: number },
  width: number, height: number): number[] {
  const x = (clientX - box.left) / (box.width || 1) * width
  const y = (clientY - box.top) / (box.height || 1) * height
  return [Math.min(width, Math.max(0, x)), Math.min(height, Math.max(0, y))]
}

/* One line of the host's reading in words: "Line 1: 3100 mm, from the end of wall 11 to wall 12". */
export function lineText(line: SketchLine, text: (key: string, ...values: unknown[]) => string): string {
  const where = ({ x, y, snap }: SketchPoint) => {
    switch (snap?.kind) {
      case 'wall_end': return text("the end of wall {0}", snap.wall_id)
      case 'on_wall': return text("wall {0}", snap.wall_id)
      case 'grid_intersection': return text("the crossing of grids {0} and {1}", ...snap.grids)
      case 'column': return text("column {0}", snap.element_id)
      case 'element_end': return text("the end of element {0}", snap.element_id)
      case 'on_grid': return text("grid {0}", snap.grid)
      default: return `(${x}, ${y})`
    }
  }
  if (line.closed) return text("Line {0}: {1} mm, closed, {2} segments", line.line, line.length_mm, line.points.length)
  const first = line.points[0], last = line.points[line.points.length - 1]
  if (line.points.length > 2) return text("Line {0}: {1} mm, {2} segments, from {3} to {4}",
    line.line, line.length_mm, line.points.length - 1, where(first), where(last))
  return text("Line {0}: {1} mm, from {2} to {3}", line.line, line.length_mm, where(first), where(last))
}

/* The longest picture (base64) that goes with a message: the host takes 1.3 MB of PNG, and a
   reverse proxy in front of a cloud host commonly takes a 2 MB body. A larger one stays on
   the page: the coordinates, which are what the model works from, go without it. */
export const MAX_IMAGE_CHARS = 1_700_000

/* What goes with the message: the strokes, and the capture with the strokes drawn on it. */
export const sketchRequest = (draft: SketchDraft) => ({
  capture_id: draft.capture_id, strokes: draft.strokes,
  ...(draft.level !== undefined ? { level: draft.level } : {}),
  ...(draft.image && draft.image.length <= MAX_IMAGE_CHARS ? { image: draft.image } : {}),
})
