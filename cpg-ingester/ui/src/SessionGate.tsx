import { useEffect, useState, type ReactNode } from 'react';
import { Button, Spinner } from '@patternfly/react-core';
import { api, ApiError } from './api/client';

export function SessionGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<'loading' | 'ready' | 'signed-out' | 'error'>('loading');
  useEffect(() => {
    let alive = true;
    const expired = () => setStatus('signed-out');
    window.addEventListener('cpg-session-expired', expired);
    api.getSession().then(() => { if (alive) setStatus('ready'); }).catch((error: unknown) => {
      if (alive) setStatus(error instanceof ApiError && error.status === 401 ? 'signed-out' : 'error');
    });
    return () => { alive = false; window.removeEventListener('cpg-session-expired', expired); };
  }, []);
  if (status === 'loading') return <Spinner aria-label="Loading session" />;
  if (status === 'error') return <p role="alert">Session service is unavailable. Please try again.</p>;
  if (status === 'signed-out') return <p>Open a new launch link supplied by your operator to continue.</p>;
  return <><Button onClick={() => void api.logout().catch(() => setStatus('error'))}>Sign out</Button>{children}</>;
}
