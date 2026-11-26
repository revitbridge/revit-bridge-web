/* Simple collapsible accordion */

import { useState, useRef, type ReactNode } from 'react'

interface Props {
  title: string
  defaultOpen?: boolean
  open?: boolean
  onToggle?: (open: boolean) => void
  children: ReactNode
  id?: string
}

export default function Accordion({ title, defaultOpen = false, open: controlledOpen, onToggle, children, id }: Props) {
  const [internalOpen, setInternalOpen] = useState(defaultOpen)
  const isOpen = controlledOpen !== undefined ? controlledOpen : internalOpen
  const ref = useRef<HTMLDivElement>(null)

  const toggle = () => {
    const next = !isOpen
    setInternalOpen(next)
    onToggle?.(next)
  }

  return (
    <div ref={ref} id={id} className="card mb-2">
      <button
        onClick={toggle}
        className="accordion-trigger btn-ghost"
        aria-expanded={isOpen}
      >
        {title}<span aria-hidden="true">{isOpen ? '▴' : '▾'}</span>
      </button>
      {isOpen && <div className="px-4 pb-3">{children}</div>}
    </div>
  )
}
