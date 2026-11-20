import { resolveConfig, type AppConfig } from './runtimeConfig';

export const APP_NAME = 'CareView EHR';

let current: AppConfig | undefined;

/** Load `/config.json` (mounted at deploy time) over the build-time defaults. */
export async function loadConfig(): Promise<AppConfig> {
  let runtime: unknown;
  try {
    const resp = await fetch('/config.json');
    if (resp.ok) runtime = await resp.json();
  } catch {
    // Config file not available or not JSON — use the build-time defaults.
  }
  current = resolveConfig(runtime, {
    medplumBaseUrl: import.meta.env.VITE_MEDPLUM_BASE_URL,
    smartRedirectOrigins: import.meta.env.VITE_SMART_REDIRECT_ORIGINS,
  });
  return current;
}

/** The configuration loaded before first render; redirects are refused until then. */
export function appConfig(): AppConfig {
  return current ?? { medplumBaseUrl: '', smartRedirectOrigins: '' };
}
