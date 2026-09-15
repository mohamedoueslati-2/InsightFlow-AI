import * as React from 'react'
import legacyLayout from './legacy-layout.html?raw'
import { loadLegacyRuntime } from '@/lib/loadLegacy'

export default function App() {
  React.useEffect(() => {
    loadLegacyRuntime().catch((error) => {
      console.error('[InsightFlow] Original frontend runtime failed to load:', error)
      const message = document.getElementById('message')
      if (message) {
        message.textContent = 'Frontend runtime could not be initialized. Check the browser console.'
        message.className = 'message-container global-message error'
      }
    })
  }, [])

  // IMPORTANT: this is the exact DOM contract from the original frontend.
  // Existing agent JS continues to own dynamic rendering inside these nodes.
  return <div dangerouslySetInnerHTML={{ __html: legacyLayout }} />
}
