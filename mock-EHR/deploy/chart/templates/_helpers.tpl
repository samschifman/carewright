{{/*
The writer UI origin (scheme://host[:port]) the EHR trusts for SMART launch.

One value feeds both the SMART client the loader registers (launch and redirect
URIs) and the OAuth page's redirect allowlist, so the two cannot drift apart.
An explicit .Values.acpWriterOrigin wins. Otherwise it is derived from the
cluster domain using the script/Helm-deployed writer UI's route name. It is
empty when neither is known.
*/}}
{{- define "mock-ehr.acpWriterOrigin" -}}
{{- if .Values.acpWriterOrigin -}}
{{- .Values.acpWriterOrigin | toString | trimSuffix "/" -}}
{{- else if .Values.clusterDomain -}}
{{- printf "https://acp-ui-%s.%s" .Release.Namespace .Values.clusterDomain -}}
{{- end -}}
{{- end -}}

{{/*
The OAuth page's redirect allowlist (comma-separated exact origins).

Defaults to the writer origin above. An explicit list may add origins but must
include the writer origin: the loader registers that origin's callback, so a
list without it would refuse the only launch the EHR is configured for.
*/}}
{{- define "mock-ehr.smartRedirectOrigins" -}}
{{- $writer := include "mock-ehr.acpWriterOrigin" . -}}
{{- $explicit := .Values.mockEhrApp.smartRedirectOrigins | toString -}}
{{- if $explicit -}}
{{- if and $writer (not (has $writer (splitList "," $explicit))) -}}
{{- fail (printf "mockEhrApp.smartRedirectOrigins must include the writer origin %s (acpWriterOrigin), whose callback the loader registers" $writer) -}}
{{- end -}}
{{- $explicit -}}
{{- else -}}
{{- $writer -}}
{{- end -}}
{{- end -}}
