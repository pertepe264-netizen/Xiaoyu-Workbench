# -*- coding: utf-8 -*-
"""💾 H3 续拍存档 · 段落档案镜像 + 顺序锚口（注册层包装）

背景
----
「续拍存档」（上游类 `MiniMaxH3MotionContextSaveLatent`）只把潜空间写成
    output/h3_context/clip_%05d_.safetensors
这喂的是**潜空间续写**：导演台执行时自己去 h3_context/ 捞最新一份，所以
「接着上一段往下拍」是通的。

但导演台的「工程」面板、段落缩略图、重抽替换要的是**段落档案**：
    output/h3_chain/<片名>/clip_000N.latent.safetensors
两者是两个落点。只接续拍存档时 → 续写通、段落档案永远是空的，
面板显示「无潜空间」、重抽没有旧帧可钉。用户看到的就是「路径没统一」。

这个包装层把两者接上：续拍存档照旧写 h3_context（续写不受影响），
同一份潜空间**再落一份**进导演台的段落档案并登记 chain.json。片名与段号
直接读导演台刚登记的那一份 —— 用户不用手写，工作流也不用改。

顺带补上「顺序锚」输入口：与 📦 段落存档 / 📤 段落调取 同一根插销。
本节点的输入来自采样器，本来就在导演台下游、顺序天然确定，这个口只是
让三个节点长得一样，接线时不用想。

同时撤掉两个「只有一个正确取值」的控件：「保存路径」与「本片段编号」。
前者改了会让导演台扫不到续写来源（而文件照样写成功，静默失效），
后者在自动模式下本来就该恒为 0。撤掉之后节点只剩「潜空间」一个输入口
和「顺序锚」一根插销，面板上再没有会骗人的旋钮。

改动位置说明
------------
全部在注册层完成，`_h3mc/` 上游一行不动 —— 该目录升级时整个覆盖，
写进去等于每次都要重新打补丁（与 banzhang_h3mc_zh / _status 同一套路）。
"""

_NODE_ID = "MiniMaxH3MotionContextSaveLatent"

# 存档固定落在这里。导演台的「潜空间续写」就是扫 output/h3_context/ 里 mtime
# 最新的一个文件（csglide_cast._latest_cont_latent_path），而它认的正则是
# _h3mc._CHAIN_SLOT_FILE = (?:_\d{5}|_\d{5}_|_clip\d{3})\.safetensors$
# —— 自动编号（带尾下划线）与固定槽位两种命名都认。
# 所以这个值不能交给用户改：改错了不会报错，文件照样写成功，只是导演台
# 再也扫不到，表现为「续写突然接不上上一段」—— 跑得通、接不上，最难查的一类。
_FIXED_PREFIX = "h3_context/clip"
# 0 = 自动模式：每跑一段落一个新文件，永不覆盖旧档。导演台永远承接最新那一个。
_FIXED_SLOT = 0

# 已经从节点上撤掉的两个控件（连同上面的固定值）。老工作流里还留着的值
# 要收下、丢掉，并且说一声 —— 静默忽略会让人以为面板上填的还是生效的。
_REMOVED_INPUTS = (("filename_prefix", _FIXED_PREFIX),
                   ("clip_index", _FIXED_SLOT))

_ANCHOR_SPEC = ("*", {
    "label": "顺序锚（接导演台任一空闲口）",
    "tooltip": "不参与计算，只保证本节点排在导演台之后执行。"
               "接导演台任意一个空闲输出口即可（比如「总帧数」）。",
})

_TAG = "[H3 续拍存档]"


def _mirror_to_chain(latent, anchor):
    """把这一份潜空间同步进导演台的段落档案。失败只影响镜像，不影响存盘。"""
    from . import csglide_chain as chain
    from .csglide_clip import CSGlideClipSaveCS

    name = chain.last_name()
    if not name:
        print("%s 导演台还没登记过工程名，本次跳过段落档案镜像"
              "（h3_context/ 的续写存档照常写入）。" % _TAG)
        return

    cid = ((chain.load_chain(name) or {}).get("current") or "").strip()
    if not cid or chain.load_clip(name, cid) is None:
        print("%s 片名「%s」的段落档案里没有「当前段」，本次跳过镜像 —— "
              "先跑一次导演台把当前段登记上。" % (_TAG, name))
        return

    meta = chain.load_clip(name, cid) or {}
    # 种子必须从档案里原样取回：📦 的 save() 会无条件写 seed，传 0 会把它抹掉。
    seed = int(meta.get("seed") or 0)

    _, report = CSGlideClipSaveCS().save(
        潜空间=latent, 片名=name, 段落=cid, 提示词="",
        帧数=0, 种子=seed, 另存为版本=True, 顺序锚=anchor)

    first = str(report or "").splitlines()
    print("%s 已同步进导演台段落档案（副本，h3_context/ 原件保留）—— %s"
          % (_TAG, first[0] if first else "%s / %s" % (name, cid)))


def apply_archive(cls_map):
    """给续拍存档节点补「顺序锚」口、撤掉两个没有正确取值的控件，并挂上段落档案镜像。

    单节点失败只跳过它，不拖垮整包加载。
    """
    cls = cls_map.get(_NODE_ID)
    if cls is None:
        return False
    try:
        # 取未绑定的原函数：cls.INPUT_TYPES 通过类访问已经是「绑定到 cls 的
        # classmethod」（前面 zh 层还会再包一层，同样绑定）。若直接保存它再在
        # 下面的 classmethod 里转发，Python 会把类作为第一个参数多传一次 ——
        # 触发 "INPUT_TYPES() takes 1 positional argument but 2 were given"，
        # 节点 schema 永久失效：/object_info 拿不到本节点，一连线提交工作流，
        # 连到本节点的下游（VAE 解码等）整链校验失败、红框报错。
        # __func__ 拿到未绑定函数本体，由我们自己显式接收并转发 cls。
        orig_input = cls.INPUT_TYPES.__func__

        def _patched_input(cls, *a, **k):
            d = orig_input(cls, *a, **k)
            req = d.get("required") or {}
            # 「保存路径 / 本片段编号」从节点上撤掉 —— 这两个各自只有一个正确
            # 取值（见 _FIXED_PREFIX / _FIXED_SLOT）。留一个能填的空，等于留一个
            # 把潜空间续写悄悄切断的开关。
            # 撤干净是安全的：撤完节点不再有控件，老图里残留的 widgets_values
            # 绑不到任何东西；而后端只按**声明过的**输入校验 ——
            # execution.py:validate_inputs 是 `for x in valid_inputs` 遍历，
            # 不是按 prompt 里的键遍历，所以多余的键它根本看不到，不会报错。
            for name, _canon in _REMOVED_INPUTS:
                req.pop(name, None)
            d.setdefault("optional", {}).setdefault("顺序锚", _ANCHOR_SPEC)
            return d

        cls.INPUT_TYPES = classmethod(_patched_input)

        fn_name = getattr(cls, "FUNCTION", "save") or "save"
        orig_fn = getattr(cls, fn_name)

        def _patched(self, *a, **kw):
            # latent 一律走关键字往下传：撤掉控件之后 ComfyUI 仍然按名传参，
            # 若这里收了位置参数再补关键字，会「multiple values for latent」。
            latent = kw.pop("latent", None)
            if latent is None and a:
                latent = a[0]
            anchor = kw.pop("顺序锚", None)
            stale = []
            for name, canon in _REMOVED_INPUTS:
                if name in kw:
                    val = kw.pop(name)
                    if val not in (None, "") and str(val) != str(canon):
                        stale.append("%s=%r" % (name, val))
            if stale:
                print("%s 「保存路径 / 本片段编号」已固定为 %s（自动编号）—— "
                      "老图里留的 %s 不再生效。"
                      % (_TAG, _FIXED_PREFIX, "、".join(stale)))
            out = orig_fn(self, latent=latent, filename_prefix=_FIXED_PREFIX,
                          clip_index=_FIXED_SLOT, **kw)
            try:
                _mirror_to_chain(latent, anchor)
            except Exception as e:
                print("⚠️ [班长全家桶] 续拍存档段落镜像跳过: %s" % e)
            return out

        setattr(cls, fn_name, _patched)
        return True
    except Exception as e:
        print("⚠️ [班长全家桶] 续拍存档未挂载: %s" % e)
        return False
