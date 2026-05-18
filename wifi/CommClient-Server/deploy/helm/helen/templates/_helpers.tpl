{{/* Helen Helm chart helpers */}}

{{- define "helen.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "helen.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.labels" -}}
helm.sh/chart: {{ include "helen.chart" . }}
{{ include "helen.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: helen
{{- end -}}

{{- define "helen.selectorLabels" -}}
app.kubernetes.io/name: {{ include "helen.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "helen.server.fullname" -}}
{{- printf "%s-server" (include "helen.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.router.fullname" -}}
{{- printf "%s-router" (include "helen.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.rendezvous.fullname" -}}
{{- printf "%s-rendezvous" (include "helen.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.postgres.fullname" -}}
{{- printf "%s-postgres" (include "helen.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.redis.fullname" -}}
{{- printf "%s-redis" (include "helen.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "helen.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "helen.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "helen.image" -}}
{{- $registry := .registry | default .Values.global.imageRegistry -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .repository .tag -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}

{{- define "helen.storageClass" -}}
{{- $sc := .storageClass | default .Values.global.storageClass -}}
{{- if $sc -}}
storageClassName: {{ $sc | quote }}
{{- end -}}
{{- end -}}

{{/*
Generate the database URL for the Helen server.
Uses bundled Postgres if enabled, otherwise expects external `externalPostgres.url`.
*/}}
{{- define "helen.databaseUrl" -}}
{{- if .Values.postgres.enabled -}}
postgresql+asyncpg://{{ .Values.postgres.username }}:$(POSTGRES_PASSWORD)@{{ include "helen.postgres.fullname" . }}:{{ .Values.postgres.port }}/{{ .Values.postgres.database }}
{{- else if .Values.externalPostgres -}}
{{ .Values.externalPostgres.url }}
{{- else -}}
sqlite+aiosqlite:////data/helen.db
{{- end -}}
{{- end -}}

{{- define "helen.redisUrl" -}}
{{- if .Values.redis.enabled -}}
redis://:$(REDIS_PASSWORD)@{{ include "helen.redis.fullname" . }}:{{ .Values.redis.port }}/0
{{- end -}}
{{- end -}}

{{- define "helen.podAntiAffinity" -}}
{{- $mode := .mode | default "soft" -}}
{{- if eq $mode "hard" -}}
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - topologyKey: kubernetes.io/hostname
      labelSelector:
        matchLabels:
{{ toYaml .labels | indent 10 }}
{{- else if eq $mode "soft" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
{{ toYaml .labels | indent 12 }}
{{- end -}}
{{- end -}}
