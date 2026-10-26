# Installs (or updates) the Local AI Host Windows service, running as YOUR account (you type its password once;
# a Windows Hello PIN is not it). The service is the only thing that starts the stack: at boot the wake proxy,
# gateway and Open WebUI come up and the model stays unloaded until a request (or `model load` on the pipe) wakes it.
# Deletes the old logon tasks "Local AI Startup", "Local AI Proxy Watchdog" and "Local WebUI Startup" if present.
#
#   .\Install-Service.ps1              build, install / update, start
#   .\Install-Service.ps1 -Uninstall   stop + delete the service
#
# After a password change the service cannot log on until you re-run this script (it updates the stored password).
# The service runs llama-wake.py and Start-LocalAI.ps1 from SCRIPTS_DIR (the repo's Launcher folder).
param([switch]$Uninstall, [pscredential]$Credential)
$ErrorActionPreference = 'Stop'

$Name   = 'LocalAIHost'
$Tasks  = 'Local AI Startup', 'Local AI Proxy Watchdog', 'Local WebUI Startup'
$Repo   = Split-Path $PSScriptRoot -Parent
$Publish = Join-Path $PSScriptRoot 'publish'
$Exe    = Join-Path $Publish 'LocalAIHost.exe'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an elevated PowerShell.' }

function Remove-OldTasks {
    foreach ($t in $Tasks) {
        if (-not (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue)) { continue }
        Unregister-ScheduledTask -TaskName $t -Confirm:$false
        Write-Host "old task '$t' deleted"
    }
}

function Stop-Host {
    $s = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($s -and $s.Status -ne 'Stopped') { Stop-Service -Name $Name -Force; $s.WaitForStatus('Stopped', '00:00:40') }
}

if ($Uninstall) {
    Stop-Host
    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) { sc.exe delete $Name | Out-Null; Write-Host "service $Name deleted" }
    Write-Host 'Nothing starts the stack at boot now. By hand: Launcher\Start-LocalAI.ps1 -NoModel and WebUI\start-webui.ps1'
    return
}

# --- account ------------------------------------------------------------------------------------
$user = "$env:USERDOMAIN\$env:USERNAME"
if (-not $Credential) { $Credential = Get-Credential -UserName $user -Message "Password of $user (the Windows account password, not the PIN). The service runs as this account." }
Add-Type -AssemblyName System.DirectoryServices.AccountManagement
$ctx = New-Object System.DirectoryServices.AccountManagement.PrincipalContext('Machine')
if (-not $ctx.ValidateCredentials($env:USERNAME, $Credential.GetNetworkCredential().Password)) { throw "Wrong password for $user." }

# "Log on as a service" right (services.msc grants it silently; sc.exe / New-Service do not)
$sid = (New-Object Security.Principal.NTAccount($user)).Translate([Security.Principal.SecurityIdentifier]).Value
$inf = Join-Path $env:TEMP 'localaihost-rights.inf'
$sdb = Join-Path $env:TEMP 'localaihost-rights.sdb'
secedit /export /cfg $inf /areas USER_RIGHTS | Out-Null
$lines = [Collections.Generic.List[string]](Get-Content -LiteralPath $inf)
$i = $lines.FindIndex([Predicate[string]] { param($l) $l -match '^SeServiceLogonRight\s*=' })
if ($i -ge 0 -and $lines[$i] -match "\*$sid(,|$)") {
    Write-Host 'log on as a service: already granted'
} else {
    if ($i -ge 0) { $lines[$i] = $lines[$i] + ",*$sid" }
    else { $lines.Insert($lines.IndexOf('[Privilege Rights]') + 1, "SeServiceLogonRight = *$sid") }
    Set-Content -LiteralPath $inf -Value $lines -Encoding Unicode
    secedit /configure /db $sdb /cfg $inf /areas USER_RIGHTS | Out-Null
    Write-Host "log on as a service: granted to $user"
}
Remove-Item -LiteralPath $inf, $sdb -ErrorAction SilentlyContinue

# --- build --------------------------------------------------------------------------------------
Stop-Host   # the exe is locked while it runs
dotnet publish (Join-Path $PSScriptRoot 'LocalAIHost.csproj') -c Release -o $Publish --nologo -v q
if ($LASTEXITCODE -ne 0) { throw 'dotnet publish failed' }

# --- hand over from the logon tasks -------------------------------------------------------------
Remove-OldTasks
# The service waits instead of starting a duplicate while another copy holds a port, so stop the old copies.
Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and $_.CommandLine -match 'llama-wake\.py|entry\.js gateway run|openclaw\.mjs.*gateway run|Local LLM.WebUI.*(open-webui|open_webui|authproxy)'
} | ForEach-Object { Write-Host "stopping old $($_.Name) $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- service ------------------------------------------------------------------------------------
$cfgPath = [Environment]::GetEnvironmentVariable('LOCALAI_CONFIG', 'User')
if (-not $cfgPath) { $cfgPath = Join-Path $Repo 'config.env' }
$bin = "`"$Exe`" --config `"$cfgPath`""
if (Get-Service -Name $Name -ErrorAction SilentlyContinue) {
    # Win32_Service.Change keeps the password off any command line (sc.exe password= would show it in the process list)
    $r = Invoke-CimMethod -InputObject (Get-CimInstance Win32_Service -Filter "Name='$Name'") -MethodName Change `
        -Arguments @{ PathName = $bin; StartName = $user; StartPassword = $Credential.GetNetworkCredential().Password }
    if ($r.ReturnValue -ne 0) { throw "Win32_Service.Change failed ($($r.ReturnValue))" }
} else {
    New-Service -Name $Name -DisplayName 'Local AI Host' -BinaryPathName $bin -Credential $Credential -StartupType Automatic | Out-Null
}
sc.exe config $Name start= delayed-auto | Out-Null
sc.exe description $Name 'Keeps the local AI stack up: wake proxy (llama-server on demand), OpenClaw gateway, Open WebUI + auth proxy. Control pipe \\.\pipe\LocalAIHost. E:\Local LLM\Service' | Out-Null
sc.exe failure $Name reset= 86400 actions= restart/5000/restart/30000/restart/60000 | Out-Null
Start-Service -Name $Name
Write-Host "service $Name running as $user"

# --- check --------------------------------------------------------------------------------------
Start-Sleep -Seconds 10
$p = New-Object IO.Pipes.NamedPipeClientStream('.', 'LocalAIHost', [IO.Pipes.PipeDirection]::InOut)
try {
    $p.Connect(5000)
    $w = New-Object IO.StreamWriter($p); $w.NewLine = "`n"; $w.AutoFlush = $true
    $w.WriteLine('status'); (New-Object IO.StreamReader($p)).ReadLine()
} catch { Write-Warning "control pipe not answering yet: $_ (log: LOG_DIR\localai-host.log)" } finally { $p.Dispose() }
