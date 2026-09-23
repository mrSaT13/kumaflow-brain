#!/usr/bin/env pwsh
# Ночной бэкап pgdata: pg_dump в ./backups/kumaflow-YYYYMMDD-HHmm.sql.gz, ротация 14 дней.
# Использование:
#   .\scripts\pg_backup.ps1
# В compose сервис `backup` уже настроен на крон `0 3 * * *`.
param(
  [string]$BackupDir = (Join-Path $PSScriptRoot "..\backups")
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$ts = Get-Date -Format "yyyyMMdd-HHmm"
$out = Join-Path $BackupDir "kumaflow-$ts.sql.gz"
Write-Host "pg_dump -> $out"
docker compose exec -T postgres pg_dump -U kumaflow kumaflow | gzip > $out
Get-ChildItem $BackupDir -Filter "kumaflow-*.sql.gz" | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item -Force
Write-Host "done, kept 14 latest"
