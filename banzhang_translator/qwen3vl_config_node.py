# -*- coding: utf-8 -*-
import re
from .banzhang_utils import MODEL_CONFIGS, Quantization, ModelType, list_gguf_models

class Qwen3VL_ModelConfig:
    @classmethod
    def INPUT_TYPES(cls):
        model_names = [name for name in MODEL_CONFIGS.keys() if not name.startswith('_')]
        default_model = "Qwen3-VL-4B-Instruct" if "Qwen3-VL-4B-Instruct" in model_names else (model_names[0] if model_names else "None")
        
        gguf_models = list_gguf_models()
        default_gguf = gguf_models[0] if gguf_models else "None"
        
        return {
            "required": {
                "🔧 模型类型": (ModelType.get_values(), {"default": ModelType.HF.value}),
                "🤖 模型选择 (HF)": (model_names or ["None"], {"default": default_model}),
                "🤖 模型文件 (GGUF)": (gguf_models, {"default": default_gguf}),
                "⚙️ 量化级别 (HF)": (list(Quantization.get_values()), {"default": Quantization.NONE}),
                "🔄 保持模型加载": ("BOOLEAN", {"default": True}),
            },
            "optional": {
                # 上下文预设：标签开头数字即真实 n_ctx，后面是场景说明，避免盲填
                "📝 上下文长度 (GGUF)": (
                    ["8192 · 省显存｜单图打标",
                     "16384 · 多图/短片段",
                     "32768 · 日常推荐｜短视频",
                     "65536 · 长视频/多帧｜27B稳妥最大",
                     "98304 · 极限｜仅2B/8B小模型"],
                    {"default": "32768 · 日常推荐｜短视频"}
                ),
            }

        }
    RETURN_TYPES = ("QWEN3VL_MODEL_CONFIG",)
    RETURN_NAMES = ("🔧 本地Qwen配置",)
    FUNCTION = "get_config"
    CATEGORY = "👨‍🏫班长/AI接口"
    
    def get_config(self, **kwargs):
        model_type = kwargs.get("🔧 模型类型")
        config = {
            "model_type": model_type,
            "keep_loaded": kwargs.get("🔄 保持模型加载", False)
        }
        
        if model_type == ModelType.HF.value:
            config["model_name"] = kwargs.get("🤖 模型选择 (HF)")
            config["quantization"] = kwargs.get("⚙️ 量化级别 (HF)")
        else:  # GGUF
            config["model_name"] = kwargs.get("🤖 模型文件 (GGUF)")
            config["gpu_layers"] = -1  # 硬编码为-1，使用所有可用GPU层
            # 解析预设标签：取开头数字（如 "32768 · 日常推荐" → 32768）
            raw_ctx = str(kwargs.get("📝 上下文长度 (GGUF)", "8192"))
            m_ctx = re.match(r"\s*(\d+)", raw_ctx)
            config["context_length"] = int(m_ctx.group(1)) if m_ctx else 8192
        
        return (config,)