# PowerShell runner. Usage:
#   .\run.ps1 eval
#   .\run.ps1 agent
#   .\run.ps1 ask "who is on reserve at BLR tomorrow?"
#   .\run.ps1 ui
param(
    [Parameter(Position = 0)][string]$Cmd = "ui",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "data\rules.json")) {
    Write-Host "ERROR: data\ is missing or incomplete." -ForegroundColor Red
    Write-Host "Copy the 9 dataset JSON files into .\data\ first." -ForegroundColor Red
    exit 1
}

switch ($Cmd) {
    "doctor" { python -m app.doctor }
    "eval"  { python -m eval.run_eval --engine }
    "agent" { python -m eval.run_eval --agent }
    "ask"   { python -m app.agent @Rest }
    "ui"    { streamlit run app/ui.py }
    default { Write-Host "Unknown command '$Cmd'. Use: doctor | eval | agent | ask | ui" }
}
