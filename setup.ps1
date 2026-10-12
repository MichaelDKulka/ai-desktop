# One-shot setup for Windows: virtualenv, install, then doctor.
# Run from the repo folder in PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup.ps1          install
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Dev     also install pytest
param([switch]$Dev)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# --- Python ------------------------------------------------------------------------------
function Get-PythonVersion($exe, $extra) {
    try {
        $out = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return [version]$out }
    } catch {}
    return $null
}

$pyExe = $null
foreach ($c in @(@{ exe = "py"; args = @("-3") }, @{ exe = "python"; args = @() }, @{ exe = "python3"; args = @() })) {
    if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
    $ver = Get-PythonVersion $c.exe $c.args
    if ($ver -and $ver -ge [version]"3.9") { $pyExe = $c.exe; $pyArgs = $c.args; $pyVer = $ver; break }
}
if (-not $pyExe) {
    Write-Host "Python 3.9 or newer is required. Install it with:  winget install Python.Python.3.12"
    Write-Host "(or from https://python.org, ticking 'Add python.exe to PATH'), then run this again."
    exit 1
}
Write-Host "Using Python $pyVer via '$pyExe $pyArgs'"

# --- Virtualenv + install ----------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $pyExe @pyArgs -m venv .venv
}
& .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
if ($Dev) {
    & .venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
} else {
    & .venv\Scripts\python.exe -m pip install --quiet -e .
}
Write-Host "Installed $(& .venv\Scripts\ai-desktop.exe --version) into .venv"

# --- Doctor ------------------------------------------------------------------------------
Write-Host ""
Write-Host "Running ai-desktop doctor:"
$ErrorActionPreference = "Continue"   # doctor exits 1 when a check fails; that's information, not a crash
& .venv\Scripts\ai-desktop.exe doctor
Write-Host ""
Write-Host "Done. Run it with:  .venv\Scripts\ai-desktop --help   (or: .venv\Scripts\Activate.ps1)"
Write-Host "Tip: 100% display scaling gives the most accurate clicks."
