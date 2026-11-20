import { useState } from "react";
import { BrowserRouter } from "react-router-dom";
import { ThemeProvider, AppShell } from "@cpg-to-acp/ui-shared";
import {
  PlusCircleIcon,
  RunningIcon,
  ListIcon,
  MonitoringIcon,
} from "@patternfly/react-icons";
import { Button } from "@patternfly/react-core";
import { SessionProvider, useSession } from "./SessionProvider";
import { logout } from "@app/services/api";
import { AppRoutes } from "./routes";

const navItems = [
  { label: "New Care Plan", path: "/", icon: <PlusCircleIcon /> },
  { label: "Runs", path: "/runs", icon: <RunningIcon /> },
  { label: "Care Plans", path: "/careplans", icon: <ListIcon /> },
  { label: "System Status", path: "/status", icon: <MonitoringIcon /> },
];

function SessionShell() {
  const { session, directIpsEnabled } = useSession();
  const [logoutError, setLogoutError] = useState("");
  return <AppShell navItems={navItems.filter((item) => item.path !== "/" || directIpsEnabled)} brandText="ACP Writer"
    headerActions={session ? <><span>Patient {session.subject}</span> <Button variant="link" onClick={() => { setLogoutError(""); void logout().catch(() => setLogoutError("Logout failed. Reload the page and try again.")); }}>Logout</Button>{logoutError && <span role="alert">{logoutError}</span>}</> : undefined}>
    <AppRoutes />
  </AppShell>;
}

export function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <SessionProvider><SessionShell /></SessionProvider>
      </BrowserRouter>
    </ThemeProvider>
  );
}
