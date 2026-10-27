param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptRoot "..")).Path
$ServiceScript = Join-Path $ScriptRoot "cosmos-service.ps1"
$FrontendIndex = Join-Path $ProjectRoot "apps\web\dist\index.html"
$FrontendRoot = Join-Path $ProjectRoot "apps\web"
$FrontendSource = Join-Path $FrontendRoot "src"

$needsBuild = -not (Test-Path -LiteralPath $FrontendIndex)
if (-not $needsBuild) {
    $buildTime = (Get-Item -LiteralPath $FrontendIndex).LastWriteTimeUtc
    $sourceFiles = @(
        Get-ChildItem -LiteralPath $FrontendSource -File -Recurse
        Get-Item -LiteralPath (Join-Path $FrontendRoot "package.json")
        Get-Item -LiteralPath (Join-Path $ProjectRoot "package-lock.json")
    )
    $needsBuild = $null -ne ($sourceFiles | Where-Object { $_.LastWriteTimeUtc -gt $buildTime })
}

if ($needsBuild) {
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

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ServiceScript start -Port $Port
if ($LASTEXITCODE -ne 0) {
    throw "COSMOS service startup failed with exit code $LASTEXITCODE."
}

$url = "http://127.0.0.1:$Port"
Start-Process $url
Write-Output "COSMOS is ready at $url."

Write-Output "Pair the browser using the operator key file location printed above."
Write-Output "Trading automation status is available in the authenticated workspace."
