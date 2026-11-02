# YT Clipper setup for Windows.
# Installs what the app needs (Python, yt-dlp, Deno, FFmpeg) and adds a "YT Clipper" shortcut
# to this folder and the Start menu. Safe to run again: it only installs what's missing.

$ErrorActionPreference = 'Stop'
$dir = $PSScriptRoot

function Step($text) { Write-Host "`n> $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "  $text" -ForegroundColor Green }
function Fail($text) { Write-Host "`n$text" -ForegroundColor Red; exit 1 }
function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

# Returns the path of a Python 3.10+ interpreter, or $null.
function Find-Python {
    foreach ($candidate in @(@('py', '-3'), @('python'))) {
        $exe = $candidate[0]
        $pre = @($candidate | Select-Object -Skip 1)
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        # Skip the Microsoft Store placeholder that only opens the Store.
        if (-not $cmd -or $cmd.Source -like '*\WindowsApps\*') { continue }
        try {
            $out = & $exe @pre -c "import sys; print(sys.executable); print(sys.version_info >= (3, 10))" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2 -and $out[1] -eq 'True') { return $out[0] }
        } catch { }
    }
    return $null
}

function Find-FFmpeg {
    if (Have 'ffmpeg') { return $true }
    $packages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    return [bool](Get-ChildItem $packages -Filter 'ffmpeg.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
}

Write-Host 'Setting up YT Clipper' -ForegroundColor White

Step 'Python'
$python = Find-Python
if (-not $python) {
    if (-not (Have 'winget')) { Fail 'Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and run setup again.' }
    Write-Host '  Installing Python...'
    winget install --id Python.Python.3.12 -e --scope user --source winget
    Refresh-Path
    $python = Find-Python
    if (-not $python) { Fail 'Python was installed, but this window cannot see it yet. Close this window and run setup again.' }
}
Ok $python

Step 'yt-dlp and Deno'
& $python -m pip install --upgrade --disable-pip-version-check -r (Join-Path $dir 'requirements.txt')
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --upgrade --disable-pip-version-check --user -r (Join-Path $dir 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail 'Could not install yt-dlp. See the messages above.' }
}
Ok 'Installed'

Step 'FFmpeg'
if (Find-FFmpeg) {
    Ok 'Already installed'
} else {
    if (-not (Have 'winget')) { Fail 'FFmpeg is needed. Install it from https://www.gyan.dev/ffmpeg/builds/ and run setup again.' }
    winget install --id Gyan.FFmpeg -e --source winget
    Refresh-Path
    if (-not (Find-FFmpeg)) { Fail 'Could not install FFmpeg. See the messages above.' }
    Ok 'Installed'
}

Step 'Shortcut'
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
$shell = New-Object -ComObject WScript.Shell
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'YT Clipper.lnk'
foreach ($path in @((Join-Path $dir 'YT Clipper.lnk'), $startMenu)) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "`"$(Join-Path $dir 'app.py')`""
    $lnk.WorkingDirectory = $dir
    $lnk.IconLocation = "$(Join-Path $dir 'icon.ico'),0"
    $lnk.Description = 'YT Clipper'
    $lnk.Save()
}
Ok 'Added "YT Clipper" to this folder and the Start menu'

Write-Host "`nAll set. Opening YT Clipper..." -ForegroundColor Green
Start-Process (Join-Path $dir 'YT Clipper.lnk')
