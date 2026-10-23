# Starts the local AI stack, once each. Safe to run repeatedly: a service that is already up is left alone
# unless -Restart.
#
#   OpenClaw / Local Agent Bridge  ->  :8080 wake proxy (llama-wake.py)  ->  :8081 llama-server  (RTX 3050 + E-cores)
#   OpenClaw gateway (Discord bot) on :18789
#
# The wake proxy forwards everything to llama-server and, after -IdleUnloadMinutes without a real request,
# kills llama-server to free ~16 GB RAM + ~5.5 GB VRAM; the next request starts it again (this script with
# -BackendOnly) and waits. KV cache and prompt cache do NOT survive an unload (see llama-wake.py).
#
# Why llama-server directly instead of `ollama serve`: Ollama's runner passes -ub 512 and keeps a
# ~1.4 GB VRAM margin it will not let us shrink, and ignores LLAMA_ARG_* overrides. Measured on
# Qwen3.6-35B-A3B Q4_K_M at 10k-token prompts (04/10/2026): Ollama 217 tok/s prefill, 15% on GPU,
# 4.1 GB VRAM; llama-server -ub 4096 --fit-target 200: 597 tok/s prefill, 5.5 GB VRAM, same ~27 tok/s
# generation. Ollama stays installed ONLY because its lib\ollama folder ships llama-server.exe and the
# CUDA backend; no Ollama models are used any more.
#
# Deliberately does NOT self-elevate: UAC is off on this PC (EnableLUA=0), so everything is already
# admin, and an elevation relaunch would only create a second copy of the process.
param(
    # Occamy-1.0 35B-A3B MoE (default again since 2026-10-09). Found by file name on any fixed drive (this PC reshuffles drive letters).
    # Swift 1.5 Qwen3.8-27B heretic (tried 2026-10-09, ~2-3 tok/s, dropped): -ModelFile Swift-1.5-Qwen3.8-27B-heretic-GSQ-RCO-IQ3_S-mtp.gguf
    #   -MmprojFile Swift-1.5-Qwen3.8-27B-heretic-mmproj-Q8_0.gguf -ModelAlias swift-1.5-27b-iq3s -ChatTemplateFile Swift-1.5-Qwen3.8-27B-heretic-chat-template.jinja
    #   -MtpDraft 2 -MmprojGpu $false -Sampling swift   (sampling: temp 1.0 top-p 0.95 top-k 20 min-p 0, no presence penalty)
    [string]$ModelFile = 'occamy-1.0-abliterated-FIT-QUALITY-17G-IQ4_XS.gguf',
    [string]$MmprojFile = 'mmproj-occamy-1.0-abliterated-Q4_0.gguf',   # '' = text only
    [string]$ModelAlias = 'occamy-1.0-fit-quality',
    # The model's embedded template 500s on several requests (extra system message, no user message, reasoning_effort high/minimal); the
    # patched one from the same repo accepts them. '' = use the GGUF's own template.
    [string]$ChatTemplateFile = '',
    [bool]$PinECores = $true,   # pin llama-server to the 12 E-cores so the P-cores stay free for the user (-PinECores $false = any core)
    [int]$MtpDraft = 0,   # --spec-type draft-mtp --spec-draft-n-max N (the file carries an MTP head); 0 = no speculative decoding
    # $false keeps the vision projector on the CPU (--no-mmproj-offload): frees ~0.7 GB of the 6 GB card for model layers.
    # Measured: GPU saves only ~0.4 s per image; the time is spent per image token (~15-23 ms each), so cap the tokens.
    [bool]$MmprojGpu = $true,
    [int]$ImageMaxTokens = 768,   # per image; 0 = model default (a 1080p screenshot is ~2000 tokens = ~49 s before the first answer token)
    [ValidateSet('occamy','swift')][string]$Sampling = 'occamy',
    [int]$FitTarget = 100,   # MiB of VRAM --fit leaves free
    # 128k default. 256k (-Context 262144) halves generation speed on this 6 GB card (see Scripts.md), so use it
    # only on demand: .\Start-LocalAI.ps1 -Restart -Context 262144  (the Local Agent Bridge does this itself)
    [int]$Context = 131072,
    [double]$IdleUnloadMinutes = 20,   # 0 = never unload the model
    [bool]$KeepAwake = $true,          # hold Windows awake (no idle sleep) while the model is loaded in VRAM; released on unload
    [switch]$Restart,
    [switch]$NoGateway,   # leave the OpenClaw gateway alone (also on -Restart)
    [switch]$Warm,        # prime the prompt cache with OpenClaw's ~23k-token system prompt
    [switch]$ProxyOnly,   # start just the wake proxy if :8080 is closed, never wake the model or touch the gateway (the watchdog task uses this)
    [switch]$BackendOnly  # internal: start llama-server on :8081 and return (called by the wake proxy)
)
$ErrorActionPreference = 'Stop'

# Intel Core Ultra 7 265K, no HT. EfficiencyClass 0 = E-cores = CPUs 2-5,10-17 (verified via
# GetSystemCpuSetInformation 04/10/2026). Mask = 0x3FC3C. Re-verify if the CPU ever changes.
$ECoreMask   = [IntPtr]0x3FC3C
$GpuUuid     = 'GPU-00000000-0000-0000-0000-000000000000'   # RTX 3050 (nvidia-smi -L)  # replace with your own UUID
$ProxyPort   = 8080    # what clients use
$LlamaPort   = 8081    # the real llama-server
$GatewayPort = 18789
$OllamaLib   = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\lib\ollama'
$LogDir      = Join-Path $env:USERPROFILE '.openclaw\logs'
$ProxyScript = Join-Path $PSScriptRoot 'llama-wake.py'

# Raw connect, not Get-NetTCPConnection: that CIM query intermittently returns nothing for a live
# listener, which made an early version start duplicate servers.
function Test-Port($port) {
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect('127.0.0.1', $port); $true } catch { $false } finally { $c.Dispose() }
}
function Stop-ByCommandLine($pattern) {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $pattern -and $_.ProcessId -ne $PID } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Wait-Backend {
    # Loading 17 GB takes a while. /health answers 503 until the model is ready.
    for ($i = 0; $i -lt 180; $i++) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:$LlamaPort/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200) { return } } catch {}
        Start-Sleep -Seconds 2
    }
}

function Start-Backend {
    # Port already open = a llama-server is running or still loading: just wait for it.
    if (Test-Port $LlamaPort) { Wait-Backend; return }
    # Keep Ollama from grabbing the GPU/RAM behind our back (its tray app respawns a server).
    Get-Process -Name 'ollama app', ollama -ErrorAction SilentlyContinue | Stop-Process -Force

    $gguf = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root "ModelCache\$ModelFile" } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $gguf) { throw "Model file not found in <drive>:\ModelCache\: $ModelFile" }
    $mmproj = $null
    if ($MmprojFile) {
        $mmproj = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root "ModelCache\$MmprojFile" } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $mmproj) { throw "Vision projector not found in <drive>:\ModelCache\: $MmprojFile" }
    }

    $cuda = Join-Path $OllamaLib 'cuda_v12'
    $env:CUDA_VISIBLE_DEVICES = $GpuUuid                     # RTX 3050 only; RX 6600 / iGPU never seen
    $env:PATH = "$cuda;$OllamaLib;$env:PATH"
    $env:GGML_BACKEND_PATH = Join-Path $cuda 'ggml-cuda.dll'  # without this llama-server silently runs CPU-only

    # MTP speculative decoding (--spec-type draft-mtp --spec-draft-n-max 2) is deliberately OFF: measured
    # on this model it lifts generation 26.5 -> 31.1 tok/s but drops prompt processing 668 -> 178 tok/s,
    # a bad trade for an agent whose cold prompt is ~24k tokens.
    # -t/-tb 12 = one thread per E-core. -ub 4096 amortises streaming the CPU-resident experts over
    # PCIe per batch (512 -> 4096 is what makes prefill ~3x faster). --fit-target 200 = leave 200 MiB
    # of VRAM free instead of ~1.4 GB. --reasoning off = Qwen3.6 non-thinking mode.
    # llama-server's own --sleep-idle-seconds is NOT used: it dropped the prompt cache and crashed with a
    # CUDA error on wake (tested 04/10/2026); the wake proxy does the unloading instead.
    # Dense 27B: the KV cache competes with weights for the 6 GB card. Measured gen speed (Swift IQ3_S): 131072 ctx q8_0 KV = 2.1 tok/s,
    # 65536 q8_0 = 4.6, 131072 q4_0 = 4.55. So long contexts use a q4_0 KV cache.
    $KvType = if ($ModelAlias -like 'swift*' -and $Context -gt 65536) { 'q4_0' } else { 'q8_0' }
    $args2 = @(
        '--model', "`"$gguf`"", '--alias', $ModelAlias,
        '--host', '127.0.0.1', '--port', $LlamaPort, '--no-webui', '-np', '1',
        '-c', $Context, '-t', 12, '-tb', 12, '-b', 4096, '-ub', 4096,
        '--flash-attn', 'on', '-ctk', $KvType, '-ctv', $KvType,
        '--fit', 'on', '--fit-target', $FitTarget, '--no-mmap',
        '--jinja', '--reasoning', 'off',
        '--cache-ram', 6144
    )
    # Sampling: Occamy = Qwen3.6 non-thinking recommendation; Swift = its model card (thinking stays off via --reasoning off).
    if ($Sampling -eq 'swift') { $args2 += @('--temp', 1.0, '--top-p', 0.95, '--top-k', 20, '--min-p', 0) }
    else { $args2 += @('--temp', 0.7, '--top-p', 0.8, '--top-k', 20, '--min-p', 0, '--presence-penalty', 1.5) }
    if ($ChatTemplateFile) {
        $tpl = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root "ModelCache\$ChatTemplateFile" } |
            Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $tpl) { throw "Chat template not found in <drive>:\ModelCache\: $ChatTemplateFile" }
        $args2 += @('--chat-template-file', "`"$tpl`"")
    }
    if ($MtpDraft -gt 0) { $args2 += @('--spec-type', 'draft-mtp', '--spec-draft-n-max', $MtpDraft) }
    if ($mmproj) { $args2 += @('--mmproj', "`"$mmproj`""); if (-not $MmprojGpu) { $args2 += '--no-mmproj-offload' }; if ($ImageMaxTokens -gt 0) { $args2 += @('--image-max-tokens', $ImageMaxTokens) } }
    $srv = Start-Process -FilePath (Join-Path $OllamaLib 'llama-server.exe') -ArgumentList $args2 -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $LogDir 'llama-stdout.log') `
        -RedirectStandardError  (Join-Path $LogDir 'llama-stderr.log')
    # Launched from the Task Scheduler watchdog the process inherits BelowNormal priority (halved generation speed), so force Normal.
    $srv.PriorityClass = 'Normal'
    if ($PinECores) { $srv.ProcessorAffinity = $ECoreMask }
    Wait-Backend
}

# --- internal mode: the wake proxy asks for the backend and nothing else ------------------------
if ($BackendOnly) { Start-Backend; return }

if ($Restart) {
    Get-Process -Name 'ollama app', ollama, llama-server -ErrorAction SilentlyContinue | Stop-Process -Force
    Stop-ByCommandLine 'llama-wake\.py'
    # -NoGateway also means "leave the gateway alone" (the Local Agent Bridge swaps context size with
    # -Restart -NoGateway and must not drop the Discord bot).
    if (-not $NoGateway) { Stop-ByCommandLine 'entry\.js gateway run|openclaw\.mjs.*gateway run' }
    Start-Sleep -Seconds 3
}

# --- wake proxy on :8080 ------------------------------------------------------------------------
if (-not (Test-Port $ProxyPort)) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe' }
    $proxyArgs = @("`"$ProxyScript`"", '--ctx', $Context, '--idle-minutes', $IdleUnloadMinutes, '--alias', $ModelAlias,
        '--launcher', "`"$PSCommandPath`"")
    if (-not $KeepAwake) { $proxyArgs += '--no-keep-awake' }
    Start-Process -FilePath $py -WindowStyle Hidden -ArgumentList $proxyArgs `
        -RedirectStandardOutput (Join-Path $LogDir 'wake-proxy.log') `
        -RedirectStandardError  (Join-Path $LogDir 'wake-proxy-err.log')
    for ($i = 0; $i -lt 30 -and -not (Test-Port $ProxyPort); $i++) { Start-Sleep -Seconds 1 }
}

if ($ProxyOnly) { return }

# --- model (blocks until llama-server is up at the right context) --------------------------------
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$ProxyPort/_gate/ensure" -ContentType 'application/json' `
    -Body (@{ ctx = $Context } | ConvertTo-Json) -TimeoutSec 900 | Out-Null

# --- OpenClaw gateway ---------------------------------------------------------------------------
# Only one instance, ever. Racing instances fighting over Temp\openclaw\gateway.*.lock is the best
# explanation for the crash-loop breaker on 2026-08-09..13 (EPERM unlink, several PIDs in one ms).
# Re-apply the exec-guard Discord approval card patch (an OpenClaw / Discord plugin update wipes it). Idempotent, must run before the gateway starts.
if (-not $NoGateway) {
    $cardPatch = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'Local LLM\ExecGuard\patch_discord_card.py' } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($cardPatch) { & python $cardPatch | Out-Null }
}
if (-not $NoGateway -and -not (Test-Port $GatewayPort)) {
    Start-Process -FilePath cmd.exe -ArgumentList '/c', 'openclaw gateway run' -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'gateway-stdout.log') `
        -RedirectStandardError  (Join-Path $LogDir 'gateway-stderr.log')
}

# exec runs in mode "full" because the exec-guard plugin is the gate. If the gateway did not load it, fall back to mode "ask".
if (-not $NoGateway) {
    $guardCheck = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'Local LLM\ExecGuard\guard_check.py' } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($guardCheck) {
        for ($i = 0; $i -lt 60 -and -not (Test-Port $GatewayPort); $i++) { Start-Sleep -Seconds 1 }
        & python $guardCheck --wait 45 | Out-Null
    }
}

# A cold first turn re-evaluates the whole ~23k-token prompt (about a minute on this setup). One
# throwaway turn through the gateway leaves that prefix in llama-server's prompt cache.
if ($Warm -and -not $NoGateway) {
    for ($i = 0; $i -lt 60 -and -not (Test-Port $GatewayPort); $i++) { Start-Sleep -Seconds 1 }
    & openclaw agent --agent main -m 'Reply with the single word: ready' | Out-Null
}
