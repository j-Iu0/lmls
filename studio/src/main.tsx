import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ReactFlowProvider } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './styles/studio.css';
import { App } from './App';
import { connectBackend } from './runtime/backend';
// Own the transport at application lifetime, outside React's mount/HMR cycle.
const disconnect = connectBackend();
const hot = (import.meta as { hot?: { dispose(callback: () => void): void } }).hot;
hot?.dispose(disconnect);
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </StrictMode>,
);
