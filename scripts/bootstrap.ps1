# Bootstrap KumaFlow on Windows (PowerShell)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSCommandPath
Set-Location (Join-Path $root "..")

Write-Host "==> Creating server venv"
Set-Location "server"
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt

if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "==> Created server\.env — please fill in credentials"
}
deactivate

Write-Host "==> Installing web deps"
Set-Location "..\web"
if (-not (Test-Path .env.local)) {
  Copy-Item .env.example .env.local
}
npm install

Write-Host "==> Done."
Write-Host "Start backend:"
Write-Host "  cd server; .\.venv\Scripts\Activate.ps1; uvicorn app.main:app --reload"
Write-Host "Start worker:"
Write-Host "  cd server; .\.venv\Scripts\Activate.ps1; python -m app.workers.rq_worker"
Write-Host "Start frontend:"
Write-Host "  cd web; npm run dev"
