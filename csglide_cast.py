"""H3 Studio — one node covering both MiniMax H3 model families.

  mode "fl2va"  -> first / last keyframes      (needs minimax_h3_fl2va_* weights)
  mode "ref2va" -> 9 images / 3 videos / 3 audio (needs minimax_h3_ref2va_* weights)

Everything the UI holds lives in the single `h3_data` JSON widget. Reference tags
are written in the prompt as @图片1 / @视频1 / @音频1 and transcribed here to
the real <Picture i> / <Video k> / <Audio j> presentation, renumbered against the
slots that are actually filled.
"""

import json
import math
import os
import re

import numpy as np
from collections import OrderedDict, deque
import torch
from PIL import Image

import nodes
import comfy.model_management
import comfy.nested_tensor
import comfy.utils
import folder_paths
import node_helpers

# 素材定位 / 参考图读取 / 媒体架槽位合并：与 Qwen 编辑台共用同一份口径
from .csglide_assets import (
    resolve_asset as _resolve_asset,
    load_image as _load_image,
    merge_rack_refs as _merge_rack_refs,
    mark_hd_upgraded as _mark_hd_upgraded,
)

# 语义桥：适配器加载与条件混合（内联模块）。默认适配器名在此单一定义。
from . import csglide_bridge

# 潜空间续写（cont.mode == "latent"）：复用 _h3mc（Motion Context）的算术，
# 绝不复制实现——H3 布局一改只需要同步 _h3mc/ 一处。
try:
    from ._h3mc.nodes import (
        MiniMaxH3MotionContext as _MC_Node,
        MiniMaxH3MotionContextLoadLatent as _MC_Load,
        VIDEO_RUN_GRID as _MC_RUN_GRID,
        _streams_from_latent as _mc_streams,
        _CHAIN_SLOT_FILE as _MC_SLOT_FILE,
        _under_output as _mc_under_output,
    )
except Exception:
    _MC_Node = None
    _MC_Load = None
    _MC_RUN_GRID = (124, 107, 90, 73, 56, 39, 22, 5, 1)
    _mc_streams = None
    _MC_SLOT_FILE = None
    _mc_under_output = None

try:
    import av
    from av.video.reformatter import Interpolation as _AV_INTERP
except Exception:  # pragma: no cover
    av = None
    _AV_INTERP = None

# VIDEO 成品对象（「整片」输出口）：用 comfy_api 的 VideoFromFile 包一条磁盘
# 路径，下游视频合并节点 get_stream_source() 即可拿到，不重新编码。
try:
    from comfy_api.latest import InputImpl as _InputImpl
except Exception:  # pragma: no cover
    _InputImpl = None


def _video_from_file(path):
    """把磁盘上的成片路径包成 VIDEO 对象；InputImpl 不可用或路径空时返回 None。"""
    if not path or not os.path.isfile(path) or _InputImpl is None:
        return None
    try:
        return _InputImpl.VideoFromFile(path)
    except Exception:
        return None


# --------------------------------------------------------------------------
# H3 constants (mirrors comfy_extras/nodes_minimax_h3.py)
# --------------------------------------------------------------------------

CANVAS_MULTIPLE = 32
BASE_SHORT_EDGE = 768
MAX_PIXELS = 768 * 1344
REF_IMAGE_MAX_LONG_EDGE = 4096    # 图片参考长边上限（视频参考不再设上限）
REF_MIN_LONG_EDGE = 32            # 参考长边下限（与画布 32 对齐一致）


def _fit_long(value):
    """向下对齐到 32 的倍数（下限 32）。

    向下而不是四舍五入：源素材比档位小时不该被放大，round 会把 1005 抬到 1008。
    """
    return max(CANVAS_MULTIPLE, (int(value) // CANVAS_MULTIPLE) * CANVAS_MULTIPLE)


def _ref_size(value, cap, fallback):
    """把参考尺寸归一为 "match" 或一个整数；cap=None 时不设上限（仅保 32 下限）。

    前端已把「下拉档位」改成「数字输入 + 跟随画布」，这里同时兼容
    旧档位字符串（512/640/.../max）与纯数字两种来源，老工程不会失效。
    """
    text = "" if value is None else str(value).strip().lower()
    if text == "match":
        return "match"
    if text == "max":
        return int(cap) if cap is not None else REF_IMAGE_MAX_LONG_EDGE
    try:
        n = int(float(text))
    except (TypeError, ValueError):
        return fallback
    n = max(REF_MIN_LONG_EDGE, n)
    if cap is not None:
        n = min(int(cap), n)
    return n
FPS = 24
AUDIO_LATENT_FPS = 40

MIN_REF_FRAMES = 5          # H3 rejects a reference video shorter than this
MAX_IMAGES = 9              # 面板本地槽位（兜底用）；媒体架槽位数量动态
MAX_VIDEOS = 3
MAX_AUDIOS = 3

# 参考视频的采样帧率：24 = 不抽帧；往低调（12/8）在同一时间跨度里取更少的帧、
# 省显存，代价是参考锚定精度下降。前端 REF_FPS_MIN/REF_FPS_MAX 的镜像。
REF_FPS_MIN = 4
REF_FPS_MAX = 24


def _clamp_ref_fps(v):
    """参考视频采样帧率，非法/缺省一律 24。

    缺字段取 24 不只是「默认值」：它等于这个设置在接入之前后端一直用的值，
    所以老工程打开后逐帧画面不变，只有显式调低过档位的工程才会变。
    """
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return REF_FPS_MAX
    return max(REF_FPS_MIN, min(REF_FPS_MAX, n))


def _ref_hd_scale(v):
    """「放大重绘」（二采/三采共用）条件的参考档位倍数；非法值一律 1.0。

    1.0 表示跟随主档位——它同时是接入前的既有行为：不重编码、不额外计算。
    小于 1 也按 1 处理：档位只能往上加清晰度，往下不是这个开关的用法。
    """
    try:
        m = float(v)
    except (TypeError, ValueError):
        return 1.0
    return max(1.0, m)


def _bridge_alpha(v):
    """语义桥强度，非法/越界一律 0（= 关闭，零开销）。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _bridge_mode(v):
    """语义桥作用范围：'text' = 掩码版（只动文本 token），其余一律 'full'。

    掩码版已从「仅尾部提示词」升级为「仅文本（按 minimax_token_tags）」——
    旧尾部掩码会把提示词里出现的 <Picture i> 引用与头部同名锚点切到两个子空间、
    破坏图文对齐；按 tag 切则锚点与提示词同走一个变换。字段名保留 binary 语义，
    方便前端做成「全量/掩码」二选一。
    """
    return "text" if str(v).strip().lower() == "text" else "full"


def align_frame_count(n):
    n = max(5, int(n))
    while n % 17 != 5:
        n += 1
    return n


def video_latent_t(frame_count):
    return 2 if frame_count <= 5 else ((frame_count - 5) // 17) * 5 + 2


def temporal_shape(length):
    frame_count = align_frame_count(length)
    duration = frame_count / FPS
    return frame_count, video_latent_t(frame_count), round(duration * AUDIO_LATENT_FPS)


def adapt_canvas(width, height):
    """768-short-edge canvas with a 768*1344 area cap, per-axis round to 32."""
    ratio = width / height
    if ratio >= 1.0:
        nom_w, nom_h = BASE_SHORT_EDGE * ratio, BASE_SHORT_EDGE
    else:
        nom_w, nom_h = BASE_SHORT_EDGE, BASE_SHORT_EDGE / ratio
    if nom_w * nom_h > MAX_PIXELS:
        s = math.sqrt(MAX_PIXELS / (nom_w * nom_h))
        nom_w, nom_h = nom_w * s, nom_h * s
    return (max(CANVAS_MULTIPLE, round(nom_w / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            max(CANVAS_MULTIPLE, round(nom_h / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


def _resize(image, width, height, crop):
    samples = image[..., :3].movedim(-1, 1)
    samples = comfy.utils.common_upscale(samples, width, height, "lanczos", crop)
    return samples.movedim(1, -1)


def snap_guide_run(n):
    """Largest 17k+5 run not exceeding n; 0 if there is not even a minimum run.

    MiniMaxH3AddGuide does this snap itself, but it then keeps the FIRST n
    frames of what it was handed. For a continuation the run has to END at the
    previous clip's last frame, so the snap happens here and the LAST n are
    kept instead.
    """
    n = int(n)
    if n < MIN_REF_FRAMES:
        return 0
    while n % 17 != 5:
        n -= 1
    return n


def cont_head_frames(cfg, frame_count):
    """本次续写会钉住的头帧数（0 = 不续写）。首尾帧锚必须避开这一段。

    只算「将要钉多少帧」，不解码任何素材：潜空间档的格点是纯算术、精确；
    文件档取请求窗口的 17k+5 吸附值，源片帧数不足时实际会更短（那时首帧
    锚落在接缝之后一两帧，无害，也不会与续写块重叠）。

    为什么必须错开：续写块钉在帧 0..n-1。首帧锚若也落在 0——
      · 文件档：两个 cond 块占同一时间坐标，画面谁也不像；
      · 潜空间档：Motion Context 把「钉头内的锚」整块丢弃，首帧完全消失。
    """
    cont = cfg.get("cont")
    if not cont:
        return 0
    frames = max(1, int(cont.get("frames") or 22))
    if (cont.get("mode") or "file") == "latent":
        if not cont.get("latent_file"):
            return 0                      # 没引用存档 = 按第一段处理
        n = max((g for g in _MC_RUN_GRID if g <= frames), default=0)
    else:
        if not cont.get("file"):
            return 0
        n = snap_guide_run(frames)
    # 首帧锚必须还在片内；留一位给末帧锚，两个锚不撞同一坐标
    return max(0, min(n, max(0, frame_count - 1)))


def flatten_exposure(frames, strength=1.0):
    """Level a guide run's brightness onto its own final frame.

    Measured on real renders: the model continues from the anchor window's
    AVERAGE exposure rather than from its last frame - consistent with the run
    being encoded as one block. A window ending below its average produced a
    clip about 19% brighter; one ending above produced a clip about 17% darker,
    and the sign followed the window both times. Scaling every frame onto the
    last one removes the cause rather than correcting the result, so nothing
    accumulates down a chain of clips.

    MEAN ONLY, deliberately: per-frame std matching was tried on the LTX chunk
    assembler and made exposure worse.

    The levelled frames are conditioning only. The model renders its own
    version of this span, so nothing here reaches the finished picture.
    """
    if strength <= 0.0 or frames.shape[0] < 2:
        return frames
    lum = frames.mean(dim=(1, 2, 3))
    target = float(lum[-1])
    if target <= 1e-6:
        return frames
    gain = 1.0 + (target / lum.clamp(min=1e-6) - 1.0) * float(strength)
    return (frames * gain.view(-1, 1, 1, 1)).clamp(0.0, 1.0)


def _empty_av_latent(width, height, length, batch_size=1):
    frame_count, latent_t, audio_t = temporal_shape(length)
    video = torch.zeros([batch_size, 24, latent_t, height // 16, width // 16],
                        device=comfy.model_management.intermediate_device())
    audio = torch.zeros([batch_size, 32, 2, audio_t],
                        device=comfy.model_management.intermediate_device())
    return {"samples": comfy.nested_tensor.NestedTensor((video, audio))}, frame_count


# --------------------------------------------------------------------------
# Asset resolution + loading
# --------------------------------------------------------------------------

def _ref_target_long(size_mode, canvas_long, ref_scale=1.0):
    """参考素材的目标长边；ref_scale 是分阶段（二采/三采）的档位倍数。

    "max" 不参与放大：它是显存安全上限而不是画质档位，乘上去只会撑爆显存。
    数值档与 match（跟随画布长边）都按倍数放大；之后调用方各自的
    min(1.0, ...) 仍然保证「永不放大源素材」——源不够大时倍数不生效。
    """
    if size_mode == "max":
        return REF_IMAGE_MAX_LONG_EDGE
    if size_mode == "match":
        return float(canvas_long) * ref_scale
    return int(size_mode) * ref_scale


def _video_target_size(vw, vh, size_mode, canvas_long, ref_scale=1.0):
    """视频参考的目标分辨率：按长边像素缩放，等比且永不放大原始尺寸。

    size_mode 为 "match"（长边跟随画布）或一个正整数（不设上限，仅长边等比、
    永不放大原始尺寸）。视频多帧、成倍吃显存，数值越大占显存越高，请按需设置。
    """
    target = _ref_target_long(size_mode, canvas_long, ref_scale)
    scale = min(1.0, target / max(vw, vh))
    return _fit_long(vw * scale), _fit_long(vh * scale)


def _load_video_frames(ref, start, end, max_frames, scale_to_canvas=False, size_mode=None,
                       canvas_long=0, fps=FPS, ref_scale=1.0):
    """Decode [start, end) seconds and resample to `fps`. Returns [N, H, W, 3].

    `fps` 只在参考视频这一路被改成面板上的「抽帧帧率」（csglide_cast.py 的
    cfg["ref_video_fps"]）：调低它，同一时间跨度里取到的帧更少。续写引导
    必须保持 24（它按帧位取尾部，秒→帧换算只用来估范围）。

    内存有界：不再把整段解码帧一次性堆进内存再采样，而是在线做最近帧采样。
    目标采样点 (start + k/fps) 单调递增、解码帧时间也单调，因此只需一格前瞻
    窗口就能判定每一帧是否命中某采样点——一旦下一帧比当前帧更远，前帧就不会
    再被任何后续采样点选中。全程只保留最终选中的那些帧的像素。
    """
    if av is None:
        raise RuntimeError("H3 Studio: PyAV is required to read reference videos.")
    path = _resolve_asset(ref)
    if path is None:
        raise FileNotFoundError("H3 Studio: video not found on disk: %s" % ref)

    slack = 0.75                      # seconds decoded past the out point

    # 采样帧预算。有明确的 out 点时一开始就能精确算出；否则先按上限截断，
    # 解码结束后再按真实跨度修订。
    if end is not None:
        want = int(round((end - start) * fps)) + 1
        want = max(MIN_REF_FRAMES, min(want, max_frames))
    else:
        want = max_frames

    picked = []                       # 选中的帧（按采样顺序，uint8）
    first_t = None
    last_t = None
    win_t = deque()                   # 一格前瞻：(时间, rgb24 ndarray)
    win_d = deque()

    def settle(eof):
        """把当前窗口已能确定的采样点尽量结算掉。"""
        while len(picked) < want:
            if len(win_d) >= 2:
                target = start + len(picked) / fps
                if abs(win_t[1] - target) <= abs(win_t[0] - target):
                    win_t.popleft()           # 下一帧至少一样近，丢弃当前帧
                    win_d.popleft()
                    continue
                # 当前帧命中；保留它，后续采样点可能复用同一帧
                picked.append(win_d[0])
                continue
            if eof and win_d:
                picked.append(win_d[0])       # 源耗尽 — 定格最后一帧
                continue
            break

    n = 0                             # 越过 start 的帧计数，仅用于 pts 缺失时的回退
    with av.open(path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        # 等比缩放目标在首帧解码时按实际帧宽高确定（比 stream 元数据更可靠）。
        # 关键优化：不再「解出全分辨率帧(4K) → 转 RGB → PIL Lanczos 缩小」——
        # 那三步全在 CPU、还留着一帧 4K 原始图。改在解码后立刻用 libswscale 的
        # reformat 在 C 层一步缩到目标尺寸（缩小时用 AREA=面积平均，降采样质量
        # 优于 Lanczos 且无振铃），峰值只留一帧缩后 rgb24。
        target = (0, 0)
        tb = float(stream.time_base) if stream.time_base else 1.0 / fps
        if start > 0:
            try:
                container.seek(int(max(0.0, start - 0.5) / tb), stream=stream)
            except Exception:
                pass
        for frame in container.decode(video=0):
            t = float(frame.pts * tb) if frame.pts is not None else (n / fps)
            if t < start - 1e-4:
                continue
            n += 1
            if end is not None and t > end + slack:
                break
            if first_t is None:
                first_t = t
            last_t = t
            if len(picked) < want:
                if scale_to_canvas:
                    if not target[0]:
                        target = _video_target_size(frame.width, frame.height, size_mode,
                                                    canvas_long, ref_scale)
                    # 目标尺寸与原帧一致时跳过重采样（reformat 仍要做 rgb24 转换）
                    if (frame.width, frame.height) != target:
                        frame = frame.reformat(
                            width=target[0], height=target[1], format="rgb24",
                            interpolation=_AV_INTERP.AREA,   # 面积平均，缩小时最优降采样
                        )
                        arr = np.ascontiguousarray(frame.to_ndarray())
                    else:
                        arr = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
                else:
                    arr = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
                win_d.append(arr)
                win_t.append(t)
                settle(False)
        settle(True)

    if not picked:
        raise ValueError("H3 Studio: no frames decoded from %s in that trim range." % ref)

    if end is None:
        span = (last_t - first_t) if (first_t is not None and last_t is not None) else 0.0
        want = int(round(span * fps)) + 1
        want = max(MIN_REF_FRAMES, min(want, max_frames))
        picked = picked[:want]

    if len(picked) < MIN_REF_FRAMES:  # 源确实耗尽 — 定格最后一帧补足
        picked += [picked[-1]] * (MIN_REF_FRAMES - len(picked))

    # 原地除法：`/ 255.0` 原本会再分配一份同尺寸副本，4K 多帧时峰值直接翻倍
    # （uint8 堆叠 + float32 + 除法临时 = 三份同时在内存里）。
    arr = np.stack(picked).astype(np.float32)
    arr /= 255.0
    return torch.from_numpy(arr)


def _load_video_tail(ref, n, start_hint=0.0):
    """Decode the LAST n frames of a clip, by POSITION rather than by time.

    The timestamp path samples targets at start + k/FPS, and start/end reach
    the node from the browser's video.duration - which for mkv is routinely a
    frame or more off the true last PTS. Read short, the window stops BEFORE
    the clip's final frame, and the continuation then picks up from slightly
    the wrong moment: the join reads as a small jump no colour or seam work
    can fix, because the two pieces are not adjacent in time.

    A continuation window is always the tail, so position is the honest
    measure. Only the last n frames are held, so the memory cost is the window
    and not the clip.
    """
    if av is None:
        raise RuntimeError("H3 Studio: PyAV is required to read reference videos.")
    path = _resolve_asset(ref)
    if path is None:
        raise FileNotFoundError("H3 Studio: video not found on disk: %s" % ref)

    keep = deque(maxlen=max(1, int(n)))
    total = 0
    with av.open(path) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        tb = float(stream.time_base) if stream.time_base else 1.0 / FPS
        # a clip at another frame rate would give n frames of a different
        # duration, so leave those to the timestamp path
        rate = float(stream.average_rate) if stream.average_rate else FPS
        if abs(rate - FPS) > 0.51:
            return None
        seek_to = max(0.0, float(start_hint or 0.0) - 1.0)
        if seek_to > 0:
            try:
                container.seek(int(seek_to / tb), stream=stream)
            except Exception:
                pass
        for frame in container.decode(video=0):
            keep.append(frame.to_ndarray(format="rgb24"))
            total += 1
            if total > 8192:
                break

    if not keep:
        raise ValueError("H3 Studio: no frames decoded from %s." % ref)
    arr = np.stack(list(keep)).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


class _RefAudioTooShort(ValueError):
    """参考音频不足一个潜空间帧（或一个样本都没有）。

    单独成类是为了区分两种意图：视频自带音轨是「参考视频」的附属品，不可用就
    沿用既有策略按静音发（画面照常生效）；独立音频参考是用户在提示词里点名要的，
    必须硬失败，不能悄悄变成静音。
    """


# 音频解码 / 重采样：全插件共用一份（csglide_audio_io），本模块不再自带副本。
# 那里的多后端降级是「同一个文件在这台机器能读、那台不能」的根治点，别再抄一份。
from .csglide_audio_io import (
    decode_audio_file,
    resample_waveform,
    stereo_channels as _stereo,
)
from . import csglide_audio_io as _audio_io


# 参考音频的长度上限：算法与常量都在公共层（csglide_audio_io），因为
# _h3mc 的动作上下文往同一颗音频 VAE 里送音频、会撞同一堵墙，两处必须同源。
AUDIO_VAE_MIN = _audio_io.AUDIO_VAE_MIN


def ref_audio_limit_seconds(frame_count):
    """参考音频的自动上限（秒）：目标时长与 5 秒取大、60 秒封顶，再由显存砍一刀。

    显存预算有一票否决权（详见 csglide_audio_io.audio_vae_budget_seconds）：
    撞显存不是「慢一点」，而是掉进核心那条对本颗 VAE 必然崩溃的分块回退，
    整次渲染报废。否则用户只会看到一条与音频毫不相干的 tuple 报错。
    """
    return _audio_io.audio_vae_budget_seconds(
        float(frame_count) / FPS,
        free_bytes=_audio_io.free_vram_bytes(),
        minimum=AUDIO_VAE_MIN)


def _load_audio(ref, start, end, max_seconds=None):
    """Returns a ComfyUI AUDIO dict, trimmed to [start, end).

    max_seconds 是整条参考的长度上限（秒）。它同时起两个作用：挡住超长音频把
    音频 VAE 推爆（见 ref_audio_limit_seconds），以及只解码到需要的长度——否则
    为了截断而把几分钟的素材整条解出来，白等也白占内存。
    """
    path = _resolve_asset(ref)
    if path is None:
        raise FileNotFoundError("H3 Studio: audio not found on disk: %s" % ref)

    start = max(0.0, float(start or 0.0))
    # 本条参考的结束时刻：显式窗口优先，窗口没封口时用 max_seconds 兜底。
    # max_seconds 约束的是**修剪之后的时长**（start 往后再取多少秒），换算到
    # 文件坐标就是 start + max_seconds。
    stop = None if end is None else float(end)
    if max_seconds is not None:
        cap = start + float(max_seconds)
        stop = cap if stop is None else min(stop, cap)

    # 必须按 stop（文件坐标）解码，不能按 stop-start：下面的 a 是在文件坐标上
    # 切的，若只解出「窗口时长」，start>0 时 a 会落到波形之外，窗口越靠后剩得
    # 越少，start 一旦超过窗口时长就直接判空。按 stop 解码同时也把长素材的
    # 代价限制在「用得到的位置」而不是整条。
    waveform, sr = decode_audio_file(path, max_seconds=stop)

    a = int(start * sr)
    b = waveform.shape[-1] if stop is None else min(int(stop * sr), waveform.shape[-1])
    if b <= a:
        raise ValueError("H3 Studio: audio trim for %s is empty." % ref)
    return {"waveform": _stereo(waveform[..., a:b])[None, ...],
            "sample_rate": int(sr)}


def _load_video_soundtrack(ref, start, end, max_seconds=None):
    try:
        return _load_audio(ref, start, end, max_seconds=max_seconds)
    except Exception as e:
        print("[H3 Studio] no usable soundtrack in %s (%s) — video sent silent." % (ref, e))
        return None


def _trimmed_duration(track):
    """加载后的实际时长（秒）。waveform 是 [B, C, T]，T 除以采样率就是了。"""
    w = track.get("waveform")
    sr = float(track.get("sample_rate") or 0.0)
    if w is None or sr <= 0:
        return 0.0
    return float(w.shape[-1]) / sr


# --------------------------------------------------------------------------
# 参考音频潜空间缓存
#
# 音频潜空间与画布无关——它不随分辨率、帧数、采样器变化，只由「这段波形的样本」
# 决定。所以键只需要「文件身份 + 裁剪窗口 + 本次长度上限」这几项：同一段参考音频
# 在多次渲染、多个分段之间可以完全复用，重复渲染时连编码这一步都不会发生，
# 那个显存峰值自然也就不存在了。
#
# 键里必须带上 max_seconds：上限是按当时的空闲显存算出来的，换了值就说明要编的
# 是另一段波形，不能复用（否则会把长音频的潜空间当成短音频的用）。
_REF_AUDIO_CACHE = OrderedDict()
_REF_AUDIO_CACHE_MAX = 16          # 一条 60 秒的潜空间约 0.6 MB，存满也就几 MB


def _ref_audio_cache_key(ref, start, end, max_seconds):
    """None 表示没法给这段素材做键（文件不在磁盘上等），此时不缓存。"""
    try:
        st = os.stat(ref)
    except OSError:
        return None
    return (os.path.abspath(ref), st.st_mtime_ns, int(st.st_size),
            round(float(start or 0.0), 4),
            None if end is None else round(float(end), 4),
            None if max_seconds is None else round(float(max_seconds), 3))


def _ref_audio_cache_get(key):
    if key is None:
        return None
    hit = _REF_AUDIO_CACHE.get(key)
    if hit is not None:
        _REF_AUDIO_CACHE.move_to_end(key)
    return hit


def _ref_audio_cache_put(key, latent, frames):
    if key is None or latent is None:
        return
    # 一律存 CPU 版本：缓存不该占着显存。
    _REF_AUDIO_CACHE[key] = (latent.detach().to("cpu"), int(frames))
    _REF_AUDIO_CACHE.move_to_end(key)
    while len(_REF_AUDIO_CACHE) > _REF_AUDIO_CACHE_MAX:
        _REF_AUDIO_CACHE.popitem(last=False)


def _cap_note(track, start, end, audio_cap):
    """只在**真的被截短**时才吐一句日志，否则返回空串。

    判据是「用户要的长度 > 上限」：没封口的窗口（end=None）必然命中，因为
    上限就是它唯一的封口；封了口但比上限长的也算是被截了。不截的时候保持
    日志安静——没发生的事不需要解释。
    """
    try:
        want = None if end is None else max(0.0, float(end) - float(start or 0.0))
        if want is not None and want <= audio_cap + 1e-6:
            return ""
        return ("  已按上限自动截到 %.1fs（上限 %.1fs = 目标时长 / 空闲显存 / 60s 三者取最小；"
                "超长参考会撞音频 VAE 显存并掉进必然失败的分块回退）"
                % (_trimmed_duration(track), audio_cap))
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Slot parsing + tag transcription
# --------------------------------------------------------------------------

def _slot_list(raw, count):
    out = []
    src = raw if isinstance(raw, list) else []
    for i in range(count):
        item = src[i] if i < len(src) and isinstance(src[i], dict) else {}
        out.append(item if item.get("file") else None)
    return out


def _pin_count(raw, default):
    """钉死的头 / 尾帧数：非数字或越界一律退回默认值。

    前端输入框是自由填写的，脏值直接喂给 build_pin_mask 会在采样前才炸 ——
    而那时候用户已经等了很久。这里收口，坏值当默认处理。
    """
    try:
        n = int(round(float(raw)))
    except (TypeError, ValueError):
        return int(default)
    if n < 0 or n > 4096:
        return int(default)
    return n


def parse_h3_data(raw):
    """Whitelisted parse — anything not listed here is dropped on purpose."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}

    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}

    def one(key):
        item = slots.get(key)
        return item if isinstance(item, dict) and item.get("file") else None

    # CONTINUE FROM: the previous clip's tail, anchored at frame 0 of this one.
    # Whitelisted like everything else - an older project file cannot smuggle
    # anything in through it.
    raw_cont = data.get("cont")
    cont = None
    if isinstance(raw_cont, dict):
        # 续写档位：file = 磁盘视频文件（原路径，解码→重编码）；latent = 上一片段
        # 的采样潜空间（Motion Context 算术，自动承接 output 目录最新的续拍存档）。
        # 缺省 file：老工程没有 mode 字段，行为逐帧不变。
        cont_mode = "latent" if raw_cont.get("mode") == "latent" else "file"
        if cont_mode == "latent" or raw_cont.get("file"):
            end = raw_cont.get("end")
            start = float(raw_cont.get("start") or 0.0)
            end = float(end) if end not in (None, "") else None
            # The UI states the window as start/end, and the span IS the frame
            # count - reading a separate "frames" field instead would silently cap
            # a 39f pick back to whatever that field last said.
            if end is not None:
                frames = int(round((end - start) * FPS)) + 1
            else:
                frames = int(raw_cont.get("frames") or 22)
            # 源片字段按档位分开：文件档 = file（adopt 拷进 input 的那份）；
            # 潜空间档 = latent_video（前端写的相对 output 成片路径，只作
            # 「源视频」输出供 Glide 续接合成拼长片）。分开存，切档互不污染；
            # 这里统一归位到 cont["file"]，下游读取方式不变。
            src = (raw_cont.get("latent_video") if cont_mode == "latent"
                   else raw_cont.get("file"))
            cont = {
                "mode": cont_mode,
                "file": str(src or ""),
                # 潜空间档的续写开关：面板引用了存档才有值（点了「引用上次片段」
                # 或工程连续渲染自动引用）。没有 = 本片段按第一段处理。
                "latent_file": str(raw_cont.get("latent_file") or ""),
                "start": start,
                "end": end,
                "frames": frames,
                "audio": bool(raw_cont.get("audio")),
                # the UI sends a checkbox; a float still works if a file carries one
                "flatten": max(0.0, min(1.0, float(raw_cont.get("flatten") or 0.0))),
            }

    # 段落档案归属：本段属于哪条片子、是第几段。
    # 空片名 = 不归档，导演台照旧单段工作（不产生任何档案文件）。
    raw_chain = data.get("chain")
    chain_ref = {"name": "", "clip": "", "shot": "",
                 "redo_mode": "保留动作", "redo_anchor": "等间隔",
                 "pin_head": 0, "pin_tail": 22}
    if isinstance(raw_chain, dict):
        chain_ref = {
            "name": str(raw_chain.get("name") or "").strip(),
            "clip": str(raw_chain.get("clip") or "").strip(),
            # 工程分段的唯一 id。后台按它认段——重抽同一分段时复用同一段档案，
            # 而不是每次都新建（clip id 前端拿不到，只能靠这个身份匹配）。
            "shot": str(raw_chain.get("shot") or "").strip(),
            # 重抽档位 / 锚点选法：随登记写进这一段档案，🧷 钉帧节点「跟随档案」
            # 时读它。前端选一次，节点不用再填一遍。
            # 重抽档位 / 锚点选法 / 钉帧数：前端存在 state **顶层**（redo_mode、
            # pin_tail…），不在 chain 里 —— 以前只从 chain 读，永远读到默认值，
            # 于是档案里恒为「保留动作 / 尾 22」，面板上选的「完整重拍」根本没
            # 传到执行侧（20261010 修正：顶层优先，chain 里也兼容）。
            "redo_mode": str(data.get("redo_mode") or raw_chain.get("redo_mode")
                             or "保留动作").strip(),
            "redo_anchor": str(data.get("redo_anchor") or raw_chain.get("redo_anchor")
                               or "等间隔").strip(),
            "pin_head": _pin_count(
                data.get("pin_head") if data.get("pin_head") is not None
                else raw_chain.get("pin_head"), 0),
            "pin_tail": _pin_count(
                data.get("pin_tail") if data.get("pin_tail") is not None
                else raw_chain.get("pin_tail"), 22),
        }

    return {
        "chain": chain_ref,
        "mode": "fl2va" if data.get("mode") == "fl2va" else "ref2va",
        "width": int(data.get("width") or 1344),
        "height": int(data.get("height") or 768),
        "length": int(data.get("length") or 124),
        "ref_image_size": _ref_size(
            data.get("ref_image_size"), REF_IMAGE_MAX_LONG_EDGE, "match"),
        "ref_video_size": _ref_size(
            data.get("ref_video_size"), None, "768"),
        # 参考视频采样帧率。它只作用于「参考视频」（_refs 的解码），不碰续写
        # 引导——引导按帧位取尾部、按 17k+5 吸附，跟秒→帧的换算无关。
        "ref_video_fps": _clamp_ref_fps(data.get("ref_video_fps")),
        # 「放大重绘」（二采/三采）条件的参考档位倍数。存面板 JSON 而不是原生
        # 控件：h3_data 已在 IS_CHANGED 里，改倍数走同一条失效链，且不占
        # widgets_values 的位置（旧工程回填不会被顶位）。
        "ref_scale_hd": _ref_hd_scale(data.get("ref_scale_hd")),
        # 语义桥（Semantic Bridge）：跨架构语义蒸馏适配器。alpha=0 关闭、零开销。
        # 适配器名前端可选（semantic_bridge/ 目录内），缺失/非法回退默认。
        "bridge_adapter": csglide_bridge.sanitize_adapter_name(
            data.get("bridge_adapter") or csglide_bridge.DEFAULT_ADAPTER),
        "bridge_alpha": _bridge_alpha(data.get("bridge_alpha")),
        # 作用范围：full 全量 / text 掩码版（仅文本 token）。
        "bridge_mode": _bridge_mode(data.get("bridge_mode")),
        "prompt": str(data.get("prompt") or ""),
        "first": one("first"),
        "last": one("last"),
        "images": _slot_list(slots.get("images"), MAX_IMAGES),
        "videos": _slot_list(slots.get("videos"), MAX_VIDEOS),
        "audios": _slot_list(slots.get("audios"), MAX_AUDIOS),
        "cont": cont,
    }


def build_tag_map(cfg, has_first, has_last):
    """Map @token -> real tag, using the same ordinal rules as the tokenizer.

    Presentation order is fixed by type: images, then videos (a soundtrack's
    <Audio j> is emitted immediately before its <Video k>), then standalone
    audio. Ordinals are 1-based per type and count only filled slots.
    """
    tags, presentation = {}, []

    if cfg["mode"] == "fl2va":
        i = 0
        if has_first:
            i += 1
            tags["@first"] = "<Picture %d>" % i
            presentation.append(("<Picture %d>" % i, "first frame"))
        if has_last:
            i += 1
            tags["@last"] = "<Picture %d>" % i
            presentation.append(("<Picture %d>" % i, "last frame"))
        return tags, presentation

    i = 0
    for n, slot in enumerate(cfg["images"], start=1):
        if slot is None:
            continue
        i += 1
        tags["@图片%d" % n] = "<Picture %d>" % i
        presentation.append(("<Picture %d>" % i, "image %d" % n))

    j, k = 0, 0
    for n, slot in enumerate(cfg["videos"], start=1):
        if slot is None:
            continue
        if slot.get("audio"):
            j += 1
            tags["@视频音频%d" % n] = "<Audio %d>" % j
            presentation.append(("<Audio %d>" % j, "video %d sound" % n))
        k += 1
        tags["@视频%d" % n] = "<Video %d>" % k
        presentation.append(("<Video %d>" % k, "video %d" % n))

    for n, slot in enumerate(cfg["audios"], start=1):
        if slot is None:
            continue
        j += 1
        tags["@音频%d" % n] = "<Audio %d>" % j
        presentation.append(("<Audio %d>" % j, "audio %d" % n))

    return tags, presentation


# <Audio N> 不在这张表里：它的 N 就是第 N 个独立音频，与 @音频N 同义。
# 旧做法把 N 当成「音频类的全局序号 j」（视频自带的声音与独立音频共用同一个
# 计数器），于是同一个 <Audio 1> 时而是某个视频的声音、时而是独立音频1，结果
# 随当日填充状态漂移。2026-09-25 起与前端 js/csglide_cast.js 统一为上面的固定
# 语义；视频自带的声音一律用 @视频音频N 引用，两套编号各数各的、互不挤占。
# （送模型那一侧的 <Audio j> 仍必须是全局 j —— 那是模型自身的协议，由
# build_tag_map 根据实况现场排号，与用户怎么写无关，勿混为一谈。）
# 它由 _AUDIO_TAG / _AUDIO_WORD 单独处理，见 normalize_refs。
_REF_ALIASES = (
    (re.compile(r"<\s*Picture\s*(\d+)\s*>", re.I), r"@图片\1"),
    (re.compile(r"<\s*Video\s*(\d+)\s*>", re.I), r"@视频\1"),
    (re.compile(r"(?<!@)\bPictures?\s*(\d+)(?!\d)", re.I), r"@图片\1"),
    (re.compile(r"(?<!@)\bImages?\s*(\d+)(?!\d)", re.I), r"@图片\1"),
    (re.compile(r"(?<!@)\bVideos?\s*(\d+)(?!\d)", re.I), r"@视频\1"),
    # 中文规则只用 (?:@\s*)? 容许 @ 后的空格：写成 @?\s* 会把「与 图片1」的
    # 空格一起吃掉，变成「与@图片1」。(?<![视音频图]) 挡住「视频音频3」里
    # 的「音频3」被音频规则二次命中，否则会被拆成「@视频@音频3」。
    (re.compile(r"(?:@\s*)?视频音频\s*(\d+)(?!\d)"), r"@视频音频\1"),
    (re.compile(r"(?<![视音频图])(?:@\s*)?(?:图片|图像)\s*(\d+)(?!\d)"), r"@图片\1"),
    (re.compile(r"(?<![视音频图])(?:@\s*)?视频\s*(\d+)(?!\d)"), r"@视频\1"),
    (re.compile(r"(?<![视音频图])(?:@\s*)?音频\s*(\d+)(?!\d)"), r"@音频\1"),
)

# <Audio N> 与裸词 Audio N：N 就是第 N 个独立音频。
# 裸词的左右边界与 _REF_ALIASES 里其他英文规则一致（(?<!@) 与 (?!\d)）。
_AUDIO_TAG = re.compile(r"<\s*Audio\s*(\d+)\s*>", re.I)
_AUDIO_WORD = re.compile(r"(?<!@)\bAudios?\s*(\d+)(?!\d)", re.I)

# 相邻标签之间补空格：@音频1@音频4 这种连贴写法来自手滑或外部工具，下游目前切
# 得开、也换得对，但难读且脆——依赖「替换按长度倒序」恰好规避前缀陷阱这种巧合。
# 补一个空格就把「标签能不能挨着」这个边界问题消掉了。
# 位置必须在所有写法都收敛成规范标签之后（见 normalize_refs 里的调用顺序）；
# 幂等：已有空格的文本不再命中 (?=@)。与前端 js 的 GLUE_RE 必须一致。
_GLUE_RE = re.compile(r"(@(?:first|last|图片\d+|视频音频\d+|视频\d+|音频\d+))(?=@)")


def _audio_tokens(videos, audios):
    """按 presentation 顺序排出全局音频 token：视频音轨在前、独立音频在后。

    <Audio N> 是模型自身的协议号（见 build_tag_map 的 j），视频自带的音轨与
    独立音频共用同一个计数器。这里必须按同一顺序排，才能让「用户写的 N」与
    「模型看到的第 N 个音频」对上。
    """
    out = []
    for n, slot in enumerate(videos or [], start=1):
        if slot and slot.get("file") and slot.get("audio"):
            out.append("@视频音频%d" % n)
    for n, slot in enumerate(audios or [], start=1):
        if slot and slot.get("file"):
            out.append("@音频%d" % n)
    return out


def _audio_ref(n, videos=None, audios=None):
    """全局第 N 个音频 → 它的规范 token（@视频音频K 或 @音频K）；越界 → None。

    N 是模型协议里的音频序号，不是「第 N 个独立音频槽」。旧实现只查 audios，
    于是同时存在视频音轨与独立音频时，用户写 <Audio 1> 被解析成 @音频1（独立
    音频），而模型侧 <Audio 1> 其实是视频1的音轨 —— 编号整体错位一格。
    与前端 js 的 audioRef() 必须给出同一个答案。空槽/越界返回 None 而不是兜底
    标签 —— 后者指向空位，会被 transcribe_prompt 静默删掉，用户只看到「写了
    却没生效」，比报错难查十倍。原文留着还能自愈：素材填进那一格后下次就换成了。
    """
    toks = _audio_tokens(videos, audios)
    return toks[n - 1] if 1 <= n <= len(toks) else None


def normalize_refs(text, videos=None, audios=None):
    out = str(text or "")
    # 音频标签先处理：放到中文规则之后会再被「视频音频」规则碾一遍。
    # 空槽时回吐原匹配串 m.group(0)（= 没改动），不能返回 None —— re 的 repl
    # 函数必须给出字符串。
    out = _AUDIO_TAG.sub(
        lambda m: _audio_ref(int(m.group(1)), videos, audios) or m.group(0), out)
    out = _AUDIO_WORD.sub(
        lambda m: _audio_ref(int(m.group(1)), videos, audios) or m.group(0), out)
    for pat, rep in _REF_ALIASES:
        out = pat.sub(rep, out)
    out = _GLUE_RE.sub(r"\1 ", out)
    return out


# ---- 别名：剧本里直接写角色名 → @图片N ----------------------------------
# 媒体架槽位可带 alias（拖入时按文件名主干自动登记，也可手动改多个称呼）。
# 提示词里出现别名即等价于写 @图片N，原文不改写——展开只发生在送模型这一路。
#
# 展开必须排在 normalize_refs 之前：normalize 认得 picture 1 / image 1 这类
# 英文写法，若先归一化，名字长得像英文标签的别名会被抢先变成 @图片N，指到
# 完全错误的槽位；反过来，展开产物 @图片N 也不该再被别名二次改写。

_ALIAS_SEP = re.compile(r"[,，、;；]+")
_ALIAS_MAX = 40       # 单个别名长度上限（与媒体架侧一致）
_ALIAS_TOTAL = 12     # 每个槽位别名个数上限


def _norm_space(text):
    """把全角空格 U+3000 换成半角 U+0020。

    中文输入法在「张三丰　剧照」这种词间位置极易产出全角空格，而用户手打
    别名时又可能是半角，两边对不上就永远命不中。归一化必须**两侧都做**：
    只改名字或只改正文，仍会有「名字半角 / 正文全角」这类组合漏掉。

    只换 U+3000，不动其他空白：制表符、NBSP 在中文输入里几乎不出现，多归一
    一类字符就多一份「我以为写的是这个、它却变成那个」的意外。分隔符本来就把
    全角逗号顿号收进了 _ALIAS_SEP，不需要在这里处理。

    口径与前端 js/csglide_cast.js 的 normSpace、js/csglide_media_rack.js 的
    normSpace 必须一致，三处各写一份迟早会飘。
    """
    return text.replace("\u3000", " ") if text else text


# 撞内置词的名字一律不注册：否则「图片1」到底算别名还是规范写法无法判定。
_BUILTIN_ALIAS = re.compile(
    r"^(?:first|last|图片\d+|视频音频\d+|视频\d+|音频\d+|"
    r"pictures?\s*\d+|videos?\s*\d+|audios?\s*\d+|images?\s*\d+)$", re.I)
# 边界按「首尾是不是 ASCII 字母」分两组：
#   ASCII 组要挡紧邻的字母数字（Tom 不能命中 Tommy）；
#   中文组只能挡数字——中文没有空格分词，若连汉字一起挡，「看小明走过来」
#   这种最常见的剧本句式会整个失效，别名等于没做。
# 边界里还要挡哨兵与反斜杠：转义过的别名（\小明）不能被展开。
# \x00 是转义哨兵，见 _ESC。
_ALIAS_EDGES = {
    "ascii": (r"(?<![\x00\\A-Za-z0-9])", r"(?![A-Za-z0-9])"),
    "cjk":   (r"(?<![\x00\\0-9])",       r"(?![0-9])"),
}

# 保护区：台词标签 <d>…</d> 内部、引号内部不展开别名。台词是给 H3 念的字，
# 换成 <Picture n> 会念错或搞乱口型。规则必须与前端 PROTECT_SRC 一致。
# 末两项是「引号开了没闭」的兜底：打字打一半、台词跨行只写了一半时闭引号还没
# 出现，闭引号规则匹配不上，后半个引号里的名字照样被展开。按行兜到行尾——
# 引号本就不跨行，吞掉一整段才是更大的事故。re.M 让 $ 认行尾。
# 前端 js/csglide_cast.js 的 PROTECT_SRC / PROTECT_FLAG 必须与此一致。
_PROTECT_RE = re.compile(
    r"<d>[\s\S]*?</d>|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d"
    r"|\"[^\"\n]*(?=$|\n)|\u201c[^\u201d\n]*(?=$|\n)", re.M)
_ESC = "\x00"          # 转义哨兵：提示词里不可能出现的字符


def _map_outside(text, fn):
    """只对保护区**外面**的片段做变换，保护区原样通过。"""
    out, pos = [], 0
    for m in _PROTECT_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


def _alias_ok(name):
    """单字不注册（误伤率爆炸），含 @ 或反斜杠不行，撞内置词不行。

    注意传入的是**去掉 @ 的**名字：@ 前缀在别名里是「仅 @ 触发」的标记，
    不是名字的一部分。

    空格是允许的，且**是名字的一部分**（「张三丰 剧照」就是一个名字）。
    分隔符只有 _ALIAS_SEP 那几个，空格不在其中，所以写法上不会歧义。名字
    首尾的空格由调用方 trim 掉（_alias_of / writeAlias 都已 trim），这里
    不重复做——中间的空格必须原样保留，否则「张三丰 剧照」会被压成
    「张三丰剧照」，与用户输入对不上，正文里怎么写都命不中。
    """
    if not name or len(name) < 2 or len(name) > _ALIAS_MAX:
        return False
    # 反斜杠留给转义（\别名 = 保留文字），名字里出现会与之冲突
    if "@" in name or "\\" in name:
        return False
    return not _BUILTIN_ALIAS.match(name)


def _alias_of(slot):
    """取出槽位里登记过的别名（str 或列表都要收）。

    这里只做「取」，**不做合法性过滤**——过滤统一在 alias_table 里由
    _alias_ok 完成，两处各判一遍就会出现「取到了但没注册」的静默失效。
    去重与 _ALIAS_TOTAL 截断保留在此：它们决定的是「登记顺序」，不是合法性。
    """
    v = slot.get("alias")
    if isinstance(v, str):
        parts = _ALIAS_SEP.split(v)
    elif isinstance(v, list):
        parts = [q for p in v if isinstance(p, str) for q in _ALIAS_SEP.split(p)]
    else:
        return []
    out = []
    for p in parts:
        # strip 先剥首尾（Python 的 strip() 默认就认全角空格），再归一内部
        # 的全角空格——顺序不能反：先归一的话，首尾的全角空格会被转成半角，
        # 但结果一样会被 strip 掉，多一步无谓转换而已。真正要保的是**中间**的
        # 空格改成半角，好与正文里的半角对齐。
        a = _norm_space(p.strip())
        if a and a not in out:
            out.append(a)
    return out[:_ALIAS_TOTAL]


def alias_table(cfg):
    """别名表 → (map, strict)。

    写法即作用域，不需要额外字段：
      小明   → 自动：正文里写「小明」就引用
      @小明  → 仅 @ 触发：必须写「@小明」才引用（名字太常见时用）
      清空   → 关闭：不参与
    map: 名字（不含 @）→ 规范 token；strict: 只认 @ 的名字集合。
    先出现者优先：重名时后者静默失效，保留前一个（不覆盖、不报错）。
    """
    out, strict = {}, set()
    for kind, fmt in (("images", "@图片%d"), ("videos", "@视频%d"), ("audios", "@音频%d")):
        arr = cfg.get(kind)
        if not isinstance(arr, list):
            continue
        for i, slot in enumerate(arr, start=1):
            if not isinstance(slot, dict):
                continue
            for raw in _alias_of(slot):
                at = raw.startswith("@")
                name = raw[1:] if at else raw
                if not _alias_ok(name) or name in out:
                    continue
                out[name] = fmt % i
                if at:
                    strict.add(name)
    return out, strict


def alias_map(cfg):
    return alias_table(cfg)[0]


def expand_aliases(text, amap, strict=()):
    """把提示词里的别名替换成规范 token。

    三件事：
      1. 保护区（<d>…</d> 与引号内）整段跳过 —— 台词原样送出去；
      2. \\别名 转义：保留文字，末尾去掉反斜杠；
      3. 每组一次扫描 + 组内最长优先：逐个 str.replace 会连锁替换，一个别名
         恰好是另一个的子串时必然出错（先换「小明」会把「小明月」也吃掉）。
    组间不会连锁——替换结果是 @图片N，而「图片N」是禁用的别名。
    """
    if not text or not amap:
        return text
    strict = set(strict or ())
    names = list(amap)
    escaped = re.sub(r"\\(?=" + "|".join(re.escape(k) for k in names) + ")",
                     _ESC, text)

    # 全局按长度倒序、**逐个别名**替换。不能分 ascii/cjk 两组批量替换：
    # 短别名（ascii 组）会抢先吃掉长别名（cjk 组）里的一段——别名「小明Ab刀」
    # 加「Ab」时，「小明Ab刀」会被截成「小明@图片2刀」。逐个按长度先长后短就
    # 没这个问题；替换结果是 @图片N（不含任何别名，且「图片N」是禁用的别名），
    # 所以也不会连锁。
    ordered = sorted(names, key=len, reverse=True)

    def seg(s):
        if not s:
            return s
        # 全角空格在这里归一：seg 只收到**保护区之外**的片段，所以台词
        # <d>…</d> 与引号内一个字符都不会被动——那些字要原样送给模型去念。
        # 必须早于下面的匹配：名字在 _alias_of 里已归一成半角，正文不归一
        # 就永远对不上。
        s = _norm_space(s)
        for n in ordered:
            key = "ascii" if any(c.isascii() and c.isalpha() for c in (n[0], n[-1])) else "cjk"
            edge_l, edge_r = _ALIAS_EDGES[key]
            at = "@" if n in strict else "@?"
            pat = re.compile(edge_l + at + "(" + re.escape(n) + ")" + edge_r)
            s = pat.sub(lambda m, _r=amap[n]: _r, s)
        return s

    return _map_outside(escaped, seg).replace(_ESC, "")


def transcribe_prompt(prompt, tags, known_tokens):
    """Replace @tokens with real tags. Tokens pointing at empty slots are dropped."""
    out = prompt
    for token in sorted(known_tokens, key=len, reverse=True):
        if token in tags:
            out = out.replace(token, tags[token])
        elif token in out:
            print("[H3 Studio] %s points at an empty slot — removed from the prompt." % token)
            out = out.replace(token, "")
    # 检测残留下来的、系统未识别的 @标签（常见于格式写错，如 @图1 应为 @图片1）
    import re
    leftover = sorted(set(re.findall(r"@\S+", out)))
    if leftover:
        print("[H3 导演台] 提示词里有未识别的 @标签（若想引用参考，请用 @图片N / @视频N / @音频N 格式）：%s"
              % ", ".join(leftover))
    return out


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------

def _filter_refs_by_prompt(cfg):
    """严格按需：提示词里提到的 @标签 决定送哪些参考——一个都没提就一个
    都不送（方案确认项）。未提到的槽位置 None 但保留编号位置，
    <Picture i> 序号由 build_tag_map 只数已填槽位自动重排。
    返回各类型保留的编号（1 起），供日志核对。"""
    prompt = cfg.get("prompt") or ""

    def nums(*pats):
        out = set()
        for pat in pats:
            out.update(int(m) - 1 for m in re.findall(pat, prompt))
        return out

    keep = {
        "images": nums(r"@图片(\d+)"),
        "videos": nums(r"@视频音频(\d+)", r"@视频(\d+)"),
        "audios": nums(r"@音频(\d+)"),
    }
    """超过各类型上限时按编号取前 cap 个，其余槽位置空——<Picture 10> / 
    <Video 4> 这类标签模型不认，送进去只会静默失效（图没生效却没报错）。
    截断规则与前端 filterUsedByPrompt 必须一致。"""
    caps = {"images": MAX_IMAGES, "videos": MAX_VIDEOS, "audios": MAX_AUDIOS}
    over = {}
    for kind, idxs in list(keep.items()):
        cap = caps[kind]
        if len(idxs) > cap:
            keep[kind] = set(sorted(idxs)[:cap])
            over[kind] = len(idxs)
    # 「一个都没提就一个都不送」是全局判定，不是逐类型判定：与前端
    # filterUsedByPrompt 的 any 同义。只提了图片时，视频侧是「位置留空的
    # 数组」还是「空数组」对下游等价（都数不出素材），但两边必须同构，
    # 否则以后任一侧加了「按空位补提示」之类的逻辑就会瞬间对不上。
    #
    # 例外：独立音频不参与按需过滤。官方 MiniMaxH3ReferenceToVideo 对 ref_audios
    # 是无条件发送的（不看提示词），导演台原先也过滤，于是「摆好了音频、提示词
    # 没写 @音频N」就静默丢弃——用户只看到音频没生效，查不出原因。
    # 音频是「配乐/音色」这类整体氛围参考，本来就不一定在剧本里被逐句提到，
    # 按需过滤对它没有省显存的意义（音频 token 占比极小），却极易误伤。
    # 音频仍受 MAX_AUDIOS 上限约束，超出的按编号取前 N 个。
    any_keep = any(keep.values())
    kept = {}
    for kind, idxs in keep.items():
        arr = cfg.get(kind)
        if not isinstance(arr, list):
            continue
        if kind == "audios":
            all_idx = set(i for i in range(len(arr)) if arr[i])
            if len(all_idx) > caps["audios"]:
                all_idx = set(sorted(all_idx)[:caps["audios"]])
            kept[kind] = sorted(i + 1 for i in all_idx)
            cfg[kind] = [arr[i] if i in all_idx else None
                         for i in range(len(arr))]
            continue
        kept[kind] = (sorted(i + 1 for i in idxs if i < len(arr) and arr[i])
                      if any_keep else [])
        cfg[kind] = ([arr[i] if i in idxs else None for i in range(len(arr))]
                     if any_keep else [])
    return kept, over


# ---------------------------------------------------------------------------
#  文本编码器用完就还
#
#  导演台是整个工作流里唯一吃 CLIP 的节点：提示词编完，它就再也不碰了。
#  可它占的显存会一直挂到工作流结束，后面的扩散模型 + LoRA 只能跟它抢。
#
#  为什么放在「节点末尾」而不是「每次编码之后」：
#    本节点内部最多要编三次 —— 主档位一次，放大重绘 1.25× / 2.0× 各一次
#    （见 cond_at）。中间丢页等于让后面每一次重编码都白花 2~11 秒。
#    放到末尾则一次都不多花：往后就是采样器，编码器确实用不上了。
#
#  为什么不怕「下次运行变慢」：
#    丢的是驻留页，权重还在原 safetensors 里。而且本节点之后马上要跑 20 GB 的
#    扩散模型，那本身就会把编码器的页挤出去 —— 丢与不丢，下次都得重新读。
# ---------------------------------------------------------------------------

def _clip_has_other_user(prompt, unique_id):
    """本轮里除了导演台，还有别的节点要拿同一个 CLIP 吗？

    看本节点的 clip 输入接的是哪个上游节点、哪个输出口，再扫一遍整张图有没有
    第二个节点也接那个口（ComfyUI 按「源节点 + 输出口」缓存同一个对象，所以
    接同一个口 = 拿到同一个 patcher）。

    拿不到图信息（老前端 / 被别处直接调用）时按「没人用」处理：代价只是几秒钟，
    不值得为它牺牲「一律回收」的确定性。
    """
    try:
        if not prompt or unique_id is None:
            return False
        mine = prompt.get(str(unique_id))
        if not isinstance(mine, dict):
            return False
        ref = (mine.get("inputs") or {}).get("clip")
        if not isinstance(ref, (list, tuple)) or len(ref) < 2:
            return False
        src, slot = str(ref[0]), ref[1]
        for nid, node in prompt.items():
            if str(nid) == str(unique_id) or not isinstance(node, dict):
                continue
            for v in (node.get("inputs") or {}).values():
                if (isinstance(v, (list, tuple)) and len(v) >= 2
                        and str(v[0]) == src and v[1] == slot):
                    return True
    except Exception:
        return False
    return False


def _release_clip_when_done(clip, prompt=None, unique_id=None):
    """导演台跑完，把文本编码器占的驻留页交还系统。失败绝不能影响出片。"""
    try:
        patcher = getattr(clip, "patcher", None)
        if patcher is None:
            return
        if _clip_has_other_user(prompt, unique_id):
            print("[H3 导演台] 文本编码器：本轮还有别的节点要用同一路 CLIP，先留着不回收。")
            return
        from .banzhang_vram_cleaner import release_model_residency
        ram_freed, vram_freed = release_model_residency(
            patcher, log=lambda m: print("[H3 导演台] " + str(m)), label="文本编码器")
        if ram_freed or vram_freed:
            print("[H3 导演台] 文本编码器：用完已交还 —— 显存 %.0f MB / 内存 %.0f MB。"
                  "权重仍在原文件里，下次要用会自动读回，不影响重复运行。"
                  % (vram_freed / 1e6, ram_freed / 1e6))
        else:
            print("[H3 导演台] 文本编码器：本轮没有可回收的驻留页（DynamicVRAM 流式加载时不常驻）。")
    except Exception as _e:
        try:
            print("[H3 导演台] 文本编码器回收跳过：%s: %s" % (type(_e).__name__, _e))
        except Exception:
            pass


class CSGlideCast:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "vae": ("VAE",),
                # NOT multiline: the v2 frontend renders a multiline STRING as a
                # real textarea overlay, which floats over the node's own UI and
                # eats every click in that band. A single-line widget is drawn on
                # the canvas and can be hidden properly.
                "h3_data": ("STRING", {"default": ""}),
            },
            "optional": {
                "audio_vae": ("VAE",),
                "first_frame": ("IMAGE",),
                "last_frame": ("IMAGE",),
                "refs": ("H3_REFS",),
                # 外接提示词：接线即替换面板提示词（模式 A）。面板 st.prompt
                # 永不被外部数据污染——断线即恢复原文。
                "prompt_text": ("STRING", {"forceInput": True}),
                # 外接 h3_data（AI 制片中枢 P1）：接线即整体替换面板配置 JSON，
                # # 断线即恢复面板。供无头编排器逐段注入画布/片长/承接/工程身份。
                "h3_override": ("STRING", {"forceInput": True}),
            },
            # 只用来判断「这个 CLIP 本轮后面还有没有人要用」——
            # 见 _clip_has_other_user。不渲染控件，不影响既有工作流。
            "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"},
        }

    # 输出端口按「常用优先」重排，隐藏暂时用不到的宽度/高度/源视频/日志四个口：
    # 正面条件 → 正面条件·二采三采 → 潜空间 → 总帧数 → 秒数 → 重叠帧 → 引导帧。
    # ComfyUI 按端口**序号**存连线（不按名字），本次重排后旧工作流接到导演台的
    # 线会整体错位、需手动重连一次——故此处只收敛这一次，之后新增端口一律追加
    # 在末尾（前几个端口不再变动，旧工程即零影响）。
    # 「放大重绘」（正面条件·二采三采）只有一份条件——二采与三采共用
    # （同一级重绘、同一套参考口径），两个端口除了让用户多接一根线之外不带来差异。
    # 第 8 口「整片」（VIDEO，20261013）：把时间轴上当前整片（最后一段在磁盘上
    # 的累积成片）作为成品视频输出，直连视频合并节点的「🎬 视频文件」口，由它
    # 直接采用、不重新编码。按铁律追加在末尾，旧工作流已有的 7 根线零影响。
    RETURN_TYPES = ("CONDITIONING", "CONDITIONING", "LATENT", "INT", "FLOAT", "INT",
                    "IMAGE", "VIDEO")
    RETURN_NAMES = ("正面条件", "正面条件·二采三采", "潜空间", "总帧数", "秒数",
                    "重叠帧", "引导帧", "整片")
    FUNCTION = "build"
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "MiniMax H3 导演节点——首/末关键帧或全参考，自动标注参考帧。"

    # ---------------- reference encoding ----------------

    @staticmethod
    def _encode_ref_audio(audio_vae, audio, label="", log=None, cache_key=None):
        if audio_vae is None:
            raise ValueError("H3 Studio: audio references need the audio VAE connected "
                             "(minimax_h3_audio_vae_fp32).")
        # 命中缓存就直接返回，连解码和编码都不发生——重复渲染/多分段渲染时，
        # 那个「音频一长就吃几 GB 显存」的峰值根本不会出现。
        hit = _ref_audio_cache_get(cache_key)
        if hit is not None:
            if log:
                log("音频 %s 复用已缓存的潜空间（%d 帧，本次跳过编码）"
                    % (os.path.basename(label) if label else "(未命名)", hit[1]))
            return hit[0], hit[1]
        waveform = audio["waveform"]
        sr = audio["sample_rate"]
        vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
        samples_in = int(waveform.shape[-1])
        if sr != vae_sr:
            waveform = resample_waveform(waveform, sr, vae_sr)
        # 音频潜空间 1 帧 = hop = 800 样本 = 1/40 秒（32 kHz）。VAE.encode 内部会
        # 先把样本数向下裁到 hop 的整数倍，不足一帧就剩下 0 样本，撞穿编码器开头
        # k=7 的卷积（"padded input size (6)"）。这里在进 VAE 前一次拦住、报人话，
        # 与视频侧的 MIN_REF_FRAMES 同一思路。
        samples = int(waveform.shape[-1])
        hop = int(audio_vae.spacial_compression_encode()) or 800
        if samples < hop:
            raise _RefAudioTooShort(
                "H3 Studio: audio reference %s is %d samples (%.4f s @ %d Hz) after "
                "resampling from %d samples @ %d Hz; one audio latent frame needs %d "
                "samples (%.4f s). Check the file itself, or its trim window in the "
                "media rack."
                % (label or "(unnamed)", samples, samples / vae_sr, vae_sr,
                   samples_in, sr, hop, hop / vae_sr))
        # 编码执行交给公共层：GPU 优先，预判放不下或 GPU 那条路失败都自动改走
        # CPU（见 csglide_audio_io.encode_audio_latent）。这里不再直接调
        # audio_vae.encode —— 那样会绕过「放不下就先撤到 CPU」的判断，直接撞进
        # 核心那条对本颗 VAE 必然崩的分块回退。
        #
        # 波形进公共层的是 [B, 声道, 采样数] 的原始形态；里面对包装器的
        # movedim(-1, 1) 已经复原过了，不要在这里提前转 channels-last，否则
        # 「声道」会被当成要按 hop 取整的空间轴（2 // 800 = 0，声道被裁没）。
        try:
            z, where = _audio_io.encode_audio_latent(
                audio_vae, waveform, label=os.path.basename(label) if label else "",
                log=log)
        except Exception as e:
            # 两条路都失败了。把「这一步要多少显存」算给用户看——原始报错是
            # 核心退到图像版分块路径后抛出的 tuple 错误，与音频毫无关系。
            raise RuntimeError(
                "H3 Studio: 音频参考「%s」编码失败（%s）。它是 %.1f 秒（%d 样本），"
                "这一步约需 %.1f GB 显存；GPU 与 CPU 两条路都试过了。音频 VAE 的"
                "占用随音频长度线性增长，建议在媒体架上把这条音频的修剪窗口拖短"
                "（或换更短的素材）后重试。"
                % (label or "(未命名)", type(e).__name__, samples / vae_sr,
                   samples, _audio_io.audio_vae_peak_gb(samples, vae_sr))) from e
        _ref_audio_cache_put(cache_key, z, z.shape[-1])
        return z, z.shape[-1]

    def _refs(self, cfg, vae, audio_vae, width, height, frame_count, log=None, ref_scale=1.0):
        ref_items, ref_blocks = [], []
        size_mode = cfg["ref_image_size"]

        def _log(*a):
            if log:
                log(*a)

        # 参考音频的自动长度上限（秒）。刻意做成「用的时候才量」：空闲显存要
        # 在参考图/参考视频的潜空间都占进来之后再读，才是音频 VAE 真能拿到的
        # 那份。视频自带的音轨与独立音频共用同一个上限——两条路都会进音频 VAE
        # 编码，也就都会撞同一个 OOM。
        def _audio_cap():
            return ref_audio_limit_seconds(frame_count)

        for n, slot in enumerate(cfg["images"], start=1):
            if slot is None:
                continue
            img = _load_image(slot["file"])
            h, w = img.shape[1], img.shape[2]
            # 固定长边档位 / match（跟随画布长边）/ max；ref_scale 是二采/三采
            # 条件的档位倍数（1.0 = 与主档位相同）
            target = _ref_target_long(size_mode, max(width, height), ref_scale)
            scale = min(1.0, target / max(w, h))   # never upscale, all tiers share this
            tw, th = _fit_long(w * scale), _fit_long(h * scale)
            resized = _resize(img[:1], tw, th, "disabled")
            _log("图片 @图片%d %s  原图 %dx%d → 编码 %dx%d  潜空间 %dx%d"
                 % (n, os.path.basename(slot["file"]), w, h, tw, th, th // 16, tw // 16))
            # 验证：resized 全程只物化一次 —— _resize 仅新建这一份张量；vae.encode 只读取它、
            # 返回 latent（非原地）；下面 ref_items["data"] 与下游 _vision_entry 均存引用而非拷贝，
            # 故不会二次物化、不额外占用一份显存。
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append({"kind": "image", "latent_h": th // 16, "latent_w": tw // 16,
                               "latent": vae.encode(resized)})
            # 原始全分辨率图片在缩放+编码后不再需要，及时释放其 CPU/显存占用，
            # 避免多图参考时前一张的原始图与下一张叠加峰值。
            del img

        video_size_mode = cfg["ref_video_size"]
        # 面板「抽帧帧率」：只作用于参考视频。调低它，同一时间跨度取到的帧更少
        # （成帧数与后面的 latent_t 一起变小），代价是锚定精度。
        ref_fps = cfg["ref_video_fps"]
        for vi, slot in enumerate(cfg["videos"], start=1):
            if slot is None:
                continue
            start = float(slot.get("start") or 0.0)
            end = slot.get("end")
            end = float(end) if end not in (None, "") else None
            # 媒体架槽位的自定帧率优先（面板「帧率」输入框），未设则用面板抽帧帧率
            slot_fps = float(slot["fps"]) if slot.get("fps") else ref_fps
            frames = _load_video_frames(slot["file"], start, end, frame_count,
                                        scale_to_canvas=True, size_mode=video_size_mode,
                                        canvas_long=max(width, height), fps=slot_fps,
                                        ref_scale=ref_scale)

            # 已在解码阶段按档位等比缩小到目标分辨率，此处直接取缩后分辨率。
            vh, vw = frames.shape[1], frames.shape[2]
            cw, ch = vw, vh
            if frames.shape[0] > frame_count:
                frames = frames[:frame_count]
            n = frames.shape[0]
            if n < MIN_REF_FRAMES:
                raise ValueError(
                    "H3 Studio: %s gave only %d frames after trimming; H3 needs %d. "
                    "Widen the trim, or use the 'last 22f' quick pick."
                    % (slot["file"], n, MIN_REF_FRAMES))
            while n % 17 != 5:
                n -= 1
            frames = frames[:n]
            z = vae.encode(frames)

            # 整段 float32 全帧只在编码阶段用到；之后唯一消费者是半速预览帧。
            # 高级索引会拷贝（拥有独立 storage），因此先抽出预览帧，再释放整段
            # 全帧（约 n*H*W*3*4 字节），避免下一段视频解码时峰值显存叠加。
            # 预览保持 0.5 秒一格：抽帧后每帧的时长变成 stride/slot_fps，步长
            # 必须跟着变，否则调低帧率会把预览也一起放慢。
            stride = max(1, int(slot_fps // 2))
            sample_idx = list(range(0, frames.shape[0], stride))
            sample = frames[sample_idx]
            timestamps = [i * stride / slot_fps for i in range(len(sample_idx))]
            del frames

            audio_latent, ref_audio_t, audio_note = None, 0, ""
            if slot.get("audio"):
                cap = _audio_cap()
                track = _load_video_soundtrack(slot["file"], start, end, max_seconds=cap)
                if track is not None:
                    # 音轨是「参考视频」的附属品：与「无音轨 / 解码失败」同一策略，
                    # 不可用就按静音发，画面照常生效，只在日志里留一句。
                    try:
                        audio_latent, ref_audio_t = self._encode_ref_audio(
                            audio_vae, track, slot["file"], log=_log,
                            cache_key=_ref_audio_cache_key(slot["file"], start, end, cap))
                    except _RefAudioTooShort as e:
                        _log("视频 %s 音轨太短，按静音发送（%s）"
                             % (os.path.basename(slot["file"]), e))
                    else:
                        ref_items.append({"type": "audio"})
                        audio_note = _cap_note(track, start, end, cap)

            ref_items.append({"type": "video", "data": sample, "timestamps": timestamps})
            ref_blocks.append({"kind": "video_audio" if ref_audio_t else "video",
                               "latent_t": z.shape[2], "latent_h": ch // 16, "latent_w": cw // 16,
                               "ref_audio_t": ref_audio_t, "latent": z, "audio_latent": audio_latent})
            _log("视频 @视频%d %s  成帧 %d  抽帧 %dfps  画布 %dx%d  含音轨=%s  参考音频时间=%d%s"
                 % (vi, os.path.basename(slot["file"]), n, slot_fps, cw, ch,
                    "是" if ref_audio_t else "否", ref_audio_t, audio_note))

        for n, slot in enumerate(cfg["audios"], start=1):
            if slot is None:
                continue
            end = slot.get("end")
            a_start = float(slot.get("start") or 0.0)
            a_end = float(end) if end not in (None, "") else None
            cap = _audio_cap()
            track = _load_audio(slot["file"], a_start, a_end, max_seconds=cap)
            audio_latent, ref_audio_t = self._encode_ref_audio(
                audio_vae, track, slot["file"], log=_log,
                cache_key=_ref_audio_cache_key(slot["file"], a_start, a_end, cap))
            ref_items.append({"type": "audio"})
            ref_blocks.append({"kind": "audio", "ref_audio_t": ref_audio_t,
                               "audio_latent": audio_latent})
            _log("音频 @音频%d %s  参考音频时间=%d%s"
                 % (n, os.path.basename(slot["file"]), ref_audio_t,
                    _cap_note(track, a_start, a_end, cap)))

        return ref_items, ref_blocks

    # ---------------- continuation ----------------

    def _continuation(self, cfg, vae, audio_vae, latent, width, height, frame_count, log=None):
        """Anchor the previous clip's tail at frame 0 of this one.

        Writes the same conditioning MiniMaxH3AddGuide writes - one keyframe
        holding the whole run as a single encoded block at resolved_frame_index
        0 - rather than calling that node, so the tail can be taken from the
        right end and levelled before the encode.

        Returns (keyframe or None, frames anchored, the frames themselves).
        The frame count is the
        head that comes back in the output and has to come off before the clip
        is joined to its predecessor.
        """
        def _log(*a):
            if log:
                log(*a)

        cont = cfg.get("cont")
        if not cont:
            return None, 0, None

        want = snap_guide_run(cont["frames"])
        if want < MIN_REF_FRAMES:
            raise ValueError(
                "H3 Studio: a continuation needs at least %d frames. Usable runs "
                "are 5, 22, 39, 56, 73." % MIN_REF_FRAMES)
        if want >= frame_count:
            raise ValueError(
                "H3 Studio: continuing from %d frames into a %d frame clip leaves "
                "nothing new. Shorten the window or lengthen the clip."
                % (want, frame_count))

        # a little slack, then snap down and keep the TAIL - a window that lands
        # between source frames can come back one short or one long, and either
        # way the run must finish on the previous clip's last frame
        # by position, so the run really does finish on the clip's last frame -
        # falls back to the timestamp path for anything not at FPS
        frames = _load_video_tail(cont["file"], want + 17, cont["start"])
        if frames is None:
            frames = _load_video_frames(cont["file"], cont["start"], cont["end"], want + 17)
        # Snap LAST. It used to be min(snap(available), want), which returns
        # `want` untouched whenever want is the smaller of the two - and want
        # comes from the browser's window, which on mkv can round to 23 or 24
        # where the file really holds 22. Anything that is not 17k+5 makes the
        # model reserve rows for one more latent frame than the encode produces,
        # which is the constant shape mismatch: same size every time for a given
        # canvas, a different size for every canvas.
        n = snap_guide_run(min(frames.shape[0], want))
        if n < MIN_REF_FRAMES:
            raise ValueError(
                "H3 Studio: %s gave only %d frames in that window; H3 needs %d. "
                "Widen the window." % (cont["file"], frames.shape[0], MIN_REF_FRAMES))
        frames = frames[-n:]

        frames = flatten_exposure(frames, cont["flatten"])
        frames = _resize(frames, width, height, "center")
        keyframe = {"resolved_frame_index": 0, "latent": vae.encode(frames)}

        # Fail fast on a guide the model cannot pack. PackedLayout reserves rows
        # for this block from the TARGET canvas grid - n = vt * (target_h//2 *
        # target_w//2), model.py:380 - yet fills them from patchify_video() of the
        # guide's OWN latent, model.py:539. When those two spatial grids disagree
        # the row counts never differ by "a bit": sampling blows up with
        # "value tensor of shape [A, 96] cannot be broadcast to indexing result of
        # shape [B, 96]" (model.py:706) and nothing in the traceback says why.
        # Compare the two grids here, while the canvas is still in hand, so the
        # user gets one readable sentence instead of a seven-times row count.
        try:
            kf = keyframe["latent"]
            tgt = latent["samples"].tensors[0]
            kf_h, kf_w = int(kf.shape[3]), int(kf.shape[4])
            tg_h, tg_w = int(tgt.shape[3]), int(tgt.shape[4])
            kf_rows = (kf_h // 2) * (kf_w // 2)
            tg_rows = (tg_h // 2) * (tg_w // 2)
            if kf_rows != tg_rows:
                raise ValueError(
                    "H3 Studio: 续写来源的引导潜空间与目标画布网格不一致——引导 %s"
                    "（空间 %dx%d → patch 行 %d）对目标画布 %dx%d（空间 %dx%d → patch 行 %d）。"
                    "H3 按目标网格预留行数、却按引导自身网格填充，两者不等就是那个 "
                    "cannot be broadcast 的形状错误。请让续写来源与本次画布同分辨率，"
                    "或重载工作流 / 重启 ComfyUI 清掉过期缓存后重试。"
                    % (tuple(kf.shape), kf_h, kf_w, kf_rows,
                       width, height, tg_h, tg_w, tg_rows))
        except ValueError:
            raise
        except Exception as e:
            _log("延续 形状校验跳过 (%s)" % e)

        # The shape mismatch reports live here. The model reserves rows from the
        # SHAPE of this guide latent and then fills them from the same tensor, so
        # if those two disagree the numbers below say by how much - and whether
        # the guide is a legal 17k+5 run in the first place.
        try:
            _log("延续 帧数窗口=%d  画布=%dx%d  引导潜空间=%s  目标潜空间=%s  片段帧数=%d"
                 % (n, width, height, tuple(keyframe["latent"].shape),
                    tuple(latent["samples"].tensors[0].shape), frame_count))
        except Exception as e:
            _log("延续 形状报告不可用 (%s)" % e)

        if cont["audio"]:
            # end=None: to EOF, so the carried sound finishes where the
            # picture does rather than at a duration the browser rounded
            # 上限同样要接上：续写音轨和参考音轨走的是同一个音频 VAE，
            # 一样会先撞显存、再撞 latent_dim=2 的分块回退死路。
            cap = ref_audio_limit_seconds(frame_count)
            track = _load_video_soundtrack(cont["file"], cont["start"], None,
                                           max_seconds=cap)
            if track is None:
                _log("延续 %s 无音轨，仅画面" % cont["file"])
            else:
                _got = _trimmed_duration(track)
                if _got + 1e-6 >= cap:
                    _log("延续 %s 音轨已按上限自动截到 %.1fs"
                         % (os.path.basename(cont["file"]), _got))
                audio_latent, audio_rt = None, 0
                try:
                    audio_latent, audio_rt = self._encode_ref_audio(
                        audio_vae, track, cont["file"], log=_log,
                        cache_key=_ref_audio_cache_key(cont["file"], cont["start"], None, cap))
                except _RefAudioTooShort as e:
                    _log("延续 %s 音轨太短，仅画面（%s）" % (cont["file"], e))
                if audio_rt > 0:
                    # anchored at frame 0, so the whole of this clip's audio grid is
                    # available; crop only if the window somehow overruns it
                    max_rt = int(latent["samples"].tensors[1].shape[-1])
                    if audio_rt > max_rt:
                        audio_latent = audio_latent[..., :max_rt].clone()
                    keyframe["audio_latent"] = audio_latent

        _log("延续 %d 帧取自 %s @ 帧0%s，重叠 %d"
             % (n, cont["file"], "（已压平）" if cont["flatten"] > 0 else "", n))
        return keyframe, n, frames

    def _continuation_latent(self, cfg, vae, cond, latent, width, height,
                             frame_count, log=None):
        """潜空间续写：上一片段的采样潜空间直接切片，复用 Motion Context 算术。

        与 _continuation（文件档）的区别：不走「解码成像素 → 缩放 → 重编码」的
        有损往返，钉住的画面位对位；声音窗口结束于接缝、向后倒推，是真正接上
        而不是重放一段像的。代价：分辨率必须一致（latent 无法缩放），且没有
        像素帧，曝光压平（flatten_exposure）不适用——亮度跳变交给拼接侧的
        「对齐亮度」兜底。

        返回 (挂好钉帧的条件, 剪头帧数)。条件已由 Motion Context 组装完毕
        （含 last_frame 锚保留、钉头内锚丢弃），调用方直接替换，不再追加。
        """
        def _log(*a):
            if log:
                log(*a)

        # 续写开关：潜空间档是否续写，由面板是否「引用」决定——引用了才续写，
        # 没引用（或点了 × 清除）就按第一段处理。这样第一段不会被磁盘上别的
        # 工程留下的存档强行接上，规则也只有一条：接不接，看有没有引用。
        # 引用的具体文件不在这里参与——执行时永远取最新存档，重跑上一段无需
        # 任何手动操作。
        if not (cfg.get("cont") or {}).get("latent_file"):
            _log("延续（潜空间）本片段未引用存档——按第一段处理（不续写）。"
                 "要承接上一段：点续写卡右上角的「引用上次片段」，"
                 "或把档位切回「文件续写」手动选源片。")
            return cond, 0

        if _MC_Node is None:
            raise RuntimeError(
                "H3 Studio: 潜空间续写依赖 _h3mc（Motion Context）组件，但它没有"
                "加载成功。请查看启动日志里的报错，或把续写档位切回「文件」。")
        # 潜空间档承接**上一段**的尾部存档。认段从"猜"改成"记"（20261010）：
        # 上一段跑完收片时，它写出的续拍存档路径已落进它的档案
        # （take_params.cont_saved，见 csglide_chain.adopt_film）—— 下一段
        # 直接按记录取，精确认段。目录里 mtime 最新可能是别的段/别的工程
        # 写的，拿错了就是"自己接自己"，接缝直接撕裂。
        # 没记录的老段退回目录按新旧扫；再不行才拿全局最新。
        latest = None
        prev_clip_id = None
        prev_end = None
        prev_row = None
        try:
            from . import csglide_chain as _chain
        except Exception:
            _chain = None
        chain_ref = cfg.get("chain") or {}
        _name = chain_ref.get("name") or ""
        _cid = chain_ref.get("clip") or ""
        if _chain is not None and _name and _cid:
            try:
                # chain.order 存的是**段落 id 字符串**，不是段落对象
                order = [str(c) for c in
                         (_chain.load_chain(_name).get("order") or [])]
                k = order.index(_cid) if _cid in order else -1
                if k > 0:
                    prev_clip_id = order[k - 1]
                    for r in (_chain.film_summary(_name).get("clips") or []):
                        if r.get("id") == prev_clip_id:
                            prev_end = int(r.get("clip_to") or 0)
                            prev_row = r
                            break
            except Exception:
                prev_clip_id = None
                prev_end = None
        # 承接窗口（20261010bd · 方案 B）：时间轴上给这一段写明了「钉住谁的哪
        # 一头」，就按 pins 走 —— 四种接法（往后接 / 往前接 / 补桥 / 循环）
        # 在这里是同一段代码，区别只在钉子放在头部还是尾部，算术全在
        # csglide_pins 里。pins 优先于面板的「引用」开关：它是用户在时间轴上
        # 明写的指令，不是靠 mtime 猜出来的。
        # 左接缝「断链」：这一段与上一段衔接处**不接续**（用户在时间轴上对这
        # 一段左缘标的 unlink）。语义是头部独立起头 —— 不承接任何段的尾巴：
        #   · 头钉一律去掉（尾钉属右接缝、与上一段无关，保留）；
        #   · 下面也不再走「没有 pins 就默认续写 prev」那条退路；
        #   · 头部承接窗口 trim_frames=0，成片拼接不裁头。
        _pins = []
        _head_unlink = False
        if _chain is not None and _name and _cid:
            try:
                _pins = _chain.pins_of(_name, _cid) or []
            except Exception:
                _pins = []
            try:
                _tr = _chain.transitions_of(_name)
                if str(_cid) in _tr and str(
                        (_tr.get(str(_cid)) or {}).get("type")) == "unlink":
                    _head_unlink = True
            except Exception:
                _head_unlink = False
            if _head_unlink:
                _pins = [p for p in _pins if str(p.get("at")) != "head"]
        if _pins:
            from . import csglide_pins as _pinsmod
            try:
                _srcs = _pinsmod.resolve_sources(
                    _name, _cid, log=_log, drop_head=_head_unlink)
                cond, _h, _t = _pinsmod.build(cond, latent, _srcs,
                                              frame_count, log=_log)
            except Exception as e:
                raise RuntimeError(
                    "H3 Studio: 承接窗口（pins）组装失败 —— %s。"
                    "清掉这一段的承接窗口即可退回「往后接」。" % e)
            # 钉住的那两截交付时要剪掉：头上是上一段末尾的重绘，尾上是后一段
            # 开头的重绘。写进档案，时间轴/保存成片才知道本段新画面是哪一段。
            try:
                _chain.update_clip(_name, _cid,
                                   {"trim_frames": int(_h), "trim_tail": int(_t)})
            except Exception:
                pass
            _log("延续（承接窗口）剪头 %d 帧、剪尾 %d 帧 —— 本段新画面 %d 帧。"
                 % (_h, _t, frame_count - _h - _t))
            return cond, _h
        if _head_unlink:
            # 断链且没有尾钉：整段独立，头部一帧都不承接。
            try:
                _chain.update_clip(_name, _cid, {"trim_frames": 0})
            except Exception:
                pass
            _log("延续：这一段与上一段**断链**（接缝不接续）—— 头部独立起头，"
                 "不承接上一段尾帧，按本段的第一段处理。")
            return cond, 0

        cands = []
        prev_saved = None
        if _chain is not None and _name and prev_clip_id:
            try:
                pm = _chain.load_clip(_name, prev_clip_id) or {}
                tp = (pm.get("take_params") or {}).get(
                    str(pm.get("current_take") or 1)) or {}
                ps = str(tp.get("cont_saved") or "")
                if ps and os.path.isfile(ps):
                    prev_saved = ps
            except Exception:
                prev_saved = None
        if prev_saved:
            cands = [prev_saved]
        elif prev_end is not None:
            cands = _cont_latent_candidates()
            if not cands:
                _log("延续（潜空间）上一段末尾应在第 %d 帧，h3_context 里没有"
                     "接近的存档 —— 先渲染上一段，否则本段按第一段处理（不续写）。"
                     % prev_end)
        if not cands:
            _p = _latest_cont_latent_path()
            cands = [_p] if _p else []

        context_latent = None
        latest = None
        tried = []
        if cands and _MC_Load is None:
            raise RuntimeError(
                "H3 Studio: _h3mc（Motion Context）组件没有加载成功，"
                "无法读取续拍存档。请查看启动日志，或把续写档位切回「文件」。")
        for c in cands:
            lat = _MC_Load().load(c, 1)[0]
            sv = _mc_streams(lat)[0]
            if sv.ndim == 4:
                sv = sv.unsqueeze(0)
            w0, h0 = int(sv.shape[4]) * 16, int(sv.shape[3]) * 16
            if (w0, h0) == (width, height):
                context_latent, latest = lat, c
                break
            tried.append("%s 是 %dx%d" % (os.path.basename(c), w0, h0))
        if context_latent is None and tried:
            # 分辨率提前用中文拦下，并给出两条能立刻执行的路。
            # 上一段当前版自己的画布一并摆出来（20261010 版本绑参数）：它就是
            # 该对齐的那个数 —— 报错让人对不上号，比报错本身更耽误事。
            pv = ""
            if prev_row and prev_row.get("w") and prev_row.get("h"):
                pv = ("上一段当前版（第 %s 版）的画布是 %dx%d —— 潜空间续写"
                      "要对齐的就是它。"
                      % (prev_row.get("take"), prev_row["w"], prev_row["h"]))
            raise ValueError(
                "H3 Studio: 上一段的续拍存档与本次画布分辨率不一致（%s；本次画布 "
                "%dx%d）。%s潜空间无法缩放 —— 要么把上一段按 %dx%d 重新生成一次"
                "（推荐，保住潜空间档的无缝接续），要么把续写档位切回「文件」"
                "（文件档走像素，允许缩放）。"
                % ("；".join(tried[:3]), width, height, pv, width, height))
        if latest is not None:
            _log("延续（潜空间）承接存档：%s"
                 % os.path.relpath(latest, folder_paths.get_output_directory()))
        if context_latent is None:
            # 引用了但磁盘上没有存档：按第一段处理，说清楚差别。
            _log("延续（潜空间）没找到存档文件——本片段按第一段处理（无续写）。"
                 "工作流里是否接了「H3 续拍存档」（采样输出 → 存档，编号 0）？")
            return cond, 0

        # 帧数吸附到潜空间整步（124…22,5,1，向下取）。面板窗口值可能不是整步，
        # 吸附后语义不变：钉住的总是尾部。
        n = max(1, int((cfg.get("cont") or {}).get("frames") or 22))
        n = max(g for g in _MC_RUN_GRID if g <= n)

        cond, trim = _MC_Node().apply(
            conditioning=cond, vae=vae, latent=latent,
            context_length=n, audio_context_length=24,
            context_latent=context_latent)
        _log("延续（潜空间）钉住 %d 帧 + 尾部声音（默认 1 秒），剪头 %d 帧。"
             "本档位始终延续声音——这正是它存在的意义；「延续声音」开关只对文件档生效。"
             % (n, trim))
        return cond, trim

    # ---------------- 段落档案 ----------------

    @staticmethod
    def _whole_film_path(cfg):
        """时间轴上的「当前整片」路径：把所有已可播段按净长拼成的**完整片**
        （档案目录下的 _preview.mp4），与监视器连播 / 「保存成片」 / 前端「推送
        整片」是同一条片子。

        旧实现取「最后一段的 film」，但实测 clip_000x.mp4 只是**单段**（约 10 秒，
        含承接重叠），并不是「到这一段为止的累积片」——真正的全轴拼接只存在于
        preview_film 产出的 _preview.mp4。这里 ensure=True 现拼 / 取缓存：段内容
        或顺序没变就复用（流复制 + 一次重编码，几秒，带签名缓存），变了才重拼。
        没有片名 / 一段都没出 → 返回 ""。
        """
        chain = cfg.get("chain") or {}
        name = str(chain.get("name") or "")
        if not name:
            return ""
        try:
            from . import csglide_chain as _chain
            res = _chain.preview_film(name, ensure=True)
        except Exception:
            return ""
        if res and res.get("ok"):
            fp = res.get("path")
            if fp and os.path.isfile(fp):
                return fp
        return ""

    @staticmethod
    def _register_clip(cfg, frame_count, log):
        """把本段登记进段落档案，返回段落 id（不归档则返回 ""）。

        片名为空 → 完全不碰磁盘，导演台照旧单段工作。
        段落 id 为空 → 新建一段并设为该片「当前段」；下游 📦 段落存档 节点
        读 chain.json 的 current 就能自动对上，用户不用手填 id。
        指定了 id → 就地更新（时长、提示词、参考），重抽就是走这条路。
        """
        chain_ref = cfg.get("chain") or {}
        name = chain_ref.get("name") or ""
        if not name:
            return ""
        try:
            from . import csglide_chain as _chain
        except Exception as e:
            log("【段落档案】模块未就绪（%s），本次不归档" % e)
            return ""

        cid = chain_ref.get("clip") or ""
        shot = chain_ref.get("shot") or ""
        refs = [s.get("file") for s in (cfg.get("images") or [])
                if isinstance(s, dict) and s.get("file")]
        # 这一段**实际**用多少帧承接到上一段。写进档案而不是只留在面板里：
        # 重抽这一段时必须用回同一个窗口，否则接缝换了个接法、下一段跟着变；
        # 时间轴上也要看得见"这一段当初是怎么接的"。
        # 0 = 没有上一段（第 1 段 / 与上一段断开），那就别写 —— 写 0 会让
        # 时间轴把它显示成一个数字，而它压根没有续接窗口。
        head = cont_head_frames(cfg, frame_count)
        win = {"trim_frames": int(head)} if head > 0 else {}
        try:
            # 记下「最近登记过的片名」：📦/📤 的片名留空时会读它，这样用户只
            # 需要在导演台工程面板里填一次工程名，两个节点就不必再手打一遍。
            _chain.mark_last(name)
            # 认段的优先级：显式 clip id → 分段身份 shot id → 新建。
            # 中间那步是「重抽同一分段」的关键：clip id 前端拿不到（端口不给），
            # 只有分段自身的 uid 能在多次运行之间保持稳定。
            if not (cid and _chain.load_clip(name, cid) is not None):
                cid = ""
                if shot:
                    for c in (_chain.summary(name).get("clips") or []):
                        if c.get("shot") == shot:
                            cid = c.get("id") or ""
                            break
            if cid:
                _chain.update_clip(name, cid, dict({
                    "frames": int(frame_count),
                    "prompt_seg": cfg.get("prompt") or "",
                    "refs": refs,
                    "shot": shot,
                    # 重抽档位 / 锚点选法落进档案：🧷 钉帧节点执行时才看得到
                    # 潜空间，而档位是**登记这一刻**决定的，档案是唯一能把它
                    # 交到执行侧的地方。
                    "redo_mode": chain_ref.get("redo_mode") or "保留动作",
                    "redo_anchor": chain_ref.get("redo_anchor") or "等间隔",
                    "pin_head": int(chain_ref.get("pin_head") or 0),
                    "pin_tail": int(chain_ref.get("pin_tail") if chain_ref.get("pin_tail") is not None else 22),
                }, **win))
            else:
                cid = _chain.add_clip(name, frames=int(frame_count),
                                      prompt_seg=cfg.get("prompt") or "",
                                      refs=refs, shot=shot,
                                      redo_mode=chain_ref.get("redo_mode") or "保留动作",
                                      redo_anchor=chain_ref.get("redo_anchor") or "等间隔",
                                      pin_head=int(chain_ref.get("pin_head") or 0),
                                      pin_tail=int(chain_ref.get("pin_tail") if chain_ref.get("pin_tail") is not None else 22),
                                      **win)
            # 当前段必须挪到刚登记的这一段：下游 📦段落存档 节点「段落留空」
            # 就是读 chain.json 的 current 来对号的。重抽走的是 update_clip
            # 那条路，而它不会挪 current（只有 add_clip 会），漏了这一步就会
            # 把这次的结果存到**上一段**的档案里 —— 看着跑通了，其实存错了
            # 地方，而且这种错最难查。
            if cid:
                _chain.set_current(name, cid)
                # 尾部没钉住（「完整重拍」档位 / 保留尾帧为 0）→ 本段末尾与
                # 旧版对不上了，下一段开头的承接帧悬空 —— 标下一段重抽，
                # 别让两段连接处的错位只靠眼睛发现。「保留动作 / 只换画质」
                # 的尾部窗口有钉帧，尾部一致，下一段不用重跑，不标。
                if (chain_ref.get("redo_mode") or "保留动作") == "完整重拍" \
                        or int(chain_ref.get("pin_tail") or 0) <= 0:
                    _order = _chain.load_chain(name).get("order") or []
                    if cid in _order:
                        _k = _order.index(cid)
                        _nxt = _order[_k + 1] if _k + 1 < len(_order) else None
                        # 同样只认「接本段的那一段」：补桥 / 往前接的下一段
                        # 头钉钉的不是本段，本段尾部变不变与它无关，标它重抽
                        # 是冤枉的（它根本没接本段）。
                        if _nxt and _chain.head_parent(name, _nxt) == str(cid):
                            _chain.set_pending(name, _nxt, "redo")
                            log("【段落档案】本段尾部未钉住 —— 下一段（%s）已标为「要重抽」" % _nxt)
            info = _chain.summary(name)
            # 本段序号要按它在档案里的**位置**算，不能用总段数顶替——两个参数
            # 都传 len(clips) 时会永远输出「第 N 段 / 共 N 段」，看不出这是第几段。
            order = [c.get("id") for c in (info.get("clips") or [])]
            pos = (order.index(cid) + 1) if cid in order else len(order)
            # 全片长度报**净长**（film_summary 口径，与时间轴一致）—— summary 的
            # 原始 sum 含各段承接重叠，比轴上多出所有重叠帧，日志里写一个、轴上
            # 显示另一个，最容易被当成"总时长算错了"。
            try:
                _total = int(_chain.film_summary(name).get("total_frames") or 0)
            except Exception:
                _total = int(info.get("total_frames") or 0)
            log("【段落档案】片「%s」· 本段 %s（第 %d 段 / 共 %d 段 · 全片 %d 帧）"
                % (name, cid, pos, len(order), _total))
        except Exception as e:
            log("【段落档案】登记失败（%s: %s），出图不受影响" % (type(e).__name__, e))
            return ""
        return cid

    # ---------------- 钉帧（重抽这一段时，别牵连后面）----------------

    @staticmethod
    def _attach_pin(latent, chain_ref, cid, log):
        """重抽已有段时，把钉帧挂到本段的潜空间上。

        判据只有一条：**这一段以前跑过、有旧版潜空间可对照**。那就是「重抽」，
        也正是「只在重抽时生效」的落地方式 —— 不必让调用方传一个 is_redo 标志，
        因为「有没有旧版」本身就把这件事说完了，而且是磁盘上的事实，不会说谎。

        全新的一段不钉：没有旧版可参照，钉骨架只会把它冻住；而新段的连贯性
        本来由 H3 原生的潜空间续写负责。钉帧解决的是另一个问题 ——
        「改了这一段，怎么保证后面各段不用跟着重跑」。

        两件事一起做，因为它们管的是不同的东西：
          · noise_mask（方案一）—— 采样中把钉住的帧锁死，让模型照着走。
            它依赖采样器原样收下遮罩；SelfLift 会在采样中途换 latent 网格，
            遮罩的参照物就不再是喂进 KSampler 的原稿，所以这一路只是 best effort
            （真机验过：带 SelfLift 时不成立 —— 那时要绕过 SelfLift 才有效）。
          · bz_pin 元数据（方案二）—— 采样器补丁跑完，把**头尾帧**从本段旧版
            原样拷回。纯张量拷贝，不经过采样器，与 SelfLift 无关。
            「后面各段不用重跑」这条性质由它保证 —— 遮罩成没成都不影响。
        """
        if not latent or not cid:
            return latent
        name = str(chain_ref.get("name") or "")
        if not name:
            return latent
        try:
            from . import csglide_chain as _chain
            from .banzhang_h3_pin import REDO_STRIDE, build_pin_mask
        except Exception as e:
            log("【钉帧】模块未就绪（%s），本次不挂遮罩也不回填" % e)
            return latent

        # 旧版潜空间 = 本段的上一版。它同时是三个问题的答案：是不是重抽、
        # 采样后尾帧以谁为准、下一段的引导头会不会变。
        try:
            meta = _chain.load_clip(name, cid) or {}
            take = int(meta.get("current_take") or 0)
            old_path = _chain.clip_latent(name, cid, take) if take > 0 else ""
        except Exception as e:
            log("【钉帧】读不出本段档案（%s）—— 本次不挂遮罩也不回填。" % e)
            return latent
        if not old_path or not os.path.isfile(old_path):
            log("【钉帧】本段还没有旧版潜空间 —— 这是首次生成，不钉帧"
                "（新段的连贯性由 H3 原生续写负责）。")
            return latent

        head = int(chain_ref.get("pin_head") or 0)
        tail = int(chain_ref.get("pin_tail")
                   if chain_ref.get("pin_tail") is not None else 22)
        mode = str(chain_ref.get("redo_mode") or "保留动作")
        if mode == "完整重拍":
            # 整段从零重拍：骨架、头尾全不钉，遮罩和回填都不挂。尾部对不上
            # 旧版是这一档的代价（也是它的含义），后面各段要跟着重跑。
            log("【钉帧】档位「完整重拍」—— 整段从零重拍，不钉帧；"
                "后面各段要跟着重跑。")
            return latent
        # ---- 钉尾至少覆盖「下一段的承接窗口」（20261010 修接缝跳变）----
        # 接缝宽度定义在接缝**右边**的那一段：下一段开头的 trim 帧，是照本段
        # 尾部那几帧重绘出来的引导头。本段尾部只要有一帧变了，下一段那个头
        # 就对不上 —— 接缝处必然跳一下，而且这种跳变是"内容对不上"，续接
        # 合成的 S 渐变切只能抹平一点点，抹不掉动作错位。
        # 面板上的「保留尾部」是**重抽力度**旋钮（钉得越多，本段能改的越少），
        # 它可以更大，但绝不能小于下一段的承接窗口 —— 小于就是"钉了也白钉"：
        # 钉住的 22 帧没变，可第 23~39 帧变了，下一段照样接不上。
        next_trim = 0
        try:
            rows = (_chain.film_summary(name).get("clips") or [])
            for _i, _r in enumerate(rows):
                if str(_r.get("id")) == str(cid) and _i + 1 < len(rows):
                    _nxt = rows[_i + 1]
                    # 只有**下一段的头钉钉的是本段**时，它的 trim 才代表
                    # 「要接本段的尾部」。补桥的下一段头钉钉的是别人 —— 那
                    # 个 trim 与本段无关，照它抬钉尾就是凭空少掉一截能改的
                    # 画面，而且不报错，只表现为「重抽了半天中间没怎么变」。
                    # 见 csglide_chain.head_parent。
                    _nt = int(_nxt.get("trim_frames") or 0)
                    # trim 为 0 就不用问了：没有承接窗口 = 没接任何人的尾巴，
                    # 本段尾部无须为它抬。（顺带绕开 pins 空列表的歧义 ——
                    # 「从来没写过」与「明确设为独立」在 pins 上长得一样。）
                    if _nt > 0 and _chain.head_parent(name, _nxt.get("id")) == str(cid):
                        next_trim = _nt
                    break
        except Exception as e:
            log("【钉帧】读不出下一段的承接窗口（%s: %s）—— 钉尾按面板值走。"
                % (type(e).__name__, e))
        if next_trim > tail:
            log("【钉帧】钉尾 %d → %d：下一段要接 %d 帧，少于这个数接缝就会跳。"
                % (tail, next_trim, next_trim))
            tail = next_trim

        # 本段自己尾部也钉着（往前接 / 补桥）时，钉尾窗口有一部分落在**要被
        # 剪掉**的那截上（末尾 trim_tail 帧是后一段头的重绘，交付时剪掉）。
        # 钉住它是白钉 —— 不会出错（剪掉就没了），但本段能改的范围平白少了。
        # 不自动减，因为「面板填的是重抽力度」这个语义不能让别的参数悄悄改掉；
        # 说清楚，让人自己决定要不要调小。
        try:
            _tt = int((_chain.load_clip(name, cid) or {}).get("trim_tail") or 0)
        except Exception:
            _tt = 0
        if _tt > 0 and tail > 0:
            log("【钉帧】本段末尾有 %d 帧是「钉住后一段的头」的重绘（交付时剪掉）"
                " —— 保留尾部 %d 帧里有一部分落在那截上，等于白钉；"
                "想让本段能改的多一点，就把「保留尾部」调小。" % (_tt, tail))

        anchor = ("motion"
                  if str(chain_ref.get("redo_anchor") or "") == "按运动强度" else "even")
        stride = int(REDO_STRIDE.get(mode, REDO_STRIDE.get("保留动作", 22)))
        if head <= 0 and tail <= 0 and stride <= 0:
            log("【钉帧】头尾都不钉、档位也是「重画」—— 这一段不做钉帧。")
            return latent

        out = dict(latent)
        try:
            out["noise_mask"] = build_pin_mask(latent, head, tail, stride, False, anchor)
        except Exception as e:
            log("【钉帧】遮罩没挂上（%s）—— 采样后的尾帧回填不受影响，照做。" % e)
        out["bz_pin"] = {"name": name, "cid": cid, "head": head, "tail": tail}

        log("【钉帧】重抽本段（旧版第 %d 版）：钉住头 %d / 尾 %d 帧，档位「%s」"
            "（骨架间隔 %d · 锚点%s）\n"
            "  · 方案 A 遮罩：已挂，让模型采样时看得见锚点（best effort ——"
            " SelfLift 会把遮罩按 1.5× 网格重采样，开着时它只作引导，不保证逐位）\n"
            "  · 方案 B 回填：采样后按旧版**逐位拷回**头尾帧，与 SelfLift 无关 ——"
            "「后面各段不用重跑」这条承诺由它保证"
            % (take, head, tail, mode, stride,
               "按运动强度" if anchor == "motion" else "等间隔"))
        return out

    # ---------------- main ----------------

    def build(self, clip, vae, h3_data, audio_vae=None, first_frame=None, last_frame=None,
              refs=None, prompt_text=None, h3_override=None, prompt=None, unique_id=None):
        try:
            return self._do_build(clip, vae, h3_data, audio_vae, first_frame, last_frame,
                                  refs, prompt_text, h3_override)
        except Exception as _e:
            print("[H3 导演台] 运行出错：%s: %s" % (type(_e).__name__, _e))
            raise
        finally:
            # 无论成功、报错还是被中断，都用完就还。放在 finally 里是因为
            # 「出错时恰好是最该把显存还回去的时候」——OOM 往往就发生在节点内部。
            _release_clip_when_done(clip, prompt, unique_id)

    def _do_build(self, clip, vae, h3_data, audio_vae=None, first_frame=None, last_frame=None,
                  refs=None, prompt_text=None, h3_override=None):
        # AI 制片中枢：外接 h3_data 接线即整体替换（断线/空串恢复面板原文）。
        if h3_override is not None and str(h3_override).strip():
            h3_data = h3_override
        cfg = parse_h3_data(h3_data)
        _merge_rack_refs(cfg, refs)
        # 外接提示词（模式 A）：接线即整体替换。外部文本里可直接写 @图片N 等
        # 标签——按需过滤与 <Picture i> 序号重排对它透明生效。
        if prompt_text is not None:
            cfg["prompt"] = str(prompt_text)
        # 一处收口：面板存的老写法与外接的官方英文写法都在这里归一成 @标签，
        # 否则按需过滤认不出 <Picture 1>，参考素材根本不会送进模型。
        # 别名先于归一化展开：名为 "image 3.png" 的素材若走归一化会被英文
        # 规则抢成 @图片1，指向错误槽位。
        # 音频引用不查表：<Audio N> 按第 N 个独立音频理解，那一格空着就保留
        # 原文 —— 宁可不生效，也不悄悄指到别处。
        #
        # 「别名模式」开关全链路唯一权威在文案台（desk.alias），经 h3_data.alias
        # 透传到这里（前端提交时由 bz_project_store 注入）。关闭时只认显式 @图片N
        # 写法 —— 剧本里裸写角色名 / 文件名主干不再被悄悄当成引用发图，与文案台
        # chip 高亮、编辑器提示严格同口径（所见即所送）。写法归一化（<Picture N>、
        # 图片N、image 1 等收敛成 @图片N）不属于别名，任何时候都照做，不受开关影响。
        # 面板模式（没接文案台，h3_data 里没这字段）默认 True，保持旧行为。
        _amap, _strict = alias_table(cfg)
        _prompt = cfg["prompt"]
        if cfg.get("alias", True):
            _prompt = expand_aliases(_prompt, _amap, _strict)
        cfg["prompt"] = normalize_refs(
            _prompt, cfg.get("videos"), cfg.get("audios"))

        logs = []

        def log(msg):
            line = str(msg)
            logs.append(line)
            print("[H3 导演台] " + line)

        if prompt_text is not None:
            log("【提示词】外接模式：prompt_text 接线，本次执行使用外部文本（面板提示词未参与）")

        def finish(cond, hd_cond, hd_upgraded):
            """Shared tail for both modes: attach the continuation, if any.

            hd_cond 是「放大重绘」端口（二采三采共用）的条件，统一在这里拼到
            末尾——两种模式都必须出这个端口，由调用方决定填重编码后的条件还是
            基础条件自身。

            hd_upgraded=False 时在潜空间上打「未升级」标记：这份条件与基础条件
            逐字相同，下游二采三采据此自动直通，不接线也不用改任何设置。
            """
            cont_mode = (cfg.get("cont") or {}).get("mode") or "file"
            guide_frames = torch.zeros((1, 64, 64, 3))
            if cont_mode == "latent":
                # 潜空间续写：Motion Context 算术在内部把钉帧与音频窗口挂进条件，
                # 返回的条件直接替换，不走下面的 keyframe 追加。
                cond, overlap = self._continuation_latent(
                    cfg, vae, cond, latent, width, height, frame_count, log=log)
            else:
                # 写了承接窗口却停在「文件」档位：pins 走的是潜空间，钉不住。
                # 静默按文件档跑出来的是「往后接」，与轴上标的接法对不上 ——
                # 这正是最难查的那种错位，必须说出来。
                try:
                    from . import csglide_chain as _c
                    _cr = cfg.get("chain") or {}
                    if _cr.get("name") and _cr.get("clip") and \
                            (_c.pins_of(_cr["name"], _cr["clip"]) or []):
                        log("⚠ 这一段在轴上标了承接窗口（钉住别的段），但续写档位"
                            "是「文件」—— 承接窗口只有**潜空间档**钉得住。"
                            "把档位切到「潜空间」，否则本次按文件档的往后接跑。")
                except Exception:
                    pass
                guide, overlap, guide_frames = self._continuation(
                    cfg, vae, audio_vae, latent, width, height, frame_count, log=log)
                # exactly what was encoded, so the anchored run can be LOOKED AT
                # instead of inferred from the result of a six minute sample
                if guide_frames is None:
                    guide_frames = torch.zeros((1, 64, 64, 3))
                if guide is not None:
                    keyframes = list(cond[0][1].get("minimax_keyframes", []))
                    keyframes.append(guide)
                    cond = node_helpers.conditioning_set_values(
                        cond, {"minimax_keyframes": keyframes})
            # 说明：原本这里有「源视频」端口——文件档 = 续写文件本身、潜空间档
            # 实时抓最新成片，供「Glide 续接合成」拼长片。现已随端口一并移除：
            # 续接合成（Join）前端早已改成「源视频不做端口、路径面板自填或自动
            # 接片」，不再依赖导演台这条输出；续写算术本身也从没读过它。
            log("")
            log("【输出】正面条件 / 潜空间 %d 帧 / %d×%d / %.2f 秒%s"
                % (frame_count, width, height, seconds,
                   (" / 延续重叠 %d 帧" % overlap) if overlap else ""))
            if not overlap:
                # 没有续写就没有可展示的引导帧，那个口出的是 64×64 的黑色占位。
                # 不写出来，下游预览看到一张小黑图会以为续写喂错了东西。
                log("【输出】「引导帧」口本次是 64×64 的空占位——这一段没有续写来源")
            if not hd_upgraded:
                log("【放大重绘】条件未升级（与一采逐字相同）——已给潜空间打标记，"
                    "下游二采/三采节点会自动直通，不跑那两次重绘")
            # 整片口：读档案里最后一段在磁盘上的累积成片，包成 VIDEO。还没渲染
            # 出过整片 / 没填片名时为 None —— 下游视频合并的 VIDEO 口是 optional，
            # 收到 None 会退回它自己的图像分支，不会报错。
            whole_path = self._whole_film_path(cfg)
            whole_film = _video_from_file(whole_path)
            if whole_film is not None:
                log("【输出】「整片」口（完整全片）：%s" % whole_path)
            else:
                log("【输出】「整片」口：当前还没有成片（先渲染 / 导出过整片）")
            return (cond, hd_cond,
                    self._attach_pin(_mark_hd_upgraded(latent, hd_upgraded),
                                     cfg.get("chain") or {}, clip_id, log),
                    frame_count, seconds, overlap, guide_frames, whole_film)

        width = max(CANVAS_MULTIPLE, (cfg["width"] // CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        height = max(CANVAS_MULTIPLE, (cfg["height"] // CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
        latent, frame_count = _empty_av_latent(width, height, cfg["length"])
        seconds = frame_count / FPS

        log("—— H3 导演台 · 运行日志 ——")
        log("模式：%s" % ("fl2va（首/尾帧）" if cfg["mode"] == "fl2va" else "ref2va（多参考）"))
        log("画布：%d × %d" % (width, height))
        log("时长：%d 帧 ≈ %.2f 秒" % (frame_count, seconds))

        # 段落档案登记：这一段归到哪条片子、是第几段。
        # 放在这里是因为 frame_count 刚算出来，且两种模式都要走（fl2va 中途
        # return 也在此之前）。登记失败只告警——档案是辅助账，不该让出图挂掉。
        clip_id = self._register_clip(cfg, frame_count, log)

        # 语义桥（Semantic Bridge）：跨架构语义蒸馏适配器。
        # 全量版对整个条件序列（提示词+参考锚点+视觉块）一起平移，图文语义一致；
        # 掩码版只动文本 token（按 minimax_token_tags），视觉块原样保留、参考画面
        # 更稳。必须定义在这里（两个模式的公共前置区）：条件的出口有两个（fl2va
        # 会中途 return），放进任一分支都会让另一条模式整个绕过它。
        bridge_alpha = cfg["bridge_alpha"]
        bridge_adapter = cfg["bridge_adapter"]
        bridge_mode = cfg["bridge_mode"]
        bridge_skip = False         # 本次运行已确认不可用，短路避免重复告警/加载

        def apply_bridge(c):
            nonlocal bridge_skip
            if bridge_alpha <= 0.0 or not bridge_adapter or bridge_skip:
                return c
            # 模块加载失败与适配器缺失都必须留下痕迹：静默 return 会让面板上的
            # 强度看起来生效、实际什么都没做。
            try:
                from . import csglide_bridge as _bridge
            except Exception as e:
                log("【语义桥】加载语义桥模块失败（%s），本次已跳过" % e)
                bridge_skip = True
                return c
            # 适配器缺失：告警一次并短路，别每次都进 _load_student 再抛错。
            try:
                _bridge.adapter_path(bridge_adapter)
            except Exception:
                log("【语义桥】未找到适配器「%s」——请放入 %s，本次已跳过"
                    % (bridge_adapter, _bridge.ADAPTER_DIR))
                bridge_skip = True
                return c
            mask = None
            if bridge_mode == "text":
                mask = _bridge.text_token_mask(c)
                if mask is None:
                    # 取不到 minimax_token_tags（如上游改了 tokenizer 接口）时
                    # 退回全量，别静默当掩码成功——否则面板显示掩码、实际全量。
                    log("【语义桥】掩码版未取到 token 标签，本次退回全量")
            try:
                return _bridge.apply_distilled_bridge(
                    c, bridge_adapter, bridge_alpha, "per_token", mask)
            except Exception as e:
                log("【语义桥】跳过（%s）" % e)
                return c

        if cfg["mode"] == "fl2va":
            first = first_frame if first_frame is not None else (
                _load_image(cfg["first"]["file"]) if cfg["first"] else None)
            last = last_frame if last_frame is not None else (
                _load_image(cfg["last"]["file"]) if cfg["last"] else None)

            tags, _ = build_tag_map(cfg, first is not None, last is not None)
            prompt = transcribe_prompt(cfg["prompt"], tags, ["@first", "@last"])

            log("")
            log("【关键帧】首帧=%s  尾帧=%s"
                % ("有" if first is not None else "无", "有" if last is not None else "无"))
            if tags:
                log("【转译】" + "  ".join("%s→%s" % (k, v) for k, v in tags.items()))

            # 续写把上一片段的尾部钉在帧 0..n-1。首帧锚自动落到接缝处，不再与
            # 续写块抢同一个时间坐标——同坐标的后果是「谁也不像」或「被整块丢弃」，
            # 没有第二种合理结果，所以自动错开、不给开关。没有续写时 head=0，
            # 与改之前逐字一致。
            head = cont_head_frames(cfg, frame_count)
            if last is not None:
                head = min(head, max(0, frame_count - 2))

            images, keyframes = [], []
            if first is not None:
                img = _resize(first[:1], width, height, "disabled")   # geometry anchor: stretch
                images.append(img)
                keyframes.append({"resolved_frame_index": head, "image": img})
            if last is not None:
                img = _resize(last[:1], width, height, "center")      # follower: cover-crop
                images.append(img)
                keyframes.append({"resolved_frame_index": frame_count - 1, "image": img})

            if head:
                log("【关键帧】首帧锚自动后移到第 %d 帧——续写钉住了帧 0..%d，"
                    "两个锚落在同一坐标会互相抵消（潜空间档会被整块丢弃）。"
                    % (head, head - 1))
            for cap, src, how in (("首帧", first, "拉伸对齐画布"),
                                  ("末帧", last, "居中裁切")):
                if src is None:
                    continue
                sw, sh = int(src.shape[2]), int(src.shape[1])
                if abs(sw * height - sh * width) > 0.02 * sh * width:
                    log("【关键帧】%s 源图 %dx%d 与画布 %dx%d 比例不一致（按%s）——"
                        "画面会变形，想要不变形请先把图裁成画布比例"
                        % (cap, sw, sh, width, height, how))

            tokens = clip.tokenize(prompt, images=images)
            cond = clip.encode_from_tokens_scheduled(tokens)
            if keyframes:
                for kf in keyframes:
                    kf["latent"] = vae.encode(kf.pop("image"))
                # minimax_frame_count 曾一并写在这里，但核心从不读它（全仓库无
                # 消费方），留着只会让人以为首尾帧锚带着帧数约束——删掉。
                cond = node_helpers.conditioning_set_values(cond, {
                    "minimax_keyframes": keyframes,
                })
            # fl2va 没有参考素材可重编码，放大重绘条件等于基础条件；关键帧在
            # 二采/三采被放大画布时由 PackedLayout 自愈钩子按需重采样（见
            # install_keyframe_autofit），不需要用户填任何倍数。
            #
            # 语义桥在这里才接进来：这是 fl2va 唯一的出口，漏了它就整个绕过。
            # 但首尾帧模式下必须让路——桥会把 <Picture i> 锚点（文本侧）乃至
            # 整段视觉块（全量侧）平移走，首尾帧约束随之漂移。自动跳过而不是
            # 加开关：「守住关键帧」与「风格迁移」冲突时前者优先，没有第二个
            # 合理答案。
            if bridge_alpha > 0.0:
                log("【语义桥】首尾帧模式已自动跳过（强度 %.2f 本次不生效）——"
                    "语义桥会平移 <Picture i> 锚点与视觉块，首尾帧约束会随之漂移。"
                    "要启用语义桥请切到「多参考」模式。" % bridge_alpha)
            else:
                cond = apply_bridge(cond)
            log("【放大重绘条件】首尾帧模式：端口与基础条件完全相同"
                "（关键帧按各阶段画布自动重采样，无需手动匹配倍数）")
            return finish(cond, cond, False)

        # ---- ref2va ----
        # 首尾帧只在 fl2va 分支里读。面板槽位也好、first_frame / last_frame
        # 端口也好，多参考模式下都是静默忽略——用户看到卡片上摆着图、画布上
        # 也接了线，却完全没有约束，且没有任何提示。这里把「摆了却用不上」
        # 直接说出来。
        if (first_frame is not None or last_frame is not None
                or cfg.get("first") or cfg.get("last")):
            log("【关键帧】⚠ 当前是「多参考」模式，首尾帧不参与生成——面板里的"
                "首帧/末帧与 first_frame / last_frame 端口都只在「首/尾帧」模式下"
                "生效。要按首尾帧约束，请把模式切到「首/尾帧」（并加载 "
                "minimax_h3_fl2va 开头的权重）。")

        kept, over = _filter_refs_by_prompt(cfg)

        def _known(fmt, base, arr, used):
            top = max(base, len(arr), max(used) if used else 0)
            return [fmt % i for i in range(1, top + 1)]

        known = (_known("@图片%d", MAX_IMAGES, cfg["images"], kept["images"])
                 + _known("@视频%d", MAX_VIDEOS, cfg["videos"], kept["videos"])
                 + _known("@视频音频%d", MAX_VIDEOS, cfg["videos"], kept["videos"])
                 + _known("@音频%d", MAX_AUDIOS, cfg["audios"], kept["audios"]))
        tags, presentation = build_tag_map(cfg, False, False)
        prompt = transcribe_prompt(cfg["prompt"], tags, known)

        log("")
        if any(kept.values()):
            label = {"images": "图片", "videos": "视频", "audios": "音频"}
            parts = [",".join("@%s%d" % (label[k], n) for n in v)
                     for k, v in kept.items() if v]
            log("【参考素材】按需发送：" + "  ".join(parts))
            # 官方交互代码图片侧上限是 <Picture 1>~<Picture 9>。别名自动展开
            # 让「一段里提到几个角色就送几张」变得极易触发；超出的引用已在
            # _filter_refs_by_prompt 里截断，这里把截断如实告诉用户。
            for k, n in over.items():
                cap = {"images": MAX_IMAGES, "videos": MAX_VIDEOS, "audios": MAX_AUDIOS}[k]
                log("【参考素材】⚠ %s 引用了 %d 个，超出上限 %d——只保留编号最小的 %d 个，"
                    "其余已从提示词移除" % (label[k], n, cap, cap))
        else:
            log("【参考素材】提示词未引用任何 @标签，本次不送参考")
        ref_items, ref_blocks = self._refs(cfg, vae, audio_vae, width, height, frame_count, log=log)

        if tags:
            log("")
            log("【提示词转译】")
            for k in sorted(tags):
                log("  %s → %s" % (k, tags[k]))

        tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
        cond = clip.encode_from_tokens_scheduled(tokens)
        if ref_blocks:
            cond = node_helpers.conditioning_set_values(cond, {"minimax_refs": ref_blocks})
        cond = apply_bridge(cond)

        def cond_at(scale):
            """按给定参考档位倍数重建一份条件。

            提示词两份逐字相同，变的是参考素材那两条通道：字典里的 minimax_refs
            （VAE 潜空间，DiT 直接看）与文本嵌入里的视觉 token。两条都重建意味着
            32B 视觉编码器要再跑一遍——视觉 token 数随档位平方增长，这是放大重绘
            档位的主要代价。
            """
            items, blocks = self._refs(cfg, vae, audio_vae, width, height, frame_count,
                                       log=log, ref_scale=scale)
            toks = clip.tokenize(prompt, minimax_ref_items=items)
            c = clip.encode_from_tokens_scheduled(toks)
            if blocks:
                c = node_helpers.conditioning_set_values(c, {"minimax_refs": blocks})
            return apply_bridge(c)

        # 二采与三采共用同一份条件：两级放大重绘吃的是同一套参考口径，分量没有
        # 依据，只会让用户多接一根线。倍数 1 = 跟随主档位，直接复用基础条件对象，
        # 零额外计算（与加此功能前完全一致）；没有参考素材时也无从重编码。
        # 三种「端口 = 基础条件复制品」的情况都明说——之前是静默的，用户以为
        # 端口在起作用，实际二采拿到的与一采一字不差。
        m = cfg["ref_scale_hd"]
        if not ref_blocks:
            hd_cond = cond
            hd_ok = False
            log("【放大重绘条件】没有参考素材可重编码 —— 端口与基础条件完全相同")
        elif m <= 1.0 + 1e-6:
            hd_cond = cond
            hd_ok = False
            log("【放大重绘条件】倍数 1.0 —— 端口与基础条件完全相同"
                "（要生效：倍数调到 1.5/2，且参考源文件分辨率 ≥ 二采后的目标）")
        else:
            target = _ref_target_long(cfg["ref_image_size"], max(width, height), m)
            log("")
            log("【放大重绘条件】参考档位 %.2f×（主档位 %s → 目标长边 %.0f）—— 二采三采共用，重编码参考素材"
                % (m, cfg["ref_image_size"], target))
            # 参考只缩不放：源文件不够大时倍数被上限截住，这是二采阶段
            # 「跟参考不一致」最常见的隐形原因，必须点名让用户能自查。
            log("【放大重绘条件】注意：参考只缩不放 —— 若上方素材「编码 WxH」小于目标长边，"
                "说明源文件分辨率不够，倍数只生效到源素材上限")
            hd_cond = cond_at(m)
            hd_ok = True
        return finish(cond, hd_cond, hd_ok)

    @classmethod
    def IS_CHANGED(cls, h3_data, **kwargs):
        # 媒体架的 rack_data 变化会随 refs 传入——一并计入，否则上游改素材
        # 而导演台 h3_data 未变时会被缓存跳过。外接提示词同理。
        # 放大重绘倍数存在 h3_data 里，改它自然改变本函数的返回值，无需单列。
        #
        # ⚠ 不要试图在这里给 first_frame / last_frame / refs / prompt_text 做指纹：
        # 它们都是连线输入，而 ComfyUI 调 IS_CHANGED 时传 execution_list=None，
        # 所有连线输入一律被置 None（execution.py:181-183 的 mark_missing）。这里
        # 拿得到 h3_data（面板自己的字符串），拿不到任何上游张量。
        #
        # 失效链真正的落点是「上游自己有没有声明变了」：缓存键 = 本节点与全部
        # 祖先各自的 IS_CHANGED 之和（comfy_execution/caching.py）。上游有
        # IS_CHANGED（自带 Load Image 有）→ 换图即失效；上游没有 → 换图不失效，
        # 且本节点无从补救。媒体架因此必须自带 IS_CHANGED（见
        # csglide_media_rack.py），本节点能做的都在这三档里。
        return "%s|%s|%s" % (h3_data, kwargs.get("refs") or "",
                             kwargs.get("prompt_text") or "")


NODE_CLASS_MAPPINGS = {"CSGlideCastCS": CSGlideCast}
NODE_DISPLAY_NAME_MAPPINGS = {"CSGlideCastCS": "🎞️ H3 导演台 (H3 Studio)"}


# --------------------------------------------------------------------------
# 潜空间续写：给前端预览用的查询路由 + 执行时的自动承接。
# 「续拍存档」自动模式（编号 0）每跑一段落一个新文件，最新的就是上一片段。
# --------------------------------------------------------------------------
def _latest_cont_latent_path():
    """output/h3_context/ 里最新的一个续拍存档（绝对路径），没有则 None。"""
    if _mc_under_output is None or _MC_SLOT_FILE is None:
        return None
    folder = _mc_under_output("h3_context")
    if not folder or not os.path.isdir(folder):
        return None
    cands = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.endswith(".safetensors") and _MC_SLOT_FILE.search(f)]
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _cont_latent_candidates(max_n: int = 8):
    """h3_context/ 里挑可试的续拍存档，按 mtime 新→旧。

    20261010 改口径：自动模式的存档（*_00281_，尾下划线）编号是**第几次
    运行**，不是帧号 —— 拿编号去贴「上一段结尾帧号」是两套口径，永远错位
    （20261009 实测：旧工程凑巧落在 ±24 窗口里的老档，把当天新写的存档永远
    挡在门外，重抽多少次都接不上）。刚跑完的那份几乎总是最新的，所以直接
    按 mtime 取最近的几个逐个试 —— 分辨率对不上的在试的环节跳过。
    认段的正解是档案里的 cont_saved（adopt_film 落的，见 _continuation_latent）；
    本函数只作退路。"""
    if _mc_under_output is None or _MC_SLOT_FILE is None:
        return []
    folder = _mc_under_output("h3_context")
    if not folder or not os.path.isdir(folder):
        return []
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.endswith(".safetensors") and _MC_SLOT_FILE.search(f)]
    files.sort(key=os.path.getmtime, reverse=True)
    return files[:max_n]


def _latest_output_video():
    """output 目录里最新的一个成片视频（绝对路径），没有则 None。

    与潜空间续写「运行时自动承接最新存档」同一条假设：最新的成片就是
    上一片段。跳过 Glide Video 给浏览器解码写的 preview 代理——它永远比
    母片新，接错会把 4:2:0 副本当源片。
    """
    try:
        from .csglide_adopt import VIDEO_EXT as _EXT, _PREVIEW_RE as _PV
    except Exception:
        _EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
        _PV = re.compile(r"(^|[._-])preview\.[^.]+$", re.IGNORECASE)
    root = folder_paths.get_output_directory()
    best, best_mtime, seen = None, -1.0, 0
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in _EXT or _PV.search(fn):
                continue
            full = os.path.join(base, fn)
            try:
                m = os.stat(full).st_mtime
            except OSError:
                continue
            if m > best_mtime:
                best, best_mtime = full, m
            seen += 1
            if seen >= 4000:        # 与 /cglide/recent_outputs 同一扫描上限
                return best
    return best


def register_cont_routes():
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return

    from ._h3mc.csrf_guard import require_same_origin

    @server.routes.post("/banzhang/h3mc/latest_latent")
    @require_same_origin
    async def _latest_latent_route(request):
        latest = _latest_cont_latent_path()
        if latest is None:
            return web.json_response({"found": False})
        rel = os.path.relpath(latest, folder_paths.get_output_directory())
        return web.json_response(
            {"found": True, "path": rel.replace(os.sep, "/")})


# --------------------------------------------------------------------------
# 首尾帧锚的网格自愈（安装一次，全局生效）
# --------------------------------------------------------------------------
def _resample_keyframe_latent(lat, latent_h, latent_w):
    """关键帧潜空间重采样到目标网格——只动空间，时间维与通道不动。"""
    b, c, t, _h, _w = lat.shape
    x = lat.reshape(b * t, c, _h, _w)
    x = torch.nn.functional.interpolate(
        x.to(torch.float32), size=(latent_h, latent_w),
        mode="bilinear", align_corners=False)
    return x.to(lat.dtype).reshape(b, c, t, latent_h, latent_w)


def install_keyframe_autofit():
    """首尾帧锚在二采/三采阶段自动对齐画布网格。

    PackedLayout 用「目标画布」的网格给关键帧预留行数
    （n = vt * frame_rows，model.py:380），却用关键帧自身潜空间的 patchify
    去填（model.py:539）。一采按 768×1344 编码的关键帧，遇到二采把潜空间放大
    2×，两者必然不等——采样直接抛 cannot be broadcast，而报错里没有一个字指向
    「首尾帧」，用户只会以为首尾帧坏了。

    不要求用户填倍数、也不重建条件：在布局构造前把关键帧对齐到目标网格，任何
    改画布的下游（二采、三采、外挂放大、续写）都自动覆盖。尺寸本来就相等时一个
    字节都不动，行为逐字不变。官方节点同样受益——它也有一样的下场。
    """
    try:
        from comfy.ldm.minimax import model as _mm
    except Exception:
        return False
    cls = getattr(_mm, "PackedLayout", None)
    if cls is None or getattr(cls, "_h3studio_autofit", False):
        return False
    original = cls.__init__

    def patched(self, *args, **kwargs):
        try:
            # 形参顺序（不含 self）：text_len, latent_t, latent_h, latent_w,
            # audio_t, keyframes, refs —— 位置回退按这个序号取，别把 self 算进去。
            latent_h = kwargs.get("latent_h", args[2] if len(args) > 2 else None)
            latent_w = kwargs.get("latent_w", args[3] if len(args) > 3 else None)
            keyframes = kwargs.get("keyframes", args[5] if len(args) > 5 else None)
            if latent_h is not None and latent_w is not None and keyframes:
                latent_h, latent_w = int(latent_h), int(latent_w)
                for kf in keyframes:
                    lat = kf.get("latent")
                    if lat is None or lat.ndim != 5:
                        continue
                    if (int(lat.shape[3]) == latent_h
                            and int(lat.shape[4]) == latent_w):
                        continue
                    print("[H3 导演台] 关键帧潜空间 %s 与当前画布网格 %dx%d 不一致 ——"
                          " 已自动重采样对齐（首尾帧锚跟着当前阶段走，二采/三采不会中断）"
                          % (tuple(lat.shape), latent_h, latent_w))
                    kf["latent"] = _resample_keyframe_latent(lat, latent_h, latent_w)
        except Exception as e:
            print("[H3 导演台] 关键帧网格自愈跳过（%s: %s）" % (type(e).__name__, e))
        return original(self, *args, **kwargs)

    cls.__init__ = patched
    cls._h3studio_autofit = True
    return True


install_keyframe_autofit()
register_cont_routes()
