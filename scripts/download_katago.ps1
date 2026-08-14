<#
go-coach: KataGo engine + model downloader

Downloads KataGo analysis engine and the default model into the katago/
directory. The engine is NOT stored in git (3.8 GB of binaries); this script
is the convenient way to fetch and maintain it separately.

Usage:
  powershell -ExecutionPolicy Bypass -File scripts\download_katago.ps1            # CPU (Eigen) build, default
  powershell -ExecutionPolicy Bypass -File scripts\download_katago.ps1 -Build cuda # CUDA build (needs NVIDIA GPU)

Options:
  -Build   eigen | cuda     engine build to fetch (default: eigen)
  -Version KataGo release tag version (default: 1.17.1)
#>
param(
    [ValidateSet("eigen", "cuda")]
    [string]$Build = "eigen",

    [string]$Version = "1.17.1"
)

$ErrorActionPreference = "Stop"

# --- paths -----------------------------------------------------------------
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$KatagoDir   = Join-Path $ProjectRoot "katago"

# --- asset names & urls -----------------------------------------------------
# Windows asset names on https://github.com/lightvector/KataGo/releases
$BuildAsset = if ($Build -eq "cuda") { "cuda12.1-cudnn8.9.7" } else { "eigen" }
$EngineUrl  = "https://github.com/lightvector/KataGo/releases/download/v$Version/katago-v$Version-$BuildAsset-windows-x64.zip"
# Default model (b28, Elo ~14110, good balance of speed/strength for teaching).
# Saved under the short name config.py expects.
$ModelName  = "kata1-b28c512nbt.bin.gz"
$ModelUrl   = "https://media.katagotraining.org/uploaded/networks/models/kata1/kata1-b28c512nbt-s13255194368-d5935380940.bin.gz"
$ModelBytes = 271440852   # expected model size in bytes

Write-Host ""
Write-Host "== go-coach: KataGo downloader =="
Write-Host "Build : $Build  (asset: $BuildAsset)"
Write-Host "Target: $KatagoDir"
Write-Host ""

New-Item -ItemType Directory -Force -Path $KatagoDir | Out-Null

# --- 1. engine ---------------------------------------------------------------
$EngineExe = Join-Path $KatagoDir "katago.exe"
if (Test-Path $EngineExe) {
    Write-Host "[skip] katago.exe already present."
} else {
    Write-Host "[1/2] downloading engine: $EngineUrl"
    $ZipPath = Join-Path $env:TEMP "katago-$Build-$Version.zip"
    Invoke-WebRequest -Uri $EngineUrl -OutFile $ZipPath -UseBasicParsing
    Write-Host "      extracting to $KatagoDir ..."
    Expand-Archive -Path $ZipPath -DestinationPath $KatagoDir -Force
    Remove-Item $ZipPath -Force
    if (-not (Test-Path $EngineExe)) {
        throw "Engine download failed: $EngineExe not found after extraction."
    }
    Write-Host "      ok."
}

# --- 2. model ----------------------------------------------------------------
$ModelPath = Join-Path $KatagoDir $ModelName
if ((Test-Path $ModelPath) -and ((Get-Item $ModelPath).Length -eq $ModelBytes)) {
    Write-Host "[skip] model already present (size matches)."
} else {
    Write-Host "[2/2] downloading model (271 MB):"
    Write-Host "      $ModelUrl"
    Invoke-WebRequest -Uri $ModelUrl -OutFile $ModelPath -UseBasicParsing
    $actual = (Get-Item $ModelPath).Length
    if ($actual -ne $ModelBytes) {
        throw "Model size mismatch: got $actual bytes, expected $ModelBytes. Re-run to retry the download."
    }
    Write-Host "      ok."
}

# --- 3. verify config template -------------------------------------------------
$OverrideCfg = Join-Path $KatagoDir "analysis_override.cfg"
if (-not (Test-Path $OverrideCfg)) {
    Write-Warning "analysis_override.cfg not found in katago/. It ships with the repo - restore it, or copy katago/analysis_override.cfg from git."
}

Write-Host ""
Write-Host "Done. katago/ now contains:"
Get-ChildItem $KatagoDir -File | Select-Object Name, @{N="Size(MB)";E={[math]::Round($_.Length/1MB,1)}}
Write-Host ""

if ($Build -eq "cuda") {
    Write-Host "NOTE: the CUDA build also needs the NVIDIA CUDA toolkit + cuDNN libraries."
    Write-Host "      See https://github.com/lightvector/KataGo#gpu-compatibility-and-installation"
}
Write-Host "Next: python app.py  (engine is auto-detected; GO_COACH_FORCE_MOCK=1 skips it)"
Write-Host ""
