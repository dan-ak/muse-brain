import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import Dashboard from './Dashboard.tsx'

// Two views share one bundle: the player client that phones open, and the
// operator dashboard for a laptop or projector. The server serves the app shell
// for /dashboard so a refresh on that URL still lands here.
const isDashboard = window.location.pathname.replace(/\/+$/, '') === '/dashboard'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {isDashboard ? <Dashboard /> : <App />}
  </StrictMode>,
)
