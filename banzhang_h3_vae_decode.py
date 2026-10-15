# -*- coding: utf-8 -*-
"""
🎬 小语的工作台 — H3 VAE 快速解码（自带模型加载 + 2x PixelShuffle 放大）
================================================================================
原版作者: Tridae (ComfyUI-MiniMaxH3_LatentUpscaler 的 vae_decode.py)
小语的工作台版改造:
    1. 不再需要官方「VAE加载器」节点: 直接从 models/vae 下拉选择 H3 VAE 文件,
       节点自己读取检查点并构建 MiniMaxH3VideoVAE, 官方原版(3通道)与
       改造版 2x PixelShuffle(12通道)检查点都能自动识别加载。
    2. 潜空间输入直接接采样器输出的音视频联合潜空间(NestedTensor),
       自动剥离音频流, 只解码视频流, 输出图像。
    3. 解码完自动把模型卸载回内存(不吃推理显存), 模型本体按文件缓存, 不重复读盘。
    4. 参数说明/分类/显示名统一小语的工作台中文风格。
    5. 模型精度可就地切换(自动/FP16/BF16/FP32), 不必改 ComfyUI 启动参数。

为什么官方 VAELoader 加载不了改造版 2x VAE:
    改造版检查点的 decoder.proj_out 有 12288 行(12 通道 = 3 色 x 2x2 子像素),
    ComfyUI 本体构建 MiniMaxH3VideoVAE 时写死 3 通道(3072 行), 尺寸对不上直接报错。
    本节点构建模型时按检查点实际行数自适应重建输出层, 绕开这个限制。

解码增强(继承自原版):
    官方 VAEDecode 无法修改 H3 VAE 的 tile_size; 且 OOM 时会退回通用 3D 分块器
    (不遵守 H3 混合规则, 接缝变网格)。本节点直接调用 H3 自带分块路径:
    大瓦片保持 64/256 重叠比例不接缝, 可选时间分块支持长视频低显存。
================================================================================
"""

from __future__ import annotations

import math
import os

import torch
import torch.nn.functional as F

import comfy.model_management as model_management
import comfy.nested_tensor
import comfy.utils

_H3_TILE_MULTIPLE = 16  # H3 原生空间 VAE 比例
_STOCK_TILE = 256
_STOCK_OVERLAP = 64

# 模型缓存: 文件路径+mtime+精度 -> {model, out_channels, upscale, dtype, device_bytes}
# 切换文件或切换精度自动替换; 同文件同精度重复解码不重读磁盘、不重建模型。
_vae_cache: dict = {}

_PRECISION_AUTO = "自动（跟随 ComfyUI 设置）"

# 界面精度选项 -> torch dtype; 自动 = 沿用 ComfyUI 全局 VAE 精度
_PRECISION_DTYPES = {
    _PRECISION_AUTO: None,
    "FP16（半精度·省显存）": torch.float16,
    "BF16（半精度·大范围）": torch.bfloat16,
    "FP32（全精度·最慢）": torch.float32,
}


def _resolve_vae_dtype(precision: str) -> torch.dtype:
    """把界面上的精度选项换算成 torch dtype。

    自动 = model_management.vae_dtype(), 即 ComfyUI 启动参数
    --fp16-vae / --bf16-vae / --fp32-vae 指定的精度(都没加时是 FP32)。
    半精度在 CPU 上跑不了卷积, 无显卡时自动退回 FP32。
    """
    dtype = _PRECISION_DTYPES.get(str(precision), None)
    if dtype is None:
        dtype = model_management.vae_dtype()
    if dtype in (torch.float16, torch.bfloat16):
        if model_management.get_torch_device().type == "cpu":
            print(f"[H3 VAE 快速解码] 当前没有可用显卡, {dtype} 无法运算, 已改用 FP32。")
            dtype = torch.float32
    return dtype


# ==============================================================================
# 自带 VAE 加载器 (替代官方 VAELoader)
# ==============================================================================

def _count_decoder_layers(sd: dict) -> int:
    """从检查点键名数出 decoder transformer 层数。"""
    layers = [
        int(k.split(".")[2])
        for k in sd
        if isinstance(k, str)
        and k.startswith("decoder.transformer_blocks.")
        and k.endswith(".scale1")
    ]
    return (max(layers) + 1) if layers else 36


def _load_h3_vae(vae_name: str, precision: str = _PRECISION_AUTO) -> dict:
    """读取并构建 H3 视频 VAE, 自适应官方版/改造版 2x 检查点。带缓存。"""
    import folder_paths

    path = folder_paths.get_full_path("vae", vae_name)
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"在 models/vae 下找不到「{vae_name}」")

    precision = str(precision)
    # 缓存按 文件+精度 分开: 换精度要按新 dtype 重新加载一次
    key = f"{path}:{os.path.getmtime(path)}:{precision}"
    cached = _vae_cache.get(key)
    if cached is not None:
        return cached
    _vae_cache.clear()  # 只保留当前文件+精度, 与原项目 patcher 缓存策略一致

    dtype = _resolve_vae_dtype(precision)
    sd = comfy.utils.load_torch_file(path, safe_load=True)
    if "decoder.proj_out.weight" not in sd:
        raise ValueError(
            f"「{vae_name}」不是 H3 视频 VAE 检查点(缺少 decoder.proj_out.weight)。"
            "请选择 MiniMax H3 的视频 VAE 文件。"
        )

    rows = int(sd["decoder.proj_out.weight"].shape[0])

    # 量化检测与官方 sd.py 同逻辑; 量化检查点保持原样, 不做精度转换
    quant = comfy.utils.detect_layer_quantization(sd, "")
    if quant is not None:
        ops = comfy.ops.mixed_precision_ops(quant, torch.float16)
        if dtype != torch.float16:
            print(f"[H3 VAE 快速解码] 「{vae_name}」是量化检查点, 已忽略「模型精度」选项。")
        dtype = torch.float16
        print(f"[H3 VAE 快速解码] 检测到 {quant} 量化检查点, 按 FP16 量化算子加载")
    else:
        ops = comfy.ops.disable_weight_init

    from comfy.ldm.minimax.vae import MiniMaxH3VideoVAE

    num_layers = _count_decoder_layers(sd)
    model = MiniMaxH3VideoVAE(operations=ops, num_layers=num_layers)

    # 自适应输出层: 检查点 proj_out 行数与默认(3通道=3072)不一致时,
    # 按检查点实际行数重建输出层并同步 out_channels。
    # 改造版 2x: 12288 行 = 12 通道 = 3 色 x 2x2 子像素 -> 解码后 PixelShuffle 2x。
    dec = model.decoder
    patch_volume = int(dec.patch_size_t) * int(dec.patch_size) ** 2
    out_channels = 3
    if rows != int(dec.proj_out.out_features):
        if rows % patch_volume:
            raise ValueError(
                f"「{vae_name}」的 decoder.proj_out 有 {rows} 行, 无法被 H3 patch 体积 "
                f"{patch_volume} 整除, 不是受支持的 H3 视频 VAE。"
            )
        out_channels = rows // patch_volume
        if out_channels % 3:
            raise ValueError(
                f"「{vae_name}」推断出 {out_channels} 个输出通道, 不是 3 的倍数。"
            )
        ratio_squared = out_channels // 3
        if math.isqrt(ratio_squared) ** 2 != ratio_squared:
            raise ValueError(
                f"「{vae_name}」的 {out_channels} 个输出通道不构成正方形 PixelShuffle 布局。"
            )
        dec.proj_out = ops.Linear(int(dec.proj_out.in_features), rows)
        dec.out_channels = out_channels

    model.load_state_dict(sd, strict=False, assign=False)
    model.eval()
    del sd

    if quant is None:
        model.to(dtype)
        model_dtype = next(model.parameters()).dtype
    else:
        model_dtype = torch.float16  # 量化算子按 fp16 计算, 与官方 sd.py 一致

    offload = model_management.unet_offload_device()
    model.to(offload)
    device_bytes = sum(p.numel() * p.element_size() for p in model.parameters())
    upscale = math.isqrt(out_channels // 3)

    print(
        f"[H3 VAE 快速解码] 已加载 {vae_name}: {num_layers} 层, "
        + (f"改造版 {upscale}x PixelShuffle ({out_channels} 通道)"
           if out_channels > 3 else "官方原版 (3 通道)")
        + f", dtype={model_dtype}, 约 {device_bytes / 1024 ** 2:.0f}MB"
    )

    bundle = {
        "model": model,
        "out_channels": out_channels,
        "upscale": upscale,
        "dtype": model_dtype,
        "device_bytes": device_bytes,
    }
    _vae_cache[key] = bundle
    return bundle


# ==============================================================================
# 潜空间音视频分离
# ==============================================================================

def _extract_video_latent(samples: dict) -> torch.Tensor:
    """从采样器输出的联合潜空间取出视频流, 音频流剥离丢弃。

    H3 联合潜空间是 NestedTensor: 成员[0]=视频 [B,24,T,H,W], 成员[1]=音频 [B,32,2,Ta]。
    普通单张量潜空间也兼容(直接当视频流)。
    """
    if "samples" not in samples:
        raise KeyError('LATENT 字典缺少 "samples"')
    raw = samples["samples"]
    if isinstance(raw, comfy.nested_tensor.NestedTensor) or getattr(raw, "is_nested", False):
        members = list(raw.unbind())
        if not members:
            raise ValueError("联合潜空间的成员列表为空")
        video = members[0]
    elif isinstance(raw, torch.Tensor):
        video = raw
    else:
        raise TypeError(f"应为 NestedTensor 或 torch.Tensor, 实际是 {type(raw)}")
    if video.ndim != 5:
        raise ValueError(
            f"应为 H3 视频潜空间 BxCxTxHxW, 实际形状 {tuple(video.shape)}"
        )
    return video


# ==============================================================================
# 解码辅助 (继承自原版 vae_decode.py, 逻辑一致)
# ==============================================================================

def _snap_multiple(value: int, multiple: int, minimum: int) -> int:
    value = max(int(minimum), int(value))
    return max(minimum, (value // multiple) * multiple)


def _effective_overlap(tile_size: int, tile_overlap: int) -> int:
    """保持官方 64/256 的重叠比例, 大瓦片才不会出现接缝。"""
    scaled = max(_STOCK_OVERLAP, (int(tile_size) * _STOCK_OVERLAP) // _STOCK_TILE)
    overlap = max(int(tile_overlap), scaled)
    overlap = _snap_multiple(overlap, _H3_TILE_MULTIPLE, _H3_TILE_MULTIPLE)
    return min(overlap, max(_H3_TILE_MULTIPLE, tile_size - _H3_TILE_MULTIPLE))


def _expand_rgb_stat(stat: torch.Tensor, packed_channels: int) -> torch.Tensor:
    """把 RGB 归一化缓冲区扩展到打包后的 PixelShuffle 通道数。

    改造版投影的排布是 R 各相位、G 各相位、B 各相位, 所以每个原生 RGB 统计值
    需要连续重复, 而不是重复整个 RGB 三元组。
    """
    if packed_channels == 3:
        return stat
    if any(size == packed_channels for size in stat.shape):
        return stat
    if packed_channels % 3:
        raise ValueError(f"无法把 RGB 统计量扩展到 {packed_channels} 个通道。")

    channel_dims = [i for i, size in enumerate(stat.shape) if size == 3]
    if not channel_dims:
        raise ValueError(
            f"应为含尺寸 3 维度的 RGB 归一化缓冲区, 实际形状 {tuple(stat.shape)}。"
        )
    channel_dim = 1 if stat.ndim > 1 and stat.shape[1] == 3 else channel_dims[0]
    return stat.repeat_interleave(packed_channels // 3, dim=channel_dim)


def _decode_once(model, dtype, video: torch.Tensor, output_device: torch.device,
                 out_channels: int) -> torch.Tensor:
    """单次(或单窗口)解码: 加载显存 -> H3 自带路径解码 -> PixelShuffle 放大。"""
    device = model_management.get_torch_device()
    bundle_bytes = sum(p.numel() * p.element_size() for p in model.parameters())

    # 显存不够时先按 ComfyUI 的标准做法腾地方: 把显存里的其他模型(采样器/文本编码器等)
    # 卸回内存, 而不是直接报错。卸载掉的那些模型下次被用到会自动重新加载。
    need = bundle_bytes * 2  # 权重 + 激活值的经验预算
    unloaded = model_management.free_memory(need, device)
    free = model_management.get_free_memory(device)
    if free < need:
        # 降低要求再来一次: 门槛低, 能连带卸载更小的模型, 尽量腾出空间
        unloaded += model_management.free_memory(bundle_bytes, device)
        free = model_management.get_free_memory(device)
    if unloaded:
        print(
            f"[H3 VAE 快速解码] 已从显存卸载 {len(unloaded)} 个模型腾空间"
            "(后续节点用到时会自动重新加载)。"
        )

    if free < bundle_bytes:
        raise RuntimeError(
            f"显存不足: 这颗 H3 VAE 权重约 {bundle_bytes / 1024 ** 2:.0f}MB, "
            f"卸载其他模型后仍只有 {free / 1024 ** 2:.0f}MB 空闲, 连权重都装不下。"
            "可换体积更小的 VAE(int8 / w4a8 量化版只有 1.7~2.8GB), "
            "或降低输出分辨率/帧数后再试。"
        )
    if free < need:
        print(
            f"[H3 VAE 快速解码] 提示: 空闲显存 {free / 1024 ** 2:.0f}MB 低于建议的 "
            f"{need / 1024 ** 2:.0f}MB, 仍会尝试解码; "
            "若报显存不足请换小 VAE 或调小「分块大小」。"
        )
    model.to(device)

    # 动态加载可能把检查点里原始的 3 通道归一化缓冲区恢复回来,
    # 所以在加载完成之后才扩展。这必须是解码前的最后一次修改。
    if out_channels > 3:
        model.pixel_mean = _expand_rgb_stat(model.pixel_mean, out_channels)
        model.pixel_std = _expand_rgb_stat(model.pixel_std, out_channels)

    pixel_samples = torch.empty(
        model.decode_output_shape(video.shape),
        device=output_device,
        dtype=torch.float32,
    )
    samples = video.to(device=device, dtype=dtype)
    try:
        model.decode(samples, output_buffer=pixel_samples)
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        model_management.soft_empty_cache()
        raise RuntimeError(
            "解码过程显存不足(OOM)。请换体积更小的 VAE(int8 / w4a8 量化版), "
            "或把「分块大小」调小、减少帧数后重试。"
        ) from exc

    if out_channels > 3:
        # torch.pixel_shuffle 要求 N,C,H,W。H3 视频是 B,C,T,H,W,
        # 把时间折进 batch, 逐帧重排, 再还原时间维。
        upscale = math.isqrt(out_channels // 3)
        batch, channels, frames, height, width = pixel_samples.shape
        frame_batch = pixel_samples.permute(0, 2, 1, 3, 4).reshape(
            batch * frames, channels, height, width
        )
        frame_batch = F.pixel_shuffle(frame_batch, upscale_factor=upscale)
        out_channels_ = channels // (upscale * upscale)
        pixel_samples = frame_batch.reshape(
            batch, frames, out_channels_, height * upscale, width * upscale
        ).permute(0, 2, 1, 3, 4).contiguous()

    # [B,C,T,H,W] -> [B,T,H,W,C]: 通道挪到最后, 与 Comfy IMAGE 布局一致(原版同样处理)
    return pixel_samples.movedim(1, -1)


def _offload_model(model) -> None:
    """解码完把模型卸载回内存, 释放推理显存(不销毁, 下次解码直接复用)。"""
    model.to(model_management.unet_offload_device())
    model_management.soft_empty_cache()


def _h3_frame_count(video_t: int) -> int:
    """把官方 H3 视频潜空间的 T 换算成解码帧数。"""
    video_t = int(video_t)
    if video_t < 2 or (video_t - 2) % 5:
        raise ValueError(
            f"时间分块要求潜空间 T 符合 17k+5 网格; 当前 T={video_t}。"
        )
    return 5 + 17 * ((video_t - 2) // 5)


def _temporal_settings(tile_frames: int, context_frames: int) -> tuple[int, int, int, int]:
    """把界面上的帧数请求换算成 H3 的因果组。

    前两个潜空间位置解码 5 帧; 之后每 5 个潜空间位置增加 17 帧。
    后面的块额外带 2 个前导潜空间位置 + 上下文组, 然后丢弃解码出的
    前缀, 只保留稳定的中段帧。
    """
    tile_groups = max(1, int(round(max(17, int(tile_frames)) / 17.0)))
    context_groups = max(0, int(round(max(5, int(context_frames)) - 5) / 17.0))
    return tile_groups, context_groups, 17 * tile_groups, 5 + 17 * context_groups


def _decode_temporal_tiled(bundle, video: torch.Tensor, output_device: torch.device,
                           tile_frames: int, context_frames: int,
                           seam_warning_threshold: float) -> torch.Tensor:
    """按因果时间窗口解码 H3, 只保留稳定区域(长视频低显存)。"""
    model = bundle["model"]
    dtype = bundle["dtype"]
    out_channels = bundle["out_channels"]
    total_frames = _h3_frame_count(video.shape[2])
    total_groups = (int(video.shape[2]) - 2) // 5
    tile_groups, context_groups, resolved_tile, resolved_context = _temporal_settings(
        tile_frames, context_frames
    )
    if total_groups <= tile_groups:
        return _decode_once(model, dtype, video, output_device, out_channels)

    print(
        f"[H3 VAE 快速解码] 时间分块: 共 {total_frames} 帧, "
        f"每块新解码 {resolved_tile} 帧, 因果上下文 {resolved_context} 帧。"
    )
    pieces = []
    completed_groups = 0
    worst_seam = 0.0

    while completed_groups < total_groups:
        core_groups = min(tile_groups, total_groups - completed_groups)
        if completed_groups == 0:
            end_t = 2 + 5 * core_groups
            decoded = _decode_once(model, dtype, video[:, :, :end_t], output_device, out_channels)
            pieces.append(decoded)
        else:
            overlap_groups = min(context_groups, completed_groups)
            # 在重叠组之前带上紧邻的 2 个因果基础位置, 保证每个窗口自身长度为 2+5k。
            start_t = 5 * (completed_groups - overlap_groups)
            end_t = 2 + 5 * (completed_groups + core_groups)
            decoded = _decode_once(model, dtype, video[:, :, start_t:end_t], output_device, out_channels)
            discard = 5 + 17 * overlap_groups
            if int(decoded.shape[1]) <= discard:
                raise RuntimeError("时间块在丢弃上下文后没有剩余稳定帧。")

            threshold = float(seam_warning_threshold)
            if threshold > 0 and pieces:
                compare = min(discard, int(pieces[-1].shape[1]))
                if compare:
                    previous = pieces[-1][:, -compare:].float()
                    repeated = decoded[:, discard - compare:discard].float()
                    difference = float(torch.mean(torch.abs(previous - repeated)).item())
                    worst_seam = max(worst_seam, difference)
                    if difference > threshold:
                        print(
                            f"[H3 VAE 快速解码] 警告: 第 "
                            f"{5 + 17 * completed_groups} 帧附近的时间接缝差异 "
                            f"{difference:.6f}, 超过阈值 {threshold:.6f}。"
                            f"请增大「时间上下文帧数」。"
                        )
            pieces.append(decoded[:, discard:])
        completed_groups += core_groups
        _offload_model(model)  # 每块解码完立即释放显存

    images = torch.cat(pieces, dim=1)
    if int(images.shape[1]) != total_frames:
        raise RuntimeError(
            f"时间重建产出 {images.shape[1]} 帧; 应为 {total_frames} 帧。"
        )
    if float(seam_warning_threshold) > 0:
        print(f"[H3 VAE 快速解码] 最大时间重叠差异: {worst_seam:.6f}。")
    return images


# ==============================================================================
# 节点
# ==============================================================================

class BanZhangH3VAEDecodeFast:
    """H3 音视频联合潜空间 -> 图像: 自带 VAE 加载, 自动剥离音频, 解码时 2x 放大。"""

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {
            "required": {
                "潜空间": ("LATENT", {"tooltip": "直接接采样器输出的音视频联合潜空间。"
                                                 "节点自动剥离音频流, 只解码视频流输出图像。"}),
                "VAE文件": (sorted(folder_paths.get_filename_list("vae")),
                            {"tooltip": "直接选 models/vae 下的 H3 视频 VAE 文件, 不需要官方「VAE加载器」节点。"
                                        "官方原版与改造版 2x 检查点都支持, 自动识别。"}),
                "模型精度": (
                    list(_PRECISION_DTYPES.keys()),
                    {
                        "default": _PRECISION_AUTO,
                        "tooltip": (
                            "解码时模型与潜空间的精度。"
                            "自动 = 沿用 ComfyUI 全局 VAE 精度(启动参数 --fp16-vae / --bf16-vae / "
                            "--fp32-vae; 一个都没加时是 FP32, 最慢最吃显存)。"
                            "FP16 最快最省显存; FP32 最准但慢; BF16 介于两者之间。"
                            "量化检查点忽略本选项, 强制 FP16。换精度会重新读一次 VAE 文件。"
                        ),
                    },
                ),
                "空间分块": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "开启: 走 H3 原生空间分块(与官方 VAEDecode 同算法)。"
                            "关闭: 每个时间块整帧一次解码——更快、更吃显存。"
                            "显存不足时 Comfy 不会静默切换成别的分块器。"
                        ),
                    },
                ),
                "分块大小": (
                    "INT",
                    {
                        "default": 256,
                        "min": 256,
                        "max": 4096,
                        "step": 16,
                        "tooltip": (
                            "空间分块大小(像素)。256 + 重叠 64 = 官方 VAEDecode 品质。"
                            "分块越大越快; 重叠会自动按 64/256 比例放大, "
                            "避免接缝变成网格。"
                        ),
                    },
                ),
                "分块重叠": (
                    "INT",
                    {
                        "default": 64,
                        "min": 16,
                        "max": 1024,
                        "step": 16,
                        "tooltip": (
                            "最小像素重叠。实际重叠 = max(该值, 分块大小/4)。"
                            "官方在 256px 时用 64。关闭空间分块时无效。"
                        ),
                    },
                ),
                "输出设备": (
                    ["CPU", "GPU"],
                    {
                        "default": "CPU",
                        "tooltip": (
                            "解码完成的 RGB 存放位置。cpu = 官方默认。"
                            "gpu = 跳过 GPU→CPU 拷贝(整段视频都占显存)。画质相同。"
                        ),
                    },
                ),
                "时间分块": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "tooltip": (
                            "关闭: 整段时间一次解码(原始行为)。"
                            "开启: 按因果窗口分段解码并丢弃重复上下文, "
                            "降低长视频的显存峰值。"
                        ),
                    },
                ),
                "每块解码帧数": (
                    "INT",
                    {
                        "default": 85,
                        "min": 17,
                        "max": 3599,
                        "step": 17,
                        "tooltip": "每个时间块大约新解码的帧数; 自动对齐到 17 帧的 H3 组。",
                    },
                ),
                "时间上下文帧数": (
                    "INT",
                    {
                        "default": 39,
                        "min": 5,
                        "max": 702,
                        "step": 17,
                        "tooltip": "每个时间块边界重复解码并丢弃的因果上下文帧数。",
                    },
                ),
                "接缝告警阈值": (
                    "FLOAT",
                    {
                        "default": 0.02,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.001,
                        "tooltip": "重叠区重复解码差异超过该平均绝对值时打印警告; 0 = 关闭检查。",
                    },
                ),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "decode"
    CATEGORY = "👨‍🏫班长/MiniMax H3"
    DESCRIPTION = (
        "H3 音视频联合潜空间直连解码: 自动剥离音频流只解视频, 不需要官方 VAE 加载器——"
        "直接在节点里选 models/vae 下的 H3 视频 VAE 文件, 官方原版与改造版 2x 检查点都支持"
        "(改造版解码时自动 PixelShuffle 放大 2x)。"
        "解码完模型自动退回内存不占推理显存。"
        "可在节点里切换模型精度(自动/FP16/BF16/FP32), 无需改 ComfyUI 启动参数。"
        "可调空间分块/输出设备/时间分块(长视频)。"
    )

    def decode(
        self,
        潜空间,
        VAE文件,
        模型精度=_PRECISION_AUTO,
        空间分块=True,
        分块大小=256,
        分块重叠=64,
        输出设备="CPU",
        时间分块=False,
        每块解码帧数=85,
        时间上下文帧数=39,
        接缝告警阈值=0.02,
    ):
        bundle = _load_h3_vae(VAE文件, 模型精度)
        model = bundle["model"]
        video = _extract_video_latent(潜空间)  # 音频流在此剥离

        tile_size = _snap_multiple(分块大小, _H3_TILE_MULTIPLE, _STOCK_TILE)
        overlap = _effective_overlap(tile_size, 分块重叠)
        model.tiling = bool(空间分块)
        model.tile_size = tile_size
        model.tile_overlap_min = overlap

        if str(输出设备).lower() == "gpu":
            out_dev = model_management.get_torch_device()
        else:
            out_dev = model_management.intermediate_device()

        try:
            if 时间分块:
                images = _decode_temporal_tiled(
                    bundle,
                    video,
                    out_dev,
                    每块解码帧数,
                    时间上下文帧数,
                    接缝告警阈值,
                )
            else:
                images = _decode_once(model, bundle["dtype"], video, out_dev,
                                      bundle["out_channels"])
        finally:
            _offload_model(model)

        # [B, C, T, H, W] -> [B*T, H, W, C]
        if len(images.shape) == 5:
            images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
        suffix = f"(含 {bundle['upscale']}x 解码放大)" if bundle["upscale"] > 1 else ""
        print(
            f"[H3 VAE 快速解码] 完成: {images.shape[0]} 帧, "
            f"{images.shape[2]}x{images.shape[1]} 像素 {suffix}"
        )
        return (images,)


# ==========================================
# 小语的工作台注册
# ==========================================
NODE_CLASS_MAPPINGS = {
    "BanZhang_H3VAEDecodeFast": BanZhangH3VAEDecodeFast,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhang_H3VAEDecodeFast": "🎬 H3 VAE 快速解码",
}
