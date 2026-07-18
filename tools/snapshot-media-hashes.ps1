[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Collection,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [string]$CompareWith,

    [switch]$Force
)

$ErrorActionPreference = "Stop"

$mediaExtensions = @(
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".heic", ".heif", ".webp", ".nef", ".psd", ".raw", ".mp4",
    ".mp", ".mov", ".avi", ".m4v", ".mpg", ".mpeg", ".mts",
    ".m2ts", ".3gp", ".wmv"
)

function Get-FullPath {
    param([string]$Path)

    return $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

function Test-IsInsideDirectory {
    param(
        [string]$Path,
        [string]$Directory
    )

    $separator = [System.IO.Path]::DirectorySeparatorChar
    $prefix = $Directory.TrimEnd([char[]]@("\", "/")) + $separator
    return $Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

if (-not (Test-Path -LiteralPath $Collection -PathType Container)) {
    throw "Fant ikke samlingsmappen: $Collection"
}

$collectionPath = (Resolve-Path -LiteralPath $Collection).Path.TrimEnd([char[]]@("\", "/"))
$outputPath = Get-FullPath -Path $Output
$outputDirectory = Split-Path -Parent $outputPath
$comparePath = $null

if ($CompareWith) {
    if (-not (Test-Path -LiteralPath $CompareWith -PathType Leaf)) {
        throw "Fant ikke fasitlisten: $CompareWith"
    }
    $comparePath = (Resolve-Path -LiteralPath $CompareWith).Path
    if ($outputPath.Equals($comparePath, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Utdatafilen må være en annen fil enn fasitlisten."
    }
}

if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    throw "Mappen for utdatafilen finnes ikke: $outputDirectory"
}
if (Test-IsInsideDirectory $outputPath $collectionPath) {
    throw "Utdatafilen må ligge utenfor bildesamlingen: $outputPath"
}
if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
    throw "Utdatafilen finnes allerede. Velg et annet navn eller bruk -Force: $outputPath"
}

$records = @(
    Get-ChildItem -LiteralPath $collectionPath -File -Recurse -Force |
        ForEach-Object {
            $relativePath = $_.FullName.Substring($collectionPath.Length)
            $relativePath = $relativePath.TrimStart([char[]]@("\", "/")).Replace("\", "/")
            $isThumbnail = $relativePath.StartsWith(
                "thumbs/",
                [System.StringComparison]::OrdinalIgnoreCase
            )
            if (($mediaExtensions -contains $_.Extension.ToLowerInvariant()) -and -not $isThumbnail) {
                [PSCustomObject][ordered]@{
                    path = $relativePath
                    size_bytes = $_.Length
                    sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
                }
            }
        } |
        Sort-Object path
)

$records | Export-Csv -LiteralPath $outputPath -NoTypeInformation -Encoding UTF8
$totalBytes = ($records | Measure-Object -Property size_bytes -Sum).Sum
if ($null -eq $totalBytes) {
    $totalBytes = 0
}

Write-Host "SHA-256-liste opprettet"
Write-Host "  Bildesamling: $collectionPath"
Write-Host "  Utdatafil: $outputPath"
Write-Host "  Mediefiler: $($records.Count)"
Write-Host "  Byte: $totalBytes"

if (-not $CompareWith) {
    exit 0
}

$expectedRows = @(Import-Csv -LiteralPath $comparePath)
$expectedByPath = @{}
$actualByPath = @{}

foreach ($row in $expectedRows) {
    if (-not $row.path -or $null -eq $row.size_bytes -or -not $row.sha256) {
        throw "Fasitlisten må ha kolonnene path, size_bytes og sha256: $comparePath"
    }
    if ($expectedByPath.ContainsKey($row.path)) {
        throw "Fasitlisten har flere poster for samme sti: $($row.path)"
    }
    $expectedByPath[$row.path] = $row
}
foreach ($row in $records) {
    if ($actualByPath.ContainsKey($row.path)) {
        throw "Bildesamlingen har flere mediefiler med samme portable sti: $($row.path)"
    }
    $actualByPath[$row.path] = $row
}

$differences = [System.Collections.Generic.List[string]]::new()
foreach ($path in ($expectedByPath.Keys | Sort-Object)) {
    if (-not $actualByPath.ContainsKey($path)) {
        $differences.Add("Mangler i restore: $path")
        continue
    }
    $expected = $expectedByPath[$path]
    $actual = $actualByPath[$path]
    if (([string]$expected.size_bytes -ne [string]$actual.size_bytes) -or
        ([string]$expected.sha256 -ne [string]$actual.sha256)) {
        $differences.Add("Endret innhold eller størrelse: $path")
    }
}
foreach ($path in ($actualByPath.Keys | Sort-Object)) {
    if (-not $expectedByPath.ContainsKey($path)) {
        $differences.Add("Uventet i restore: $path")
    }
}

if ($differences.Count -gt 0) {
    Write-Host "Sammenligning feilet"
    foreach ($difference in $differences) {
        Write-Host "  $difference"
    }
    Write-Host "  Avvik: $($differences.Count)"
    exit 1
}

Write-Host "Sammenligning bestått"
Write-Host "  Fasit: $comparePath"
Write-Host "  Avvik: 0"
exit 0
