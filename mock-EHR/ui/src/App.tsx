import { Loading, AppShell } from '@medplum/react';
import { useMedplum, useMedplumProfile } from '@medplum/react-hooks';
import {
  IconCalendarEvent,
  IconClipboardCheck,
  IconMail,
  IconStethoscope,
  IconUsers,
} from '@tabler/icons-react';
import { Suspense } from 'react';
import { Navigate, Route, Routes, useLocation, useSearchParams } from 'react-router';
import { OAuthPage } from './pages/OAuthPage';
import { APP_NAME } from './config';
import { AllergiesTab } from './pages/AllergiesTab';
import { CarePlansTab } from './pages/CarePlansTab';
import { EncountersTab } from './pages/EncountersTab';
import { LabsTab } from './pages/LabsTab';
import { MedicationsTab } from './pages/MedicationsTab';
import { ObservationsTab } from './pages/ObservationsTab';
import { PatientChartPage } from './pages/PatientChartPage';
import { PatientListPage } from './pages/PatientListPage';
import { SignInPage } from './pages/SignInPage';
import { StubPage } from './pages/StubPage';
import { TimelineTab } from './pages/TimelineTab';

export function App() {
  const medplum = useMedplum();
  const profile = useMedplumProfile();
  const location = useLocation();
  const [searchParams] = useSearchParams();

  if (medplum.isLoading()) {
    return null;
  }

  if (location.pathname === '/oauth') return <OAuthPage />;

  if (!profile) {
    return (
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route path="*" element={<Navigate to="/signin" replace />} />
      </Routes>
    );
  }

  return (
    <AppShell
      logo={
        <>
          <IconStethoscope size={24} />
          <span style={{ marginLeft: 8, fontWeight: 600 }}>{APP_NAME}</span>
        </>
      }
      pathname={location.pathname}
      searchParams={searchParams}
      resourceTypeSearchDisabled
      headerSearchDisabled
      menus={[
        {
          title: 'Navigation',
          links: [
            { icon: <IconUsers />, label: 'Patients', href: '/Patient' },
            { icon: <IconCalendarEvent />, label: 'Schedule', href: '/Schedule' },
            { icon: <IconMail />, label: 'Messages', href: '/Communication' },
            { icon: <IconClipboardCheck />, label: 'Tasks', href: '/Task' },
          ],
        },
      ]}
    >
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/" element={<Navigate to="/Patient" replace />} />
          <Route path="/signin" element={<Navigate to="/Patient" replace />} />
          <Route path="/Patient" element={<PatientListPage />} />
          <Route path="/Patient/:patientId" element={<PatientChartPage />}>
            <Route index element={<Navigate to="timeline" replace />} />
            <Route path="timeline" element={<TimelineTab />} />
            <Route path="encounters" element={<EncountersTab />} />
            <Route path="medications" element={<MedicationsTab />} />
            <Route path="vitals" element={<ObservationsTab />} />
            <Route path="labs" element={<LabsTab />} />
            <Route path="allergies" element={<AllergiesTab />} />
            <Route path="careplans" element={<CarePlansTab />} />
          </Route>
          <Route path="/Schedule" element={<StubPage resourceType="Schedule" />} />
          <Route path="/Communication" element={<StubPage resourceType="Communication" />} />
          <Route path="/Task" element={<StubPage resourceType="Task" />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
