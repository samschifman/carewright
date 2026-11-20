export interface AppConfig {
  medplumBaseUrl: string;
  /** Comma-separated exact writer origins the OAuth page may redirect to. */
  smartRedirectOrigins: string;
}

export interface BuildDefaults {
  medplumBaseUrl?: string;
  smartRedirectOrigins?: string;
}

/**
 * Merge the deploy-time `/config.json` over the values compiled into the image.
 *
 * The redirect allowlist is a security setting: when the deployment supplies the
 * key at all, its value replaces the build-time one, and an empty or malformed
 * value refuses every redirect. Only a config file without the key (local
 * compose, a dev server) falls back to what the image was built with.
 */
export function resolveConfig(runtime: unknown, build: BuildDefaults): AppConfig {
  const file: Record<string, unknown> =
    runtime && typeof runtime === 'object' && !Array.isArray(runtime) ? (runtime as Record<string, unknown>) : {};
  const medplumBaseUrl =
    typeof file.medplumBaseUrl === 'string' && file.medplumBaseUrl
      ? file.medplumBaseUrl
      : build.medplumBaseUrl || 'http://localhost:8103/';
  let smartRedirectOrigins = build.smartRedirectOrigins || '';
  if ('smartRedirectOrigins' in file) {
    smartRedirectOrigins = typeof file.smartRedirectOrigins === 'string' ? file.smartRedirectOrigins : '';
  }
  return { medplumBaseUrl, smartRedirectOrigins };
}
