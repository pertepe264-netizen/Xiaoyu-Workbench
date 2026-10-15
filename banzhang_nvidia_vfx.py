# -*- coding: utf-8 -*-
"""ComfyUI 节点：NVIDIA RTX 视频特效三件套（超分 / 转HDR / 插帧）

显卡硬件加速，不下载 AI 模型、不占模型显存，走的是 NVIDIA 驱动里的 RTX SDK。
显卡干活的那部分在 banzhang_nvidia_vfx_engine.py。

硬门槛：
    - NVIDIA 带 Tensor Core 的显卡（RTX 20 系及以上）、驱动 ≥ 570.65（视频插帧仅 40/50 系）
    - CUDA 版 PyTorch（ComfyUI 官方 CUDA 整合包即可）
    - 依赖 nvidia-vfx（首次启动自动装；真 wheel 仅 NVIDIA 官方源有，
      公共 PyPI 是空壳；也可双击 Install_RTX_VFX_Deps.bat，或放 wheels/ 离线装）

改编自 Apache-2.0 协议的 Comfy-Org/ComfyUI_Nvidia_VFX_Nodes，
2026-10-09 并入小语的工作台。
2026-10-13：🔍 超分降噪节点新增「视频」输入/输出（流式边解码边超分边编码，
原音直拷，兼容与「🎥 视频合并」一致的全部 VIDEO 形态）；取消「状态」输出口
（处理信息改打在控制台）。
"""
import gc

import torch

from .banzhang_dlss5_fg_engine import as_fps
from .banzhang_dlss5_fg_media import (
    StreamEncoder,
    build_source,
    build_video,
    resolve_video_path,
    video_from_file,
)
from .banzhang_nvidia_vfx_engine import (
    ENCODINGS,
    FRAME_MODES,
    HDR_COLORSPACES,
    NvidiaVFXError,
    QUALITY_LEVELS,
    ensure_sdk,
    frame_generation,
    plan_output_size,
    super_resolution,
    super_resolution_stream,
    true_hdr,
)

CAT_IMAGE = "👨‍🏫班长/图像处理"
CAT_VIDEO = "👨‍🏫班长/视频处理"


def 进度回调():
    """进度条 + 随时响应"中断"按钮。"""
    import comfy.model_management as mm
    from comfy.utils import ProgressBar

    bar = ProgressBar(100)

    def callback(done, total):
        bar.update_absolute(int(100 * done / max(1, total)))
        mm.throw_exception_if_processing_interrupted()

    return callback


def run_guarded(动作: str, fn):
    """把显卡 SDK 的英文报错翻译成人话。"""
    try:
        return fn()
    except NvidiaVFXError:
        raise
    except Exception as exc:
        if 动作 == "插帧":
            可能 = "RTX 视频插帧官方只支持 RTX 40 / RTX 50 系列显卡（RTX 30 及更早不支持）"
        else:
            可能 = "显存不够、尺寸太大、驱动太旧"
        raise NvidiaVFXError(
            f"RTX{动作}失败：{exc}\n"
            f"常见原因：{可能}；CUDA 版 PyTorch 没装对；一次性喂的帧太多。"
            f"可以试着降分辨率、降档位、减少一次处理的帧数。") from None


def _vfx_log(msg: str) -> None:
    print(f"[班长RTX特效] {msg}", flush=True)


def _video_fps(components):
    """从视频组件里把源帧率抠出来（保持分数精度）。"""
    config = getattr(components, "config", None)
    fps = getattr(config, "fps", None) if config is not None else None
    if fps:
        return as_fps(fps)
    frame_rate = getattr(components, "frame_rate", None)
    return as_fps(frame_rate if frame_rate is not None else 24)


class BanZhang_RTX_SuperResolution:
    """放大 / 降噪 / 去模糊。图像、视频都能进；视频走流式管线，内存与片长无关。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "缩放方式": (["按倍数放大", "指定输出尺寸"], {"default": "按倍数放大",
                         "tooltip": "「按倍数放大」= 在下面填几倍（比如 2 就是长宽各变 2 倍）；"
                                    "「指定输出尺寸」= 在下面直接填想要的宽高。"
                                    "注意：档位选「降噪」或「去模糊」时，无论这里选什么都保持原尺寸。"}),
                "放大倍数": ("FLOAT", {"default": 2.0, "min": 1.0, "max": 4.0, "step": 0.01,
                                     "tooltip": "仅「按倍数放大」时有效。2.0 = 长宽各 ×2（面积 ×4）。"
                                                "实际输出会对齐到 8 的倍数，可能差几个像素，正常。"}),
                "输出宽度": ("INT", {"default": 1920, "min": 64, "max": 8192, "step": 8,
                                   "tooltip": "仅「指定输出尺寸」时有效。想放大填大数，"
                                              "想缩小也行（比如 3840 → 1920）。"}),
                "输出高度": ("INT", {"default": 1080, "min": 64, "max": 8192, "step": 8,
                                   "tooltip": "仅「指定输出尺寸」时有效，同上。"}),
                "处理档位": (list(QUALITY_LEVELS), {"default": "常规放大·极致",
                         "tooltip": "选哪个决定了它干什么，选错等于白跑：\n"
                                    "· 常规放大：有压缩痕迹的普通片源（网上下载的视频、AI 出图），"
                                    "越往下越清晰越慢。\n"
                                    "· 降噪：只去噪点和压缩块，不改尺寸；档位越高去得越狠，"
                                    "但细节也会跟着变糊。\n"
                                    "· 去模糊：只锐化，拯救对焦不实/糊掉的画面，不改尺寸。\n"
                                    "· 高清片源：给干净的无损/高码率素材放大，不会多余地抹细节。\n"
                                    "· 直播流：摄像头、直播、视频会议画面专用。\n"
                                    "· 纯插值放大：不用 AI 的双三次插值，最快，只是纯粹拉大。"}),
                "强度": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                                 "tooltip": "效果强弱。1.0 拉满；觉得处理过头（太锐/太糊）"
                                            "就往下调到 0.5 左右试试。"}),
                "像素精度": (list(ENCODINGS), {"default": "8位",
                         "tooltip": "显卡内部用什么精度算。「10位(更精细)」明暗层次更细腻、"
                                    "暗部更干净，但更吃显存也稍慢；"
                                    "它不会把普通画面变成 HDR，那是「RTX 转HDR」节点的事。"}),
            },
            "optional": {
                # 两个输入口的顺序即节点上的上下顺序：图像在上、视频在下
                "图像": ("IMAGE", {"tooltip": "要处理的图片，或视频解码出来的一批帧，批次顺序就是时间顺序。"
                                              "与「视频」至少接一个；只接图像时，除了图像输出，"
                                              "还会按「序列源帧率」封装出一段视频。"}),
                "视频": ("VIDEO", {"tooltip": "推荐入口：接「加载视频」/插帧/FlashVSR 等节点的 VIDEO 输出，"
                                             "文件路径型（含 FlashVSR 直出的 mp4 路径）、内存型、"
                                             "上传文件都认，和「🎥 视频合并」的视频入口一致。"
                                             "走边解码边超分边编码，内存/显存峰值与片长无关，"
                                             "原音轨默认直拷到输出视频。"}),
                "序列源帧率": ("INT", {"default": 24, "min": 1, "max": 240, "step": 1,
                             "tooltip": "仅「图像」输入时有效：这批图当作多少 fps 的源，"
                                        "输出视频按此帧率封装。"}),
                "保留原音": (["开", "关"], {"default": "开",
                         "tooltip": "仅「视频」输入时有效：开 = 输出视频带上源片音轨"
                                    "（有源文件时从文件直拷，不重编码）；关 = 无声。"}),
            },
        }

    # 输出只留「图像」「视频」；处理信息打在控制台，不再占一个状态口
    RETURN_TYPES = ("IMAGE", "VIDEO")
    RETURN_NAMES = ("图像", "视频")
    FUNCTION = "run"
    CATEGORY = CAT_IMAGE

    def run(self, 缩放方式, 放大倍数, 输出宽度, 输出高度, 处理档位, 强度, 像素精度,
            图像=None, 视频=None, 序列源帧率=24, 保留原音="开"):
        if 图像 is None and 视频 is None:
            raise NvidiaVFXError("没有输入：把「图像」或「视频」接到节点上再跑。")

        out_image, out_video = None, None

        if 图像 is not None:
            out_image, img_note = run_guarded("超分", lambda: super_resolution(
                图像, resize_type=缩放方式, scale=放大倍数, width=输出宽度, height=输出高度,
                quality=处理档位, strength=强度, encoding=像素精度, progress=进度回调()))
            _vfx_log(img_note)
            src_fps = as_fps(max(1, int(序列源帧率 or 24)))
            video_obj, vnote = build_video(out_image, src_fps, None, None)
            if video_obj is not None:
                out_video = video_obj
            _vfx_log(vnote)

        if 视频 is not None:
            out_video, vid_note = run_guarded("超分", lambda: self._run_video(
                视频, resize_type=缩放方式, scale=放大倍数, width=输出宽度, height=输出高度,
                quality=处理档位, strength=强度, encoding=像素精度,
                keep_audio=(保留原音 == "开"), progress=进度回调()))
            _vfx_log(vid_note)

        return out_image, out_video

    # ------------------------------------------------------------ 视频分支

    def _run_video(self, video, *, resize_type, scale, width, height, quality,
                   strength, encoding, keep_audio, progress):
        """视频流式超分：边解码边超分边编码，效果器只创建一次，原音直拷。"""
        source_path = resolve_video_path(video, log=_vfx_log)
        source_video_for_copy = (source_path or video) if keep_audio else None
        chunked = build_source(source_path, log=_vfx_log) if source_path else None

        def run_stream(frames_iter, total, src_w, src_h, fps, audio):
            out_w, out_h = plan_output_size(src_h, src_w, resize_type, scale,
                                            width, height, quality)
            encoder = StreamEncoder(out_w, out_h, fps, audio, source_video_for_copy)
            try:
                out_w, out_h, _ = super_resolution_stream(
                    frames_iter, total, src_w, src_h, encoder.write,
                    resize_type=resize_type, scale=scale, width=width, height=height,
                    quality=quality, strength=strength, encoding=encoding,
                    progress=progress)
                encoder.close()
            except BaseException:
                encoder.abort()
                raise
            return encoder, out_w, out_h

        if chunked is not None:
            # 首选：ffmpeg 管道分块解码，内存里永远只有几帧
            reader, source = chunked
            src_fps = as_fps(reader.fps if reader.fps > 0 else 24)
            total_in, src_h, src_w = int(reader.count), int(reader.height), int(reader.width)
            _vfx_log(f"流式解码 {total_in} 帧 {src_w}x{src_h} @{float(src_fps):g}fps")

            def frames():
                for i in range(total_in):
                    rgba, _ = source(i)
                    yield rgba[..., :3]

            encoder, out_w, out_h = run_stream(frames(), total_in, src_w, src_h, src_fps, None)
            mode = "边解码边超分边编码"
        else:
            # 兜底：拿不到文件路径（内存里现拼的 VIDEO），整段解码进内存再流式编码
            components_getter = getattr(video, "get_components", None)
            if not callable(components_getter):
                raise NvidiaVFXError(
                    "视频输入拿不到可读文件路径，也不是内存 VIDEO 对象，无法处理；"
                    "请改接「加载视频」或上游视频节点的 VIDEO 输出。")
            components = components_getter()
            tensor = components.images
            src_fps = _video_fps(components)
            total_in, src_h, src_w = (int(tensor.shape[0]), int(tensor.shape[1]),
                                      int(tensor.shape[2]))
            audio = components.audio if keep_audio else None

            def frames():
                for i in range(total_in):
                    f = tensor[i].detach().cpu().mul(255.0).round_().clamp_(0.0, 255.0)
                    yield f.to(torch.uint8).numpy()[..., :3]

            encoder, out_w, out_h = run_stream(frames(), total_in, src_w, src_h, src_fps, audio)
            mode = "流式超分编码"

        gc.collect()
        out = video_from_file(encoder.path)
        audio_note = "音轨从源视频直拷" if encoder.source else "无音轨"
        note = (f"{src_w}x{src_h} → {out_w}x{out_h}，{total_in} 帧 @{float(src_fps):g}fps，"
                f"{mode}，{audio_note}")
        return out, note


class BanZhang_RTX_TrueHDR:
    """普通画面 → HDR 数据。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE", {"tooltip": "普通的（SDR）图片或视频帧。尺寸不会变，"
                                              "只改明暗和色彩的编码方式。"}),
                "对比度": ("INT", {"default": 100, "min": 0, "max": 200, "step": 1,
                                 "tooltip": "输出画面对比强弱。100 = 原样；想更通透可以提到 110~130，"
                                            "觉得死黑死白就往下调。"}),
                "饱和度": ("INT", {"default": 100, "min": 0, "max": 200, "step": 1,
                                 "tooltip": "输出色彩浓淡。100 = 原样。HDR 色域比普通画面大，"
                                            "一般 100~120 就够，调太高会艳得发假。"}),
                "中间灰": ("INT", {"default": 50, "min": 10, "max": 100, "step": 1,
                                 "tooltip": "画面里「中间亮度」定在多高，决定整体亮还是暗。"
                                            "画面偏暗就调高，偏亮/高光糊成一片就调低。默认 50 最稳。"}),
                "峰值亮度": ("INT", {"default": 650, "min": 400, "max": 2000, "step": 10,
                                   "tooltip": "按多少尼特的 HDR 屏幕来映射（单位 nits）。"
                                              "普通 HDR 电视/显示器填 600~1000，"
                                              "高端高亮度屏可以填 1000~2000。"
                                              "这个值要和实际播放设备对得上，"
                                              "否则要么高光削平、要么整片发暗。"}),
                "去色带": (["开", "关"], {"default": "开",
                         "tooltip": "开 = 顺手做一次去色带（把天空、暗部那种一圈圈色块抹平）。"
                                    "画面本来很干净也可以关掉，省一点时间。"}),
                "输出色彩": (list(HDR_COLORSPACES), {"default": "HDR(HLG,电视通用)",
                         "tooltip": "「HLG」= 电视/流媒体通用，不挑屏幕，直接播就能看，推荐默认选它；"
                                    "「PQ」= 电影和蓝光常用，高光更准，但需要播放端支持 PQ 元数据。"
                                    "⚠️ 本节点输出的是 HDR 数值，ComfyUI 的图本身不带 HDR 标记，"
                                    "所以存出来看着发灰是正常的——下游保存/编码时必须按 BT.2020 标记，"
                                    "千万别当普通图看。"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    FUNCTION = "run"
    CATEGORY = CAT_IMAGE

    def run(self, 图像, 对比度, 饱和度, 中间灰, 峰值亮度, 去色带, 输出色彩):
        return run_guarded("转HDR", lambda: true_hdr(
            图像, contrast=对比度, saturation=饱和度, middle_gray=中间灰,
            luminance=峰值亮度, debanding=(去色带 == "开"), colorspace=输出色彩,
            progress=进度回调()))


class BanZhang_RTX_FrameGeneration:
    """在相邻帧之间补 AI 帧：提帧率 / 做慢动作。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "图像": ("IMAGE", {"tooltip": "按顺序排好的视频帧（比如「加载视频」解码出来的一批）。"
                                              "至少要有 2 帧，只有 1 帧会原样返回。"
                                              "⚠️ 插帧对显卡要求最高：官方只支持 RTX 40 / RTX 50 系列，"
                                              "RTX 30 及更早会直接报错（此时请用「DLSS5 帧生成插帧」"
                                              "或别家的插帧节点）。"}),
                "生成方式": (["按倍数插帧", "指定中间位置"], {"default": "按倍数插帧",
                         "tooltip": "「按倍数插帧」= 均匀地把帧率翻几倍，正经提帧率/做慢动作用这个；"
                                    "「指定中间位置」= 每两帧之间只补 1 帧，"
                                    "并且由你指定补在什么时间点，用来抽帧或非匀速。"}),
                "帧率倍数": ("INT", {"default": 2, "min": 2, "max": 16, "step": 1,
                                   "tooltip": "仅「按倍数插帧」时有效。2 = 24fps 变 48fps。"
                                              "倍数越高越吃显卡也越慢，一般 2~4 就够。"}),
                "中间位置": ("FLOAT", {"default": 0.5, "min": 0.01, "max": 0.99, "step": 0.01,
                                     "tooltip": "仅「指定中间位置」时有效。0 = 贴着前一帧，"
                                                "1 = 贴着后一帧，0.5 = 正中间（等于 2 倍插帧）。"
                                                "填 0.5 以外的值，帧与帧的间隔就不均匀了。"}),
                "画质档": (list(FRAME_MODES), {"default": "中",
                         "tooltip": "补帧质量。「中」画质和速度最平衡；"
                                    "「高」运动更准、拖影更少但明显更慢；"
                                    "「低」最快，快速运动处容易出现糊影。"}),
                "自动识别转场": (["开", "关"], {"default": "开",
                         "tooltip": "开 = 让显卡自己判断「换镜头了」，换镜头处不补帧，"
                                    "避免上一镜的画面串到下一镜（鬼影）。一般保持开着。"}),
                "强制标记为转场": (["开", "关"], {"default": "关",
                         "tooltip": "高级选项。开 = 把每一对帧都当成「这里是切镜」，"
                                    "也就是不补中间帧。只有你确定素材就是一堆独立镜头、"
                                    "或者只想处理一对帧时才开。"}),
                "像素精度": (list(ENCODINGS), {"default": "8位",
                         "tooltip": "显卡内部用什么精度算。「10位(更精细)」明暗过渡更细腻，"
                                    "更吃显存也稍慢；它不会把普通画面变成 HDR。"}),
            },
        }

    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("图像", "状态")
    FUNCTION = "run"
    CATEGORY = CAT_VIDEO

    def run(self, 图像, 生成方式, 帧率倍数, 中间位置, 画质档, 自动识别转场, 强制标记为转场,
            像素精度):
        return run_guarded("插帧", lambda: frame_generation(
            图像, generation_type=生成方式, multiplier=帧率倍数, timestep=中间位置,
            mode=画质档, auto_shot_change=(自动识别转场 == "开"),
            force_shot_change=(强制标记为转场 == "开"), encoding=像素精度,
            progress=进度回调()))


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

# 没有依赖就自动装一次（装不上就先不注册，免得点了才发现动不了）
if ensure_sdk():
    NODE_CLASS_MAPPINGS = {
        "BanZhang_RTX_SuperResolution": BanZhang_RTX_SuperResolution,
        "BanZhang_RTX_TrueHDR": BanZhang_RTX_TrueHDR,
        "BanZhang_RTX_FrameGeneration": BanZhang_RTX_FrameGeneration,
    }
    NODE_DISPLAY_NAME_MAPPINGS = {
        "BanZhang_RTX_SuperResolution": "🔍 RTX 超分降噪",
        "BanZhang_RTX_TrueHDR": "🌈 RTX 转HDR",
        "BanZhang_RTX_FrameGeneration": "🎞️ RTX 视频插帧",
    }
else:
    print("⚠️ [小语的工作台] RTX 超分 / 转HDR / 插帧 三个节点暂未启用（缺 nvidia-vfx），"
          "其余节点不受影响；双击插件目录里的 Install_RTX_VFX_Deps.bat 可一键补装"
          "（自动优先用 wheels/ 离线包，再走 NVIDIA 官方源），装好后重启 ComfyUI。")
