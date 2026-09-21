@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "WorkCountServer\WorkCountServer.exe" goto missing
if not exist "PlatformConnector\PlatformConnector.exe" goto missing

set "WORKCOUNT_HOST=127.0.0.1"
set "WORKCOUNT_PORT=8765"
set "WORKCOUNT_DB=%CD%\data\workcount.db"
if not exist data mkdir data

start "工作量系统服务（关闭此窗口即停止）" "%CD%\WorkCountServer\WorkCountServer.exe"
powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 30;$i++){try{$r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/ready' -TimeoutSec 2; if($r.StatusCode -eq 200){$ok=$true;break}}catch{}; Start-Sleep -Seconds 1}; if($ok){Start-Process 'http://127.0.0.1:8765/'}else{Write-Host '启动失败：请检查程序文件、三个模板和 8765 端口'; Read-Host '按回车键关闭'}"
exit /b

:missing
echo 程序文件不完整，请重新解压整个文件夹。
pause
exit /b 1
