param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$MaiBotRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) ".dev\maibot")
)

$ErrorActionPreference = "Stop"
$source = (Resolve-Path -LiteralPath $SourceRoot).Path
$maibot = (Resolve-Path -LiteralPath $MaiBotRoot).Path
$target = Join-Path $maibot "data\MaiMBot\plugins\maitu-photo-studio"
$expectedPrefix = (Join-Path $maibot "data\MaiMBot\plugins") + [IO.Path]::DirectorySeparatorChar
$resolvedTarget = [IO.Path]::GetFullPath($target)

if (-not $resolvedTarget.StartsWith($expectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Plugin target escaped the MaiBot plugin directory: $resolvedTarget"
}

$rootFiles = @(
    "_manifest.json",
    "plugin.py",
    "requirements.txt",
    "README.md"
)
$packageFiles = @(
    "__init__.py",
    "commands.py",
    "compression.py",
    "config.py",
    "continuity.py",
    "gallery.py",
    "llm_adapter.py",
    "logging_utils.py",
    "models.py",
    "prompts.py",
    "provider.py",
    "reference_service.py",
    "runtime.py",
    "sdk_compat.py",
    "selection.py",
    "service.py",
    "storage.py",
    "task_manager.py"
)

New-Item -ItemType Directory -Force -Path $resolvedTarget | Out-Null
$packageTarget = Join-Path $resolvedTarget "maitu_photo"
New-Item -ItemType Directory -Force -Path $packageTarget | Out-Null

foreach ($name in $rootFiles) {
    $from = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $from -PathType Leaf)) {
        throw "Required plugin file is missing: $from"
    }
    Copy-Item -LiteralPath $from -Destination (Join-Path $resolvedTarget $name) -Force
}

foreach ($name in $packageFiles) {
    $from = Join-Path (Join-Path $source "maitu_photo") $name
    if (-not (Test-Path -LiteralPath $from -PathType Leaf)) {
        throw "Required package file is missing: $from"
    }
    Copy-Item -LiteralPath $from -Destination (Join-Path $packageTarget $name) -Force
}

Write-Output "Synced $($rootFiles.Count + $packageFiles.Count) whitelisted files to $resolvedTarget"
