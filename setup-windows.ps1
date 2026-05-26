<#
.SYNOPSIS
Installerer Bildebank på Windows.

.DESCRIPTION
Scriptet installerer nødvendige avhengigheter, kloner Bildebank fra GitHub,
setter opp Python-miljø og lager kommandoen bildebank.

.PARAMETER InstallDir
Mappen der Bildebank skal installeres.
Standard er ~/kode/bildebank.

.PARAMETER CommandName
Navnet på kommandoen som skal lages.
Standard er bildebank.

.PARAMETER RepoUrl
Git-repoet som skal klones.
Vanligvis trenger du ikke endre dette.

.PARAMETER Branch
Git-branchen som skal installeres eller oppdateres fra.
Standard er main.

.PARAMETER SkipInstall
Ikke installer Git eller Python automatisk.
Scriptet stopper i stedet hvis noe mangler.

.EXAMPLE
.\setup-windows.ps1

Kjører installasjonen med standardvalg.

.EXAMPLE
.\setup-windows.ps1 -InstallDir "$HOME\programmer\bildebank"

Installerer Bildebank i en annen mappe.

.EXAMPLE
.\setup-windows.ps1 -SkipInstall

Kjører uten automatisk installasjon av Git eller Python.

.EXAMPLE
.\setup-windows.ps1 -Branch devel

Installerer eller oppdaterer fra devel-branchen.

.EXAMPLE
.\setup-windows.ps1 -InstallDir "$HOME\kode\bildebank-test" -CommandName bb2

Installerer Bildebank i en testmappe og lager kommandoen bb2.
#>
param(
    [string]$RepoUrl = "https://github.com/tcamundsen/bildebank.git",
    [string]$Branch = "main",
    [string]$InstallDir = (Join-Path $HOME "kode\bildebank"),
    [string]$CommandName = "bildebank",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Test-Command {
    param([string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        $command = "$FilePath $($ArgumentList -join ' ')"
        throw "Kommando feilet med exit code ${LASTEXITCODE}: $command"
    }
}

function ConvertTo-AbsolutePath {
    param([string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Refresh-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Ensure-WingetPackage {
    param(
        [string]$PackageId,
        [string]$Name
    )

    if ($SkipInstall) {
        throw "$Name mangler. Installer $Name manuelt, eller kjør setup uten -SkipInstall."
    }
    if (-not (Test-Command "winget")) {
        throw "$Name mangler, og winget finnes ikke. Installer $Name manuelt og kjør setup på nytt."
    }

    Write-Step "Installerer $Name med winget"
    Invoke-Native -FilePath "winget" -ArgumentList @(
        "install",
        "--id",
        $PackageId,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements"
    )
    Refresh-ProcessPath
}

function Ensure-Git {
    if (Test-Command "git") {
        Write-Host "Git finnes allerede: $(git --version)"
        return
    }
    Ensure-WingetPackage -PackageId "Git.Git" -Name "Git for Windows"
    if (-not (Test-Command "git")) {
        throw "Git ble installert, men finnes ikke i PATH ennå. Lukk PowerShell og kjør setup på nytt."
    }
}

function Test-Python313 {
    if (-not (Test-Command "py")) {
        return $false
    }
    try {
        & py -3.13 --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Ensure-Python {
    if (Test-Python313) {
        Write-Host "Python 3.13 finnes allerede: $(py -3.13 --version)"
        return
    }
    Ensure-WingetPackage -PackageId "Python.Python.3.13" -Name "Python 3.13"
    if (-not (Test-Python313)) {
        throw "Python 3.13 ble installert, men py -3.13 virker ikke ennå. Lukk PowerShell og kjør setup på nytt."
    }
}

function Get-ValidatedCommandName {
    param([string]$Name)

    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "CommandName kan ikke være tom."
    }
    if ($Name.EndsWith(".cmd", [StringComparison]::OrdinalIgnoreCase)) {
        $Name = $Name.Substring(0, $Name.Length - 4)
    }
    if ($Name -notmatch '^[A-Za-z0-9_.-]+$') {
        throw "CommandName kan bare inneholde bokstaver, tall, punktum, understrek og bindestrek: $Name"
    }
    if ($Name -in @(".", "..")) {
        throw "CommandName kan ikke være '$Name'."
    }
    return $Name
}

function Get-ValidatedBranchName {
    param([string]$Name)

    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "Branch kan ikke være tom."
    }
    if ($Name -notmatch '^[A-Za-z0-9._/-]+$') {
        throw "Branch kan bare inneholde bokstaver, tall, punktum, understrek, bindestrek og skråstrek: $Name"
    }
    if ($Name.StartsWith("/") -or $Name.EndsWith("/") -or $Name.Contains("//")) {
        throw "Branch har ugyldig format: $Name"
    }
    return $Name
}

function Ensure-Repo {
    param(
        [string]$RepoDir,
        [string]$Branch
    )

    if (Test-Path -LiteralPath (Join-Path $RepoDir ".git")) {
        Write-Step "Oppdaterer eksisterende repo fra $Branch"
        Push-Location $RepoDir
        try {
            Invoke-Native -FilePath "git" -ArgumentList @("fetch", "origin")
            & git rev-parse --verify --quiet "refs/heads/$Branch" *> $null
            if ($LASTEXITCODE -eq 0) {
                Invoke-Native -FilePath "git" -ArgumentList @("switch", $Branch)
            } else {
                Invoke-Native -FilePath "git" -ArgumentList @("switch", "--track", "origin/$Branch")
            }
            Invoke-Native -FilePath "git" -ArgumentList @("pull", "--ff-only")
        } finally {
            Pop-Location
        }
        return
    }

    if (Test-Path -LiteralPath $RepoDir) {
        $children = Get-ChildItem -LiteralPath $RepoDir -Force
        if ($children.Count -gt 0) {
            throw "Installasjonsmappen finnes, men er ikke et tomt git-repo: $RepoDir"
        }
    } else {
        $parentDir = Split-Path -Parent $RepoDir
        if (-not [string]::IsNullOrWhiteSpace($parentDir)) {
            New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
        }
    }

    Write-Step "Laster ned bildebank fra GitHub ($Branch)"
    Invoke-Native -FilePath "git" -ArgumentList @("clone", "--branch", $Branch, $RepoUrl, $RepoDir)
}

function Ensure-Venv {
    param([string]$RepoDir)

    $venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Step "Lager Python-miljo"
        Push-Location $RepoDir
        try {
            Invoke-Native -FilePath "py" -ArgumentList @("-3.13", "-m", "venv", ".venv")
        } finally {
            Pop-Location
        }
    }

    Write-Step "Installerer bildebank i Python-miljoet"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "pip", "install", "-e", ".")
    } finally {
        Pop-Location
    }
}

function Ensure-ExifTool {
    param([string]$RepoDir)

    Write-Step "Installerer ExifTool"
    $venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "bildebank.cli", "exiftool-install")
    } catch {
        Write-Host "Kunne ikke installere ExifTool automatisk: $($_.Exception.Message)"
        Write-Host "Du kan prøve igjen etter setup med:"
        Write-Host "  $CommandName exiftool-install"
    }
}

function Ensure-CommandShim {
    param(
        [string]$BinDir,
        [string]$CommandName
    )

    $defaultShim = Join-Path $BinDir "bildebank.cmd"
    if (-not (Test-Path -LiteralPath $defaultShim)) {
        throw "Fant ikke kommando-wrapper: $defaultShim"
    }

    $commandShim = Join-Path $BinDir "$CommandName.cmd"
    if ([string]::Equals($CommandName, "bildebank", [StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    Write-Step "Lager kommandoen $CommandName"
    Copy-Item -LiteralPath $defaultShim -Destination $commandShim -Force
}

function Add-ToUserPath {
    param([string]$Directory)

    $resolved = (Resolve-Path -LiteralPath $Directory).Path
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @()
    if ($userPath) {
        $parts = $userPath -split ";" | Where-Object { $_ -ne "" }
    }

    $alreadyPresent = $false
    foreach ($part in $parts) {
        if ([string]::Equals($part.TrimEnd("\"), $resolved.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
            $alreadyPresent = $true
            break
        }
    }

    if (-not $alreadyPresent) {
        Write-Step "Legger bildebank i bruker-PATH"
        $newPath = if ($userPath) { "$userPath;$resolved" } else { $resolved }
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Refresh-ProcessPath
    } else {
        Write-Host "PATH inneholder allerede: $resolved"
    }
}

function Warn-CommandCollision {
    param(
        [string]$ExpectedBinDir,
        [string]$CommandName
    )

    $command = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return
    }
    if ([string]::IsNullOrWhiteSpace($command.Source)) {
        Write-Host "Advarsel: '$CommandName' finnes allerede som $($command.CommandType)."
        Write-Host "Hvis feil kommando starter, endre eller fjern den eksisterende kommandoen."
        return
    }
    $expected = (Resolve-Path -LiteralPath $ExpectedBinDir).Path
    $actual = Split-Path -Parent $command.Source
    if (-not [string]::Equals($actual.TrimEnd("\"), $expected.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)) {
        Write-Host "Advarsel: '$CommandName' finnes allerede her: $($command.Source)"
        Write-Host "Hvis feil kommando starter, flytt $expected tidligere i PATH."
    }
}

$CommandName = Get-ValidatedCommandName -Name $CommandName
$Branch = Get-ValidatedBranchName -Name $Branch
$InstallDir = ConvertTo-AbsolutePath -Path $InstallDir

Write-Step "Sjekker Git og Python"
Ensure-Git
Ensure-Python

$repoDir = $InstallDir
Ensure-Repo -RepoDir $repoDir -Branch $Branch
Ensure-Venv -RepoDir $repoDir
Ensure-ExifTool -RepoDir $repoDir

$binDir = Join-Path $repoDir "bin"
Ensure-CommandShim -BinDir $binDir -CommandName $CommandName
Add-ToUserPath -Directory $binDir
Warn-CommandCollision -ExpectedBinDir $binDir -CommandName $CommandName

Write-Step "Ferdig"
Write-Host "Programmet ligger i: $repoDir"
Write-Host "Start en ny PowerShell og test:"
Write-Host "  $CommandName --help"
Write-Host ""
Write-Host "Hvis du vil oppdatere senere:"
Write-Host "  $CommandName update"
