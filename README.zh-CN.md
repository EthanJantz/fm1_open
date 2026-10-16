# FM1 Open V12

[English](README.md) · 简体中文

8 轨、8 槽采样、16 套 FX DEMO，已含 SDK。

[在线工作台与开发包](https://open.m-vave.com/fm1-workbench) · [官方论坛](https://forum.m-vave.com)

在工作台中点击“源码与 SDK”，下载与线上版本对应的开发包。

## 编译

需要 Windows x64、PowerShell、`curl.exe` 和 `tar.exe`。编译固件还需安装[杰理 PI32v2 clang 4.0.1 r3 编译器](https://doc.zh-jieli.com/Tools/zh-cn/dev_tools/dev_env/jl_toolchain.html)，所在目录须含 `bin/clang.exe`。

将 `FM1_SOURCE_URL` 设置为本次构建对应源码包的 HTTPS 地址，包内须含本次修改、许可声明和编译／安装脚本；未配置时，网页构建和打包会停止。

M-VAVE 官方发布统一使用 `https://open.m-vave.com/releases/` 下的对应源码包地址。发布前确认该地址可下载，再生成在线及离线工作台；修改源码后须同步提供对应开发包。第三方构建可使用自己的源码托管地址。

~~~powershell
.\build.bat --web
$env:FM1_PI32_TOOLCHAIN = 'C:\JL\pi32'
.\build.bat --package
~~~

网页由附带的 C 源码编译。首次编译会下载 WASI SDK 27，可设置 `FM1_WASI_SDK` 复用已有安装。

输出：`build/preview/index.html`（预览）、`build/dx7/generated/FM1_OFFICIAL_OPEN.fwsc`（固件）、`build/official-web/FM1-Official-Open-Source.html`（完整工作台）。

硬件驱动在 `app/board/`；音频中断调用 `fm1_engine_render()`，`app/audio_output.c` 处理 USB／蓝牙混音和 USB 录音。

APP 采用 [GPLv3＋SDK 链接例外](LICENSE.md)。设备引擎以二进制提供，另适用 [SDK 与第三方条款](THIRD_PARTY.md)。

刷写前请备份工程和采样。
