param(
    [string]$BuildRoot = "C:\GoogleExportBuild",
    [switch]$IncludeDebug,
    [switch]$CleanVenv,
    [switch]$SkipZip = $true
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

$workspace = Join-Path $BuildRoot "google_auto_export_build"
$stageRoot = Join-Path $workspace "stage_src"
$pyiDistRoot = Join-Path $workspace ".tmp_pyinstaller_dist"
$pyiWorkRoot = Join-Path $workspace ".tmp_pyinstaller_work"
$venvDir = Join-Path $workspace ".venv_pack"
$pipCache = Join-Path $workspace ".pip_cache"
$tmpDir = Join-Path $workspace ".tmp"

$repoDistFinal = Join-Path $repoRoot "dist\MyApp"
$repoDistDebug = Join-Path $repoRoot "dist\MyAppDebug"
$repoReleaseDir = Join-Path $repoRoot "release"
$repoReleaseZip = Join-Path $repoReleaseDir "MyApp_win.zip"
$repoReleaseZipDebug = Join-Path $repoReleaseDir "MyAppDebug_win.zip"
$repoBuildLog = Join-Path $repoReleaseDir "build_release.log"
$buildRequirements = (Resolve-Path (Join-Path $PSScriptRoot "requirements.google.build.txt")).Path

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = "[build-google] $Message"
    Write-Host $line
    Add-Content -LiteralPath $repoBuildLog -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $line"
}

function Ensure-Dir {
    param([Parameter(Mandatory = $true)][string]$Path)
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Copy-DirectoryMirror {
    param(
        [Parameter(Mandatory = $true)][string]$From,
        [Parameter(Mandatory = $true)][string]$To,
        [string[]]$ExcludeDirs = @(),
        [string[]]$ExcludeFiles = @()
    )

    Ensure-Dir -Path $To
    $args = @(
        $From,
        $To,
        "/MIR",
        "/R:2",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP"
    )
    if ($ExcludeDirs.Count -gt 0) {
        $args += "/XD"
        $args += $ExcludeDirs
    }
    if ($ExcludeFiles.Count -gt 0) {
        $args += "/XF"
        $args += $ExcludeFiles
    }
    $null = robocopy @args
    if ($LASTEXITCODE -gt 7) {
        throw "robocopy failed: $From -> $To (exit=$LASTEXITCODE)"
    }
}

function Ensure-CommandPython {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return "python"
    }
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCommand) {
        return "py -3"
    }
    throw "Python launcher not found. Install Python 3.12+ and retry."
}

function Ensure-BuildVenv {
    param([Parameter(Mandatory = $true)][string]$VenvPath)

    if ($CleanVenv -and (Test-Path -LiteralPath $VenvPath)) {
        Write-Step "Cleaning build venv"
        Remove-Item -LiteralPath $VenvPath -Recurse -Force -ErrorAction SilentlyContinue
    }

    if (!(Test-Path -LiteralPath $VenvPath)) {
        Write-Step "Creating build venv: $VenvPath"
        $pyCmd = Ensure-CommandPython
        $venvCreated = $false
        if ($pyCmd -eq "python") {
            & python -m venv $VenvPath
            if ($LASTEXITCODE -eq 0) { $venvCreated = $true }
        }
        else {
            & py -3 -m venv $VenvPath
            if ($LASTEXITCODE -eq 0) { $venvCreated = $true }
        }

        if (-not $venvCreated) {
            Write-Step "python -m venv failed, retrying with virtualenv"
            if ($pyCmd -eq "python") {
                & python -m virtualenv $VenvPath
            }
            else {
                & py -3 -m virtualenv $VenvPath
            }
            if ($LASTEXITCODE -eq 0) { $venvCreated = $true }
        }

        if (-not $venvCreated) {
            throw "Failed to create build venv."
        }
    }
}

function Install-BuildDependencies {
    param(
        [Parameter(Mandatory = $true)][string]$VenvPython,
        [Parameter(Mandatory = $true)][string]$RequirementsPath
    )

    Write-Step "Upgrading pip"
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }

    Write-Step "Installing runtime/build deps"
    & $VenvPython -m pip install -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install dependencies."
    }
}

function Invoke-LauncherBuild {
    param(
        [Parameter(Mandatory = $true)][string]$PyInstallerPath,
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Console
    )

    Write-Step "Building launcher: $Name (console=$Console)"

    Remove-Item -LiteralPath (Join-Path $pyiDistRoot $Name) -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $pyiWorkRoot $Name) -Recurse -Force -ErrorAction SilentlyContinue

    $windowFlag = if ($Console) { "--console" } else { "--noconsole" }
    $appData = "{0};app" -f (Join-Path $stageRoot "app")
    $streamlitData = "{0};.streamlit" -f (Join-Path $stageRoot ".streamlit")
    $exporterData = "{0};google_ads_exporter" -f (Join-Path $stageRoot "google_ads_exporter")
    $configData = "{0};config" -f (Join-Path $stageRoot "config")
    $mainData = "{0};." -f (Join-Path $stageRoot "main.py")

    $args = @(
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name", $Name,
        "--distpath", $pyiDistRoot,
        "--workpath", $pyiWorkRoot,
        "--specpath", $workspace,
        "--contents-directory", "_internal",
        "--hidden-import", "streamlit.web.cli",
        "--hidden-import", "streamlit.web.bootstrap",
        "--hidden-import", "streamlit.runtime.scriptrunner.script_runner",
        "--hidden-import", "streamlit.runtime.scriptrunner.magic",
        "--hidden-import", "streamlit.runtime.scriptrunner.magic_funcs",
        "--collect-submodules", "streamlit.runtime.scriptrunner",
        "--hidden-import", "playwright.sync_api",
        "--hidden-import", "pandas",
        "--hidden-import", "openpyxl",
        "--collect-data", "streamlit",
        "--collect-data", "playwright",
        "--copy-metadata", "streamlit",
        "--copy-metadata", "playwright",
        "--copy-metadata", "pandas",
        "--copy-metadata", "openpyxl",
        "--exclude-module", "tensorflow",
        "--exclude-module", "torch",
        "--exclude-module", "langchain",
        "--add-data", $appData,
        "--add-data", $streamlitData,
        "--add-data", $exporterData,
        "--add-data", $configData,
        "--add-data", $mainData,
        $windowFlag,
        $LauncherPath
    )

    & $PyInstallerPath @args
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed for $Name"
    }
}

function Remove-HeavyPackages {
    param([Parameter(Mandatory = $true)][string]$SitePackagesPath)

    $patterns = @(
        "tensorflow*",
        "torch*",
        "langchain*",
        "langsmith*",
        "triton*"
    )

    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $SitePackagesPath -Filter $pattern -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
            }
    }
}

function Stage-FinalPayload {
    param(
        [Parameter(Mandatory = $true)][string]$FinalRoot,
        [Parameter(Mandatory = $true)][string]$VenvPython,
        [Parameter(Mandatory = $true)][string]$VenvSitePackages
    )

    $basePythonHome = (& $VenvPython -c "import os,sys; print(os.path.abspath(sys.base_prefix))").Trim()
    if (!(Test-Path -LiteralPath $basePythonHome)) {
        throw "Failed to resolve base python home: $basePythonHome"
    }

    $runtimeRoot = Join-Path $FinalRoot "_internal\python_runtime"
    $runtimeSitePackages = Join-Path $runtimeRoot "Lib\site-packages"

    Write-Step "Staging base python runtime"
    Copy-DirectoryMirror -From $basePythonHome -To $runtimeRoot

    Write-Step "Overlaying site-packages"
    Copy-DirectoryMirror -From $VenvSitePackages -To $runtimeSitePackages

    Write-Step "Pruning heavy modules"
    Remove-HeavyPackages -SitePackagesPath $runtimeSitePackages

    Ensure-Dir -Path (Join-Path $FinalRoot "logs")
    Ensure-Dir -Path (Join-Path $FinalRoot "output")
    Ensure-Dir -Path (Join-Path $FinalRoot "downloads")
    Ensure-Dir -Path (Join-Path $FinalRoot "config")

    Copy-DirectoryMirror -From (Join-Path $stageRoot "google_ads_exporter") -To (Join-Path $FinalRoot "google_ads_exporter") -ExcludeDirs @("__pycache__") -ExcludeFiles @("*.pyc")
    Copy-DirectoryMirror -From (Join-Path $stageRoot "app") -To (Join-Path $FinalRoot "app") -ExcludeDirs @("__pycache__") -ExcludeFiles @("*.pyc")
    Copy-DirectoryMirror -From (Join-Path $stageRoot ".streamlit") -To (Join-Path $FinalRoot ".streamlit")
    Copy-DirectoryMirror -From (Join-Path $stageRoot "config") -To (Join-Path $FinalRoot "config")

    Copy-Item -LiteralPath (Join-Path $stageRoot "main.py") -Destination (Join-Path $FinalRoot "main.py") -Force
    Copy-Item -LiteralPath (Join-Path $stageRoot "README.txt") -Destination (Join-Path $FinalRoot "README.txt") -Force
}

function Wait-AnyPort {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$Port = 8501,
        [int]$MaxOffset = 10,
        [int]$TimeoutSec = 90
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        for ($offset = 0; $offset -le $MaxOffset; $offset++) {
            $candidatePort = $Port + $offset
            try {
                $client = New-Object System.Net.Sockets.TcpClient
                $iar = $client.BeginConnect($HostName, $candidatePort, $null, $null)
                if ($iar.AsyncWaitHandle.WaitOne(350)) {
                    $client.EndConnect($iar)
                    $client.Close()
                    return $candidatePort
                }
                $client.Close()
            }
            catch {}
        }
        Start-Sleep -Milliseconds 250
    }
    return -1
}

function Stop-PortOwners {
    param(
        [int]$StartPort = 8501,
        [int]$MaxOffset = 10
    )
    $ports = @()
    for ($offset = 0; $offset -le $MaxOffset; $offset++) {
        $ports += ($StartPort + $offset)
    }
    $pattern = ":(?:{0})\s+.*LISTENING\s+(\d+)$" -f ($ports -join "|")
    $pids = New-Object System.Collections.Generic.HashSet[int]
    netstat -ano | ForEach-Object {
        $line = $_.ToString()
        if ($line -match $pattern) {
            [void]$pids.Add([int]$Matches[1])
        }
    }
    foreach ($pid in $pids) {
        if ($pid -le 0) { continue }
        & taskkill /PID $pid /T /F | Out-Null
    }
}

function Test-Exe {
    param([Parameter(Mandatory = $true)][string]$ExePath)

    Get-Process | Where-Object { $_.ProcessName -like "MyApp*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
    Stop-PortOwners -StartPort 8501 -MaxOffset 10

    $proc = Start-Process -FilePath $ExePath -PassThru
    $openedPort = Wait-AnyPort -HostName "127.0.0.1" -Port 8501 -MaxOffset 10 -TimeoutSec 110
    if ($openedPort -lt 0) {
        if ($proc -and -not $proc.HasExited) {
            & taskkill /PID $proc.Id /T /F | Out-Null
        }
        throw "Smoke test failed (no streamlit port open): $ExePath"
    }

    Start-Sleep -Seconds 5
    if ($proc.HasExited) {
        throw "Smoke test failed (process exited early): $ExePath"
    }

    & taskkill /PID $proc.Id /T /F | Out-Null
}

function Assert-RequiredSource {
    $required = @(
        "launcher.py",
        "app\main.py",
        ".streamlit\config.toml",
        "google_ads_exporter",
        "main.py",
        "README.txt",
        "config\runtime_settings.json"
    )
    foreach ($item in $required) {
        $path = Join-Path $repoRoot $item
        if (!(Test-Path -LiteralPath $path)) {
            throw "Required source missing: $path"
        }
    }
}

function Prepare-StageSource {
    Write-Step "Preparing stage source"
    Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
    Ensure-Dir -Path $stageRoot
    Ensure-Dir -Path (Join-Path $stageRoot "app")
    Ensure-Dir -Path (Join-Path $stageRoot ".streamlit")
    Ensure-Dir -Path (Join-Path $stageRoot "config")

    Copy-Item -LiteralPath (Join-Path $repoRoot "launcher.py") -Destination (Join-Path $stageRoot "launcher.py") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "app\main.py") -Destination (Join-Path $stageRoot "app\main.py") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot ".streamlit\config.toml") -Destination (Join-Path $stageRoot ".streamlit\config.toml") -Force
    Copy-DirectoryMirror -From (Join-Path $repoRoot "google_ads_exporter") -To (Join-Path $stageRoot "google_ads_exporter") -ExcludeDirs @("__pycache__") -ExcludeFiles @("*.pyc")
    Copy-Item -LiteralPath (Join-Path $repoRoot "main.py") -Destination (Join-Path $stageRoot "main.py") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "README.txt") -Destination (Join-Path $stageRoot "README.txt") -Force
    Copy-Item -LiteralPath (Join-Path $repoRoot "config\runtime_settings.json") -Destination (Join-Path $stageRoot "config\runtime_settings.json") -Force
}

Ensure-Dir -Path $repoReleaseDir
Set-Content -LiteralPath $repoBuildLog -Value ""
Write-Step "Build start"
Write-Step "Repo root: $repoRoot"
Write-Step "Build root: $BuildRoot"

$repoRootAbs = [System.IO.Path]::GetFullPath($repoRoot)
$buildRootAbs = [System.IO.Path]::GetFullPath($BuildRoot)
if ($buildRootAbs.StartsWith($repoRootAbs, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "BuildRoot must be outside repo path. Current: $buildRootAbs"
}

Assert-RequiredSource

Ensure-Dir -Path $BuildRoot
Ensure-Dir -Path $workspace
Ensure-Dir -Path $pipCache
Ensure-Dir -Path $tmpDir
Ensure-Dir -Path $pyiDistRoot
Ensure-Dir -Path $pyiWorkRoot

$env:TEMP = $tmpDir
$env:TMP = $tmpDir
$env:TMPDIR = $tmpDir
$env:PIP_CACHE_DIR = $pipCache

Prepare-StageSource

Ensure-BuildVenv -VenvPath $venvDir
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPyInstaller = Join-Path $venvDir "Scripts\pyinstaller.exe"
$venvSitePackages = Join-Path $venvDir "Lib\site-packages"
if (!(Test-Path -LiteralPath $venvPython)) { throw "Missing venv python: $venvPython" }

Install-BuildDependencies -VenvPython $venvPython -RequirementsPath $buildRequirements
if (!(Test-Path -LiteralPath $venvPyInstaller)) { throw "Missing pyinstaller: $venvPyInstaller" }

Invoke-LauncherBuild -PyInstallerPath $venvPyInstaller -LauncherPath (Join-Path $stageRoot "launcher.py") -Name "MyApp" -Console:$false
Stage-FinalPayload -FinalRoot (Join-Path $pyiDistRoot "MyApp") -VenvPython $venvPython -VenvSitePackages $venvSitePackages
Test-Exe -ExePath (Join-Path $pyiDistRoot "MyApp\MyApp.exe")

if ($IncludeDebug) {
    Invoke-LauncherBuild -PyInstallerPath $venvPyInstaller -LauncherPath (Join-Path $stageRoot "launcher.py") -Name "MyAppDebug" -Console:$true
    Stage-FinalPayload -FinalRoot (Join-Path $pyiDistRoot "MyAppDebug") -VenvPython $venvPython -VenvSitePackages $venvSitePackages
    Test-Exe -ExePath (Join-Path $pyiDistRoot "MyAppDebug\MyAppDebug.exe")
}

Remove-Item -LiteralPath $repoDistFinal -Recurse -Force -ErrorAction SilentlyContinue
Copy-DirectoryMirror -From (Join-Path $pyiDistRoot "MyApp") -To $repoDistFinal

if (-not $SkipZip) {
    Remove-Item -LiteralPath $repoReleaseZip -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path $repoDistFinal -DestinationPath $repoReleaseZip -Force
}

if ($IncludeDebug) {
    Remove-Item -LiteralPath $repoDistDebug -Recurse -Force -ErrorAction SilentlyContinue
    Copy-DirectoryMirror -From (Join-Path $pyiDistRoot "MyAppDebug") -To $repoDistDebug
    if (-not $SkipZip) {
        Remove-Item -LiteralPath $repoReleaseZipDebug -Force -ErrorAction SilentlyContinue
        Compress-Archive -Path $repoDistDebug -DestinationPath $repoReleaseZipDebug -Force
    }
}

Write-Step "Build done"
Write-Step "ReleaseDir: $repoDistFinal"
if (-not $SkipZip) {
    Write-Step "ReleaseZip: $repoReleaseZip"
}
