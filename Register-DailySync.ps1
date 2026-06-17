<#
.SYNOPSIS
  Registriert einen täglichen Windows Task Scheduler Job für den Parqet Sync (20:00 Uhr).
.NOTES
  Als Administrator ausführen für systemweite Aufgabe.
  Oder ohne Admin für benutzerspezifische Aufgabe.
#>

$TaskName  = "ParqetDashboard-DailySync"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Suche Python in der venv oder system-wide
$PythonVenv = Join-Path $ScriptDir ".venv\Scripts\python.exe"
$PythonExe  = if (Test-Path $PythonVenv) { $PythonVenv } else { (Get-Command python -ErrorAction SilentlyContinue).Source }

if (-not $PythonExe) {
    Write-Error "Python nicht gefunden. Bitte Python installieren und 'Starten.bat' zuerst ausführen."
    exit 1
}

$SyncScript = Join-Path $ScriptDir "sync.py"

# Entferne alte Aufgabe falls vorhanden
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Trigger: täglich um 20:00 Uhr
$Trigger = New-ScheduledTaskTrigger -Daily -At "20:00"

# Aktion: Python sync.py aufrufen
$Action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "`"$SyncScript`"" `
    -WorkingDirectory $ScriptDir

# Einstellungen: auch bei Akkubetrieb ausführen, nach Anmeldung
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -WakeToRun:$false `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Trigger   $Trigger `
    -Action    $Action `
    -Settings  $Settings `
    -Principal $Principal `
    -Description "Parqet Portfolio Dashboard — täglicher Kurs-Sync um 20:00 Uhr" `
    -Force | Out-Null

Write-Host ""
Write-Host "✅ Task '$TaskName' erfolgreich registriert." -ForegroundColor Green
Write-Host "   Täglich um 20:00 Uhr wird sync.py ausgeführt."
Write-Host ""
Write-Host "Verwalten unter: Aufgabenplanung > Aufgabenplanungsbibliothek"
Write-Host ""

# Sofortigen Test-Run anbieten
$run = Read-Host "Sync jetzt einmal testweise ausführen? (j/n)"
if ($run -eq 'j') {
    Write-Host "Starte Sync..." -ForegroundColor Cyan
    & $PythonExe $SyncScript
}
