import { MedplumClient } from '@medplum/core';
import { MedplumProvider } from '@medplum/react-hooks';
import { MantineProvider, createTheme } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router';
import { App } from './App';
import { loadConfig } from './config';

import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';
import '@medplum/react/styles.css';

const theme = createTheme({
  primaryColor: 'blue',
  fontSizes: {
    xs: '0.6875rem',
    sm: '0.875rem',
    md: '0.875rem',
    lg: '1rem',
    xl: '1.125rem',
  },
});

async function init(): Promise<void> {
  const config = await loadConfig();

  const medplum = new MedplumClient({
    baseUrl: config.medplumBaseUrl,
    cacheTime: 60000,
    autoBatchTime: 100,
    onUnauthenticated: () => {
      if (!['/signin', '/oauth'].includes(window.location.pathname)) {
        window.location.href = '/signin';
      }
    },
  });

  const router = createBrowserRouter([{ path: '*', Component: App }]);

  function navigate(path: string): Promise<void> {
    return router.navigate(path);
  }

  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <MedplumProvider medplum={medplum} navigate={navigate}>
        <MantineProvider theme={theme}>
          <Notifications position="bottom-right" />
          <RouterProvider router={router} />
        </MantineProvider>
      </MedplumProvider>
    </StrictMode>
  );
}

init();
