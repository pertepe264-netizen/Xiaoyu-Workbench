"""H3 钉帧 —— 让某些帧在采样中原样不动。

这是「改一段不牵连后面」的地基。原理（已在本机 ComfyUI 源码中查实）：

    comfy/samplers.py  KSamplerX0Inpaint.__call__
        latent_mask = 1. - denoise_mask
        x = x * denoise_mask + scale_latent_inpaint(...) * latent_mask
        out = self.inner_model(x, sigma, ...)
        out = out * denoise_mask + self.latent_image * latent_mask

最后一行是关键：遮罩为 0 的地方，输出 **逐位等于** 你传进去的 latent_image，
模型再怎么跑也改不动它。所以只要把尾 22 帧标 0，重抽之后这 22 帧还是原来
那些数字，后面一段拿到的引导就一模一样 —— 后面一段一个都不用重跑。

遮罩的形状（同样查实，samplers.py sample()）：

    if denoise_mask.is_nested:
        denoise_masks = denoise_mask.unbind()[:len(latent_shapes)]
    for i in range(len(denoise_masks), len(latent_shapes)):
        denoise_masks.append(torch.ones(latent_shapes[i]))     # 缺的流补 1
    ...
    if len(denoise_masks) > 1:
        denoise_mask, _ = comfy.utils.pack_latents(denoise_masks)

也就是说：遮罩用 NestedTensor（画面 + 声音两条流）最稳妥；只给画面时，
声音那条流会自动补成全 1（= 声音整段重新生成）。所以本节点默认只管画面。
"""

import torch

try:
    from comfy.nested_tensor import NestedTensor
except Exception:      # pragma: no cover
    NestedTensor = None

# ---- 帧 ↔ 潜空间步 的换算（20261010be）------------------------------------
# 这个节点的「保留头部 / 保留尾部 / 骨架间隔」三个输入**全是帧数**（widget
# 上就写着"帧数"，tooltip 也说"像素上约等于 22 帧"），但遮罩是画在潜空间
# 的**时间维**上的 —— 那里一格是一个 step，不是一帧。以前直接拿帧数当步数
# 用，于是：124 帧（37 步）的段填「保留尾部 22」实际钉掉 22 步 = 73 帧，
# 填 39 直接把 37 步全钉住 —— 整段冻住，重抽一次等于白跑，而且不报错，
# 只表现为"跑了六分钟中间一点没变"。
# H3 视频 VAE 的步长是 1,4,4,4,4 循环，所以换算必须带方向：从头部数和从
# 尾部数经过的步长序列不一样（头第一个是 1，尾最后一个是 4）。
FRAME_PER_TOKEN = (1, 4, 4, 4, 4)


def _pixel_frames_of(total):
    """total 个潜空间步覆盖多少像素帧。"""
    return sum(FRAME_PER_TOKEN[k % 5] for k in range(int(total)))


def _steps_covering(frames, total, from_tail=False):
    """覆盖末尾（或开头）``frames`` 帧需要几个潜空间步。

    ``from_tail`` 决定按哪个方向累加步长。返回 (步数, 实际覆盖帧数)；
    帧数不在栅格上时取**至少**覆盖它的最小步数（宁可多钉一点，也不能让
    该钉的帧没钉住 —— 少钉一帧接缝就跳）。
    """
    n = max(0, int(frames))
    if n <= 0:
        return 0, 0
    k, covered = 0, 0
    idx = total - 1 if from_tail else 0
    while covered < n and 0 <= idx < total:
        covered += FRAME_PER_TOKEN[idx % 5]
        idx += -1 if from_tail else 1
        k += 1
    return k, covered


def _step_at_frame(frame, total):
    """第 ``frame`` 帧落在第几个潜空间步上（钉骨架锚点用）。"""
    acc, j = 0, 0
    while j < total - 1 and acc + FRAME_PER_TOKEN[j % 5] <= int(frame):
        acc += FRAME_PER_TOKEN[j % 5]
        j += 1
    return j


def _streams(samples):
    """取出 latent 的各条流（H3 是 画面 + 声音 两条）。"""
    if samples is None:
        return []
    if getattr(samples, "is_nested", False):
        return list(samples.unbind())
    return [samples]


# ---------------------------------------------------------------------------
# 探针：采样器到底有没有收到遮罩、收到的是不是全 1
#
# 第一次真机验证失败（380160 个值全变）说明遮罩要么没送到、要么被改成了全 1，
# 光看结果猜不出是哪一种。这里在 KSamplerX0Inpaint.__call__ 上挂一层只读包装，
# 第一次被调用时把遮罩的形状 / 0 值比例打到控制台。只有用过钉帧节点才 arm，
# 不污染别的工作流。
# ---------------------------------------------------------------------------
_PROBE = {"armed": False, "msg": None}


def _install_probe():
    try:
        import comfy.samplers as _S
        orig = _S.KSamplerX0Inpaint.__call__
        if getattr(orig, "_bz_probe", False):
            return

        def probe(self, x, sigma, denoise_mask, model_options=None, seed=None):
            if _PROBE.get("armed") and _PROBE.get("msg") is None:
                try:
                    if denoise_mask is None:
                        _PROBE["msg"] = ("探针：采样器没收到遮罩（denoise_mask=None）"
                                         " —— 遮罩在到达采样器之前就被丢了")
                    else:
                        dm = denoise_mask
                        flat = dm.flatten() if hasattr(dm, "flatten") else None
                        z = int((flat < 0.5).sum())
                        n = int(flat.numel())
                        _PROBE["msg"] = (
                            "探针：遮罩 %s，0 值 %d/%d（%.1f%%），min=%.4f max=%.4f%s" % (
                                tuple(dm.shape), z, n, 100.0 * z / max(1, n),
                                float(flat.min()), float(flat.max()),
                                "" if z > 0 else "  ← 全是 1，等于没钉"))
                except Exception as _e:
                    _PROBE["msg"] = "探针：读遮罩时出错 %s" % _e
                print("[H3 钉帧·探针] " + _PROBE["msg"])
            return orig(self, x, sigma, denoise_mask, model_options or {}, seed)

        probe._bz_probe = True
        _S.KSamplerX0Inpaint.__call__ = probe
    except Exception as e:      # pragma: no cover
        print("[H3 钉帧] 探针安装失败（不影响节点使用）：%s" % e)


_install_probe()


def _motion_energy(v):
    """每一帧的「运动强度」——相邻帧潜空间差值的绝对值，按通道与空间取平均。

    用来挑锚点：等间隔钉帧不管画面内容，有可能把锚钉在一段几乎静止的过渡
    帧上（钉了等于没钉，动作照样跑偏），也可能错过关键动作帧。按运动强度
    挑，锚就落在动作真正发生的地方。
    """
    x = v.detach().float()
    if x.dim() != 5:
        return None
    # [B, C, T, H, W] → 逐帧差值 → 在 B/C/H/W 上取平均 → [T-1]
    d = (x[:, :, 1:] - x[:, :, :-1]).abs().mean(dim=(0, 1, 3, 4))
    if d.numel() == 0:
        return None
    # 最后一帧没有"下一帧"，沿用前一帧的变化量（长度对齐回 T）
    return torch.cat([d, d[-1:]], dim=0)


def _pick_motion_anchors(v, count, min_gap):
    """按运动强度挑出 count 个锚点帧号，相邻锚点至少隔 min_gap 帧。

    贪心：从最强的帧开始取，太挤的跳过。这样做是为了不让锚点全挤在一次
    挥手里、而后面半段一个锚都没有。
    """
    e = _motion_energy(v)
    T = v.shape[2]
    if e is None or T <= 0 or count <= 0:
        return []
    order = torch.argsort(e, descending=True).tolist()
    picked = []
    for i in order:
        if i < 0 or i >= T:
            continue
        if any(abs(i - j) < max(1, int(min_gap)) for j in picked):
            continue
        picked.append(i)
        if len(picked) >= count:
            break
    return sorted(picked)


def build_pin_mask(latent, head=0, tail=22, stride=0, keep_audio=False,
                   anchor="even"):
    """按帧号在**时间维**上标 0（保留）／1（重新生成）。

    head    保留开头几帧（续接时是上一段的引导头）
    tail    保留结尾几帧（重抽时钉住，后面段落才不用重跑）
    stride  每隔几帧保留一帧（骨架重绘：动作不变，只换细节）；0 = 关
    keep_audio  声音也按同样比例保留；默认关，声音整段重生成
    anchor  "even" = 等间隔钉；"motion" = 按运动强度挑关键动作帧钉
            （stride 这时决定锚点的**数量**与最小间隔，而不是位置）
    """
    if NestedTensor is None:
        raise RuntimeError("H3 钉帧：NestedTensor 不可用，请更新 ComfyUI。")
    samples = latent.get("samples")
    streams = _streams(samples)
    if not streams:
        raise ValueError("H3 钉帧：这个 latent 是空的。")

    v = streams[0]
    if v.dim() != 5:
        raise ValueError("H3 钉帧：画面 latent 应该是 5 维 [B,C,T,H,W]，拿到的是 %d 维。" % v.dim())

    T = v.shape[2]
    # 帧数 → 步数。head/tail/stride 三个参数**都是帧**，而 keep 里存的是
    # 步号 —— 不换算的话填 22 会钉掉 73 帧、填 39 会钉满整段（见文件头
    # FRAME_PER_TOKEN 那段注释）。
    h_steps, h_cov = _steps_covering(head, T, from_tail=False)
    t_steps, t_cov = _steps_covering(tail, T, from_tail=True)

    keep = set()
    for i in range(h_steps):
        keep.add(i)
    for i in range(t_steps):
        keep.add(T - 1 - i)
    # 钉满了就是没重画：说出来，别让人等六分钟发现一点没变。
    if h_steps + t_steps >= T and T > 1:
        print("[H3 钉帧] ⚠ 头尾钉住 %d 步里的 %d 步（头 %d 帧→%d 步 / 尾 %d 帧"
              "→%d 步）—— 整段都被钉住了，重抽不会有任何变化。把「保留头部」"
              "或「保留尾部」调小。" % (T, h_steps + t_steps,
                                       int(head), h_steps, int(tail), t_steps))
    s = max(0, int(stride))
    skel = set()
    if s > 0:
        if anchor == "motion":
            # 锚点数与等间隔一致（每隔 s 帧一个），最小间隔取半个步长：既不
            # 会全挤在一处，也不会因为"必须隔够"而少钉太多。
            n = max(1, int(_pixel_frames_of(T) / s))
            skel.update(_pick_motion_anchors(v, n, max(1, _step_at_frame(s, T) // 2)))
        else:
            # 每隔 s **帧**钉一帧：换算成步号，而不是每 s 步钉一步（后者在
            # H3 上是每约 3.4×s 帧才钉一次，比填的稀疏得多）。
            f = 0
            while f < _pixel_frames_of(T):
                skel.add(_step_at_frame(f, T))
                f += s
        keep.update(skel)
        # 骨架太密 = 整段冻住。H3 一格 ≈ 3.4 帧，间隔小于一个整步时"每隔 s
        # 帧钉一帧"物理上做不到（钉的最小粒度是**一步**，一步里的 1~4 帧要
        # 么全钉要么全放）。这时不报错，只表现为"跑了六分钟中间一点没变"——
        # 必须喊出来。
        if T > 1 and len(skel) >= max(2, int(T * 0.9)):
            print("[H3 钉帧] ⚠ 骨架间隔 %d 帧在 H3 上做不到：潜空间一格约 3.4 帧，"
                  "钉的最小粒度是一整步（一步 = 1~4 帧）—— 每隔 %d 帧钉一次被吸附成"
                  "「每步都钉」，%d 步里钉住 %d 步，整段冻住，重抽不会有任何变化。"
                  "请把间隔调到 5 帧以上。" % (s, s, T, len(skel)))

    # ⚠️ 通道维必须是 1，不能是 C。
    # 采样器拿到遮罩后会走 comfy.utils.reshape_mask(mask, latent_shape)：
    #     dims = len(shape)-2 = 3  →  input_mask.reshape((1,1,-1,H,W))  →  trilinear 到 (T,H,W)
    # 通道维给 C 的话，C×T 会被三线性插值压回 T，钉住的信息直接被平均掉，
    # 遮罩进了采样器就全是模糊值 —— 表现为「钉住的帧全变了」。
    # 通道维给 1：reshape 恒等、同尺寸插值恒等，最后 repeat 到 C 个通道，逐帧精确。
    # （这是第一次真机验证失败的根因，别改回去。）
    m = torch.ones((v.shape[0], 1) + tuple(v.shape[2:]),
                   dtype=torch.float32, device=v.device)
    for i in keep:
        m[:, :, i] = 0.0

    masks = [m]

    if keep_audio and len(streams) > 1:
        ashape = streams[1].shape
        a = torch.ones((ashape[0], 1) + tuple(ashape[2:]),
                       dtype=torch.float32, device=v.device)
        # 声音的时间轴和画面不是一格对一帧（H3 里 1 画面帧 ≈ 5/3 声音格）。
        # 这里按比例映射，够用但不追求逐格精确：声音的接缝在听感上宽容得多。
        at = a.shape[-1]
        for i in keep:
            lo = int(round(i / max(1, T) * at))
            hi = max(lo + 1, int(round((i + 1) / max(1, T) * at)))
            a[..., lo:hi] = 0.0
        masks.append(a)

    return NestedTensor(masks)


def diag_mask(latent):
    """离线自检：模拟采样器对遮罩的处理，看钉住的帧有没有活下来。

    不用跑模型、不用 GPU，几秒钟出结果。真机跑之前先跑这个，
    省得白等一次 H3。返回 (是否通过, 文字报告)。
    """
    mask = latent.get("noise_mask")
    if mask is None:
        return False, "这个 latent 没挂遮罩。"
    try:
        from comfy.utils import reshape_mask
    except Exception:
        return False, "拿不到 comfy.utils.reshape_mask，跳过。"

    streams = _streams(latent.get("samples"))
    mstreams = _streams(mask)
    lines = []
    all_ok = True
    for i, ms in enumerate(mstreams):
        if i >= len(streams):
            break
        target = tuple(streams[i].shape)
        out = reshape_mask(ms, target).float()
        # 遮罩里 0 的位置，经过 reshape_mask 之后还应该是 0
        src_keep = (ms < 0.5)
        got = out.expand_as(streams[i]) if out.shape != streams[i].shape else out
        kept_vals = got[src_keep.expand_as(got) if src_keep.shape != got.shape else src_keep]
        mx = float(kept_vals.max()) if kept_vals.numel() else 0.0
        ok = mx == 0.0
        all_ok = all_ok and ok
        lines.append("第 %d 条流：目标 %s，遮罩 %s → 处理后 %s；"
                     "钉住处最大值 %.6f —— %s"
                     % (i + 1, target, tuple(ms.shape), tuple(out.shape), mx,
                        "✅ 精确为 0" if ok else "❌ 被插值毁了"))
    head = ("结果：遮罩经 reshape_mask 后仍然精确 —— 可以上真机。"
            if all_ok else
            "结果：遮罩被 reshape_mask 插值毁了 —— 先别跑 H3，修遮罩形状。")
    return all_ok, head + "\n" + "\n".join(lines)


# 重抽档位 → 骨架间隔。单位是**帧**，但 H3 潜空间一格 ≈ 3.4 帧，所以间隔
# 小于一个整步（4 帧）时物理上做不到 —— 「隔 2 帧钉一帧」会被吸附成「每步
# 都钉」，等于整段冻住，重抽白跑（build_pin_mask 里会为此告警）。
# 这两个数不是拍脑袋定的，是**按旧的钉住比例反推**出来的：以前帧数被当步数
# 用，124 帧（37 步）的段上「保留动作」钉 5 步 ≈ 13%、「只换画质」钉 19 步
# ≈ 51%。换算修好之后要让档位的手感不变，所以：
#   保留动作：22 帧（≈ 每 6~7 步，124 帧段约 6 个锚，钉住约 16%）
#   只换画质：7 帧（≈ 每 2 步，钉住约 51%）
#   重画：    不钉骨架 —— 只钉头尾，内容全部重新生成
#   完整重拍：什么都不钉 —— -1 是哨兵，不是间隔值；骨架、头尾全放开，
#             整段从零重拍。代价是尾部对不上旧版，后面各段都得跟着重跑。
REDO_STRIDE = {"保留动作": 22, "只换画质": 7, "重画": 0, "完整重拍": -1}
FULL_RESHOOT = "完整重拍"
FOLLOW = "跟随档案（导演台）"


def _chain():
    try:
        from . import csglide_chain as chain
        return chain
    except Exception:      # pragma: no cover
        try:
            import csglide_chain as chain
            return chain
        except Exception:
            return None


def resolve_redo(mode, anchor, stride):
    """把「档位 + 锚点选法」解析成 (stride, anchor)。

    档位选「跟随档案」时读段落档案：导演台面板上选的档位会随登记写进这一段
    的档案，钉帧节点不必让人再去填一遍参数（两处填不一致，是最难查的一类坑）。
    读不到档案（没接导演台 / 没登记过）就退回「保留动作」，并说一声——静默
    降级会让人以为选的档位生效了。
    """
    # 老工作流里没有这两个控件 —— ComfyUI 会传 None。按"跟随档案"处理，
    # 让旧图不至于因为加了控件就跑不起来（缺值静默炸掉最难查）。
    m, a = (mode or FOLLOW), (anchor or FOLLOW)
    if m == FOLLOW or a == FOLLOW:
        ch = _chain()
        name = cid = ""
        try:
            name = (ch.last_name() or "").strip() if ch else ""
            if name:
                cid = ((ch.load_chain(name) or {}).get("current") or "").strip()
        except Exception:
            name = cid = ""
        meta = None
        if name and cid:
            try:
                meta = ch.load_clip(name, cid)
            except Exception:
                meta = None
        if meta:
            if m == FOLLOW:
                m = str(meta.get("redo_mode") or "保留动作")
            if a == FOLLOW:
                a = "motion" if str(meta.get("redo_anchor") or "") == "按运动强度" else "even"
            print("[H3 钉帧] 跟随档案：%s / %s → 档位 %s · 锚点 %s"
                  % (name, cid, m, a))
        else:
            if m == FOLLOW:
                m = "保留动作"
            if a == FOLLOW:
                a = "even"
            print("[H3 钉帧] 读不到段落档案（没接导演台或还没登记过）——"
                  "档位退回「保留动作」。要固定档位请直接在节点上选。")

    s = REDO_STRIDE.get(m)
    if s is None:                       # 自定义
        s = max(0, int(stride))
    return s, ("motion" if a == "按运动强度" else "even")


class BanZhangH3PinMask:
    """给 latent 挂上钉帧遮罩，交给 KSampler 用。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "latent": ("LATENT", {"label": "潜空间"}),
                "保留头部": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1,
                                    "label": "保留头部帧数",
                                    "tooltip": "开头几帧原样不动。续接时引导头就在这里。"}),
                "保留尾部": ("INT", {"default": 22, "min": 0, "max": 4096, "step": 1,
                                    "label": "保留尾部帧数",
                                    "tooltip": "结尾几帧原样不动（像素上约等于 22 帧）。"
                                               "重抽时钉住它，后面各段就一个都不用重跑。"}),
                "重抽档位": ([FOLLOW, "保留动作", "只换画质", "重画", "完整重拍", "自定义"],
                           {"default": FOLLOW, "label": "重抽档位",
                            "tooltip":                                        "跟随档案 = 用导演台面板上选的那一档（推荐，"
                                       "两处不用各填一遍）；\n"
                                       "保留动作 = 隔 22 帧钉一次（约 6 个锚），"
                                       "动作轨迹留住、只换细节；\n"
                                       "只换画质 = 隔 7 帧钉一次（约一半钉住），"
                                       "画面几乎不动、只提质感；\n"
                                       "重画 = 只钉头尾，内容全部重新生成；\n"
                                       "完整重拍 = 什么都不钉，整段从零重拍"
                                       "（后面各段要跟着重跑）。\n"
                                       "注意：H3 潜空间一格 ≈ 3.4 帧，间隔小于"
                                       " 4 帧做不到，会被吸附成整段钉住。"}),
                "锚点选法": ([FOLLOW, "等间隔", "按运动强度"],
                           {"default": FOLLOW, "label": "骨架锚点怎么选",
                            "tooltip": "等间隔 = 每隔 N 帧钉一帧（可能钉在静止的过渡帧上）；\n"
                                       "按运动强度 = 挑动作变化最大的那些帧钉 —— 锚落在"
                                       "关键动作上，连贯性更强。档位决定锚点数量。"}),
                "骨架间隔": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1,
                                    "label": "自定义骨架间隔（档位=自定义时用）",
                                    "tooltip": "每隔几帧钉一帧，只在档位选「自定义」时生效。"}),
                "保留声音": ("BOOLEAN", {"default": False, "label": "声音也保留",
                                        "tooltip": "关 = 声音整段重新生成（默认，最稳）；"
                                                   "开 = 声音按同样时间比例保留（近似）。"}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("潜空间",)
    FUNCTION = "pin"
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "给潜空间标出哪些帧不许动，采样后这些帧会逐位保持原样。"

    def pin(self, latent, 保留头部, 保留尾部, 重抽档位=None, 锚点选法=None,
            骨架间隔=0, 保留声音=False):
        _PROBE["armed"] = True
        _PROBE["msg"] = None
        stride, anchor = resolve_redo(重抽档位, 锚点选法, 骨架间隔 or 0)
        if stride < 0:
            # 「完整重拍」（哨兵 -1）：骨架、头尾全不钉。挂遮罩等于把这一段
            # 冻在旧版上，跟档位本身的含义正好相反 —— 原样直通才是对的。
            print("[H3 钉帧] 档位「%s」—— 整段从零重拍，不挂遮罩（后面各段"
                  "要跟着重跑）。" % FULL_RESHOOT)
            return (latent,)
        out = dict(latent)
        out["noise_mask"] = build_pin_mask(
            latent, int(保留头部), int(保留尾部), stride, bool(保留声音), anchor)
        t = out["noise_mask"].tensors[0]        # [B, 1, T, H, W]
        zeros = int((t[0, 0, :, 0, 0] == 0).sum())
        # t.shape[2] 是**步**不是帧 —— 以前直接拿它当帧数打印，日志说"画面
        # 37 帧"而实际是 124 帧，看着就对不上号。
        print("[H3 钉帧] 遮罩已挂上：画面 %d 帧（%d 步），其中 %d 步钉住"
              "（开头 %d 帧 / 结尾 %d 帧 / 骨架间隔 %d 帧 / 锚点 %s）"
              % (_pixel_frames_of(t.shape[2]), t.shape[2], zeros,
                 int(保留头部), int(保留尾部), stride,
                 "按运动强度" if anchor == "motion" else "等间隔"))
        ok, report = diag_mask(out)
        print("[H3 钉帧] 遮罩离线自检：" + ("通过" if ok else "不通过") + "\n  "
              + report.replace("\n", "\n  "))
        if not ok:
            print("⚠️ [H3 钉帧] 遮罩形状不对，采样器会把它插值毁掉 —— 别往下跑。")
        return (out,)


class BanZhangH3PinVerify:
    """自检：采样之后，被钉住的帧到底有没有真的没变。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "采样前": ("LATENT", {"label": "采样前（挂了遮罩的那一份）"}),
                "采样后": ("LATENT", {"label": "采样后（KSampler 的输出）"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("报告",)
    FUNCTION = "verify"
    OUTPUT_NODE = True
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "比对被钉住的帧在采样前后是否逐位相同。"

    def verify(self, 采样前, 采样后):
        mask = 采样前.get("noise_mask")
        if mask is None:
            return ("没有找到遮罩：请把「采样前」接到 H3 钉帧遮罩的输出，"
                    "而不是导演台直接出来的潜空间。",)

        a = _streams(采样前.get("samples"))
        b = _streams(采样后.get("samples"))
        m = _streams(mask)
        if not a or not b:
            return ("潜空间是空的，比不了。",)

        lines = []
        total_bad = 0
        max_diff_all = 0.0
        for si in range(min(len(a), len(b))):
            x0, x1 = a[si], b[si]
            if si < len(m):
                mk = m[si]
                # 遮罩的通道维是 1（为了让 reshape_mask 恒等），这里广播回画面通道数
                while mk.dim() > x0.dim():
                    mk = mk[..., 0]
                keep = (mk < 0.5)
                if keep.shape != x0.shape:
                    try:
                        keep = keep.expand(x0.shape)
                    except Exception:
                        keep = (mk < 0.5).reshape(1, 1, *mk.shape[2:]).expand(x0.shape)
            else:
                keep = torch.zeros_like(x0, dtype=torch.bool)

            if x1.shape != x0.shape:
                lines.append("第 %d 条流形状变了（%s → %s），跳过。"
                             % (si + 1, tuple(x0.shape), tuple(x1.shape)))
                continue

            n = int(keep.sum())
            if n == 0:
                lines.append("第 %d 条流：没有钉住任何帧。" % (si + 1))
                continue
            bad = int((x0[keep] != x1[keep]).sum())
            total_bad += bad
            # 逐位不等和"差一点点"是两回事：前者是遮罩没生效，后者只是浮点误差
            d = (x0[keep] - x1[keep]).abs().max()
            d = float(d) if d.numel() else 0.0
            max_diff_all = max(max_diff_all, d)
            if bad == 0:
                verdict = "完全一致 ✅"
            elif d < 1e-5:
                verdict = "有 %d 个值只差 %.2e（浮点误差，实际等于没变）✅" % (bad, d)
            else:
                verdict = "不一致 ❌（最大差 %.4f）" % d
            lines.append("第 %d 条流：钉住 %d 个值，其中 %d 个变了 —— %s"
                         % (si + 1, n, bad, verdict))

        # 判定口径（与 A1' 对齐）：
        #   max_diff < 1e-5 → 浮点抖动，VAE 量化后画面零差别 → 通过
        #   max_diff >= 1e-5 → 内容真变了 → 不通过，提示走方案 B
        if total_bad == 0:
            head = ("✅ 结论：钉住的帧在采样后逐位相同。"
                    "重抽这一段，后面各段不用重跑。")
        elif max_diff_all < 1e-5:
            head = ("✅ 结论：钉住的帧内容未变（最大差 %.2e，纯浮点抖动，"
                    "解码量化后画面零差别）。A1' 成立 —— "
                    "重抽这一段，后面各段拿到的引导一致，不用重跑。" % max_diff_all)
        else:
            head = ("❌ 结论：有 %d 个值被改动了（最大差 %.4f）。"
                    "这条不通 —— 需要改用「生成后把尾帧潜空间替换回去」"
                    "的兜底办法。" % (total_bad, max_diff_all))

        probe = _PROBE.get("msg")
        if probe:
            head += "\n\n" + probe
            if "没收到遮罩" in probe or "全是 1" in probe:
                head += ("\n→ 遮罩根本没进采样器。把你这条链路上的班长补丁节点"
                         "（SelfLift / 显存优化 / VRAM 守卫）逐个 Ctrl+B 绕过，"
                         "找出是谁把遮罩吃了。")
        return (head + "\n\n" + "\n".join(lines),)


class BanZhangH3TailRestore:
    """兜底办法（方案 B）：采样跑完之后，把尾帧从参考 latent 原样拷回去。

    如果 🧪 钉帧自检 报「有值被改动」，说明遮罩这条路在当前版本走不通，
    就用这个节点 —— 不依赖采样器，纯张量拷贝，永远成立。
    代价：接缝处可能有极轻微的跳变，靠续接合成的 S 渐变切抹平。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "采样后": ("LATENT", {"label": "采样后（要修的那一份）"}),
                "参考": ("LATENT", {"label": "参考（尾帧以它为准）"}),
                "回填头部": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 1,
                                   "label": "回填头部帧数"}),
                "回填尾部": ("INT", {"default": 22, "min": 0, "max": 4096, "step": 1,
                                   "label": "回填尾部帧数"}),
            }
        }

    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES = ("潜空间",)
    FUNCTION = "restore"
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "采样完把头尾帧从参考潜空间原样拷回去，保证后面各段不用重跑。"

    @staticmethod
    def _time_dim(t):
        return 2 if t.dim() == 5 else -1

    @staticmethod
    def _counts(回填头部, 回填尾部, d, n_video_steps, is_video):
        """把**帧数**换算成这条流上要拷的**格数**。

        和 build_pin_mask 同一个坑：参数是帧，时间维一格是一个 step。以前直接
        拿 22 去 index_copy_，124 帧（37 步）的段上实际拷掉 22 步 = 73 帧 ——
        重抽的一半成果被旧版盖回去，表现为"中间没怎么变"。
        声音那条流的时间轴不是一格一帧，按本片自己的画/声比例折算。
        """
        T = d.shape[2 if (is_video and d.dim() == 5) else -1]
        if is_video:
            h = _steps_covering(回填头部, T, from_tail=False)[0]
            t = _steps_covering(回填尾部, T, from_tail=True)[0]
        else:
            # 声音：拿本片「画面总帧数 : 声音总格数」的比例折算，不写死 5/3
            # —— 比例随分辨率/长度变，写死就会在别的尺寸上悄悄偏。
            nvf = max(1, _pixel_frames_of(n_video_steps))
            scale = T / float(nvf)
            h = int(round(int(回填头部) * scale))
            t = int(round(int(回填尾部) * scale))
        h = max(0, min(h, T))
        t = max(0, min(t, T - h))
        return h, t

    def restore(self, 采样后, 参考, 回填头部, 回填尾部):
        dst = _streams(采样后.get("samples"))
        if not dst:
            raise ValueError("H3 尾帧回填：「采样后」是空的。")

        # 参考为空 = 还没有上一次的存档（首次运行）。原样放过，照样能存下第一版，
        # 第二次运行就有得比了 —— 否则首跑必崩，用户得先绕开这个节点才行。
        if 参考 is None or 参考.get("bz_empty") or 参考.get("samples") is None:
            print("[H3 尾帧回填] 参考为空（还没有上次存档）—— 本次原样放过，"
                  "先存下第一版；下次运行就会用它回填尾帧。")
            return (采样后,)

        src = _streams(参考.get("samples"))

        out = dict(采样后)
        streams = []
        n_copied = 0
        skipped = []
        # 画面总步数 —— 声音那条流折算时用它算「画:声」比例
        n_video_steps = dst[0].shape[2] if dst[0].dim() == 5 else 0
        for i, d in enumerate(dst):
            d = d.clone()
            if i >= len(src):
                # 参考流数比输出少（比如参考只有画面、输出是画面+声音）。
                # 以前这分支会去引用上一轮的 s，要么 NameError 要么报个假形状。
                skipped.append("第 %d 条流：参考只有 %d 条流，没有可对的那一份，跳过。"
                               % (i + 1, len(src)))
                streams.append(d)
                continue
            s = src[i]
            if s.shape != d.shape:
                # 尺寸不符必须记下来喊出去 —— 静默跳过最坑：用户以为钉住了，
                # 实际一帧没动，后面各段照样全变。
                skipped.append("第 %d 条流：参考 %s 与输出 %s 网格不一致，跳过。"
                               % (i + 1, tuple(s.shape), tuple(d.shape)))
                streams.append(d)
                continue
            ax = self._time_dim(d)
            T = d.shape[ax]
            head, tail = self._counts(回填头部, 回填尾部, d,
                                      n_video_steps, d.dim() == 5)
            if head:
                idx_h = torch.arange(head, device=d.device)
                d.index_copy_(ax, idx_h, s.index_select(ax, idx_h).to(d.dtype))
                n_copied += head
            if tail:
                idx = torch.arange(T - tail, T, device=d.device)
                d.index_copy_(ax, idx, s.index_select(ax, idx).to(d.dtype))
                n_copied += tail
            streams.append(d)

        out["samples"] = streams[0] if len(streams) == 1 else NestedTensor(streams)
        if n_copied:
            # n_copied 是**格数**（画面按步、声音按格），不是帧 —— 参数是帧，
            # 换算在 _counts 里做过了。别再写成"共 N 帧"，那会让人以为 22 帧
            # 只回填了 7 格是坏了。
            print("[H3 尾帧回填] 已回填 %d 条流，共 %d 格（头 %d 帧 / 尾 %d 帧"
                  "换算而来；画面一格 = 1 步，声音一格 = 1 个音频格）"
                  % (len(streams), n_copied, int(回填头部), int(回填尾部)))
        if skipped:
            # 静默跳过最坑：用户以为钉住了，实际一帧没动。必须喊出来。
            print("⚠️ [H3 尾帧回填] **一帧都没回填**！\n  " + "\n  ".join(skipped)
                  + "\n  常见原因：参考来自不同分辨率的片子，或用了「🎯 H3 放大采样器」"
                    "（SelfLift 会把网格放大 1.5×，那样输出的潜空间比输入大，拷不回去）。"
                    "\n  想开 SelfLift 就用官方 KSampler —— 它走 guider.sample()，"
                    "会按输入网格重打包，尺寸不变，回填才成立。")
        return (out,)


class BanZhangH3FrameVerify:
    """A1' 的画面级验证：解码后比对尾 22 帧。

    钉帧自检（BanZhangH3PinVerify）比的是**潜空间**，在开了 SelfLift
    （采样中途换网格）之后，潜空间逐位比对物理上不可能成立 —— 参照物本身
    被重采样了。但 A1' 只要求「尾 22 帧**解码出来的画面**与重抽前一致」，
    因为下一段拿到的引导是从解码画面取的，不是从潜空间取的。

    这个节点在**像素域**比对，对 SelfLift 开 / 关都成立。
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "重抽前画面": ("IMAGE", {"label": "重抽前的尾帧画面"}),
                "重抽后画面": ("IMAGE", {"label": "重抽后的尾帧画面"}),
                "比对尾部": ("INT", {"default": 22, "min": 1, "max": 4096, "step": 1,
                                    "label": "比对尾部帧数",
                                    "tooltip": "取两份画面的最后 N 帧比对。"}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("报告",)
    FUNCTION = "verify"
    OUTPUT_NODE = True
    CATEGORY = "👨‍🏫班长/视频处理"
    DESCRIPTION = "在像素域比对尾帧画面，验证 A1'（解码画面一致）。"

    def verify(self, 重抽前画面, 重抽后画面, 比对尾部):
        a = 重抽前画面
        b = 重抽后画面
        n = min(int(比对尾部), a.shape[0], b.shape[0])
        if n <= 0:
            return ("两份画面都是空的，比不了。",)
        ta = a[-n:]
        tb = b[-n:]
        if ta.shape != tb.shape:
            return ("两份画面形状不一致（%s vs %s），比对尾部帧数可能超出了其中一份。"
                    % (tuple(a.shape), tuple(b.shape)),)

        diff = (ta - tb).abs()
        maxd = float(diff.max())
        mean = float(diff.mean())
        # 像素域在 [0,1]，1e-3 约等于 1/255 的 1/4 —— 肉眼不可见的量化级
        ok = maxd < 1e-3
        head = ("✅ A1' 成立：尾 %d 帧解码画面一致（最大差 %.2e，均值 %.2e，"
                "肉眼不可见）。下一段拿到的引导与重抽前一致，不用重跑。"
                % (n, maxd, mean) if ok else
                "❌ A1' 不成立：尾 %d 帧解码画面有可见差异（最大差 %.4f）。"
                "引导会变，下一段需要重跑。" % (n, maxd))
        return (head,)


NODE_CLASS_MAPPINGS = {
    "BanZhangH3PinMask": BanZhangH3PinMask,
    "BanZhangH3PinVerify": BanZhangH3PinVerify,
    "BanZhangH3TailRestore": BanZhangH3TailRestore,
    "BanZhangH3FrameVerify": BanZhangH3FrameVerify,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhangH3PinMask": "🔒 H3 钉帧遮罩 (H3 Pin Mask)",
    "BanZhangH3PinVerify": "🧪 H3 钉帧自检 (H3 Pin Verify)",
    "BanZhangH3TailRestore": "🧷 H3 尾帧回填 (H3 Tail Restore)",
    "BanZhangH3FrameVerify": "🖼️ H3 画面自检 (H3 Frame Verify)",
}
