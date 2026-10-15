"""H3 段落存档 / 调取 —— 把「一段」当成一份能反复读写的档案。

和 _h3mc 的 Save/Load Latent 的区别：那边按 clip_index 编号排成一列，
时间轴一乱序就对不上号；这边按段落 id 走，段落 id 由段落档案统一发号，
改名、换序、插段都不会接错。

存档格式沿用 _h3mc 的 {"video":…, "audio":…}，两边文件互通。
"""

import json
import os

import torch

try:
    from safetensors.torch import load_file as _st_load, save_file as _st_save
except Exception:      # pragma: no cover
    _st_load = _st_save = None

try:
    from comfy.nested_tensor import NestedTensor
except Exception:      # pragma: no cover
    NestedTensor = None

try:                                  # 作为插件包被 ComfyUI 加载
    from . import csglide_chain as chain
except Exception:                     # pragma: no cover —— 单独跑自检时
    import csglide_chain as chain

_META = {"format": "h3_motion_context_av_v1"}


def _streams(samples):
    if samples is None:
        return []
    if getattr(samples, "is_nested", False):
        return list(samples.unbind())
    return [samples]


def _write_st(path, tensors):
    """Windows 上 safetensors 是内存映射的，直接覆盖会炸；先写 .tmp 再替换。"""
    import gc
    tmp = path + ".tmp"
    try:
        _st_save(tensors, tmp, metadata=_META)
        try:
            os.replace(tmp, path)
        except OSError:
            gc.collect()
            os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class _NameMixin:
    """片名解析：留空就跟导演台走。

    导演台每次运行都会 mark_last(工程名)。两个节点留空读它，就永远不会出现
    「导演台叫 A、存档节点写的是 B，看着跑通了其实存进另一个档案」这种事。
    """

    @staticmethod
    def _name(片名):
        n = (片名 or "").strip()
        if n:
            return n
        n = chain.last_name()
        if not n:
            raise ValueError(
                "H3 段落：片名留空，但也没有「最近登记过」的片子 —— "
                "先在 🎞️ H3 导演台的「工程」面板里填工程名（它就是片名），"
                "跑一次导演台；或者直接在片名栏手打一个。")
        print("[H3 段落] 片名留空 → 跟随导演台：%s" % n)
        return n


class CSGlideClipSaveCS(_NameMixin):
    """把采样出来的一段存进段落档案。重抽会留成新版本，旧版本不丢。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "潜空间": ("LATENT", {"label": "潜空间"}),
                "片名": ("STRING", {"default": "", "label": "片名",
                                   "tooltip": "一条片子一个文件夹，存在 output/h3_chain/<片名>/ 下。\n"
                                              "**留空 = 跟随导演台**：导演台工程面板里的工程名就是片名，"
                                              "它每次运行都会登记，这里留空即可自动对齐 —— 不用手打，"
                                              "也不会因为两处打不一致而存进另一个档案。"}),
                "段落": ("STRING", {"default": "", "label": "段落 id",
                                   "tooltip": "留空 = 自动追加为新的一段；"
                                              "填 clip_0002 = 覆盖/新增这一段的版本。"}),
                "提示词": ("STRING", {"multiline": True, "default": "",
                                    "label": "本段提示词"}),
                "帧数": ("INT", {"default": 0, "min": 0, "max": 100000, "step": 1,
                                "label": "本段帧数",
                                "tooltip": "0 = 以导演台登记的为准（推荐）。"
                                           "只有单独使用本节点、没接导演台时才需要手填。"}),
                "种子": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff,
                                "label": "种子"}),
            },
            "optional": {
                "另存为版本": ("BOOLEAN", {"default": False, "label": "另存为新版本",
                                          "tooltip": "开 = 保留旧版本，写成 t2/t3…；"
                                                     "关 = 覆盖当前版本。"}),
                # 与「段落调取」同一根插销：不参与计算，纯粹为了保证**排序** ——
                # 导演台要先 _register_clip 把「当前段」登记并钉进 chain.json，
                # 本节点「段落留空 = 自动对号当前段」才读得准。不接的话本节点
                # 和导演台是两条独立分支，谁先跑由图遍历顺序决定，就可能把档案
                # 存到上一轮残留的那一段上。
                "顺序锚": ("*", {"label": "顺序锚（接导演台任一空闲口）",
                                "tooltip": "不参与计算，只保证本节点排在导演台之后执行："
                                           "导演台要先把本段登记成「当前段」，这里才自动落到"
                                           "同一个文件夹、同一段上 —— 不用手写片名和段号。"
                                           "接导演台任意一个空闲输出口即可（比如「总帧数」）。"}),
            }
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("段落 id", "报告")
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "把一段存进段落档案，供时间轴随时调取、重抽、替换。"

    def save(self, 潜空间, 片名, 段落, 提示词, 帧数, 种子, 另存为版本=False,
             顺序锚=None):
        if _st_save is None:
            raise RuntimeError("H3 段落存档：safetensors 不可用。")
        if 顺序锚 is None:
            print("[H3 段落存档] 提示：没接「顺序锚」，本节点与导演台是两条独立分支，"
                  "谁先跑不保证 —— 若存进去的段不对，把导演台「总帧数」接到「顺序锚」上。")
        streams = _streams(潜空间.get("samples"))
        if not streams:
            raise ValueError("H3 段落存档：潜空间是空的。")

        片名 = self._name(片名)
        cid = (段落 or "").strip()
        # 帧数填 0 = 「听导演台的」。导演台登记时写的是真实帧数，存档节点
        # 不该拿自己的默认值把它盖掉——否则改了导演台长度、忘了同步这里，
        # 档案里的时长就是错的。
        want_frames = int(帧数) if int(帧数) > 0 else 0
        fallback_frames = want_frames or 149
        base = {"seed": int(种子)}
        if want_frames:
            base["frames"] = want_frames

        if not cid:
            # 留空 = 「存到导演台刚登记的那一段」。导演台运行时会把自己那段
            # 设为该片的 current，存档节点读它就能对上，用户不必手填 id。
            # 只有导演台没跑过（current 为空或已失效）才另起一段。
            cur = (chain.load_chain(片名) or {}).get("current") or ""
            if cur and chain.load_clip(片名, cur) is not None:
                cid = cur
                chain.update_clip(片名, cid, dict(base))
            else:
                cid = chain.add_clip(片名, prompt_seg=提示词 or "",
                                     frames=fallback_frames, seed=int(种子))
        else:
            if chain.load_clip(片名, cid) is None:
                chain.add_clip(片名, prompt_seg=提示词 or "",
                               frames=fallback_frames, seed=int(种子))
            else:
                patch = dict(base)
                if 提示词:
                    patch["prompt_seg"] = 提示词
                chain.update_clip(片名, cid, patch)

        meta = chain.load_clip(片名, cid) or {}
        take = int(meta.get("current_take") or 0)
        if 另存为版本:
            take += 1
        take = max(1, take)

        path = chain.clip_latent(片名, cid, take)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tensors = {}
        for i, s in enumerate(streams):
            tensors["video" if i == 0 else "audio" if i == 1 else "s%d" % i] = \
                s.cpu().contiguous()
        _write_st(path, tensors)

        takes = list(meta.get("takes") or [])
        if take not in takes:
            takes.append(take)
        chain.update_clip(片名, cid, {
            "takes": takes, "current_take": take,
            "has_latent": True, "pending": None,
        })

        names = ", ".join("%s(%s)" % (k, tuple(v.shape)) for k, v in tensors.items())
        report = "已存档：%s / %s 第 %d 版\n文件：%s\n流：%s" % (
            片名, cid, take, os.path.relpath(path, os.path.dirname(chain.chain_dir(片名)) + "/.."),
            names)
        print("[H3 段落存档] " + report.replace("\n", " | "))
        return (cid, report)


class CSGlideClipLoadCS(_NameMixin):
    """把档案里的某一段调回图里。重抽时用它拿引导头，替换时用它拿旧版本。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "片名": ("STRING", {"default": "", "label": "片名",
                                   "tooltip": "留空 = 跟随导演台（用导演台工程面板里的工程名）。"}),
                "段落": ("STRING", {"default": "", "label": "段落 id",
                                    "tooltip": "留空 = 取该片「当前段」（与 📦 存档节点"
                                               "对齐的那一段）。"}),
                "版本": ("INT", {"default": 0, "min": 0, "max": 999, "step": 1,
                                "label": "版本（0=当前）"}),
                "没有档案时": (["直通（不回填）", "报错"],
                              {"default": "直通（不回填）",
                               "label": "没有档案时",
                               "tooltip": "首次运行还没有存档时怎么办。"
                                          "「直通」= 返回空标记，下游 🧷 尾帧回填 "
                                          "会原样放过，照样能存下第一版。"}),
            },
            "optional": {
                # 不参与计算，纯粹是为了**排序**：导演台要先跑完 _register_clip
                # 把「当前段」登记进 chain.json，本节点才读得到对的段。
                # 不接这根线的话，本节点和导演台是两条独立的分支，谁先跑由
                # 图遍历顺序决定 —— 那就可能读到上一轮残留的 current，静默取错段。
                "顺序锚": ("*", {"label": "顺序锚（接导演台任一空闲口）",
                                "tooltip": "不参与计算，只保证本节点排在导演台之后执行："
                                           "导演台要先登记「当前段」，这里才读得到正确的段。"
                                           "接导演台任意一个空闲输出口即可（比如「总帧数」）。"}),
            },
        }

    RETURN_TYPES = ("LATENT", "STRING")
    RETURN_NAMES = ("潜空间", "段落信息")
    FUNCTION = "load"
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "把段落档案里的潜空间调回图里。"

    def load(self, 片名, 段落, 版本=0, 没有档案时="直通（不回填）", 顺序锚=None):
        if _st_load is None:
            raise RuntimeError("H3 段落调取：safetensors 不可用。")
        if 顺序锚 is None:
            print("[H3 段落调取] 提示：没接「顺序锚」，本节点与导演台是两条独立分支，"
                  "谁先跑不保证 —— 若取到的段不对，把导演台「总帧数」接到「顺序锚」上。")

        def empty(why):
            """没有可取的档案：直通模式下给一个空标记，让下游回填原样放过。"""
            if str(没有档案时).startswith("报错"):
                raise ValueError("H3 段落调取：%s" % why)
            print("[H3 段落调取] %s —— 直通（下游回填会原样放过）" % why)
            return ({"samples": None, "bz_empty": True}, why)

        片名 = self._name(片名)
        cid = (段落 or "").strip()
        if not cid:
            cid = (chain.load_chain(片名) or {}).get("current") or ""
            if not cid:
                return empty("片「%s」还没有任何段落" % 片名)
        meta = chain.load_clip(片名, cid)
        if meta is None:
            return empty("没有这一段（%s / %s）" % (片名, cid))
        take = int(版本) if int(版本) > 0 else int(meta.get("current_take") or 1)
        path = chain.clip_latent(片名, cid, take)
        if not os.path.exists(path):
            return empty("这一段还没存过潜空间（%s）" % path)

        data = _st_load(path)
        order = [k for k in ("video", "audio") if k in data]
        order += [k for k in data.keys() if k not in order]
        streams = [data[k] for k in order]
        if NestedTensor is None or len(streams) < 2:
            samples = streams[0]
        else:
            samples = NestedTensor(streams)

        info = json.dumps({
            "id": meta.get("id"), "index": meta.get("index"),
            "frames": meta.get("frames"), "take": take,
            "takes": meta.get("takes"), "pending": meta.get("pending"),
            "prompt": meta.get("prompt_seg"),
            "shapes": {k: tuple(data[k].shape) for k in order},
        }, ensure_ascii=False, indent=1)
        return ({"samples": samples}, info)


NODE_CLASS_MAPPINGS = {
    "CSGlideClipSaveCS": CSGlideClipSaveCS,
    "CSGlideClipLoadCS": CSGlideClipLoadCS,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "CSGlideClipSaveCS": "📦 H3 段落存档 (H3 Clip Save)",
    "CSGlideClipLoadCS": "📤 H3 段落调取 (H3 Clip Load)",
}
