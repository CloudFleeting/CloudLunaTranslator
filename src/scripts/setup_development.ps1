[CmdletBinding()]
param(
    [string]$ReleaseTag,
    [switch]$KeepArchive
)

$ErrorActionPreference = "Stop"

$sourceRoot = Split-Path -Parent $PSScriptRoot
$version = (Get-Content -Raw -LiteralPath (Join-Path $sourceRoot "version.txt")).Trim()
if (-not $ReleaseTag) {
    $ReleaseTag = "v$version"
}

$expectedTag = "v$version"
if ($ReleaseTag -ne $expectedTag) {
    throw "Release tag $ReleaseTag does not match the checkout version $expectedTag."
}

$releaseUri = "https://api.github.com/repos/HIllya51/LunaTranslator/releases/tags/$ReleaseTag"
$headers = @{ "User-Agent" = "CloudLunaTranslator-Development-Setup" }
$release = Invoke-RestMethod -Headers $headers -Uri $releaseUri
$asset = $release.assets | Where-Object { $_.name -eq "LunaTranslator_x64.zip" } | Select-Object -First 1
if (-not $asset) {
    throw "The $ReleaseTag release does not contain LunaTranslator_x64.zip."
}
if (-not $asset.digest -or -not $asset.digest.StartsWith("sha256:")) {
    throw "GitHub did not provide a SHA-256 digest for $($asset.name)."
}

$workRoot = Join-Path $env:TEMP "cloud-luna-runtime-$($ReleaseTag.TrimStart('v'))"
$archivePath = Join-Path $workRoot $asset.name
$extractRoot = Join-Path $workRoot "extracted"

if (Test-Path -LiteralPath $workRoot) {
    $resolvedWorkRoot = (Resolve-Path -LiteralPath $workRoot).Path
    $resolvedTemp = (Resolve-Path -LiteralPath $env:TEMP).Path
    if (-not $resolvedWorkRoot.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean an unexpected work directory: $resolvedWorkRoot"
    }
    Remove-Item -LiteralPath $resolvedWorkRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $workRoot | Out-Null

Write-Host "Downloading $($asset.browser_download_url)"
Invoke-WebRequest -Headers $headers -Uri $asset.browser_download_url -OutFile $archivePath

$expectedHash = $asset.digest.Substring("sha256:".Length).ToLowerInvariant()
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "Checksum mismatch for $archivePath. Expected $expectedHash, got $actualHash."
}

$archiveCommand = @("K7C.exe", "K7.exe", "NanaZipC.exe") |
    ForEach-Object { Get-Command $_ -ErrorAction SilentlyContinue } |
    Select-Object -First 1

if ($archiveCommand) {
    Write-Host "Extracting with $($archiveCommand.Source)"
    & $archiveCommand.Source x -y "-o$extractRoot" $archivePath
    if ($LASTEXITCODE -ne 0) {
        throw "$($archiveCommand.Name) exited with code $LASTEXITCODE."
    }
}
else {
    Write-Warning "No NanaZip command alias was found; using Windows ZIP extraction."
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot
}

$packageRoot = Join-Path $extractRoot "LunaTranslator_x64"
if (-not (Test-Path -LiteralPath (Join-Path $packageRoot "LunaTranslator.exe"))) {
    throw "The extracted package layout is not recognized: $packageRoot"
}

foreach ($launcher in @("LunaTranslator.exe", "LunaTranslator_admin.exe", "LunaTranslator_debug.bat")) {
    Copy-Item -LiteralPath (Join-Path $packageRoot $launcher) -Destination (Join-Path $sourceRoot $launcher) -Force
}

$packageFiles = Join-Path $packageRoot "files"
$targetFiles = Join-Path $sourceRoot "files"
foreach ($directory in @("DLL64", "LunaHook", "Magpie", "ocrmodel", "runtime3.13-64")) {
    $source = Join-Path $packageFiles $directory
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination $targetFiles -Recurse -Force
    }
}
foreach ($executable in @("LunaSubprocess32.exe", "LunaSubprocess64.exe")) {
    Copy-Item -LiteralPath (Join-Path $packageFiles $executable) -Destination (Join-Path $targetFiles $executable) -Force
}

$localeRoot = Join-Path $packageFiles "Locale"
Get-ChildItem -LiteralPath $localeRoot -Recurse -File | Where-Object { $_.Extension -ne ".xml" } | ForEach-Object {
    $relativePath = $_.FullName.Substring($localeRoot.Length + 1)
    $destination = Join-Path (Join-Path $targetFiles "Locale") $relativePath
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
}

New-Item -ItemType Directory -Path (Join-Path $sourceRoot "userconfig-dev") -Force | Out-Null

Write-Host "Installed the verified $ReleaseTag x64 runtime into $sourceRoot"
Write-Host "SHA-256: $actualHash"
Write-Host "Development profile: $(Join-Path $sourceRoot 'userconfig-dev')"

if (-not $KeepArchive) {
    Remove-Item -LiteralPath $workRoot -Recurse -Force
}
