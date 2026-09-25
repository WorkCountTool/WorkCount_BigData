$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$base = (Get-Location).Path
$buildRoot = Join-Path $base 'build\portable-windows'

python -m PyInstaller --noconfirm --clean --onedir --name WorkCountServer `
  --distpath "$buildRoot\dist" --workpath "$buildRoot\work" --specpath "$buildRoot\spec" app.py
if ($LASTEXITCODE -ne 0) { throw 'WorkCountServer build failed' }
python -m PyInstaller --noconfirm --clean --onedir --name PlatformConnector --collect-all selenium `
  --distpath "$buildRoot\dist" --workpath "$buildRoot\work" --specpath "$buildRoot\spec" tools/platform_sync.py
if ($LASTEXITCODE -ne 0) { throw 'PlatformConnector build failed' }

$output = Join-Path $base 'release\WorkCount-Windows-x64'
New-Item -ItemType Directory -Force -Path $output | Out-Null
Copy-Item -Recurse -Force "$buildRoot\dist\WorkCountServer" $output
Copy-Item -Recurse -Force "$buildRoot\dist\PlatformConnector" $output
Copy-Item -Recurse -Force "$base\static" $output
Copy-Item -Force "$base\启动工作量系统.bat", "$base\离线使用说明.txt" $output
New-Item -ItemType Directory -Force -Path "$output\templates" | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $output 'templates\张子豪-表1：人工智能学院（部）2026-2027学年第一学期工作量预算汇总表.xlsx')
Compress-Archive -Path $output -DestinationPath "$base\release\WorkCount-Windows-x64-no-templates.zip" -Force
Write-Host "已生成 $output；本构建不上传私人 Excel 模板。"
