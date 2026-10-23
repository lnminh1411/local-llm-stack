' Watchdog action: make sure the wake proxy on :8080 is running (no-op when it is). Never wakes the model.
' Run by the scheduled task "Local AI Proxy Watchdog" (logon + every 2 minutes). No console window.
Option Explicit
Dim shell
Set shell = CreateObject("WScript.Shell")
WScript.Quit shell.Run("powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""F:\!PC Scripts\Start-LocalAI.ps1"" -ProxyOnly", 0, True)
