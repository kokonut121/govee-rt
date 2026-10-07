# govee-rt installer for Windows. Usage (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/kokonut121/govee-rt/main/install.ps1 | iex"
$ErrorActionPreference = "Stop"
$source = if ($env:GOVEE_RT_SOURCE) { $env:GOVEE_RT_SOURCE } else { "git+https://github.com/kokonut121/govee-rt" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (a fast Python tool installer) ..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
Write-Host "Installing govee-rt ..."
uv tool install --force --python 3.12 $source
uv tool update-shell | Out-Null

Write-Host ""
Write-Host "Done. Windows can't record what it plays by default: install VB-Cable"
Write-Host "(https://vb-audio.com/Cable/), set 'CABLE Input' as the playback device, and turn on"
Write-Host "'Listen to this device' for 'CABLE Output' so you still hear it."
Write-Host "Then open a new terminal and run:  govee-rt"
