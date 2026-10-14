# ============================================================
#  TClaude 安装脚本 (Windows)
#  来源: iwiki.woa.com/p/4022204191
#  用法: powershell -ExecutionPolicy Bypass -File .\install-tclaude.ps1
#        powershell -ExecutionPolicy Bypass -File .\install-tclaude.ps1 -Proxy http://127.0.0.1:12639
#
#  特点:
#    - 自动探测 iOA(SmartVPN) 的本地代理端口。iOA 是"本地代理"模式，
#      不走虚拟网卡（Get-NetAdapter 看不到 NGNClient Adapter 是正常的），
#      所以 npm/curl/Invoke-WebRequest 必须显式走代理才能访问内网制品库。
#    - 直接下载官方二进制，步骤透明可验证
#    - 幂等：重复执行即升级
# ============================================================
param(
    [string] $Proxy   = "",          # 手动指定代理，如 http://127.0.0.1:12639
    [string] $Version = "latest"     # latest 或具体版本号，如 0.1.8
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'

$Base       = 'https://mirrors.tencent.com/repository/generic/tclaude/tclaude'
$Product    = 'tclaude'
$InstallDir = Join-Path $env:LOCALAPPDATA 'tcli\bin'
$Dest       = Join-Path $InstallDir "$Product.exe"

function Write-Step($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)    { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn2($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Write-Err($m)   { Write-Host "    $m" -ForegroundColor Red }

# 统一的请求封装：自动带上代理
function Get-Url {
    param([string]$Uri, [string]$OutFile, [int]$TimeoutSec = 60, [string]$ProxyUrl)
    $params = @{ UseBasicParsing = $true; Uri = $Uri; TimeoutSec = $TimeoutSec }
    if ($ProxyUrl) { $params.Proxy = $ProxyUrl }
    if ($OutFile) { $params.OutFile = $OutFile; Invoke-WebRequest @params | Out-Null; return $null }
    return Invoke-WebRequest @params
}

# ---------- 自动探测 iOA 本地代理 ----------
function Find-IoaProxy {
    $pids = (Get-Process -Name 'SmartVPN','iOA' -ErrorAction SilentlyContinue).Id
    if (-not $pids) { return $null }
    $ports = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $pids -contains $_.OwningProcess -and $_.LocalAddress -eq '127.0.0.1' -and $_.LocalPort -ne 53 } |
        Select-Object -ExpandProperty LocalPort -Unique | Sort-Object
    foreach ($p in $ports) {
        try {
            $null = Get-Url -Uri "$Base/LAST_VERSION" -TimeoutSec 6 -ProxyUrl "http://127.0.0.1:$p"
            return "http://127.0.0.1:$p"
        } catch { }
    }
    return $null
}

# ---------- 1. 解析网络通道 ----------
Write-Step "[1/5] 解析网络通道"
$useProxy = $null
if ($Proxy) {
    $useProxy = $Proxy
    Write-Ok "使用指定代理: $useProxy"
} else {
    $useProxy = Find-IoaProxy
    if ($useProxy) { Write-Ok "自动探测到 iOA 代理: $useProxy" }
    else { Write-Warn2 "未探测到 iOA 代理，尝试直连（若失败请先登录 iOA 零信任）" }
}

# ---------- 2. 取版本 ----------
Write-Step "[2/5] 查询版本"
$ver = $Version
if ($ver -eq 'latest') {
    $tmpVer = [IO.Path]::GetTempFileName()
    try {
        Get-Url -Uri "$Base/LAST_VERSION" -OutFile $tmpVer -TimeoutSec 30 -ProxyUrl $useProxy
        $ver = (Get-Content -LiteralPath $tmpVer -Raw).Trim()
    } catch {
        Write-Err "取版本失败: $($_.Exception.Message)"
        Write-Host "    请确认 iOA 已登录；也可手动指定：-Proxy http://127.0.0.1:12639" -ForegroundColor Yellow
        exit 1
    } finally {
        Remove-Item $tmpVer -Force -ErrorAction SilentlyContinue
    }
}
Write-Ok "版本: $ver"

# ---------- 3. 下载二进制 ----------
Write-Step "[3/5] 下载 $Product-$ver-windows-x64.exe"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$tmpFile = Join-Path $InstallDir ".$Product.$([IO.Path]::GetRandomFileName()).tmp"
try {
    Get-Url -Uri "$Base/$ver/$Product-$ver-windows-x64.exe" -OutFile $tmpFile -TimeoutSec 600 -ProxyUrl $useProxy
    $len = (Get-Item $tmpFile).Length
    if ($len -lt 500KB) { throw "下载体积异常（$len bytes），可能被拦截" }
    Write-Ok "已下载 $([Math]::Round($len/1MB,2)) MB"
    Move-Item -Force -LiteralPath $tmpFile -Destination $Dest
} catch {
    Write-Err "下载失败: $($_.Exception.Message)"
    Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------- 4. 校验 + 配置 PATH ----------
Write-Step "[4/5] 校验与配置 PATH"
# 注意：不要把原生命令直接接进 Select-Object -First 1，
# 那会提前终止管道并把 $LASTEXITCODE 搞成非零，导致误判。
$verOut  = & $Dest --version 2>&1
$verCode = $LASTEXITCODE
if ($verCode -ne 0) { Write-Err "二进制校验失败 (exit=$verCode)"; exit 1 }
$verLine = @($verOut) | Select-Object -First 1
Write-Ok "执行成功: $verLine"

$userPath = [Environment]::GetEnvironmentVariable('Path','User')
$entries = @($userPath -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($entries -notcontains $InstallDir) {
    [Environment]::SetEnvironmentVariable('Path', (($entries + $InstallDir) -join ';'), 'User')
    Write-Ok "已加入用户级 PATH: $InstallDir"
} else {
    Write-Ok "PATH 已包含: $InstallDir"
}
$env:Path = "$InstallDir;$env:Path"

# ---------- 5. 完成 ----------
Write-Step "[5/5] 完成"
Write-Host ""
Write-Host "  安装位置: $Dest" -ForegroundColor Gray
Write-Host "  下一步（需要你本人操作）：" -ForegroundColor Green
Write-Host "    tclaude login      # 走 iOA / 浏览器完成员工身份登录" -ForegroundColor Gray
Write-Host "    tclaude            # 进入交互模式" -ForegroundColor Gray
Write-Host ""
Write-Host "  提示：守护进程若报 'no authentication session'，就是还没登录。" -ForegroundColor Yellow
