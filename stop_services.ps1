$ErrorActionPreference = "Continue"
$studio = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*app.py*' })
$comfy  = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*ComfyUI*main.py*' })
if ($studio) { $studio | ForEach-Object { Write-Host ("停平台 PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host "平台未在运行" }
if ($comfy)  { $comfy  | ForEach-Object { Write-Host ("停 ComfyUI PID " + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force } } else { Write-Host "ComfyUI 未在运行" }
Start-Sleep -Seconds 2
$left = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*app.py*' -or $_.CommandLine -like '*ComfyUI*main.py*' })
if ($left) { Write-Host "仍有残留进程" } else { Write-Host "已全部停止" }