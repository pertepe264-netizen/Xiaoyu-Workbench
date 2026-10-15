# -*- coding: utf-8 -*-
"""
班长全家桶 — 万能滑条 (Universal Slider)
合并自 Goohaitools-comfyui 的孤海万能滑条，去除孤海品牌前缀。
前端 js/banzhang_universal_slider.js 负责 Canvas 滑条绘制与设置面板。
"""


class AnyType(str):
    def __ne__(self, __value: object) -> bool:
        return False


any_type = AnyType("*")


class BanZhangUniversalSlider:

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "值": ("FLOAT", {
                    "default": 0.75,
                    "min": -999999,
                    "max": 999999,
                    "step": 0.01,
                    "display": "slider",
                }),
            },
            "hidden": {
                "output_type": (["float", "int"], {"default": "float"}),
            },
        }

    RETURN_TYPES = (any_type,)
    RETURN_NAMES = ("*",)
    FUNCTION = "execute"
    CATEGORY = "👨‍🏫班长/工具与标注"

    def execute(self, 值, output_type="float"):
        processed_value = round(值, 10)
        if output_type == "int":
            return (int(round(processed_value)),)
        else:
            return (float(processed_value),)

    @classmethod
    def IS_CHANGED(cls, 值, output_type="float"):
        processed_value = round(float(值), 10)
        if output_type == "int":
            return int(round(processed_value))
        return processed_value


# ── 节点注册映射 ─────────────────────────────────
NODE_CLASS_MAPPINGS = {
    "BanZhang_UniversalSlider": BanZhangUniversalSlider,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhang_UniversalSlider": "万能滑条",
}
