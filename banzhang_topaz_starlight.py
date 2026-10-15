# -*- coding: utf-8 -*-
"""ComfyUI node: Topaz Starlight 2.5 (calls Topaz neuroserver as external engine).
All input names in Chinese for readability.

原独立插件 ComfyUI-TopazStarlight 并入小语的工作台, 管线函数见 banzhang_topaz_pipeline.py。
2026-09-08: 支持运行中进度条 (comfy ProgressBar) 与执行中/排队中两种取消 (中断时终止引擎进程)。
"""
import os, tempfile, uuid
import numpy as np
import torch
from .banzhang_topaz_pipeline import write_frames_to_video, run_starlight, read_video_to_frames, TopazUserInterrupt


class TopazStarlight:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE",),
                "模型": (["星光 2.6", "Astra", "Astra HQ", "Astra Fast", "Astra Sharp"],
                         {"default": "星光 2.6",
                          "tooltip": "星光2.6=1.7.1引擎(官方换代后的星光)；Astra系列需≥9帧；Fast需Intel核显"}),
                "显存上限": ("FLOAT", {"default": 14.0, "min": 8.0, "max": 32.0, "step": 0.1,
                                     "tooltip": "神经服务器显存上限(GiB，可小数)。留2~4G余量防驱动借共享内存变慢：16G卡建议14，24G卡建议20，32G卡建议28；吃紧降到12/10；低于8可能offload到CPU超慢"}),
                "放大倍数": ("FLOAT", {"default": 2.00, "min": 1.00, "max": 4.00, "step": 0.01,
                                       "tooltip": "输出放大倍数：1=不放大只增强；支持两位小数如 1.78/2.67（参考表里的精确倍率）；内部对齐到偶数像素"}),
                "帧率": ("INT", {"default": 24, "min": 1, "max": 120,
                                 "tooltip": "和视频实际帧率一致"}),
                "增强强度": ("FLOAT", {"default": 1.0, "min": 0.5, "max": 1.5, "step": 0.1,
                                       "tooltip": "细节增强力度：0.7柔和 / 1.0默认 / 1.3细节最猛"}),
                "输入质量": (["无损·短片极致 (qp2)", "近无损 (qp8)", "默认·推荐 (qp14)",
                              "均衡·长片 (qp20)", "省空间·试跑 (qp26)", "自定义"],
                             {"default": "默认·推荐 (qp14)",
                              "tooltip": "帧喂给引擎前的中间编码质量(constqp，越小越无损)。qp2/8临时文件大、编码慢，短片极致可用；qp14视觉近无损甜点位；qp20长片省空间；26以上伪影会被星光脑补放大；选「自定义」用下方 qp 数值"}),
                "自定义qp": ("INT", {"default": 14, "min": 0, "max": 40,
                                     "tooltip": "仅当上方「输入质量」选「自定义」时生效；越小越无损，>26 伪影会被星光放大"}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("图像",)
    FUNCTION = "run"
    CATEGORY = "👨‍🏫班长/视频处理"

    def run(self, 图像, 模型, 显存上限, 放大倍数, 帧率, 增强强度, 输入质量, 自定义qp):
        import comfy.model_management as mm
        from comfy.utils import ProgressBar
        mm.throw_exception_if_processing_interrupted()   # 排队期间已被取消: 不启动引擎直接退出

        b, h, w, c = 图像.shape
        if c != 3:
            raise ValueError(f'TopazStarlight expects RGB images, got {c} channels')
        frames = (图像 * 255.0).clamp(0, 255).cpu().numpy().astype(np.uint8)

        MODEL_IDS = {"星光 2.6": "slp-26",   # 1.7.1 官方换代: 星光即 2.6 权重
                     "Astra": "astra", "Astra HQ": "astrahq",
                     "Astra Fast": "astrafast", "Astra Sharp": "astrasharp"}
        model_id = MODEL_IDS.get(模型, "slp-26")

        QP_IDS = {"无损·短片极致 (qp2)": 2, "近无损 (qp8)": 8, "默认·推荐 (qp14)": 14,
                  "均衡·长片 (qp20)": 20, "省空间·试跑 (qp26)": 26, "自定义": None}
        qp = 自定义qp if 输入质量 == "自定义" else QP_IDS.get(输入质量, 14)
        qp = max(0, min(40, int(qp)))

        pbar = ProgressBar(b)
        def _progress_cb(done):
            pbar.update_absolute(int(done))          # ProgressBar 自带上限钳制与节流
        def _interrupt_check():
            return mm.processing_interrupted()

        tag = uuid.uuid4().hex[:10]
        tmp = tempfile.gettempdir()
        in_video = os.path.join(tmp, f'topaz_in_{tag}.mp4')
        out_video = os.path.join(tmp, f'topaz_out_{tag}.mp4')
        try:
            print(f'[TopazStarlight] {模型} {b}帧 {w}x{h} -> {w*放大倍数}x{h*放大倍数} (x{放大倍数}) 强度{增强强度} fps={帧率}')
            write_frames_to_video(frames, 帧率, in_video, qp, interrupt_check=_interrupt_check)
            run_starlight(in_video, out_video, 放大倍数, b, w, h, 增强强度, model_id,
                          max_gpu_mem=显存上限, progress_cb=_progress_cb, interrupt_check=_interrupt_check)
            out_frames = read_video_to_frames(out_video, interrupt_check=_interrupt_check)
            print(f'[TopazStarlight] 完成: {out_frames.shape[0]}帧 @ {out_frames.shape[2]}x{out_frames.shape[1]}')
        except TopazUserInterrupt:
            mm.interrupt_current_processing(False)   # 清取消标志, 防止污染下一个任务
            raise mm.InterruptProcessingException() from None
        finally:
            for f in (in_video, out_video):
                try:
                    if os.path.isfile(f):
                        os.remove(f)
                except OSError:
                    pass

        pbar.update_absolute(b)
        tensor = torch.from_numpy(out_frames.astype(np.float32) / 255.0)
        return (tensor,)


NODE_CLASS_MAPPINGS = {
    "TopazStarlight": TopazStarlight,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TopazStarlight": "Topaz 星光2.6 超分",
}
