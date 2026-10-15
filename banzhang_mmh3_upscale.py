"""MiniMax H3 终极放大（2 节点整合版）—— 移植自 LBH-123-AI 的
Minimax H3 Latent Upscaler (3D) + MMH3 Split Upscale，合并回「内置放大 + 空间分块重采样」形态。

- 放大：神经网络潜空间放大（3D，三模式：按倍数/目标尺寸/兆像素）
- 空间分块重采样：沿长边单轴切条，重采样后按「核心区独占」融合（每个像素只由唯一一块生成）
  - Euler 采样器：逐步同步——每 sigma 步算完所有 tile 融合成全局再推进，消除接缝
  - 非 Euler：逐块完整采样 + 核心区窗口融合
  - 音频用「音频重绘强度」控制（不参与空间分块）：0=直接透传原始音频，1=全部重绘，
    中间值为潜空间线性混合比例
  - 简化：overlap/fade 百分比参数

════════════════════════════════════════════════════════════════════════════════
  关键机制备忘（务必先读，勿乱改）
════════════════════════════════════════════════════════════════════════════════

【干净上下文机制】—— 本节点消除「接缝 / 分身（小人）」的核心，模型档与插值档共用。
  - 分块采样时，每块用「核心区二值掩码」作为 denoise_mask 注入采样：
      1 区 = 核心区（本块唯一负责生成、且唯一被采纳的像素范围）；
      0 区 = overlap 边带（保留放大后的原始像素，t≈0.999，只作上下文条件喂给模型）。
  - 融合窗口与掩码完全同形（核心区二值）：相邻块核心区严格互补覆盖 → 每个像素只由
    唯一一块生成，既不会把静态底图混进接缝（重影），也不会让相邻两块在同一处各生成
    一遍（接缝处多余重复画面）。
  - Euler 路径额外做「干净上下文画布」：块外余量每步换成静态底图按视觉条件时间步
    （t≈0.999）重加噪注入，让相邻块边界两侧看到同一份内容逐步收敛（防接缝/防分身）。

【1.5x 分块奇数起点/宽度 → 接缝错位】
  - H3 模型按 2×2 patch 网格工作（坐标、offset 均以 latent 格 // 2 计）。
  - tile 起点与 stride 必须对齐到 2 的倍数（ALIGN=2），否则 patch 坐标错位半格，
    非整数倍放大（如 1.5x）时相邻块接缝处出现重影/错位。
  - 已修：_compute_tile_starts 里 stride/overlap 均按 ALIGN 对齐偶数。

【三线性插值档「整幅糊 + 重影」根因 与 预显影解法】—— 重要，勿删预显影逻辑
  问题：
    模型档底图是神经网络放大产物，含高频、符合真实 latent 分布；插值档底图是
    F.interpolate 三线性插值的纯数学拉伸，缺高频、偏离真实分布（糊）。
    干净上下文机制默认「底图 = 可信干净内容」，模型档成立、插值档不成立：
    把模糊插值底图当干净参照钉进 overlap 边带 → 模型被告知"周围就是这么糊" →
    核心区也照着糊底图重绘 → 整幅糊 + 生成内容与模糊带对不上（重影）。
  关键澄清：高起始 sigma（如 0.93）下，底图进入采样的「唯一反复通道」是干净上下文
    （每步每块把底图切片按 t≈0.999 钉进重叠带），不是一次性的初始混合。所以整幅发糊
    的主导污染源是干净上下文，而非初始 x 里底图占比低（0.93 下底图仅 ~7%）。

  错误尝试（已否决，勿重蹈）：
    方案一「插值档禁用干净上下文」→ 糊/重影虽减，但干净上下文的防接缝/防分身作用
    被一并砍掉 → 接缝、分身（小人）回归。故干净上下文不能一刀切禁用。

  正确解法（已落地）—— 预显影（develop）：
    插值档在放大之后、分块重绘之前，先做一次「整幅、低起始强度、少步数」的采样，
    把模糊 latent 快速打磨成清晰可信的底图（模型补高频），再进干净上下文分块重绘。
    这样：底图可信 → 干净上下文钉进去的是清晰参照 → 不糊不重影；
          干净上下文保留 → 防接缝/防分身依旧生效。
    - 触发条件：免模型档（放大模型名以「⚡」开头，见 _is_free_model）且
      放大参数["pre_steps"] > 0。
    - 自定义参数（参数节点）：预显影步数（0=关闭）、预显影强度（默认 0.4，
      越低越忠实底图，越高重绘越强/越易偏离）。
    - 预显影用与正式采样同一份噪声（同 seed），只是 sigma 曲线不同，逻辑一致。
    - 模型档 is_free_model=False，直接跳过预显影，行为不受影响。
    - 2026-10 变更：免模型档由三线性改为最近邻（零模糊带），同时移除「高频补偿」
      ——最近邻本无模糊，反锐化只会放大块状感。

【轮廓重影：根因 = 底图自带过渡带】—— 2026-10 定论，务必先读
  现象：非整数倍（50→64、64→88 等）时物体轮廓出现跟轮廓走线的重影；
        预显影步数 0 时遍布、1 时只剩零星；倍数越大越严重。
  根因：底图若自带 1~2 格模糊带，后续重采样的「锐化」只能在模糊边旁另画一条
        新边 = 轮廓双线重影。边缘原样保留（过渡带 0 格）则只会更清晰、不分叉。
  用户实测确认（最高优先级，压倒任何理论推断）：
    画面效果最好的是 最近邻精确、区域平均、最近邻 —— 正是过渡带 0 格的那三项。
    球面(1 格)、双线性/三线性/双三次(2 格) 实测都更差，双三次最差，
    故这四种已从下拉移除，只保留上述三项 + 真实模型档。
  已证伪的假说一（勿再沿用）：「非整数倍最近邻产生 1x/2x 条纹 → 16 像素轮廓错位」。
    该假说曾驱动一次改版（按倍数自动切最近邻/三线性），被直接实测推翻：圆盘轮廓
    逐行跳变 3 vs 2 格、竖直/水平硬边位置 0 错位；所谓「27 处错位」只是源索引
    统计量，不产生可见轮廓位移。改用三线性并未解决问题。
  已证伪的判断二（勿再沿用）：在把全部方式列成可选项后，又按「零过冲 + 常数保真」
    把球面(bislerp) 标成「实测最优、建议先试」。用户实跑发现它不如那三个零过渡带的。
    → 「过冲 / 常数偏差」不是决定画面观感的指标，「过渡带宽度」才是。
  选型结论：免模型档的判据 = 过渡带 0 格。「放大模型」下拉现在只保留
    最近邻精确 / 区域平均 / 最近邻 三项 + 真实模型档，代码不再按倍数自动切换
    （自动切换两次都选错）。预显影对这三项应保持 0 —— 本无带可磨，
    多做一步锐化反而多一道风险。
  仍存疑、需继续分离的两个方向（不要凭推理下结论）：
    A) 后续重采样的去噪强度过高 → 模型在原有边缘旁重新画一条边；
    B) 干净上下文边带钉住旧底图（t≈0.999）而核心区已重绘 → 同一轮廓两个版本。
    分离方法：把倍数改成整数倍（×2）跑一次；若重影仍在，则根因在重采样而非插值。

════════════════════════════════════════════════════════════════════════════════
"""
import math
import os
import glob
import gc
import re
from collections import OrderedDict
import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange

import folder_paths
import comfy.conds
import comfy.model_management
import comfy.model_patcher
import comfy.ops
import comfy.sample
import comfy.samplers
import comfy.utils
import comfy.nested_tensor
import latent_preview
from comfy_extras.nodes_custom_sampler import Noise_RandomNoise
from comfy.k_diffusion.sampling import to_d
from comfy.ldm.minimax.model import VISUAL_COND_TIMESTEP as _VISUAL_COND_TIMESTEP

from .banzhang_h3_tile_pos import (
    install as _install_tile_pos,
    set_tile_offset as _set_tile_offset,
    clear_tile_offset as _clear_tile_offset,
)

# 导演台的「放大重绘条件未升级」标记：条件与一采逐字相同时，二采三采纯属
# 白跑，本节点据此直通。口径与导演台/csglide_assets 共用一份。
from .csglide_assets import hd_upgraded as _hd_upgraded

# 内置 LoRA 的融合逻辑直接复用「多 LoRA 管理器」的 model-only 原生 patch 路径
# （model.clone() + add_patches），机制、显存占用与独立加载器完全一致，不重写。
from .banzhang_lora_loader import _apply_lora_to_model

# 自定义参数类型（legacy 直接使用类型名字符串）
H3_UPSCALE_PARAM = "H3_UPSCALE_PARAM"
H3_SPATIAL_PARAM = "H3_SPATIAL_PARAM"
H3_TEMPORAL_PARAM = "H3_TEMPORAL_PARAM"

# 内置 LoRA 下拉的「关闭」项：选中（或强度为 0）即不往二采模型打任何补丁
BUILTIN_LORA_OFF = "🚫 关闭"

VAE_DOWNSAMPLE = 16
ALIGN = 2
# 放大模型目录
_LATENT_UPSCALE_FOLDER = "latent_upscale_models"

# ============================================================
# 免模型插值方式（不加载 AI 模型，纯数学拉伸）
# ------------------------------------------------------------
# 每种方式 = 一个下拉选项。选项名以 "⚡ " 开头，靠这个前缀识别为免模型档
# （_is_free_model），不依赖具体字符串。
# ⚠ 不能以 "(" 开头，否则会被 _run_upscale 当作"缺模型占位符"拦截。
#
# 【选型依据：用户实测结论（2026-10-02），压倒任何理论推断】
#   只保留实测最好的三项，共同点 = 硬边过渡带 0 格（边缘原样保留、不被抹开）：
#     · 最近邻精确  · 区域平均  · 最近邻
#   机理：后续重采样要在原边缘上「锐化」。底图若自带 1~2 格模糊带，锐化时
#   只能在模糊边旁另画一条新边 = 轮廓双线重影；边缘若原样保留，锐化只会让
#   这条边更清晰，不会分叉出第二条。故判据是「过渡带是否为 0」。
#
#   已删（实测不如上面三项，故不再提供）：
#     · 球面 bislerp  过渡带 1 格 —— 曾被误标「实测最优」，实跑证明不如三项
#     · 双线性/三线性 过渡带 2 格 —— 边缘被抹开
#     · 双三次        过渡带 2 格 + 值域过冲 72 格 —— 还会造出分布外值
#     · lanczos       不可用：官方实现内部转 uint8 并按多波段处理，24 通道报错
#   （删前实测数据，28x50 -> 36x64，latent 1 格 = 16 像素：
#      最近邻精确/区域平均/最近邻 过渡带 0 格；球面 1 格；双线性·三线性·双三次 2 格；
#      双三次过冲 72 格；lanczos TypeError。）
#
# 区域平均的已知代价：非整数倍下 adaptive_avg_pool 会把边界格算两遍
# （实测均值偏移 1.55e-2）。实测画面上这个代价可接受，故仍保留。
# ============================================================
FREE_MODEL_LABEL_PREFIX = "⚡ "

NEAREST_MODEL_NAME = "⚡ 最近邻插值（免模型）"
AREA_MODEL_NAME = "⚡ 区域平均插值（免模型）"
NEAREST_EXACT_MODEL_NAME = "⚡ 最近邻精确（免模型）"

# 下拉顺序 = 用户实测给出的次序
FREE_MODEL_LABELS = [
    NEAREST_EXACT_MODEL_NAME,
    AREA_MODEL_NAME,
    NEAREST_MODEL_NAME,
]
# 已从下拉移除、但仍需识别的历史标签：早期版本提供过「⚡ 三线性插值（免模型）」，
# 已保存的工作流里存的仍是这个字符串。保留识别与执行分支，否则老工作流会报错。
LEGACY_TRILINEAR_LABEL = "⚡ 三线性插值（免模型）"

if _LATENT_UPSCALE_FOLDER not in folder_paths.folder_names_and_paths:
    folder_paths.add_model_folder_path(
        _LATENT_UPSCALE_FOLDER,
        os.path.join(folder_paths.models_dir, _LATENT_UPSCALE_FOLDER)
    )


def _is_free_model(model_name):
    """是否免模型插值档（按前缀识别，新方式无需再登记）。"""
    return isinstance(model_name, str) and model_name.startswith(FREE_MODEL_LABEL_PREFIX)



# ============================================================
# 放大模型归一化统计（24 通道）
# ============================================================
LATENTS_MEAN = [
    0.858090341091156, -0.9606591463088989, 1.0661640167236328, -0.5090325474739075,
    -0.2727581858634949, -1.3675414323806763, -0.2553254961967468, -0.26907554268836975,
    -0.5376840829849243, -0.0464097298681736, 0.6657370328903198, 0.19690127670764923,
    -0.5460608005523682, -0.4035342037677765, -0.23683024942874908, 0.25928452610969543,
    -0.30133944749832153, 0.211341992020607, -1.1206848621368408, 0.3581933379173279,
    -0.04225143790245056, 0.2604829967021942, 0.22864092886447906, 0.7056031823158264,
]
LATENTS_STD = [
    1.2223774194717407, 1.2767263650894165, 1.6831774711608887, 1.7549455165863037,
    1.5636216402053833, 2.194143533706665, 0.9653137922286987, 1.0569885969161987,
    0.841948926448822, 0.7729952931404114, 1.8955937623977661, 0.946841835975647,
    0.7996809482574463, 0.44988900423049927, 0.7197399735450745, 0.6936293244361877,
    2.961095094680786, 2.7694199085235596, 3.0496184825897217, 2.1088054180145264,
    3.276226282119751, 3.1627357006073, 2.2816812992095947, 2.6127843856811523,
]


def _make_norm_tensors(device, dtype):
    mean = torch.tensor(LATENTS_MEAN, dtype=dtype, device=device).view(1, -1, 1, 1, 1)
    std = torch.tensor(LATENTS_STD, dtype=dtype, device=device).view(1, -1, 1, 1, 1)
    return mean, std


def _resolve_device(backend):
    if backend == "cpu":
        return torch.device("cpu")
    if backend == "cuda":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    raise ValueError(f"不支持的设备：{backend}")


# ============================================================
# 3D 放大网络组件
# ============================================================
# 放大器的层一律用 comfy.ops 的等价物（而不是 torch.nn 原生层）。
# 原因：ModelPatcherDynamic 只对带 comfy_cast_weights 标记的层做动态换入换出，
# 原生 nn.Conv3d / nn.Linear / nn.GroupNorm 没有这个标记，会被整体常驻在显存里
# —— 包了 Dynamic 也享受不到动态显存。换成 comfy.ops 后权重才真正可换出。
# 没被 Dynamic 接管时（比如 CPU 模式），这些层的 comfy_cast_weights 保持 False，
# forward 走原生实现，行为与 torch.nn 逐位一致。
_DWI = comfy.ops.disable_weight_init


def normalization(channels):
    return _DWI.GroupNorm(32, channels)


def zero_module(module):
    for p in module.parameters():
        p.detach().zero_()
    return module


class AttnBlock3D(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.norm = normalization(in_channels)
        self.q = _DWI.Conv3d(in_channels, in_channels, 1)
        self.k = _DWI.Conv3d(in_channels, in_channels, 1)
        self.v = _DWI.Conv3d(in_channels, in_channels, 1)
        self.proj_out = _DWI.Conv3d(in_channels, in_channels, 1)

    def forward(self, x):
        h = self.norm(x)
        q = rearrange(self.q(h), "b c t h w -> b 1 (t h w) c")
        k = rearrange(self.k(h), "b c t h w -> b 1 (t h w) c")
        v = rearrange(self.v(h), "b c t h w -> b 1 (t h w) c")
        h = F.scaled_dot_product_attention(q, k, v)
        h = rearrange(h, "b 1 (t h w) c -> b c t h w", t=x.shape[2], h=x.shape[3], w=x.shape[4])
        return x + self.proj_out(h)


class ResBlockEmb3D(nn.Module):
    def __init__(self, channels, emb_channels, dropout=0, out_channels=None):
        super().__init__()
        self.out_channels = out_channels or channels
        self.in_layers = nn.Sequential(
            normalization(channels), nn.SiLU(),
            _DWI.Conv3d(channels, self.out_channels, 3, padding=1),
        )
        self.emb_layers = nn.Sequential(
            nn.SiLU(), _DWI.Linear(emb_channels, 2 * self.out_channels),
        )
        self.out_norm = normalization(self.out_channels)
        self.out_layers = nn.Sequential(
            nn.SiLU(), nn.Dropout(p=dropout),
            zero_module(_DWI.Conv3d(self.out_channels, self.out_channels, 3, padding=1)),
        )
        self.skip = (
            _DWI.Conv3d(channels, self.out_channels, 1)
            if self.out_channels != channels else nn.Identity()
        )

    def forward(self, x, emb):
        h = self.in_layers(x)
        emb_out = self.emb_layers(emb).type(h.dtype)
        while len(emb_out.shape) < len(h.shape):
            emb_out = emb_out[..., None]
        scale, shift = torch.chunk(emb_out, 2, dim=1)
        h = self.out_norm(h) * (1 + scale) + shift
        h = self.out_layers(h)
        return self.skip(x) + h


class TemporalConv(nn.Module):
    def __init__(self, channels, kernel_size=5):
        super().__init__()
        padding = kernel_size // 2
        self.norm = normalization(channels)
        self.dwconv = _DWI.Conv3d(channels, channels,
                                kernel_size=(kernel_size, 1, 1),
                                padding=(padding, 0, 0),
                                groups=channels)
        self.pwconv = _DWI.Conv3d(channels, channels, kernel_size=1)
        nn.init.zeros_(self.pwconv.weight)
        nn.init.zeros_(self.pwconv.bias)

    def forward(self, x):
        identity = x
        h = self.norm(x)
        h = F.silu(h)
        h = self.dwconv(h)
        h = self.pwconv(h)
        return identity + h


class LatentResizer3D(nn.Module):
    def __init__(self, in_channels=24, in_blocks=12, out_blocks=12,
                 channels=512, dropout=0.1, attn=False,
                 temporal_every=2, temporal_kernel=5):
        super().__init__()
        self.conv_in = _DWI.Conv3d(in_channels, channels, 3, padding=1)
        embed_dim = 64
        self.embed = nn.Sequential(
            _DWI.Linear(1, embed_dim), nn.SiLU(), _DWI.Linear(embed_dim, embed_dim))

        self.in_blocks = nn.ModuleList()
        for b in range(in_blocks):
            if (b == 1 or b == in_blocks - 1) and attn:
                self.in_blocks.append(AttnBlock3D(channels))
            self.in_blocks.append(ResBlockEmb3D(channels, embed_dim, dropout))
            if temporal_every > 0 and b % temporal_every == 0:
                self.in_blocks.append(TemporalConv(channels, temporal_kernel))

        self.out_blocks = nn.ModuleList()
        for b in range(out_blocks):
            if (b == 1 or b == out_blocks - 1) and attn:
                self.out_blocks.append(AttnBlock3D(channels))
            self.out_blocks.append(ResBlockEmb3D(channels, embed_dim, dropout))
            if temporal_every > 0 and b % temporal_every == 0:
                self.out_blocks.append(TemporalConv(channels, temporal_kernel))

        self.norm_out = normalization(channels)
        self.conv_out = _DWI.Conv3d(channels, in_channels, 3, padding=1)

    def forward(self, x, scale=None, target_size=None, enable_chunking=True, chunk=None):
        if target_size is not None:
            size = target_size
        elif scale is not None:
            size = tuple(int(round(s * scale)) for s in x.shape[-3:])
        else:
            return x

        if size == x.shape[-3:]:
            return x

        B, C, T, H, W = x.shape

        # 时间维切块：3D 卷积网络处理长 T 时整段前向会爆显存，按 32 段切块 + 重叠
        # 融合（确定性，无重影——这跟采样阶段的时间分块是两码事）。
        tk = 0
        for b in self.in_blocks:
            if isinstance(b, TemporalConv):
                tk = b.dwconv.weight.shape[2]
                break
        overlap = tk
        # 块长原本写死 32。3D 卷积的激活随块长线性增长，8GB 卡在 32 上会在 Conv3D
        # 处 OOM（上游 issue/PR #46 实测：降到 12 就能跑完，峰值约 3.9GB）。
        # 现在块长由外部给定（自动按显存，或用户手选）。
        chunk = int(chunk) if chunk else 32
        chunk = max(overlap * 2 + 1, min(chunk, T))

        if not enable_chunking or T <= chunk:
            return self._forward_seg(x, scale, size)

        print(f"[H3放大] 时间分块：T={T} 块长={chunk} 块数={(T + chunk - 1) // chunk} 重叠={overlap}")

        x_padded = F.pad(x, (0, 0, 0, 0, overlap, overlap), mode='replicate')

        out_full = torch.zeros(B, C, T, size[-2], size[-1], device=x.device, dtype=x.dtype)
        weight_full = torch.zeros(1, 1, T, 1, 1, device=x.device, dtype=x.dtype)

        start = 0
        while start < T:
            seg_start = start
            seg_end = min(T, start + chunk)
            out_start = max(0, seg_start - overlap)
            out_end = min(T, seg_end + overlap)
            lo = max(0, out_start - overlap)
            hi = min(T + 2 * overlap, out_end + overlap)

            seg = x_padded[:, :, lo:hi].contiguous()
            seg_size = (hi - lo, size[-2], size[-1])
            seg_out = self._forward_seg(seg, scale, seg_size)

            s0 = (out_start + overlap) - lo
            s1 = s0 + (out_end - out_start)
            valid_out = seg_out[:, :, s0:s1]
            n_valid = out_end - out_start

            weight = torch.ones(n_valid, device=x.device, dtype=x.dtype)
            if seg_start > out_start:
                blend_len = seg_start - out_start
                weight[:blend_len] = torch.arange(1, blend_len + 1, device=x.device, dtype=x.dtype) / (blend_len + 1)
            if out_end > seg_end:
                blend_len = out_end - seg_end
                weight[-blend_len:] = torch.arange(blend_len, 0, -1, device=x.device, dtype=x.dtype) / (blend_len + 1)

            out_full[:, :, out_start:out_end] += valid_out * weight.view(1, 1, n_valid, 1, 1)
            weight_full[:, :, out_start:out_end] += weight.view(1, 1, n_valid, 1, 1)

            start += chunk
            del seg, seg_out, valid_out
            if start % (chunk * 4) == 0:
                gc.collect()

        out_full = out_full / weight_full.clamp(min=1e-8)
        return out_full

    def _forward_seg(self, x, scale, size):
        scale_emb = torch.tensor(
            [scale - 1 if scale is not None else 0.0],
            dtype=x.dtype, device=x.device).unsqueeze(0)
        emb = self.embed(scale_emb)

        x = self.conv_in(x)
        for b in self.in_blocks:
            if isinstance(b, ResBlockEmb3D):
                emb_t = emb.expand(x.shape[0], -1)
                x = b(x, emb_t)
            else:
                x = b(x)

        x = F.interpolate(x, size=size, mode="trilinear", align_corners=False)

        for b in self.out_blocks:
            if isinstance(b, ResBlockEmb3D):
                emb_t = emb.expand(x.shape[0], -1)
                x = b(x, emb_t)
            else:
                x = b(x)

        x = self.norm_out(x)
        x = F.silu(x)
        x = self.conv_out(x)
        return x


# ============================================================
# 放大模型加载
# ============================================================
MODEL_CACHE = OrderedDict()

# 放大模型常驻上限。缓存无上限时，换过的模型会一直占着内存/显存到进程结束，
# 表现就是「跑的队列越多越卡，重启才好」。上限 2 覆盖「来回切两个模型」的常态。
MODEL_CACHE_LIMIT = 2


def _release_patcher(patcher):
    """把放大器从 ComfyUI 的显存账本里摘掉，并把权重搬回 CPU。

    不摘的话 ComfyUI 会一直以为它还占着那份显存 —— 账本对不上，该腾的时候不腾，
    最后变成「放大器早就不用了，扩散模型却加载不进来」。
    """
    comfy.model_management.unload_model_and_clones(
        patcher, unload_additional_models=False)
    if not patcher.is_dynamic():
        # 动态显存模式下权重由 vbar 托管，参数可能已被替换成 vbar 张量，不能自己搬；
        # 只有普通模式才需要手动搬回 CPU。
        patcher.model.to("cpu")


def _cache_put(key, patcher):
    """放入缓存并淘汰最久未用的，超上限的模型被丢弃后由 gc 回收。"""
    MODEL_CACHE[key] = patcher
    MODEL_CACHE.move_to_end(key)
    while len(MODEL_CACHE) > MODEL_CACHE_LIMIT:
        evicted_key, evicted = MODEL_CACHE.popitem(last=False)
        _release_patcher(evicted)
        print(f"[H3放大] 缓存超限，已释放放大模型: {evicted_key}")
    gc.collect()


def clear_model_cache():
    """彻底清空放大模型缓存（供卸载/清理类节点调用）。"""
    if MODEL_CACHE:
        print(f"[H3放大] 清空放大模型缓存（{len(MODEL_CACHE)} 个）")
        for patcher in MODEL_CACHE.values():
            _release_patcher(patcher)
    MODEL_CACHE.clear()
    gc.collect()


def scan_models():
    """下拉列表 = 全部免模型插值方式 + latent_upscale_models 目录里的真实模型。"""
    names = [
        name for name in folder_paths.get_filename_list(_LATENT_UPSCALE_FOLDER)
        if os.path.splitext(name)[1].lower() in (".pth", ".safetensors")
    ]
    if names:
        return list(FREE_MODEL_LABELS) + names
    return list(FREE_MODEL_LABELS) + [
        f"(请将模型放入: {folder_paths.get_folder_paths(_LATENT_UPSCALE_FOLDER)[0]})"
    ]


# ============================================================
# 免模型插值执行
# ------------------------------------------------------------
# 【两次判断错误留下的教训，勿重蹈】
#   错误一（条纹论）：曾按「非整数倍最近邻的 1x/2x 条纹造成 16 像素轮廓错位」把
#     非整数倍改走三线性。该结论被直接实测推翻——圆盘轮廓逐行跳变 3 vs 2 格、
#     竖直/水平硬边位置 0 错位，所谓「27 处错位」只是源索引统计量，不产生可见位移。
#   错误二（本轮）：在已把全部方式列成可选项后，又按「零过冲 + 常数保真」把
#     bislerp 排到首位、标注「实测最优」。用户实际跑完发现最好的是
#     最近邻精确 / 区域平均 / 最近邻（即过渡带 0 格的那三项），bislerp 反而不如它们。
#     → 我用来排序的指标（过冲、常数偏差）根本不是决定画面观感的那个。
#
# 【真正的判据】过渡带宽度，而非过冲或拉伸均匀性
#   后续重采样要在原边缘上「锐化」。底图若自带 1~2 格模糊带，锐化时只能在
#   模糊边旁另画一条新边 = 轮廓双线重影；边缘若原样保留，锐化只会让这条边
#   更清晰，不会分叉出第二条。故「过渡带 0 格」才是好底图的标准。
#   这也解释了为何带插值的方式（双线性/三线性/双三次）实测都更差。
#
# 各选项一律走各自纯粹的算子，不再按倍数自动切换——自动切换两次都选错了。
# ============================================================
def _free_model_interpolate(s, t, h_out, w_out, model_name):
    """按标签执行对应的免模型插值。s 为 (B, C, T, H, W)。"""
    size = (t, h_out, w_out)
    if model_name == NEAREST_MODEL_NAME:
        return F.interpolate(s, size=size, mode="nearest")
    if model_name == NEAREST_EXACT_MODEL_NAME:
        return F.interpolate(s, size=size, mode="nearest-exact")
    if model_name == AREA_MODEL_NAME:
        return F.adaptive_avg_pool3d(s, (t, h_out, w_out))
    if model_name == LEGACY_TRILINEAR_LABEL:
        # 仅老工作流的旧标签保留历史的自动切换行为（整数倍最近邻 / 非整数倍三线性），
        # 以免改变既有工作流的结果基线。该标签已从下拉移除，此处只做兼容识别。
        if (h_out % s.shape[3] == 0) and (w_out % s.shape[4] == 0):
            return F.interpolate(s, size=size, mode="nearest")
        return F.interpolate(s, size=size, mode="trilinear", align_corners=False)
    # 老工作流里可能残留的其它历史标签（双线性/双三次/球面等曾短暂提供过），
    # 或新增方式时漏了登记：统一退回最近邻，不崩掉。
    return F.interpolate(s, size=size, mode="nearest")


def _load_raw_sd(path):
    if path.endswith('.safetensors'):
        try:
            from safetensors import safe_open
            with safe_open(path, framework="pt", device="cpu") as f:
                sd = {k: f.get_tensor(k) for k in f.keys()}
        except ImportError:
            from safetensors.torch import load_file
            sd = load_file(path, device='cpu')
    else:
        sd = torch.load(path, map_location='cpu', weights_only=False)
    if isinstance(sd, dict) and 'model' in sd:
        sd = sd['model']
    sd = {k: v.to(torch.float16) if v.dtype == torch.float8_e4m3fn else v
          for k, v in sd.items()}
    return sd


def _extract_upscaler_sd(sd):
    if any(k.startswith("upscaler.") for k in sd):
        return {k[len("upscaler."):]: v for k, v in sd.items() if k.startswith("upscaler.")}
    return sd


def _detect_arch(sd):
    cfg = {
        "in_channels": 24, "in_blocks": 12, "out_blocks": 12, "channels": 512,
        "dropout": 0.1, "attn": False, "temporal_every": 2, "temporal_kernel": 5,
    }
    if 'conv_in.weight' in sd:
        cfg["in_channels"] = sd['conv_in.weight'].shape[1]
        cfg["channels"] = sd['conv_in.weight'].shape[0]
    in_ids, out_ids = set(), set()
    temporal_seen = False
    for k in sd.keys():
        m = re.match(r'in_blocks\.(\d+)\.in_layers\.', k)
        if m:
            in_ids.add(int(m.group(1)))
        m = re.match(r'out_blocks\.(\d+)\.in_layers\.', k)
        if m:
            out_ids.add(int(m.group(1)))
        if 'dwconv.weight' in k:
            temporal_seen = True
    if in_ids:
        cfg["in_blocks"] = len(in_ids)
    if out_ids:
        cfg["out_blocks"] = len(out_ids)
    if temporal_seen:
        cfg["temporal_every"] = 2
        for k in sd.keys():
            if k.endswith('dwconv.weight'):
                cfg["temporal_kernel"] = sd[k].shape[2]
                break
    else:
        cfg["temporal_every"] = 0
    cfg["attn"] = False
    return cfg


def _sd_native_dtype(sd):
    """取权重文件的原生精度（fp8 已在 _load_raw_sd 里转成 fp16）。"""
    t = sd.get("conv_in.weight")
    if t is None:
        t = next(iter(sd.values()), None)
    if t is None or not t.is_floating_point():
        return torch.float32
    if t.dtype in (torch.float8_e4m3fn, torch.float8_e5m2):
        return torch.float16
    return t.dtype


def _resolve_compute_dtype(native, device):
    """放大器的计算精度：跟随 ComfyUI 的全局 dtype 策略，只降不升。

    权重是 fp32、而全局策略是 fp16/bf16（多数卡的默认）时，按原生精度跑会让放大器
    独占一份 fp32 权重 + fp32 激活：显存翻倍、速度减半，还跟扩散模型的 dtype 策略
    打架。这里取「原生」与「全局」中更低的那个 —— 省显存省时间，且绝不升精度。
    CPU 一律 fp32（fp16 在 CPU 上既慢、算子又不全）—— 把 device 传进去，
    ComfyUI 自己就会对 CPU / MPS 给出 fp32，无需在这里另判。
    """
    mm = comfy.model_management
    if mm.should_use_bf16(device):
        policy = torch.bfloat16
    elif mm.should_use_fp16(device):
        policy = torch.float16
    else:
        policy = torch.float32
    _RANK = {torch.float16: 0, torch.bfloat16: 1, torch.float32: 2}
    return native if _RANK.get(native, 2) <= _RANK[policy] else policy


def _is_mixed_dtype(up_sd):
    """checkpoint 是否混了多种浮点精度。

    混合精度的放大器（典型：主体 BF16 + 上百个 FP16 fallback 张量）**不能**统一
    cast —— bf16 只有 8 位尾数、fp16 有 10 位，把 fp16 张量 cast 成 bf16 等于把它的
    值舍掉一截（上游 PR #53 修的就是这个：统一 cast 会 round away 那 150 个 FP16
    张量）。遇到这种 ckpt 必须保持每个张量自己的 dtype，否则放大器会带上精度损伤。
    """
    seen = {v.dtype for v in up_sd.values() if v.is_floating_point()}
    return len(seen) > 1


def load_model(name, device):
    """加载放大模型，返回一个包住它的 ComfyUI ModelPatcher。

    精度不再由用户选：按 ComfyUI 全局策略对齐（见 _resolve_compute_dtype）。
    包 ModelPatcher 是为了让放大器进 ComfyUI 的显存账本 —— 内核看得见它，才会
    在显存紧张时把它和别的模型一起排队驱逐，而不是像裸 nn.Module 那样谁也看不见、
    只能靠手工 .to("cpu") 碰运气。取用通过 load_models_gpu，卸载通过 _release_patcher。
    """
    # 先查缓存：命中就直接复用，不再白白读一遍 1GB 级的权重文件
    cache_key = f"{name}::{device.type}"
    hit = MODEL_CACHE.get(cache_key)
    if hit is not None:
        MODEL_CACHE.move_to_end(cache_key)
        return hit

    try:
        path = folder_paths.get_full_path_or_raise(_LATENT_UPSCALE_FOLDER, name)
    except Exception as e:
        raise FileNotFoundError(f"模型文件未找到：{name}") from e
    raw_sd = _load_raw_sd(path)
    up_sd = _extract_upscaler_sd(raw_sd)
    native = _sd_native_dtype(up_sd)
    # 混合精度 ckpt 一个字节都不能统一 cast（见 _is_mixed_dtype），保持原生；
    # 单一精度的才按 ComfyUI 全局策略降精度省显存。
    mixed = _is_mixed_dtype(up_sd)
    dtype = native if mixed else _resolve_compute_dtype(native, device)
    cfg = _detect_arch(up_sd)
    # 实例化前先腾一次：权重 1GB 级，跟扩散模型抢地方时容易在 .to(device) 这步就炸。
    # 之后交给 ModelPatcher / load_models_gpu 按账本管理。
    # （注意别用 free_memory(0, dev)：实测它什么都不卸 —— memory_to_free = 0 - 空闲 < 0，
    #   判据不成立，只做 soft_empty_cache。必须传真实需求量才会真的腾。）
    if device.type == "cuda":
        comfy.model_management.free_memory(
            int(sum(v.numel() * dtype.itemsize for v in up_sd.values()) * 2), device)
    model = LatentResizer3D(
        in_channels=cfg["in_channels"], in_blocks=cfg["in_blocks"], out_blocks=cfg["out_blocks"],
        channels=cfg["channels"], dropout=cfg["dropout"], attn=cfg["attn"],
        temporal_every=cfg["temporal_every"], temporal_kernel=cfg["temporal_kernel"],
    )
    model.load_state_dict(up_sd, strict=True)
    # 权重已进模型，原始 state_dict（可达 1GB+）不再常驻
    del raw_sd, up_sd
    gc.collect()
    model = model.to(device, dtype).eval().requires_grad_(False)
    # 包进 ComfyUI 的账本：size 给准确值，ComfyUI 才知道它占多大、该为它腾多少。
    #
    # 用 CoreModelPatcher 而不是直接写死 ModelPatcherDynamic：
    #   · 本次启动启用了 DynamicVRAM（aimdo 初始化成功）→ 它就是动态版，放大器
    #     权重按层换入换出，这是小显存扛住大画布的关键；
    #   · 没启用（--disable-dynamic-vram / 不支持的 GPU / 运行期被关）→ 它就是普通版，
    #     自动降级成整体常驻，**不会崩**。
    # 必须走属性访问（comfy.model_patcher.CoreModelPatcher）而不是 from-import：
    # CoreModelPatcher 这个模块级名字会在启动后被重新绑定，from-import 会拿到旧值。
    #
    # 前提：放大器的层已全部换成 comfy.ops 的等价物（见 _DWI），否则动态版也管不了
    # 它们 —— 原生 nn.Conv3d 没有 comfy_cast_weights 标记，会被整体常驻在显存里。
    #
    # load_device 必须**带编号**（cuda:0，而不是 cuda）：动态显存路径用
    # load_device.index 去 aimdo 取设备上下文，而 torch.device("cuda") 的 index 是 None，
    # 一进来就 int(None) 崩溃（放大器根本加载不起来）。用 ComfyUI 自己的取法补上编号。
    load_dev = device
    if load_dev.type == "cuda" and load_dev.index is None:
        load_dev = comfy.model_management.get_torch_device()
    patcher = comfy.model_patcher.CoreModelPatcher(
        model, load_device=load_dev, offload_device=torch.device("cpu"),
        size=sum(p.numel() * p.element_size() for p in model.parameters()))
    _cache_put(cache_key, patcher)
    print(f"[H3放大] 已加载放大模型：{name} | 计算精度：{dtype}"
          f"（权重原生 {native}"
          f"{'｜混合精度 ckpt，保持原生不统一 cast' if mixed else '，已按 ComfyUI 全局精度策略对齐'}）| "
          f"参数量：{sum(p.numel() for p in model.parameters()):,} | "
          f"显存托管：{'动态（按层换入换出）' if patcher.is_dynamic() else '普通（整体常驻）'}")
    return patcher


def _run_upscale(video, param):
    """内置执行 3D 放大（只放大视频，返回放大后的 video tensor）。"""
    model_name = param["model_name"]
    if model_name.startswith('('):
        raise ValueError("请先将模型文件放入 latent_upscale_models 目录")
    mode = param["mode_code"]
    device = param.get("device", "cuda")
    force_unload = param.get("force_unload", True)

    orig_dtype = video.dtype
    orig_device = video.device
    dev = _resolve_device(device)

    s = video.to(device=dev, copy=True)
    b, c, t, h_in, w_in = s.shape
    downsample = VAE_DOWNSAMPLE

    if mode == "scale":
        scale_val = param["scale"]
        w_pixel_target = w_in * downsample * scale_val
        h_pixel_target = h_in * downsample * scale_val
        effective_scale = scale_val
    elif mode == "target":
        w_pixel_target = float(param["width"])
        h_pixel_target = float(param["height"])
        effective_scale = (w_pixel_target / (w_in * downsample) + h_pixel_target / (h_in * downsample)) / 2.0
    elif mode == "megapixels":
        target_pixels = param["megapixels"] * 1024 * 1024
        aspect_ratio = w_in / h_in
        h_pixel_target = (target_pixels / aspect_ratio) ** 0.5
        w_pixel_target = h_pixel_target * aspect_ratio
        effective_scale = (w_pixel_target / (w_in * downsample) + h_pixel_target / (h_in * downsample)) / 2.0
    else:
        raise ValueError(f"不支持的放大模式：{mode}")

    # 宽高严格等比：先定唯一 scale 算高，再用「实际高度比例」回推宽度。
    # 原实现对 w/h 各自做 round(像素/32)*32，非整数倍时横竖缩放比例必然不同
    # （实测 50x28：@1.1x 差 4.34%、@1.25x 差 3.56%、@1.75x 差 2.60%）
    # → 画面被非均匀拉伸，垂直边与水平边位移量不同。
    # 现改为只对齐到 2（H3 patch_size=(1,2,2)，model.py:477；不对齐时模型
    # pad_to_patch_size 会自动补零，故无需 16/32 对齐），且宽度由实际高度比例
    # 反推而非独立取整 → 两方向共用同一比例。
    # 实测误差：@1.1x 4.34%→0.79%、@1.25x 3.56%→1.18%、@1.75x 2.60%→0.33%，
    # 整数倍仍为 0%。残余误差仅来自末位对齐到 2，在 latent 尺度下不可见。
    scale_out = float(effective_scale)
    h_out = max(2, int(round(h_in * scale_out)) // 2 * 2)
    w_out = max(2, int(round(w_in * (h_out / h_in))) // 2 * 2)

    if effective_scale < 1.0 and (w_out < w_in or h_out < h_in):
        raise ValueError("该模型仅支持放大（有效倍数 >= 1.0）。")
    if w_out == w_in and h_out == h_in:
        return video

    print(f"[H3放大] 潜空间 {w_in}x{h_in} -> {w_out}x{h_out} | "
          f"像素 {w_out * downsample}x{h_out * downsample} | 倍数={scale_out:.3f} | "
          f"实际宽×{w_out / w_in:.4f} 高×{h_out / h_in:.4f}")

# 免模型插值档：具体用哪种方式由用户在下拉里选，逐项实测对比。
    # 早期版本曾按「整数倍→最近邻 / 非整数倍→三线性」自动切换，其依据
    # 「非整数倍最近邻的 1x/2x 条纹造成 16 像素轮廓错位」已被实测证伪
    # （圆盘轮廓逐行跳变 3 vs 2 格、硬边位置 0 错位），故改为全量可选。
    # 详见 _free_model_interpolate 上方的说明。
    if _is_free_model(model_name):
        int_multiple = (h_out % h_in == 0) and (w_out % w_in == 0)
        with torch.inference_mode():
            out = _free_model_interpolate(s, t, h_out, w_out, model_name)
        out = out.to(device=orig_device, dtype=orig_dtype)
        # 区域平均在非整数倍下会把边界格算两遍（均值偏移，实测 1.55e-2），如实提示
        warn = ""
        if model_name == AREA_MODEL_NAME and not int_multiple:
            warn = (" ⚠ 区域平均在非整数倍下会把边界格重复计入，"
                    "均值可能轻微偏移；整数倍时该方式等价纯复制、无此问题。")
        print(f"[H3放大] ✅ 免模型插值完成：{model_name} | "
              f"{'整数倍' if int_multiple else f'非整数倍 ×{w_out / w_in:.3f}'}"
              f"{warn}")
        return out

    if dev.type == "cuda":
        comfy.model_management.soft_empty_cache()
    # 读 1GB 级权重 + 实例化可能要好几秒，取消时不必再等下去
    comfy.model_management.throw_exception_if_processing_interrupted()
    patcher = load_model(model_name, dev)
    # 经 ComfyUI 账本加载：显存放得下就原地放（不惊动别的模型），放不下时由
    # ComfyUI 按真实体积挑该卸的卸 —— 比手工 free_memory 估得准。
    # force_full_load 必须开：放大器不是 diffusion model，一旦被切成部分加载，
    # 前向会逐层搬权重，慢到不可接受（就是「运行极慢」那个坑的真正来源）。
    comfy.model_management.load_models_gpu([patcher], force_full_load=True)
    model = patcher.model
    # 按已解析的计算精度跑（见 _resolve_compute_dtype）
    compute_dtype = next(model.parameters()).dtype
    if dev.type == "cuda":
        # 峰值 ≈ 输入 + 输出 + 权重：放大器内部虽按「放大时间分块」切了 3D 卷积，
        # 但输入/输出两张整条 latent 是常驻的，长片大倍数下这一步仍可能是全链路最高点。
        # 估一次并在不够时把可操作的退路直接打给用户，而不是让他等一场 OOM。
        _est = int(s.numel() * compute_dtype.itemsize
                   + b * c * t * h_out * w_out * compute_dtype.itemsize
                   + sum(p.numel() * p.element_size() for p in model.parameters()))
        _free = comfy.model_management.get_free_memory(dev)
        print(f"[H3放大] 显存预估：需要约 {_fmt_mem(_est)} / 当前空闲 {_fmt_mem(_free)}")
        if _est > _free:
            print("[H3放大] ⚠ 显存可能不够，爆显存会直接中断、不够时被 ComfyUI 降级成"
                  "逐层搬权重（耗时成倍上涨）。可依次尝试：\n"
                  "  ① 降低「放大倍数」（显存随像素数平方增长，2.0→1.5 省约 44%）\n"
                  "  ② 改用「⚡」免模型插值档（不加载放大模型，几乎不吃显存）\n"
                  "  ③ 把「放大时间分块」手动调小（默认按显存自动，最小 4）——\n"
                  "     它切的是放大器内部 3D 卷积，块越小峰值越低\n"
                  "  ④ 启动参数去掉 --disable-smart-memory / --highvram，"
                  "让 ComfyUI 能部分换出权重")
    # 到这一步才确定计算精度：直接按目标精度重取一份，避免「原精度副本 + 计算精度
    # 副本」两份整条 latent 同时占着显存（长片大画布下这一份可达数百 MB）。
    s = video.to(device=dev, dtype=compute_dtype, copy=True)
    norm_mean, norm_std = _make_norm_tensors(dev, compute_dtype)
    # 放大器的时间分块长度：0 = 按空闲显存自动挑（见 _auto_upscale_chunk）。
    # 这是放大器内部 3D 卷积的切块，与主节点的「时间分块参数」（重采样分段）无关。
    _tk = 5
    for _b in model.in_blocks:
        if isinstance(_b, TemporalConv):
            _tk = int(_b.dwconv.weight.shape[2])
            break
    _chunk = int(param.get("temporal_chunk", 0) or 0)
    if _chunk <= 0:
        _chunk = _auto_upscale_chunk(
            comfy.model_management.get_free_memory(dev), _tk, c, h_out, w_out,
            compute_dtype.itemsize,
            sum(p.numel() * p.element_size() for p in model.parameters()))
        print(f"[H3放大] 时间分块：自动档按空闲显存取块长 {_chunk}（上限 {_UP_CHUNK_DEFAULT}）")
        if _chunk <= _UP_CHUNK_MIN:
            print(f"[H3放大] ⚠ 当前显存只够块长 {_UP_CHUNK_MIN} —— 这已经是最碎的一档：\n"
                  "        再省就只能降低「放大倍数」或改用「⚡」免模型插值档。")
    with torch.inference_mode():
        s_norm = (s - norm_mean) / norm_std
        del s
        out = model(s_norm, scale=effective_scale, target_size=(t, h_out, w_out),
                    chunk=_chunk)
        del s_norm
        out = out * norm_std + norm_mean
    out = out.to(device=orig_device, dtype=orig_dtype)

    if force_unload:
        # 用完即焚：从 ComfyUI 账本里摘掉 + 权重搬回 CPU，不留在显存里跟扩散模型抢地方。
        _release_patcher(patcher)
        print("[H3放大] ✅ 放大模型已移出显存（权重搬回 CPU，留在缓存里供复用）。")
    # 不卸载时**故意留在账本里**：后面扩散模型要显存时，ComfyUI 会自己把它换出，
    # 这正是包 ModelPatcher 换来的好处 —— 换之前它只是个谁也看不见的裸模块。
    if dev.type == "cuda":
        comfy.model_management.soft_empty_cache()
        gc.collect()

    return out


def is_h3_av_latent(samples):
    return (samples is not None and samples.is_nested and len(samples.tensors) == 2
            and samples.tensors[0].ndim == 5 and samples.tensors[0].shape[1] == 24
            and samples.tensors[1].ndim == 4 and samples.tensors[1].shape[1] == 32)


def px_to_lat(px):
    return max(ALIGN, (round(px / VAE_DOWNSAMPLE) // ALIGN) * ALIGN)


def snap_align(v):
    return max(0, int(round(v / ALIGN)) * ALIGN)


# ============================================================
# 采样
# ============================================================
def build_guider(model, cond):
    guider = comfy.samplers.CFGGuider(model)
    guider.inner_set_conds({"positive": cond})
    return guider


def _build_sigmas(model, scheduler, steps, denoise):
    total_steps = int(steps)
    if denoise is not None and denoise < 1.0:
        total_steps = int(steps / denoise)
    sigmas = comfy.samplers.calculate_sigmas(
        model.get_model_object("model_sampling"), scheduler, total_steps).cpu()
    return sigmas[-(int(steps) + 1):]


def sample_piece(piece, cond, model, noise_tensor, seed, sampler, sigmas, callback=None,
                 denoise_mask=None):
    """采样一块。denoise_mask 语义 1=生成 0=保留（干净上下文）：
    分块时把 overlap 边带标成 0，让模型在边带读到放大后的原始像素（t≈0.999），
    块外真实内容从第一步可见 → 空背景块不凭空补主体（防小人）、边界两侧看到
    同一份内容逐步收敛（防接缝）。"""
    latent = dict(piece)
    latent_image = latent["samples"]
    latent_image = comfy.sample.fix_empty_latent_channels(
        model, latent_image,
        latent.get("downscale_ratio_spacial", None),
        latent.get("downscale_ratio_temporal", None),
    )
    latent["samples"] = latent_image
    noise_mask = latent.get("noise_mask")
    if denoise_mask is not None:
        noise_mask = denoise_mask
    guider = build_guider(model, cond)
    if callback is None:
        x0_output = {}
        callback = latent_preview.prepare_callback(guider.model_patcher, sigmas.shape[-1] - 1, x0_output)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    samples = guider.sample(
        noise_tensor, latent_image, sampler, sigmas,
        denoise_mask=noise_mask, callback=callback,
        disable_pbar=disable_pbar, seed=seed,
    )
    return samples.to(comfy.model_management.intermediate_device())


# ============================================================
# 参数整合节点
# ============================================================
# 显存预设 → 空间瓦片尺寸（tile 像素宽）
# tile 越小 = 切块越多 = 峰值显存越低，但接缝风险与总耗时上升。
# 4/6 GB 档按等比外推补上（4G 沿用放大器侧 4G 档的 8 条同量级思路），
# 供小显存卡使用；实际峰值仍由 _fit_n_tiles_to_free_vram 按空闲显存兜底。
# 每档可单独指定重叠带（格）。不指定时退回 _OVERLAP_MIN_CELLS。
# 8GB 档刻意把 tile 调小、重叠压到 6 格：该档原本 tile=352（22 格）在 72 格画布上
# 只切 3 块，而每块实际宽度 = stride(24) + 2×重叠(8) = 40 格 —— 重叠带吃掉 40%，
# 单块 token 反而比一采整幅还多。压到 20 格 tile / 6 格重叠后：切 4 块、每块 30 格、
# token 降 25%。6 格（96 像素）仍高于会出亮条的 4 格下限，接缝风险可控。
_VRAM_PRESETS = {
    "4 GB":  {"tile": 224, "overlap": 8},
    "6 GB":  {"tile": 288, "overlap": 8},
    "8 GB":  {"tile": 320, "overlap": 6},
    "12 GB": {"tile": 448, "overlap": 8},
    "16 GB": {"tile": 512, "overlap": 8},
    "24 GB": {"tile": 640, "overlap": 10},
}
# 空间分块固定推荐值（对应 LBH 官方默认，普通用户无需调整）
_OVERLAP_RATIO = 0.25
# 重叠带绝对下限（格）：小瓦片（4G/6G）按 0.25 比例算出的重叠带只有 4 格，接缝两侧
# 互相"看不到"对方 → 对不齐 → 亮条、随画面闪。给重叠带设绝对下限，保证任何瓦片都有
# 足够边界上下文（8 格 = 12G/16G 档的水平）。只加宽边带，不改块数与核心区宽度。
_OVERLAP_MIN_CELLS = 8
_MIN_TILE = 256

# ── 放大阶段的时间分块（放大器内部 3D 卷积的切块，与重采样阶段的「每块帧数」是两码事）
# 块长原本写死 32：3D 卷积的激活随块长线性增长，8GB 卡在 32 上会在 Conv3D 处 OOM
# （上游 PR #46 实测：降到 12 就能跑完，峰值约 3.9GB；块越小越省显存、越慢）。
# 下限 12：块长必须大于 2×overlap（temporal kernel 通常 5，即 10）才有核心区可言，
# 再小只会被 forward 夹回 11，等于没调。12 也正是上游实测救活 8GB 卡的那个值。
_UP_CHUNK_CHOICES = ["自动（按显存）", "32", "24", "16", "12"]
_UP_CHUNK_DEFAULT = 32
_UP_CHUNK_MIN = 12
# 标定依据：上游那组实测（4060 8GB / bf16 / 块长 12 / 输出 latent 44×76，峰值 3.9GB）。
# 扣掉权重后，「块长 + 2×overlap」这段在 3D 网络里的激活 ≈ 单步输出体积的 900 倍
# （22 步 × 0.153MB × 900 ≈ 3.0GB，加权重约 3.9GB，与实测吻合）。
_UP_ACT_PER_STEP = 900
_UP_BUDGET_RATIO = 0.8


def _auto_upscale_chunk(free_bytes, overlap, c, h_out, w_out, itemsize, weight_bytes=0):
    """按空闲显存挑放大器的时间分块长度；拿不到显存信号就用默认块长（不猜小）。

    weight_bytes 必须先从预算里扣掉：放大器权重是常驻的，不扣就会把余量算厚
    （上游实测 8GB 卡块长 32 OOM、12 可跑，差值正好在这个量级）。
    """
    if not free_bytes or free_bytes <= 0:
        return _UP_CHUNK_DEFAULT
    per_step = max(1, c * h_out * w_out * itemsize)
    act_per_step = per_step * _UP_ACT_PER_STEP
    usable = free_bytes - int(weight_bytes)
    if usable <= 0:
        return _UP_CHUNK_MIN
    chunk = int((usable * _UP_BUDGET_RATIO) // act_per_step) - 2 * int(overlap)
    return max(_UP_CHUNK_MIN, min(_UP_CHUNK_DEFAULT, chunk))
# 显存兜底的估算常数（见 _fit_n_tiles_to_free_vram）：
#   _ATT_BYTES_PER_TOKEN：单个注意力 token 的隐藏态字节数（H3 hidden × fp16 的量级）
#   _ATT_ACT_FACTOR：同时存活的激活倍数，与放大器侧 act_factor=12 同量级，保守取
_ATT_BYTES_PER_TOKEN = 4096
_ATT_ACT_FACTOR = 12


class BanZhang_MMH3_UpscaleParams:
    """把「放大 / 空间分块」两组参数打包进一个节点。
    界面已简化：放大给核心参数，空间分块用「显存预设」一键搞定。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # ---- 放大 ----
                "放大模型": (scan_models(), {"tooltip":
                                                   "latent_upscale_models 目录下的 H3 放大模型（神经网络放大，"
                                                   "底图含真实高频、无插值伪影）。\n"
                                                   "⚡ 开头的是免模型插值档：纯数学拉伸、速度快显存省，但不新增细节。\n"
                                                   "【实测结论】只保留效果最好的三项，共同点是硬边过渡带 0 格、"
                                                   "边缘原样保留：\n"
                                                   "  · 最近邻精确（nearest-exact）\n"
                                                   "  · 区域平均（area）\n"
                                                   "  · 最近邻（nearest）\n"
                                                   "原因：后续重采样要在原边缘上锐化。底图若自带 1~2 格模糊带，"
                                                   "锐化时只能在模糊边旁另画一条新边 = 轮廓双线重影；"
                                                   "边缘原样保留则只会更清晰，不会分叉。\n"
                                                   "带过渡带的插值方式（球面/双线性/三线性/双三次）实测都更差，已移除。"}),
                "放大模式": (["按倍数", "目标尺寸", "兆像素"], {"default": "按倍数",
                             "tooltip": "目标尺寸计算方式：按倍数/目标尺寸/兆像素。"}),
                "放大倍数": ("FLOAT", {"default": 2.0, "min": 1.0, "max": 4.0, "step": 0.05,
                                      "tooltip": "仅「按倍数」模式生效。下限 1.0 = 只能放大不能缩小；"
                                                 "填 1.0 等于不放大（节点会原样返回，等同空跑）。"}),
                "目标宽度": ("INT", {"default": 1280, "min": 64, "max": 8192, "step": 8,
                                     "tooltip": "仅「目标尺寸」模式生效。"}),
                "目标高度": ("INT", {"default": 704, "min": 64, "max": 8192, "step": 8,
                                     "tooltip": "仅「目标尺寸」模式生效。"}),
                "兆像素": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 16.0, "step": 0.1,
                                     "tooltip": "仅「兆像素」模式生效。"}),
                # ---- 分块预设 ----
                "显存预设": (list(_VRAM_PRESETS.keys()), {"default": "24 GB",
                             "tooltip": "一键设置空间分块瓦片尺寸。显存越大瓦片越大、切块越少、拼贴感越轻。"}),
                # ---- 预显影（仅「⚡」免模型插值档生效）----
                "预显影步数": ("INT", {"default": 0, "min": 0, "max": 8, "step": 1,
                                      "tooltip": "仅选「⚡」开头的免模型插值档时生效；用真模型时忽略本项。"
                                                 "先用整幅低强度采样把插值底图磨成清晰底图，再进空间分块重绘。\n"
                                                 "⚠ 建议保持 0：现在提供的三种免模型插值（最近邻精确/区域平均/最近邻）"
                                                 "本就没有过渡带，无需打磨；预显影只会让边缘被再重绘一遍，"
                                                 "多一道锐化，反而可能诱发轮廓双线重影。"}),
                "预显影强度": ("FLOAT", {"default": 0.4, "min": 0.05, "max": 0.95, "step": 0.05,
                                      "tooltip": "预显影的起始去噪强度（越低越忠实底图；越高重绘越强、越易偏离）。"
                                                 "仅在确实开启预显影时有意义，建议 0.3~0.4："
                                                 "0.05~0.2 效果太弱，0.7 以上容易偏离原图。"
                                                 "仅预显影步数 > 0 时生效。"}),
                # ---- 时间分块（与空间分块独立：连了才生效，互不干扰）----
                "每块帧数": ("INT", {"default": 73, "min": 5, "max": 2000, "step": 1,
                                    "tooltip": "时间分块的单块长度（解码后的视频帧）。73 帧 ≈ 3 秒 @24fps。\n"
                                               "H3 的时间轴只能按 5+17n 帧切（17 帧 = 1 个完整时间周期），"
                                               "填其它值会自动吸附到最近的合法档位（…39 / 56 / 73 / 90…）。\n"
                                               "块越短显存越省，但块数越多、耗时越长。\n"
                                               "⚠ 只有把「时间分块参数」接到「H3 终极放大」才生效。"}),
                "重叠帧数": ("INT", {"default": 22, "min": 5, "max": 1000, "step": 1,
                                    "tooltip": "相邻时间块之间重叠的帧数，同样吸附到 5+17n 档位，"
                                               "且必须小于「每块帧数」（22 帧 ≈ 1 秒）。\n"
                                               "这段帧不重新生成：上一块已精修的结果会被原样锁进下一块的开头，"
                                               "只作为上下文给模型看（防块间运动断裂），生成后丢弃。\n"
                                               "给得越大块间越连贯，但每块重复计算的部分越多。"}),
                # ---- 放大阶段的时间分块（后加的，必须留在最后）----
                # ⚠ 新增参数一律追加到本字典**末尾**：ComfyUI 的已保存工作流是按
                #   widgets_values 的**位置**回填的，在中间插入会让老工作流整体错位一格。
                #   （实测事故：本项曾插在「预显影步数」之后，结果老工作流的
                #    预显影强度读到了每块帧数的 73、本项读到了预显影强度的 0.3。）
                "放大时间分块": (_UP_CHUNK_CHOICES, {"default": _UP_CHUNK_CHOICES[0],
                                                "tooltip": "放大模型**内部** 3D 卷积的时间切块长度（latent 步）。\n"
                                                           "跟上面「每块帧数」（重采样阶段的时间分块）是两码事：\n"
                                                           "那个切的是重采样的段，这个切的是放大器一次前向能扛多少帧。\n"
                                                           "【自动（按显存）】← 推荐：按当前空闲显存挑块长，显存越紧切得越碎。\n"
                                                           "块越小越省显存、越慢。爆显存（尤其 8GB 卡放大到大画布）时手动往小调，\n"
                                                           "最小 12 —— 再小也没有核心区了，此时只能降低「放大倍数」\n"
                                                           "或改用「⚡」免模型插值档（控制台会打这行提示）。"}),
            },
        }

    RETURN_TYPES = (H3_UPSCALE_PARAM, H3_SPATIAL_PARAM, H3_TEMPORAL_PARAM)
    RETURN_NAMES = ("放大参数", "空间分块参数", "时间分块参数")
    FUNCTION = "build"
    CATEGORY = "👨‍🏫班长/MiniMax H3"

    def build(self, 放大模型, 放大模式, 放大倍数, 目标宽度, 目标高度, 兆像素,
              显存预设, 预显影步数, 预显影强度, 每块帧数, 重叠帧数,
              放大时间分块=_UP_CHUNK_CHOICES[0]):
        放大 = {
            "model_name": 放大模型,
            "mode_code": {"按倍数": "scale", "目标尺寸": "target", "兆像素": "megapixels"}[放大模式],
            "scale": 放大倍数, "width": 目标宽度, "height": 目标高度, "megapixels": 兆像素,
            "force_unload": True, "device": "cuda",
            "pre_steps": int(预显影步数), "pre_denoise": float(预显影强度),
            # 0 = 自动（按显存），正整数 = 手动块长
            "temporal_chunk": (0 if str(放大时间分块).startswith("自动")
                               else int(str(放大时间分块).split(" ")[0])),
        }

        p = _VRAM_PRESETS[显存预设]

        tw = th = px_to_lat(p["tile"])
        # 重叠取「本档预设值」与「比例算出的值」的较大者，再用 tw-ALIGN 封顶。
        # 小瓦片由此获得与大瓦片同量级的边界上下文 → 接缝两侧对得齐，亮条/闪消失。
        # 未给档位配 overlap 时退回 _OVERLAP_MIN_CELLS（各档现有取值已逐一核对不变）。
        _ov_preset = snap_align(p.get("overlap", _OVERLAP_MIN_CELLS))
        ol_w = min(tw - ALIGN, max(_ov_preset, snap_align(tw * _OVERLAP_RATIO)))
        ol_h = min(th - ALIGN, max(_ov_preset, snap_align(th * _OVERLAP_RATIO)))
        mt = min(px_to_lat(_MIN_TILE), th, tw)
        # 不带 fw/fh/feather：两条消费路径（主节点核心区独占融合、SelfLift 满分辨率
        # 收尾）都只用 tw/ol_*/mt，其余字段无人消费，故不产出。
        空间 = {"tw": tw, "th": th, "ol_w": ol_w, "ol_h": ol_h, "mt": mt}
        # 时间分块只带两个用户意图值；合法档位吸附与窗口划分放在主节点做——
        # 那里才拿得到 latent 的真实长度。
        时间 = {"chunk_frames": int(每块帧数), "overlap_frames": int(重叠帧数)}
        return (放大, 空间, 时间)


# ============================================================
# 空间长边单轴分块（对齐 ComfyUI-MiniMax-H3-Block-based-sampler）
# ------------------------------------------------------------
# 原自研 2D 网格 + 颜色匹配补偿链因与 H3 条件网格冲突而弃用，
# 全部改为以第三方成功实现为准：
#   - 只沿长边（H 或 W）单轴切条，避免二维网格造成条件几何错位
#   - Euler 采样器：逐步同步——每 sigma 步算完所有 tile 并按核心区窗口融合成
#     全局 denoised 再推进一次，消除接缝与块颜色漂移（显存仍=单瓦片）
#   - 非 Euler：逐块完整采样后核心区窗口融合
#   - 关键帧：与画布尺寸一致则整幅应用（不逐块裁）；低清则停止硬注入
#   - 音频随模型联合采样（不参与空间分块），同步/逐块时取各块均值；
#     输出是否采用由主节点「音频重绘强度」决定（0=透传原始音频，1=全部重绘）
# ============================================================
# 硬约束（勿改）：本节点的分块融合窗口必须与「干净上下文掩码」完全相同，即核心区二值。
# 依据（H3 原生语义）：forward 会做 out = out * denoise_mask。
#   · 融合窗口比掩码宽（羽化>0）→ 窗口内落在掩码 0 区的像素被回填成静态底图，
#     底图与生成内容半透明叠加 = 接缝重影；
#   · 把掩码也一起扩宽让羽化区透明 → 相邻两块在过渡带各自完整生成一遍，高起始 sigma
#     下两者结果不一致 = 接缝处出现多余的重复画面。
# 唯一自洽做法：每个像素只由唯一一块生成（核心区独占、零羽化重叠）。
#
# 上游实证（LBH 仓库 issue #51，第三方实测）：Split Upscale 用「接缝带渐变 ramp +
# 重采样」的策略（seam_denoise < 1.0），实测出**每 17 帧一次的亮度脉冲**——17 帧正是
# H3 VAE 的一个时间块（17 像素帧 / 5 latent 步）。根因不在瓦片尺寸、也不在颜色匹配，
# 而在 ramp 本身：ComfyUI 的 inpaint wrapper 会按 (1 - mask) 把源底图混进每一步和
# 最终结果，ramp 区于是变成「精修 latent 与放大器底图的逐体素混合」，解码后在每个
# 时间块开头留一个台阶；新鲜区相对 ramp 越窄，脉冲越强（实测 0.91 vs 原生 0.05）。
# 该 issue 的修复值正是 seam_denoise = 1.0。
# 我们的掩码是二值的（没有 ramp 区），等价于它的 1.0 —— 不受影响。
# **这条是「别再把羽化 / 渐变 ramp 加回来」的外部证据，勿改。**
#
# 说明：`_make_core_window_1d`（余弦羽化窗）仍供 SelfLift 满分辨率收尾使用——
# 那里的掩码是整幅的（非逐块核心区），窗口与会话外的掩码无同形要求，故不受本约束限制。


def _make_core_window_1d(size, c_lo, c_hi, feather, dtype, device):
    """核心区独占窗：本 tile 只在核心 [c_lo, c_hi) 内取值（=1），核心边界做
    feather 宽余弦过渡，核心区外的 overlap 余量全为 0（由相邻 tile 的核心覆盖）。

    相邻 tile 的过渡带区间相同、方向相反（一个 1→0、一个 0→1），权重和恒为 1，
    因此融合不会把两份预测半透明叠加——从机制上消除接缝重影。
    """
    w = torch.zeros(size, dtype=dtype, device=device)
    lo = max(0, int(c_lo))
    hi = min(size, int(c_hi))
    if hi <= lo:
        w.fill_(1.0)
        return w
    half = max(1, min(int(feather) // 2, (hi - lo) // 2))
    w[lo:hi] = 1.0
    if lo > 0:
        a = max(0, lo - half)
        b = min(size, lo + half)
        t = torch.linspace(0.0, 1.0, b - a, dtype=dtype, device=device)
        w[a:b] = 0.5 - 0.5 * torch.cos(t * math.pi)
    if hi < size:
        a = max(0, hi - half)
        b = min(size, hi + half)
        t = torch.linspace(0.0, 1.0, b - a, dtype=dtype, device=device)
        w[a:b] = 0.5 - 0.5 * torch.cos((1.0 - t) * math.pi)
    return w


def _fit_n_tiles_to_free_vram(n_tiles, H, W, T, device, model_patcher=None, overlap=0):
    """按实际空闲显存给分块数兜底加切，逻辑与放大器侧 _smart_tile_count 同构。

    「显存预设」是用户对显卡档位的静态声明，但同一次运行里别的节点可能正占着
    显存（VAE、上一个模型的残留），按预设满打满算就会 OOM。故测一次真实空闲，
    不够就往上加切。

    估算式与放大器侧**故意不同**：那里估的是卷积特征激活（∝ 面积×通道×并行特征图），
    这里是 Transformer 注意力激活，随每块的 token 数（长边格数 × 另一维格数 × T）
    增长，且每步都对全幅反复计算。系数 _ATT_ACT_FACTOR 是按经验取的保守并行倍数，
    与 _smart_tile_count 的 act_factor=12 同一量级；宁可多切也不要 OOM。

    预设与实际空闲取小：预设是上限（用户可能特意要小 tile），实际空闲是兜底。
    """
    if device is None or getattr(device, "type", "") != "cuda":
        return n_tiles
    try:
        comfy.model_management.soft_empty_cache()
    except Exception:
        pass
    try:
        free_bytes, _total = torch.cuda.mem_get_info(device)
    except Exception:
        return n_tiles
    # ── 主模型权重：只提示，不参与预算 ────────────────────────────────────
    # 采样是懒加载的，此刻主模型常不在卡上，mem_get_info 量到的空闲比采样时真实
    # 可用多出一整个权重。曾经把这部分从预算里扣掉，结果预算一变小就触发「自动
    # 加切」，块数被推到 8~16 → 二采耗时成倍上涨，表现为「卡住不动」（用户反馈
    # 大面积二采变慢即由此而来）。切块根本减不了权重，扣它只会误伤。故改为只提示。
    missing = 0
    if model_patcher is not None:
        try:
            missing = max(0, int(model_patcher.model_size()) - int(model_patcher.loaded_size()))
        except Exception:
            missing = 0
    if missing > 0:
        print(f"[H3空间] ⓘ 主模型还有 {_fmt_mem(missing)} 权重未进显存（采样时才加载），"
              f"当前空闲 {_fmt_mem(free_bytes)}。若二采偏慢，优先连「时间分块参数」降 T。")
    # 512MB 余量：融合画布是整幅尺寸、每步还要落一块全幅 fp32 中间量。
    budget = free_bytes - 512 * 1024 * 1024
    if budget <= 0:
        print(f"[H3空间] ⚠ 实际空闲显存仅 {_fmt_mem(free_bytes)}，分块数维持 {n_tiles}")
        return n_tiles

    long_cells = max(H, W) // ALIGN
    short_cells = min(H, W) // ALIGN

    def est(n):
        """n 块时每块的注意力激活估算。n<=1 视作整幅，返回整幅值（不是 None，
        否则调用方打印当前块数时会拿到 None）。

        注：这里按 long_cells/n 估（不含两侧重叠带），是**有意**偏松的口径。
        曾改成与 _compute_tile_starts 同口径（stride + 2×overlap，每块实际宽
        1.67 倍），结果预算一紧就触发自动加切，块数从 3 被推到 8~16，二采耗时
        成倍上涨 —— 用户大面积反馈「二采卡住不动」。宁可估松一点少切块：
        真不够时由 ComfyUI 的显存管理兜底，也比把块数推爆更划算。
        """
        seg_long = math.ceil(long_cells / max(1, n))
        tokens = seg_long * short_cells * max(1, T)
        return tokens * _ATT_BYTES_PER_TOKEN * _ATT_ACT_FACTOR

    # 现有块数装得下就直接维持，不再往上加。原先这里无条件从 n_tiles+1 开始找，导致
    # 每次都比预设多切一块（多一条接缝，是分身/蠕动的诱因之一），且日志里打出
    # 「按 N 块估算≈X GB 超出」是固定文案、并不代表真的超出。
    _w = f" − 待加载权重 {_fmt_mem(missing)}" if missing else ""
    if est(n_tiles) <= budget:
        print(f"[H3空间] 显存兜底：空闲={_fmt_mem(free_bytes)}{_w} → 预算 {_fmt_mem(budget)} | "
              f"按 {n_tiles} 块估算≈{_fmt_mem(est(n_tiles))} 装得下 → 维持 {n_tiles} 块")
        return n_tiles

    # 加切上限从 16 降到 8：块数越多，每块两侧重叠带占比越高，总 token 不降反升
    # ——单次 forward 是小了，但次数成倍涨，总账更慢；而且窄块上下文不足会出
    # 分身/蠕动。实测切到 16 块时二采慢到像卡死（用户大面积反馈）。宁可到 8 为止。
    for cand in (n_tiles + 1, n_tiles + 2, n_tiles + 4, 6, 8):
        if cand <= n_tiles:
            continue
        e = est(cand)
        if e <= budget:
            print(f"[H3空间] 显存兜底：空闲={_fmt_mem(free_bytes)}{_w} → 预算 {_fmt_mem(budget)} | "
                  f"按 {n_tiles} 块估算≈{_fmt_mem(est(n_tiles))} 超出 → 自动切 {cand} 块"
                  f"（每块≈{_fmt_mem(e)}）")
            return cand
    print(f"[H3空间] ⚠ 空闲={_fmt_mem(free_bytes)}{_w} | 预算={_fmt_mem(budget)} 仍不足"
          f"（{n_tiles} 块估算≈{_fmt_mem(est(n_tiles))}）→ 最多切 8 块，不再往上。"
          f"若二采很慢或显存吃紧，优先连「时间分块参数」降 T（比加切空间块有效得多）。")
    return n_tiles if n_tiles >= 8 else 8


def _fmt_mem(n):
    if n is None:
        return "n/a"
    if n >= 2 ** 30:
        return f"{n / 2**30:.1f} GB"
    return f"{n / 2**20:.0f} MB"


def _compute_tile_starts(total, n_tiles, overlap):
    """单轴分块：整除 stride + 两侧 overlap，末尾补足，去重加固尾。

    H3 模型按 2×2 patch 网格工作（坐标、offset 均以 latent 格 // 2 计），
    因此 tile 起点与 stride 必须对齐到 2 的倍数，否则 patch 坐标错位半格，
    相邻块接缝处出现重影/错位（1.5x 等非整数倍放大时尤其明显）。
    """
    if n_tiles <= 1:
        return [0], total
    # 对齐到偶数（patch 网格）；ALIGN=2
    stride = int(round(total / n_tiles))
    stride = max(ALIGN, (stride // ALIGN) * ALIGN)
    overlap = (overlap // ALIGN) * ALIGN
    tile_size = stride + 2 * overlap
    # 核心区边界按画布全长均分，而不是用 stride 累加：stride 向下取偶会累积零头，按 stride
    # 累加时末块窗口盖不到画布末端，末端几列不属于任何核心区 → 融合权值为 0 → 被合并除法
    # 压成 0（解码后即画面边缘的脏条）。均分边界可在「块数不变、块尺寸不变（显存不变）」
    # 的前提下让末段核心恰好收在画布末端：既不多切一块（多一块就多一条接缝），也不会把末块
    # 挤成窄条（窄块上下文不足，采样时会凭空造出分身 / 蠕动的一团）。向上取偶保持起点偶数。
    starts = []
    for i in range(n_tiles):
        b = ((int(round(i * total / n_tiles)) + ALIGN - 1) // ALIGN) * ALIGN
        starts.append(max(0, b - overlap))
    dedup = []
    for s in starts:
        if not dedup or s > dedup[-1]:
            dedup.append(s)
    return dedup, tile_size


# ============================================================
# 时间分块（沿时间轴切块。与空间分块正交：外层切时间、内层切空间，互不干扰）
# ------------------------------------------------------------
# H3 的时间轴不是任意可切的：每个 latent 步覆盖的像素帧按 1,4,4,4,4 循环，
# 5 个 latent 步 = 17 帧才是一个完整周期。因此：
#   · 合法窗口帧数只有 5 + 17k（对应 latent 步 5k+2）；
#   · 每个窗口的起点必须落在周期相位 0（latent 步索引为 5 的倍数），否则模型
#     看到的时间相位与整条不一致，跨块的运动会对不上。
# 取 stride = chunk_t - overlap_t：两者都是 5k+2，差恒为 5 的倍数 → 每个窗口
# 起点自动落在相位 0，不需要任何额外对齐，也不必注入全局时间坐标（H3 的
# 时间 RoPE 只看相对差，相位对齐后块内相对差与整条逐位相同）。
# 块间接力：上一块的真实输出锁进本块开头（denoise_mask=0，不重绘），丢弃这段
# 重叠后拼接 → 核心区严格连续覆盖整条，每帧只由唯一一块生成，无缝且零漂移。
# ============================================================

# 每个 latent 步覆盖的像素帧（与 comfy.ldm.minimax.model.FRAME_PER_TOKEN 一致）
_FRAME_PER_TOKEN = (1, 4, 4, 4, 4)


def _frame_offsets(total_t):
    """每个 latent 步起始的像素帧索引，长度 total_t + 1。"""
    out = [0]
    for k in range(total_t):
        out.append(out[-1] + _FRAME_PER_TOKEN[k % len(_FRAME_PER_TOKEN)])
    return out


def _steps_for_frames(n):
    """从周期相位 0 起，覆盖 n 个像素帧需要几个 latent 步；不整则返回 None。"""
    k, covered = 0, 0
    while covered < n:
        covered += _FRAME_PER_TOKEN[k % len(_FRAME_PER_TOKEN)]
        k += 1
    return k if covered == n else None


def _h3_temporal_windows(total_t, chunk_frames, overlap_frames):
    """按 H3 帧网格规划时间窗口。返回 (windows, overlap_t)。

    windows 为 latent 步区间 [(start, end)]。需要分块时才返回多段，否则返回
    单段 [(0, total_t)] —— 调用方据此决定走分块还是整条。
    """
    offsets = _frame_offsets(total_t)
    total_frames = offsets[-1]
    legal = []
    k = 0
    while True:
        n = 5 + 17 * k
        if n > total_frames:
            break
        legal.append(n)
        k += 1
    if len(legal) < 2:
        return [(0, total_t)], 0
    chunk_f = min(legal, key=lambda n: abs(n - int(chunk_frames)))
    chunk_t = _steps_for_frames(chunk_f)
    # 重叠必须严格小于块长，否则 stride 为 0；块长已是最小档则无法分块。
    smaller = [n for n in legal if n < chunk_f]
    if not smaller or chunk_t is None or chunk_t >= total_t:
        return [(0, total_t)], 0
    overlap_f = min(smaller, key=lambda n: abs(n - int(overlap_frames)))
    overlap_t = _steps_for_frames(overlap_f)
    stride = chunk_t - overlap_t
    last = total_t - chunk_t
    starts = list(range(0, last + 1, stride))
    if not starts or starts[-1] != last:
        starts.append(last)
    return [(s, min(total_t, s + chunk_t)) for s in starts], overlap_t


def _temporal_clean_mask(T, lock_steps, device):
    """时间块掩码：头部 lock_steps 步（上一块已精修、接过来的尾部）置 0 = 锁定不重绘。

    与空间分块的干净上下文掩码同一条链路：0 区由 denoise_mask 一路钉住（guider 侧
    回填原值 + H3 forward 侧把 timestep 乘 0），模型只把它当上下文读、不重新生成。
    与空间掩码叠加时直接相乘即可（空间维为 1，广播自动生效）。
    """
    m = torch.ones((1, 1, int(T), 1, 1), dtype=torch.float32, device=device)
    if lock_steps > 0:
        m[:, :, :int(lock_steps)] = 0.0
    return m


def _slice_cond_time(cond, frame_start, frame_end, total_frames):
    """只保留落在本时间窗内的关键帧，并把时间索引改成本块内的相对值。

    不筛会把片头的首帧锚注入每一块 —— 模型会理解成「本块的第 0 帧就是片头」，
    于是每一块开头都被拉回第一帧的画面。末块额外放行尾帧锚（帧号 == 总帧数）。
    """
    if cond is None:
        return cond
    out = []
    for tensor, d in cond:
        nd = dict(d)
        kfs = nd.get("minimax_keyframes")
        if kfs:
            local = []
            for kf in kfs:
                f = int(kf.get("resolved_frame_index", 0))
                last_window = frame_end >= total_frames
                if frame_start <= f < frame_end or (last_window and f == total_frames):
                    item = dict(kf)
                    item["resolved_frame_index"] = max(0, f - frame_start)
                    local.append(item)
            if local:
                nd["minimax_keyframes"] = local
            else:
                nd.pop("minimax_keyframes", None)
        out.append([tensor, nd])
    return out


def _tile_clean_mask(tile_h, tile_w, T, core_lo, core_hi, axis, device):
    """构建单块的干净上下文 mask（1=生成，0=保留）——它同时就是本块的融合窗口。

    1 区 = 核心区 [core_lo, core_hi)，也就是本块唯一负责生成、且唯一被采纳的像素范围；
    0 区 = 核心区外的 overlap 余量（保留放大后的原始像素、t≈0.999），只作为上下文条件
    喂给模型，不参与融合。返回 [1, 1, T, tile_h, tile_w] 的 mask。

    「掩码与融合窗口同形」是本节点消除接缝异常的根本机制（见文件头硬约束注释）：
    1 区二值、相邻块核心区严格互补覆盖 → 每个像素只由唯一一块生成，既不会把静态底图
    混进接缝（重影），也不会让相邻两块在同一处各生成一遍（接缝处多余重复画面）；
    同时块外余量恒为 t≈0.999 的干净条件行 → 高起始 sigma 下模型知道边界之外是什么。
    """
    size = tile_h if axis == "H" else tile_w
    lo = max(0, int(core_lo))
    hi = min(size, int(core_hi))
    w1d = torch.zeros(size, dtype=torch.float32, device=device)
    if hi > lo:
        w1d[lo:hi] = 1.0
    else:
        w1d.fill_(1.0)
    mask = w1d.view(1, 1, 1, -1, 1) if axis == "H" else w1d.view(1, 1, 1, 1, -1)
    return mask.expand(1, 1, T, tile_h, tile_w).contiguous()


def _adapt_keyframes_cond(cond, h, w):
    """关键帧尺寸与画布一致 → 保留为高清硬关键帧（逐块采样时按 tile 裁切）；
    尺寸不一致 → 去掉 minimax_keyframes（停止硬注入），避免低清锚点拖影/发糊。

    返回 (cond, has_hard_kf)。has_hard_kf=True 表示存在高清硬关键帧（尺寸==画布），
    此时逐步同步 Euler 会因关键帧空间网格与 tile 错位而失效，必须走逐块采样。
    """
    if cond is None:
        return cond, False
    out = []
    removed = False
    has_hard_kf = False
    for tensor, d in cond:
        nd = dict(d)
        kfs = nd.get("minimax_keyframes")
        if kfs:
            ok = True
            for kf in kfs:
                lt = kf.get("latent")
                if lt is not None and (lt.shape[3] != h or lt.shape[4] != w):
                    ok = False
                    break
            if not ok:
                nd.pop("minimax_keyframes", None)
                removed = True
            else:
                has_hard_kf = True
        out.append([tensor, nd])
    if removed:
        print("[H3分块] ⚠ 检测到低清首/尾帧（尺寸≠画布），本次停止其硬注入，避免拖影/发糊")
    return out, has_hard_kf


def _crop_kf_axis(cond, axis, start, end):
    """按当前 tile 沿单轴裁剪 keyframe，并补齐 H3 的 2×2 patch 网格。

    对应参考插件 _apply_minimax_keyframe_region：顺序采样时高清 keyframe 必须
    与当前 tile 用同一个空间网格，否则 model forward 触发 shape mismatch。
    """
    out = []
    for tensor, d in cond:
        nd = dict(d)
        kfs = nd.get("minimax_keyframes")
        if kfs:
            tiled = []
            for kf in kfs:
                item = dict(kf)
                lt = item.get("latent")
                if lt is not None:
                    if axis == "H":
                        region = lt[:, :, :, start:end, :].contiguous()
                    else:
                        region = lt[:, :, :, :, start:end].contiguous()
                    # H3 forward 会把目标视频补到偶数 H/W；条件需同步补齐避免少一行/列 token
                    pad_h = (-region.shape[-2]) % 2
                    pad_w = (-region.shape[-1]) % 2
                    if pad_h or pad_w:
                        region = F.pad(region, (0, pad_w, 0, pad_h, 0, 0), mode="replicate")
                    item["latent"] = region
                tiled.append(item)
            nd["minimax_keyframes"] = tiled
        out.append([tensor, nd])
    return out


def _sample_synchronized_euler(model, cond, full_nested, full_noise, seed, sampler,
                               sigmas, axis, starts, tile_size, overlap, H, W, audio,
                               time_mask=None):
    """逐步同步 Euler 单轴分块（第三方机制）。

    每次 guider.sample 内：每 sigma 步依次预测全部 tile，再按「核心区独占」合并成
    全局 denoised——融合窗口就是该块的干净上下文掩码（核心区二值），相邻块核心区
    严格互补覆盖，因此每个像素只由唯一一块生成、不存在任何交叉淡化；音频取各块均值，
    最后只推进一次 Euler。所有块共享同一条全局轨迹；重叠区只作上下文不作融合，
    接缝与重复画面从机制上消除；显存峰值仍只等于单块。
    """
    full_shapes = [tuple(full_nested.tensors[0].shape), tuple(full_nested.tensors[1].shape)]
    axis_dim = 3 if axis == "H" else 4
    axis_total = H if axis == "H" else W
    last = len(starts) - 1

    regions = []
    T = full_nested.tensors[0].shape[2]
    for tile_idx, ax_start in enumerate(starts):
        ax_end = min(ax_start + tile_size, axis_total)
        actual_size = ax_end - ax_start
        # 核心区边界：与下一块的交界 = 下一块起点 + overlap（任意分块几何下均为完整划分）；
        # 首块核心从 0 起，末块核心延伸到画布末端
        c_lo = 0 if tile_idx == 0 else min(axis_total, ax_start + overlap) - ax_start
        c_hi = (axis_total - ax_start if tile_idx == last
                else min(axis_total, starts[tile_idx + 1] + overlap) - ax_start)
        # 本块干净上下文掩码（1=生成 0=保留，与逐块路径 _tile_clean_mask 同构）：
        # 核心区正常去噪，overlap 边带标保留；预测前注入 conds 的 model_conds，
        # H3 forward 据此把保留行钉在视觉条件时间步（t≈0.999）并置零其速度。
        # 它同时就是本块的融合窗口（硬约束：两者必须同形，见文件头注释）。
        tile_h = actual_size if axis == "H" else H
        tile_w = actual_size if axis == "W" else W
        clean_mask = _tile_clean_mask(tile_h, tile_w, T, c_lo, c_hi, axis, audio.device)
        # 时间分块的锁定步叠加进来：空间维为 1，广播即可，无需按轴切片。
        if time_mask is not None:
            clean_mask = clean_mask * time_mask
        regions.append((ax_start, ax_end, clean_mask))

    # 权重表必须保留时间维：叠加时间分块的锁定步后，同一空间位置在不同时间步的
    # 掩码不再相同，只取时间维第 0 帧会把整块误判成「未被任何块覆盖」。
    weight_shape = (1, 1, T, axis_total, 1) if axis == "H" else (1, 1, T, 1, axis_total)
    weights = torch.zeros(weight_shape, dtype=torch.float32, device=audio.device)
    for ax_start, ax_end, mask in regions:
        # 只需掩码沿分块轴的剖面，故把另一空间维切到长度 1；时间维要留着。
        if axis == "H":
            weights[:, :, :, ax_start:ax_end, :] += mask[:, :, :, :, :1]
        else:
            weights[:, :, :, :, ax_start:ax_end] += mask[:, :, :, :1, :]
    weights = weights.clamp(min=1e-8)
    # 兜底：未被任何块核心区覆盖的列（权值≈0）不能被合并除法压成 0（解码后就是画面
    # 右缘的脏竖条），标记出来逐步由上下文底图原地留守。
    uncovered_mask = weights < 1e-6
    uncovered = bool(uncovered_mask.any())
    if uncovered:
        print(f"[H3空间] ⚠ {int(uncovered_mask.sum())} 列未被任何分块覆盖，逐步由上下文底图留守")

    # 干净上下文画布：块外像素让模型"看得见"。每步每块预测前，把「掩码=0 的块外
    # 余量」换成静态放大底图按视觉条件时间步注入（aug 干净 + 1-aug 噪声，与
    # KSamplerX0Inpaint.scale_latent_inpaint / 逐块路径逐位同构，t≈0.999 自洽）；
    # 同时把本块掩码注入 conds 的 model_conds，H3 forward 据此把保留行 t 钉 0.999、
    # 速度置零——块外真实像素从第一步即为已知条件，空背景块不凭空补主体（防小人）、
    # 相邻块边界两侧看到同一份内容逐步收敛（防接缝）。
    # 上下文源固定为静态底图 x0_init（不随步更新）：与逐块路径注入的 tile latent
    # 完全一致，避免"移动靶"导致边界内容每步被二次描画（重影）。
    # 注入用的噪声与 x 初始化同源（full_noise），保证跨块/跨步一致。
    x0_init = full_nested.tensors[0].to(device=audio.device, dtype=torch.float32)
    noise_full = full_noise.tensors[0].to(device=audio.device, dtype=torch.float32)
    aug = float(_VISUAL_COND_TIMESTEP)

    # 注：此处曾有一道「低起始 sigma 闸门」（sigma<0.55 时自动关闭干净上下文），
    # 已移除。原因：该闸门的依据是「边带静态副本与核心区构成矛盾双描述→重影」，
    # 但实测 H3 的 inpaint 在二值 mask 下 x_blend_weight 恒为 0、并不产生混合带，
    # 前提不成立；而关闭干净上下文会失去其防接缝作用，分块时反而可能新增接缝。
    # 故恢复为无条件启用干净上下文。
    print(f"[H3空间] 干净上下文画布已启用（{len(regions)} 块，边带按 t≈0.999 注入 + 原生 denoise_mask）")

    # 【A｜活动上下文】原先 ctx 源固定为静态底图 x0_init（:987/:991），整趟采样不动，
    # 于是边带与核心区始终是「同一内容的两个版本」：低 sigma 下核心区那份被模型
    # 改动过，边带那份原封不动 → 两者错位 = 重影。改为每步融合完成后用本次结果
    # 覆盖 ctx 源，让边带与核心区来自同一条收敛轨迹、轮廓逐步对齐（latent 迭代
    # 回灌的思路）。第一步仍以 x0_init 起步，保证有可信初始条件。
    ctx_state = x0_init.clone()

    guider = build_guider(model, cond)
    x0_output = {}
    callback = latent_preview.prepare_callback(guider.model_patcher, sigmas.shape[-1] - 1, x0_output)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    original_extra = dict(getattr(sampler, "extra_options", {}) or {})
    original_inpaint = dict(getattr(sampler, "inpaint_options", {}) or {})

    @torch.no_grad()
    def synchronized_euler(model_k, x, step_sigmas, extra_args=None, callback=None, disable=None,
                           s_churn=0.0, s_tmin=0.0, s_tmax=float("inf"), s_noise=1.0, **_unused):
        extra_args = {} if extra_args is None else extra_args
        s_in = x.new_ones([x.shape[0]])
        prepared_model = model_k.inner_model.inner_model
        saved_model_shapes = getattr(prepared_model, "latent_shapes", None)
        saved_conds = {}
        added_mask_keys = []
        # 【A】ctx_state 在外层定义、内层每步写回 → 必须声明 nonlocal
        nonlocal ctx_state

        def set_tile_shapes(tile_shapes, clean_mask=None):
            prepared_model.latent_shapes = tile_shapes
            for cond_group in getattr(model_k.inner_model, "conds", {}).values():
                if cond_group is None:
                    continue
                for cd in cond_group:
                    model_conds = cd.get("model_conds", {}) if isinstance(cd, dict) else {}
                    sc = model_conds.get("latent_shapes")
                    if sc is not None and hasattr(sc, "cond"):
                        saved_conds.setdefault(id(sc), (sc, sc.cond))
                        sc.cond = tile_shapes
                    # 干净上下文掩码注入 model_conds：经 get_area_and_mult→process_cond
                    # →cond_cat→apply_model kwargs 直达 H3 forward 的 denoise_mask，
                    # 与逐块路径（sample_piece 的 denoise_mask=）同一条模型侧链路。
                    if clean_mask is not None:
                        dm = model_conds.get("denoise_mask")
                        if dm is None:
                            model_conds["denoise_mask"] = comfy.conds.CONDRegular(clean_mask)
                            added_mask_keys.append(model_conds)
                        else:
                            saved_conds.setdefault(id(dm), (dm, dm.cond))
                            dm.cond = clean_mask

        try:
            for step_idx in comfy.utils.model_trange(len(step_sigmas) - 1, disable=disable):
                if s_churn > 0:
                    gamma = min(s_churn / (len(step_sigmas) - 1), 2 ** 0.5 - 1) \
                        if s_tmin <= step_sigmas[step_idx] <= s_tmax else 0.0
                else:
                    gamma = 0.0
                sigma_hat = step_sigmas[step_idx] * (gamma + 1)
                if gamma > 0:
                    eps = torch.randn_like(x) * s_noise
                    x = x + eps * (sigma_hat ** 2 - step_sigmas[step_idx] ** 2) ** 0.5

                streams = comfy.utils.unpack_latents(x, full_shapes)
                video_x = streams[0]
                audio_x = streams[1] if len(streams) > 1 else None
                video_denoised = torch.zeros_like(video_x, dtype=torch.float32)
                audio_denoised = torch.zeros_like(audio_x, dtype=torch.float32) if audio_x is not None else None

                for ax_start, ax_end, clean_mask in regions:
                    # 逐步同步路径：每步每块一次前向，块间检查取消（同步路径块数多、
                    # 步数多，是最容易点完取消还在干等的地方）
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    if axis == "H":
                        video_tile = video_x[:, :, :, ax_start:ax_end, :]
                        ctx_t = ctx_state[:, :, :, ax_start:ax_end, :]
                        eps_t = noise_full[:, :, :, ax_start:ax_end, :]
                    else:
                        video_tile = video_x[:, :, :, :, ax_start:ax_end]
                        ctx_t = ctx_state[:, :, :, :, ax_start:ax_end]
                        eps_t = noise_full[:, :, :, :, ax_start:ax_end]
                    # 干净上下文：1 区（生成区=本块核心区）保留带噪 tile；0 区（块外余量）
                    # 换成 ctx 源按视觉条件时间步注入（aug 干净 + 1-aug 噪声，
                    # t≈0.999 自洽，与 scale_latent_inpaint 同构）。混合权重用掩码本身：
                    # 掩码 1 区的输入是纯带噪 tile，与模型侧 m=1 的 timestep 自洽。
                    # 注：此处曾有一项「边带扰动」（sigma 低时把 aug 从 0.999 削弱到 0.90），
                    # 已随「低起始 sigma 闸门」一并移除——两者依据同一假设（边带静态副本
                    # 与核心区构成矛盾双描述），该假设已被实测否证（二值 mask 下 H3 的
                    # x_blend_weight 恒为 0，不产生混合带）。恢复为恒定 aug=0.999。
                    clean_ctx = ctx_t * aug + eps_t * (1.0 - aug)
                    tile_v_in = (video_tile.to(clean_ctx.dtype) * clean_mask
                                 + clean_ctx.to(video_tile.dtype) * (1.0 - clean_mask)).contiguous()
                    tile_streams = [tile_v_in]
                    if audio_x is not None:
                        tile_streams.append(audio_x)
                    tile_x, tile_shapes = comfy.utils.pack_latents(tile_streams)
                    del tile_v_in
                    set_tile_shapes(tile_shapes, clean_mask)
                    # 全局坐标注入：把本块 video 段空间坐标换成整幅坐标系下的真实位置，
                    # 模型才知道自己处理的是画面第几块（防分身/防接缝）。
                    _mo = extra_args.get("model_options") or {}
                    _to = _mo.setdefault("transformer_options", {})
                    if axis == "H":
                        _set_tile_offset(_to, H, W, ax_end - ax_start, W,
                                         ax_start // 2, 0)
                    else:
                        _set_tile_offset(_to, H, W, H, ax_end - ax_start,
                                         0, ax_start // 2)
                    try:
                        tile_pred = model_k(tile_x, sigma_hat * s_in, **extra_args)
                    finally:
                        _clear_tile_offset(_to)
                    pred_streams = comfy.utils.unpack_latents(tile_pred, tile_shapes)
                    pred_video = pred_streams[0].float()
                    # 不做输出侧底图回混：融合窗口与掩码同形，窗口非零处恒为 m=1，
                    # H3 forward 的 out = out * m 在 m=1 处是恒等变换；掩码 0 区（块外余量）
                    # 的预测会被掩码本身乘成 0，不会进入结果。
                    if axis == "H":
                        video_denoised[:, :, :, ax_start:ax_end, :] += pred_video * clean_mask
                    else:
                        video_denoised[:, :, :, :, ax_start:ax_end] += pred_video * clean_mask
                    if audio_denoised is not None:
                        audio_denoised += pred_streams[1].float()

                video_denoised = video_denoised / weights
                if uncovered:
                    video_denoised = torch.where(uncovered_mask, ctx_state, video_denoised)
                # 【A】本次融合结果回灌为下一步的边带上下文源：边带与核心区自此
                # 来自同一条收敛轨迹，轮廓对齐 → 消除「静态副本 vs 已改动核心区」
                # 的错位重影。原地覆盖，避免额外占用一份全幅显存。
                ctx_state = video_denoised.to(ctx_state.dtype)
                merged = [video_denoised.to(dtype=video_x.dtype)]
                if audio_denoised is not None:
                    audio_denoised = audio_denoised / float(len(regions))
                    merged.append(audio_denoised.to(dtype=audio_x.dtype))
                denoised, _ = comfy.utils.pack_latents(merged)

                prepared_model.latent_shapes = full_shapes
                if callback is not None:
                    callback({"x": x, "i": step_idx, "sigma": step_sigmas[step_idx],
                              "sigma_hat": sigma_hat, "denoised": denoised})
                d = to_d(x, sigma_hat, denoised)
                x = x + d * (step_sigmas[step_idx + 1] - sigma_hat)
            return x
        finally:
            prepared_model.latent_shapes = saved_model_shapes
            for cond_obj, old_value in saved_conds.values():
                cond_obj.cond = old_value
            # 本次新建的 denoise_mask 键：整幅/其它 tile 路径不该看到，删除还原
            for model_conds in added_mask_keys:
                model_conds.pop("denoise_mask", None)

    sync_sampler = comfy.samplers.KSAMPLER(synchronized_euler, extra_options=original_extra,
                                           inpaint_options=original_inpaint)
    samples = guider.sample(full_noise, full_nested, sync_sampler, sigmas,
                            denoise_mask=None, callback=callback, disable_pbar=disable_pbar, seed=seed)
    out_a = None
    if getattr(samples, "is_nested", False) and len(samples.tensors) > 1:
        out_a = samples.tensors[1].to(audio.device, audio.dtype)
    out_v = (samples.tensors[0] if getattr(samples, "is_nested", False) else samples)
    out_v = out_v.to(audio.device, audio.dtype)
    del weights, regions
    if audio.device.type == "cuda":
        torch.cuda.empty_cache()
    return out_v, out_a


def _sample_tiles_sequential(model, cond, full_nested, full_noise, seed, sampler, sigmas,
                             axis, starts, tile_size, overlap, H, W, audio,
                             time_mask=None):
    """非 Euler 的逐块完整采样 + 核心区独占融合（融合窗口 = 干净上下文掩码 = 核心区二值）。"""
    axis_total = H if axis == "H" else W
    device = audio.device
    output = torch.zeros_like(full_nested.tensors[0], dtype=torch.float32, device=device)
    # 权重表保留时间维（原因同逐步同步路径：叠加时间锁定后掩码不再时间均匀）
    T_all = int(full_nested.tensors[0].shape[2])
    wshape = (1, 1, T_all, axis_total, 1) if axis == "H" else (1, 1, T_all, 1, axis_total)
    weights = torch.zeros(wshape, dtype=torch.float32, device=device)
    disable_pbar = not comfy.utils.PROGRESS_BAR_ENABLED
    video_tensor = full_nested.tensors[0].to(device=device)
    audio_tensor = audio.to(device=device)
    video_noise = full_noise.tensors[0].to(device=device)
    audio_noise = full_noise.tensors[1].to(device=device)
    audio_out = torch.zeros_like(audio_tensor, dtype=torch.float32)

    starts_, tile_size_ = starts, tile_size
    for tile_idx, ax_start in enumerate(starts_):
        # 逐块路径每块都要跑完整条 sigma 曲线，块与块之间必须能响应取消
        comfy.model_management.throw_exception_if_processing_interrupted()
        ax_end = min(ax_start + tile_size_, axis_total)
        if axis == "H":
            tile_latent = video_tensor[:, :, :, ax_start:ax_end, :].contiguous()
            tile_vn = video_noise[:, :, :, ax_start:ax_end, :].contiguous()
        else:
            tile_latent = video_tensor[:, :, :, :, ax_start:ax_end].contiguous()
            tile_vn = video_noise[:, :, :, :, ax_start:ax_end].contiguous()

        has_prev = tile_idx > 0
        has_next = tile_idx < len(starts_) - 1
        # 核心区边界：与下一块的交界 = 下一块起点 + overlap；末块核心延伸到画布末端
        c_lo = 0 if not has_prev else min(axis_total, ax_start + overlap) - ax_start
        c_hi = (axis_total - ax_start if not has_next
                else min(axis_total, starts_[tile_idx + 1] + overlap) - ax_start)

        piece = {"samples": comfy.nested_tensor.NestedTensor((tile_latent, audio_tensor))}
        tn = comfy.nested_tensor.NestedTensor((tile_vn, audio_noise))
        # 高清 keyframe 必须按当前 tile 精确裁切并补齐 2×2 网格，避免 model forward shape mismatch
        tile_cond = _crop_kf_axis(cond, axis, ax_start, ax_end)
        # 干净上下文：核心区=生成、overlap 边带=保留（放大后的原始像素），防小人/防接缝
        tile_h = tile_latent.shape[3]
        tile_w = tile_latent.shape[4]
        T = tile_latent.shape[2]
        clean_mask = _tile_clean_mask(tile_h, tile_w, T, c_lo, c_hi, axis, device)
        # 时间分块的锁定步叠加进来：空间维为 1，广播即可，无需按轴切片。
        if time_mask is not None:
            clean_mask = clean_mask * time_mask
        # 全局坐标注入：把本块的空间坐标换成整幅坐标系下的真实位置。不注入时模型把
        # 每块当成从 (0,0) 开始的独立画布 → 每块都重绘一整幅画面（小人/接缝）。
        # offset 写进 model_options.transformer_options，采样时 diffusion wrapper 读取。
        _to = model.model_options.setdefault("transformer_options", {})
        if axis == "H":
            _set_tile_offset(_to, H, W, ax_end - ax_start, W, ax_start // 2, 0)
        else:
            _set_tile_offset(_to, H, W, H, ax_end - ax_start, 0, ax_start // 2)
        try:
            out = sample_piece(piece, tile_cond, model, tn, seed, sampler, sigmas,
                               denoise_mask=clean_mask)
        finally:
            _clear_tile_offset(_to)
        tile_v = (out.tensors[0] if getattr(out, "is_nested", False) else out)
        tile_v = tile_v.to(device=device)
        # 音频无空间维：每块都带完整音频预测，取各块均值即为重绘结果
        if getattr(out, "is_nested", False) and len(out.tensors) > 1:
            audio_out += out.tensors[1].to(device=device).float()

        # 融合窗口就是本块的干净上下文掩码（核心区二值）：相邻块核心区严格互补覆盖，
        # 每个像素只由唯一一块生成，故不存在交叉淡化带来的重影/双份画面。
        if axis == "H":
            output[:, :, :, ax_start:ax_end, :] += tile_v.float() * clean_mask
            weights[:, :, :, ax_start:ax_end, :] += clean_mask[:, :, :, :, :1]
        else:
            output[:, :, :, :, ax_start:ax_end] += tile_v.float() * clean_mask
            weights[:, :, :, :, ax_start:ax_end] += clean_mask[:, :, :, :1, :]
        del tile_latent, tile_vn, tile_v, clean_mask
        if device.type == "cuda":
            torch.cuda.empty_cache()

    output = output / weights.clamp(min=1e-8)
    # 兜底：未被任何块核心区覆盖的列（权值为 0）用放大底图原地留守。宁可这几列维持
    # 底图画质，也不让「0 ÷ 最小值」把整列压成 0——那解码出来就是画面右缘的脏竖条。
    uncovered = weights < 1e-6
    if bool(uncovered.any()):
        print(f"[H3空间] ⚠ {int(uncovered.sum())} 列未被任何分块覆盖，已用放大底图回填")
        output = torch.where(uncovered, video_tensor.float(), output)
    audio_out = (audio_out / max(1, len(starts_))).to(device, audio_tensor.dtype)

    return output.to(device, full_nested.tensors[0].dtype), audio_out


def _sample_spatial_tiled(chunk_v, chunk_a, cond, model, seed, sampler, sigmas,
                          chunk_noise_v, chunk_noise_a, n_tiles, overlap, time_mask=None):
    """空间长边单轴分块采样总入口。采样器 Euler 走逐步同步；否则走逐块融合。

    time_mask 是外层时间分块给的「锁定本块头部 N 步」掩码（形状 [1,1,T,1,1]，
    空间维为 1）。它与空间分块正交：不分块时直接作为 denoise_mask 下发；分块时
    与每块的核心区掩码相乘（广播即可，无需按轴切片）。不传 = 不锁定任何时间步。
    返回 (视频, 音频)：音频为模型联合采样的重绘结果，是否采用由主节点「音频重绘强度」决定。
    """
    H, W = chunk_v.shape[3], chunk_v.shape[4]
    # 关键帧适配（与画布一致 → 高清硬关键帧，需逐块裁切；否则停硬注入）
    ccond, has_hard_kf = _adapt_keyframes_cond(cond, H, W)

    # ── 设备统一 ─────────────────────────────────────────────────────
    # video 常已在 cuda（放大 / 预显影搬过去的），而 audio 可能还留在 cpu（上游接了
    # 显存净化把张量卸回 cpu，或本来就没上过卡）。NestedTensor 只做逻辑分组、不做
    # 设备对齐，guider.sample 内部 comfy.utils.pack_latents 会把两个张量 torch.cat
    # 成一个 → 跨设备直接抛
    #   "Expected all tensors to be on the same device, but found at least two
    #    devices, cuda:0 and cpu!"
    #
    # 分两种情况：
    # ① 分块路径（n_tiles>1，逐步同步 / 逐块融合）：内部把 audio.device 当推理设备用
    #    来生成 clean_mask/weights 等，而采样时 latent 必然被搬到 cuda —— 这里**必须
    #    强制统一到推理设备**，否则 clean_mask 留在 cpu、与 cuda 的 video_tile 相乘
    #    直接炸（就是本次用户报的那个错）。
    # ② 不分块路径（n_tiles<=1）：只在 video/audio 真跨设备时才统一（修 pack_latents
    #    的跨设备报错）；两者同设备时保持原样，交给 ComfyUI 按需搬 —— 小显存卡靠
    #    「整条 latent 留在 cpu、只让当前这段上卡」省显存。
    need_dev = comfy.model_management.get_torch_device()
    dev_v, dev_a = chunk_v.device, chunk_a.device
    if n_tiles > 1 or dev_v != dev_a:
        device = need_dev
        if chunk_v.device != device:
            chunk_v = chunk_v.to(device)
        if chunk_a.device != device:
            chunk_a = chunk_a.to(device)
        if chunk_noise_v.device != device:
            chunk_noise_v = chunk_noise_v.to(device)
        if chunk_noise_a.device != device:
            chunk_noise_a = chunk_noise_a.to(device)
    else:
        device = dev_v
    if time_mask is not None and time_mask.device != device:
        time_mask = time_mask.to(device)
    v_dtype, a_dtype = chunk_v.dtype, chunk_a.dtype
    full_nested = comfy.nested_tensor.NestedTensor((chunk_v, chunk_a))
    full_noise = comfy.nested_tensor.NestedTensor((chunk_noise_v, chunk_noise_a))

    if n_tiles <= 1:
        piece = {"samples": full_nested}
        out = sample_piece(piece, ccond, model, full_noise, seed, sampler, sigmas,
                           denoise_mask=time_mask)
        if getattr(out, "is_nested", False):
            return (out.tensors[0].to(device, v_dtype),
                    out.tensors[1].to(device, a_dtype))
        return out.to(device, v_dtype), chunk_a

    axis = "H" if H >= W else "W"
    axis_size = H if axis == "H" else W
    starts, tile_size = _compute_tile_starts(axis_size, n_tiles, overlap)
    if len(starts) <= 1 or tile_size >= axis_size * 0.9:
        piece = {"samples": full_nested}
        out = sample_piece(piece, ccond, model, full_noise, seed, sampler, sigmas,
                           denoise_mask=time_mask)
        if getattr(out, "is_nested", False):
            return (out.tensors[0].to(device, v_dtype),
                    out.tensors[1].to(device, a_dtype))
        return out.to(device, v_dtype), chunk_a

    # （video / audio / 噪声已在入口统一到推理设备，此处无需再搬）
    # 块数多又没有图像锚（首帧参考图）时，把「主体被每块各生成一遍」的风险先说出来，
    # 免得用户对着好几个莫名其妙的人不知道该调哪个参数。
    if len(starts) >= 4 and not has_hard_kf:
        print(f"[H3空间] 提示：{len(starts)} 块、且没有与画布同网格的首帧参考图 —— "
              "每块面对的都是完整提示词，主体可能被每块各生成一遍"
              "（一个人说话 → 好几个人说话）。\n"
              "        若出现：① 降低「去噪强度」（底图约束变强）；"
              "② 接首帧参考图（分块会按块裁切）；"
              "③ 把「显存预设」往上调一档（块更少）。")

    euler = getattr(getattr(sampler, "sampler_function", None), "__name__", "") == "sample_euler"
    if euler and not has_hard_kf:
        print(f"[H3空间] ⚡ Euler 逐步同步分块（长边{axis} 共{len(starts)}块）")
        return _sample_synchronized_euler(model, ccond, full_nested, full_noise, seed,
                                          sampler, sigmas, axis, starts, tile_size, overlap,
                                          H, W, chunk_a.to(device), time_mask=time_mask)
    if has_hard_kf:
        print(f"[H3空间] 🔀 检测到高清硬关键帧，改用逐块采样 + 按 tile 裁切关键帧（长边{axis} 共{len(starts)}块）")
    else:
        print(f"[H3空间] 🔀 逐块采样 + 核心区独占融合（长边{axis} 共{len(starts)}块）")
    return _sample_tiles_sequential(model, ccond, full_nested, full_noise, seed,
                                    sampler, sigmas, axis, starts, tile_size, overlap, H, W,
                                    chunk_a.to(device), time_mask=time_mask)


def _sample_temporal_windows(video, audio, cond, model, seed, sampler, sigmas,
                             noise_v, noise_a, windows, n_tiles=1, overlap=8):
    """沿时间轴逐段重采样，段间用「上一段的真实输出锁定」接力。

    每段 = [上一段尾部（锁定，只读不重画）] + [本段核心（唯一生成）]。锁定段由
    _temporal_clean_mask 置 0 后经 denoise_mask 链路钉住：guider 侧回填原值、
    H3 forward 侧把 timestep 乘 0，模型只把它当上下文读。生成后丢弃锁定段再拼接，
    各段核心严格连续、无缝覆盖整条时间轴 —— 每帧只由唯一一段生成，既不重影也不漂移。

    关键帧按时间窗裁剪：不筛的话片头的首帧锚会被注入每一段，模型会理解成「本段的
    第 0 帧就是片头」，于是每段开头都被拉回第一帧的画面。

    噪声取整条噪声的对应切片（不是每段重新生成），保证各段噪声互不重复。
    """
    T = int(video.shape[2])
    offsets = _frame_offsets(T)
    total_frames = offsets[-1]
    # 注意：这里刻意不把整条 video/audio/噪声搬到 cuda。时间分块的全部意义就是
    # 「整条 latent 留在 cpu，只有当前这一段上卡」——整条先上卡会让长片白白多占
    # 几份 latent 显存。故只在循环里切片，由 _sample_spatial_tiled 入口统一每段设备。
    audio_total = int(audio.shape[-1])
    v_parts, a_parts = [], []
    prev_out = None
    prev_end = None
    for wi, (s, e) in enumerate(windows):
        # 每段之间检查一次取消：长片切成十几段时，不检查的话点完取消还得干等好几段
        comfy.model_management.throw_exception_if_processing_interrupted()
        lock = max(0, prev_end - s) if prev_end is not None else 0
        v_in = video[:, :, s:e].clone()
        if lock:
            # 接力：上一段的真实输出原样放进本段开头，随后由掩码钉住，不重画
            v_in[:, :, :lock] = prev_out[:, :, -lock:].to(v_in.device, v_in.dtype)
        a0 = min(audio_total, int(round(offsets[s] * audio_total / total_frames)))
        a1 = min(audio_total, int(round(offsets[e] * audio_total / total_frames)))
        if a1 <= a0:
            a1 = min(audio_total, a0 + 1)
        a_in = audio[..., a0:a1].contiguous()
        # 音频锁定段取与视频同一处时间边界，拼接后音轨才严格等长、不错位
        a_lock = 0
        if lock:
            a_lock = max(0, min(a1 - a0,
                                int(round(offsets[s + lock] * audio_total / total_frames)) - a0))
        t_mask = _temporal_clean_mask(e - s, lock, v_in.device)
        local_cond = _slice_cond_time(cond, offsets[s], offsets[e], total_frames)
        chunk_v, chunk_a = _sample_spatial_tiled(
            v_in, a_in, local_cond, model, seed, sampler, sigmas,
            noise_v[:, :, s:e], noise_a[..., a0:a1],
            n_tiles=n_tiles, overlap=overlap, time_mask=t_mask)
        v_parts.append(chunk_v[:, :, lock:].detach().to("cpu"))
        a_parts.append(chunk_a[..., a_lock:].detach().to("cpu"))
        prev_out, prev_end = chunk_v, e
        del v_in, chunk_v, chunk_a
        print(f"[H3时间] 第 {wi + 1}/{len(windows)} 段完成"
              f"（锁定 {lock} 步 / 核心 {e - s - lock} 步）")

    # 输出统一到 video 所在设备：audio 可能还在 cpu（上游显存净化卸回 / 本来没上卡），
    # 若各按各的设备拼回去，输出的 NestedTensor 又是一个 cuda 一个 cpu，下游节点内部
    # 再 cat 一次就又炸同一个错。以 video 设备为准，保证两个张量同设备。
    out_dev = video.device
    out_v = torch.cat(v_parts, dim=2).to(device=out_dev, dtype=video.dtype)
    out_a = torch.cat(a_parts, dim=-1).to(device=out_dev, dtype=audio.dtype)
    del v_parts, a_parts
    if int(out_v.shape[2]) != T:
        raise RuntimeError(f"[H3时间] 拼接后 {out_v.shape[2]} latent 步、原片 {T} 步 —— "
                           "窗口未覆盖整条时间轴，已中止以免输出跳帧")
    return out_v, out_a


# ============================================================
# 主节点（内置放大 + 分块重采样）
# ============================================================
class BanZhang_MMH3_Upscale:
    """终极放大主节点：内置放大（可选）+ 可选的时间分块 / 空间分块重采样。
    视频始终采用重采样结果；音频由「音频重绘强度」控制：0=透传原始音频，1=全部重绘，
    中间值为潜空间线性混合。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "模型": ("MODEL", {"tooltip": "用于重采样每个分块/瓦片的扩散模型。"}),
                "条件": ("CONDITIONING", {"tooltip": "用于生成该潜变量的 conditioning。"}),
                "潜空间": ("LATENT", {"tooltip": "待增强的 MiniMax H3 AV 潜变量。"}),
                # ── 内置 LoRA（二采修复专用）──────────────────────────────
                # 仅在本节点的二采重采样（含预显影）时生效：不影响一采，也不
                # 作用于潜空间放大网络（那是独立网络）。选「🚫 关闭」或强度 0
                # 即不使用；与外部「多 LoRA 管理器」为叠加关系（节点内再打一次补丁）。
                "lora_file": (
                    [BUILTIN_LORA_OFF] + folder_paths.get_filename_list("loras"),
                    {"tooltip": "二采修复专用 LoRA：仅在本节点重采样时生效，不影响一采"
                                "与潜空间放大；选「🚫 关闭」则不使用。可与外部多 LoRA 管理器叠加。"}),
                "lora_strength": ("FLOAT", {"default": 1.0, "min": -10.0, "max": 10.0, "step": 0.05,
                                           "tooltip": "内置 LoRA 强度：0 等同关闭，负数为反向。"}),
                "随机种子": ("INT", {"default": 0, "min": 0, "max": 18446744073709551615,
                                    "control_after_generate": True,
                                    "tooltip": "内置噪声的种子。"}),
                # 默认 euler：逐步同步分块路径只认 sample_euler（见 _sample_spatial_tiled
                # 的判定）。其它采样器会退化成「逐块独立跑完整条 sigma 曲线」，
                # 小瓦片下各块各自为政，容易出现分身小人 / 接缝。
                "采样器": (comfy.samplers.KSampler.SAMPLERS, {"default": "euler"}),
                "调度器": (comfy.samplers.KSampler.SCHEDULERS, {"default": "simple"}),
                "步数": ("INT", {"default": 1, "min": 1, "max": 10000, "step": 1}),
                "去噪强度": ("FLOAT", {"default": 0.2, "min": 0.0, "max": 1.0, "step": 0.01}),
                "音频重绘强度": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.05,
                                     "tooltip": "0=直接透传一采的原始音频（推荐，音色不漂移）；"
                                                "1=音频随视频一起全部重绘（音色/音效可能改变）；"
                                                "中间值=原始音频与重绘音频按此比例混合（如 0.5 各半，更稳）。"}),
            },
            "optional": {
                "sigmas": ("SIGMAS", {"tooltip": "可选外部 sigma 调度，连接后内置调度器/步数/去噪失效。"}),
                "放大参数": (H3_UPSCALE_PARAM, {"tooltip": "「H3 终极放大参数」的放大输出。不连则跳过放大。"}),
                "空间分块参数": (H3_SPATIAL_PARAM, {"tooltip": "「H3 终极放大参数」的空间分块输出。不连则不切分。"}),
                "时间分块参数": (H3_TEMPORAL_PARAM, {"tooltip": "「H3 终极放大参数」的时间分块输出。不连则整条一次性重采样。\n"
                                                              "连上后按 H3 的时间帧网格切成若干段逐段重采样，"
                                                              "显存峰值只与单段长度有关（长片必备）。\n"
                                                              "与「空间分块参数」完全独立：可只连一个，也可两个都连"
                                                              "（先切时间、每段内再切空间）。\n"
                                                              "段与段之间靠「上一段的真实输出锁进下一段开头」接力，"
                                                              "那段只读不重画，所以内容不会漂移、接缝不会重影。"}),
            },
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("潜空间",)
    FUNCTION = "enhance"
    CATEGORY = "👨‍🏫班长/MiniMax H3"

    @classmethod
    def VALIDATE_INPUTS(cls, lora_file=None, **kwargs):
        # 内置 LoRA 文件存在性校验（combo 值可能来自别的机器、或文件已被移动）：
        # 不存在 → 节点红框、队列拦截，与原生 LoRA 加载器体验一致。
        # 其余输入由框架按类型 / 范围做默认校验，**kwargs 统一吞掉。
        if lora_file:
            name = str(lora_file).strip().replace("\\", "/")
            if name != BUILTIN_LORA_OFF and folder_paths.get_full_path("loras", name) is None:
                return (f"内置 LoRA 文件未找到：{name}\n"
                        "请确认已放入 models/loras 目录，子目录层级、文件名（含大小写）一致；"
                        "或在顶部下拉选「🚫 关闭」。")
        return True

    def enhance(self, 潜空间, 条件, 模型, sigmas=None, 随机种子=0, 采样器="sa_solver",
                调度器="simple", 步数=1, 去噪强度=0.2, 音频重绘强度=0.0,
                lora_file=BUILTIN_LORA_OFF, lora_strength=1.0,
                放大参数=None, 空间分块参数=None, 时间分块参数=None):
        # ── 内置 LoRA 注入（必须在一切采样 / load_models_gpu 之前）──────────
        # 这里打一次补丁后，后续预显影、强制全量加载、分块 clone（见下方
        # n_tiles>1 分支）都会自动携带补丁，结尾 unload_model_and_clones 统一
        # 清理；LoRA 只读盘一次，全部空间/时间分块共享同一个 patched model。
        lora_name = str(lora_file or "").strip().replace("\\", "/")
        try:
            lora_strength_f = float(lora_strength)
        except (TypeError, ValueError):
            lora_strength_f = 1.0
        if lora_name and lora_name != BUILTIN_LORA_OFF and lora_strength_f != 0.0:
            lora_path = folder_paths.get_full_path("loras", lora_name)
            if lora_path is None:
                raise FileNotFoundError(
                    f"[H3终极放大] 内置 LoRA 文件未找到：{lora_name}\n"
                    "请确认文件在 models/loras 目录，且子目录层级、文件名（含大小写）"
                    "与选择时一致；或在顶部下拉选「🚫 关闭」。")
            lora_sd = comfy.utils.load_torch_file(lora_path, safe_load=True)
            模型, matched = _apply_lora_to_model(模型, lora_sd, lora_strength_f)
            if matched == 0:
                raise RuntimeError(
                    f"[H3终极放大] 内置 LoRA 无效：{lora_name}\n"
                    "该 LoRA 与当前模型不匹配（如别的模型架构的 LoRA），"
                    "没有任何权重被应用，请换成对应模型的 LoRA。")
            print(f"[H3终极放大] 内置 LoRA 已启用：{lora_name}"
                  f"（强度 {lora_strength_f:.2f}，+{matched} 处补丁，仅作用于二采）")
        else:
            print("[H3终极放大] 内置 LoRA：关闭（不使用）")

        noise = Noise_RandomNoise(int(随机种子))
        sampler = comfy.samplers.sampler_object(采样器)
        if sigmas is None:
            sigmas = _build_sigmas(模型, 调度器, 步数, 去噪强度)

        samples = 潜空间["samples"]
        if not is_h3_av_latent(samples):
            raise ValueError("期望 MiniMax H3 AV 潜变量（嵌套 视频+音频）")
        video, audio = samples.tensors[0], samples.tensors[1]
        if video.shape[0] != 1:
            raise ValueError("仅支持 Batch 1")

        # 1. 内置放大
        # 免模型档标记：按名称前缀判定（⚡ 开头），涵盖下拉里全部免模型插值方式。
        # 预显影对整个免模型档有效。
        is_free_model = False
        h0, w0 = video.shape[3], video.shape[4]
        if 放大参数 is not None:
            is_free_model = _is_free_model(放大参数.get("model_name"))
            video = _run_upscale(video, 放大参数)

        B, C, T, H, W = video.shape
        upscaled = (H, W) != (h0, w0)

        # 1.2 「条件未升级」只说明导演台那份重采样条件与一采逐字相同（面板倍数 ≤ 1、
        # 无参考素材、首尾帧模式），它描述的是**条件**，不是**这次要不要干活**。
        # 画布一旦被放大，重采样就是必经步骤——哪怕条件没重建，也得在新尺寸上
        # 跑一遍，否则放大白做、输出尺寸退回一采。
        # 故只有「条件未升级」且「画布确实没变大」时才真的等于白跑，才直通。
        if not _hd_upgraded(潜空间) and not upscaled:
            print("[H3终极放大] 条件未升级（与一采逐字相同）且画布尺寸未变 —— "
                  "跳过本次重采样，潜空间原样输出")
            return (潜空间,)
        if not _hd_upgraded(潜空间):
            print(f"[H3终极放大] 条件未升级（导演台那份重采样条件与一采逐字相同："
                  f"面板倍数 ≤ 1 / 无参考素材 / 首尾帧模式），但画布已从 "
                  f"{w0}x{h0} 放大到 {W}x{H} —— 照常重采样。注意：参考素材没有按"
                  f"新画布重建，主体一致性可能偏弱；如需一致，把导演台"
                  f"「放大重绘倍数」调成与本节点「放大倍数」相同。")

        # 1.5 预显影：整幅低起始 sigma 快速打磨插值底图，使其含高频、贴近真实 latent
        # 分布，再进干净上下文分块重绘。是否需要取决于所选插值方式：
        #   · 双线性 / 三线性 / 双三次：带 2 格过渡带 → 建议 1~2 步磨掉；
        #   · 最近邻 / 最近邻精确 / 区域平均：无过渡带 → 可选，纯当轻度打磨。
        # 由「预显影步数」控制（0=关闭）。真模型档底图本就有高频，直接跳过。
        if is_free_model and 放大参数 is not None:
            pre_steps = max(0, int(放大参数.get("pre_steps", 0)))
            if pre_steps > 0:
                pre_denoise = float(放大参数.get("pre_denoise", 0.4))
                pre_sigmas = _build_sigmas(模型, 调度器, pre_steps, pre_denoise)
                # 关键帧适配：与正式分块采样保持一致——低清关键帧（尺寸≠画布）停止硬注入，
                # 否则预显影阶段会被低清锚点拖影/发糊，等于把污染带进打磨后的底图。
                # 预显影是整幅，无需 has_hard_kf（那是分块选路的依据）。
                pre_cond, _ = _adapt_keyframes_cond(条件, H, W)
                # 设备统一：放大后 video 在 cuda，而 audio 可能仍在 cpu（例如前面接了
                # 显存净化把张量卸回 cpu）。NestedTensor 打包后 guider.sample 内部的
                # pack_latents 会 torch.cat 跨设备张量 → 报 "tensors is on cpu, different
                # from other tensors on cuda"。故与正式分块路径一致，先把 video/audio
                # 及对应噪声统一到推理设备（正式路径在 _sample_spatial_tiled 里同样处理）。
                pre_dev = comfy.model_management.get_torch_device()
                pre_noise = noise.generate_noise({"samples": torch.zeros_like(video, dtype=torch.float32)})
                pre_piece = {"samples": comfy.nested_tensor.NestedTensor(
                    (video.to(pre_dev), audio.to(pre_dev)))}
                pre_noise_nested = comfy.nested_tensor.NestedTensor(
                    (pre_noise.to(pre_dev),
                     noise.generate_noise({"samples": torch.zeros_like(audio, dtype=torch.float32)}).to(pre_dev)))
                print(f"[H3预显影] 免模型档整幅预显影：{pre_steps} 步、起始强度 {pre_denoise:.2f}（低强度打磨插值底图）")
                pre_out = sample_piece(pre_piece, pre_cond, 模型, pre_noise_nested, noise.seed,
                                       sampler, pre_sigmas)
                if getattr(pre_out, "is_nested", False):
                    video = pre_out.tensors[0].to(video.device, video.dtype)
                else:
                    video = pre_out.to(video.device, video.dtype)
                B, C, T, H, W = video.shape

        # 2. 分块规划：时间分块（外层，可选）与空间分块（内层，可选）彼此独立。
        #    两者都连时先切时间、每段内再切空间；只连一个就只切那一维。
        #    注意：放大与预显影都在切块之前按整条完成——放大器是 3D 的，时间感受野
        #    比单个时间块还长，按块放大会让每块边缘都被填充污染。
        temporal_windows = None
        if 时间分块参数 is not None:
            temporal_windows, _ov = _h3_temporal_windows(
                T, 时间分块参数.get("chunk_frames", 73),
                时间分块参数.get("overlap_frames", 22))
            if len(temporal_windows) <= 1:
                print("[H3时间] 全片短于一个时间块 —— 不切时间块，整条一次性采样")
                temporal_windows = None
        chunk_t = (temporal_windows[0][1] - temporal_windows[0][0]) if temporal_windows else T
        if temporal_windows:
            print(f"[H3时间] {len(temporal_windows)} 个时间块 × 每块 {chunk_t} latent 步"
                  f"（总 {T} 步）；块间靠上一段真实输出锁定接力")
            if float(去噪强度) >= 0.99:
                print("[H3时间] ⚠ 去噪强度=1.00（一采 / 从零生成）时不建议用时间分块：\n"
                      "        每段的核心区都是从噪声重新生成，而块内时间相位都从片头算起，\n"
                      "        模型容易每段都生成相似的「开头运动」→ 画面按块循环（像 GIF 循环）。\n"
                      "        长片一采请改用导演台的「潜空间续写」（专为段间衔接设计）；\n"
                      "        或把「每块帧数」加到超过总帧数（= 退化成不切时间块）。")

        # 空间分块（长边单轴，对齐第三方）
        n_tiles, tile_overlap = 1, 8
        if 空间分块参数 is not None:
            sp = 空间分块参数
            tile_px = sp.get("tw", sp.get("th", 40))
            long_edge = max(H, W)
            # 预设给出的块数作下限（不再硬顶 8：显存兜底可能需要切更多）
            n_tiles = max(2, int(round(long_edge / max(1, tile_px))))
            tile_overlap = max(0, min(32, int(sp.get("ol_w", sp.get("ol_h", 8)))))
            if long_edge <= sp.get("mt", 24):
                n_tiles = 1
            else:
                # 预设是静态查表（按显卡档位），不反映本次运行的实际占用。
                # 测一次真实空闲显存，不够就往上加切——小显存卡的唯一补救。
                # 时间分块时按单块长度估（显存只与同时进模型的那一段有关）。
                n_tiles = _fit_n_tiles_to_free_vram(
                    n_tiles, H, W, chunk_t, comfy.model_management.get_torch_device(),
                    model_patcher=模型, overlap=tile_overlap)

        # 「一个人说话变成好几个人」的分身，根因就在这里：每一块拿到的是**完整提示词**，
        # 而它自己只是画面的一个小条带。「一个人在说话」这个语义对每块都成立，于是每块
        # 各自生成了一个人。干净上下文（边带钉底图）只在 overlap 那一圈有效，核心区仍是
        # 自由生成；去噪强度越高（越接近从噪声重来），核心区越不受底图约束，分身越明显。
        # 真正能压住的是图像锚：接了首帧参考图（keyframe）后分块会按块裁切，每块只看到
        # 自己那一段画面（见 _sample_spatial_tiled 的 has_hard_kf 分支）。
        if n_tiles > 1 and float(去噪强度) >= 0.99:
            print("[H3空间] ⚠ 去噪强度=1.00（一采 / 从零生成）且已开启空间分块：\n"
                  "        每块只看得见自己那一条，却都面对完整的提示词 ——\n"
                  "        「一个人在说话」会被每块各生成一遍，出来就是好几个人在说话。\n"
                  "        建议按这个顺序试：\n"
                  "        ① 先用整幅一采（本节点不连「空间分块参数」）出小图，\n"
                  "           再用本节点低去噪强度（0.1~0.2）分块放大重绘；\n"
                  "        ② 接上首帧参考图（keyframe），分块会自动按块裁切，\n"
                  "           每块只看到自己那段画面（要求与画布同网格）；\n"
                  "        ③ 显存允许时把「显存预设」往上调一档：瓦片更大 → 块更少。")

        noise_v = noise.generate_noise({"samples": torch.zeros_like(video, dtype=torch.float32)})
        noise_a = noise.generate_noise({"samples": torch.zeros_like(audio, dtype=torch.float32)})

        # ── 采样前把主模型完整加载进显存，并把加载比例打出来 ─────────────────
        # 背景：ComfyUI 加载模型时会按它自己对激活需求的估算决定「全量加载」还是
        # 「部分加载（lowvram，权重留在内存、forward 逐层搬上卡）」。估算偏保守时
        # 走部分加载 —— 表现恰好是：显存读数偏低、同一工作流耗时在 1 分钟与 1 小时
        # 之间随机跳变（取决于加载瞬间的空闲显存，见三采耗时异常报告：10.8 GB
        # 的读数 + 同形状块耗时差 3.7 倍 + 49 秒与 51 分钟反转）。
        # 显式 force_full_load 消除这个抖动；真放不下时 ComfyUI 仍会自动降级，不会崩。
        try:
            comfy.model_management.load_models_gpu([模型], force_full_load=True)
            _ms, _ls = int(模型.model_size()), int(模型.loaded_size())
            if _ls >= _ms:
                print(f"[H3终极放大] 模型已全量进显存：{_fmt_mem(_ls)} / {_fmt_mem(_ms)} ✓")
            else:
                print(f"[H3终极放大] ⚠ 模型只进了 {_fmt_mem(_ls)} / {_fmt_mem(_ms)}"
                      f"（{_ls * 100 // max(1, _ms)}%）—— 显存放不下，forward 会逐层搬权重，"
                      f"耗时会成倍上涨。请降低画布/倍数，或换更大显存。")
        except Exception as exc:
            print(f"[H3终极放大] ⚠ 预加载主模型失败（{exc}），交由采样器自行加载")

        # 分块开启（n_tiles>1）时，给模型挂全局坐标注入 wrapper：让每个 tile 的 video
        # 段拿到整幅坐标系下的真实位置（防分身/防接缝）。clone 避免污染原模型，
        # 后续本节点卸载模型时按 clone 处理。
        if n_tiles > 1:
            模型 = 模型.clone()
            _install_tile_pos(模型)

        if temporal_windows is None:
            out_v, out_a = _sample_spatial_tiled(
                video, audio, 条件, 模型, noise.seed, sampler, sigmas,
                noise_v, noise_a,
                n_tiles=n_tiles, overlap=tile_overlap,
            )
        else:
            out_v, out_a = _sample_temporal_windows(
                video, audio, 条件, 模型, noise.seed, sampler, sigmas,
                noise_v, noise_a, temporal_windows,
                n_tiles=n_tiles, overlap=tile_overlap,
            )
        # 音频重绘强度：0=直接透传原始音频；1=全部重绘；中间值潜空间线性混合
        w = max(0.0, min(1.0, float(音频重绘强度)))
        if w <= 0.0 or out_a is None:
            print("[H3混] 音频重绘强度：0.00（直接透传一采原始音频）")
            out_a = audio
        else:
            if w < 1.0:
                print(f"[H3混] 音频重绘强度：{w:.2f}（原始音频与重绘音频混合）")
                # 潜空间线性混合：强度即"原始音频 ←→ 重绘音频"的比例（先对齐设备）
                out_a = audio.to(device=out_a.device, dtype=out_a.dtype).lerp(out_a, w)
        # 输出设备统一：out_v / out_a 必须落在同一设备，否则下游节点（VAE 解码、保存、
        # 再打包 NestedTensor）内部一旦 torch.cat 两个张量就又跨设备报错。以放大后
        # video 所在设备为准（与 sample_piece 收尾的 intermediate_device 惯例一致）。
        out_dev = video.device
        out_v = out_v.to(device=out_dev)
        out_a = out_a.to(device=out_dev)

        if hasattr(模型, "clone_base_uuid"):
            comfy.model_management.unload_model_and_clones(模型, unload_additional_models=False)
            comfy.model_management.soft_empty_cache()
        return ({"samples": comfy.nested_tensor.NestedTensor((out_v, out_a))},)


NODE_CLASS_MAPPINGS = {
    "BanZhang_MMH3_UpscaleParams": BanZhang_MMH3_UpscaleParams,
    "BanZhang_MMH3_Upscale": BanZhang_MMH3_Upscale,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhang_MMH3_UpscaleParams": "🎬 H3 终极放大参数",
    "BanZhang_MMH3_Upscale": "🎬 H3 终极放大",
}