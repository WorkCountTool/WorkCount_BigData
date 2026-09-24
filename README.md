# 教学工作量核算台

从青果教务平台按学期读取当前登录教师的数据，核算工作量，并按学校原模板生成带公式的预算、决算和个人学时表。

## 启动

安装 Python 3.11、Chrome/Chromium，并运行：

```bash
python3 -m pip install -r requirements.txt
python3 app.py
```

浏览器访问 `http://127.0.0.1:8765`。

## 双击运行包

macOS Apple Silicon 的压缩包在 `release/WorkCount-macOS-arm64.zip`，解压整个文件夹后双击“启动工作量系统.command”。Windows x64 的完整包生成后解压并双击“启动工作量系统.bat”。在 macOS Intel 上应使用 `WorkCount-macOS-x86_64.zip`，不要混用 CPU 架构。

每台电脑须事先安装最新版 Google Chrome 或 Microsoft Edge，首次运行青果连接器可能需要访问网络获取匹配的浏览器驱动；还须能直接访问青果平台。Windows 会优先使用 Chrome，启动失败时自动回退到 Edge。启动入口仅监听本机的 `127.0.0.1:8765`，关闭服务窗口即停止服务。详见包内的“离线使用说明.txt”。

本机重建 Apple Silicon 包：`bash tools/build_mac.sh`。Windows 和 Intel Mac 二进制由 `.github/workflows/build-portable.yml` 手动构建；工作流产物不含私有 Excel 模板，不是完整可用包。Windows 下载后运行 `python3 tools/assemble_ci_package.py 不含模板的ZIP Windows-x64`；Intel Mac 下载到 `release/ci-artifacts/macos-intel/` 后运行 `bash tools/assemble_mac_intel.sh`。完整包会在本机加入三个原模板，切勿公开发布。

## 使用

1. 每位教师使用自己的青果平台账号和密码登录。
2. 登录后只读取可用学期列表；点击“查询并计算”才读取所选学期。密码保存在服务器内存中的登录会话，退出或会话过期时清除，不写入数据库。
3. 每位教师只能使用自己的平台账号查询其本人数据；退出后可切换账号。
4. 选择学期后可查看核算明细，并生成原格式 Excel。
5. 个人学时表按周、日期和实际上课节次填写，例如 `1-2`、`3-4`、`5-7`，同时保留周合计、月合计和总表公式。
6. 系统仅采用平台同步数据，不接受本地 Excel 导入作为数据来源。

实时查询的数据只保存在当前服务器进程内存中；`data/workcount.db` 用于旧版演示和本地开发，不保存实时同步结果。服务器重启后须重新登录。导出的文件直接返回浏览器。

## 部署到公网

GitHub 仓库只存代码。三个学校 Excel 原模板以及教师数据、账号密码都不能提交到公开仓库；将模板文件放在服务器的 `templates/` 目录，文件名须与 `app.py` 中的 `REQUIRED_TEMPLATES` 完全一致。`/ready` 会检查模板是否齐全。

服务器需安装 Docker Compose，具备访问 `qgjw.suet.edu.cn` 的网络，并为域名配置指向该服务器的 DNS A/AAAA 记录，开放 80/443 端口。运行：

```bash
WORKCOUNT_DOMAIN=workcount.example.org docker compose -f compose.yaml -f compose.production.yaml up -d --build
```

将命令中的域名替换为实际域名。Caddy 自动申请 HTTPS 证书，服务端 Cookie 标记为 `Secure`。不要直接把容器的 8765 HTTP 端口暴露到公网。可通过 `docker compose ps` 和 `https://你的域名/ready` 检查状态。

注意：本应用使用单进程内存会话和 SQLite，不应水平扩容或把登录任务分发到多个实例；公网长期运行还需要网络层访问控制、监控和适当的资源配额。部署前应核实青果平台允许的自动访问方式，并经学校确认教师数据的对外部署要求。

## 验证

```bash
PYTHONPYCACHEPREFIX=/tmp/workcount-pycache python3 -m py_compile app.py tools/platform_sync.py
python3 -m unittest discover -s tests -v
curl http://127.0.0.1:8765/health
```

环境变量：`WORKCOUNT_HOST`、`WORKCOUNT_PORT`、`WORKCOUNT_DB`、`WORKCOUNT_CONNECTOR_PYTHON`、`WORKCOUNT_CHROME_BINARY`、`WORKCOUNT_SECURE_COOKIES`、`WORKCOUNT_MAX_LOGINS`。
