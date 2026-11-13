# NEXUS STUDIO 一键启动：ComfyUI(8188) + 平台(18081)，两个一起起
# 部署前按本机实际修改下面三项 ComfyUI 路径；不使用自动拉起 ComfyUI 可留空。
$ErrorActionPreference = "Continue"
$studio   = Split-Path -Parent $MyInvocation.MyCommand.Path
$croot    = ""                                 # ComfyUI 安装根目录，例如 "D:\ComfyUI\ComfyUI-Installs\ComfyUI"
$cpx      = if ($croot) { Join-Path $croot "ComfyUI\.venv\Scripts\python.exe" } else { "" }
$cyaml    = ""                                 # 模型附加路径配置 yaml，没有可留空
$cinput   = ""                                 # ComfyUI 输入目录，没有可留空
$coutput  = ""                                 # ComfyUI 输出目录，没有可留空
$clog     = if ($croot) { Join-Path $croot "logs\comfyui-auto.log" } else { Join-Path $studio "data\comfy.log" }

function PortOpen([int]$port) {
  try {
    $c = New-Object Net.Sockets.TcpClient
    $ar = $c.BeginConnect("127.0.0.1", $port, $null, $null)
    $ok = $ar.AsyncWaitHandle.WaitOne(3000)
    $res = ($ok -and $c.Connected)
    $c.Close(); return $res
  } catch { return $false }
}
function Probe($url) {
  # 生成中的 ComfyUI 会把 HTTP 拖到超时，但端口是活的。判活必须先用 TCP，只看 HTTP 会误判离线并拉第二个实例（直接抢死 8188）。
  try { $u = [Uri]$url; if (PortOpen $u.Port) { return $true } } catch {}
  try { $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 4; return ($r.StatusCode -eq 200) } catch { return $false }
}

Write-Host "=========================================================="
Write-Host "  NEXUS STUDIO   AI 视频创作系统"
Write-Host "  平台 http://127.0.0.1:18081    ComfyUI http://127.0.0.1:8188"
Write-Host "=========================================================="
Write-Host ""

# ---------- 1. ComfyUI ----------
Write-Host "[1/3] 检查 ComfyUI (8188) ..."
$inst = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*ComfyUI\main.py*" })
if ((Probe "http://127.0.0.1:8188/system_stats") -or ($inst.Count -gt 0)) {
  if (-not (Probe "http://127.0.0.1:8188/system_stats")) {
    Write-Host "      进程已在（端口还没开），等它就绪，绝不重复拉第二个实例 ..."
    $swx = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Probe "http://127.0.0.1:8188/system_stats") -and $swx.Elapsed.TotalSeconds -lt 240) { Start-Sleep -Seconds 3 }
  }
  Write-Host "      已在运行，跳过启动"
} else {
  Write-Host "      未运行，正在启动（首次加载约 30-60 秒）..."
  if (-not (Test-Path -LiteralPath $cpx)) {
    Write-Host ("      [ERROR] 找不到 ComfyUI python：" + $cpx)
  } elseif (-not (Test-Path -LiteralPath $cyaml)) {
    Write-Host ("      [ERROR] 找不到模型配置：" + $cyaml)
  } else {
    $argstr = '-s ComfyUI\main.py --enable-assets --enable-manager'
    if ($cyaml) { $argstr += ' --extra-model-paths-config "' + $cyaml + '"' }
    if ($cinput) { $argstr += ' --input-directory "' + $cinput + '"' }
    if ($coutput) { $argstr += ' --output-directory "' + $coutput + '"' }
    Start-Process -FilePath $cpx -ArgumentList $argstr -WorkingDirectory $croot -WindowStyle Minimized -RedirectStandardOutput $clog -RedirectStandardError ($clog + ".err")
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while (-not (Probe "http://127.0.0.1:8188/system_stats") -and $sw.Elapsed.TotalSeconds -lt 180) { Start-Sleep -Seconds 3 }
    if (Probe "http://127.0.0.1:8188/system_stats") {
      Write-Host ("      ComfyUI 就绪（用时 " + [int]$sw.Elapsed.TotalSeconds + " 秒）")
    } else {
      Write-Host "      [WARN] 180 秒未就绪，平台继续启动；日志：" $clog
    }
  }
}

# ---------- 2. 平台 ----------
Write-Host ""
Write-Host "[2/3] 检查平台 (18081) ..."
if (Probe "http://127.0.0.1:18081/api/state") {
  Write-Host "      已在运行，跳过启动"
} else {
  Write-Host "      正在启动平台 ..."
  $py = (Get-Command python -ErrorAction SilentlyContinue).Source
  if (-not $py) { $py = "python" }
  Start-Process -FilePath $py -ArgumentList "app.py" -WorkingDirectory $studio -WindowStyle Minimized `
    -RedirectStandardOutput (Join-Path $studio "data\server.out.log") `
    -RedirectStandardError  (Join-Path $studio "data\server.err.log")
  $sw2 = [Diagnostics.Stopwatch]::StartNew()
  while (-not (Probe "http://127.0.0.1:18081/api/state") -and $sw2.Elapsed.TotalSeconds -lt 40) { Start-Sleep -Seconds 1 }
  if (Probe "http://127.0.0.1:18081/api/state") {
    Write-Host ("      平台就绪（用时 " + [int]$sw2.Elapsed.TotalSeconds + " 秒）")
  } else {
    Write-Host "      [ERROR] 平台没起来，看 data\server.err.log"
  }
}

# ---------- 3. 浏览器 ----------
Write-Host ""
Write-Host "[3/3] 打开浏览器 ..."
Start-Process "http://127.0.0.1:18081/"
Write-Host ""
Write-Host "----------------------------------------------------------"
if (Probe "http://127.0.0.1:8188/system_stats") { Write-Host "   ComfyUI : 在线 8188" } else { Write-Host "   ComfyUI : 离线（生成时自动用预演帧兜底）" }
if (Probe "http://127.0.0.1:18081/api/state")     { Write-Host "   平台    : 在线 18081" } else { Write-Host "   平台    : 离线" }
Write-Host "----------------------------------------------------------"
Write-Host "   关闭本窗口不会停止服务；停止请双击 一键停止.bat"
Write-Host ""