param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status")]
    [string]$Action = "status",
    [switch]$Build,
    [switch]$StartPaused,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptRoot "..")).Path
$OutputRoot = Join-Path $ProjectRoot "output"
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$FrontendIndex = Join-Path $ProjectRoot "apps\web\dist\index.html"
$StatePath = Join-Path $OutputRoot "cosmos-api.pid.json"
$StdoutPath = Join-Path $OutputRoot "cosmos-managed.stdout.log"
$StderrPath = Join-Path $OutputRoot "cosmos-managed.stderr.log"

function Test-CosmosHealth {
    param([int]$HealthPort)
    try {
        $response = Invoke-RestMethod `
            -Uri "http://127.0.0.1:$HealthPort/api/health" `
            -TimeoutSec 2
        return $null -ne $response
    }
    catch {
        return $false
    }
}

function Get-CosmosState {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Service state is unreadable: $StatePath"
    }
}

function Get-OwnedCosmosProcess {
    param($State)
    if ($null -eq $State -or $null -eq $State.pid) {
        return $null
    }
    $process = Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $details = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)"
    $commandLine = [string]$details.CommandLine
    if (
        $commandLine -notmatch [regex]::Escape("cosmos_quant.api:app") -or
        $commandLine -notmatch [regex]::Escape("uvicorn")
    ) {
        throw "PID $($process.Id) is not the managed COSMOS API process; refusing to control it."
    }
    return $process
}

function Stop-CosmosProcessTree {
    param([System.Diagnostics.Process]$Process)
    $allProcesses = @(Get-CimInstance Win32_Process)
    $pending = @([int]$Process.Id)
    $descendants = @()
    while ($pending.Count -gt 0) {
        $parentId = $pending[0]
        $pending = @($pending | Select-Object -Skip 1)
        $children = @($allProcesses | Where-Object { $_.ParentProcessId -eq $parentId })
        foreach ($child in $children) {
            if ($child.Name -in @("conhost.exe", "OpenConsole.exe")) {
                continue
            }
            $commandLine = [string]$child.CommandLine
            $isUvicornProcess = (
                $commandLine -match [regex]::Escape("cosmos_quant.api:app") -and
                $commandLine -match [regex]::Escape("uvicorn")
            )
            $isResearchWorker = (
                $commandLine -match [regex]::Escape("anyio.to_process") -and
                $child.Name -eq "python.exe"
            )
            if (
                -not $isUvicornProcess -and
                -not $isResearchWorker
            ) {
                throw "Child PID $($child.ProcessId) is not a COSMOS API process; refusing to stop the process tree."
            }
            $descendants += [int]$child.ProcessId
            $pending += [int]$child.ProcessId
        }
    }
    foreach ($processId in @($descendants | Sort-Object -Descending)) {
        Stop-Process -Id $processId -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $Process.Id -ErrorAction SilentlyContinue
}

function Remove-StaleState {
    $resolvedOutput = [System.IO.Path]::GetFullPath($OutputRoot)
    $resolvedState = [System.IO.Path]::GetFullPath($StatePath)
    if (-not $resolvedState.StartsWith($resolvedOutput, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a state file outside the project output directory."
    }
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force
    }
}

function Show-Status {
    $state = Get-CosmosState
    $process = Get-OwnedCosmosProcess -State $state
    $healthPort = if ($null -ne $state -and $null -ne $state.port) {
        [int]$state.port
    }
    else {
        $Port
    }
    $healthy = Test-CosmosHealth -HealthPort $healthPort
    return [pscustomobject]@{
        healthy = $healthy
        managed_process_running = $null -ne $process
        pid = if ($process) { $process.Id } else { $null }
        port = $healthPort
        url = "http://127.0.0.1:$healthPort"
        data_dir = if ($state) { $state.data_dir } else { $null }
        state_file = $StatePath
    }
}

switch ($Action) {
    "status" {
        $status = Show-Status
        $status | ConvertTo-Json | Write-Output
        if (-not $status.healthy) {
            exit 1
        }
        return
    }
    "start" {
        if (-not (Test-Path -LiteralPath $PythonPath)) {
            throw "Python runtime is missing: $PythonPath"
        }
        New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
        Push-Location $ProjectRoot
        try {
            $dataDirectory = & $PythonPath -c "import sys; sys.path.insert(0, 'backend'); from cosmos_quant.settings import Settings; print(Settings().cosmos_data_dir.resolve())"
            if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the configured data directory." }
            $dataDirectory = ([string]$dataDirectory).Trim()
        }
        finally { Pop-Location }
        $existingState = Get-CosmosState
        $existingProcess = Get-OwnedCosmosProcess -State $existingState
        if ($existingProcess) {
            if (-not $existingState.data_dir -or $existingState.data_dir -ne $dataDirectory) {
                throw "A managed instance uses a different or unknown data directory; stop it before starting this configuration."
            }
            $existingPort = [int]$existingState.port
            if (Test-CosmosHealth -HealthPort $existingPort) {
                Write-Output "COSMOS API is already healthy on port $existingPort (PID $($existingProcess.Id))."
                return
            }
            throw "Managed COSMOS process PID $($existingProcess.Id) exists but is unhealthy; inspect $StderrPath."
        }
        if ($existingState) {
            Remove-StaleState
        }
        if ($Build) {
            Push-Location $ProjectRoot
            try {
                & npm run build
                if ($LASTEXITCODE -ne 0) {
                    throw "Frontend production build failed with exit code $LASTEXITCODE."
                }
            }
            finally {
                Pop-Location
            }
        }
        if (-not (Test-Path -LiteralPath $FrontendIndex)) {
            throw "Frontend build is missing. Run with -Build once before managed startup."
        }
        $arguments = @(
            "-m", "uvicorn",
            "cosmos_quant.api:app",
            "--app-dir", "backend",
            "--host", "127.0.0.1",
            "--port", "$Port"
        )
        $previousStartPaused = $env:COSMOS_AUTOMATION_START_PAUSED
        if ($StartPaused) { $env:COSMOS_AUTOMATION_START_PAUSED = "true" }
        try {
        $process = Start-Process `
            -FilePath $PythonPath `
            -ArgumentList $arguments `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -PassThru
        }
        finally { $env:COSMOS_AUTOMATION_START_PAUSED = $previousStartPaused }
        @{
            pid = $process.Id
            port = $Port
            project_root = $ProjectRoot
            data_dir = $dataDirectory
            started_at = [DateTimeOffset]::UtcNow.ToString("o")
        } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding utf8

        # A large local research ledger may need time for an integrity-checked
        # pre-migration SQLite backup before FastAPI can report healthy.
        $deadline = [DateTime]::UtcNow.AddSeconds(180)
        while ([DateTime]::UtcNow -lt $deadline) {
            if ($process.HasExited) {
                $tail = if (Test-Path -LiteralPath $StderrPath) {
                    (Get-Content -LiteralPath $StderrPath -Tail 20) -join [Environment]::NewLine
                }
                else {
                    "No stderr log was created."
                }
                Remove-StaleState
                throw "COSMOS API exited during startup.$([Environment]::NewLine)$tail"
            }
            if (Test-CosmosHealth -HealthPort $Port) {
                Write-Output "COSMOS API is healthy at http://127.0.0.1:$Port (PID $($process.Id))."
                return
            }
            Start-Sleep -Milliseconds 500
            $process.Refresh()
        }
        if (-not $process.HasExited) {
            Stop-CosmosProcessTree -Process $process
        }
        Remove-StaleState
        throw "COSMOS API did not become healthy within 180 seconds; inspect $StderrPath."
    }
    "stop" {
        $state = Get-CosmosState
        $process = Get-OwnedCosmosProcess -State $state
        if ($null -eq $process) {
            if ($state) {
                Remove-StaleState
            }
            Write-Output "COSMOS managed API is not running."
            return
        }
        Stop-CosmosProcessTree -Process $process
        try {
            Wait-Process -Id $process.Id -Timeout 15 -ErrorAction Stop
        }
        catch {
            if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
                throw "COSMOS API PID $($process.Id) did not stop within 15 seconds."
            }
        }
        Remove-StaleState
        Write-Output "COSMOS API PID $($process.Id) stopped."
        return
    }
}
