# Registers the two scheduled tasks of the stack, fully in the background and without VBScript:
#   "Local AI Startup"        at logon (+1 min): Start-LocalAI.ps1 -Warm   (model, gateway, cache warm-up)
#   "Local AI Proxy Watchdog" at logon + every 2 min: Start-LocalAI.ps1 -ProxyOnly   (restarts a dead wake proxy, never wakes the model)
# The action is `conhost.exe --headless powershell.exe ...`: conhost never creates a console window, so nothing flashes
# on screen (powershell.exe -WindowStyle Hidden alone still flashes one for a moment). Trade-off: Task Scheduler's
# "Last Run Result" always reads 0, so read the logs in LOG_DIR to see failures.
# Re-run any time; existing tasks are replaced. Remove with: Unregister-ScheduledTask 'Local AI Startup','Local AI Proxy Watchdog' -Confirm:$false
. (Join-Path $PSScriptRoot 'LocalAI-Config.ps1')
$cfg    = Get-LocalAIConfig
$script = Join-Path (Get-Cfg $cfg 'SCRIPTS_DIR' $PSScriptRoot) 'Start-LocalAI.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "Start-LocalAI.ps1 not found at $script (check SCRIPTS_DIR in config.env)" }

function New-HiddenAction([string]$extra) {
    New-ScheduledTaskAction -Execute 'conhost.exe' `
        -Argument "--headless powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`" $extra"
}
$pri = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = 'PT1M'
Register-ScheduledTask -TaskName 'Local AI Startup' -Action (New-HiddenAction '-Warm') -Trigger $logon -Principal $pri -Force `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries) `
    -Description 'Starts the local AI stack at logon, hidden, no VBScript.' | Out-Null

$trg = @((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
         (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)))
Register-ScheduledTask -TaskName 'Local AI Proxy Watchdog' -Action (New-HiddenAction '-ProxyOnly') -Trigger $trg -Principal $pri -Force `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4) `
    -Description 'Starts the llama wake proxy if it is not running. Never wakes the model.' | Out-Null

Get-ScheduledTask -TaskName 'Local AI Startup', 'Local AI Proxy Watchdog' | Select-Object TaskName, State | Format-Table -AutoSize
