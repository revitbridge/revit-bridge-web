import { useState } from 'react'
import { uiText } from '../../utils/strings'
import Icon from './Icon'

export default function Snippet({ title, text }: { title?: string; text: string }) {
  const [result, setResult] = useState<{ text: string; feedback: string }>()
  return <div className="snippet"><div className="snippet-head">{title && <h3>{title}</h3>}
    <button className="btn-ghost btn-sm" type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setResult({ text, feedback: 'Copied' }) }
      catch { setResult({ text, feedback: 'Copy failed' }) }
    }}><Icon name="copy" />{uiText('Copy')}</button></div>
    <pre className="command-block">{text}</pre>{result?.text === text && <small role="status">{uiText(result.feedback)}</small>}
  </div>
}
