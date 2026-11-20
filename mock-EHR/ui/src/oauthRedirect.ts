/** Only the configured BFF callback may receive an authorization code. */
export function oauthRedirect(uri: string | null, allowedOrigins: string): URL | undefined {
  try {
    const url = new URL(uri || '');
    const allowed = allowedOrigins.split(',').map((origin) => origin.trim()).filter(Boolean);
    if (['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
        && !url.search && !url.hash && url.pathname === '/api/v1/launch/callback'
        && allowed.includes(url.origin)) return url;
  } catch { /* Invalid authorization request. */ }
  return undefined;
}
