"""MiniMax H3 语义桥：蒸馏适配器的加载与条件混合。

上游（MiniMax_H3_Semantic_Bridge v1.0 research release）是个独立节点包，本插件只
用它的加载器与数学，因此内联成模块——节点类、下拉列表、NODE_CLASS_MAPPINGS 都是
给独立安装准备的，本插件既没注册它们也用不到。

适配器放这里（跟着插件走，插件目录可整体搬移，换机器/换 ComfyUI 安装位置都不会
找不到）：

    ComfyUI-Banzhang-All/semantic_bridge/*.safetensors

数值口径与上游逐字一致：条件张量按 [B, T, 5120] 断言 → 每 token 做 RMS 归一化 →
过三层 MLP（5120→512→512→5120，SiLU）→ 与原生条件做幅值对齐 → 按 α 插值混合。
miniMax H3 自身的权重不被修改，改动只发生在喂给它的条件上。
"""

import os

import torch
import torch.nn as nn
from safetensors.torch import load_file


ADAPTER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic_bridge")
os.makedirs(ADAPTER_DIR, exist_ok=True)

# 默认适配器文件名。导演台不暴露模型选择，恒用这一个；就绪状态接口也以此为准。
DEFAULT_ADAPTER = "MiniMaxH3_SemanticBridge_v1.safetensors"

_STUDENT_CACHE = {}


def adapter_path(name):
    path = os.path.join(ADAPTER_DIR, name)
    if os.path.isfile(path):
        return path
    raise FileNotFoundError(
        "Distilled adapter was not found.\n\n"
        f"Selected: {name}\nExpected folder:\n{ADAPTER_DIR}\n"
    )


def sanitize_adapter_name(name):
    """把外部传来的适配器名约束成本目录内的纯文件名。

    只取 basename（挡住路径穿越），非 .safetensors 或文件不存在时回退默认——
    「选了不存在的模型」不该炸一次渲染，回退默认并在前端就绪位提示即可。
    """
    base = os.path.basename(str(name or "").strip())
    if not base or not base.lower().endswith(".safetensors"):
        return DEFAULT_ADAPTER
    try:
        adapter_path(base)
    except FileNotFoundError:
        return DEFAULT_ADAPTER
    return base


def list_adapters():
    """semantic_bridge/ 目录下的全部 .safetensors，按名字排序。"""
    try:
        names = [f for f in os.listdir(ADAPTER_DIR)
                 if f.lower().endswith(".safetensors")
                 and os.path.isfile(os.path.join(ADAPTER_DIR, f))]
    except OSError:
        return []
    return sorted(names)


def text_token_mask(conditioning):
    """构造「仅文本」掩码：True=文本 token，False=视觉块。

    H3 的条件 metadata 带 minimax_token_tags（tag=0 = 视觉块，含两侧
    <|vision_start|>/<|vision_end|>；tag=1 = 文本）。掩码版要动的是「语义」——
    文本（提示词 + <Picture i>/<Audio j>/<Video k> 锚点）应一起平移、保持一致；
    视觉块是画面内容、不在语义蒸馏的射程内，应原样保留。

    这是对旧「仅尾部提示词」掩码的修正：旧版按序列位置切，尾部提示词里出现的
    <Picture i> 引用与头部同名锚点会被移到两个不同子空间，破坏图文对齐（已实测
    使画面逻辑变差）；按 tag 切则所有文本 token 走同一个变换，一致性天然成立。

    取不到 tags 或长度对不上时返回 None，调用方退回全量。
    """
    if not conditioning:
        return None
    item = conditioning[0]
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None
    native, metadata = item[0], item[1]
    tags = metadata.get("minimax_token_tags") if isinstance(metadata, dict) else None
    if tags is None:
        return None
    try:
        if not isinstance(tags, torch.Tensor):
            tags = torch.as_tensor(tags)
        tags = tags.reshape(-1)
        T = int(native.shape[1]) if getattr(native, "ndim", 0) >= 2 else -1
        if T < 0 or tags.numel() != T:
            return None
        return (tags == 1).to(dtype=torch.bool)
    except Exception:
        return None


def _rms_normalize(x):
    x = x.float()
    rms = torch.sqrt(x.pow(2).mean(dim=-1, keepdim=True) + 1e-6)
    return x / rms


def _magnitude_match(source, target):
    source = source.float()
    target = target.float()
    source_rms = torch.sqrt(source.pow(2).mean(dim=-1, keepdim=True) + 1e-8)
    target_rms = torch.sqrt(target.pow(2).mean(dim=-1, keepdim=True) + 1e-8)
    return source * (target_rms / source_rms)


class SemanticStudent(nn.Module):
    """v1 结构：三层 MLP（5120→512→512→5120，SiLU）。"""

    def __init__(self, input_dim=5120, hidden_dim=512, output_dim=5120):
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim, bias=True)
        self.fc2 = nn.Linear(hidden_dim, hidden_dim, bias=True)
        self.fc3 = nn.Linear(hidden_dim, output_dim, bias=True)
        self.act = nn.SiLU()

    def forward(self, x):
        x = self.act(self.fc1(x))
        x = self.act(self.fc2(x))
        return self.fc3(x)


class SemanticStudentTransformer(nn.Module):
    """T8 结构：输入投影 5120→256 + N 层 self-attention 编码器 + 输出投影 256→5120。

    与 Hermes 的 trans 加载器同构：in_proj / 每层 linear1+linear2+norm1+norm2+
    self_attn(in/out_proj) / 末尾 norm / out_proj。激活函数上游用 ReLU（linear1
    后接、linear2 前接，是标准 transformer-encoder FFN 布局），这里对齐。
    """

    def __init__(self, d_model=256, nhead=4, num_layers=2, dim_feedforward=1024):
        super().__init__()
        self.in_proj = nn.Linear(5120, d_model, bias=True)
        layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=dim_feedforward,
            dropout=0.0, activation="relu", batch_first=True,
        )
        self.encoder = nn.TransformerEncoder(layer, num_layers=num_layers)
        self.norm = nn.LayerNorm(d_model)
        self.out_proj = nn.Linear(d_model, 5120, bias=True)

    def forward(self, x):
        # x: [B, T, 5120] —— batch_first，编码器按序列维做注意力
        x = self.in_proj(x)
        x = self.encoder(x)
        x = self.norm(x)
        return self.out_proj(x)


def _load_student(adapter_name, device):
    path = adapter_path(adapter_name)
    key = (os.path.abspath(path), str(device))
    cached = _STUDENT_CACHE.get(key)
    if cached is not None:
        return cached

    weights = load_file(path, device="cpu")
    names = set(weights.keys())

    # 结构判别：v1 = 三层 MLP；T8 = net.encoder.* 的 transformer 编码器。
    if "fc1.weight" in names:
        model = _load_mlp(weights)
    elif "net.in_proj.weight" in names:
        model = _load_transformer(weights)
    else:
        raise RuntimeError(
            "Invalid distilled adapter: unknown tensor layout. "
            f"Keys found: {sorted(names)[:8]} ..."
        )

    model = model.to(device=device, dtype=torch.float32)
    model.eval()
    _STUDENT_CACHE[key] = model
    print(f"[MiniMax H3 Semantic Bridge] Loaded adapter: {adapter_name}")
    return model


def _load_mlp(weights):
    required = ["fc1.weight", "fc1.bias", "fc2.weight", "fc2.bias",
                "fc3.weight", "fc3.bias"]
    missing = [k for k in required if k not in weights]
    if missing:
        raise RuntimeError(f"Invalid distilled adapter. Missing tensors: {missing}")
    for k, want in (("fc1.weight", (512, 5120)), ("fc2.weight", (512, 512)),
                    ("fc3.weight", (5120, 512))):
        if tuple(weights[k].shape) != want:
            raise RuntimeError(f"Unexpected {k} shape: {tuple(weights[k].shape)}")

    model = SemanticStudent()
    with torch.no_grad():
        for k in required:
            getattr_from_path(model, k).copy_(weights[k].float())
    return model


def _load_transformer(weights):
    required = ["net.in_proj.weight", "net.in_proj.bias",
                "net.norm.weight", "net.norm.bias",
                "net.out_proj.weight", "net.out_proj.bias"]
    missing = [k for k in required if k not in weights]
    if missing:
        raise RuntimeError(f"Invalid distilled adapter. Missing tensors: {missing}")

    # 从权重反推层数与维度，避免写死 num_layers/dim
    d_model = int(weights["net.in_proj.weight"].shape[0])          # 256
    layer_idx = 0
    while f"net.encoder.layers.{layer_idx}.self_attn.in_proj_weight" in weights:
        layer_idx += 1
    num_layers = layer_idx                                          # 2
    dim_ff = int(weights["net.encoder.layers.0.linear1.weight"].shape[0])  # 1024
    # nhead 反推：in_proj_weight 是 3*d_model × d_model（Q/K/V 拼接）
    head_dim = 64
    nhead = d_model // head_dim                                     # 4

    model = SemanticStudentTransformer(
        d_model=d_model, nhead=nhead, num_layers=num_layers, dim_feedforward=dim_ff)

    with torch.no_grad():
        model.in_proj.weight.copy_(weights["net.in_proj.weight"].float())
        model.in_proj.bias.copy_(weights["net.in_proj.bias"].float())
        model.norm.weight.copy_(weights["net.norm.weight"].float())
        model.norm.bias.copy_(weights["net.norm.bias"].float())
        model.out_proj.weight.copy_(weights["net.out_proj.weight"].float())
        model.out_proj.bias.copy_(weights["net.out_proj.bias"].float())
        for li in range(num_layers):
            layer = model.encoder.layers[li]
            p = f"net.encoder.layers.{li}"
            layer.self_attn.in_proj_weight.copy_(weights[p + ".self_attn.in_proj_weight"].float())
            layer.self_attn.in_proj_bias.copy_(weights[p + ".self_attn.in_proj_bias"].float())
            layer.self_attn.out_proj.weight.copy_(weights[p + ".self_attn.out_proj.weight"].float())
            layer.self_attn.out_proj.bias.copy_(weights[p + ".self_attn.out_proj.bias"].float())
            layer.linear1.weight.copy_(weights[p + ".linear1.weight"].float())
            layer.linear1.bias.copy_(weights[p + ".linear1.bias"].float())
            layer.linear2.weight.copy_(weights[p + ".linear2.weight"].float())
            layer.linear2.bias.copy_(weights[p + ".linear2.bias"].float())
            layer.norm1.weight.copy_(weights[p + ".norm1.weight"].float())
            layer.norm1.bias.copy_(weights[p + ".norm1.bias"].float())
            layer.norm2.weight.copy_(weights[p + ".norm2.weight"].float())
            layer.norm2.bias.copy_(weights[p + ".norm2.bias"].float())
    return model


def getattr_from_path(obj, dotted):
    """按点路径取属性：getattr_from_path(model, "fc1.weight") == model.fc1.weight"""
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def apply_distilled_bridge(conditioning, adapter_name, alpha, magnitude_match, mask=None):
    """把蒸馏语义变换应用到 conditioning。

    mask 为可选的位置掩码，形状 [T]（bool，True=应用变换）。传 None 时对整段
    序列应用。传掩码时，掩码外的位置原样保留——用于只动「纯提示词」、护住参考
    锚点与视觉块的精准模式。
    """
    result = []

    for item in conditioning:
        if len(item) != 2:
            raise RuntimeError("Unexpected ComfyUI CONDITIONING structure.")

        native = item[0]
        metadata = item[1]

        if native.ndim != 3 or native.shape[-1] != 5120:
            raise RuntimeError(
                "Expected MiniMax H3 conditioning [B,T,5120], "
                f"got {tuple(native.shape)}"
            )

        student = _load_student(adapter_name, native.device)
        h = native.float()
        x = _rms_normalize(h)

        with torch.inference_mode():
            projected = student(x)

        if magnitude_match == "per_token":
            projected = _magnitude_match(projected, h)
        elif magnitude_match == "global":
            source_rms = torch.sqrt(projected.pow(2).mean() + 1e-8)
            target_rms = torch.sqrt(h.pow(2).mean() + 1e-8)
            projected = projected * (target_rms / source_rms)

        hybrid = h + float(alpha) * (projected - h)
        if mask is not None:
            m = mask.to(h.device).reshape(-1)
            # 长度对不上（理论上 text_token_mask 已校验，这里兜底）→ 按全量处理。
            if m.numel() == h.shape[1]:
                hybrid = torch.where(m.view(1, -1, 1), hybrid, h)
        hybrid = hybrid.to(dtype=native.dtype)

        new_metadata = dict(metadata)
        new_metadata["sensenova_h3_distilled"] = True
        new_metadata["sensenova_h3_distilled_alpha"] = float(alpha)
        new_metadata["sensenova_h3_distilled_mode"] = magnitude_match
        new_metadata["sensenova_h3_distilled_mask"] = "full" if mask is None else "text"
        new_metadata["sensenova_h3_distilled_adapter"] = adapter_name

        result.append([hybrid, new_metadata])

    return result
