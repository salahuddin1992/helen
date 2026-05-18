/**
 * Renderer process entry point.
 */
import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { AppErrorBoundary } from './components/common/AppErrorBoundary';
import { installGlobalErrorHandlers } from './services/call/globalErrorHandlers';
import { installRendererWatchdog } from './services/call/rendererWatchdog';
import './styles/globals.css';

// Install reliability machinery before the React tree mounts so the
// very first render error, chunk-load failure, or blank-screen failure
// is captured and recoverable.
installGlobalErrorHandlers();
installRendererWatchdog();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <AppErrorBoundary>
      <App />
    </AppErrorBoundary>
  </React.StrictMode>
);
