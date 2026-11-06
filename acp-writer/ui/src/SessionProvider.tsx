import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Bullseye, Spinner } from "@patternfly/react-core";
import { ApiError, getSession, type BrowserSession } from "@app/services/api";
import { SmartLaunchPage } from "@app/pages/SmartLaunchPage";

interface SessionState {
  session: BrowserSession | null;
  directIpsEnabled: boolean;
  loading: boolean;
  error: string | null;
}
const Context = createContext<SessionState>({ session: null, directIpsEnabled: false, loading: true, error: null });
export const useSession = () => useContext(Context);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ session: null, directIpsEnabled: false, loading: true, error: null });
  const navigate = useNavigate();
  const navigateRef = useRef(navigate);
  navigateRef.current = navigate;
  useEffect(() => {
    let alive = true;
    const expired = () => {
      setState({ session: null, directIpsEnabled: false, loading: false, error: null });
      navigateRef.current("/launch", { replace: true });
    };
    const changed = (event: Event) => {
      const session = (event as CustomEvent<BrowserSession>).detail;
      if (alive) setState({ session, directIpsEnabled: session.directIpsEnabled, loading: false, error: null });
    };
    window.addEventListener("acp-session-changed", changed);
    window.addEventListener("acp-session-expired", expired);
    getSession().then((session) => {
      if (alive) setState({ session, directIpsEnabled: session.directIpsEnabled, loading: false, error: null });
    }).catch((error: unknown) => {
      if (alive && error instanceof ApiError && error.status === 401 && !error.directIpsEnabled) navigateRef.current("/launch", { replace: true });
      if (alive) setState({ session: null, directIpsEnabled: error instanceof ApiError && error.directIpsEnabled,
        loading: false, error: error instanceof ApiError && error.status === 401 ? null : "Session service is unavailable. Please try again." });
    });
    return () => { alive = false; window.removeEventListener("acp-session-expired", expired); window.removeEventListener("acp-session-changed", changed); };
  }, []);
  return <Context.Provider value={state}>{children}</Context.Provider>;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const state = useSession();
  const { pathname } = useLocation();
  if (pathname === "/launch") return <SmartLaunchPage />;
  if (state.loading) return <Bullseye><Spinner aria-label="Loading session" /></Bullseye>;
  if (state.error) return <p role="alert">{state.error}</p>;
  if (!state.session && !(pathname === "/" && state.directIpsEnabled)) return <SmartLaunchPage />;
  return children;
}
