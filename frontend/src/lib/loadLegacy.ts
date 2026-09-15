const scripts = [
  '/legacy/validation-ui.js',
  '/legacy/cleaning-ui.js',
  '/legacy/dashboard.js',
  '/legacy/script.js',
  '/legacy/report-ui.js',
]

declare global {
  interface Window { __INSIGHTFLOW_LEGACY_LOADED__?: boolean }
}

export async function loadLegacyRuntime() {
  if (window.__INSIGHTFLOW_LEGACY_LOADED__) return
  window.__INSIGHTFLOW_LEGACY_LOADED__ = true

  for (const src of scripts) {
    await new Promise<void>((resolve, reject) => {
      const script = document.createElement('script')
      script.src = src
      script.async = false
      script.dataset.insightflowLegacy = 'true'
      script.onload = () => resolve()
      script.onerror = () => reject(new Error(`Unable to load ${src}`))
      document.body.appendChild(script)
    })
  }

  // script.js intentionally registers its original bootstrap on DOMContentLoaded.
  // Because React mounts after the browser's first DOMContentLoaded, replay the
  // event once after every original script has been loaded. The scripts themselves
  // are byte-for-byte copies of the user's original frontend logic.
  document.dispatchEvent(new Event('DOMContentLoaded', { bubbles: true }))
}
