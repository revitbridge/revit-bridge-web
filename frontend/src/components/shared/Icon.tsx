const paths = {
  mark: 'M12 2 22 7.5 12 13 2 7.5 12 2ZM2 12l10 5.5L22 12M2 16.5 12 22l10-5.5',
  Chat: 'M20 14a3 3 0 0 1-3 3H9l-5 4v-4a3 3 0 0 1-2-3V7a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3zM7 9h8M7 12h5',
  Tools: 'M4 7h16v13H4zM8 7V4h8v3M4 12h16M10 12v2h4v-2',
  Workflows: 'M3 3h6v6H3zM15 15h6v6h-6zM6 9v6a3 3 0 0 0 3 3h6',
  Evidence: 'M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h5',
  Settings: 'M3 6h8m4 0h6M3 12h2m4 0h12M3 18h11m4 0h3M11 4v4M6 10v4M15 16v4',
  panel: 'M3 5h18v14H3zM15 5v14',
  plus: 'M12 5v14M5 12h14',
  close: 'm6 6 12 12M18 6 6 18',
  send: 'm4 11 16-7-6 16-3-7-7-2Zm7 2 9-9',
  down: 'm6 9 6 6 6-6',
  up: 'm6 15 6-6 6 6',
  grip: 'M8 5h1m5 0h1M8 12h1m5 0h1M8 19h1m5 0h1',
  select: 'M4 8V4h4m8 0h4v4m0 8v4h-4M8 20H4v-4m5-8 7 4-3 1-1 4-3-9Z',
  model: 'm12 3 8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9',
  copy: 'M8 8h12v13H8zM16 8V3H3v13h5',
} as const

export default function Icon({ name }: { name: keyof typeof paths }) {
  return <svg className="icon" aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round">
    <path d={paths[name]} />
  </svg>
}
