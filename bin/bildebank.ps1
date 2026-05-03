$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = Split-Path -Parent $ScriptDir
$Python = Join-Path $RepoDir ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Error "Fant ikke '$Python'. Opprett venv og installer prosjektet først: py -3.13 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e ."
    exit 1
}

& $Python -m bilder @args
exit $LASTEXITCODE
