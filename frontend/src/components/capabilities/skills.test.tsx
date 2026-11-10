/* A skill's Markdown as the skills view shows it (no browser needed). */

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { SkillContent } from './SkillsView'

describe('SkillContent', () => {
  it('renders the Markdown without images; links open in a new tab without a referrer', () => {
    const html = renderToStaticMarkup(<SkillContent content={
      '# Walls\n\n![diagram](https://tracker.example/p.gif?u=1)\n\n![][ref]\n\nSee [the API](https://www.revitapidocs.com/).\n\n[ref]: https://tracker.example/r.gif'
    } />)
    expect(html).toContain('<h1>Walls</h1>')
    expect(html).not.toContain('<img')
    expect(html).not.toContain('tracker.example')
    expect(html).toContain('[diagram]')
    expect(html).toContain('<a href="https://www.revitapidocs.com/" target="_blank" rel="noopener noreferrer">the API</a>')
  })

  it('shows a placeholder for an empty skill', () => {
    expect(renderToStaticMarkup(<SkillContent content="" />)).toContain('(empty)')
  })
})
