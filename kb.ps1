# kb.ps1 —— AiDoc 本地知识库一键维护脚本
# 用法：在 E:\AiDoc 目录下右键“使用 PowerShell 运行”，或执行  powershell -File .\kb.ps1
# 作用：重跑 generate_index.py 刷新 README.md 分类导航

#pragma region Engine ZXB
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "==== 刷新知识库导航 (generate_index.py) ====" -ForegroundColor Cyan
Push-Location $Root
try {
    python "generate_index.py"
    Write-Host "完成。" -ForegroundColor Green
} catch {
    Write-Host "  生成导航失败，请确认已安装 Python。" -ForegroundColor Red
}
Pop-Location
#pragma endregion
