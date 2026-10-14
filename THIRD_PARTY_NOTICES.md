# Chrome 字幕版第三方组件

本文件仅描述独立 Chrome 字幕版。程序使用自行安装的 Google Chrome，不分发 Chrome 浏览器、模型权重、登录资料或本机管理员授权。

## 程序运行库

| 组件 | 本版用途与官方来源 |
| --- | --- |
| Python 3.12 | 程序运行时；[Python](https://www.python.org/)，发行目录保留所用运行时的 `LICENSE.txt` |
| PySide6-Essentials / shiboken6 / Qt 6.12.0 | 图形界面，动态使用 Qt DLL；[Qt for Python](https://www.qt.io/qt-for-python) |
| NumPy 2.5.3 | 可选音频增强的数据处理；[NumPy](https://github.com/numpy/numpy) |
| sherpa-onnx 1.13.8 / ONNX Runtime 1.28.2 | 可选 GTCRN 语音增强；[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)、[ONNX Runtime](https://github.com/microsoft/onnxruntime) |
| websocket-client 1.9.2 | 程序自有 Chrome 的本机 CDP 通道；[官方源码](https://github.com/websocket-client/websocket-client/) |
| PyInstaller 6.22.3 | 打包引导程序；[PyInstaller](https://github.com/pyinstaller/pyinstaller)，保留 `COPYING.txt` 与引导程序例外说明 |

发行目录 `licenses` 保存安装依赖提供的许可原文。补充许可与对应来源位于源码 `third_party_licenses`，构建复制到 `licenses/supplemental`；`MANIFEST.json` 记录原始来源、版本、大小与 SHA-256，不包含本机路径或用户资料。

Qt 安装包声明 `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`，本程序动态使用未修改的发行 DLL。同版本 Qt 源码、许可与第三方归属见补充目录 `Qt/NOTICE.txt`；商业许可引用文件不授予商业许可。ONNX Runtime 同版本 MIT 原文及第三方归属亦保留。

补充许可目录保留拆分前已经取得的其他组件文本，以维持来源记录；这些文本不表示本版包含对应组件。Chrome 版不初始化或分发 SenseVoice / Silero VAD、CTranslate2、SentencePiece、PyAudioWPatch、soxr、WebView2、llama.cpp、CUDA / cuDNN 运行库。

## 外置 GTCRN 模型

GTCRN 作者 [官方仓库](https://github.com/Xiaobin-Rong/gtcrn) 许可为 MIT。程序可下载 [sherpa-onnx 官方增强模型资产](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speech-enhancement-models) `gtcrn_simple.onnx`，模型大小 535,638 bytes，SHA-256 `e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534`。权重外置，不随本程序包分发。

## Chrome 原生字幕

字幕识别、翻译、语言资源和浏览器更新由 Google Chrome 提供；本程序通过 Windows UI Automation 读取原生字幕气泡，并提供独立显示和可选前置增强。字幕气泡的辅助功能结构不属于稳定对外 API，透明隐藏与读取兼容性仍需实机验证。

原生识别在设备端处理，实时翻译将字幕发送给 Google；语言与设置参见 [Google 官方帮助](https://support.google.com/chrome/answer/10538231?hl=zh-Hans)。本项目不是 Google 官方产品。

第三方许可适用于相应组件，不替代模型、浏览器或平台条款，也不授予平台发送权限。本仓库公开自有源码，但未指定自有代码的开放源代码许可证，不应将第三方 MIT 等许可视作整个项目的许可证。
