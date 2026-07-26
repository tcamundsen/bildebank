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

function Invoke-GitCapture {
    param(
        [string]$RepoDir,
        [string[]]$ArgumentList
    )

    $output = & git -C $RepoDir @ArgumentList 2>&1
    if ($LASTEXITCODE -ne 0) {
        $command = "git -C $RepoDir $($ArgumentList -join ' ')"
        $details = (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
        if ($details) {
            throw "Kommando feilet med exit code ${LASTEXITCODE}: ${command}: $details"
        }
        throw "Kommando feilet med exit code ${LASTEXITCODE}: $command"
    }
    return (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
}

function ConvertTo-AbsolutePath {
    param([string]$Path)
    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Assert-PlainDirectory {
    param(
        [string]$Path,
        [string]$Description
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "$Description er ikke en mappe: $Path"
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description kan ikke vaere en lenke eller et reparse point: $Path"
    }
}

function Assert-PlainFile {
    param(
        [string]$Path,
        [string]$Description
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer) {
        throw "$Description er ikke en fil: $Path"
    }
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Description kan ikke vaere en lenke eller et reparse point: $Path"
    }
}

function Get-ValidatedRepoUrl {
    param([string]$Url)

    if ([string]::IsNullOrWhiteSpace($Url)) {
        throw "RepoUrl kan ikke vaere tom."
    }
    $Url = $Url.Trim()
    if ($Url.StartsWith("-") -or $Url -match "[`0`r`n]") {
        throw "RepoUrl har ugyldig format: $Url"
    }
    return $Url
}

function Get-NormalizedRepoUrl {
    param([string]$Url)

    $normalized = $Url.Trim().TrimEnd([char[]]@("/", "\"))
    if ($normalized.EndsWith(".git", [StringComparison]::OrdinalIgnoreCase)) {
        $normalized = $normalized.Substring(0, $normalized.Length - 4)
    }
    return $normalized
}

function Assert-ExpectedOrigin {
    param(
        [string]$RepoDir,
        [string]$ExpectedUrl
    )

    $actualUrl = Invoke-GitCapture -RepoDir $RepoDir -ArgumentList @(
        "remote",
        "get-url",
        "origin"
    )
    $actual = Get-NormalizedRepoUrl -Url $actualUrl
    $expected = Get-NormalizedRepoUrl -Url $ExpectedUrl
    if (-not [string]::Equals($actual, $expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw (
            "Installasjonsmappen bruker en annen Git-origin enn oppgitt RepoUrl. " +
            "Forventet: $ExpectedUrl. Fant: $actualUrl"
        )
    }
}

function Remove-LegacyPythonMetadata {
    param([string]$RepoDir)

    Remove-Item -LiteralPath (Join-Path $RepoDir "bilder.egg-info") -Recurse -Force -ErrorAction SilentlyContinue
}

function Assert-BildebankPythonPackage {
    param([string]$RepoDir)

    $initFile = Join-Path $RepoDir "bildebank\__init__.py"
    Assert-PlainFile -Path $initFile -Description "Bildebank-pakken"

    $venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
    $smokeDirectory = Join-Path $RepoDir "bildebank-tools\setup-smoke"
    New-Item -ItemType Directory -Path $smokeDirectory -Force | Out-Null
    Push-Location $smokeDirectory
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @(
            "-c",
            "import sys, bildebank, bildebank.cli; from pathlib import Path; actual = Path(bildebank.__file__).resolve(strict=True); expected = Path(sys.argv[1]).resolve(strict=True); sys.exit('bildebank importeres fra feil sted: %s' % actual) if actual != expected else None",
            $initFile
        )
    } finally {
        Pop-Location
    }
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
        & py -3.13 -c (
            "import platform, struct, sys; " +
            "ok = (sys.implementation.name == 'cpython' and " +
            "sys.version_info[:2] == (3, 13) and struct.calcsize('P') == 8 and " +
            "platform.machine().lower() in ('amd64', 'x86_64')); " +
            "sys.exit(0 if ok else 1)"
        ) *> $null
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
        throw (
            "64-bit CPython 3.13 for x64 ble installert, men virker ikke ennå. " +
            "Lukk PowerShell og kjør setup på nytt."
        )
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
    if (
        $Name.StartsWith("-") -or
        $Name.StartsWith("/") -or
        $Name.EndsWith("/") -or
        $Name.EndsWith(".") -or
        $Name.EndsWith(".lock", [StringComparison]::OrdinalIgnoreCase) -or
        $Name.Contains("//") -or
        $Name.Contains("..") -or
        $Name -in @(".", "..")
    ) {
        throw "Branch har ugyldig format: $Name"
    }
    return $Name
}

function Assert-GitBranchName {
    param([string]$Name)

    & git check-ref-format --branch $Name *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Branch er ikke et gyldig Git-branch-navn: $Name"
    }
}

function Assert-CleanRepo {
    param([string]$RepoDir)

    $status = Invoke-GitCapture -RepoDir $RepoDir -ArgumentList @(
        "status",
        "--porcelain=v1",
        "--untracked-files=no"
    )
    if ($status) {
        throw (
            "Programrepoet har lokale endringer i Git-sporede filer. " +
            "Commit eller tilbakestill dem for du kjører setup:`n$status"
        )
    }
}

function Get-UniqueSiblingPath {
    param(
        [string]$TargetPath,
        [string]$Purpose
    )

    $parentDir = Split-Path -Parent $TargetPath
    $leafName = Split-Path -Leaf $TargetPath
    if ([string]::IsNullOrWhiteSpace($parentDir) -or [string]::IsNullOrWhiteSpace($leafName)) {
        throw "InstallDir kan ikke vaere roten av en disk: $TargetPath"
    }
    do {
        $candidate = Join-Path $parentDir (
            ".$leafName.$Purpose-$([Guid]::NewGuid().ToString('N'))"
        )
    } while (Test-Path -LiteralPath $candidate)
    return $candidate
}

function Assert-ValidBildebankCheckout {
    param(
        [string]$RepoDir,
        [string]$Branch,
        [string]$ExpectedUrl
    )

    Assert-PlainDirectory -Path $RepoDir -Description "Git-repoet"
    Assert-PlainDirectory -Path (Join-Path $RepoDir ".git") -Description "Git-metadata"
    Assert-ExpectedOrigin -RepoDir $RepoDir -ExpectedUrl $ExpectedUrl
    Assert-CleanRepo -RepoDir $RepoDir

    $currentBranch = Invoke-GitCapture -RepoDir $RepoDir -ArgumentList @(
        "branch",
        "--show-current"
    )
    if (
        [string]::IsNullOrWhiteSpace($currentBranch) -or
        -not [string]::Equals(
            $currentBranch,
            $Branch,
            [StringComparison]::Ordinal
        )
    ) {
        throw (
            "Git-repoet er ikke på forventet branch '$Branch'. " +
            "Fant: '$currentBranch'. Bruk en egen InstallDir for en annen branch."
        )
    }

    $requiredFiles = @(
        "pyproject.toml",
        "requirements\windows-py313-base.lock",
        "bildebank\__init__.py",
        "bin\bildebank.cmd",
        "update.ps1"
    )
    foreach ($relativePath in $requiredFiles) {
        Assert-PlainFile `
            -Path (Join-Path $RepoDir $relativePath) `
            -Description "Programfil"
    }
}

function Update-ExistingRepo {
    param(
        [string]$RepoDir,
        [string]$Branch,
        [string]$ExpectedUrl
    )

    Assert-ValidBildebankCheckout `
        -RepoDir $RepoDir `
        -Branch $Branch `
        -ExpectedUrl $ExpectedUrl
    $upstream = Invoke-GitCapture -RepoDir $RepoDir -ArgumentList @(
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}"
    )
    $expectedUpstream = "origin/$Branch"
    if (-not [string]::Equals(
        $upstream,
        $expectedUpstream,
        [StringComparison]::Ordinal
    )) {
        throw (
            "Branch '$Branch' følger ikke '$expectedUpstream'. Fant: '$upstream'."
        )
    }

    $updateScript = Join-Path $RepoDir "update.ps1"
    Write-Step "Oppdaterer eksisterende installasjon fra $Branch"
    & $updateScript -RepoDir $RepoDir
    if (-not $?) {
        throw "update.ps1 klarte ikke å oppdatere installasjonen."
    }
    Assert-ValidBildebankCheckout `
        -RepoDir $RepoDir `
        -Branch $Branch `
        -ExpectedUrl $ExpectedUrl
}

function Clone-NewRepoToStaging {
    param(
        [string]$StagingDir,
        [string]$Branch,
        [string]$ExpectedUrl
    )

    Write-Step "Laster ned bildebank fra GitHub ($Branch)"
    Invoke-Native -FilePath "git" -ArgumentList @(
        "clone",
        "--branch",
        $Branch,
        "--single-branch",
        "--",
        $ExpectedUrl,
        $StagingDir
    )
    Assert-ValidBildebankCheckout `
        -RepoDir $StagingDir `
        -Branch $Branch `
        -ExpectedUrl $ExpectedUrl
}

function Publish-NewRepo {
    param(
        [string]$StagingDir,
        [string]$RepoDir
    )

    $backupDir = $null
    if (Test-Path -LiteralPath $RepoDir) {
        Assert-PlainDirectory -Path $RepoDir -Description "Installasjonsmappen"
        $children = @(Get-ChildItem -LiteralPath $RepoDir -Force)
        if ($children.Count -gt 0) {
            throw "Installasjonsmappen finnes, men er ikke et tomt git-repo: $RepoDir"
        }
        $backupDir = Get-UniqueSiblingPath -TargetPath $RepoDir -Purpose "setup-empty"
        Move-Item -LiteralPath $RepoDir -Destination $backupDir
    }

    try {
        Move-Item -LiteralPath $StagingDir -Destination $RepoDir
    } catch {
        if (
            -not (Test-Path -LiteralPath $RepoDir) -and
            $backupDir -and
            (Test-Path -LiteralPath $backupDir)
        ) {
            Move-Item -LiteralPath $backupDir -Destination $RepoDir
        }
        throw
    }
    return $backupDir
}

function Restore-NewRepoAfterFailure {
    param(
        [string]$RepoDir,
        [AllowNull()]
        [string]$BackupDir
    )

    $failedDir = $null
    if (Test-Path -LiteralPath $RepoDir) {
        $failedDir = Get-UniqueSiblingPath -TargetPath $RepoDir -Purpose "setup-failed"
        Move-Item -LiteralPath $RepoDir -Destination $failedDir
    }
    if (
        $BackupDir -and
        (Test-Path -LiteralPath $BackupDir) -and
        -not (Test-Path -LiteralPath $RepoDir)
    ) {
        Move-Item -LiteralPath $BackupDir -Destination $RepoDir
    }
    if ($failedDir) {
        Write-Host "Den ufullstendige installasjonen er bevart her: $failedDir"
    }
}

function Complete-NewRepoPublication {
    param(
        [AllowNull()]
        [string]$BackupDir
    )

    if ($BackupDir -and (Test-Path -LiteralPath $BackupDir)) {
        Assert-PlainDirectory -Path $BackupDir -Description "Backup av tom installasjonsmappe"
        if (@(Get-ChildItem -LiteralPath $BackupDir -Force).Count -ne 0) {
            throw "Backupen av den opprinnelige installasjonsmappen er ikke tom: $BackupDir"
        }
        Remove-Item -LiteralPath $BackupDir -Force
    }
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
        Remove-LegacyPythonMetadata -RepoDir $RepoDir
        $dependencyLock = Join-Path $RepoDir "requirements\windows-py313-base.lock"
        if (-not (Test-Path -LiteralPath $dependencyLock)) {
            throw "Installasjonen mangler dependency-lockfilen: $dependencyLock"
        }
        Invoke-Native -FilePath $venvPython -ArgumentList @(
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            $dependencyLock
        )
        Invoke-Native -FilePath $venvPython -ArgumentList @(
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            "."
        )
        Assert-BildebankPythonPackage -RepoDir $RepoDir
    } finally {
        Pop-Location
    }
}

function Ensure-ExifTool {
    param([string]$RepoDir)

    Write-Step "Installerer ExifTool"
    $venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "bildebank", "exiftool-install")
    } catch {
        Write-Host "Kunne ikke installere ExifTool automatisk: $($_.Exception.Message)"
        Write-Host "Du kan prøve igjen etter setup med:"
        Write-Host "  $CommandName exiftool-install"
    } finally {
        Pop-Location
    }
}

function Ensure-FFmpeg {
    param([string]$RepoDir)

    Write-Step "Installerer FFmpeg"
    $venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
    Push-Location $RepoDir
    try {
        Invoke-Native -FilePath $venvPython -ArgumentList @("-m", "bildebank", "ffmpeg-install")
    } catch {
        Write-Host "Kunne ikke installere FFmpeg automatisk: $($_.Exception.Message)"
        Write-Host "Bildebank er fortsatt installert. Du kan prøve igjen etter setup med:"
        Write-Host "  $CommandName ffmpeg-install"
    } finally {
        Pop-Location
    }
}

function Ensure-CommandShim {
    param(
        [string]$BinDir,
        [string]$CommandName
    )

    $defaultShim = Join-Path $BinDir "bildebank.cmd"
    Assert-PlainFile -Path $defaultShim -Description "Kommando-wrapper"

    $commandShim = Join-Path $BinDir "$CommandName.cmd"
    if ([string]::Equals($CommandName, "bildebank", [StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if (Test-Path -LiteralPath $commandShim) {
        Assert-PlainFile -Path $commandShim -Description "Eksisterende kommando-wrapper"
    }

    Write-Step "Lager kommandoen $CommandName"
    $tempShim = Join-Path $BinDir (
        ".$CommandName.cmd.setup-$([Guid]::NewGuid().ToString('N'))"
    )
    try {
        Copy-Item -LiteralPath $defaultShim -Destination $tempShim
        Move-Item -LiteralPath $tempShim -Destination $commandShim -Force
    } finally {
        Remove-Item -LiteralPath $tempShim -Force -ErrorAction SilentlyContinue
    }
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
$RepoUrl = Get-ValidatedRepoUrl -Url $RepoUrl
$InstallDir = ConvertTo-AbsolutePath -Path $InstallDir

Write-Step "Sjekker Git og Python"
Ensure-Git
Ensure-Python
Assert-GitBranchName -Name $Branch

$repoDir = $InstallDir
$gitDir = Join-Path $repoDir ".git"
$newInstall = -not (Test-Path -LiteralPath $gitDir)
$newInstallBackup = $null

if ($newInstall) {
    if (Test-Path -LiteralPath $repoDir) {
        Assert-PlainDirectory -Path $repoDir -Description "Installasjonsmappen"
        if (@(Get-ChildItem -LiteralPath $repoDir -Force).Count -gt 0) {
            throw "Installasjonsmappen finnes, men er ikke et tomt git-repo: $repoDir"
        }
    } else {
        $parentDir = Split-Path -Parent $repoDir
        if ([string]::IsNullOrWhiteSpace($parentDir)) {
            throw "InstallDir kan ikke vaere roten av en disk: $repoDir"
        }
        New-Item -ItemType Directory -Path $parentDir -Force | Out-Null
    }

    $stagingDir = Get-UniqueSiblingPath -TargetPath $repoDir -Purpose "setup-staging"
    try {
        Clone-NewRepoToStaging `
            -StagingDir $stagingDir `
            -Branch $Branch `
            -ExpectedUrl $RepoUrl
        $newInstallBackup = Publish-NewRepo `
            -StagingDir $stagingDir `
            -RepoDir $repoDir
    } catch {
        if (Test-Path -LiteralPath $stagingDir) {
            Write-Host "Ufullstendig staging er bevart her: $stagingDir"
        }
        throw
    }
} else {
    Update-ExistingRepo `
        -RepoDir $repoDir `
        -Branch $Branch `
        -ExpectedUrl $RepoUrl
}

try {
    if ($newInstall) {
        Ensure-Venv -RepoDir $repoDir
    }
    Ensure-ExifTool -RepoDir $repoDir
    Ensure-FFmpeg -RepoDir $repoDir

    $binDir = Join-Path $repoDir "bin"
    Ensure-CommandShim -BinDir $binDir -CommandName $CommandName
    Add-ToUserPath -Directory $binDir
    Warn-CommandCollision -ExpectedBinDir $binDir -CommandName $CommandName

    if ($newInstall) {
        Complete-NewRepoPublication -BackupDir $newInstallBackup
    }
} catch {
    $setupError = $_.Exception.Message
    if ($newInstall) {
        try {
            Restore-NewRepoAfterFailure `
                -RepoDir $repoDir `
                -BackupDir $newInstallBackup
        } catch {
            throw (
                "Installasjonen feilet, og den opprinnelige installasjonsmappen " +
                "kunne ikke gjenopprettes. Installasjonsfeil: $setupError. " +
                "Rollback-feil: $($_.Exception.Message)"
            )
        }
    }
    throw "Installasjonen feilet: $setupError"
}

Write-Step "Ferdig"
Write-Host "Programmet ligger i: $repoDir"
Write-Host "Start en ny PowerShell og test:"
Write-Host "  $CommandName --help"
Write-Host ""
Write-Host "Hvis du vil oppdatere senere:"
Write-Host "  $CommandName update"
