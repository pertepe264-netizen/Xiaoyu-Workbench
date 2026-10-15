# -*- coding: utf-8 -*-
"""引擎层：EMA-VFI 插帧（CVPR 2023，Apache-2.0 可商用）

和 RIFE 的分工：
    RIFE     快、省显存，通用场景够用（默认）
    EMA-VFI  多尺度光流 + 注意力融合，画质高一档，代价是慢一些、显存多一些

网络代码见 _ema_vfi/（上游 MCG-NJU/EMA-VFI 原样保留）。
权重 ours_t.safetensors（约 250MB，支持任意时间点插帧）放
ComfyUI/models/ema_vfi/，缺失时自动从国内镜像下载。

精度说明：本引擎固定按单精度(fp32)运行。它是注意力架构，
半精度下 warp 采样会与卷积结果产生混合精度冲突（torch.cat 会直接报 dtype 错），
硬上 fp16 得不偿失，所以不做半精度开关。
"""
import os
import urllib.request

import torch
import torch.nn.functional as F

from .banzhang_stream_util import free_cache, rounds_for, stream_interpolate as _stream
from ._ema_vfi import feature_extractor as fe_mod
from ._ema_vfi import flow_estimation as flow_mod

MODEL_FILENAME = "ours_t.safetensors"
MODEL_URL = "https://hf-mirror.com/WAS/EMA-VFI/resolve/main/ours_t.safetensors"
_MODEL_MIN_BYTES = 100 * 1024 * 1024       # 完整权重约 250MB，小于此值视为下载不完整
_NO_AUTO_DOWNLOAD_ENV = "BANZHANG_NO_AUTO_PIP"

# 别的 EMA-VFI 插件里权重常见的落地路径（相对 ComfyUI/models），命中就复用，不重复下载。
_LOCAL_CANDIDATES = [
    "ema_vfi/ours_t.safetensors",
    "EMA-VFI/ours_t.safetensors",
    "ema/ours_t.safetensors",
    "frame_interpolation/ema_vfi/ours_t.safetensors",
    "frame_interpolation/EMA-VFI/ours_t.safetensors",
]
_LOCAL_SEARCH_DIRS = ["ema_vfi", "EMA-VFI", "ema", "frame_interpolation"]

_CACHE = {}
_resolved_path = None


class EMAVFIError(RuntimeError):
    """给用户看的中文报错。"""


def _models_dir() -> str:
    import folder_paths

    path = os.path.join(folder_paths.models_dir, "ema_vfi")
    os.makedirs(path, exist_ok=True)
    return path


def _good_weight(path: str) -> bool:
    try:
        return bool(path) and os.path.isfile(path) and os.path.getsize(path) > _MODEL_MIN_BYTES
    except OSError:
        return False


def find_local_weight():
    """在 ComfyUI 本地模型目录里找已下载好的 EMA-VFI 权重；找不到返回 None。"""
    import glob
    import folder_paths

    models_root = folder_paths.models_dir

    for rel in _LOCAL_CANDIDATES:
        p = os.path.join(models_root, rel.replace("/", os.sep))
        if _good_weight(p):
            return p

    for d in _LOCAL_SEARCH_DIRS:
        base = os.path.join(models_root, d)
        for p in glob.glob(os.path.join(base, "**", MODEL_FILENAME), recursive=True):
            if _good_weight(p):
                return p
    return None


def model_path() -> str:
    """实际要用的权重路径：优先解析到的本地文件，否则是规范存放位置。"""
    global _resolved_path
    if _good_weight(_resolved_path):
        return _resolved_path
    return os.path.join(_models_dir(), MODEL_FILENAME)


def ensure_model() -> bool:
    """本地已有权重就直接用（不重复下载）；没有才联网下载（约 250MB）。

    不想自动下载就设 BANZHANG_NO_AUTO_PIP=1。
    """
    global _resolved_path
    canonical = os.path.join(_models_dir(), MODEL_FILENAME)
    if _good_weight(canonical):
        _resolved_path = canonical
        return True

    local = find_local_weight()
    if local:
        _resolved_path = local
        print(f"[班长全家桶] 检测到本地已有的 EMA-VFI 权重，直接复用（不重复下载）：\n  {local}")
        return True

    if str(os.environ.get(_NO_AUTO_DOWNLOAD_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
        return False

    print("[班长全家桶] 首次启用 EMA-VFI 插帧，本地未找到，正在下载模型（约 250MB）...")

    def _report(blocks, block_size, total):
        if total > 0 and blocks % 200 == 0:
            print(f"  已下载 {blocks * block_size / 1048576:.0f}MB / {total / 1048576:.0f}MB")

    try:
        urllib.request.urlretrieve(MODEL_URL, canonical + ".tmp", _report)
        os.replace(canonical + ".tmp", canonical)
    except Exception as exc:
        print(f"[班长全家桶] EMA-VFI 模型下载失败：{exc}\n"
              f"  手动下载 {MODEL_URL}\n"
              f"  放到 {canonical}")
        return False
    _resolved_path = canonical
    print("[班长全家桶] EMA-VFI 模型下载完成。")
    return True


# ------------------------------------------------------------------ 网络构建

def _model_config(f=32, w=7, depth=(2, 2, 2, 4, 4)):
    """上游 config.py 的 init_model_config，原样搬过来（不要改数值，要和权重对得上）。"""
    from functools import partial

    import torch.nn as nn

    depth = list(depth)
    return {
        "embed_dims": [f, 2 * f, 4 * f, 8 * f, 16 * f],
        "motion_dims": [0, 0, 0, 8 * f // depth[-2], 16 * f // depth[-1]],
        "num_heads": [8 * f // 32, 16 * f // 32],
        "mlp_ratios": [4, 4],
        "qkv_bias": True,
        "norm_layer": partial(nn.LayerNorm, eps=1e-6),
        "depths": depth,
        "window_sizes": [w, w],
    }, {
        "embed_dims": [f, 2 * f, 4 * f, 8 * f, 16 * f],
        "motion_dims": [0, 0, 0, 8 * f // depth[-2], 16 * f // depth[-1]],
        "depths": depth,
        "num_heads": [8 * f // 32, 16 * f // 32],
        "window_sizes": [w, w],
        "scales": [4, 8, 16],
        "hidden_dims": [4 * f, 4 * f],
        "c": f,
    }


def load_model():
    """加载并缓存模型（只有一个精度，加载一次）。"""
    if _CACHE.get("net") is not None:
        return _CACHE["net"]

    if not ensure_model():
        raise EMAVFIError(
            f"找不到 EMA-VFI 权重：{model_path()}\n"
            f"手动下载后放进去就行，或联网重启一次让插件自己下。")

    try:
        from safetensors.torch import load_file
    except Exception as exc:
        raise EMAVFIError(f"缺少 safetensors 依赖（ComfyUI 自带，可能环境不完整）：{exc}") from None

    import comfy.model_management as mm

    backbone_cfg, flow_cfg = _model_config()
    net = flow_mod.MultiScaleFlow(fe_mod.feature_extractor(**backbone_cfg), **flow_cfg)

    state = load_file(model_path())
    # 上游训练用 DDP 存盘，键名带 module. 前缀；转换时可能已去掉，两种都兼容
    state = {k.replace("module.", ""): v for k, v in state.items()}
    # attn_mask / HW 是运行时按分辨率算出来的缓存，不该出现在权重里
    state = {k: v for k, v in state.items()
             if "attn_mask" not in k and "HW" not in k}

    missing, unexpected = net.load_state_dict(state, strict=False)
    if missing:
        print(f"⚠️ [班长全家桶] EMA-VFI 权重缺失 {len(missing)} 个键，示例：{list(missing)[:3]}")

    net.eval().to(mm.get_torch_device())
    for p in net.parameters():
        p.requires_grad_(False)
    _CACHE["net"] = net
    print(f"[班长全家桶] EMA-VFI 模型加载完成（{sum(p.numel() for p in net.parameters()) / 1e6:.1f}M 参数）。")
    return net


# ------------------------------------------------------------------ 推理

@torch.inference_mode()
def _infer(net, img0, img1, timestep: float = 0.5) -> torch.Tensor:
    """输入 (1,3,H,W) 的 0~1 浮点图，输出中间帧。边长补到 32 的倍数（上游要求）。"""
    device = next(net.parameters()).device
    img0 = img0.to(device)
    img1 = img1.to(device)
    _, _, height, width = img0.shape
    pad_h = (-height) % 32
    pad_w = (-width) % 32
    if pad_h or pad_w:
        img0 = F.pad(img0, (0, pad_w, 0, pad_h), mode="replicate")
        img1 = F.pad(img1, (0, pad_w, 0, pad_h), mode="replicate")

    pair = torch.cat((img0, img1), 1).float()
    pred = net(pair, timestep=timestep)[3]      # 返回顺序：(光流, 掩码, 各尺度粗融合, 最终帧)
    pred = pred.float().clamp(0.0, 1.0)
    if pad_h or pad_w:
        pred = pred[:, :, :height, :width]
    return pred


def _make_infer(net):
    def infer(img0, img1, timestep):
        return _infer(net, img0, img1, timestep)

    return infer


# ------------------------------------------------------------------ 流式插帧

def stream_interpolate(frames_iter, total_in: int, multiplier: int, fp16: bool,
                       sink, progress=None) -> None:
    """流式插帧入口，签名与 RIFE 引擎保持一致，节点层可无差别调用。"""
    try:
        rounds = rounds_for(multiplier)
    except ValueError as exc:
        raise EMAVFIError(str(exc)) from None

    net = load_model()
    _stream(frames_iter, total_in, rounds, sink, _make_infer(net),
            fp16=False, progress=progress)      # 固定单精度，理由见文件头
    free_cache()


__all__ = ["EMAVFIError", "ensure_model", "load_model", "model_path", "stream_interpolate"]
