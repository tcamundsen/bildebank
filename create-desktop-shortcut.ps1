param(
    [string]$RepoDir = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

$shell = New-Object -ComObject WScript.Shell
$desktopDir = $shell.SpecialFolders.Item("Desktop")
if ([string]::IsNullOrWhiteSpace($desktopDir) -or
    -not (Test-Path -LiteralPath $desktopDir -PathType Container)) {
    throw "Fant ikke brukerens skrivebordsmappe."
}

$shortcutPath = Join-Path $desktopDir "Bildebank.lnk"
if (Test-Path -LiteralPath $shortcutPath) {
    return
}

$startScript = Join-Path $RepoDir "start-bildebank.cmd"
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf)) {
    throw "Fant ikke startfilen: $startScript"
}

$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = [IO.Path]::GetFullPath($startScript)
$shortcut.WorkingDirectory = [IO.Path]::GetFullPath($RepoDir)
$shortcut.Description = "Start Bildebank"
$shortcut.Save()
Write-Host "Laget skrivebordssnarvei: $shortcutPath"
