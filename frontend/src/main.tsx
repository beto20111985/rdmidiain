import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { purgeLegacyAuthStorage } from './lib/legacyStorage'
import './styles/globals.css'

// Remove tokens deixados no localStorage pela versão anterior do painel.
purgeLegacyAuthStorage()

// O tema já foi aplicado antes da pintura por /theme-init.js (index.html).
const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('Elemento #root não encontrado em index.html.')

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
