import { SessionGate, useSession } from "./SessionProvider";
import { Navigate, Route, Routes } from "react-router-dom";
import { IpsView } from "@app/pages/IpsView";
import { SmartLaunchPage } from "@app/pages/SmartLaunchPage";
import { RunListPage } from "@app/pages/RunListPage";
import { RunDetailPage } from "@app/pages/RunDetailPage";
import { CarePlanList } from "@app/pages/CarePlanList";
import { CarePlanDetail } from "@app/pages/CarePlanDetail";
import { SystemStatus } from "@app/pages/SystemStatus";

export function AppRoutes() {
  const { directIpsEnabled } = useSession();
  return (
    <SessionGate>
    <Routes>
      <Route path="/" element={directIpsEnabled ? <IpsView /> : <Navigate to="/runs" replace />} />
      <Route path="/launch" element={<SmartLaunchPage />} />
      <Route path="/runs" element={<RunListPage />} />
      <Route path="/runs/:runId" element={<RunDetailPage />} />
      <Route path="/careplans" element={<CarePlanList />} />
      <Route path="/careplans/:id" element={<CarePlanDetail />} />
      <Route path="/status" element={<SystemStatus />} />
    </Routes>
    </SessionGate>
  );
}
