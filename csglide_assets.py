# -*- coding: utf-8 -*-
"""素材接入：上传文件的定位、参考图读取、媒体架槽位合并。

H3 导演台 / H3 媒体架 / Qwen 编辑台 三处共用同一套口径：
  · 素材经 /upload/image 落在 input/cglide/，槽位里只存文件名；
  · 「媒体架接线槽位覆盖面板同编号槽位」的合并规则只有一份。
任一侧改口径都不该让另一侧静默漂移，所以这些函数集中在此。
"""

import json
import os

import numpy as np
import torch
from PIL import Image, ImageOps

import folder_paths

ASSET_SUBFOLDER = "cglide"
# 早期版本把素材落在 whatdreamscost/ 下，老工程要能继续打开
LEGACY_ASSET_SUBFOLDERS = ("whatdreamscost",)

REF_LIMIT = 64          # 单类槽位硬上限，防失控 JSON（与前端 MAX_SLOTS 一致）


# --------------------------------------------------------------------------
# 放大重绘（二采/三采）链的跳过标记
# --------------------------------------------------------------------------
# 导演台在「放大重绘条件」与基础条件逐字相同的时候（面板「放大倍数」≤ 1、
# 没有参考素材可重编码、首尾帧模式），二采三采拿到的条件与一采完全一致——
# 再跑两遍放大重采样不会多出任何细节，纯白费时间。此时导演台在潜空间上打
# 这个标记，下游的放大节点读到就把潜空间原样传下去，整条二采三采链路自动
# 退化成「只用一采」，不用改任何接线。
#
# 走潜空间而不是走条件：条件端口要额外接线（用户常常不接），而且给条件塞
# 自定义键会被采样器当模型输入往下传；潜空间只是个普通 dict，官方 KSampler
# 的 common_ksampler 里 `out = latent.copy()` 会把它原样带到下一节点。
HD_UPGRADE_KEY = "banzhang_hd_upgraded"


def mark_hd_upgraded(latent, upgraded):
    """在潜空间上标记「放大重绘条件是否真的升级过」。返回新 dict，不改原对象。"""
    out = dict(latent)
    out[HD_UPGRADE_KEY] = bool(upgraded)
    return out


def hd_upgraded(latent):
    """读标记。没标记一律当作已升级 —— 外部潜空间与旧工作流保持原行为不变。"""
    try:
        return bool(latent.get(HD_UPGRADE_KEY, True))
    except AttributeError:
        return True


def resolve_asset(ref):
    """Find an uploaded asset. Tries the ref as given, then each asset folder."""
    if not ref:
        return None
    root = folder_paths.get_input_directory()
    candidates = [os.path.join(root, ref)]
    base = os.path.basename(ref)
    for sub in (ASSET_SUBFOLDER,) + LEGACY_ASSET_SUBFOLDERS:
        candidates.append(os.path.join(root, sub, base))
    candidates.append(os.path.join(root, base))
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def load_image(ref):
    """读取一张上传的参考图，返回 [1, H, W, 3] float32。"""
    path = resolve_asset(ref)
    if path is None:
        raise FileNotFoundError("素材未找到：%s" % ref)
    src = Image.open(path)
    # 透明通道必须显式压平，不能交给 convert("RGB")：它只是把 A 通道**丢掉**，
    # 透明像素会原样保留文件里「藏在 alpha 下面」的 RGB（实测 (255,0,0,α=0) 会
    # 变成纯红），于是那张图送进模型后的底色不可预期——黑、白、杂色都出现过。
    # 统一合成到白底，把「透明 = 白」变成确定语义。
    has_alpha = (src.mode in ("RGBA", "LA")
                 or (src.mode == "P" and "transparency" in src.info))
    img = ImageOps.exif_transpose(src)
    if has_alpha:
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")
        print("[素材] 参考图 %s 含透明通道，已按白底压平。" % ref)
    else:
        img = img.convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


def merge_rack_refs(cfg, refs):
    """外接媒体架（H3_REFS）覆盖同编号面板槽位；媒体架空槽位由面板兜底。

    与导演台 first_frame 的既有先例一致：接线了就以接线为准。槽位数量动态：
    媒体架有多少就扩到多少（上限 REF_LIMIT）。只处理调用方 cfg 里已有的类型，
    导演台给三类（图/视频/音频），Qwen 编辑台只给图片。
    """
    if not refs:
        return
    try:
        data = json.loads(refs) if isinstance(refs, str) else refs
    except Exception:
        return
    slots = data.get("slots") if isinstance(data, dict) else None
    if not isinstance(slots, dict):
        return
    for kind in ("images", "videos", "audios"):
        wired = slots.get(kind)
        mine = cfg.get(kind)
        if not isinstance(wired, list) or not isinstance(mine, list):
            continue
        for i, slot in enumerate(wired[:REF_LIMIT]):
            while len(mine) <= i:
                mine.append(None)
            if isinstance(slot, dict) and slot.get("file"):
                mine[i] = slot
