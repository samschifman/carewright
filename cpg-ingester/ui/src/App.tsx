import {
  Button,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadLogo,
  MastheadMain,
  MastheadToggle,
  Nav,
  NavItem,
  NavList,
  Page,
  PageSidebar,
  PageSidebarBody,
} from '@patternfly/react-core';
import { BarsIcon } from '@patternfly/react-icons';
import { useCallback, useState } from 'react';
import { Route, Routes, useLocation, useNavigate } from 'react-router';

import { SessionGate } from './SessionGate';

import { DashboardPage } from './pages/DashboardPage';
import { UploadPage } from './pages/UploadPage';
import { RunDetailPage } from './pages/RunDetailPage';

export function App() {
  const location = useLocation();
  const navigate = useNavigate();
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const toggleSidebar = useCallback(() => setSidebarOpen((prev) => !prev), []);

  const masthead = (
    <Masthead display={{ default: 'inline' }}>
      <MastheadMain>
        <MastheadToggle>
          <Button
            variant="plain"
            aria-label="Toggle sidebar"
            onClick={toggleSidebar}
          >
            <BarsIcon />
          </Button>
        </MastheadToggle>
        <MastheadBrand>
          <MastheadLogo>
            <span style={{ fontSize: 'var(--pf-t--global--font--size--lg)', fontWeight: 700, color: '#151515' }}>
              CPG Ingester
            </span>
          </MastheadLogo>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent />
    </Masthead>
  );

  const sidebar = (
    <PageSidebar isSidebarOpen={sidebarOpen}>
      <PageSidebarBody>
        <Nav>
          <NavList>
            <NavItem
              isActive={location.pathname === '/'}
              onClick={() => navigate('/')}
            >
              Dashboard
            </NavItem>
            <NavItem
              isActive={location.pathname === '/upload'}
              onClick={() => navigate('/upload')}
            >
              Upload CPG
            </NavItem>
          </NavList>
        </Nav>
      </PageSidebarBody>
    </PageSidebar>
  );

  return (
    <SessionGate><Page masthead={masthead} sidebar={sidebar} isManagedSidebar={false}>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/upload" element={<UploadPage />} />
        <Route path="/runs/:runId" element={<RunDetailPage />} />
      </Routes>
    </Page></SessionGate>
  );
}
