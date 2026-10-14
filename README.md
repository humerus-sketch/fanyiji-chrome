# 翻译姬·Chrome字幕版

独立 Chrome 字幕项目，不初始化本地 ASR、VAD、标准翻译流水线或 WebView2。

## 下载

- [Windows x64 完整程序包](https://github.com/humerus-sketch/fanyiji-chrome/releases/download/v0.1.1/FanyiJiChrome-v0.1.1-windows-x64.zip)。无需安装 Python，完整解压后运行 `翻译姬·Chrome字幕版.exe`；不要只复制 EXE。
- [独立源码 ZIP](https://github.com/humerus-sketch/fanyiji-chrome/releases/download/v0.1.1/FanyiJiChrome-v0.1.1-source.zip)。
- [Release 与 SHA-256 校验文件](https://github.com/humerus-sketch/fanyiji-chrome/releases/tag/v0.1.1)。

语音版是另一个项目：[翻译姬·语音版](https://github.com/humerus-sketch/fanyiji)。两版可以独立安装和运行。

## 首次使用

1. 自行安装官方 Google Chrome。程序使用已有 Chrome，不包含 Chrome 浏览器安装包，也不包含 B站账号。
2. 启动程序，点击“Chrome 直播间”，填写目标房号，手动点击“打开专用 Chrome 直播间”；需要登录时自行完成登录。
3. 在专用 Chrome 的“设置 → 无障碍功能”开启“实时字幕”，添加日语并等待 Chrome 下载语言资源；开启“实时翻译”，目标语言选择中文。
4. 让直播页播放日语讲话，先确认 Chrome 原生中文字幕气泡有文字，再检查翻译姬的字幕显示。首次使用先保持原生气泡可见，读取正常后再尝试隐藏。

关闭气泡后可点击程序的“恢复原生字幕”；该操作取消隐藏选项并恢复显示，必要时刷新专用直播页。
原生字幕识别在设备端进行，实时翻译会将字幕发送给 Google，翻译需要网络；语言配置和可用条件参见 [Google 官方说明](https://support.google.com/chrome/answer/10538231?hl=zh-Hans)。
无法使用 Google 实时翻译时，可以使用另一个项目的语音版选择本地或其他翻译源。

## 功能

- 手动打开专用 Chrome 直播间，使用独立登录资料；也可只读连接现有 Chrome。
- 读取原生中文字幕并分段显示，透明隐藏、显示和恢复原生字幕气泡。
- 可选 GTCRN 前置增强、模型下载／校验、音频自动恢复和读取进程健康检测。
- 稳定译文弹幕同步、清空待发队列、词规则管理，公开程序包均可使用。
- 简体中文、日文、英文界面；默认字幕 16 号、透明度 66%。

直接打开完整程序目录中的 **翻译姬·Chrome字幕版.exe**，不能只复制 EXE。
点击“Chrome 直播间”，填写房号并手动打开专用窗口。首次由用户自行配置 Chrome 实时字幕／实时翻译和日译中资源。
现有 Chrome 只读连接不能隐藏气泡或发送弹幕。
独立资料目录：`%LOCALAPPDATA%\FanyiJiChrome`。
专用覆盖变量：`FANYIJI_CHROME_DATA`、`FANYIJI_CHROME_MODELS`。

## 弹幕同步

0.1.1 起公开程序包可使用弹幕同步，无需本机管理员授权或 `local-features.json`。首次使用默认关闭；更新保留此前保存的开关和设置。

1. 点击“Chrome 直播间”，填写目标直播间并手动打开专用 Chrome。
2. 在专用窗口自行登录 B站账号，确认直播页和中文字幕正常。
3. 勾选“同步稳定的新中文字幕到直播弹幕”，发送目标填写专用 Chrome 当前打开的同一房间，点击“保存弹幕设置”。

仅专用 Chrome 可发送；现有 Chrome 的只读连接继续只读取字幕。发送前检查登录，间隔至少 3.5 秒，保留结果核验、中文正文限制、分段、过滤词与队列处理。公开功能权限不改变 B站实际投递结果，也不自动发送测试消息。

## 边界

这是基于 Chrome 原生字幕气泡辅助功能结构的实验集成；Chrome 更新、语言资源缺失或窗口结构变化可能导致读取不可用。不是 Google 官方产品。
Chrome 只提供译文快照，没有日语原文或官方终稿标记，稳定入队由本程序判断。
GTCRN 不能保证去除全部伴奏／唱歌，也不能区分主播与游戏角色讲话。
普通字幕不调用 Codex。词规则管理保留相关分析组件，不会打开语音版浏览器进行规则验证。
拆分隔离运行链路，不代表翻译准确率已经改善。

## 打包

```powershell
python tools/build.py
```

输出 `dist/翻译姬·Chrome字幕版/`，不带本地识别、CTranslate2、WebView2 或 llama.cpp CPU / NVIDIA GPU 运行库。

## 资料隔离

首次正常启动会复制旧设置、词规则、相关浏览器资料和失败句历史，保留原目录。
待发队列和统计重新开始；两版配置、日志、队列、单实例锁及浏览器资料互不共用。
已下载权重通过 `models-location.json` 复用，未复制或加入程序包。
新电脑默认模型目录为 `%LOCALAPPDATA%\FanyiJiModels\models`。
源码和程序包均不包含账号、Cookie、历史授权文件或模型权重；每位用户在自己的专用 Chrome 中登录。
启动不会自动打开直播间。

## 源码运行

使用 Python 3.12，当前项目可独立安装，不需要父目录或另一个项目。
两个项目应各自使用虚拟环境；不要把两套可编辑源码安装进同一个环境。

```powershell
python -m pip install -e ".[dev]"
python run.py
python -m pytest -q
```

`python run.py --smoke-test` 只检查界面，不采集声音、不打开浏览器、不写设置。
兼容旧 `FANYIJI_*` / `LOCAL_SUBTITLES_*` 环境变量；同时运行两版应使用各自专用变量。
显式 DATA 覆盖时不会导入旧账号资料。
