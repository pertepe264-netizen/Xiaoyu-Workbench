# -*- coding: utf-8 -*-
"""✍️ H3 文案台 —— 提示词的独立节点。

为什么独立出去：导演台那块面板已经装了画布、参考卡、成片监视器、分段条，
提示词挤在最后面，写长剧本要一路往下滚。拆出来之后导演台只管「怎么生成」，
文案台只管「说什么」。

链路是 **媒体架 → 文案台 → 导演台**：

    媒体架 ──参考素材──> 文案台 ──参考素材──> 导演台
                           └────提示词────────┘

参考素材是**透传**的：文案台自己不编码素材（那是导演台的活），它只是需要
看一眼槽位，好把 @图片1 / @视频2 / @音频1 这些标签摆成 chips 让用户点。
透传而不是让导演台自己接一根线，是为了让「标签里看到的」和「实际送进去的」
永远是同一份——两头各接一次，素材架换一次位就可能不一致。

段身份由**前端**写进 state，不靠执行顺序推导：文案台排在导演台前面执行，
那时 chain.json 里的 current 还是上一段，靠后端反查必然认错段。前端直接读
导演台节点上的工程快照（同一个 gcast_project），拿到当前分段的 uid。
"""

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from . import csglide_chain as chain
except Exception:                                    # 顶层导入（自检）
    import csglide_chain as chain                     # noqa: E402

# 提示词里 @标签 的规范写法，与 csglide_cast.py 保持一致（那里是权威定义）。
# 这里只做「本段/片子级拼接」与归档，不改写法规则。
_GLUE_RE = re.compile(r"(@(?:first|last|图片\d+|视频音频\d+|视频\d+|音频\d+))(?=@)")


def _clean(t):
    return re.sub(r"[ \t]+\n", "\n", str(t or "").replace("\r\n", "\n")).strip()


def compose_text(global_text, seg_text, sep="\n"):
    """片子级 + 本段 = 最终提示词。

    片子级在**前**：它是全片统一的风格/基调，本段是这一段要发生的事。
    反过来写会让每段的开头都在重复世界观，模型的注意力被拉到固定前缀上，
    段与段的差别被稀释。
    """
    parts = [_clean(global_text), _clean(seg_text)]
    parts = [p for p in parts if p]
    out = sep.join(parts)
    return _GLUE_RE.sub(r"\1 ", out)


class CSGlideDeskCS:
    """✍️ H3 文案台 —— 写提示词的地方，接在媒体架与导演台之间。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 前端把整份 state 写进这个 widget（多行会让 v2 前端渲染成
                # 浮在节点上的 textarea，所以与导演台 h3_data 一样走单行
                # STRING + 前端隐藏）。
                "desk_data": ("STRING", {"default": ""}),
            },
            "optional": {
                "参考素材": ("H3_REFS", {
                    "tooltip": "接 H3 媒体架的「参考素材」。只看不编码："
                               "拿它把 @图片N / @视频N / @音频N 摆成标签，"
                               "点一下就插进提示词。"}),
                "外接提示词": ("STRING", {
                    "forceInput": True,
                    "tooltip": "接线即整体替换本节点的输出（给外部文本源用）。"
                               "留空才用面板里写的词。"}),
                "片名": ("STRING", {
                    "default": "",
                    "tooltip": "留空 = 跟随导演台（用导演台工程面板里的工程名）。"}),
                "段落": ("STRING", {
                    "default": "",
                    "tooltip": "段落 id。留空 = 跟随导演台当前分段。"}),
            },
        }

    RETURN_TYPES = ("H3_REFS", "STRING")
    RETURN_NAMES = ("参考素材", "提示词")
    FUNCTION = "compose"
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = ("H3 文案台——提示词的独立编辑节点。接在媒体架与导演台之间："
                   "参考素材原样透传给导演台，提示词输出给导演台的「外接提示词」。")

    def compose(self, 参考素材=None, 外接提示词=None, 片名="", 段落="",
                desk_data=""):
        st = {}
        try:
            st = json.loads(desk_data) if desk_data else {}
        except Exception:
            st = {}
        if not isinstance(st, dict):
            st = {}

        # 片名的认法：显式参数 → 面板 state 里跟来的（前端从导演台读的）
        # → 最近登记过的片子。三条都不成才不归档。
        name_hint = (片名 or "").strip() or ((st.get("chain") or {}).get("name") or "")

        # 外接优先：接了线就是外部文本源说了算，面板里的词不参与。
        if 外接提示词 is not None:
            text = str(外接提示词)
            print("[H3 文案台] 外接模式：使用外部文本（%d 字）" % len(text))
            self._archive(name_hint, 段落, text)
            return (参考素材, text)

        g = st.get("global") or ""
        segs = st.get("segs") if isinstance(st.get("segs"), dict) else {}

        # 认段：显式段落 id → 面板记下的段身份 → 留空（只出片子级）
        cid = (段落 or "").strip() or (st.get("chain") or {}).get("clip") or ""
        shot = (st.get("chain") or {}).get("shot") or ""
        seg = segs.get(cid) if cid else ""
        if not seg and shot:
            # 段身份兜底：clip id 可能还没回传（新段第一次跑），按 shot 找
            for k, v in segs.items():
                if k == shot:
                    seg = v
                    break

        text = compose_text(g, seg)
        print("[H3 文案台] 片子级 %d 字 + 本段 %d 字 → %d 字%s"
              % (len(_clean(g)), len(_clean(seg)), len(text),
                 ("（段 %s）" % cid) if cid else "（未认段，只出片子级）"))

        self._archive(name_hint, 段落 or cid, text)
        return (参考素材, text)

    @staticmethod
    def _archive(片名, cid, text):
        """把最终文本写进段落档案 —— 时间轴与重抽都靠它回显。

        写档失败绝不能挡出图：文案台的活是「说出文本」，归档只是顺手。
        """
        name = (片名 or "").strip()
        if not name:
            try:
                name = chain.last_name()
            except Exception:
                name = ""
        if not name or not cid:
            return
        try:
            if chain.load_clip(name, cid) is None:
                return
            chain.update_clip(name, cid, {"prompt_seg": text})
            print("[H3 文案台] 已归档到 %s / %s" % (name, cid))
        except Exception as e:
            print("[H3 文案台] 归档失败（不影响出图）: %s: %s"
                  % (type(e).__name__, e))


NODE_CLASS_MAPPINGS = {"CSGlideDeskCS": CSGlideDeskCS}
NODE_DISPLAY_NAME_MAPPINGS = {"CSGlideDeskCS": "✍️ H3 文案台 (H3 Desk)"}
