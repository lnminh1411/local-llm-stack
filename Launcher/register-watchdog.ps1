$name = 'Local AI Proxy Watchdog'
$vbs  = 'F:\!PC Scripts\Start-LocalAI-proxy-hidden.vbs'
$act  = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
$trg  = @((New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
          (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 3650)))
$set  = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
          -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Priority 4
$pri  = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $act -Trigger $trg -Settings $set -Principal $pri -Force -Description 'Starts the llama wake proxy (:8080) if it is not running. Never wakes the model. Removing it: Unregister-ScheduledTask "Local AI Proxy Watchdog"' | Out-Null
Start-ScheduledTask -TaskName $name
Start-Sleep -Seconds 12
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State | Format-List
(Get-ScheduledTaskInfo -TaskName $name) | Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List
