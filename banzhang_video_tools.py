# -*- coding: utf-8 -*-
"""
👨‍🏫班长 视频与音频工具模块
包含：视频合并、音乐播放器、手动输入歌词（上游传入/一键固定）、加载歌词文件、图像对比
"""

import os
import asyncio
import subprocess
import threading
import queue
import numpy as np
import folder_paths
import tempfile
import torch
import torch.nn.functional as F
import random
import string
import wave
import sys
import json
import shutil
import fractions
import mimetypes

try:
    import av  # PyAV：ffmpeg 库随 Python 包自带，无需系统安装 ffmpeg
    HAS_AV = True
except ImportError:
    HAS_AV = False
    print("[Banzhang] PyAV(av) missing: 视频合并将不可用，请 pip install av")

# ==========================================
# 依赖检查
# ==========================================
HAS_VIDEO = False
HAS_AUDIO = False

try:
    import imageio_ffmpeg
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    HAS_VIDEO = True
    print("[Banzhang] imageio_ffmpeg OK, banzhangVideoCombine enabled")
except ImportError:
    ffmpeg_path = "ffmpeg"
    print("[Banzhang] imageio_ffmpeg missing, trying system ffmpeg")

try:
    import torchaudio
    HAS_AUDIO = True
    print("[Banzhang] torchaudio OK, MusicPlayer enabled")
except ImportError:
    print("[Banzhang] torchaudio missing, MusicPlayer disabled")

try:
    import soundfile as sf
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "soundfile"])
        import soundfile as sf
    except Exception:
        sf = None

try:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    from comfy.utils import ProgressBar
except ImportError:
    ProgressBar = None

# ==========================================
# API 接口：在系统文件管理器中打开文件夹
# ==========================================
try:
    from server import PromptServer
    from aiohttp import web
    HAS_SERVER = True
except Exception as e:
    HAS_SERVER = False
    print(f"⚠️ [班长] 无法导入 PromptServer，文件夹打开接口不可用: {e}")

if HAS_SERVER:
    @PromptServer.instance.routes.post("/banzhang_api/open_folder")
    async def 打开文件夹接口(request):
        try:
            数据包 = await request.json()
            目标路径 = str(数据包.get("path", "")).strip().strip('"').strip("'")
            if not 目标路径:
                return web.json_response({"success": False, "msg": "路径为空"})
            if not os.path.exists(目标路径):
                return web.json_response({"success": False, "msg": "路径不存在"})
            try:
                if os.path.isfile(目标路径):
                    # 传入的是文件：弹出资源管理器并自动定位选中该文件
                    if os.name == "nt":
                        subprocess.Popen(f'explorer /select,"{os.path.normpath(目标路径)}"')
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", "-R", 目标路径])
                    else:
                        subprocess.Popen(["xdg-open", os.path.dirname(目标路径)])
                else:
                    # 传入的是目录：直接打开该目录
                    if os.name == "nt":
                        os.startfile(目标路径)
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", 目标路径])
                    else:
                        subprocess.Popen(["xdg-open", 目标路径])
            except Exception as e:
                return web.json_response({"success": False, "msg": str(e)})
            return web.json_response({"success": True, "path": 目标路径})
        except Exception as e:
            return web.json_response({"success": False, "msg": str(e)})

    # 视频文件读取接口：供前端预览保存在自定义路径（output 目录之外）的视频
    # 支持 Range 请求，保证 <video> 可拖动进度
    @PromptServer.instance.routes.get("/banzhang_api/video_file")
    async def 读取视频文件接口(request):
        try:
            文件路径 = str(request.query.get("path", "")).strip()
            视频后缀 = (".mp4", ".mov", ".mkv", ".webm", ".gif", ".webp", ".avi", ".png", ".jpg", ".jpeg")
            if not 文件路径 or not os.path.isfile(文件路径):
                return web.Response(status=404, text="file not found")
            if not 文件路径.lower().endswith(视频后缀):
                return web.Response(status=403, text="forbidden")

            文件大小 = os.path.getsize(文件路径)
            content_type = mimetypes.guess_type(文件路径)[0] or "application/octet-stream"

            区间头 = request.headers.get("Range")
            if 区间头 and 区间头.startswith("bytes="):
                try:
                    起止 = 区间头[6:].split("-")
                    起点 = int(起止[0]) if 起止[0] else 0
                    终点 = int(起止[1]) if 起止[1] else 文件大小 - 1
                    终点 = min(终点, 文件大小 - 1)
                except ValueError:
                    return web.Response(status=416)
                with open(文件路径, "rb") as f:
                    f.seek(起点)
                    数据 = f.read(终点 - 起点 + 1)
                return web.Response(status=206, body=数据, headers={
                    "Content-Type": content_type,
                    "Content-Range": f"bytes {起点}-{终点}/{文件大小}",
                    "Accept-Ranges": "bytes",
                })

            with open(文件路径, "rb") as f:
                数据 = f.read()
            return web.Response(status=200, body=数据, headers={
                "Content-Type": content_type,
                "Accept-Ranges": "bytes",
            })
        except Exception as e:
            return web.Response(status=500, text=str(e))

    # 调色台「应用并导出」接口：直接对本地已有视频做调色重编码
    # 不进 ComfyUI 执行队列、不触发上游节点、不写入任何节点控件
    @PromptServer.instance.routes.post("/banzhang_api/bake_video")
    async def 视频调色烘焙接口(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"success": False, "msg": "请求体不是合法 JSON"})

        源路径 = str(body.get("src", "")).strip()
        if not 源路径 or not os.path.isfile(源路径):
            return web.json_response({"success": False, "msg": "源视频路径无效或文件不存在"})

        # 复用 _read_tune 的兜底与范围夹紧：前端漏传某项也不会炸
        tune = _read_tune(body.get("tune") or {})
        # 输出跟随源文件所在目录，方便与原片并排对比
        保存目录 = os.path.dirname(源路径) or folder_paths.get_output_directory()

        try:
            # 编码是 CPU 密集操作，丢线程池执行，避免阻塞 ComfyUI 的 HTTP 服务
            结果 = await asyncio.to_thread(
                _bake_video_tuned, 源路径, tune, 保存目录, body.get("fps"))
        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({"success": False, "msg": f"导出失败: {e}"})

        print(f"[Banzhang] 调色导出 → {结果.get('path', '')} | {结果.get('msg', '')}".strip())
        return web.json_response(结果)


    # ==========================================
    # 工作流元数据嵌入（拖入 ComfyUI 画布可还原工作流）
    # 说明：不能用 -metadata 命令行传值——工作流 JSON 常达数十 KB，
    # 会触发 Windows 命令行长度上限（WinError 206）；改用 ffmetadata 文件。
    # ==========================================
    def _embed_workflow_metadata(src_path, dst_path, embed_meta, fmt_ext):
        meta_txt = dst_path + ".ffmeta"
        try:
            lines = [";FFMETADATA1"]
            for k, v in embed_meta.items():
                esc = v.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
                lines.append(f"{k}={esc}")
            with open(meta_txt, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

            cmd = [ffmpeg_path, "-y", "-loglevel", "error", "-i", src_path, "-i", meta_txt,
                   "-map", "0", "-map_metadata", "1", "-c", "copy"]
            if fmt_ext in ("mp4", "mov", "m4v"):
                # use_metadata_tags 保留任意键名；faststart 将 moov 前置（前端只读前 64MB）
                cmd += ["-movflags", "use_metadata_tags+faststart"]
            cmd += ["-f", {"mp4": "mp4", "mov": "mov", "m4v": "mp4", "mkv": "matroska", "webm": "webm"}.get(fmt_ext, fmt_ext), dst_path]
            remux = subprocess.run(cmd, capture_output=True, timeout=300)
            ok = remux.returncode == 0 and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
            if not ok:
                print(f"[Banzhang] workflow embed failed: {remux.stderr.decode(errors='replace')[:200]}")
            return ok
        except Exception as embed_e:
            print(f"[Banzhang] workflow embed error: {embed_e}")
            return False
        finally:
            if os.path.isfile(meta_txt):
                try:
                    os.remove(meta_txt)
                except OSError:
                    pass


# ==========================================
# 音频工具函数
# ==========================================
def save_audio_robust(filepath, waveform, sample_rate, format="wav"):
    if isinstance(waveform, torch.Tensor):
        data = waveform.detach().cpu()
        if data.dim() == 3:
            data = data[0]
    else:
        data = waveform

    # 统一转为 numpy 用于后续处理
    if isinstance(data, torch.Tensor):
        data_np = data.numpy()
    else:
        data_np = np.asarray(data)

    if data_np.ndim == 1:
        data_np = np.expand_dims(data_np, axis=0)

    format_lower = format.lower()

    if format_lower == "wav":
        try:
            data_int = np.clip(data_np * 32767, -32768, 32767).astype(np.int16)
            channels, samples = data_int.shape

            with wave.open(filepath, 'wb') as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                if channels == 1:
                    wf.writeframes(data_int.tobytes())
                else:
                    interleaved = np.empty((samples * channels,), dtype=np.int16)
                    for c in range(channels):
                        interleaved[c::channels] = data_int[c]
                    wf.writeframes(interleaved.tobytes())
            return True
        except Exception as e:
            print(f"[Banzhang] wave save failed: {e}")

    # 非 wav 格式：soundfile → torchaudio → ffmpeg，依次降级。
    # 顺序不能反过来：便携版实测 torchaudio.save 因缺 libtorchcodec 对**所有**
    # 格式一律失败（wav / flac / ogg / mp3 全试过），放在前面等于每次都白跑一轮、
    # 还往终端刷一条吓人的报错；而 soundfile 自带 libsndfile，wav/flac/ogg 都好。
    # 剩下的 mp3 之类 soundfile 也写不了的，交给最后的 ffmpeg。
    if data_np.ndim == 2 and data_np.shape[0] > 1:
        data_for_save = data_np.T  # (channels, samples) -> (samples, channels)
    else:
        data_for_save = data_np.squeeze()

    if sf:
        try:
            sf.write(filepath, data_for_save, sample_rate, format=format_lower)
            return True
        except Exception as e_sf:
            print(f"[Banzhang] soundfile save failed: {e_sf}")

    try:
        torchaudio.save(filepath, data, sample_rate, format=format_lower)
        return True
    except Exception as e:
        print(f"[Banzhang] torchaudio save failed: {e}")

    # fallback: 使用 ffmpeg 保存（支持 mp3/ogg/flac 等）
    if format_lower in ("mp3", "ogg", "flac"):
        try:
            # 将 numpy 数据写入临时 wav，再用 ffmpeg 转码
            tmp_wav = filepath + ".tmp.wav"
            if save_audio_robust(tmp_wav, data_np, sample_rate, format="wav"):
                codec_map = {"mp3": "libmp3lame", "ogg": "libvorbis", "flac": "flac"}
                codec = codec_map.get(format_lower, "libmp3lame")
                result = subprocess.run(
                    [ffmpeg_path, "-y", "-i", tmp_wav, "-c:a", codec, "-b:a", "192k", filepath],
                    capture_output=True, text=True
                )
                os.remove(tmp_wav)
                if result.returncode == 0:
                    return True
                else:
                    print(f"[Banzhang] ffmpeg encode failed: {result.stderr}")
        except Exception as e_ff:
            print(f"[Banzhang] ffmpeg fallback failed: {e_ff}")

    return False


def load_audio_file(filepath):
    ext = filepath.lower().split('.')[-1]
    if ext == 'wav':
        try:
            with wave.open(filepath, 'rb') as wf:
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
                if sampwidth == 1:
                    dtype = np.uint8
                elif sampwidth == 2:
                    dtype = np.int16
                elif sampwidth == 4:
                    dtype = np.int32
                else:
                    dtype = np.int16
                arr = np.frombuffer(raw, dtype=dtype)
                if channels > 1:
                    arr = arr.reshape(-1, channels).T
                else:
                    arr = arr.reshape(1, -1)
                tensor = torch.from_numpy(arr.astype(np.float32) / (2 ** (8 * sampwidth - 1)))
                return tensor, sr
        except:
            pass
    # 非标准 wav：交给全插件共用的解码层（soundfile → PyAV → torchaudio 依次
    # 降级）。原先只有 torchaudio + soundfile 两级，漏掉了格式最全的 PyAV。
    try:
        from .csglide_audio_io import decode_audio_file
        return decode_audio_file(filepath)
    except Exception:
        pass
    # 最终兜底：用 ffmpeg 转成 wav（仅依赖内置 wave 模块读取，不依赖第三方解码库）
    try:
        tmp_wav = os.path.join(tempfile.gettempdir(), f"_banzhang_decode_{random.randint(0, 10 ** 8)}.wav")
        result = subprocess.run(
            [ffmpeg_path, "-y", "-i", filepath, "-ac", "2", "-ar", "44100", tmp_wav],
            capture_output=True, text=True
        )
        if result.returncode == 0 and os.path.exists(tmp_wav):
            waveform, sr = load_audio_file(tmp_wav)  # wav 走内置 wave 读取
            try:
                os.remove(tmp_wav)
            except Exception:
                pass
            return waveform, sr
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except Exception:
                pass
    except Exception as e:
        print(f"[Banzhang] ffmpeg decode fallback failed: {e}")
    raise Exception("Audio decode failed")


# ==========================================
# 编码辅助：ffmpeg 子进程管道编码（进程隔离）
# 编码器原生崩溃只失败本节点，不再拖垮 ComfyUI 主进程
# ==========================================
_NVENC_CACHE = {}


def _has_nvenc(codec="h264_nvenc"):
    """检测 GPU 硬件编码器是否可用（结果缓存，只查一次）。"""
    if codec in _NVENC_CACHE:
        return _NVENC_CACHE[codec]
    ok = False
    try:
        r = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        ok = codec in r.stdout.decode("utf-8", "replace")
    except Exception:
        ok = False
    _NVENC_CACHE[codec] = ok
    return ok


# 调色参数的键名与默认值：调色台「应用并导出」走 bake 接口，节点上已无这些控件，
# 仅供接口解析与烘焙成片报告使用（改名会同时影响前端面板的提交字段，勿单侧修改）
TUNE_DEFAULTS = {
    "🎨 饱和度": 1.0,
    "✨ 泛光强度": 0.0, "✨ 泛光起点": 0.75, "✨ 泛光范围": 4.0,
    "🌫️ 柔雾强度": 0.0, "🌫️ 柔焦范围": 4.0, "🌫️ 灰雾浓度": 0.30,
    "🔥 暖晕强度": 0.0, "🔥 暖晕起点": 0.70, "🔥 暖晕范围": 5.0,
}


def _read_tune(kwargs):
    """从 kwargs 读全部调色参数：旧工作流缺控件时取默认值，缺一个也不报错"""
    tune = {}
    for name, d in TUNE_DEFAULTS.items():
        try:
            v = float(kwargs.get(name, d))
        except Exception:
            v = d
        if not (v == v):      # NaN
            v = d
        tune[name] = v
    tune["🎨 饱和度"] = min(2.0, max(0.0, tune["🎨 饱和度"]))
    tune["✨ 泛光强度"] = min(1.0, max(0.0, tune["✨ 泛光强度"]))
    tune["✨ 泛光起点"] = min(0.95, max(0.0, tune["✨ 泛光起点"]))
    tune["✨ 泛光范围"] = min(10.0, max(1.0, tune["✨ 泛光范围"]))
    tune["🌫️ 柔雾强度"] = min(1.0, max(0.0, tune["🌫️ 柔雾强度"]))
    tune["🌫️ 柔焦范围"] = min(10.0, max(1.0, tune["🌫️ 柔焦范围"]))
    tune["🌫️ 灰雾浓度"] = min(1.0, max(0.0, tune["🌫️ 灰雾浓度"]))
    tune["🔥 暖晕强度"] = min(1.0, max(0.0, tune["🔥 暖晕强度"]))
    tune["🔥 暖晕起点"] = min(0.95, max(0.0, tune["🔥 暖晕起点"]))
    tune["🔥 暖晕范围"] = min(10.0, max(1.0, tune["🔥 暖晕范围"]))
    return tune


def _tune_text(tune):
    """详情面板的调色行：只列出真正生效的项"""
    t = ""
    if abs(tune["🎨 饱和度"] - 1.0) > 1e-4:
        t += f"🎨 饱和度 : {tune['🎨 饱和度']:g}\n"
    if tune["✨ 泛光强度"] > 1e-4:
        t += f"✨ 泛光 : {tune['✨ 泛光强度']:g} / 起点 {tune['✨ 泛光起点']:g} / 范围 {tune['✨ 泛光范围']:g}\n"
    if tune["🌫️ 柔雾强度"] > 1e-4:
        t += f"🌫️ 柔雾 : {tune['🌫️ 柔雾强度']:g} / 范围 {tune['🌫️ 柔焦范围']:g} / 灰雾 {tune['🌫️ 灰雾浓度']:g}\n"
    if tune["🔥 暖晕强度"] > 1e-4:
        t += f"🔥 暖晕 : {tune['🔥 暖晕强度']:g} / 起点 {tune['🔥 暖晕起点']:g} / 范围 {tune['🔥 暖晕范围']:g}\n"
    return t


def _blur_direct(x, s):
    """原尺寸可分离高斯：横向一遍、纵向一遍。x: NCHW"""
    r = max(1, int(3 * s))
    k = torch.arange(-r, r + 1, dtype=x.dtype, device=x.device)
    kern = torch.exp(-0.5 * (k / s) ** 2)
    kern = kern / kern.sum()
    n, c, h, w = x.shape
    t = x.reshape(n * c, 1, h, w)
    t = F.conv2d(F.pad(t, (r, r, 0, 0), mode="replicate"), kern.view(1, 1, 1, -1))
    t = F.conv2d(F.pad(t, (0, 0, r, r), mode="replicate"), kern.view(1, 1, -1, 1))
    return t.reshape(n, c, h, w)


def _blur(x, s, ds_min=6.0, short_min=192):
    """高斯模糊，replicate 填充避免边缘发黑。x: NCHW

    sigma 是按画面宽度算出来的（见 _apply_tuning 的 rad * 0.00x * w），4K 下
    涨到 90 以上、核宽近 600 像素，逐帧六次这种卷积——这是调色导出慢的主因。
    模糊是纯低频操作，放到 1/ds 的小图上算完再放大回来，像素量与核宽同时
    缩小。实测（3090）：4K 13~16 倍、1080p 5 倍，与全尺寸结果平均差 0.07
    灰阶（0-255），肉眼不可分辨。

    sigma 小时 ds 落到 1，走原尺寸路径，不引入额外插值误差；short_min 保证
    小图短边不低于 192，低分辨率素材不会为了省算力而糊过头。
    """
    ds = max(1, int(s // ds_min))
    ds = min(ds, max(1, min(x.shape[-2], x.shape[-1]) // short_min))
    h, w = x.shape[-2] // ds, x.shape[-1] // ds
    if ds > 1 and h >= 2 and w >= 2:
        small = F.interpolate(x, size=(h, w), mode="area")
        small = _blur_direct(small, s / ds)
        return F.interpolate(small, size=(x.shape[-2], x.shape[-1]),
                             mode="bilinear", align_corners=False)
    return _blur_direct(x, s)


def _bright_pass(x, thr):
    """提取亮部：用亮度做 mask 但保留原色，低于 thr 的全部压成 0。x: NCHW"""
    lum = x[:, 0:1] * 0.299 + x[:, 1:2] * 0.587 + x[:, 2:3] * 0.114
    mask = torch.clamp((lum - thr) / max(1e-4, 1.0 - thr), 0.0, 1.0)
    return x * mask


@torch.inference_mode()
def _apply_tuning(images, tune):
    """合并调色：饱和度 + 泛光 + 暖晕 + 柔雾 在单次遍历内完成。

    四种效果各自单独实现过时，一次调色要经历 4 次 NHWC↔NCHW 转置、4 次分批
    循环、4 次显存回退判定；合并后只需一次遍历，并全程 inference_mode 关掉
    autograd 记录——显著降低「调好参数后生成偏慢」的开销。
    处理顺序：饱和度 → 泛光 → 暖晕 → 柔雾。
    """
    sat = tune["🎨 饱和度"]
    bloom_i, bloom_t, bloom_r = tune["✨ 泛光强度"], tune["✨ 泛光起点"], tune["✨ 泛光范围"]
    warm_i, warm_t, warm_r = tune["🔥 暖晕强度"], tune["🔥 暖晕起点"], tune["🔥 暖晕范围"]
    soft_i, soft_r, haze = tune["🌫️ 柔雾强度"], tune["🌫️ 柔焦范围"], tune["🌫️ 灰雾浓度"]

    need_sat = abs(sat - 1.0) > 1e-4
    need_bloom = bloom_i > 1e-4
    need_warm = warm_i > 1e-4
    need_soft = soft_i > 1e-4
    if not (need_sat or need_bloom or need_warm or need_soft):
        return images

    # 参数预归一化（写代码时保持与单独实现版取值一致，避免手滑改错）
    bloom_thr = min(0.99, max(0.0, float(bloom_t)))
    bloom_rad = min(10.0, max(1.0, float(bloom_r)))
    warm_thr = min(0.99, max(0.0, float(warm_t)))
    warm_rad = min(10.0, max(1.0, float(warm_r)))
    warm_tint = (1.35, 0.60, 0.28)
    soft_mix = soft_i * 0.5
    soft_rad = min(10.0, max(1.0, float(soft_r)))
    soft_hz = min(1.0, max(0.0, float(haze)))
    soft_contrast = 1.0 - soft_hz * 0.18
    soft_lift = soft_hz * 0.055

    def _batch(t):
        n, h, w, _ = t.shape
        # 批次上限：CUDA 上按实时可用显存算，CPU 上固定 4 帧。
        # 旧口径写死 400 万像素预算，4K（829 万）一除就是 0，兜底成 1——
        # 等于逐帧跑，显卡完全吃不饱。每帧峰值按 6 份 float32 估（输入 /
        # 输出 / 中间量），只取四成可用显存，别把生成模型的位置挤掉。
        step = 4
        if t.is_cuda:
            try:
                free, _ = torch.cuda.mem_get_info()
                step = max(1, min(16, int(free * 0.4) // max(1, h * w * 3 * 4 * 6)))
            except Exception:
                pass
        # 预分配输出、逐块写回：不再把每块结果攒在列表里最后 torch.cat。
        # 攒列表 + cat 会再申请一份与整段等大的连续内存，长片开启调色时
        # 就是这一份把内存顶爆的。写回式只多一份输出，峰值与块大小无关。
        out = torch.empty((n, h, w, 3), dtype=t.dtype, device=t.device)
        for s in range(0, n, step):
            e = min(s + step, n)
            x = t[s:e, :, :, :3].permute(0, 3, 1, 2)          # NHWC → NCHW
            if need_sat:
                gray = x[:, 0:1] * 0.299 + x[:, 1:2] * 0.587 + x[:, 2:3] * 0.114
                x = torch.clamp(gray + (x - gray) * sat, 0.0, 1.0)
            if need_bloom:
                glow = _blur(_bright_pass(x, bloom_thr), max(1.0, bloom_rad * 0.005 * w))
                x = torch.clamp(x + glow * bloom_i, 0.0, 1.0)
            if need_warm:
                glow = _blur(_bright_pass(x, warm_thr), max(1.0, warm_rad * 0.005 * w))
                glow = torch.cat([glow[:, 0:1] * warm_tint[0],
                                  glow[:, 1:2] * warm_tint[1],
                                  glow[:, 2:3] * warm_tint[2]], dim=1)
                x = torch.clamp(x + glow * warm_i, 0.0, 1.0)
            if need_soft:
                x = x * (1 - soft_mix) + _blur(x, max(0.5, soft_rad * 0.0022 * w)) * soft_mix
                x = torch.clamp((x - 0.5) * soft_contrast + 0.5 + soft_lift, 0.0, 1.0)
            out[s:e, :, :, :] = x.permute(0, 2, 3, 1)          # NCHW → NHWC，就地写回
        return out

    try:
        return _batch(images)
    except Exception as e:
        print(f"[Banzhang] 调色 GPU 处理失败，回退 CPU: {e}")
        try:
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return _batch(images.detach().cpu())


def _build_encode_candidates(fmt_str, w_ext, h_ext, n_frames, ext, fps=24.0):
    """编码参数候选链：按固定档位顺序逐个尝试，失败自动换下一档，尽量保住成品。

    档位顺序（画质优先）：GPU 高画质 → CPU 高画质 → GPU 极速 → CPU 极速 → CPU 兜底。
    档位不暴露为节点参数，全自动执行——新增 required 参数会让旧工作流报
    「Required input is missing」，并使 widgets_values 按下标错位。
    NVENC 在 GPU 上完成编码，几乎不占系统内存，是长视频/高分辨率场景下
    避免原生崩溃（内存不足 access violation）的备选路径。
    长视频(>=300帧)额外启用碎片化写入，进程被杀时已编码部分仍可抢救。
    """
    heavy = (w_ext * h_ext >= 1280 * 720) or (n_frames >= 300)  # 启用碎片化抢救写入
    # 碎片化写入：每约 2 秒一个关键帧，让已编码的分片及时落盘
    mux = (["-movflags", "+frag_keyframe+empty_moov", "-g", str(max(1, min(int(fps * 2), 50)))]
           if (n_frames >= 300 or heavy) and ext in ("mp4", "mov") else [])

    def wrap(args):
        return args + mux

    # ProRes / WEBM 无硬件编码器，直接走原 CPU 链路
    if "ProRes" in fmt_str:
        return [
            ("ProRes 422HQ", wrap(["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"])),
            ("ProRes 422 降级", wrap(["-c:v", "prores_ks", "-profile:v", "2", "-pix_fmt", "yuv422p10le"])),
        ]
    if "WEBM" in fmt_str:
        return [
            ("VP9 CRF20", wrap(["-c:v", "libvpx-vp9", "-crf", "20", "-b:v", "0", "-pix_fmt", "yuv420p"])),
            ("VP8 极速兜底", wrap(["-c:v", "libvpx", "-crf", "30", "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p"])),
        ]

    nv = ("HEVC" in fmt_str or "H265" in fmt_str)
    nvenc_name = "hevc_nvenc" if nv else "h264_nvenc"
    gpu_ok = _has_nvenc(nvenc_name) if HAS_VIDEO else False
    # HEVC 在 mov/mkv 容器需要显式 tag
    hevc_tag = ["-tag:v", "hvc1"] if nv and ext in ("mov", "mkv", "mp4") else []

    gpu_fast = wrap(["-c:v", nvenc_name, "-preset", "p1", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-pix_fmt", "yuv420p"] + hevc_tag)
    gpu_hq = wrap(["-c:v", nvenc_name, "-preset", "p4", "-tune", "hq", "-rc", "vbr", "-cq", "16", "-pix_fmt", "yuv420p"] + hevc_tag)
    cpu_hq = wrap(["-c:v", "libx264" if not nv else "libx265", "-crf", "12" if not nv else "16",
                   "-preset", "medium", "-pix_fmt", "yuv420p"] + hevc_tag)
    cpu_fast = wrap(["-c:v", "libx264" if not nv else "libx265", "-crf", "18" if not nv else "22",
                     "-preset", "veryfast", "-pix_fmt", "yuv420p"] + hevc_tag)
    cpu_last = wrap(["-c:v", "libx264", "-crf", "23", "-preset", "ultrafast", "-pix_fmt", "yuv420p"])

    # 固定档位顺序：画质优先，逐档降级
    candidates = []
    if gpu_ok:
        # NVENC 预检不可用时跳过 GPU 档，避免注定失败的尝试
        candidates.append(("GPU 高画质", gpu_hq))
    candidates.append(("CPU 高画质", cpu_hq))
    if gpu_ok:
        candidates.append(("GPU 极速", gpu_fast))
    candidates.append(("CPU 极速", cpu_fast))
    candidates.append(("CPU 兜底", cpu_last))
    return candidates


def _ffmpeg_encode_frames(images, n_frames, w_ext, h_ext, fps, out_path, candidates, audio_args, err_tmp, pbar=None, out_fmt=None):
    """管道喂原始 RGB 帧给 ffmpeg 子进程编码；候选参数逐个尝试，成功即返回。

    out_fmt：显式容器格式（part 文件后缀不可识别，必须显式指定）。
    stderr 落临时文件（非管道）：避免编码器日志写满缓冲与喂帧互相死锁。
    返回 (ok, err_msg, used_label)
    """
    last_err = ""
    for idx, (label, vargs) in enumerate(candidates):
        proc = None
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
            cmd = [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{w_ext}x{h_ext}", "-r", str(fps), "-i", "pipe:0",
            ] + audio_args + vargs + (["-f", out_fmt] if out_fmt else []) + [out_path]
            with open(err_tmp, "w", encoding="utf-8", errors="replace") as err_f:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err_f)
                # 批量转 uint8 后整块写入：帧在 GPU 时直接在 GPU 上转换，
                # 传输量从 float32(12B/像素) 降到 uint8(3B/像素)，并减少 Python 逐帧开销
                batch = max(1, min(32, 20000000 // max(1, w_ext * h_ext)))
                if images.is_cuda:
                    # 按实时可用显存再收一次上限（只用一半），显存紧张时自动缩小批次
                    try:
                        free_bytes, _ = torch.cuda.mem_get_info()
                        per_frame = w_ext * h_ext * 3 * 5  # float32 中间量 + uint8 结果
                        batch = max(1, min(batch, int(free_bytes * 0.5) // per_frame))
                    except Exception:
                        pass
                for s in range(0, n_frames, batch):
                    e = min(s + batch, n_frames)
                    chunk = images[s:e, :h_ext, :w_ext]
                    try:
                        u8 = chunk.mul(255).round_().clamp_(0, 255).to(torch.uint8)
                        if chunk.is_cuda:
                            u8 = u8.cpu().numpy()
                        else:
                            u8 = u8.numpy()
                    except Exception:
                        # 显存不足等：退回 CPU 逐帧转换，保证能出片
                        u8 = np.stack([
                            np.ascontiguousarray((images[i].detach().cpu().numpy()[:h_ext, :w_ext] * 255).astype(np.uint8))
                            for i in range(s, e)
                        ])
                    proc.stdin.write(np.ascontiguousarray(u8).tobytes())
                    if idx == 0 and pbar:
                        pbar.update(e - s)
                proc.stdin.close()
                ret = proc.wait()
                proc = None

            if ret == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                if idx > 0:
                    print(f"[Banzhang] 编码降级成功，使用档位: {label}")
                return True, "", label

            tail = ""
            try:
                with open(err_tmp, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read().strip()[-400:]
            except Exception:
                pass
            last_err = f"[{label}] 退出码 {ret} {tail}".strip()
            print(f"[Banzhang] 编码尝试失败 {last_err}")
        except Exception as e:
            last_err = f"[{label}] {e}"
            print(f"[Banzhang] 编码尝试异常 {last_err}")
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
    return False, last_err or "未知错误", ""


def _ffmpeg_remux(src_path, dst_path, ext, in_fmt=None):
    """碎片/临时文件 → 标准容器（重建 moov + faststart），失败返回 False。"""
    try:
        cmd = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
        if in_fmt:
            cmd += ["-f", in_fmt]
        cmd += ["-i", src_path, "-c", "copy", "-err_detect", "ignore_err"]
        if ext in ("mp4", "mov"):
            cmd += ["-movflags", "+faststart"]
        cmd += [dst_path]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = r.returncode == 0 and os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0
        if not ok and os.path.isfile(dst_path):
            try:
                os.remove(dst_path)
            except Exception:
                pass
        return ok
    except Exception as e:
        print(f"[Banzhang] remux 失败: {e}")
        return False


def _bake_progress(src_path, done, total):
    """导出进度广播。前端「应用并导出」按钮据此显示百分比。"""
    try:
        PromptServer.instance.send_sync("banzhang.bake.progress", {
            "src": src_path, "done": int(done), "total": int(max(total, done)),
        })
    except Exception:
        pass


def _bake_video_tuned(src_path, tune, save_dir, out_fps=None):
    """对本地已有视频做调色重编码：不进 ComfyUI 队列、不触发上游节点、不写任何节点控件。

    三段流水线——后台线程解码 → GPU 调色 → 后台线程写编码器，三者重叠进行：
    解码的 swscale 是纯 CPU 活，写管道要等编码器，把它们从主线上挪开后
    GPU 才不会干等。峰值内存只与批次大小相关，长视频不会一次性铺满内存。
    音频用 -c:a copy 无损搬运（webm 容器不支持 aac，该分支改 libopus）。
    输出与源文件同目录、同容器，命名 {源名}_调色_NN.{ext}，原片保留。
    每次都是「从传进来的源重新算」，因此反复导出不会累积失真。
    返回 {success:True, path, filename, fps, frame_count, engine, report} 或 {success:False, msg}
    """
    if not HAS_AV:
        return {"success": False, "msg": "缺少 PyAV(av) 组件，无法解码源视频（pip install av）"}
    if not HAS_VIDEO:
        return {"success": False, "msg": "缺少 ffmpeg(imageio_ffmpeg) 组件，无法编码"}

    ext = os.path.splitext(src_path)[1].lstrip(".").lower()
    if ext not in ("mp4", "mov", "mkv", "webm"):
        return {"success": False, "msg": f"不支持对 .{ext} 文件做调色导出（仅 mp4 / mov / mkv / webm）"}

    fmt_std = {"mp4": "MP4 (H264/通用)", "mov": "MOV (H264/网页兼容)",
               "mkv": "MKV (H264/防损坏)", "webm": "WEBM (网页/透明)"}[ext]
    out_fmt = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "webm": "webm"}[ext]

    # --- 探测源信息 ---
    try:
        with av.open(src_path) as c:
            vs = c.streams.video[0]
            src_w, src_h = int(vs.width), int(vs.height)
            src_fps = float(vs.average_rate) if vs.average_rate else 24.0
            n_frames = int(vs.frames) if vs.frames else 0
            if not n_frames and c.duration:
                n_frames = int(c.duration / 1_000_000.0 * src_fps)
            has_audio = len(c.streams.audio) > 0
    except Exception as e:
        return {"success": False, "msg": f"无法读取源视频信息: {e}"}

    fps = float(out_fps) if out_fps else src_fps
    w_ext, h_ext = (src_w // 2) * 2, (src_h // 2) * 2
    if w_ext < 2 or h_ext < 2:
        return {"success": False, "msg": f"源视频尺寸异常: {src_w}x{src_h}"}

    # --- 自增命名：与源文件同目录，原片保留 ---
    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception as e:
        return {"success": False, "msg": f"无法创建保存目录: {e}"}
    stem = os.path.splitext(os.path.basename(src_path))[0]
    counter = 1
    while True:
        file_name = f"{stem}_调色_{counter:02d}.{ext}"
        file_path = os.path.join(save_dir, file_name)
        if not os.path.exists(file_path):
            break
        counter += 1

    part_path = file_path + ".part"
    err_tmp = file_path + ".ffmpegerr"
    candidates = _build_encode_candidates(fmt_std, w_ext, h_ext, n_frames, ext, fps)

    # 调色走 GPU：泛光/柔焦的高斯是卷积运算，显卡比 CPU 快几十倍。
    # 编码档位本来就有 GPU 档，此前慢的正是这一段被固定建在 CPU 张量上。
    cuda_ok = torch.cuda.is_available()

    # 批次大小：按实时可用显存算，而不是写死一个字节预算——写死的 256MB 在
    # 4K 下只够 2 帧，显卡刚起步就收工。每帧峰值按 6 份 float32 估（输入 /
    # 输出 / 中间量），只取四成可用显存，别把生成模型的位置挤掉。
    per_frame = w_ext * h_ext * 3 * 4
    batch_max = 4
    if cuda_ok:
        try:
            free, _ = torch.cuda.mem_get_info()
            batch_max = max(1, min(16, int(free * 0.4) // max(1, per_frame * 6)))
        except Exception:
            pass

    last_err = ""
    ok = False
    used_label = ""
    frames_written = 0

    for idx, (label, vargs) in enumerate(candidates):
        proc = None
        try:
            if os.path.isfile(part_path):
                os.remove(part_path)

            cmd = [
                ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{w_ext}x{h_ext}", "-r", str(fps), "-i", "pipe:0",
                "-i", src_path, "-map", "0:v:0",
            ]
            if has_audio:
                # webm 只认 opus/vorbis，源音轨常是 aac，必须转码；其余容器直接 copy 无损
                if ext == "webm":
                    cmd += ["-map", "1:a:0", "-c:a", "libopus", "-b:a", "192k"]
                else:
                    cmd += ["-map", "1:a:0", "-c:a", "copy"]
            cmd += vargs + ["-f", out_fmt, part_path]

            with open(err_tmp, "w", encoding="utf-8", errors="replace") as err_f:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=err_f)

                def _tune_batch(arrs):
                    """一批已解码的 RGB uint8 帧 → torch 调色 → 转回 uint8。

                    GPU 可用时在显卡上调色（卷积快几十倍），显存不足等异常整批
                    退回 CPU 重算，保证能出片。这里故意不做 .cpu() 回传——回传
                    与写管道都交给写线程，主线程立刻去算下一批。
                    """
                    arr = np.stack(arrs)

                    def _run(on_gpu):
                        t = torch.from_numpy(arr)
                        if on_gpu:
                            # 先以 uint8 上传、到显卡上再转 float：4K 16 帧的
                            # 传输量从 1.6GB 降到 400MB，CPU 侧也省掉一次
                            # 1.6GB float32 的分配与写（实测省一半耗时）
                            t = t.cuda(non_blocking=True)
                        t = t.float().div_(255.0)
                        t = _apply_tuning(t, tune)
                        return t.mul(255).round_().clamp_(0, 255).to(torch.uint8)

                    u8 = None
                    if cuda_ok:
                        try:
                            u8 = _run(True)
                        except Exception as e:
                            print(f"[Banzhang] 调色导出 GPU 调色失败，回退 CPU: {e}")
                            try:
                                torch.cuda.empty_cache()
                            except Exception:
                                pass
                            u8 = None
                    if u8 is None:
                        u8 = _run(False)
                    return u8

                def _read_frames():
                    """后台解码：AUTO 让解码器自身多核，队列让它与调色、写盘重叠。"""
                    try:
                        with av.open(src_path) as c:
                            vs = c.streams.video[0]
                            vs.thread_type = "AUTO"
                            for frame in c.decode(vs):
                                # 裁剪到偶数尺寸（编码器要求），与节点内编码策略一致
                                frame_q.put(frame.to_ndarray(format="rgb24")[:h_ext, :w_ext])
                    except Exception as e:
                        frame_q.put(e)
                    finally:
                        frame_q.put(None)

                def _write_frames():
                    """后台写管道：uint8 回传与 stdin 阻塞都在这里，主线不等。"""
                    while True:
                        item = write_q.get()
                        if item is None:
                            break
                        try:
                            arr = item.cpu().numpy() if item.is_cuda else item.numpy()
                            proc.stdin.write(np.ascontiguousarray(arr).tobytes())
                        except Exception as e:
                            # 编码器提前退出（管道破裂 / 参数不被接受）。异常不能
                            # 让线程停在这里：队列一满主线程就永远卡在 put 上，
                            # 所以只记录、继续排空，交给主线去换下一档编码。
                            write_err.append(e)

                frames_written = 0
                frame_q = queue.Queue(maxsize=batch_max * 2)
                write_q = queue.Queue(maxsize=1)
                write_err = []
                reader = threading.Thread(target=_read_frames, daemon=True)
                writer = threading.Thread(target=_write_frames, daemon=True)
                reader.start()
                writer.start()
                try:
                    buf = []
                    while True:
                        item = frame_q.get()
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            raise item
                        buf.append(item)
                        if len(buf) >= batch_max:
                            write_q.put(_tune_batch(buf))
                            frames_written += len(buf)
                            _bake_progress(src_path, frames_written, n_frames)
                            buf = []
                    if buf:
                        write_q.put(_tune_batch(buf))
                        frames_written += len(buf)
                        _bake_progress(src_path, frames_written, n_frames)
                    write_q.put(None)
                    writer.join()
                    if write_err:
                        raise write_err[0]
                finally:
                    # 解码异常时也要放哨兵，否则写线程卡在 get() 上不收工
                    write_q.put(None)
                    reader.join(timeout=5)

                proc.stdin.close()
                ret = proc.wait()
                proc = None

            if ret == 0 and os.path.isfile(part_path) and os.path.getsize(part_path) > 0:
                ok = True
                used_label = label
                break

            tail = ""
            try:
                with open(err_tmp, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read().strip()[-300:]
            except Exception:
                pass
            last_err = f"[{label}] 退出码 {ret} {tail}".strip()
            print(f"[Banzhang] 调色导出编码失败 {last_err}")
        except Exception as e:
            last_err = f"[{label}] {e}"
            print(f"[Banzhang] 调色导出编码异常 {last_err}")
        finally:
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass

    if os.path.isfile(err_tmp):
        try:
            os.remove(err_tmp)
        except Exception:
            pass

    if not ok:
        if os.path.isfile(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
        return {"success": False, "msg": f"已尝试 {len(candidates)} 档编码参数均失败：{last_err}"}

    # --- 收尾：碎片 → 标准容器（重建 moov + faststart） ---
    if _ffmpeg_remux(part_path, file_path, ext, out_fmt):
        if os.path.isfile(part_path):
            try:
                os.remove(part_path)
            except Exception:
                pass
    else:
        os.replace(part_path, file_path)

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    print(f"[Banzhang] 调色导出完成 {file_path}（{frames_written} 帧 / {size_mb:.2f} MB / 档位 {used_label}）")

    return {
        "success": True,
        "path": file_path,
        "filename": file_name,
        "fps": fps,
        "frame_count": frames_written,
        "engine": used_label,
        "report": (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 文件名称 : {file_name}\n"
            f"📏 物理尺寸 : {w_ext} x {h_ext}\n"
            f"🎞️ 总帧数量 : {frames_written} 帧\n"
            f"📉 视频帧率 : {fps:g} FPS\n"
            f"💾 文件体积 : {size_mb:.2f} MB\n"
            f"📼 编码格式 : {ext.upper()}" + ("（含源音轨）" if has_audio else "") + "\n"
            + _tune_text(tune)
            + f"⚙️ 编码档位 : {used_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 输出路径 : {file_path}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        ),
    }


def _safe_name(s):
    """文件名安全化：Windows 文件名不允许 <>:"/\\|?* 与控制符，一律换成连字符。

    子图内的节点 unique_id 形如 1377:1264，直接拼进文件名时冒号会被 Windows
    当成 NTFS 备用数据流(ADS)分隔符——数据写进隐藏的 :1264_0001.mp4 流，
    真正落盘的是名为「前缀_N1377」的 0 字节空文件，且 os.path.exists 仍为真，
    直到 os.replace 才抛 WinError 87，报错点离真因很远，极难排查。
    用户手填的文件前缀同样可能含有这些字符，一并处理。
    """
    return "".join("-" if (c in '<>:"/\\|?*' or ord(c) < 32) else c for c in str(s)).strip()


def _strip_view_ds(workflow_data):
    """删除工作流里保存的画布视角(extra.ds)。

    视频合并嵌入元数据时会把当前画布视角一并写进视频；该视角常指着
    某个放大后的角落，拖回 ComfyUI 时只看到几个节点，其余全在画面外
    （被误认为"工作流错位"）。删掉它之后，ComfyUI 拖入视频时会自动
    "适应画布"，所有节点一次全部落入窗口（实测 39/39 节点可见）。
    """
    if not isinstance(workflow_data, dict):
        return
    extra = workflow_data.get("extra")
    if isinstance(extra, dict):
        extra.pop("ds", None)


# ==========================================
# 视频合并节点
# ==========================================
class banzhangVideoCombine:
    """
    班长: 图像序列转视频 (V3.8)
    将 ComfyUI IMAGE 批次转换为视频文件，支持音频叠加。
    """

    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    # === VIDEO 输入：解析上游文件路径（兼容字符串 & ComfyUI {filename,subfolder,type}） ===
    def _resolve_video_path(self, video_val):
        if not video_val:
            return ""
        if isinstance(video_val, dict):
            try:
                base = folder_paths.get_annotated_filepath(video_val)
            except Exception:
                base = ""
            if not base:
                base = video_val.get("filename", "") or ""
            # 若拿到的是相对/基础名，尝试在输出目录拼接回退
            for cand in (base, os.path.join(self.output_dir, os.path.basename(base))):
                if cand and os.path.isfile(cand):
                    return cand
            return base
        # ComfyUI VIDEO 对象（RIFE 插帧 / DLSS5 插帧等节点的输出）：取背后的成品文件路径
        if not isinstance(video_val, str):
            getter = getattr(video_val, "get_stream_source", None)
            if callable(getter):
                try:
                    info = getter()
                except Exception:
                    info = None
                if isinstance(info, (tuple, list)) and info:
                    path = str(info[0])
                    return path if os.path.exists(path) else ""
                if isinstance(info, str) and os.path.exists(info):
                    return info
            return ""
        s = str(video_val)
        if os.path.isfile(s):
            return s
        try:
            resolved = folder_paths.get_annotated_filepath(s)
            if resolved and os.path.isfile(resolved):
                return resolved
        except Exception:
            pass
        cand = os.path.join(self.output_dir, os.path.basename(s))
        return cand if os.path.isfile(cand) else ""

    # === 直接采用成品视频文件：保存到自定义路径（唯一位置）或输出目录，不做重新编码 ===
    def _adopt_existing(self, src_path, prefix, node_id, custom_path, prompt_data=None, extra_pnginfo_data=None, save_metadata=True):
        src_ext = os.path.splitext(src_path)[1].lstrip(".") or "mp4"
        # 自定义路径生效时为唯一保存位置，output 不再留文件（预览走 /banzhang_api/video_file 接口）
        if custom_path and custom_path.strip():
            save_dir = custom_path.strip()
            if not os.path.exists(save_dir):
                os.makedirs(save_dir, exist_ok=True)
        else:
            save_dir = self.output_dir

        save_prefix = f"{_safe_name(prefix)}_N{_safe_name(node_id)}"
        counter = 1
        while True:
            file_name = f"{save_prefix}_{counter:04}.{src_ext}"
            file_path = os.path.join(save_dir, file_name)
            if not os.path.exists(file_path):
                break
            try:
                with open(file_path, 'ab'):
                    pass
                counter += 1
            except PermissionError:
                counter += 1

        # 自定义路径生效时直接复制到目标位置（save_dir 已指向该处），不再向 output 留副本
        # 优先 ffmpeg remux 嵌入工作流元数据（拖入 ComfyUI 可还原工作流）；失败则回退普通复制
        embed_done = False
        if save_metadata and HAS_VIDEO and src_ext.lower() in ("mp4", "mov", "m4v", "mkv", "webm"):
            adopt_meta = {}
            if prompt_data is not None:
                adopt_meta["prompt"] = json.dumps(prompt_data)
            if isinstance(extra_pnginfo_data, dict) and extra_pnginfo_data.get("workflow"):
                _strip_view_ds(extra_pnginfo_data["workflow"])
                adopt_meta["workflow"] = json.dumps(extra_pnginfo_data["workflow"])
            if adopt_meta:
                tmp_path = file_path + ".embedtmp"
                if _embed_workflow_metadata(src_path, tmp_path, adopt_meta, src_ext.lower()):
                    os.replace(tmp_path, file_path)
                    embed_done = True
                    print(f"[Banzhang] 已嵌入工作流元数据: {file_path}")
                elif os.path.isfile(tmp_path):
                    os.remove(tmp_path)
        if not embed_done:
            shutil.copy2(src_path, file_path)

        # 尽力探测源视频信息（失败则显示未知）
        # fps / 帧数一并返回给前端：同步预览要按帧号寻址，靠前端从详情文本
        # 正则猜帧率会猜错（解析不到就默认 24），对齐基准必须是真值。
        width = height = n_frames = duration_sec = fps = None
        if HAS_AV:
            try:
                with av.open(src_path) as c:
                    vs = c.streams.video[0]
                    width, height = vs.width, vs.height
                    n_frames = vs.frames or None
                    duration_sec = (c.duration or 0) / 1_000_000.0
                    if vs.average_rate:
                        fps = float(vs.average_rate)
            except Exception:
                pass

        f_size = os.path.getsize(file_path) / (1024 * 1024)
        w_txt = f"{width}" if width else "未知"
        h_txt = f"{height}" if height else "未知"
        f_txt = f"{n_frames}" if n_frames else "未知"
        dur_txt = f"{int(duration_sec)}" if duration_sec else "未知"
        fps_txt = f"{fps:g}" if fps else "未知"

        detail_report = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 文件名称 : {file_name}\n"
            f"📏 物理尺寸 : {w_txt} x {h_txt}\n"
            f"🎞️ 总帧数量 : {f_txt} 帧\n"
            f"⏱️ 播放时长 : {dur_txt} 秒\n"
            f"📉 视频帧率 : {fps_txt} FPS\n"
            f"💾 文件体积 : {f_size:.2f} MB\n"
            f"📼 编码格式 : {src_ext.upper()} (成品)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 输出路径 : {file_path}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        if custom_path and custom_path.strip():
            abs_path = file_path
        else:
            abs_path = ""

        return {
            "ui": {
                "painter_output": [{"filename": file_name, "subfolder": "", "type": "output",
                                    "abs_path": abs_path, "src_path": file_path,
                                    "fps": fps, "frame_count": n_frames}],
                "detail_info": [detail_report]
            },
            "result": ()
        }

    # === 图片模式：逐张保存为 PNG（可选择嵌入工作流元数据），不走视频编码 ===
    def _save_images(self, images, prefix, node_id, custom_path, prompt_data, extra_pnginfo_data, save_metadata, audio=None):
        if not HAS_PIL:
            raise RuntimeError("缺少 Pillow，无法保存图片")

        if custom_path and custom_path.strip():
            save_dir = custom_path.strip()
            if not os.path.exists(save_dir):
                os.makedirs(save_dir, exist_ok=True)
        else:
            save_dir = self.output_dir

        save_prefix = f"{_safe_name(prefix)}_N{_safe_name(node_id)}"
        n_frames, h, w, c = images.shape

        metadata = None
        if save_metadata:
            metadata = PngInfo()
            if prompt_data is not None:
                metadata.add_text("prompt", json.dumps(prompt_data))
            if extra_pnginfo_data is not None:
                for key in extra_pnginfo_data:
                    metadata.add_text(key, json.dumps(extra_pnginfo_data[key]))

        saved = []
        counter = 1
        for i in range(n_frames):
            while True:
                file_name = f"{save_prefix}_{counter:04}.png"
                file_path = os.path.join(save_dir, file_name)
                if not os.path.exists(file_path):
                    break
                counter += 1

            img_np = np.ascontiguousarray((images[i].detach().cpu().numpy() * 255).astype(np.uint8))
            Image.fromarray(img_np).save(file_path, pnginfo=metadata, compress_level=4)
            saved.append((file_name, file_path))
            counter += 1

        total_mb = sum(os.path.getsize(p) for _, p in saved) / (1024 * 1024)
        if custom_path and custom_path.strip():
            abs_path = saved[0][1]
        else:
            abs_path = ""

        path_txt = "\n".join(p for _, p in saved) if len(saved) > 1 else saved[0][1]
        detail_report = (
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 文件名称 : {saved[0][0]}"
            + (f"\n🖼️ 图片数量 : {len(saved)} 张" if len(saved) > 1 else "") + "\n"
            f"📏 物理尺寸 : {w} x {h}\n"
            f"💾 文件体积 : {total_mb:.2f} MB\n"
            f"📄 编码格式 : PNG (图片)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ 输出路径 : {path_txt}" +
            ("\n⚠️ 图片模式不支持音频，音频已被忽略" if audio is not None else "") + "\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

        return {
            "ui": {
                "painter_output": [{"filename": n, "subfolder": "", "type": "output",
                                    "abs_path": abs_path} for n, _ in saved],
                "detail_info": [detail_report]
            },
            "result": (images, None, audio)
        }

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "⏱️ 帧率": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 240.0, "step": 0.01}),
                "📼 视频格式": ([
                    "MP4 (H264/通用)",
                    "MP4 (H265/高画质)",
                    "MOV (H264/网页兼容)",
                    "MOV (ProRes/专业剪辑)",
                    "MKV (H264/防损坏)",
                    "WEBM (网页/透明)",
                    "GIF (动图)",
                    "WEBP (动图)",
                ],),
                "🏷️ 文件前缀": ("STRING", {"default": "班长作品"}),
                "💾 保存元数据": ("BOOLEAN", {"default": True}),
                # 新增控件必须追加在末尾：ComfyUI 旧工作流按位置（而非名称）回填 widgets_values，
                # 插到中间会让旧工作流的帧率/格式/前缀整体错位一格
                "🔀 输出模式": (["自动", "强制视频", "强制图片"], {"default": "自动"}),
            },
            "optional": {
                "🖼️ 图像": ("IMAGE",),
                "🎬 视频文件": ("VIDEO",),
                "🎵 音频": ("AUDIO",),
                "📂 自定义保存路径": ("STRING", {"default": ""}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID"
            }
        }

    RETURN_TYPES = ("IMAGE", "VIDEO", "AUDIO")
    RETURN_NAMES = ("🖼️ 图像透传", "🎬 视频透传", "🎵 音频透传")
    OUTPUT_NODE = True
    CATEGORY = "👨‍🏫班长/视频处理"
    FUNCTION = "execute_combine"

    def execute_combine(self, **kwargs):
        detail_report = "waiting..."
        audio_warning = ""

        try:
            images = kwargs.get("🖼️ 图像")
            fps = kwargs.get("⏱️ 帧率", 24.0)
            fmt_str = kwargs.get("📼 视频格式", "MP4 (H264/通用)")
            prefix = kwargs.get("🏷️ 文件前缀", "班长作品")
            audio = kwargs.get("🎵 音频")
            custom_path = kwargs.get("📂 自定义保存路径", "")
            save_metadata = kwargs.get("💾 保存元数据", True)

            node_id = kwargs.get("unique_id", "000")
            prompt_data = kwargs.get("prompt", None)
            extra_pnginfo_data = kwargs.get("extra_pnginfo", None)

            # 输出帧率即基准帧率（倍速已移除）
            out_fps = float(fps)
            print(f"[Banzhang] 合并参数 → 帧率={out_fps:g}")

            # --- VIDEO 输入：直接采用上游成品文件，不再重新编码 ---
            video_src = kwargs.get("🎬 视频文件")
            src_path = self._resolve_video_path(video_src)
            if src_path:
                ret = self._adopt_existing(src_path, prefix, node_id, custom_path, prompt_data,
                                           extra_pnginfo_data, save_metadata)
                # 透传：图像原样 + 视频原样 + 音频原样（串联下一个合并节点可直接接「🎬 视频文件」）
                ret["result"] = ret["result"] + (images, video_src, audio)
                return ret

            # --- 图片也空：无任何输入 ---
            if images is None:
                raise RuntimeError("未接入 🖼️ 图像 或 🎬 视频文件，请至少连接一个输入")

            # --- 输出模式判定：图片模式跳过视频编码，直接存 PNG ---
            # 自动 = 只有单张图片且未接音频（接了音频说明就是要出视频）→ 存图片；其余走视频编码
            # 空串/非法值兜底为自动（老工作流补值场景）
            输出模式 = kwargs.get("🔀 输出模式") or "自动"
            if 输出模式 not in ("自动", "强制视频", "强制图片"):
                输出模式 = "自动"

            n_frames_img, _, _, _ = images.shape
            if 输出模式 == "强制图片" or (输出模式 == "自动" and n_frames_img == 1 and audio is None):
                return self._save_images(images, prefix, node_id, custom_path, prompt_data, extra_pnginfo_data, save_metadata, audio)

            ext_map = {"MP4": "mp4", "MOV": "mov", "MKV": "mkv", "WEBM": "webm", "GIF": "gif", "WEBP": "webp"}
            ext = next((v for k, v in ext_map.items() if k in fmt_str), "mp4")

            # --- 文件自增命名（自定义路径生效时为唯一保存位置，output 不再留副本） ---
            if custom_path and custom_path.strip():
                save_dir = custom_path.strip()
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir, exist_ok=True)
            else:
                save_dir = self.output_dir

            save_prefix = f"{_safe_name(prefix)}_N{_safe_name(node_id)}"
            counter = 1
            while True:
                file_name = f"{save_prefix}_{counter:04}.{ext}"
                file_path = os.path.join(save_dir, file_name)
                if not os.path.exists(file_path):
                    break
                try:
                    with open(file_path, 'ab'):
                        pass
                    counter += 1
                except PermissionError:
                    counter += 1

            sub_dir = ""

            n_frames, h, w, c = images.shape
            w_ext, h_ext = (w // 2) * 2, (h // 2) * 2

            if ProgressBar:
                pbar = ProgressBar(n_frames)
            else:
                pbar = None

            # --- 视频格式判定：动图走 Pillow，视频走 ffmpeg 子进程 / PyAV 回退 ---
            is_anim = ("GIF" in fmt_str or "WEBP" in fmt_str)

            # --- 元数据首帧保存（仅动图格式需要）---
            # mp4/mov/mkv/webm 已把工作流写进容器元数据，再存一份 PNG 是纯冗余；
            # GIF/WEBP 走 Pillow 编码，容器存不了元数据，这张首帧是唯一的工作流载体。
            if save_metadata and is_anim and n_frames > 0 and HAS_PIL:
                try:
                    first_frame = images[0:1].cpu().numpy()
                    first_frame_np = (first_frame[0] * 255).astype(np.uint8)
                    pil_img = Image.fromarray(first_frame_np)
                    metadata = PngInfo()
                    if prompt_data is not None:
                        metadata.add_text("prompt", json.dumps(prompt_data))
                    if extra_pnginfo_data is not None:
                        for key in extra_pnginfo_data:
                            metadata.add_text(key, json.dumps(extra_pnginfo_data[key]))
                    first_frame_name = f"{save_prefix}_{counter:04}_meta.png"
                    first_frame_path = os.path.join(save_dir, first_frame_name)
                    pil_img.save(first_frame_path, pnginfo=metadata, compress_level=4)
                except Exception as meta_e:
                    print(f"[Banzhang] metadata save failed: {meta_e}")

            # --- 准备音频（PCM 16 位，供 PyAV 编码使用） ---
            pcm, sr, ch = None, None, None
            if audio and not is_anim:
                # 注意：局部变量不能叫 wave，会遮蔽标准库 wave 模块，导致后面写临时 WAV 失败、音频静默丢失
                wave_np = audio['waveform'][0].detach().cpu().numpy().astype(np.float32)
                sr = int(audio['sample_rate'])
                ch = int(wave_np.shape[0])
                pcm = (np.clip(wave_np, -1.0, 1.0) * 32767.0).astype(np.int16)
                # 以画面时长为基准裁剪音频尾部：视频 15 秒、音频 30 秒时只取前 15 秒，
                # 避免 ffmpeg 按最长流输出，导致画面播完后尾段定格（音频却还在响）
                if out_fps > 0 and n_frames > 0:
                    视频采样数 = int(round(n_frames / out_fps * sr))
                    if 视频采样数 < pcm.shape[1]:
                        pcm = pcm[:, :视频采样数]
                        audio_warning = (f"\n🎵 音频(源 {wave_np.shape[1] / sr:.1f}s)已按画面 "
                                         f"{n_frames / out_fps:.1f}s 截断尾部")
                        print(f"[Banzhang] {audio_warning.strip()}")
            elif audio and is_anim:
                audio_warning = "\n⚠️ GIF/WEBP 动图格式不支持音频，音频已被忽略"
                print(f"[Banzhang] {audio_warning.strip()}")

            frames_written = 0
            error_occurred = False
            used_ffmpeg = False
            used_label = ""

            # --- 工作流元数据（写入视频容器，拖入 ComfyUI 画布可还原工作流）---
            # 键名与 ComfyUI 前端 ISOBMFF/WebM 解析器约定一致：prompt（API 格式）+ workflow（UI 格式）
            embed_meta = {}
            if save_metadata:
                if prompt_data is not None:
                    embed_meta["prompt"] = json.dumps(prompt_data)
                if isinstance(extra_pnginfo_data, dict) and extra_pnginfo_data.get("workflow"):
                    _strip_view_ds(extra_pnginfo_data["workflow"])
                    embed_meta["workflow"] = json.dumps(extra_pnginfo_data["workflow"])

            if not is_anim:
                # --- 编码前腾显存：把上游残留的 CUDA 缓存块还给驱动，给帧转换留余量 ---
                try:
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass

                # --- 编码前健康检查：磁盘空间 + 帧内存日志（长视频最容易在这里翻车）---
                try:
                    frame_mem_gb = images.element_size() * images.nelement() / (1024 ** 3)
                    print(f"[Banzhang] 待编码 {n_frames} 帧 {w_ext}x{h_ext}，张量约 {frame_mem_gb:.2f} GB")
                    usage = shutil.disk_usage(save_dir)
                    need_bytes = int(w_ext * h_ext * 3 * n_frames * 0.3) + 256 * 1024 * 1024
                    if usage.free < need_bytes:
                        raise RuntimeError(
                            f"磁盘空间不足：预计需要 {need_bytes / (1024 ** 3):.1f} GB，"
                            f"仅剩 {usage.free / (1024 ** 3):.1f} GB，请先清理或改保存路径"
                        )
                except RuntimeError:
                    raise
                except Exception as chk_e:
                    print(f"[Banzhang] 磁盘预检跳过: {chk_e}")

                if HAS_VIDEO:
                    # ═══ ffmpeg 子进程编码（进程隔离：编码器原生崩溃只失败本节点，不再拖垮 ComfyUI 主进程） ═══
                    # 音频：先落临时 WAV（标准库 wave，无第三方依赖），作为 ffmpeg 第二输入
                    wav_tmp, audio_args = None, []
                    if pcm is not None:
                        try:
                            ch_use = 2 if ch >= 2 else 1
                            wav_tmp = file_path + ".audiotmp.wav"
                            with wave.open(wav_tmp, "wb") as wf:
                                wf.setnchannels(ch_use)
                                wf.setsampwidth(2)
                                wf.setframerate(sr)
                                wf.writeframes(np.ascontiguousarray(pcm[:ch_use].T).tobytes())
                            acodec = "pcm_s16le" if "ProRes" in fmt_str else ("libopus" if "WEBM" in fmt_str else "aac")
                            audio_args = ["-i", wav_tmp, "-c:a", acodec, "-b:a", "192k"]
                            if acodec == "libopus":
                                audio_args += ["-ar", "48000"]
                        except Exception as wav_e:
                            audio_warning = f"\n⚠️ 音频写入临时文件失败，本段无音频: {wav_e}"
                            print(f"[Banzhang] {audio_warning.strip()}")
                            if wav_tmp and os.path.exists(wav_tmp):
                                os.remove(wav_tmp)
                            wav_tmp = None

                    # stderr 落临时文件（避免管道缓冲写满导致与 stdin 喂帧互相死锁）
                    part_path = file_path + ".part"
                    err_tmp = file_path + ".ffmpegerr"
                    out_fmt = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "webm": "webm"}.get(ext)
                    candidates = _build_encode_candidates(fmt_str, w_ext, h_ext, n_frames, ext, out_fps)
                    ok, enc_err, used_label = _ffmpeg_encode_frames(
                        images, n_frames, w_ext, h_ext, out_fps,
                        part_path, candidates, audio_args, err_tmp, pbar, out_fmt
                    )
                    for tmp_f in (err_tmp, wav_tmp):
                        if tmp_f and os.path.exists(tmp_f):
                            try:
                                os.remove(tmp_f)
                            except Exception:
                                pass

                    if not ok:
                        # 全部档位失败：保留已编码的碎片文件作为抢救成品，绝不删已生成内容
                        # 注：碎片 mp4 末尾分片可能不完整，先尝试容错 remux，失败则原样保留
                        part_name, part_saved = "", ""
                        try:
                            if os.path.isfile(part_path) and os.path.getsize(part_path) > 0:
                                part_name = f"{save_prefix}_{counter:04}_PART.{ext}"
                                part_final = os.path.join(save_dir, part_name)
                                if _ffmpeg_remux(part_path, part_final, ext, out_fmt):
                                    part_saved = part_final
                                else:
                                    os.replace(part_path, part_final)
                                    part_saved = part_final
                        except Exception as keep_e:
                            print(f"[Banzhang] 保留部分成品失败: {keep_e}")
                            if os.path.isfile(part_path):
                                part_saved, part_name = part_path, os.path.basename(part_path)
                        detail_report = (
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"❌ 视频编码失败（已自动尝试 {len(candidates)} 档参数）\n"
                            f"{enc_err}\n"
                            + (f"🛡️ 已保留部分成品（末尾片段可能不完整）：\n{part_saved}\n" if part_saved else "")
                            + f"━━━━━━━━━━━━━━━━━━━━━━"
                        )
                        print(f"[Banzhang] {detail_report}")
                        return {
                            "ui": {
                                "painter_output": ([
                                    {"filename": part_name, "subfolder": sub_dir, "type": "output",
                                     "abs_path": part_saved if (custom_path and custom_path.strip()) else "",
                                     "src_path": part_saved}
                                ] if part_saved else []),
                                "detail_info": [detail_report]
                            },
                            "result": (images, None, None)
                        }

                    # 收尾：碎片文件 → 标准容器（重建 moov + faststart），并按需嵌入工作流元数据
                    try:
                        if embed_meta and ext in ("mp4", "mov", "mkv", "webm"):
                            tmp_path = file_path + ".embedtmp"
                            if _embed_workflow_metadata(part_path, tmp_path, embed_meta, ext):
                                os.replace(tmp_path, file_path)
                                print(f"[Banzhang] 已嵌入工作流元数据: {file_path}")
                            else:
                                if os.path.isfile(tmp_path):
                                    os.remove(tmp_path)
                                if not _ffmpeg_remux(part_path, file_path, ext, out_fmt):
                                    os.replace(part_path, file_path)
                        else:
                            if not _ffmpeg_remux(part_path, file_path, ext, out_fmt):
                                os.replace(part_path, file_path)
                    finally:
                        if os.path.isfile(part_path):
                            try:
                                os.remove(part_path)
                            except Exception:
                                pass
                    used_ffmpeg = True
                else:
                    used_ffmpeg = False
                    # ═══ PyAV 编码回退（无 ffmpeg 可执行文件时；主进程内编码，非首选路径） ═══
                    if not HAS_AV:
                        raise RuntimeError("缺少 ffmpeg(imageio_ffmpeg) 与 PyAV(av)，无法编码视频")
                    container = None
                    try:
                        container = av.open(file_path, mode="w")

                        if "ProRes" in fmt_str:
                            vcodec = "prores"
                        elif "H265" in fmt_str:
                            vcodec = "libx265"
                        elif "WEBM" in fmt_str:
                            vcodec = "libvpx-vp9"
                        else:
                            vcodec = "libx264"

                        vstream = container.add_stream(vcodec, rate=fractions.Fraction(out_fps).limit_denominator(1000))
                        vstream.width = w_ext
                        vstream.height = h_ext
                        vstream.pix_fmt = "yuv422p10le" if "ProRes" in fmt_str else "yuv420p"

                        # 画质对齐：H264 CRF8 / H265 CRF4 / VP9 无损，preset slower
                        if vcodec == "libx264":
                            vstream.options["crf"] = "8"
                            vstream.options["preset"] = "slower"
                        elif vcodec == "libx265":
                            vstream.options["crf"] = "4"
                            vstream.options["preset"] = "slower"
                            vstream.options["aq-mode"] = "3"
                        elif vcodec == "libvpx-vp9":
                            vstream.options["lossless"] = "1"

                        # 音频流（通道数收敛到 1/2，s16p 平面布局逐通道送入）
                        astream = None
                        if pcm is not None:
                            ch_use = 2 if ch >= 2 else 1
                            acodec = "pcm_s16le" if "ProRes" in fmt_str else ("libopus" if "WEBM" in fmt_str else "aac")
                            astream = container.add_stream(acodec, rate=sr)
                            astream.layout = "stereo" if ch_use == 2 else "mono"

                        # 逐帧编码（裁剪到偶数尺寸 + 色彩转换到流格式，满足 yuv420p 校验）
                        for i in range(n_frames):
                            frame_np = images[i].detach().cpu().numpy()
                            # 上游 tensor 可能非 C-contiguous，PyAV from_ndarray 强制要求连续，此处统一保证
                            frame_rgb = np.ascontiguousarray((frame_np[:h_ext, :w_ext] * 255).astype(np.uint8))
                            vframe = av.VideoFrame.from_ndarray(frame_rgb, format="rgb24")
                            vframe = vframe.reformat(width=w_ext, height=h_ext, format=vstream.pix_fmt)
                            for packet in vstream.encode(vframe):
                                container.mux(packet)
                            frames_written += 1
                            if pbar:
                                pbar.update(1)

                        # 冲刷视频流尾帧
                        for packet in vstream.encode():
                            container.mux(packet)

                        # 编码音频（s16p 平面格式，shape=通道×采样；残差/采样率交给 PyAV 内部重采样）
                        if astream is not None:
                            ch_use = 2 if ch >= 2 else 1
                            # waveform 经上游变换后可能非 C-contiguous，from_ndarray 强制要求连续
                            aframe = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm[:ch_use]), format="s16p", layout=astream.layout)
                            aframe.sample_rate = sr
                            for packet in astream.encode(aframe):
                                container.mux(packet)
                            for packet in astream.encode():
                                container.mux(packet)

                    except Exception as enc_e:
                        error_occurred = True
                        import traceback
                        traceback.print_exc()
                        detail_report = f"PyAV 编码失败: {enc_e}"
                    finally:
                        if container is not None:
                            try:
                                container.close()
                            except Exception:
                                pass

                    if error_occurred:
                        try:
                            if os.path.exists(file_path):
                                os.remove(file_path)
                        except Exception:
                            pass
                        return {
                            "ui": {"painter_output": [], "detail_info": [detail_report]},
                            "result": (images, None, None)
                        }
            else:
                # ═══════════ GIF / WEBP：Pillow 直接编码动图，无需 ffmpeg ═══════════
                if not HAS_PIL:
                    raise RuntimeError("缺少 Pillow，无法编码 GIF/WEBP 动图")
                ms = max(1, int(1000 / out_fps))
                frames_pil = []
                for i in range(n_frames):
                    frame_np = images[i].detach().cpu().numpy()
                    frame_rgb = np.ascontiguousarray((frame_np[:h_ext, :w_ext] * 255).astype(np.uint8))
                    frames_pil.append(Image.fromarray(frame_rgb, mode="RGB"))
                if not frames_pil:
                    raise RuntimeError("没有可编码的帧")
                frames_pil[0].save(
                    file_path, save_all=True, append_images=frames_pil[1:],
                    duration=ms, loop=0, optimize=False,
                )
                frames_written = n_frames
                if pbar:
                    pbar.update(n_frames)

            # --- 嵌入工作流元数据（ffmpeg 无损 remux；拖入 ComfyUI 画布可还原工作流）---
            # ffmpeg 子进程路径已在收尾阶段直接嵌入，此处仅处理 PyAV 回退 / GIF 之外的场景
            if embed_meta and not used_ffmpeg and HAS_VIDEO and ext in ("mp4", "mov", "mkv", "webm"):
                tmp_path = file_path + ".embedtmp"
                if _embed_workflow_metadata(file_path, tmp_path, embed_meta, ext):
                    os.replace(tmp_path, file_path)
                    print(f"[Banzhang] 已嵌入工作流元数据: {file_path}")
                elif os.path.isfile(tmp_path):
                    os.remove(tmp_path)

            # --- 生成详情报告 ---
            f_size = os.path.getsize(file_path) / (1024 * 1024)
            duration_sec = n_frames / out_fps
            engine_txt = f"⚙️ 编码档位 : {used_label}\n" if used_label else ""
            detail_report = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📂 文件名称 : {file_name}\n"
                f"📏 物理尺寸 : {w_ext} x {h_ext}\n"
                f"🎞️ 总帧数量 : {n_frames} 帧\n"
                f"⏱️ 播放时长 : {int(duration_sec)} 秒\n"
                f"📉 视频帧率 : {out_fps:g} FPS\n"
                f"💾 文件体积 : {f_size:.2f} MB\n"
                f"📼 编码格式 : {fmt_str.split(' ')[0]}\n"
                f"{engine_txt}"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ 输出路径 : {file_path}{audio_warning}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )

            if custom_path and custom_path.strip():
                abs_path = file_path
            else:
                abs_path = ""

            return {
                "ui": {
                "painter_output": [{"filename": file_name, "subfolder": sub_dir, "type": "output",
                                    "abs_path": abs_path, "src_path": file_path,
                                    "fps": float(out_fps), "frame_count": n_frames}],
                "detail_info": [detail_report]
                },
                "result": (images, video_src, audio)
            }

        except Exception as e:
            import traceback
            traceback.print_exc()
            # 异常也返回 ui，让详情面板与前端能显示失败原因（而不是静默保留旧预览）
            error_detail = (
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"❌ 执行失败 : {e}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            return {
                "ui": {
                    "painter_output": [],
                    "detail_info": [error_detail],
                },
                "result": (kwargs.get("🖼️ 图像"), kwargs.get("🎬 视频文件"), kwargs.get("🎵 音频"))
            }


# ==========================================
# 音乐播放器节点
# ==========================================
class MusicPlayerWithLyricsNode:
    @classmethod
    def INPUT_TYPES(cls):
        input_dir = folder_paths.get_input_directory()
        audio_files = []
        if os.path.exists(input_dir):
            audio_files = [f for f in os.listdir(input_dir) if f.lower().endswith(
                ('.mp3', '.wav', '.flac', '.m4a', '.ogg', '.opus', '.aac', '.aif', '.aiff', '.webm'))]
        if not audio_files:
            audio_files = ["no audio file"]

        return {
            "required": {
                "📂 音乐文件": (sorted(audio_files),),
                "🌈 频谱样式": (["镜像频谱", "丝滑波形", "环形扩散", "像素点阵"], {"default": "镜像频谱"}),
                "▶️ 自动播放": ("BOOLEAN", {"default": True}),
                "📄 保存文件名": ("STRING", {"default": "banzhang_audio"}),
                "🏷️ 保存格式": (["mp3", "wav", "flac", "ogg"], {"default": "mp3"}),
                "📂 保存目录": ("STRING", {"default": "", "placeholder": "为空则自动保存到 output/audio"}),
                "💾 保存到输出目录": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "🎵 音频": ("AUDIO",),
                "📝 歌词": ("STRING", {"forceInput": True}),
            }
        }

    RETURN_TYPES = ("AUDIO",)
    RETURN_NAMES = ("🎵 音频(透传)",)
    FUNCTION = "execute_play"
    CATEGORY = "👨‍🏫班长/音频处理"
    OUTPUT_NODE = True

    def execute_play(self, **kwargs):
        audio_input = kwargs.get("🎵 音频")
        file_component = kwargs.get("📂 音乐文件")
        lyrics = kwargs.get("📝 歌词")
        save_prefix = kwargs.get("📄 保存文件名", "audio")
        save_format = kwargs.get("🏷️ 保存格式", "mp3")
        save_path_str = kwargs.get("📂 保存目录", "").strip()
        # 方案 B：试听与导出解耦，默认试听不落盘，避免 output/audio 垃圾堆积
        save_enabled = bool(kwargs.get("💾 保存到输出目录", False))

        result_ui = {"audio": []}
        result_audio = None
        from_input = False

        if audio_input is not None:
            result_audio = audio_input
            from_input = True
        elif file_component and file_component != "no audio file":
            path = folder_paths.get_annotated_filepath(file_component)
            if path and os.path.exists(path):
                try:
                    waveform, sr = load_audio_file(path)
                    if waveform.dim() == 2:
                        waveform = waveform.unsqueeze(0)
                    result_audio = {"waveform": waveform, "sample_rate": sr}
                    result_ui["audio"].append({"filename": file_component, "subfolder": "", "type": "input"})
                    # 自动匹配同目录同名 .lrc 歌词（外部传入歌词优先）
                    if not lyrics:
                        lrc_candidate = os.path.splitext(path)[0] + ".lrc"
                        if os.path.exists(lrc_candidate):
                            try:
                                with open(lrc_candidate, "r", encoding="utf-8", errors="ignore") as lf:
                                    lyrics = lf.read()
                                print(f"[Banzhang] 已自动匹配同名歌词: {os.path.basename(lrc_candidate)}")
                            except Exception:
                                pass
                except Exception as e:
                    print(f"[Banzhang] 音乐文件解码失败，已降级为静音: {e}")

        if result_audio is None:
            # 无可用音频时给 1 秒静音，避免播放器点播放毫无反馈
            result_audio = {"waveform": torch.zeros((1, 1, 44100)), "sample_rate": 44100}

        # 预览：仅当音频来自管线输入时需要生成临时预览文件（按所选保存格式，做到所见即所得）
        if from_input:
            random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
            preview_ext = save_format if save_format in ("wav", "mp3", "ogg", "flac") else "wav"
            preview_name = f"preview_{random_suffix}.{preview_ext}"
            temp_dir = folder_paths.get_temp_directory()
            if temp_dir:
                preview_path = os.path.join(temp_dir, preview_name)
                if not save_audio_robust(preview_path, result_audio["waveform"], result_audio["sample_rate"], format=preview_ext):
                    # 回退到 wav 预览，保证播放器始终可播
                    preview_name = f"preview_{random_suffix}.wav"
                    preview_path = os.path.join(temp_dir, preview_name)
                    save_audio_robust(preview_path, result_audio["waveform"], result_audio["sample_rate"], format="wav")
                result_ui["audio"].append({"filename": preview_name, "subfolder": "", "type": "temp"})

        # 保存到输出目录（仅在勾选「保存到输出目录」时执行，试听只走 temp 预览）
        if save_enabled:
            try:
                if not save_path_str:
                    final_output_dir = os.path.join(folder_paths.get_output_directory(), "audio")
                else:
                    final_output_dir = save_path_str if os.path.isabs(save_path_str) else os.path.join(folder_paths.get_output_directory(), save_path_str)

                os.makedirs(final_output_dir, exist_ok=True)
                filename = f"{save_prefix}_{''.join(random.choices(string.digits, k=4))}.{save_format}"
                full_path = os.path.join(final_output_dir, filename)
                ok = save_audio_robust(full_path, result_audio["waveform"], result_audio["sample_rate"], format=save_format)
                if ok:
                    result_ui["status"] = f"✅ 已保存: {os.path.basename(full_path)}"
                else:
                    result_ui["status"] = f"⚠️ 保存失败，请检查解码与格式: {filename}"
            except Exception as e:
                print(f"[Banzhang] auto save failed: {e}")
                result_ui["status"] = f"❌ 保存失败: {e}"
        else:
            result_ui["status"] = "🎧 试听模式：未写入 output（勾选「保存到输出目录」可导出）"

        # 回传音频元信息，供前端播放器展示（时长 / 采样率 / 声道）
        try:
            _w = result_audio["waveform"]
            result_ui["meta"] = [{
                "duration": round(float(_w.shape[-1]) / float(result_audio["sample_rate"]), 3),
                "sample_rate": int(result_audio["sample_rate"]),
                "channels": int(_w.shape[1]) if _w.dim() == 3 else 1,
            }]
        except Exception as e:
            print(f"[Banzhang] meta report failed: {e}")

        if lyrics:
            result_ui["lyrics"] = [lyrics]
        return {"ui": result_ui, "result": (result_audio,)}


class BanZhang_LyricsInput:
    """📝 手动输入歌词：可直接手输，也可接入上游文本。
    不固定（跟随）时透传传入文本并回填到节点编辑框；节点上点「📌 固定」后
    布尔置真，框内内容定格为本地副本，可自由编辑，运行/刷新都不再被上游覆盖；
    再点「🔓 取消固定」恢复跟随。"""

    MODE_PIN_CN = "📌 固定编辑"   # 旧版 combo 存档（中文枚举）兼容用

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 本地文本框沿用旧键名且排在首位：旧工作流 widgets_values 按位恢复不丢内容
                "📝 歌词文本": ("STRING", {"multiline": True, "default": ""}),
                # 布尔承载固定状态（前端隐藏勾选框，由一个 toggle 按钮翻转）
                "pinned": ("BOOLEAN", {"default": False,
                               "tooltip": "True=固定本地编辑（运行不覆盖）；False=跟随上游传入。节点上用按钮切换"}),
            },
            "optional": {
                "text_in": ("STRING", {"forceInput": True,
                               "tooltip": "上游文本（如「加载歌词文件」、LLM 输出）。未固定时输出并回填到编辑框；点「固定」后定格为本地副本"}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("lyrics",)
    FUNCTION = "process"
    CATEGORY = "👨‍🏫班长/音频处理"
    # 标记为输出节点：右键「对所选输出节点进行排队」可单独执行（只跑上游文本来源），
    # 便于在节点上完成「传入 → 回填 → 固定」，不必连带播放器等下游重跑；STRING 输出口照常透传。
    OUTPUT_NODE = True

    @staticmethod
    def _normalize_pinned(raw):
        """固定状态归一化：兼容旧版 combo 留下的中文枚举字符串。"""
        if isinstance(raw, str):
            return raw == BanZhang_LyricsInput.MODE_PIN_CN
        return bool(raw)

    def process(self, **kwargs):
        local = kwargs.get("📝 歌词文本", "") or ""
        pinned = self._normalize_pinned(kwargs.get("pinned", False))
        # 接线与否看键是否存在；空串是合法传入值(上游字段为空)，必须能清空本地旧内容
        connected = "text_in" in kwargs
        incoming = kwargs.get("text_in", None)
        incoming_text = "" if incoming is None else str(incoming)

        if not pinned and connected:
            out_text = incoming_text
        else:
            out_text = local

        result_ui = {}
        if not pinned and connected:
            # 跟随态始终回填(含空串→前端清空编辑框)
            result_ui["incoming_text"] = [incoming_text]
        return {"ui": result_ui, "result": (out_text.strip(),)}


class BanZhang_LoadLyricsFile:
    @classmethod
    def INPUT_TYPES(cls):
        dir_path = folder_paths.get_input_directory()
        files = [f for f in os.listdir(dir_path) if f.lower().endswith(('.lrc', '.txt'))] if os.path.exists(dir_path) else []
        if not files:
            files = ["no lyrics file"]
        return {"required": {"📂 歌词文件": (sorted(files),)}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "load"
    CATEGORY = "👨‍🏫班长/音频处理"

    def load(self, **kwargs):
        file_name = kwargs.get("📂 歌词文件")
        if not file_name or file_name == "no lyrics file":
            return ("",)
        path = os.path.join(folder_paths.get_input_directory(), file_name)
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                return (f.read(),)
        except Exception as e:
            print(f"[Banzhang] 歌词文件读取失败: {e}")
            return ("",)


# ==========================================
# 图像对比节点
# ==========================================
class Banzhang_ImageCompare:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image_a": ("IMAGE",),
                "image_b": ("IMAGE",),
                "mode": (["hstack", "vstack", "diff"], {"default": "hstack"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("result",)
    FUNCTION = "compare_images"
    CATEGORY = "👨‍🏫班长/图像处理"

    def compare_images(self, image_a, image_b, mode):
        if image_a is None or image_b is None:
            return (None,)

        if len(image_a.shape) == 3:
            image_a = image_a.unsqueeze(0)
        if len(image_b.shape) == 3:
            image_b = image_b.unsqueeze(0)

        if image_a.shape[1:] != image_b.shape[1:]:
            image_b = torch.nn.functional.interpolate(
                image_b.permute(0, 3, 1, 2),
                size=(image_a.shape[1], image_a.shape[2]),
                mode='bilinear',
                align_corners=False,
            ).permute(0, 2, 3, 1)

        if mode == "hstack":
            result = torch.cat([image_a, image_b], dim=2)
        elif mode == "vstack":
            result = torch.cat([image_a, image_b], dim=1)
        else:
            result = torch.abs(image_a - image_b)

        return (result,)


# ==========================================
# 注册节点
# ==========================================
NODE_CLASS_MAPPINGS = {
    "Banzhang_ImageCompare": Banzhang_ImageCompare,
    "banzhangVideoCombine": banzhangVideoCombine,
    "BanZhang_MusicPlayerWithLyrics": MusicPlayerWithLyricsNode,
    "BanZhang_LyricsInput": BanZhang_LyricsInput,
    "BanZhang_LoadLyricsFile": BanZhang_LoadLyricsFile,
}

WEB_DIRECTORY = "./js"

NODE_DISPLAY_NAME_MAPPINGS = {
    "Banzhang_ImageCompare": "🔍 图像对比器",
    "banzhangVideoCombine": "🎥 视频合并",
    "BanZhang_MusicPlayerWithLyrics": "🎵 班长音乐播放器",
    "BanZhang_LyricsInput": "📝 手动输入歌词",
    "BanZhang_LoadLyricsFile": "📂 加载歌词文件",
}
