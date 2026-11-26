import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { ViewImage } from '../../types/api'
import { uiText as t } from '../../utils/strings'

export default function Capture({ image, hidden, older, expired, toggle }: {
  image: ViewImage; hidden: boolean; older: boolean; expired: boolean; toggle: () => void
}) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const dialog = useRef<HTMLDialogElement>(null)
  const close = useRef<HTMLButtonElement>(null)
  const src = 'data:image/png;base64,' + image.png_base64
  useEffect(() => {
    if (!open || !dialog.current) return
    const modal = dialog.current
    const opener = trigger.current
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const origin = opener?.getBoundingClientRect()
    modal.showModal()
    const picture = modal.querySelector('img')?.getBoundingClientRect()
    if (origin && picture) modal.style.setProperty('--capture-origin',
      `${origin.x + origin.width / 2 - picture.x}px ${origin.y + origin.height / 2 - picture.y}px`)
    close.current?.focus()
    return () => {
      modal.close()
      document.body.style.overflow = overflow
      opener?.focus({ preventScroll: true })
    }
  }, [open])
  const reason = hidden ? t('Hidden from model') : image.context_reason === 'context_disabled' ? t('Capture context is disabled')
    : image.context_reason === 'vision_unavailable' ? t('Image input is disabled for this planner')
    : expired || image.context_reason === 'unavailable' ? t('Capture no longer in context')
    : older || image.context_reason === 'summary' ? t('Only the view name and dimensions are used') : t('Used as context')
  return <figure className="chat-capture">
    <button ref={trigger} type="button" aria-label={t('Enlarge view')} onClick={() => setOpen(true)}>
      <img src={src} alt={image.view_name} /></button>
    <figcaption>{image.view_name} · {image.width} × {image.height}
      <label><input type="checkbox" role="switch" checked={hidden} onChange={toggle} />{t('Hide from model')}</label>
      <small>{reason}</small></figcaption>
    {open && createPortal(<dialog className="capture-viewer" ref={dialog} aria-label={image.view_name}
      onCancel={e => { e.preventDefault(); setOpen(false) }}
      onClick={e => { if (e.target === e.currentTarget) setOpen(false) }}
      onKeyDown={e => {
        if (e.key === 'Escape') { e.preventDefault(); setOpen(false) }
        if (e.key === 'Tab') { e.preventDefault(); close.current?.focus() }
      }}>
      <button ref={close} type="button" aria-label={t('Close')} onClick={() => setOpen(false)}>×</button>
      <img src={src} alt={image.view_name} />
    </dialog>, document.body)}
  </figure>
}
