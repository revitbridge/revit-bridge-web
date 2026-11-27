import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ResolvedSketch, SketchCapture, SketchDraft } from '../../types/api'
import { getErrorMessage } from '../../utils/errors'
import { uiText as t } from '../../utils/strings'
import { MAX_STROKES, SKETCH_COLOUR, capturePixel, captureSketchView, lineText, resolveSketch, thinned } from './sketch'
import './sketchPad.css'

/* Overlay the model lines read by the host on the designer's strokes. */

const pathOf = (points: number[][]) => points.map(([x, y], index) => (index ? 'L' : 'M') + x + ' ' + y).join(' ')

/* The capture with the strokes on it, as base64 PNG of the capture's own size; '' when the
   browser cannot draw it (the message then goes without a picture). */
function drawnPicture(picture: HTMLImageElement, capture: SketchCapture, strokes: number[][][]): string {
  try {
    const canvas = document.createElement('canvas')
    canvas.width = capture.width
    canvas.height = capture.height
    const context = canvas.getContext('2d')
    if (!context) return ''
    context.drawImage(picture, 0, 0, capture.width, capture.height)
    context.strokeStyle = SKETCH_COLOUR
    context.lineWidth = Math.max(4, capture.width / 320)
    context.lineCap = 'round'
    context.lineJoin = 'round'
    for (const stroke of strokes) {
      context.beginPath()
      stroke.forEach(([x, y], index) => index ? context.lineTo(x, y) : context.moveTo(x, y))
      context.stroke()
    }
    return canvas.toDataURL('image/png').replace(/^data:image\/png;base64,/, '')
  } catch {
    return ''
  }
}

export default function SketchPad({ onUse, onClose, capture: takeCapture = captureSketchView, resolve = resolveSketch }: {
  onUse: (draft: SketchDraft) => void
  onClose: () => void
  capture?: () => Promise<SketchCapture>
  resolve?: typeof resolveSketch
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const picture = useRef<HTMLImageElement>(null)
  const drawing = useRef<number[][] | null>(null)
  const request = useRef(0)
  const [capture, setCapture] = useState<SketchCapture | null>(null)
  const [level, setLevel] = useState<string>()
  const [strokes, setStrokes] = useState<number[][][]>([])
  const [live, setLive] = useState<number[][]>([])
  const [reading, setReading] = useState<ResolvedSketch | null>(null)
  const [status, setStatus] = useState<'capturing' | 'ready' | 'reading'>('capturing')
  const [error, setError] = useState('')

  const load = useCallback(() => {
    const mine = ++request.current
    takeCapture().then(shot => {
      if (mine !== request.current) return
      setLevel(shot.kind === 'axonometric' ? shot.levels[0]?.name : undefined)
      setCapture(shot); setStatus('ready')
    }, (problem: unknown) => {
      if (mine !== request.current) return
      setCapture(null); setStatus('ready'); setError(getErrorMessage(problem))
    })
  }, [takeCapture])
  const take = () => {
    setStatus('capturing'); setError(''); setStrokes([]); setReading(null); setLive([])
    load()
  }

  useEffect(() => {
    const modal = dialog.current
    modal?.showModal?.()
    load()                                   // the first capture: the pad opens in the capturing state
    return () => { modal?.close?.() }
  }, [load])

  const read = (shot: SketchCapture, next: number[][][], nextLevel = level) => {
    const mine = ++request.current
    const before = strokes
    setLevel(nextLevel)
    if (nextLevel !== level) setReading(null)
    setStrokes(next)
    if (!next.length) { setReading(null); setStatus('ready'); setError(''); return }
    setStatus('reading'); setError('')
    resolve(shot.capture_id, next, nextLevel).then(answer => {
      if (mine !== request.current) return
      setReading(answer); setStatus('ready')
    }, (problem: unknown) => {
      if (mine !== request.current) return
      // What the host could not read is taken back - a new stroke, an undo or a clear alike -
      // so the strokes, the dark lines and the list stay what will be sent.
      setStrokes(before); setLevel(level); setReading(reading); setStatus('ready'); setError(getErrorMessage(problem))
    })
  }

  const pixel = (event: React.PointerEvent) => {
    const box = picture.current!.getBoundingClientRect()
    return capturePixel(event.clientX, event.clientY, box, capture!.width, capture!.height)
  }
  const full = strokes.length >= MAX_STROKES
  const canDraw = !!capture && status === 'ready' && !full

  return createPortal(
    <dialog className="sketch-pad" ref={dialog} aria-label={t("Sketch on the view")}
      onCancel={event => { event.preventDefault(); onClose() }}>
      <header>
        <strong>{t("Sketch on the view")}</strong>
        <span className="sketch-pad-view">{capture ? `${capture.view_name}${capture.level ? ' · ' + capture.level : ''}` : ''}</span>
        {capture?.kind === 'axonometric' && <label className="sketch-pad-level">{t("Level")}
          <select value={level ?? ''} disabled={status !== 'ready'}
            onChange={event => read(capture, strokes, event.target.value)}>
            {capture.levels.map(option => <option key={option.name} value={option.name}>{option.name}</option>)}
          </select>
        </label>}
        <button type="button" className="sketch-pad-close" aria-label={t("Close")} onClick={onClose}>×</button>
      </header>
      <p className="sketch-pad-hint">{t("Draw on the view. The dark lines show the model coordinates that will be sent.")}
        {capture?.kind === 'axonometric' && ' ' + t("The line is projected onto the chosen level.")}</p>
      <div className="sketch-pad-stage">
        {capture && <div className={'sketch-pad-surface' + (canDraw ? ' can-draw' : '')}
          onPointerDown={event => {
            if (!canDraw || event.button !== 0) return
            event.currentTarget.setPointerCapture?.(event.pointerId)
            drawing.current = [pixel(event)]
            setLive(drawing.current)
          }}
          onPointerMove={event => {
            if (!drawing.current) return
            drawing.current = [...drawing.current, pixel(event)]
            setLive(drawing.current)
          }}
          onPointerUp={() => {
            const stroke = drawing.current
            drawing.current = null
            setLive([])
            if (stroke && stroke.length > 1 && capture) read(capture, [...strokes, thinned(stroke)])
          }}
          onPointerCancel={() => { drawing.current = null; setLive([]) }}>
          <img ref={picture} src={'data:image/png;base64,' + capture.png_base64} alt={capture.view_name} draggable={false} />
          <svg viewBox={`0 0 ${capture.width} ${capture.height}`} preserveAspectRatio="none" aria-hidden="true">
            {strokes.map((stroke, index) => <path key={index} className="sketch-stroke" d={pathOf(stroke)} />)}
            {live.length > 1 && <path className="sketch-stroke" d={pathOf(live)} />}
            {reading?.lines.map(line => <g key={line.line}>
              <path className="sketch-read" d={pathOf(line.pixels) + (line.closed ? ' Z' : '')} />
              {line.pixels.map(([x, y], index) => <circle key={index} cx={x} cy={y} r={capture.width / 200}
                className={line.points[index]?.snap ? 'sketch-snap' : 'sketch-end'} />)}
            </g>)}
          </svg>
        </div>}
        {status === 'capturing' && <div className="sketch-pad-message" role="status">{t("Capturing the view from Revit...")}</div>}
      </div>
      {reading && strokes.length > 0 && <ul className="sketch-pad-lines">
        {reading.lines.map(line => <li key={line.line}>{lineText(line, t)}</li>)}
        {reading.warnings.map((warning, index) => <li key={'w' + index} className="sketch-pad-warning">{warning}</li>)}
      </ul>}
      {status === 'reading' && <div className="sketch-pad-message" role="status">{t("Reading the line...")}</div>}
      {full && <div className="sketch-pad-message">{t("That is the most lines one sketch takes.")}</div>}
      {error && <div className="sketch-pad-error" role="alert">{error}</div>}
      <footer>
        <button type="button" onClick={() => capture && read(capture, strokes.slice(0, -1))} disabled={!strokes.length || status !== 'ready'}>{t("Undo")}</button>
        <button type="button" onClick={() => capture && read(capture, [])} disabled={!strokes.length || status !== 'ready'}>{t("Clear")}</button>
        <button type="button" onClick={take} disabled={status === 'capturing'}>{t("Capture again")}</button>
        <span className="sketch-pad-space" />
        <button type="button" onClick={onClose}>{t("Cancel")}</button>
        <button type="button" className="btn-primary" disabled={!capture || !reading || !strokes.length || status !== 'ready'}
          onClick={() => {
            if (!capture || !reading) return
            onUse({ capture_id: capture.capture_id, strokes, image: picture.current ? drawnPicture(picture.current, capture, strokes) : '',
              ...(level !== undefined ? { level } : {}), view_name: capture.view_name, lines: reading.lines })
          }}>{t("Use this sketch")}</button>
      </footer>
    </dialog>, document.body)
}
