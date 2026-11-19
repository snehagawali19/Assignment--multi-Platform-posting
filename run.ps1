# Always start the API with this project's Python 3.12 venv.
# Do NOT use: python -m venv .venv  (that uses system Python 3.15 and breaks pydantic)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py312 = Join-Path $env:APPDATA "uv\python\cpython-3.12.15-windows-x86_64-none\python.exe"
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

function Ensure-Venv {
    $needsRebuild = $true
    if (Test-Path $venvPython) {
        $ver = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($ver -eq "3.12") {
            try {
                & $venvPython -c "import fastapi, pydantic_core" | Out-Null
                $needsRebuild = $false
            } catch {
                $needsRebuild = $true
            }
        }
    }
    if (-not $needsRebuild) { return }
    if (-not (Test-Path $py312)) {
        Write-Host "Python 3.12 not found. Install with: uv python install 3.12"
        exit 1
    }
    Write-Host "Rebuilding .venv with Python 3.12 (system python is 3.15 and breaks this project)..."
    if (Test-Path ".venv") { Remove-Item -Recurse -Force ".venv" }
    & $py312 -m venv .venv
    & $venvPython -m pip install -r requirements.txt
}

Ensure-Venv
& $venvPython -c "import sys; print('Using Python', sys.version.split()[0])"
& $venvPython -m uvicorn app.main:app --host 0.0.0.0 --port 4000
