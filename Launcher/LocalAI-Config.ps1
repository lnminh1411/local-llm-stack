# Dot-source this, then call Get-LocalAIConfig. Reads the central config.env (KEY=VALUE, '#' comments).
# Lookup order: $env:LOCALAI_CONFIG, the user-level LOCALAI_CONFIG (a session started before it was set lacks the first), config.env beside this script, config.env one folder up (repo checkout).
function Get-LocalAIConfigPath {
    $cands = @($env:LOCALAI_CONFIG, [Environment]::GetEnvironmentVariable('LOCALAI_CONFIG', 'User'), (Join-Path $PSScriptRoot 'config.env'), (Join-Path (Split-Path $PSScriptRoot -Parent) 'config.env'))
    foreach ($c in $cands) { if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { return (Resolve-Path -LiteralPath $c).Path } }
    throw "config.env not found. Copy config.env.example to config.env or set LOCALAI_CONFIG to its full path."
}

function Get-LocalAIConfig {
    $cfg = [ordered]@{}
    foreach ($line in Get-Content -LiteralPath (Get-LocalAIConfigPath) -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $cfg[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    $cfg
}

function Get-Cfg($cfg, [string]$key, $default = '') {
    if ($cfg.Contains($key) -and $cfg[$key] -ne '') { $cfg[$key] } else { $default }
}
