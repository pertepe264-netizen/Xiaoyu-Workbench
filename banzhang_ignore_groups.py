# -*- coding: utf-8 -*-
"""
班长全家桶 — 忽略多组 (Ignore Groups)
合并自 Goohaitools-comfyui 的忽略多组孤海，去除孤海品牌前缀。
前端 js/banzhang_ignore_groups.js 负责编组开关列表 UI 与旁路/禁用控制。
"""


class BanZhangIgnoreGroups:
    """忽略多组 — 通过开关控制工作流中各编组的忽略/旁路状态"""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ()
    FUNCTION = "execute"
    CATEGORY = "👨‍🏫班长/工具与标注"
    OUTPUT_NODE = True

    def execute(self):
        return ()


NODE_CLASS_MAPPINGS = {
    "BanZhang_IgnoreGroups": BanZhangIgnoreGroups,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhang_IgnoreGroups": "忽略多组",
}
