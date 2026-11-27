/* A sketch on a capture of the active plan view: the picture to draw on, and what the host
   understood of the strokes (model millimetres, snapped to walls). Nothing here converts a
   pixel to a coordinate: the host does, with the frame it kept for the capture. */

import type { ResolvedSketch, SketchCapture, SketchDraft } from '../../types/api'
import { post } from '../shared/http'

const B = '/api/v1/bridge'

export const SKETCH_COLOUR = '#e11d2e'
export const MAX_STROKES = 8
const MAX_POINTS = 600           // a stroke is thinned to this many points before it is sent

export const captureSketchView = () => post<SketchCapture>(`${B}/sketch/capture`, {})

export const resolveSketch = (captureId: string, strokes: number[][][]) =>
  post<ResolvedSketch>(`${B}/sketch/resolve`, { capture_id: captureId, strokes })

/* The stroke with at most MAX_POINTS points, the first and the last kept, rounded to a tenth. */
export function thinned(stroke: number[][]): number[][] {
  const step = Math.max(1, Math.ceil(stroke.length / MAX_POINTS))
  const kept = stroke.filter((_, index) => index % step === 0 || index === stroke.length - 1)
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
export function lineText(line: ResolvedSketch['lines'][number], text: (key: string, ...values: unknown[]) => string): string {
  const where = (point: ResolvedSketch['lines'][number]['points'][number]) =>
    point.snap?.kind === 'wall_end' ? text("the end of wall {0}", point.snap.wall_id)
      : point.snap?.kind === 'on_wall' ? text("wall {0}", point.snap.wall_id)
      : `(${point.x}, ${point.y})`
  const first = line.points[0], last = line.points[line.points.length - 1]
  return text("Line {0}: {1} mm, from {2} to {3}", line.line, line.length_mm, where(first), where(last))
}

/* What goes with the message: the strokes, and the capture with the strokes drawn on it. */
export const sketchRequest = (draft: SketchDraft) => ({
  capture_id: draft.capture_id, strokes: draft.strokes, ...(draft.image ? { image: draft.image } : {}),
})
