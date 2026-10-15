# -*- coding: utf-8 -*-
"""
🎓 ComfyUI 小语的工作台 (Xiaoyu Workbench)
================================================================================
📅 版本: v2.1.1 整合版
👨‍🏫 作者: 班长
💻 适用: ComfyUI

⚠️⚠️⚠️ AI 开发必读（人工也一样）⚠️⚠️⚠️
    新增 / 修改任何节点前：
      1. 先读本包根目录  AGENTS.md
      2. 带界面的节点（addDOMWidget / 自定义绘制 / 自定义 widget）
         必须再读  docs/AI节点开发铁律.md  并照抄其第 11 节骨架
    那里规定了新旧两套前端的尺寸声明、刷新恢复、隐藏 widget 合法写法、
    端口中文化等强制规则——违反会导致节点底部空白 / 面板裁切 / 刷新丢配置。
================================================================================

🎯 整合内容:
    ✅ 音频处理节点 (音频循环收集器、音频定长与补齐)
    ✅ 潜在空间工具 (潜在空间保存/加载)
    ✅ 图像处理节点 (全能分辨率、视频-图像桥接器)
    ✅ 文本工具 (智能保存、区间提取、分批、分割)
    ✅ 字幕工具 (视频字幕添加器、语音转脚本)
    ✅ 可视化加载器
    ✅ 图像编辑API (Banana2全能版)
    ✅ 智能视频分镜切割 (场景检测与视频分割)
    ✅ 显存管理 (清理内存 + 顶栏监控)
    ✅ 全能系统净化 (显存深度回收 + aimdo VBAR 归因 + 声音提示)
    ✅ 显存守卫 (显存预留 + 快速中断，合并节点)
    ✅ 开发工具 (URL下载、文件上传、执行时间统计、日志控制台)
    ✅ Topaz 星光超分 (星光2.6/Astra 视频增强放大, 合并自 ComfyUI-TopazStarlight)
    ✅ MiniMax H3 预览覆盖 (采样逐帧实时预览, 合并自 ComfyUI-MiniMaxH3-PreviewOverride)
    ✅ MiniMax H3 Latent 放大 (2D/3D 神经网络潜空间放大, 单节点 backend 切换, 合并自 Comfyui_Minimax_h3_latent_Upscaler)
    ✅ 图片高清放大二采重绘 (⚡插值/upscale_models模型放大 → VAE 编码(可分块), 图片版终极放大)
    ✅ 图片分块二采 (大图潜空间切块逐块采样 + 核心区余弦窗融合, 显存峰值≈单瓦片, 防 4K OOM)
    ✅ RTX 视频特效三件套 (超分降噪去模糊 / 转HDR / AI插帧, 显卡硬件加速不吃模型显存,
       需 NVIDIA RTX 显卡 + pip 装 nvidia-vfx; 改编自 Apache-2.0 的 ComfyUI_Nvidia_VFX_Nodes)
    ✅ 插帧 RIFE/EMA (开源模型, 不挑显卡, 20/30/40/50 系与 A 卡都能跑, 是 30 系插帧的解决方案;
       流式边解码边插帧边编码, 内存/显存峰值与片长无关;
       RIFE 可选 v4.26(最新·画质最佳) / v4.25(动漫最强·最稳) 两版权重,
       EMA-VFI(CVPR2023) 画质更高一档;
       权重缺失时自动下载; 分别改编自 MIT 的 Practical-RIFE 与 Apache-2.0 的 EMA-VFI)
    ✅ FlashVSR 扩散超分 (自包含, 权重读 models/FlashVSR, 不依赖官方插件与外部程序)
    ✅ DLSS5 帧生成插帧 (NVIDIA NGX DLSS-FG 2/3/4 倍与指定目标帧率; 引擎文件自备放 models/dlss5/dlssg,
       官方支持 RTX 40/50, RTX 30/20 系可打社区补丁 dlssg_for_sm86 用上——已实测通过;
       改编自 MIT 协议的 ComfyUI-RH-DLSS5)
================================================================================
"""

# ==============================================================================
# 导入所有模块的节点映射
# ==============================================================================

# 1. 音频节点
try:
    from . import banzhang_audio_nodes
    Audio_Cls = getattr(banzhang_audio_nodes, 'NODE_CLASS_MAPPINGS', {})
    Audio_Names = getattr(banzhang_audio_nodes, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 音频节点加载失败: {e}")
    Audio_Cls, Audio_Names = {}, {}

# 2. 潜在空间工具
try:
    from . import banzhang_latent_tools
    Latent_Cls = getattr(banzhang_latent_tools, 'NODE_CLASS_MAPPINGS', {})
    Latent_Names = getattr(banzhang_latent_tools, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 潜在空间工具加载失败: {e}")
    Latent_Cls, Latent_Names = {}, {}

# 3. 分辨率与图像处理
try:
    from . import banzhang_resolution
    Res_Cls = getattr(banzhang_resolution, 'NODE_CLASS_MAPPINGS', {})
    Res_Names = getattr(banzhang_resolution, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 分辨率节点加载失败: {e}")
    Res_Cls, Res_Names = {}, {}

# 4. 文本工具
try:
    from . import banzhang_text_tools
    Text_Cls = getattr(banzhang_text_tools, 'NODE_CLASS_MAPPINGS', {})
    Text_Names = getattr(banzhang_text_tools, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 文本工具加载失败: {e}")
    Text_Cls, Text_Names = {}, {}

# 5. 字幕工具
try:
    from . import banzhang_subtitle
    Sub_Cls = getattr(banzhang_subtitle, 'NODE_CLASS_MAPPINGS', {})
    Sub_Names = getattr(banzhang_subtitle, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 字幕工具加载失败: {e}")
    Sub_Cls, Sub_Names = {}, {}

# 6. 可视化加载器
try:
    from . import banzhang_visual_loader
    Vis_Cls = getattr(banzhang_visual_loader, 'NODE_CLASS_MAPPINGS', {})
    Vis_Names = getattr(banzhang_visual_loader, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 可视化加载器加载失败: {e}")
    Vis_Cls, Vis_Names = {}, {}

# 7. 图像编辑API
try:
    from . import banzhang_image_api
    API_Cls = getattr(banzhang_image_api, 'NODE_CLASS_MAPPINGS', {})
    API_Names = getattr(banzhang_image_api, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 图像API节点加载失败: {e}")
    API_Cls, API_Names = {}, {}

# 8. 显存守护工具函数 (无节点，被显存守卫调用)
try:
    from . import banzhang_fast_terminate
    Terminate_Cls, Terminate_Names = {}, {}
except Exception as e:
    print(f"⚠️ [小语的工作台] 显存守护工具加载失败: {e}")
    Terminate_Cls, Terminate_Names = {}, {}

# 9. 图像处理工具
try:
    from . import banzhang_image_splitter
    Splitter_Cls = getattr(banzhang_image_splitter, 'NODE_CLASS_MAPPINGS', {})
    Splitter_Names = getattr(banzhang_image_splitter, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 图像拆分器节点加载失败: {e}")
    Splitter_Cls, Splitter_Names = {}, {}


# 10. AI翻译器工具 (包含万能空节点、抓取神器、AI全能生成、Qwen3VL配置)
try:
    from . import banzhang_translator
    Trans_Cls = getattr(banzhang_translator, 'NODE_CLASS_MAPPINGS', {})
    Trans_Names = getattr(banzhang_translator, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] AI翻译器节点加载失败: {e}")
    Trans_Cls, Trans_Names = {}, {}

# 11. 额外工具 (音频保存、音频拼接、两宫格分割、替换首尾帧)
try:
    from . import banzhang_extra_tools
    Extra_Cls = getattr(banzhang_extra_tools, 'NODE_CLASS_MAPPINGS', {})
    Extra_Names = getattr(banzhang_extra_tools, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 额外工具节点加载失败: {e}")
    Extra_Cls, Extra_Names = {}, {}

# 12. 视频工具 (视频合并、音乐播放器、图像对比)
try:
    from . import banzhang_video_tools
    Video_Cls = getattr(banzhang_video_tools, 'NODE_CLASS_MAPPINGS', {})
    Video_Names = getattr(banzhang_video_tools, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 视频工具节点加载失败: {e}")
    Video_Cls, Video_Names = {}, {}

# 13. 智能视频分镜切割 (场景检测与分割)
try:
    from . import banzhang_scene_splitter
    Scene_Cls = getattr(banzhang_scene_splitter, 'NODE_CLASS_MAPPINGS', {})
    Scene_Names = getattr(banzhang_scene_splitter, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 视频分镜切割节点加载失败: {e}")
    Scene_Cls, Scene_Names = {}, {}

# 14. 显存管理 (显存预留、清理显存、清理内存 + 后台监控)
try:
    from . import banzhang_vram_reserved
    Vram_Cls = getattr(banzhang_vram_reserved, 'NODE_CLASS_MAPPINGS', {})
    Vram_Names = getattr(banzhang_vram_reserved, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 显存管理节点加载失败: {e}")
    Vram_Cls, Vram_Names = {}, {}

# 15. 全能显存净化 (v6.9 净空闲逻辑版 + 系统净化 + 声音提示)
try:
    from . import banzhang_vram_cleaner
    CleanVram_Cls = getattr(banzhang_vram_cleaner, 'NODE_CLASS_MAPPINGS', {})
    CleanVram_Names = getattr(banzhang_vram_cleaner, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 全能显存净化节点加载失败: {e}")
    CleanVram_Cls, CleanVram_Names = {}, {}

# 16. 类型转换器 (万能转 String/Int/Float/Bool)
try:
    from . import banzhang_type_converter
    Conv_Cls = getattr(banzhang_type_converter, 'NODE_CLASS_MAPPINGS', {})
    Conv_Names = getattr(banzhang_type_converter, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 类型转换器节点加载失败: {e}")
    Conv_Cls, Conv_Names = {}, {}

# 17. 开发工具 (URL下载、文件上传、执行时间统计、日志控制台)
try:
    from . import banzhang_dev_utils
    DevUtils_Cls = getattr(banzhang_dev_utils, 'NODE_CLASS_MAPPINGS', {})
    DevUtils_Names = getattr(banzhang_dev_utils, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 开发工具节点加载失败: {e}")
    DevUtils_Cls, DevUtils_Names = {}, {}

# 18. KSampler Sigmas 补丁 (内置 KSampler 恢复 sigmas 端口)
try:
    from . import banzhang_ksampler_patch
except Exception as e:
    print(f"⚠️ [小语的工作台] KSampler Sigmas 补丁加载失败: {e}")

# 20. 显存安全 LoRA 加载器 (实时融合，规避加速 LoRA 预融合导致的 OOM)
try:
    from . import banzhang_lora_loader
    LoraSafe_Cls = getattr(banzhang_lora_loader, 'NODE_CLASS_MAPPINGS', {})
    LoraSafe_Names = getattr(banzhang_lora_loader, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 显存安全 LoRA 加载器加载失败: {e}")
    LoraSafe_Cls, LoraSafe_Names = {}, {}

# 21. CGlide 视频工具集 (合并自 ComfyUI-CGlide)
try:
    from . import (
        csglide_seed,
        csglide_cast, csglide_join, csglide_media_rack,
    )
    Glide_Cls, Glide_Names = {}, {}
    for _m in (csglide_seed,
               csglide_cast, csglide_join, csglide_media_rack):
        Glide_Cls.update(getattr(_m, 'NODE_CLASS_MAPPINGS', {}))
        Glide_Names.update(getattr(_m, 'NODE_DISPLAY_NAME_MAPPINGS', {}))
except Exception as e:
    print(f"⚠️ [小语的工作台] CGlide 节点加载失败: {e}")
    Glide_Cls, Glide_Names = {}, {}

# 21a. H3 段落存档 / 调取（长片时间轴的存取层）
try:
    from . import csglide_clip
    Clip_Cls = getattr(csglide_clip, 'NODE_CLASS_MAPPINGS', {})
    Clip_Names = getattr(csglide_clip, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 段落存档加载失败: {e}")
    Clip_Cls, Clip_Names = {}, {}

# 21a-2. H3 文案台（提示词独立节点：媒体架 → 文案台 → 导演台）
try:
    from . import csglide_desk
    Desk_Cls = getattr(csglide_desk, 'NODE_CLASS_MAPPINGS', {})
    Desk_Names = getattr(csglide_desk, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 文案台加载失败: {e}")
    Desk_Cls, Desk_Names = {}, {}

# 21a-3. H3 AI 制片中枢（N1 制片中枢 / N2a 资产入库 / N2b 资产架 / N3 DAG编排器 / N4 段尾归位）
try:
    from . import banzhang_producer
    Producer_Cls = getattr(banzhang_producer, 'NODE_CLASS_MAPPINGS', {})
    Producer_Names = getattr(banzhang_producer, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 AI 制片中枢加载失败: {e}")
    Producer_Cls, Producer_Names = {}, {}

# CGlide 服务端路由 (/cglide/recent_outputs、/cglide/adopt_output、/cglide/probe)，仅注册路由不注册节点
try:
    from . import csglide_adopt  # noqa: F401
except Exception as e:
    print(f"⚠️ [小语的工作台] CGlide 路由加载失败: {e}")

# CGlide 续接合成剪辑面板路由 (/cglide/join/info、/thumb、/waveform)，仅注册路由不注册节点
try:
    from . import csglide_join_panel  # noqa: F401
except Exception as e:
    print(f"⚠️ [小语的工作台] 续接剪辑面板路由加载失败: {e}")

# 21b. H3 钉帧遮罩（长片时间轴地基：改一段不动后面的数学保证）
try:
    from . import banzhang_h3_pin
    Pin_Cls = getattr(banzhang_h3_pin, 'NODE_CLASS_MAPPINGS', {})
    Pin_Names = getattr(banzhang_h3_pin, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 钉帧遮罩加载失败: {e}")
    Pin_Cls, Pin_Names = {}, {}

# CGlide 长片段落档案路由 (/cglide/chain/summary、/clip、/update、/film、/resize、
# /seam、/current、/lock、/pending、/export_plan、/export、/sync_check)，仅注册路由不注册节点
try:
    from . import csglide_chain  # noqa: F401
except Exception as e:
    print(f"⚠️ [小语的工作台] 长片段落档案路由加载失败: {e}")

# 22. H3 SLA 注意力加速（内部走官方 comfy-kitchen sol-attn，合并自 ComfyUI-H3-SLA-Attention）
try:
    from . import banzhang_sla_attention
    Sla_Cls = getattr(banzhang_sla_attention, 'NODE_CLASS_MAPPINGS', {})
    Sla_Names = getattr(banzhang_sla_attention, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 SLA 注意力节点加载失败: {e}")
    Sla_Cls, Sla_Names = {}, {}

# 24. 孤海实用工具合并 (万能滑条、忽略多组、取色器 —— 去除孤海前缀，前端另有注释虚拟节点)
try:
    from . import banzhang_universal_slider
    Utils_Cls = getattr(banzhang_universal_slider, 'NODE_CLASS_MAPPINGS', {})
    Utils_Names = getattr(banzhang_universal_slider, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 万能滑条节点加载失败: {e}")
    Utils_Cls, Utils_Names = {}, {}

try:
    from . import banzhang_ignore_groups
    Utils_Cls.update(getattr(banzhang_ignore_groups, 'NODE_CLASS_MAPPINGS', {}))
    Utils_Names.update(getattr(banzhang_ignore_groups, 'NODE_DISPLAY_NAME_MAPPINGS', {}))
except Exception as e:
    print(f"⚠️ [小语的工作台] 忽略多组节点加载失败: {e}")

try:
    from . import banzhang_color_picker
    Utils_Cls.update(getattr(banzhang_color_picker, 'NODE_CLASS_MAPPINGS', {}))
    Utils_Names.update(getattr(banzhang_color_picker, 'NODE_DISPLAY_NAME_MAPPINGS', {}))
except Exception as e:
    print(f"⚠️ [小语的工作台] 取色器节点加载失败: {e}")

# 25. 注释虚拟节点 (后端桩：仅用于节点库归类，渲染由 js/banzhang_note.js 接管)
try:
    from . import banzhang_note
    Note_Cls = getattr(banzhang_note, 'NODE_CLASS_MAPPINGS', {})
    Note_Names = getattr(banzhang_note, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 注释节点加载失败: {e}")
    Note_Cls, Note_Names = {}, {}

# 26. Topaz 星光超分 (调用 Topaz neuroserver 外部引擎, 合并自 ComfyUI-TopazStarlight)
try:
    from . import banzhang_topaz_starlight
    Topaz_Cls = getattr(banzhang_topaz_starlight, 'NODE_CLASS_MAPPINGS', {})
    Topaz_Names = getattr(banzhang_topaz_starlight, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] Topaz 星光超分节点加载失败: {e}")
    Topaz_Cls, Topaz_Names = {}, {}

# 27. MiniMax H3 预览覆盖 (采样逐帧实时预览, 合并自 ComfyUI-MiniMaxH3-PreviewOverride)
try:
    from . import banzhang_mmh3_preview
    H3Prev_Cls = getattr(banzhang_mmh3_preview, 'NODE_CLASS_MAPPINGS', {})
    H3Prev_Names = getattr(banzhang_mmh3_preview, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] MiniMax H3 预览覆盖节点加载失败: {e}")
    H3Prev_Cls, H3Prev_Names = {}, {}

# 29. H3 Latent 放大 (2D/3D 合一) —— 单节点 backend 下拉切换 2D 主干(仅空间) / 3D 主干(时空联合)，合并自 Comfyui_Minimax_h3_latent_Upscaler
try:
    from . import banzhang_h3_latent_upscaler_3d
    H3Latent_Cls = getattr(banzhang_h3_latent_upscaler_3d, 'NODE_CLASS_MAPPINGS', {})
    H3Latent_Names = getattr(banzhang_h3_latent_upscaler_3d, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 Latent放大节点加载失败: {e}")
    H3Latent_Cls, H3Latent_Names = {}, {}

# 30. Qwen Image 2.1 编辑台 (参考图走 H3 媒体架/面板槽位，与导演台同一套素材接入口径)
try:
    from . import csglide_qwen
    Qwen_Cls = getattr(csglide_qwen, 'NODE_CLASS_MAPPINGS', {})
    Qwen_Names = getattr(csglide_qwen, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] Qwen 编辑台加载失败: {e}")
    Qwen_Cls, Qwen_Names = {}, {}

# 31. H3 终极放大（内置纯插值放大 + 空间分块重采样；只注册「参数 + 主节点」两个）
try:
    from . import banzhang_mmh3_upscale
    _H3Up_Cls = getattr(banzhang_mmh3_upscale, 'NODE_CLASS_MAPPINGS', {})
    _H3Up_Names = getattr(banzhang_mmh3_upscale, 'NODE_DISPLAY_NAME_MAPPINGS', {})
    H3Up_Cls = {k: v for k, v in _H3Up_Cls.items()
                if k in ("BanZhang_MMH3_UpscaleParams", "BanZhang_MMH3_Upscale")}
    H3Up_Names = {k: v for k, v in _H3Up_Names.items() if k in H3Up_Cls}
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 终极放大节点加载失败: {e}")
    H3Up_Cls, H3Up_Names = {}, {}

# 32. H3 Motion Context（MiniMax H3 片段续拍：真续声 + 潜空间钉帧，合并自 ComfyUI-H3-Motion-Context）
try:
    from . import _h3mc
    H3MC_Cls = getattr(_h3mc, 'NODE_CLASS_MAPPINGS', {})
    H3MC_Names = getattr(_h3mc, 'NODE_DISPLAY_NAME_MAPPINGS', {})
    # 只对菜单暴露「存档」一个：读档被导演台潜空间档的自动承接取代（执行时
    # 实时取最新存档，无需手动接线）；钉帧算术已被潜空间档内部调用，剪头由
    # 导演台「重叠帧」输出承担，链控被工程连续渲染取代，接缝检测是排查工具
    # ——这五个从菜单隐藏，但 _h3mc/ 代码完整保留（导演台在用）。
    for _k in ("MiniMaxH3MotionContext", "MiniMaxH3MotionContextTrim",
               "MiniMaxH3MotionContextLoadLatent",
               "MiniMaxH3MotionContextChain", "MiniMaxH3MotionContextSeamProbe"):
        H3MC_Cls.pop(_k, None)
        H3MC_Names.pop(_k, None)
    # 统一小语的工作台风格：中文显示名/分类/参数说明（只改显示层，不动 _h3mc 内部）
    from .banzhang_h3mc_zh import apply_zh as _h3mc_apply_zh
    _h3mc_apply_zh(H3MC_Cls, H3MC_Names)
    # 续拍存档：输出改潜空间直通（原顶部状态信息面板已于 2026-10-12
    # 应用户要求移除，banzhang_h3mc_status 只剩直通包装；同样不动 _h3mc 内部）
    from .banzhang_h3mc_status import apply_status as _h3mc_apply_status
    _h3mc_apply_status(H3MC_Cls)
    # 续拍存档 → 导演台段落档案镜像 + 「顺序锚」口（顺序在 _status 之后：
    # 本层包的是 _status 已经包过的 save）
    from .banzhang_h3mc_archive import apply_archive as _h3mc_apply_archive
    _h3mc_apply_archive(H3MC_Cls)
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 Motion Context 节点加载失败: {e}")
    H3MC_Cls, H3MC_Names = {}, {}

# 33. 自定义 Sigma 曲线 (前端可视化画曲线，输出 SIGMAS + 步数，直连 KSampler sigmas 口)
try:
    from . import banzhang_sigmas_custom
    Sigma_Cls = getattr(banzhang_sigmas_custom, 'NODE_CLASS_MAPPINGS', {})
    Sigma_Names = getattr(banzhang_sigmas_custom, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 自定义 Sigma 曲线节点加载失败: {e}")
    Sigma_Cls, Sigma_Names = {}, {}

# 35. H3 VAE 快速解码 (可调瓦片 + 解码时 PixelShuffle 2x 放大, 合并自 ComfyUI-MiniMaxH3_LatentUpscaler)
try:
    from . import banzhang_h3_vae_decode
    H3VAEDec_Cls = getattr(banzhang_h3_vae_decode, 'NODE_CLASS_MAPPINGS', {})
    H3VAEDec_Names = getattr(banzhang_h3_vae_decode, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] H3 VAE 快速解码节点加载失败: {e}")
    H3VAEDec_Cls, H3VAEDec_Names = {}, {}

# 36. FlashVSR 扩散超分 (自包含, 权重读 models/FlashVSR, 不依赖官方插件与外部程序)
try:
    from . import banzhang_flashvsr
    FlashVSR_Cls = getattr(banzhang_flashvsr, 'NODE_CLASS_MAPPINGS', {})
    FlashVSR_Names = getattr(banzhang_flashvsr, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] FlashVSR 扩散超分节点加载失败: {e}")
    FlashVSR_Cls, FlashVSR_Names = {}, {}

# 37. DLSS5 帧生成插帧 (NVIDIA NGX DLSS-FG, 需自备 models/dlss5/dlssg 引擎文件;
#     RTX 40/50 官方支持, RTX 30 及更早不支持)
try:
    from . import banzhang_dlss5_fg
    DLSS5FG_Cls = getattr(banzhang_dlss5_fg, 'NODE_CLASS_MAPPINGS', {})
    DLSS5FG_Names = getattr(banzhang_dlss5_fg, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] DLSS5 帧生成插帧节点加载失败: {e}")
    DLSS5FG_Cls, DLSS5FG_Names = {}, {}

# 38. 图片高清放大（二采重绘）(「放大模型」下拉: ⚡插值档/upscale_models 神经网络模型档,
#     → VAE 编码(可分块); 二采 denoise 交官方 K 采样器; 渐进省显存另接 34b 图片渐进补丁)
try:
    from . import banzhang_image_upscale
    HiresUp_Cls = getattr(banzhang_image_upscale, 'NODE_CLASS_MAPPINGS', {})
    HiresUp_Names = getattr(banzhang_image_upscale, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] 图片高清放大二采节点加载失败: {e}")
    HiresUp_Cls, HiresUp_Names = {}, {}

# 39. RTX 视频特效三件套 (NVIDIA RTX SDK 硬件加速: 超分/降噪/去模糊 + 转HDR + AI插帧;
#     需 NVIDIA RTX 显卡, 依赖 nvidia-vfx 未安装时本模块不注册节点并给出安装提示)
try:
    from . import banzhang_nvidia_vfx
    VFX_Cls = getattr(banzhang_nvidia_vfx, 'NODE_CLASS_MAPPINGS', {})
    VFX_Names = getattr(banzhang_nvidia_vfx, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] RTX 视频特效节点加载失败: {e}")
    VFX_Cls, VFX_Names = {}, {}

# 40. 插帧 RIFE/EMA (开源模型, 不挑显卡, 30 系插帧的解决方案; 权重缺失时自动下载)
#     RIFE: v4.26 / v4.25 双版权重可选;  EMA-VFI: 画质更高一档(固定单精度)
try:
    from . import banzhang_rife
    RIFE_Cls = getattr(banzhang_rife, 'NODE_CLASS_MAPPINGS', {})
    RIFE_Names = getattr(banzhang_rife, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] RIFE 插帧节点加载失败: {e}")
    RIFE_Cls, RIFE_Names = {}, {}

# 41. YuE2 智能乐谱中枢 (一个节点收口 ABC：智能路由文生规划 / 翻唱转谱 / 手动 / 直生)
try:
    from . import banzhang_yue2_smart
    SmartPlan_Cls = getattr(banzhang_yue2_smart, 'NODE_CLASS_MAPPINGS', {})
    SmartPlan_Names = getattr(banzhang_yue2_smart, 'NODE_DISPLAY_NAME_MAPPINGS', {})
except Exception as e:
    print(f"⚠️ [小语的工作台] YuE2 智能乐谱中枢加载失败: {e}")
    SmartPlan_Cls, SmartPlan_Names = {}, {}

# ==============================================================================
# 合并所有节点映射
# ==============================================================================
NODE_CLASS_MAPPINGS = {
    **Audio_Cls,
    **Latent_Cls,
    **Res_Cls,
    **Text_Cls,
    **Sub_Cls,
    **Vis_Cls,
    **API_Cls,
    **Terminate_Cls,
    **Splitter_Cls,
    **Trans_Cls,
    **Extra_Cls,
    **Video_Cls,
    **Scene_Cls,
    **Vram_Cls,
    **CleanVram_Cls,
    **Conv_Cls,
    **DevUtils_Cls,
    **LoraSafe_Cls,
    **Glide_Cls,
    **Sla_Cls,
    **H3Latent_Cls,
    **Utils_Cls,
    **Note_Cls,
    **Topaz_Cls,
    **H3Prev_Cls,
    **Qwen_Cls,
    **H3Up_Cls,
    **H3MC_Cls,
    **Sigma_Cls,
    **H3VAEDec_Cls,
    **FlashVSR_Cls,
    **DLSS5FG_Cls,
    **HiresUp_Cls,
    **VFX_Cls,
    **RIFE_Cls,
    **Clip_Cls,
    **Desk_Cls,
    **Producer_Cls,
    **Pin_Cls,
    **SmartPlan_Cls,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    **Audio_Names,
    **Latent_Names,
    **Res_Names,
    **Text_Names,
    **Sub_Names,
    **Vis_Names,
    **API_Names,
    **Terminate_Names,
    **Splitter_Names,
    **Trans_Names,
    **Extra_Names,
    **Video_Names,
    **Scene_Names,
    **Vram_Names,
    **CleanVram_Names,
    **Conv_Names,
    **DevUtils_Names,
    **LoraSafe_Names,
    **Glide_Names,
    **Sla_Names,
    **H3Latent_Names,
    **Utils_Names,
    **Note_Names,
    **Topaz_Names,
    **H3Prev_Names,
    **Qwen_Names,
    **H3Up_Names,
    **H3MC_Names,
    **Sigma_Names,
    **H3VAEDec_Names,
    **FlashVSR_Names,
    **DLSS5FG_Names,
    **HiresUp_Names,
    **VFX_Names,
    **RIFE_Names,
    **Clip_Names,
    **Desk_Names,
    **Producer_Names,
    **Pin_Names,
    **SmartPlan_Names,
}

# ==============================================================================
# Web 前端目录配置
# ==============================================================================
# 创建 js 目录存放前端文件
WEB_DIRECTORY = "./js"

# ==============================================================================
# 启动信息
# ==============================================================================
总节点数 = len(NODE_CLASS_MAPPINGS)

print("=" * 60)
print("🎓👨‍🏫 小语的工作台 (Xiaoyu Workbench) v2.1 整合版")
print("=" * 60)
print(f"✅ 成功加载 {总节点数} 个节点:")

if Audio_Cls:
    print(f"   🎵 音频节点: {len(Audio_Cls)} 个")
if Latent_Cls:
    print(f"   🌀 潜在空间: {len(Latent_Cls)} 个")
if Res_Cls:
    print(f"   🖼️ 图像处理: {len(Res_Cls)} 个")
if Text_Cls:
    print(f"   📝 文本工具: {len(Text_Cls)} 个")
if Sub_Cls:
    print(f"   🎬 字幕工具: {len(Sub_Cls)} 个")
if Vis_Cls:
    print(f"   👁️ 可视化: {len(Vis_Cls)} 个")
if API_Cls:
    print(f"   🤖 图像API: {len(API_Cls)} 个")
if Splitter_Cls:
    print(f"   ✂️ 图像拆分: {len(Splitter_Cls)} 个")
if Trans_Cls:
    print(f"   🤖 AI翻译器: {len(Trans_Cls)} 个")
if Extra_Cls:
    print(f"   🎵 额外工具: {len(Extra_Cls)} 个")
if Video_Cls:
    print(f"   🎬 视频工具: {len(Video_Cls)} 个")
if Scene_Cls:
    print(f"   🍿 视频分镜: {len(Scene_Cls)} 个")
if Vram_Cls:
    print(f"   💾 显存管理: {len(Vram_Cls)} 个")
if CleanVram_Cls:
    print(f"   🧹 全能净化: {len(CleanVram_Cls)} 个")
if Conv_Cls:
    print(f"   🔄 类型转换: {len(Conv_Cls)} 个")
if DevUtils_Cls:
    print(f"   🛠️ 开发工具: {len(DevUtils_Cls)} 个")
if LoraSafe_Cls:
    print(f"   ️ 显存安全LoRA: {len(LoraSafe_Cls)} 个")
if Glide_Cls:
    print(f"   🎥 CGlide视频: {len(Glide_Cls)} 个")
if Clip_Cls:
    print(f"   📦 H3段落存档: {len(Clip_Cls)} 个")
if Desk_Cls:
    print(f"   ✍️ H3文案台: {len(Desk_Cls)} 个")
if Pin_Cls:
    print(f"   🔒 H3钉帧遮罩: {len(Pin_Cls)} 个")
if Sla_Cls:
    print(f"   🧠 H3 SLA注意力: {len(Sla_Cls)} 个")
if H3Latent_Cls:
    print(f"   🎬 H3 Latent放大(2D/3D): {len(H3Latent_Cls)} 个")
if Utils_Cls:
    print(f"   🧰 孤海实用工具: {len(Utils_Cls)} 个")
if Topaz_Cls:
    print(f"   ⭐ Topaz星光超分: {len(Topaz_Cls)} 个")
if H3Prev_Cls:
    print(f"   👁️ MiniMax H3预览覆盖: {len(H3Prev_Cls)} 个")
if Qwen_Cls:
    print(f"   🎨 Qwen编辑台: {len(Qwen_Cls)} 个")
if H3MC_Cls:
    print(f"   🎞️ H3 Motion Context: {len(H3MC_Cls)} 个")
if H3Up_Cls:
    print(f"   🔍 H3 终极放大: {len(H3Up_Cls)} 个")
if Sigma_Cls:
    print(f"   📈 自定义Sigma曲线: {len(Sigma_Cls)} 个")
if H3VAEDec_Cls:
    print(f"   🎬 H3 VAE快速解码: {len(H3VAEDec_Cls)} 个")
if FlashVSR_Cls:
    print(f"   ✨ FlashVSR扩散超分: {len(FlashVSR_Cls)} 个")
if DLSS5FG_Cls:
    print(f"   🎞️ DLSS5帧生成插帧: {len(DLSS5FG_Cls)} 个")
if HiresUp_Cls:
    print(f"   🖼️ 图片高清放大二采: {len(HiresUp_Cls)} 个")
if VFX_Cls:
    print(f"   🖼️ RTX视频特效(超分/HDR/插帧): {len(VFX_Cls)} 个")
if RIFE_Cls:
    print(f"   🎞️ 插帧 RIFE/EMA(全显卡通用·流式): {len(RIFE_Cls)} 个")
if SmartPlan_Cls:
    print(f"   🎼 YuE2智能乐谱中枢: {len(SmartPlan_Cls)} 个")

print("=" * 60)
print("🚀 所有节点已准备就绪！开始创作吧！")
print("=" * 60)

# ==============================================================================
# 导出声明
# ==============================================================================
__all__ = [
    'NODE_CLASS_MAPPINGS',
    'NODE_DISPLAY_NAME_MAPPINGS',
    'WEB_DIRECTORY'
]
