@echo off
chcp 65001 >nul
setlocal EnableExtensions
title 小语的工作台 - RTX 视频特效依赖安装

REM ============================================================================
REM  给 RTX 超分降噪 / RTX 转HDR / RTX 视频插帧 三个节点安装依赖
REM
REM  说明：其实不用手动跑本脚本——把插件放进 custom_nodes\ 后，第一次启动
REM  ComfyUI 会自动安装依赖。本脚本只是给“启动时没装上”的情况手动补装。
REM
REM  关键坑：公共 PyPI（以及华为/清华/中科大等镜像）上的 nvidia-vfx 只有
REM  4.8KB 的空壳包，真正的 wheel（约 400MB，含全套 SDK）只在 NVIDIA 官方源
REM  https://pypi.nvidia.com 发布，所以必须带 --extra-index-url。
REM
REM  安装顺序：
REM    1) 先检查是否已经装好（已装就直接结束，绝不重复安装/占用 DLL）
REM    2) 插件目录 wheels\ 里的离线 wheel（断网也能装）
REM    3) NVIDIA 官方源（自动下载真 wheel）
REM    4) 公共 PyPI 兜底
REM ============================================================================

set "WHEELS_DIR=%~dp0wheels"

REM ---- 1. 找 Python：优先 ComfyUI 便携版，其次系统 PATH ----------------------
set "PY="
if exist "%~dp0..\..\..\python_embeded\python.exe" set "PY=%~dp0..\..\..\python_embeded\python.exe"
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do if not defined PY set "PY=%%p"
)
if not defined PY (
    echo.
    echo [错误] 没找到 Python。请确认本插件在 ComfyUI\custom_nodes\ 目录里，
    echo 或手动执行：
    echo     python -m pip install nvidia-vfx --extra-index-url https://pypi.nvidia.com
    pause
    exit /b 1
)
echo 使用 Python：%PY%
echo.

REM ---- 2. 已装好就直接结束（避免 ComfyUI 开着时 DLL 被占用导致安装失败）-------
"%PY%" -c "import nvvfx" >nul 2>nul
if not errorlevel 1 (
    echo nvidia-vfx 已经安装好了，无需重复安装。
    goto success
)

REM ---- 3. 优先安装 wheels\ 目录里的离线 wheel -------------------------------
set "LOCAL_WHEEL="
for /f "delims=" %%f in ('dir /b /a-d "%WHEELS_DIR%\nvidia_vfx-*.whl" 2^>nul') do (
    if not defined LOCAL_WHEEL set "LOCAL_WHEEL=%WHEELS_DIR%\%%f"
)
if defined LOCAL_WHEEL (
    echo 找到离线安装包，开始本地安装（无需联网）：
    echo     %LOCAL_WHEEL%
    echo.
    "%PY%" -m pip install --force-reinstall --no-deps "%LOCAL_WHEEL%"
    if not errorlevel 1 goto verify
    echo.
    echo [警告] 离线 wheel 安装失败（可能是 Python 版本不匹配），改为联网安装...
    echo.
)

REM ---- 4. 联网安装：NVIDIA 官方源（真 wheel 唯一发布地） --------------------
echo 正在从 NVIDIA 官方源安装 nvidia-vfx（约 400MB，慢网请耐心等）...
"%PY%" -m pip install --no-cache-dir nvidia-vfx --extra-index-url https://pypi.nvidia.com
if not errorlevel 1 goto verify

echo.
echo [警告] NVIDIA 官方源安装失败，尝试当前环境配置的公共源（可能只有空壳）...
"%PY%" -m pip install --no-cache-dir nvidia-vfx
if not errorlevel 1 goto verify

REM ---- 5. 全部失败 ------------------------------------------------------------
echo.
echo ============================================================
echo [失败] 自动安装没成功。建议：
echo   1) 关掉 ComfyUI 后重跑本脚本（DLL 被占用会导致安装失败）；
echo   2) 若还不行，手动执行下面命令后把报错截图发出来：
echo     "%PY%" -m pip install nvidia-vfx --extra-index-url https://pypi.nvidia.com
echo.
echo  另：这三个节点需要 RTX 20 系及以上显卡（视频插帧仅 40/50 系），
echo  显卡驱动建议升级到 570.65 以上。
echo ============================================================
pause
exit /b 1

REM ---- 6. 验证导入 ------------------------------------------------------------
:verify
echo.
"%PY%" -c "import nvvfx"
if errorlevel 1 goto driver
:success
echo.
echo 全部完成，重启 ComfyUI 后即可在右键菜单找到三个 RTX 视频特效节点。
echo （重启 ComfyUI 时它也会自动识别依赖，无需其它操作。）
pause
exit /b 0

:driver
echo.
echo [注意] 包装上了但 import nvvfx 失败：多半是显卡驱动太旧，
echo        请把 NVIDIA 驱动升级到 570.65 以上后重启 ComfyUI。
pause
exit /b 1
