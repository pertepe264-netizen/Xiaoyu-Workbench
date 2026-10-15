# -*- coding: utf-8 -*-
"""插帧公共骨架：流式逐帧处理 + 帧格式转换。

RIFE 和 EMA-VFI 两个引擎共用这一套：
    内存/显存峰值 = 模型 + 一对源帧 + 2^倍数 个中间帧，与视频多长完全无关。

各引擎只负责两件事：
    1. 提供 infer(img0, img1, timestep) -> 中间帧张量
    2. 报告自己支持多高的插帧倍数
"""
import gc

import numpy as np
import torch

# 倍数 -> 递归次数（2 倍补 1 帧 = 1 次；4 倍 = 2 次；8 倍 = 3 次）
MULTIPLIER_ROUNDS = {2: 1, 4: 2, 8: 3}

SUPPORTED_MULTIPLIERS = tuple(MULTIPLIER_ROUNDS)


def rounds_for(multiplier: int) -> int:
    rounds = MULTIPLIER_ROUNDS.get(int(multiplier))
    if rounds is None:
        raise ValueError(f"不支持的倍数：{multiplier}，只能选 "
                         f"{'/'.join(str(m) for m in SUPPORTED_MULTIPLIERS)}")
    return rounds


def free_cache() -> None:
    """把显存缓存还回去（每处理完一对帧调用一次，峰值更平稳）。"""
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def to_gpu(rgb_np, fp16: bool) -> torch.Tensor:
    """numpy uint8 RGB 帧 -> (1,3,H,W) 浮点张量。"""
    t = torch.from_numpy(np.ascontiguousarray(rgb_np, dtype=np.uint8))
    t = t.to(dtype=torch.float32).div_(255.0).movedim(-1, 0).unsqueeze(0)
    return t.half() if fp16 else t


def to_np(mid: torch.Tensor) -> np.ndarray:
    """(1,3,H,W) 浮点张量 -> numpy uint8 RGB 帧。

    注意：推理是在 inference_mode 里跑的，产出的张量出作用域后不允许原地改，
    所以这里全程用非原地算子，并重新包一层 inference_mode。
    """
    with torch.inference_mode():
        frame = mid.squeeze(0).movedim(0, -1).clamp(0.0, 1.0).mul(255.0).round()
    return frame.to(torch.uint8).cpu().numpy()


def interp_mids(infer, img0, img1, rounds: int) -> list:
    """递归插中间帧，返回 2^rounds - 1 个中间帧（按时间顺序）。

    和「反复整段对插」在数学上完全等价，但每次只驻留当前这一对的中间结果。
    """
    if rounds <= 0:
        return []
    mid = infer(img0, img1, 0.5)
    if rounds == 1:
        return [mid]
    return (interp_mids(infer, img0, mid, rounds - 1)
            + [mid]
            + interp_mids(infer, mid, img1, rounds - 1))


def stream_interpolate(frames_iter, total_in: int, rounds: int, sink, infer,
                       fp16: bool = True, progress=None) -> None:
    """流式插帧：逐帧喂 numpy uint8 RGB，边插边把结果帧吐给 sink。

    frames_iter : 生成器，逐帧产出 HxWx3 uint8
    sink        : 每插出一帧就调用一次（通常是视频编码器的 write）
    infer       : infer(img0, img1, timestep) -> (1,3,H,W)
    """
    total_pairs = max(0, int(total_in) - 1)
    prev = None
    served = 0
    for cur_rgb in frames_iter:
        cur = to_gpu(cur_rgb, fp16)
        if prev is None:
            sink(cur_rgb)          # 第一帧原样落地
        else:
            for mid in interp_mids(infer, prev, cur, rounds):
                sink(to_np(mid))
            sink(cur_rgb)          # 再落当前源帧
        prev = cur
        served += 1
        if progress is not None and served >= 2:
            progress(served - 1, total_pairs)
    free_cache()


__all__ = ["MULTIPLIER_ROUNDS", "SUPPORTED_MULTIPLIERS", "rounds_for", "free_cache",
           "to_gpu", "to_np", "interp_mids", "stream_interpolate"]
