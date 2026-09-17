param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$AppName = "FOD" + [char]0x6807 + [char]0x6CE8 + [char]0x5DE5 + [char]0x5177
$GuideName = ([string][char]0x4F7F) + [char]0x7528 + [char]0x8BF4 + [char]0x660E + ".txt"
$BuildRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "build"))
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "dist"))
$ReleaseRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "release"))
$ReleaseApp = [System.IO.Path]::GetFullPath((Join-Path $ReleaseRoot $AppName))
$ReleaseZip = [System.IO.Path]::GetFullPath((Join-Path $ReleaseRoot ($AppName + "_Windows.zip")))
$SpecPath = Join-Path $ProjectRoot "fod_annotation_tool.spec"
$GuidePath = Join-Path $ProjectRoot "packaging\user_guide_zh_CN.txt"

function Assert-BuildTarget([string]$TargetPath) {
    $ResolvedTarget = [System.IO.Path]::GetFullPath($TargetPath)
    $RootPrefix = $ProjectRoot.TrimEnd('\') + '\'
    if (-not $ResolvedTarget.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $ResolvedTarget"
    }
}

foreach ($Target in @($BuildRoot, $DistRoot, $ReleaseApp, $ReleaseZip)) {
    Assert-BuildTarget $Target
}

foreach ($Directory in @($BuildRoot, $DistRoot, $ReleaseApp)) {
    if (Test-Path -LiteralPath $Directory) {
        Remove-Item -LiteralPath $Directory -Recurse -Force
    }
}
if (Test-Path -LiteralPath $ReleaseZip) {
    Remove-Item -LiteralPath $ReleaseZip -Force
}

New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null

if ($PythonPath) {
    $ResolvedPython = [System.IO.Path]::GetFullPath($PythonPath)
    & $ResolvedPython -m PyInstaller --noconfirm --clean `
        --workpath $BuildRoot --distpath $DistRoot $SpecPath
} else {
    $CondaCommand = Get-Command conda -ErrorAction Stop
    & $CondaCommand.Source run -n fod-annotator python -m PyInstaller `
        --noconfirm --clean --workpath $BuildRoot --distpath $DistRoot $SpecPath
}
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$BuiltApp = Join-Path $DistRoot $AppName
$BuiltExe = Join-Path $BuiltApp ($AppName + ".exe")
if (-not (Test-Path -LiteralPath $BuiltExe)) {
    throw "Build output does not contain the expected executable: $BuiltExe"
}

Copy-Item -LiteralPath $BuiltApp -Destination $ReleaseApp -Recurse
Copy-Item -LiteralPath $GuidePath -Destination (Join-Path $ReleaseApp $GuideName)

$ForbiddenFiles = Get-ChildItem -LiteralPath $ReleaseApp -Recurse -File | Where-Object {
    $_.Extension -in @(".py", ".pyc", ".pyo", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
}
if ($ForbiddenFiles) {
    $Names = ($ForbiddenFiles | ForEach-Object FullName) -join "`n"
    throw "Release directory contains forbidden files:`n$Names"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$ZipAttempts = 0
while ($true) {
    try {
        if (Test-Path -LiteralPath $ReleaseZip) {
            Remove-Item -LiteralPath $ReleaseZip -Force
        }
        [System.IO.Compression.ZipFile]::CreateFromDirectory(
            $ReleaseApp,
            $ReleaseZip,
            [System.IO.Compression.CompressionLevel]::Optimal,
            $true
        )
        break
    } catch {
        $ZipAttempts++
        if ($ZipAttempts -ge 5) {
            throw
        }
        Start-Sleep -Seconds 2
    }
}
Write-Host "Windows release created: $ReleaseZip"
