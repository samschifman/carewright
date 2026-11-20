import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import { Bullseye, EmptyState, EmptyStateBody } from "@patternfly/react-core";

// Export the navigation adapter so tests can exercise forwarding in jsdom.
export const launchNavigation = { replace: (url: string) => window.location.replace(url) };

export function SmartLaunchPage() {
  const [params] = useSearchParams();
  const query = params.toString();
  const launching = params.has("iss") && params.has("launch");
  useEffect(() => {
    if (launching) launchNavigation.replace("/api/v1/launch?" + query);
  }, [launching, query]);
  return (
    <Bullseye>
      <EmptyState titleText={launching ? "Launching…" : "Launch from your EHR"} headingLevel="h1">
        <EmptyStateBody>Open a patient in your EHR and choose Generate Care Plan.</EmptyStateBody>
      </EmptyState>
    </Bullseye>
  );
}
