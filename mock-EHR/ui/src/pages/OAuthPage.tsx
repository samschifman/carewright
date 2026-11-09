import { Alert, Title } from '@mantine/core';
import { SignInForm } from '@medplum/react';
import { useSearchParams } from 'react-router';
import { oauthRedirect } from '../oauthRedirect';

/** Medplum hosts authorization at its appBaseUrl, separately from its API. */
export function OAuthPage() {
  const [params] = useSearchParams();
  const clientId = params.get('client_id');
  const redirectUri = params.get('redirect_uri');
  const challenge = params.get('code_challenge');
  const redirect = oauthRedirect(redirectUri, import.meta.env.VITE_SMART_REDIRECT_ORIGINS || '');
  if (!clientId || !redirect || !challenge || params.get('code_challenge_method') !== 'S256') {
    return <Alert color="red" title="Invalid authorization request">Return to your EHR and launch the application again.</Alert>;
  }
  const destination = redirect;
  return <SignInForm
    clientId={clientId}
    redirectUri={redirect.toString()}
    launch={params.get('launch') || undefined}
    scope={params.get('scope') || undefined}
    nonce={params.get('nonce') || undefined}
    codeChallenge={challenge}
    codeChallengeMethod="S256"
    disableGoogleAuth
    chooseScopes
    onCode={(code) => {
      destination.searchParams.set('code', code);
      const state = params.get('state');
      if (state) destination.searchParams.set('state', state);
      window.location.replace(destination.toString());
    }}
  ><Title order={3}>Authorize care-plan access</Title></SignInForm>;
}
