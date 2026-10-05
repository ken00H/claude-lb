{{/*
Expand the name of the chart.
*/}}
{{- define "claude-lb.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "claude-lb.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Headless service name for per-pod bridge DNS.
*/}}
{{- define "claude-lb.bridgeHeadlessServiceName" -}}
{{- printf "%s-bridge" (include "claude-lb.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Stable workload resource name. Separate from fullname to allow controller-kind migration without same-name conflicts.
*/}}
{{- define "claude-lb.workloadName" -}}
{{- printf "%s-workload" (include "claude-lb.fullname" .) | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "claude-lb.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "claude-lb.labels" -}}
helm.sh/chart: {{ include "claude-lb.chart" . }}
{{ include "claude-lb.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/*
Selector labels — IMMUTABLE after first deploy (name + instance ONLY, never version/chart)
*/}}
{{- define "claude-lb.selectorLabels" -}}
app.kubernetes.io/name: {{ include "claude-lb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
StatefulSet workload selector labels. The traffic lane is part of the StatefulSet's
immutable selector, so it stays even though only one lane exists.
*/}}
{{- define "claude-lb.workloadSelectorLabels" -}}
{{- include "claude-lb.selectorLabels" . }}
claude-lb.soju.dev/traffic: workload
{{- end }}

{{/*
ServiceAccount name resolution
*/}}
{{- define "claude-lb.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "claude-lb.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret name — returns existingSecret or generated name
*/}}
{{- define "claude-lb.secretName" -}}
{{- if .Values.auth.existingSecret }}
{{- .Values.auth.existingSecret }}
{{- else }}
{{- include "claude-lb.fullname" . }}
{{- end }}
{{- end }}

{{/*
Database URL secret name — may differ from the app secret when using a dedicated external DB secret.
*/}}
{{- define "claude-lb.databaseUrlSecretName" -}}
{{- if and (not .Values.postgresql.enabled) .Values.externalDatabase.existingSecret }}
{{- .Values.externalDatabase.existingSecret }}
{{- else }}
{{- include "claude-lb.secretName" . }}
{{- end }}
{{- end }}

{{/*
Database URL — TWO code paths:
  1. postgresql.enabled: synthesize URL from sub-chart values
  2. external: use externalDatabase.url or synthesize from discrete fields
This is used in secret.yaml to populate the database-url secret key.
*/}}
{{- define "claude-lb.databaseUrl" -}}
{{- if .Values.postgresql.enabled }}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" .Values.postgresql.auth.username .Values.postgresql.auth.password .Release.Name .Values.postgresql.auth.database }}
{{- else if .Values.externalDatabase.url }}
{{- .Values.externalDatabase.url }}
{{- else if and .Values.externalDatabase.host .Values.externalDatabase.user .Values.externalDatabase.database }}
{{- printf "postgresql+asyncpg://%s@%s:%v/%s" .Values.externalDatabase.user .Values.externalDatabase.host (.Values.externalDatabase.port | default 5432) .Values.externalDatabase.database }}
{{- else }}
{{- fail "No database URL source configured. Enable postgresql, set externalDatabase.url, provide externalDatabase.host/user/database, configure externalDatabase.existingSecret, auth.existingSecret, or externalSecrets.enabled." }}
{{- end }}
{{- end }}

{{/*
Migration hook phases — default to pre-install when DB credentials are already available without ExternalSecrets materialization.
*/}}
{{- define "claude-lb.migrationHookPhases" -}}
{{- if .Values.externalSecrets.enabled -}}
post-install,pre-upgrade
{{- else if .Values.postgresql.enabled -}}
pre-upgrade
{{- else if or .Values.auth.existingSecret .Values.externalDatabase.existingSecret -}}
pre-install,pre-upgrade
{{- else -}}
post-install,pre-upgrade
{{- end -}}
{{- end }}

{{/*
Migration job service account — pre-install hooks cannot rely on chart-created ServiceAccounts.
Use an operator-provided existing SA when explicitly configured; otherwise fall back to default.
*/}}
{{- define "claude-lb.migrationServiceAccountName" -}}{{- if and .Values.externalSecrets.enabled .Values.serviceAccount.create -}}{{- include "claude-lb.serviceAccountName" . -}}{{- else if .Values.serviceAccount.name -}}{{- .Values.serviceAccount.name -}}{{- else -}}default{{- end -}}{{- end }}

{{/*
Human-readable install mode label used in NOTES and docs.
*/}}
{{- define "claude-lb.installMode" -}}
{{- if .Values.postgresql.enabled -}}
bundled
{{- else if .Values.externalSecrets.enabled -}}
external-secrets
{{- else -}}
external-db
{{- end -}}
{{- end }}

{{/*
Image string — resolves registry/repository:tag with optional digest override
*/}}
{{- define "claude-lb.image" -}}
{{- $registry := .Values.global.imageRegistry | default .Values.image.registry }}
{{- $repository := .Values.image.repository }}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- if .Values.image.digest }}
{{- printf "%s/%s@%s" $registry $repository .Values.image.digest }}
{{- else }}
{{- printf "%s/%s:%s" $registry $repository $tag }}
{{- end }}
{{- end }}

{{/*
Helm test image string — resolves registry/repository:tag with optional digest override
*/}}
{{- define "claude-lb.testImage" -}}
{{- $registry := .Values.global.imageRegistry | default .Values.test.image.registry }}
{{- $repository := .Values.test.image.repository }}
{{- if .Values.test.image.digest }}
{{- printf "%s/%s@%s" $registry $repository .Values.test.image.digest }}
{{- else }}
{{- printf "%s/%s:%s" $registry $repository .Values.test.image.tag }}
{{- end }}
{{- end }}

{{/*
Merged nodeSelector: global.nodeSelector + local nodeSelector (local wins).
*/}}
{{- define "claude-lb.nodeSelector" -}}
{{- $merged := mustMergeOverwrite (deepCopy (.Values.global.nodeSelector | default dict)) (.Values.nodeSelector | default dict) -}}
{{- if $merged }}
{{- toYaml $merged }}
{{- end }}
{{- end -}}

{{/*
Global-only nodeSelector for hooks/tests so app-specific placement does not block installs.
*/}}
{{- define "claude-lb.globalNodeSelector" -}}
{{- with (.Values.global.nodeSelector | default dict) }}
{{- toYaml . }}
{{- end }}
{{- end -}}
