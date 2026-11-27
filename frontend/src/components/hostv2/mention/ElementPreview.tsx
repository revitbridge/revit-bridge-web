import { useEffect, useRef, useState } from 'react'
import { uiText } from '../../../utils/strings'
import type { ElementShape, FetchPreview } from '../../../types/hostv2'
import { boxMesh, drawShape, sizeText } from './shape'

/* A small turning picture of the element the picker is on, so the designer can tell what it
   is before choosing it. The shape comes from the host as it is (a coarse mesh in millimetres,
   or only a size): it is drawn to be recognised, never to be measured. */

const WAIT_MS = 180            // moving through the list does not ask for every row
const KEEP = 40                // shapes kept for rows the designer comes back to
const TURN = 0.0006            // radians per millisecond

const cache = new Map<string, ElementShape>()

function remember(key: string, shape: ElementShape) {
  cache.delete(key)
  cache.set(key, shape)
  if (cache.size > KEEP) cache.delete(cache.keys().next().value as string)
}

export function ElementPreview({ id, documentId, fetchPreview }: {
  id: number; documentId?: string; fetchPreview: FetchPreview
}) {
  const key = `${documentId ?? ''}#${id}`
  const [loaded, setLoaded] = useState<{ key: string; shape: ElementShape | null; error: string } | null>(null)
  const canvas = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (cache.has(key)) return
    let current = true
    const timer = setTimeout(() => {
      fetchPreview(id).then(shape => {
        remember(key, shape)
        if (current) setLoaded({ key, shape, error: '' })
      }, (error: unknown) => {
        if (current) setLoaded({ key, shape: null, error: error instanceof Error ? error.message : String(error) })
      })
    }, WAIT_MS)
    return () => { current = false; clearTimeout(timer) }
  }, [id, key, fetchPreview])

  const shape = cache.get(key) ?? (loaded?.key === key ? loaded.shape : null)
  const error = !shape && loaded?.key === key ? loaded.error : ''

  useEffect(() => {
    const element = canvas.current
    if (!element || !shape) return
    let context: CanvasRenderingContext2D | null = null
    try { context = element.getContext('2d') } catch { context = null }
    if (!context) return
    const mesh = shape.shape === 'mesh' && shape.triangles.length ? shape : boxMesh(shape.size_mm)
    const ratio = Math.min(window.devicePixelRatio || 1, 2)
    const width = element.clientWidth || 160, height = element.clientHeight || 120
    element.width = Math.round(width * ratio)
    element.height = Math.round(height * ratio)
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    const color = getComputedStyle(element).color || '#0f6b5c'
    const still = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    let frame = 0
    const started = performance.now()
    const paint = (now: number) => {
      drawShape(context!, width, height, mesh, shape.size_mm, 0.6 + (still ? 0 : (now - started) * TURN), color)
      if (!still) frame = requestAnimationFrame(paint)
    }
    paint(started)
    return () => cancelAnimationFrame(frame)
  }, [shape])

  return (
    <div className="mention-preview" aria-live="polite">
      <canvas ref={canvas} className="mention-preview-canvas" role="img"
        aria-label={shape ? uiText("Approximate 3D shape of {0}", shape.name || '#' + id) : uiText("Loading the shape...")} />
      <div className="mention-preview-caption">
        {shape && <>
          <span className="mention-preview-name">{shape.category_label || shape.category || uiText("Element")} #{id}</span>
          <span>{sizeText(shape.size_mm)}</span>
          <span>{shape.partial ? uiText("Approximate shape, partly drawn") : shape.shape === 'box'
            ? uiText("Approximate size only") : uiText("Approximate shape")}</span>
        </>}
        {!shape && !error && <span>{uiText("Loading the shape...")}</span>}
        {error && <span>{uiText("No shape to show: {0}", error)}</span>}
      </div>
    </div>
  )
}

export default ElementPreview
