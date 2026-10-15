"""H3 段落档案 —— 一条片子 = 一个文件夹，一个段落 = 一份档案（潜空间 + 元数据）。

一条原则：时间轴和潜空间存档必须是一套账。以前潜空间按 clip_index 编号存在
h3_context/ 下，时间轴另有一套顺序，两套编号一旦对不上就会出「接错了段」这种
最难查的错。现在段落档案统一发号，潜空间文件跟着段落 id 走。

磁盘布局：

    output/h3_chain/<片名>/
        chain.json                   整条片：顺序、帧率、当前段、锁定
        clip_0001.json               第 1 段元数据
        clip_0001.latent.safetensors 第 1 段潜空间（兼容 _h3mc 的存档格式）
        clip_0001.t2.latent.safetensors  第 1 段第 2 个版本
        clip_0001.mp4                成片

只做读写和路由，不做界面；界面在 js/csglide_studio.js（阶段 4）。
"""

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time

try:
    import folder_paths
except Exception:      # pragma: no cover
    folder_paths = None

# 本地统一状态库（20261012 方案 A）：新工程的 chain/clip 权威在 SQLite，
# 老工程（目录里已有 chain.json）继续走 JSON。store 模块自带 /cglide/store/* 路由。
try:
    from . import csglide_store as store
except ImportError:      # pragma: no cover - 独立脚本直跑
    import csglide_store as store

# 引导头默认承接帧数（出厂默认）。H3 续写窗口合法格点是 17k+5：5/22/39/56…，
# 注册段时前端按 state.cont.frames 显式传值；这里是「没显式传」时的兜底，默认 39。
DEFAULT_TRIM = 39
import functools

# chain 档案的读-改-写临界区锁（20261012）：路由搬进线程池后，多个写请求
# 可能并发跑（事件循环不再天然串行化同步段）。RLock 允许函数互相嵌套调用
# （add_clip 内部会调 update_clip / save_clip）。
_chain_lock = threading.RLock()


def chainlocked(fn):
    @functools.wraps(fn)
    def _w(*a, **k):
        with _chain_lock:
            return fn(*a, **k)
    return _w


async def _io(fn, *a, **k):
    """路由里把同步档案 IO（json 读写 / stat / listdir）丢线程池：

        meta = await _io(update_clip, name, cid, patch)

    aiohttp 单事件循环，同步 IO 直接跑在路由里会堵死所有 HTTP 请求 ——
    K 盘上一次 film_summary 几十次 stat，期间整个 ComfyUI 界面都卡。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *a, **k))


# ---------------- WebSocket 变更广播（20261012 方案B）----------------
# 档案任何写点落盘后，向所有前端推一条轻量「变了」信号；前端 bz_store
# 收到后按片名在 100ms 合帧窗口里只拉一次 film_summary（信号-拉取模式：
# 消息极小、不怕乱序/重复/丢条，权威状态永远以拉取结果为准）。
# ComfyUI 的 PromptServer.send_sync 内部已是 loop.call_soon_threadsafe，
# 所以从事件循环线程 / executor 工作线程 / 工作流执行线程调用都安全。
_WS_EVENT = "bz.chain.event"


def ws_broadcast(kind, name, clip=None, **extra):
    """档案变更广播。无界面/自检环境（PromptServer 不存在）静默跳过。"""
    try:
        from server import PromptServer
        ps = getattr(PromptServer, "instance", None)
        if ps is None:
            return
        payload = {"type": kind, "name": name}
        if clip is not None:
            payload["clip"] = str(clip)
        if extra:
            payload.update(extra)
        ps.send_sync(_WS_EVENT, payload)
    except Exception:
        pass


def _last_saved_cont():
    """_h3mc 最近一次自动写出的续拍存档（120 秒内才算数，否则 None）。

    存档节点写文件是本次采样的最后一步，收片（adopt_film）紧跟其后 ——
    时间窗内那条 LAST_SAVED 就是本次 run 写的。把它记到该段档案名下
    （take_params.cont_saved），下一段续写就能**精确认段**：目录扫描只能
    猜「最新的是谁」，串两个工程就把别的段的档接进来了。
    组件读不到 / 记录太旧 / 文件已删，都如实返回 None。
    """
    try:
        from ._h3mc import nodes as _mc_nodes
    except Exception:
        try:
            from _h3mc import nodes as _mc_nodes
        except Exception:
            return None
    rec = getattr(_mc_nodes, "LAST_SAVED", None) or {}
    p = str(rec.get("path") or "")
    t = float(rec.get("time") or 0)
    if p and os.path.isfile(p) and (time.time() - t) < 120:
        return p
    return None


def _out_dir():
    return folder_paths.get_output_directory() if folder_paths is not None else "."


# ---------------- 成片实测帧数（20261010）----------------
# H3MC 链路登记成片走 adopt_film，而它从来不写 film_frames —— 档案里恒为 0。
# 前端"这条成片是不是累积片"的判据（videoT0 / 连播落段 / 雪碧图 / 波形窗口）
# 全都靠这个字段分辨单段片与累积片，0 等于失去地面真相，只能靠猜 ——
# "整片连播每一段播两遍、前后衔接不对"就是从这开始的。
_FFPROBE_CACHE = {}


def _ffprobe_exe():
    """Best-effort ffprobe。imageio-ffmpeg 只带 ffmpeg 不带 ffprobe，所以
    PATH 优先、imageio 兄弟目录兜底；都找不到返回 None（调用方放弃探测）。"""
    found = shutil.which("ffprobe")
    if found:
        return found
    try:
        import imageio_ffmpeg  # noqa: WPS433 - 可选依赖
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        cand = os.path.join(os.path.dirname(exe),
                            "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.isfile(cand):
            return cand
    except Exception:
        pass
    return None


def probe_film_frames(path, timeout=15):
    """成片视频流的**实测**帧数，按 (路径, mtime, size) 缓存；失败返回 0。

    -count_frames 要真解码数一遍，几百帧的成片不到一秒；同一份文件（没被
    重抽覆盖过）只探一次。"""
    if not path or not os.path.isfile(path):
        return 0
    try:
        st = os.stat(path)
        key = (os.path.normcase(os.path.realpath(path)), st.st_mtime, st.st_size)
    except OSError:
        return 0
    hit = _FFPROBE_CACHE.get(key)
    if hit is not None:
        return int(hit)
    n = 0
    exe = _ffprobe_exe()
    if exe:
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=nb_read_frames",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=timeout)
            for line in (out.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    n = int(line)
                    break
        except Exception:
            n = 0
    _FFPROBE_CACHE[key] = n
    return n


def chain_dir(name):
    safe = "".join(c for c in (name or "未命名") if c not in '\\/:*?"<>|').strip() or "未命名"
    return os.path.join(_out_dir(), "h3_chain", safe)


def unique_chain_name(base):
    """给一个还没被占用的片名：base 没目录就用它，否则 base 2 / base 3 ……
    「新建整片」自动命名用，避免重名把新片写进旧片档案。"""
    base = (base or "未命名整片").strip() or "未命名整片"
    root = os.path.join(_out_dir(), "h3_chain")

    def taken(cand):
        safe = "".join(c for c in cand if c not in '\\/:*?"<>|').strip() or "未命名"
        return os.path.exists(os.path.join(root, safe))

    if not taken(base):
        return base
    i = 2
    while taken("%s %d" % (base, i)):
        i += 1
    return "%s %d" % (base, i)


def _last_path():
    return os.path.join(_out_dir(), "h3_chain", "_last.json")


def mark_last(name):
    """记下最近一次导演台登记过的片名。

    📦段落存档 / 📤段落调取 的「片名」留空时读它 —— 用户只要在导演台工程
    面板里填了工程名，这两个节点就不必再手打一遍，也不会因为打错或忘了同步
    而存进**另一个**档案（那是最难查的一类坑：看着跑通了，其实存错了地方）。
    """
    if not (name or "").strip():
        return
    _write_json(_last_path(), {"name": name, "time": time.time()})


def last_name():
    return ((_read_json(_last_path(), {}) or {}).get("name") or "").strip()


# JSON 读缓存（20261012 联动提速）：时间轴每 1~2 秒轮询一次 summary，
# 一次 film_summary 要读 1 + N 个 json、stat 3N+ 次，全部是同步阻塞 IO。
# 按 (path, mtime, size) 缓存解析结果，文件没变就是一次字典查找；写后主动失效。
_json_memo = {}
_json_memo_lock = threading.Lock()


def _read_json(path, default):
    try:
        st = os.stat(path)
    except Exception:
        return default
    key = os.path.normcase(os.path.abspath(path))
    sig = (st.st_mtime, st.st_size)
    with _json_memo_lock:
        hit = _json_memo.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    with _json_memo_lock:
        _json_memo[key] = (sig, data)
    return data


def _invalidate_json(path):
    """写盘后让读缓存失效（_write_json 已自动调用，外部直写时可补调）。"""
    key = os.path.normcase(os.path.abspath(path))
    with _json_memo_lock:
        _json_memo.pop(key, None)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    _invalidate_json(path)


def load_chain(name):
    d = chain_dir(name)
    if store.has_db(d):
        # DB 工程：权威在库，库读不出来就是默认空账，绝不回退翻 JSON
        doc = store.load_chain_doc(d, name)
        if doc is not None:
            return doc
    return _read_json(os.path.join(d, "chain.json"), None) or {
        "name": name, "fps": 24, "trim_frames": DEFAULT_TRIM,
        "prompt_global": "", "current": None, "locked": False, "order": []
    }


@chainlocked
def save_chain(name, data):
    d = chain_dir(name)
    # 首次写定型：全新工程（db/json 都没有）自动建库；老工程（chain.json 在）
    # ensure_db 返回 'json'，继续写 JSON，互不干扰。
    if store.ensure_db(d) == "db":
        store.save_chain_doc(d, name, data)
    else:
        _write_json(os.path.join(d, "chain.json"), data)


def clip_json(name, cid):
    return os.path.join(chain_dir(name), "%s.json" % cid)


def clip_latent(name, cid, take=1):
    stem = cid if take <= 1 else "%s.t%d" % (cid, take)
    return os.path.join(chain_dir(name), "%s.latent.safetensors" % stem)


def clip_film(name, cid, take=1):
    """这一段第 take 版的成片路径。与 clip_latent 同目录、同命名口径。

    成片必须和潜空间档案待在一起：一个片子的东西散落在 output 根目录，用户
    清目录时不知道哪个 mp4 属于哪一段哪个版本，备份/迁移也无从下手。放进来
    之后，拷走 h3_chain/<片名>/ 这一个文件夹就是完整工程。
    """
    stem = cid if take <= 1 else "%s.t%d" % (cid, take)
    return os.path.join(chain_dir(name), "%s.mp4" % stem)


def clip_cont(name, cid, take=1):
    """这一段第 take 版的**续拍存档**（下一段从它接着写）。

    与成片/原稿同目录、同命名口径（20261010bb）：存档节点写出的那份落在
    h3_context/ 里、编号跟成片对不上（clip_00301_ vs clip_0001.t12），备份时
    得记着「还有一摞档在别的目录」。收片时拷一份进来，一个文件夹就是完整工程。
    """
    stem = cid if take <= 1 else "%s.t%d" % (cid, take)
    return os.path.join(chain_dir(name), "%s.cont.safetensors" % stem)


def load_clip(name, cid):
    d = chain_dir(name)
    if store.has_db(d):
        # DB 工程库里没有这段 = 真没有（同 JSON 工程里 json 文件缺失返回 None）
        return store.load_clip_doc(d, str(cid))
    return _read_json(clip_json(name, cid), None)


@chainlocked
def reorder_clips(name, order):
    """按给定的段落 id 顺序重排 chain.order（时间轴拖动排序用）。

    规则与理由：
      · **只认识档案里已有的 id**，且忽略重复 —— 前端可能把整条轴的顺序发过来，
        其中混着只存在于工程、还没登记的段（它们没有 id），发什么都不能凭空造段。
      · 名单里没提到的段**不丢**，按原相对顺序跟在后面。宁可少排一段，不可吞段：
        chain.order 是时间轴与成片链的唯一依据，静默丢掉一段等于凭空删了一整段画面。
      · 返回新的完整顺序，前端据此回显（而不是自己猜）。
    """
    chain = load_chain(name)
    cur = [c for c in (chain.get("order") or []) if c]
    have = set(cur)
    seen = set()
    head = []
    for c in (order or []):
        c = str(c)
        if c in have and c not in seen:
            seen.add(c)
            head.append(c)
    tail = [c for c in cur if c not in seen]
    chain["order"] = head + tail
    save_chain(name, chain)
    store.log_event(chain_dir(name), "reorder", None,
                    {"order": chain["order"]}, "monitor")
    ws_broadcast("reordered", name)
    return {"order": chain["order"], "moved": len(head), "kept": len(tail)}


@chainlocked
def save_clip(name, cid, meta):
    meta = dict(meta)
    meta["updated"] = time.time()
    d = chain_dir(name)
    if store.ensure_db(d) == "db":
        store.save_clip_doc(d, cid, meta)
    else:
        _write_json(clip_json(name, cid), meta)


def _dedup_order(order, name=None):
    """去掉 order 里的重复段 id，保序、保首个。

    旧版 next_cid 按 len(order)+1 发号：删过一段之后长度缩回去，下一次
    「接一段」就会发出一个**仍然存在**的 id —— 新段 json 直接覆盖旧段，
    order 里出现重复 id。此后删段只能删掉第一个同 id 项，另一个成了永远
    删不掉的幽灵段：时间轴与「全片时长」被它撑得永远比实际长（工程里删到
    只剩一段，底栏还是 6 段的总时长）。除了修发号源头，每次写 order 时
    顺手清一遍历史遗留的重复项，老档案自愈。

    传了 name 再多修一类：段落 json 已经不在的 id（覆盖/手删留下的空壳）
    直接摘掉 —— 留着只会让时间轴多画一段「档案读不出来」的空壳引人误会。
    """
    seen, out = set(), []
    for c in (order or []):
        if not c or c in seen:
            continue
        if name is not None and load_clip(name, c) is None:
            continue
        seen.add(c)
        out.append(c)
    return out


def next_cid(name):
    """发一个**没被占用**的段落 id。

    占用 = 出现在 order 里，或磁盘上已有同号段落 json（order 修不到的残留）。
    绝不发会覆盖已有段的号 —— 那正是「删一段再接一段，另一段凭空消失、
    底栏总时长再也降不回去」的根因。
    """
    chain = load_chain(name)
    taken = set(c for c in (chain.get("order") or []) if c)
    try:
        for fn in os.listdir(chain_dir(name)):
            if fn.startswith("clip_") and fn.endswith(".json"):
                taken.add(fn[:-5])
    except Exception:
        pass
    n = len(taken) + 1
    while ("clip_%04d" % n) in taken:
        n += 1
    return "clip_%04d" % n


@chainlocked
def add_clip(name, **fields):
    """新增一段，返回段落 id。fields 覆盖默认值。

    同一分段 uid（meta.shot）只允许有一段：工程被还原/重载后 clip id 丢了
    再登记，不能又在档案里堆一段 —— 那是「档案越堆越长、删都删不干净」的
    另一半根因。命中已有段就补齐其余字段后原样返回它的 id。
    """
    chain = load_chain(name)
    shot_uid = str(fields.get("shot") or "")
    if shot_uid:
        for c in _dedup_order(chain.get("order")):
            if str((load_clip(name, c) or {}).get("shot") or "") == shot_uid:
                patch = dict((k, v) for k, v in fields.items() if k != "shot")
                if patch:
                    update_clip(name, c, patch)
                return c
    cid = next_cid(name)
    meta = {
        "id": cid, "index": len(chain["order"]) + 1,
        "frames": 149, "fps": chain.get("fps", 24),
        # order 空 = 这是全片第一段，不承接任何段 → trim=0（正常流程兜底；
        # 前端 registerShot 已按工程真实段序显式传 trim，这里只防别的入口漏发）。
        "trim_frames": 0 if not chain.get("order")
        else chain.get("trim_frames", DEFAULT_TRIM),
        "prompt_seg": "", "refs": [],
        "seed": 0, "steps": 4, "cfg": 1.0,
        "bridge": {"adapter": "", "alpha": 0.2, "mode": "text"},
        "takes": [], "current_take": 0,
        "seam_left": None, "seam_right": None,
        "tail_lock": True, "pending": None, "has_latent": True,
        # 锁定 = 钉住编辑目标（见 set_locked），不是"这段不许改"
        "locked": False,
        # 成片：第 k 段的 film = 生成完第 k 段之后的**累积**成片。
        # 播放头拖到第 k 段就播这一版 —— 和剪辑软件里「看到第 k 个镜头为止」
        # 的直觉一致。Join 每次写出成片都会把它登记到当前段。
        "film": None, "film_frames": 0, "film_seam": None,
        # 契约（20261010ba）：这条成片**开头有几帧是承接/旧内容**。
        # 本段新画面 = [film_head, film_head + 净长)。落盘时算一次并写死，
        # 下游一律读它 —— 不再各处猜「这条是累积片还是单段片」。
        # None = 老档案没记（film_summary 会补一次，见 _migrate_film_head）。
        "film_head": None,
        # 承接窗口（20261010bb · 方案 B 的地基）：这一段**生成时钉住了谁的
        # 哪一段画面**。四种接法全是它的配置，不是四套代码：
        #   · 往后接 extend：一条 at="head"（钉住上一段尾部）
        #   · 往前接 prepend：一条 at="tail"（钉住后一段头部）
        #   · 补桥 bridge：head + tail 两条（两个父），中间留噪声让模型填
        #   · 循环 loop：head 钉最后一段尾、tail 钉第一段头
        # 生成侧照它拼输入，输出只取没被钉住的那一截 —— 于是四种接法共用
        # 一条链路。空列表 = 还没接过（独立生成的首段）。
        "pins": [],
        # 承接窗口钉在**尾部**时（往前接 / 补桥 / 循环），交付的片子末尾那
        # 几帧是后一段开头的重绘，进成片前要剪掉，否则与后一段重复。
        # 0 = 尾部没钉（往后接 / 独立首段）—— 与 trim_frames 对称，只是反方向。
        "trim_tail": 0,
    }
    meta.update(fields)
    # 这里**不**吸附帧数：档案要记的是导演台真实跑出来的帧数，改它就是篡改
    # 记录（潜空间是 149 帧，档案却写 141，后面按档案裁就裁错了）。
    # 吸附只在 resize_clip 里做 —— 那是用户主动选时长，必须给他一个合法的。
    save_clip(name, cid, meta)
    chain["order"] = _dedup_order(chain.get("order"), name) + [cid]
    chain["current"] = cid
    save_chain(name, chain)
    store.log_event(chain_dir(name), "clip_add", cid,
                    {"frames": meta.get("frames"), "shot": shot_uid}, "backend")
    ws_broadcast("clip_added", name, cid)
    return cid


@chainlocked
def update_clip(name, cid, patch):
    meta = load_clip(name, cid)
    if meta is None:
        return None
    meta.update(patch or {})
    save_clip(name, cid, meta)
    ws_broadcast("clip_updated", name, cid)
    return meta


# 四档待办的**代价**完全不同，凡是列给用户看的地方都要说清是哪一档：
# trim / rejoin 只是重拼一次（秒级），grow / redo 要重新跑模型（分钟级）。
# 只写"待办"两个字，用户没法判断值不值得现在跑。
PENDING_TEXT = {
    "grow": "待加长（要重新生成）",
    "trim": "待裁剪（要重拼一次）",
    # 换过版本（swap takes）之后：后面各段的累积成片里还夹着旧版本，
    # 重拼一次就好 —— 不用重新采样。
    "rejoin": "待重拼（换过版本，成片还没跟上）",
    "redo": "待重抽（要重新生成）",
}


def set_pending(name, cid, kind):
    """kind: None / 'grow'（要加长）/ 'trim'（要裁剪）/ 'redo'（要重抽）"""
    return update_clip(name, cid, {"pending": kind})


# ---- 时长：网格与吸附 ----------------------------------------------------
# H3 只认 17k+5 这一段时长的格点（17 是它的时间压缩步长，+5 是首帧开销）。
# 不在格点上的帧数模型直接拒，所以时间轴上拖出来的数必须吸附回去——
# 吸附在**改时长这一刻**做，而不是等到出图时才报"帧数非法"。
FRAME_GRID = 17
FRAME_BASE = 5
MIN_CLIP_FRAMES = 5
# 上限与导演台 LENGTH_PRESETS 的最大值对齐（719 ≈ 30s）。再长显存撑不住，
# 与其出到一半炸掉，不如在时间轴上就挡住。
MAX_CLIP_FRAMES = 719


def snap_frames(n, hi=None):
    """吸附到 17k+5，并夹进 [MIN, hi]。"""
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        n = MIN_CLIP_FRAMES
    top = int(hi or MAX_CLIP_FRAMES)
    n = max(MIN_CLIP_FRAMES, min(top, n))
    k = max(0, int(round((n - FRAME_BASE) / FRAME_GRID)))
    out = FRAME_BASE + FRAME_GRID * k
    # round 可能把结果顶出上限（例如 719 → 726），回退一格
    while out > top and k > 0:
        k -= 1
        out = FRAME_BASE + FRAME_GRID * k
    return max(MIN_CLIP_FRAMES, out)


def resize_clip(name, cid, frames):
    """改一段的时长 —— 真正决定「后面要不要重跑」的就是这个函数。

    加长：多出来的帧模型根本没画过，凭空变不出来，所以本段标 **grow**（要重
    生成）。但重生成时尾部 22 帧是钉住的（方案 A 遮罩 / 方案 B 回填），
    下一段的引导头取自那个尾部 —— 没变，所以**下一段不用重跑**。这就是
    「拖长第 N 段，第 N+1 段成片不变」（A4）。

    缩短：本段尾部被裁掉了，而下一段的引导头正是从那个尾部取的 —— 源头没了，
    下一段必须标 **redo**。这是诚实的代价，不能假装没事：不标的话下一段会
    对着一个不存在的引导头继续跑，接缝处直接撕裂。本段自己标 **trim**：
    成片要重拼一次才会真的短下来，但已经生成的潜空间照样能用，不必重采样。
    """
    meta = load_clip(name, cid)
    if meta is None:
        return None
    old = int(meta.get("frames") or 0)
    new = snap_frames(frames)
    if new == old:
        return {"clip": meta, "changed": False, "pending": meta.get("pending"),
                "next": None, "next_pending": None}

    # 没有**段落存档原稿**（clip_XXXX.latent.safetensors，只有「📦 H3 段落
    # 存档」节点会写）的段：加长**不再硬拒**（20261013）。
    #
    # 硬拒的历史后果：用户实际用的是「💾 H3 续拍存档」（写的是
    # clip_XXXX.cont.safetensors），而这个判定只认 📦 那一个文件名 —— 于是
    # 除第 1 段外，其余各段**永远**判定"无原稿"，一拉长就被拒、时间轴弹回
    # 原位，"中间的段加不了时长"就是这么来的。
    #
    # 没有原稿失去的只是 **grow 那条捷径**（尾部钉住、后面各段不用重跑），
    # 不等于**不能改时长**：老老实实把本段标 redo（整段重抽），本段尾部会变，
    # 下一段的头钉正是取自本段尾部，所以下一段也一并标 redo。用户把一段从
    # 3 秒拉到 10 秒，本来就是打算重抽的 —— 拦住他才是错。
    no_stock = bool(new > old and not _has_latent_on_disk(name, meta))

    order = (load_chain(name) or {}).get("order") or []
    i = order.index(cid) if cid in order else -1
    nxt = order[i + 1] if 0 <= i < len(order) - 1 else None

    # 从未渲染过的规划段（无成片路径、无实测帧；外部导入的 mp4 会有 film，
    # 不在此列）：改时长只是改计划，没有任何已生成画面要重采样或重拼，直接
    # 写帧长即可 —— 不标 grow/trim/redo、不牵连下一段。否则用户在右侧设置
    # 卡把一段还没抽过的片从 10 秒改成 4 秒，轴上立刻挂"待重拼/下一段待重跑"
    # 的标记，纯属吓唬人（20261012）。
    if not meta.get("film") and int(meta.get("film_frames") or 0) == 0:
        meta = update_clip(name, cid, {"frames": new})
        store.log_event(chain_dir(name), "resize", cid,
                        {"from": old, "to": new, "planned": True}, "monitor")
        ws_broadcast("clip_resized", name, cid)
        return {"clip": meta, "changed": True, "from": old, "to": new,
                "planned": True, "pending": meta.get("pending"),
                "next": None, "next_pending": None}

    if new > old:
        # 有原稿 → grow（尾部钉住，后面各段不动）。
        # 无原稿 → redo（整段重抽）；本段尾部会变，下一段的头钉取自本段尾部，
        #          所以下一段也跟着标 redo —— 这是诚实的代价，总比"改了却不说"好。
        pending, next_pending = ("redo" if no_stock else "grow"), \
                               ("redo" if (no_stock and nxt) else None)
    else:
        # 缩短**本段也要标**，标的是 trim（待裁剪）而不是 redo。
        #
        # 原因：本段的成片是「到本段为止的累积成片」，缩短之后它得重拼一次
        # 才会真的短下来 —— 磁盘上那一版还是 149 帧。可本段已经生成的潜空间
        # 照样能用（只是少用后面几帧），不需要重新采样，所以代价和「加长」
        # 完全不是一个量级：trim = 重拼一下，grow/redo = 重新跑模型。
        #
        # 以前这里给本段标 None（等于"没事了"），于是「导出成片」的清单看不
        # 出这一段其实已经改过 —— 用户会拿到一条比档案设定长的片子，还以为
        # 导出坏了。分档标出来，代价也一并说清。
        pending, next_pending = "trim", ("redo" if nxt else None)

    meta = update_clip(name, cid, {"frames": new, "pending": pending})
    next_meta = update_clip(name, nxt, {"pending": next_pending}) if nxt else None
    store.log_event(chain_dir(name), "resize", cid,
                    {"from": old, "to": new, "pending": pending,
                     "next": nxt, "next_pending": next_pending}, "monitor")
    # 帧长改了必须广播（20261012）：时间轴段块宽度读的是档案 frames，右侧设置
    # 区改时长（不只拖段块边缘）也走本函数；不广播，轴只能等下一次轮询/手动刷新。
    ws_broadcast("clip_resized", name, cid)

    return {"clip": meta, "changed": True, "from": old, "to": new,
            "pending": pending, "next": nxt, "next_pending": next_pending,
            "next_clip": next_meta,
            # 没有段落存档原稿、只能整段重抽（不再拒绝）：前端要说清代价，
            # 否则用户只会看到"能拉长了"却不知道后面一段也要跟着重跑。
            "no_stock": no_stock,
            "reason": ("这一段没有「📦 H3 段落存档」原稿，加长后要**整段重抽**；"
                       "它后面那一段的头钉取自本段尾部，也一并标了「待重跑」"
                       "（想只重抽本段、不动后面，请渲染时接上「📦 H3 段落存档」节点）"
                       if no_stock else "")}


@chainlocked
def set_current(name, cid):
    """把某段设为「当前段」——三个节点共享的编辑目标就是它。

    导演台切段时立刻写一次（不等执行），文案台与媒体架读它，于是
    「切到哪一段，两边就跟着切到哪一段」。20261012 起落盘后经 WebSocket
    广播 bz.chain.event(current)，前端 bz_store 毫秒级跟随，不再靠轮询。
    """
    chain = load_chain(name)
    if cid and load_clip(name, cid) is None:
        return None
    chain["current"] = cid or None
    save_chain(name, chain)
    store.log_event(chain_dir(name), "current", cid, {}, "cast")
    ws_broadcast("current", name, cid)
    return chain


@chainlocked
def set_locked(name, cid, locked):
    """锁定一段。

    锁的是**编辑目标**：钉住之后，再点别的段，文案台与媒体架继续写锁定的
    这一段（不跟着走）。同时也钉住时长——把手消失，拖不动。

    为什么是"锁编辑目标"而不是"锁内容"：长片的实际用法是「我正盯着第 3 段
    调，别让我一点时间轴就跳走」。锁内容（不许重抽）反而没用——重抽本来就
    是手动触发的，不存在误触。
    """
    meta = load_clip(name, cid)
    if meta is None:
        return None
    out = update_clip(name, cid, {"locked": bool(locked)})
    store.log_event(chain_dir(name), "lock", cid, {"locked": bool(locked)}, "cast")
    return out


def set_seam(name, cid, side, score, note=None):
    """记一段接缝的评分（side: 'left' / 'right'）。

    分数由续接合成在真正拼的时候算（那里才同时看得到两段），这里只负责存，
    面板拿它把分数画在两段之间。
    """
    if side not in ("left", "right"):
        return None
    patch = {"seam_%s" % side: None if score is None else float(score)}
    if note is not None:
        patch["seam_%s_note" % side] = str(note)
    return update_clip(name, cid, patch)


# ---- 版本（takes）-------------------------------------------------------
# 一段可以抽很多次。每次重抽存成一个新版本（潜空间 .t2/.t3…，成片同名），
# 旧的一版原样留在磁盘上 —— 于是「换一个镜头」变成「在这一段里选第几版」，
# 而不是「把这一段覆盖掉、后面全部跟着重跑」。
#
# 版本号就是文件名里的 .tN，与 clip_latent / clip_film 同一套命名口径：
# 磁盘上看到一个 clip_0003.t2.latent.safetensors，就是第 3 段第 2 版，
# 不需要另开一份索引去记它。

def take_count(name, cid, meta=None):
    """这一段实际抽过几个版本（以磁盘为准）。

    档案里的 takes[] 只是「我们登记过什么」，磁盘才是「现在真有什么」——
    用户可能手动删过，也可能手动拷进来一个。数磁盘，面板才不会显示一个
    点进去 404 的版本。
    """
    m = meta if meta is not None else load_clip(name, cid)
    if m is None:
        return 0
    ts = {int(t) for t in (m.get("takes") or []) if str(t).isdigit() and int(t) > 0}
    ts.add(int(m.get("current_take") or 1))
    ts.add(1)
    n = 0
    for t in ts:
        if (os.path.isfile(clip_latent(name, cid, t))
                or os.path.isfile(clip_film(name, cid, t))):
            n += 1
    return n


def take_rows(name, cid):
    """这一段所有版本的清单 —— 面板「版本」菜单直接吃这个。"""
    meta = load_clip(name, cid)
    if meta is None:
        return None
    cur = int(meta.get("current_take") or 1)
    ts = {int(t) for t in (meta.get("takes") or []) if str(t).isdigit() and int(t) > 0}
    ts.add(cur)
    ts.add(1)
    rows = []
    for t in sorted(ts):
        lp, fp = clip_latent(name, cid, t), clip_film(name, cid, t)
        have_l, have_f = os.path.isfile(lp), os.path.isfile(fp)
        if not (have_l or have_f):
            continue                      # 档案记着、磁盘上没有 = 已经没了
        rows.append({
            "take": t, "current": t == cur,
            "latent": lp if have_l else None,
            "film": fp if have_f else None,
            "bytes": (os.path.getsize(lp) if have_l
                      else os.path.getsize(fp) if have_f else 0),
            "mtime": (os.path.getmtime(lp) if have_l
                      else os.path.getmtime(fp) if have_f else None),
        })
    return {"id": cid, "current": cur,
            # 注意不能写 ``meta.get("trim_frames") or DEFAULT_TRIM``：trim 合法值
            # 0（首段不承接）会被 ``0 or 22`` 吞成 22。只有缺字段才退回默认。
            "trim_frames": int(meta["trim_frames"])
            if meta.get("trim_frames") is not None else DEFAULT_TRIM,
            "takes": rows}


def tail_fingerprint(name, cid, take, trim=None):
    """这一段第 take 版**尾部窗口**的指纹。

    为什么只看尾部：下一段的引导头正是从这里取的。两版尾部指纹相同 = 下一段
    拿到的是一模一样的引导头 = 它的潜空间照样成立，换这一版不牵连后面。这是
    「换一段而不必把后面全换掉」在物理上唯一的判据 —— 不能靠猜，只能比。

    量化到 1e-3 再哈希：同一份画面必然同哈希，采样里 1e-4 级的浮点噪声不会
    把两个本该相同的尾部判成不同。取不到（没有潜空间档案 / safetensors 不
    可用）就返回 None，调用方必须按「无法验证」处理，不能当成「相同」。
    """
    path = clip_latent(name, cid, take)
    if not os.path.isfile(path):
        return None
    try:
        import hashlib
        import torch
        from safetensors.torch import load_file as _load
        data = _load(path)
    except Exception:
        return None
    t = data.get("video")
    if t is None:
        for k in data.keys():
            t = data[k]
            break
    if t is None:
        return None
    try:
        n = int(trim or DEFAULT_TRIM)
        w = t[-n:] if (n > 0 and t.shape[0] > n) else t
        q = torch.round(w.float() * 1000.0).contiguous()
        return hashlib.sha1(q.numpy().tobytes()).hexdigest()[:16]
    except Exception:
        return None


def set_take(name, cid, take, trim=None):
    """把这一段换成第 take 版，并把代价说清楚。

    换版本有两种结果，代价差一个量级，必须分开报：

      · 两版尾部窗口指纹**相同** —— 下一段的引导头没变，它的潜空间照样成立。
        只有「到各段为止的累积成片」是旧的，重拼一次即可（rejoin：解码+拼接，
        不用重新采样）。这正是重抽时钉住尾帧想换来的东西。
      · 指纹**不同** —— 下一段的引导头来自一个已经不存在的尾部，它必须重抽
        （redo）。这是诚实的代价：不标的话它会接着一个不存在的引导头跑，
        接缝处直接撕裂。

    指纹取不到时 same=None：照实说「没法验证」，而不是显示一个绿色的
    「不影响后面」—— 假的安全感比明确的未知更害人。
    """
    meta = load_clip(name, cid)
    if meta is None:
        return None
    rows = take_rows(name, cid) or {}
    known = [r["take"] for r in rows.get("takes", [])]
    try:
        take = int(take)
    except (TypeError, ValueError):
        return {"ok": False, "error": "版本号必须是整数"}
    if take not in known:
        return {"ok": False, "changed": False,
                "error": "这一段没有第 %d 版（现有：%s）"
                         % (take, "、".join(str(t) for t in known) or "无")}

    old = int(meta.get("current_take") or 1)
    if take == old:
        return {"ok": True, "changed": False, "take": take, "from": old,
                "tail_same": True, "next": None, "next_pending": None}

    trim_n = int(trim or meta.get("trim_frames") or DEFAULT_TRIM)
    fp_old = tail_fingerprint(name, cid, old, trim_n)
    fp_new = tail_fingerprint(name, cid, take, trim_n)
    same = None if (fp_old is None or fp_new is None) else (fp_old == fp_new)

    order = (load_chain(name) or {}).get("order") or []
    i = order.index(cid) if cid in order else -1
    nxt = order[i + 1] if 0 <= i < len(order) - 1 else None
    after = order[i + 1:] if i >= 0 else []

    patch = {"current_take": take}
    # 这一版自己带成片（重抽时 Join 写出来的就是 .tN.mp4）→ 成片直接指过去，
    # 这一段不算待办；否则成片还是旧版那一版，得重拼。
    own_film = clip_film(name, cid, take)
    if os.path.isfile(own_film):
        patch["film"] = own_film
        patch["pending"] = None
    else:
        patch["pending"] = "rejoin"
    meta = update_clip(name, cid, patch)
    # 这版自己的生成参数（画布等）。前端拿它把面板回滚到"当时怎么生成的"，
    # 版本与参数从此绑定 —— 切到哪版，面板就是哪版的画布。
    tp = (meta.get("take_params") or {}).get(str(take)) or {}

    next_pending = None
    if same is False and nxt:
        next_pending = "redo"
        update_clip(name, nxt, {"pending": "redo"})
        after = after[1:]
    # 下游每一段的累积成片都含这一段 —— 换版之后全是旧的。只标「重拼」，
    # 且不降档：已经在等「重抽」的段不会被改回「重拼」。
    for c in after:
        _raise_pending(name, c, "rejoin")
    store.log_event(chain_dir(name), "take_swap", cid,
                    {"from": old, "take": take, "tail_same": same,
                     "next_pending": next_pending}, "monitor")

    return {"ok": True, "changed": True, "from": old, "take": take,
            "takes": known, "trim": trim_n,
            "take_params": tp,
            "tail_same": same,
            "tail_note": (None if same is None else
                          "尾部 %d 帧一致 —— 下一段的引导头没变" % trim_n if same else
                          "尾部 %d 帧变了 —— 下一段的引导头来自旧版本" % trim_n),
            "next": nxt, "next_pending": next_pending,
            "rejoined": after, "clip": meta}


# 待办档次的轻重。只用于「不降档」比较：一段已经在等重抽（redo/grow），
# 就不能因为一次换版本把它改回「只要重拼」——那等于把更重的代价藏起来。
_PENDING_RANK = {"trim": 1, "rejoin": 2, "grow": 3, "redo": 3}


def _raise_pending(name, cid, kind):
    """把一段的待办提到 kind；已有待办更重就不动它。"""
    meta = load_clip(name, cid)
    if meta is None:
        return None
    cur = meta.get("pending")
    if cur and _PENDING_RANK.get(cur, 0) >= _PENDING_RANK.get(kind, 0):
        return meta
    return update_clip(name, cid, {"pending": kind})


def drop_take(name, cid, take):
    """删掉一个版本（潜空间 + 成片一起删）。

    当前版本不给删：时间轴上正挂着它，删了这一段就没有可用潜空间，后面各段
    的引导头也断了。要删就先切到别的版本。
    """
    meta = load_clip(name, cid)
    if meta is None:
        return None
    try:
        take = int(take)
    except (TypeError, ValueError):
        return {"ok": False, "error": "版本号必须是整数"}
    cur = int(meta.get("current_take") or 1)
    if take == cur:
        return {"ok": False, "error": "不能删当前正在用的第 %d 版 —— "
                                      "先切到别的版本再删。" % take}
    gone = []
    for p in (clip_latent(name, cid, take), clip_film(name, cid, take)):
        if os.path.isfile(p):
            try:
                os.remove(p)
                gone.append(os.path.basename(p))
            except OSError as e:
                return {"ok": False, "error": "删不掉 %s：%s"
                                              % (os.path.basename(p), e)}
    takes = [int(t) for t in (meta.get("takes") or []) if str(t).isdigit()]
    meta = update_clip(name, cid, {"takes": [t for t in takes if t != take]})
    store.log_event(chain_dir(name), "take_drop", cid,
                    {"take": take, "removed": gone}, "monitor")
    return {"ok": True, "take": take, "removed": gone, "clip": meta}


@chainlocked
def set_film(name, cid, path, frames=None, seam=None, head=None):
    """把「生成完这一段之后的成片」登记到这一段上。

    语义很关键：不是「这一段的成片」，而是「到这一段为止的成片」。
    所以时间轴上点第 k 段，监视器播的就是前 k 段合起来的片子。

    ``head`` 是契约：这条片子开头有几帧是承接/旧内容（见 _compute_film_head）。
    收片时算一次写死，下游不再猜。
    """
    patch = {"film": path or None}
    if frames is not None:
        patch["film_frames"] = int(frames)
    if head is not None:
        patch["film_head"] = int(head)
    if seam is not None:
        patch["film_seam"] = seam
    out = update_clip(name, cid, patch)
    store.log_event(chain_dir(name), "film_set", cid,
                    {"path": path, "frames": frames}, "backend")
    return out


def _has_latent_on_disk(name, c):
    """has_latent 的真实判定：档案说有还不够，当前版本的 latent 文件得真在。

    只纠「渲染过（有成片路径）却没存档」这一种 —— 从未渲染的段本来就没有
    latent 文件，不能因此把它标成无原稿（那是外部导入段的语义）。

    字段缺失（老档案还没有这个字段）按"有档案"处理：直接 `not c.get(...)`
    会把 None 和 False 混为一谈，老工程一升级就全被标成"无原稿"，加长/重抽
    全被拒 —— 升级即瘫痪，而且看不出为什么。
    """
    _hl = c.get("has_latent")
    if _hl is not None and not _hl:
        return False
    if not c.get("film"):
        return True
    take = max(1, int(c.get("current_take") or 1))
    # 实时判定：用户手动删 latent 后加长/重抽拦截语义必须立刻生效。
    return os.path.isfile(clip_latent(name, c.get("id"), take))


def film_summary(name):
    """整片的成片视图 —— 导演台监视器 / 时间轴直接吃这个。

    返回每段在全片里的起止帧（clip_from/clip_to），前端拿它画时间轴；
    `film` 是该段为止的累积成片；`playable` 说明这份成片还在不在磁盘上
    （用户可能手动清了 output，不能让面板对着一个 404 播）。

    性能（20261012）：json 读走 _read_json 的 mtime 缓存（命中≈字典查找），
    本函数整体在路由里经线程池执行（不堵事件循环）。**不做结果 TTL 缓存** ——
    playable/head 必须对用户手动删片即时反应（_film_selftest 红线），而省下来
    的帧账计算只是纯内存算术；重复请求由前端 refresh 闸门与 self-heal 合并。
    """
    info = summary(name)
    fps = float(info.get("fps") or 24)
    # 段序：pin_mode 判断「循环」要拿它比轴向位置（尾段比头段还靠前 = 绕回）
    _order = [str(x.get("id")) for x in (info.get("clips") or [])]
    try:
        _trans = transitions_of(name)
    except Exception:
        _trans = {}
    at = 0
    rows = []
    for i, c in enumerate(info["clips"]):
        n = int(c.get("frames") or 0)
        # 当前版自己的生成参数（adopt_film 落的，20261010）：画布宽高跟着
        # 版本走。没记录的段（老渲染）是 None —— 前端据此不回滚面板。
        _tp = (c.get("take_params") or {}).get(
            str(max(1, int(c.get("current_take") or 1)))) or {}
        # film_frames 自愈（20261010）：adopt_film 早年不写它，存量档案恒 0。
        # 现场探一次实测帧数并**写回档案**——此后这一段走缓存零开销，前端
        # 的累积/单段判据从此有地面真相。
        ff = int(c.get("film_frames") or 0)
        _film_path = c.get("film")
        # playable 必须实时：用户手动删掉成片后轴上要立刻转"不可播"，
        # 目录清单有 1.5s TTL，这里不能用它（_film_selftest 红线）。
        # 单次 stat 在线程池路由里跑、film_summary 又有 0.8s TTL 压频，
        # N 段 N 次 stat 可接受。
        _film_on_disk = bool(_film_path) and os.path.isfile(_film_path)
        if ff <= 0 and _film_on_disk:
            ff = probe_film_frames(_film_path)
            if ff > 0:
                try:
                    update_clip(name, c.get("id"), {"film_frames": ff})
                    c["film_frames"] = ff        # 本轮 summary 直接用上
                except Exception:
                    pass
        # 净长模型（20261010 重做）：第 k 段视频里开头 trim 帧是上一段末尾的
        # 重绘（承接重叠），它们在时间轴上**归属上一段的展示区** —— 段块只画
        # 本段的**新画面净长**（frames - trim），段与段首尾干净相接，连接处
        # 不再重复/叠盖。trim 至少给本段留 1 帧。
        trim = min(int(c.get("trim_frames") or 0), max(0, n - 1)) if i > 0 else 0
        net = max(1, n - trim)
        # 尾钉的对称账（20261010bd）：钉在尾部时末尾那几帧是**后一段**开头的
        # 重绘，同样不属于本段。默认 0 → 与改之前逐字相同，往后接/首段不受影响。
        tail = min(int(c.get("trim_tail") or 0), max(0, net - 1))
        net = max(1, net - tail)
        # 契约迁移（20261010ba）：老档案没有 film_head。这里用同一笔账算一次
        # 并写回档案 —— 从此这一段永久走契约，不再判断它是什么形态。
        head = c.get("film_head")
        if ff > 0:
            _want_head = _compute_film_head(at, at + net, ff, head_trim=trim)
            # 单段片（成片盖不到本段在全片里的净末）：开头旧帧**恒等于头部
            # trim**。尾钉功能上线早期用 ff-净长 反推，把末尾的尾钉也算进了
            # 开头 —— 带尾钉的补桥 / 往前接 / 循环 / 断链残留段 film_head 偏大
            # 一个 tail，导出裁切整体错位。这里与 film_frames 同口径自愈写回；
            # 累积片（ff 盖得过净末）走绝对坐标、没有这个问题，不去动它。
            _single = ff < (at + net)
            if head is None or (_single and int(head or 0) != int(_want_head)):
                head = _want_head
                try:
                    update_clip(name, c.get("id"), {"film_head": int(head)})
                except Exception:
                    pass
        head = int(head or 0)
        _pins = pins_of(name, c.get("id"))
        _cid = str(c.get("id"))
        _head_unlink = bool(_cid in _trans
                            and (_trans.get(_cid) or {}).get("type") == "unlink")
        # 显示用接法：断链（头部不接续）时把头钉摘掉再判 —— 往后接变独立、
        # 补桥/循环只剩尾钉（显示成往前接）。真实 pin_mode（生成血统）仍给全量。
        _disp_pins = [p for p in _pins if str(p.get("at")) != "head"] \
            if _head_unlink else _pins
        rows.append({
            "id": c.get("id"), "index": i + 1,
            "name": c.get("name") or "第 %d 段" % (i + 1),
            # 认段靠它：档案里的 shot 是「分段身份 uid」，工程对象里的 shot.id
            # 就是同一串。前端拿它把「工程预设」和「档案事实」对齐做 mtime 校验。
            "shot": c.get("shot") or "",
            # 档案的写入时间戳（save_clip 每次都刷新）。前端拿它比对工程对象的
            # 同步戳，判断「档案是不是被时间轴/后端改得更新」。
            "updated": c.get("updated"),
            # frames = 这段视频的完整长度（含承接重叠）；块宽/全片长度用的是
            # 扣掉重叠后的净长（clip_to - clip_from）。
            "frames": n,
            "clip_from": at, "clip_to": at + net,
            "film": c.get("film"),
            "film_frames": int(c.get("film_frames") or 0),
            # 契约：这条成片开头有几帧是承接（本段新画面从这里开始）。
            # 前端的时间轴定位、播放落点、保存成片裁切**全部读这一个数**。
            "film_head": head,
        # 承接窗口（方案 B 地基）：这段生成时钉住了谁的哪一段画面。
        # 时间轴据此显示它是往后接/往前接/补桥/循环，四种接法共用一套。
        "pins": _pins,
        # 接法名由后端一次算定（source/extend/prepend/bridge/loop）—— 前端
        # 只管显示，不再自己判一遍；两处判据稍有不同的话，就是"轴上显示补桥、
        # 跑出来是往后接"这种最难查的错位。
        "pin_mode": pin_mode(_pins, _order),
        "head_unlink": _head_unlink,
        "disp_pin_mode": pin_mode(_disp_pins, _order),
            "film_seam": c.get("film_seam"),
            # 接缝评分：只在真正拼过的时候才有（续接合成算的）。第 1 段没有
            # 左边可接的东西，所以是 None —— 面板据此不画，而不是画个 0。
            "seam_left": c.get("seam_left"),
            "seam_left_note": c.get("seam_left_note"),
            "seam_right": c.get("seam_right"),
            "seam_right_note": c.get("seam_right_note"),
            # 实时 isfile（理由同 _film_on_disk，不用目录清单缓存）
            "playable": bool(c.get("film")) and os.path.isfile(c.get("film") or ""),
            # has_latent 以磁盘为准：档案标了 true、但当前版本的潜空间文件
            # 不在（渲染时工作流没接「📦 段落存档」），重抽/加长的承诺兑现
            # 不了 —— 如实报 false，时间轴才会给它"无原稿"的灰边、拖加长
            # 才会被拒。只纠「渲染过（有成片）却没档案」这一种：从未渲染的
            # 段本来就没有 latent 文件，不能因此把它标成"外部导入"。
            "has_latent": _has_latent_on_disk(name, c),
            "pending": c.get("pending"),
            "locked": bool(c.get("locked")),
            # 版本：这一段抽过几个、现在用第几个。时间轴块上显示 v(k/n)，
            # 右键菜单据此列出可切换的版本。以磁盘为准（见 take_count）。
            "take": max(1, int(c.get("current_take") or 1)),
            "takes": take_count(name, c.get("id"), c),
            # 这一段当初是用多少帧的窗口接上一段的（0 = 没有上一段）。
            # 输出的是**净长模型里的有效值**：min(设定的窗口, 本段帧数 - 1)，
            # 与块宽（clip_to - clip_from = frames - trim）同一套口径。
            "trim_frames": trim,
            # 尾钉剪掉几帧（往前接 / 补桥 / 循环才有）。时间轴上本段的新画面
            # 是 [头, 尾) 之间那一截，块宽已经把它扣掉了。
            "trim_tail": tail,
            # 当前版的画布（版本绑定参数，20261010）。None = 没记录。
            "w": _tp.get("w"), "h": _tp.get("h"),
        })
        at += net
    # 播放头默认落在最后一段有成片的地方
    head = None
    for r in reversed(rows):
        if r["playable"]:
            head = r["id"]
            break
    return {
        "name": name, "fps": fps,
        # 全片长度 = 扣完各段承接重叠后的**净长**（= 最后一段的 clip_to），
        # 与时间轴布局同一套口径 —— 用原始 sum 会比轴长出所有重叠帧，
        # scrub 的全片坐标、时间码就全错位了。
        "total_frames": at,
        "trim_frames": info.get("trim_frames", DEFAULT_TRIM),
        "seconds": (at / fps) if fps else 0.0,
        "clips": rows,
        # 各段的接法（哪种 pins 配置）。要整条 order 才认得出循环，所以在
        # clips 之外单独给一份，省得前端自己再排一遍顺序。
        "pin_modes": {r["id"]: pin_mode(r["pins"], [x["id"] for x in rows])
                      for r in rows},
        "head": head,
        # 当前编辑段（chain.current）：媒体架/文案台跟随切段就认它，
        # 20261012 起 bz_store 一次拉取拿全，不必再单查 /current。
        "current": info.get("current"),
        "missing": [r["id"] for r in rows if not r["playable"]],
        # 段落 json 读不出来的段（被手动删了 / 写坏了）。面板要报，不然用户
        # 只会看到"第 3 段不见了"，而不知道是档案坏了。
        "broken": info.get("broken") or [],
    }


def _safe_file_part(s, fallback="未命名"):
    """文件名里能用的那部分。与 chain_dir 同一套过滤，别各写一份。"""
    t = "".join(c for c in (s or "") if c not in '\\/:*?"<>|').strip()
    return t or fallback


def export_plan(name, at=None):
    """「导出成片」之前要跟用户说清的那张清单。

    导出读的是磁盘上**已经生成好**的成片，不是重新渲染一遍。所以点下去之前
    必须先把两件事摆出来，否则用户拿到一条跟想象中不一样的片子，只会以为
    导出坏了：

      1. 片子到哪儿为止 —— 只有生成过的段才有成片，最后一段没跑过，导出就
         只到倒数第二段。这是最容易踩的坑。
      2. 改动进没进片子 —— 段上挂着的待办是**档案里的设定**，成片还是改之前
         那一版。不跑一次它不会自己变。

    刻意不在这里自动去跑：导出是个读盘动作，偷偷重渲染会让「点一下导出」
    变成「跑了二十分钟」。列清楚，让用户自己决定。

    at 为空 = 导出能拿到的最新版（最后一段还拿得到成片的段）。
    """
    fs = film_summary(name)
    rows = fs["clips"]
    fps = float(fs.get("fps") or 24) or 24.0
    broken = fs.get("broken") or []

    def brief(r):
        return {"index": r["index"], "name": r["name"],
                "frames": r["frames"], "playable": r["playable"]}

    if at:
        pick = next((r for r in rows if r["id"] == at), None)
        if pick is None:
            return {"name": name, "ok": False, "blocker": "no_such_clip",
                    "beyond": [], "pending": [], "broken": broken,
                    "message": "没有这一段：%s" % at}
    else:
        # 默认导出「能拿到的最新版」——不是最后一段。最后一段可能压根没跑过。
        pick = next((r for r in reversed(rows) if r["playable"]), None)

    if pick is None:
        return {"name": name, "ok": False, "blocker": "no_film",
                "beyond": [brief(r) for r in rows], "pending": [],
                "broken": broken, "total_clips": len(rows),
                "message": "这部片子还没有任何一段生成过成片 —— 先跑一次再来导出。"}

    cut = pick["index"]
    frames = int(pick.get("film_frames") or 0)
    return {
        "name": name, "ok": True, "blocker": None,
        "at": pick["id"], "at_index": cut, "at_name": pick["name"],
        "path": pick.get("film") or "",
        "frames": frames,
        "seconds": (frames / fps) if fps else 0.0,
        "file": "%s_到第%d段.mp4" % (_safe_file_part(name), cut),
        "total_clips": len(rows),
        # 导出点之后的段：全都进不了这条片子。
        "beyond": [brief(r) for r in rows if r["index"] > cut],
        # 导出点之内还挂着待办的段：设定改了，成片没跟上。
        "pending": [{"index": r["index"], "name": r["name"],
                     "kind": r["pending"],
                     "text": PENDING_TEXT.get(r["pending"], r["pending"]),
                     "frames": r["frames"]}
                    for r in rows if r["index"] <= cut and r.get("pending")],
        "broken": broken,
        "message": "",
    }


# ---------------- 保存成片：整轴拼接导出（20261010）----------------
# 「保存成片」与「导出成片」不是一回事，别再混：
#   · 导出成片（export_plan / /chain/export）：把**某一段自己的成片文件**下载走；
#   · 保存成片（export_film_plan / export_film / /chain/export_film）：把时间轴
#     上**所有段**按净长拼成一条完整视频写进档案目录。连接处裁掉每段头部的
#     承接重叠帧（净长模型：第 k 段视频开头 trim 帧是上一段末尾的重绘，在成片
#     里归属上一段），成片总长 = 最后一段的 clip_to，与时间轴布局同一套口径。
_FILM_EXPORT_LOCK = threading.Lock()   # 同一片子的拼接串行化：别让两次点击互相踩临时文件


def _join_panel():
    """取播放版 / 无损拼接所在的模块。

    延迟导入：那个模块 import 了 server 与 aiohttp（要注册路由），而本模块会被
    别的链路在 ComfyUI 起来之前就 import —— 顶层硬引会把它拖进不该有的时机。
    取不到（旧环境 / 单独跑自检）返回 None，调用方退回重编码路径。
    """
    try:
        from . import csglide_join_panel as m            # noqa: F401
        return m
    except Exception:
        try:
            import csglide_join_panel as m               # noqa: F401
            return m
        except Exception:
            return None


def pins_of(name, cid):
    """这一段的承接窗口（生成时钉住了谁的哪一段）。空 = 独立生成的首段。"""
    c = load_clip(name, cid) or {}
    p = c.get("pins")
    return [dict(x) for x in p] if isinstance(p, list) else []


@chainlocked
def set_pins(name, cid, pins):
    """改写这一段的承接窗口。**只有**动手生成之前改它才有意义 —— 生成完
    再改等于篡改血统，接法跟实际画面对不上。"""
    out = update_clip(name, cid, {"pins": [dict(x) for x in (pins or [])]})
    store.log_event(chain_dir(name), "pins", cid,
                    {"pins": [dict(x) for x in (pins or [])]}, "cast")
    return out


def pin_mode(pins, order=None):
    """这段是哪种接法 —— 给时间轴显示用，四种接法共用一套 pins。

    ``source`` 独立首段 / ``extend`` 往后接 / ``prepend`` 往前接 /
    ``bridge`` 补桥 / ``loop`` 循环（补桥的一种，但绕回了开头）。
    """
    heads = [p for p in (pins or []) if p.get("at") == "head"]
    tails = [p for p in (pins or []) if p.get("at") == "tail"]
    if heads and tails and order:
        # 绕回：尾端钉的那段在轴向位置比头端钉的还靠前 → 首尾相接的循环
        try:
            i_h = order.index(heads[-1].get("from"))
            i_t = order.index(tails[-1].get("from"))
            if i_t < i_h:
                return "loop"
        except (ValueError, AttributeError):
            pass
        return "bridge"
    if heads and tails:
        return "bridge"
    if tails:
        return "prepend"
    if heads:
        return "extend"
    return "source"


def transitions_of(name):
    """整片的接缝状态映射：后一段 cid -> {"type":"unlink"}（断链）。
    没有记录 = 链接（默认）。成片视觉转场（溶解/淡变）已移除。"""
    ch = load_chain(name)
    t = ch.get("transitions")
    return dict(t) if isinstance(t, dict) else {}


@chainlocked
def set_transition(name, cid, t):
    """写一条接缝状态：unlink（断链、播放不连播）记下；cut / 空 = 删除（回链接）。
    只有这两态。返回新映射。"""
    ch = load_chain(name)
    tt = ch.get("transitions")
    if not isinstance(tt, dict):
        tt = {}
    if str((t or {}).get("type") or "cut") == "unlink":
        tt[str(cid)] = {"type": "unlink", "overlap": 0}
    else:
        tt.pop(str(cid), None)
    ch["transitions"] = tt
    save_chain(name, ch)
    store.log_event(chain_dir(name), "transition", cid, t, "monitor")
    ws_broadcast("transition", name, cid)
    return tt


def pins_from_mode(name, cid, mode, window=22, src="", src2=""):
    """把「接法」翻译成 pins —— :func:`pin_mode` 的逆。

    时间轴上选一个接法，后端照它生成，所以这两头必须严格互为逆运算，
    否则「轴上显示补桥、跑出来是往后接」。``take`` 记 0 = 取源段当前版，
    执行时再定（换版本不用改 pins）。
    """
    order = [str(c) for c in (load_chain(name).get("order") or [])]
    w = int(window or 22)

    def pin(who, at):
        return {"from": who, "take": 0, "window": w, "at": at}

    if not mode or mode == "source":
        return []
    if mode == "extend":
        return [pin(src, "head")] if src else []
    if mode == "prepend":
        return [pin(src, "tail")] if src else []
    if mode == "bridge":
        if not (src and src2):
            return []
        return [pin(src, "head"), pin(src2, "tail")]
    if mode == "loop":
        # 绕回：头钉**最后一段**的尾巴、尾钉**第一段**的头，于是这一段既
        # 接得上末尾也接得上开头 —— 补桥的一种，只是两个父一个在轴首一个在轴尾。
        first = order[0] if order else ""
        last = order[-1] if order else ""
        if not (first and last and first != last):
            return []
        return [pin(last, "head"), pin(first, "tail")]
    return []


def head_parent(name, cid, fallback_prev=True):
    """这一段的**头钉钉的是谁** —— 没有头钉（独立首段 / 纯往前接）返回 ``""``。

    它是「本段开头那几帧是从谁的尾巴重绘来的」的唯一答案，也是上一段决定
    **自己尾部要钉多少帧**的唯一依据（见 csglide_cast._attach_pin）。

    为什么非要问它、不能只看「下一段的 trim_frames」：补桥的那一段同样有
    ``trim_frames``（它头钉钉了 22 帧），但钉的是**别人** —— 本段若照着把
    尾部也钉 22 帧，就是凭空少掉一截能改的画面，而且这种"少掉"不报错，
    只表现为"重抽了半天中间没怎么变"。

    没有 pins 的老工程退回传统语义（轴上前一段）—— 那时只有往后接一种接法，
    「下一段」就是「接本段的那一段」，两者本来就是同义词。
    """
    pins = pins_of(name, cid)
    for p in pins:
        if p.get("at") == "head":
            return str(p.get("from") or "")
    if pins:
        # 写过 pins 却没有头钉 = 明确不接任何人（往前接 / 独立起头）
        return ""
    if not fallback_prev:
        return ""
    try:
        order = [str(c) for c in (load_chain(name).get("order") or [])]
        k = order.index(str(cid)) if str(cid) in order else -1
        return order[k - 1] if k > 0 else ""
    except Exception:
        return ""


def pin_sources(name, exclude=""):
    """能当承接窗口源片的段 —— 有续拍存档（latent）才钉得住。

    没 latent 的段（外部导入 / 渲染时没接「📦 段落存档」）给不出来，
    时间轴据此把它们从候选里划掉，而不是让用户选了才报错。
    """
    out = []
    for i, c in enumerate(summary(name)["clips"]):
        cid = c.get("id")
        if cid == exclude:
            continue
        take = max(1, int(c.get("current_take") or 1))
        p = ""
        try:
            cand = clip_cont(name, cid, take)
            if cand and os.path.isfile(cand):
                p = cand
        except Exception:
            p = ""
        if not p:
            try:
                tp = (c.get("take_params") or {}).get(str(take)) or {}
                ps = str(tp.get("cont_saved") or "")
                if ps and os.path.isfile(ps):
                    p = ps
            except Exception:
                p = ""
        if not p:
            continue
        out.append({
            "id": cid, "index": i + 1,
            "name": c.get("name") or "第 %d 段" % (i + 1),
            "take": take, "frames": int(c.get("frames") or 0),
        })
    return out


def _span_of(name, cid):
    """这一段在全片里的 ``[from, to)``（净长口径）与**头部**承接帧数。

    找不到返回 ``(0, 0, 0)``；否则 ``(from, to, head_trim)``。

    净长模型：重叠帧只记在接缝**右段**头部（第 1 段没有上一段，trim 记 0），
    段块宽度 = ``frames - trim - trim_tail``。整条 = 各段净长之和。第三个返回值
    是头部那截 trim（尾钉 trim_tail 不算在内），收片算 film_head 要用 ——
    单段片的开头旧帧只来自头部，不能把尾钉也计进去。
    """
    at = 0
    for i, c in enumerate(summary(name)["clips"]):
        n = int(c.get("frames") or 0)
        trim = min(int(c.get("trim_frames") or 0), max(0, n - 1)) if i > 0 else 0
        net = max(1, n - trim)
        net = max(1, net - min(int(c.get("trim_tail") or 0), max(0, net - 1)))
        if c.get("id") == cid:
            return at, at + net, trim
        at += net
    return 0, 0, 0


def _compute_film_head(clip_from, clip_to, film_frames, head_trim=None):
    """契约：这条成片**开头有几帧是承接/旧内容**，本段新画面从这之后开始。

    全片**只有这一处**判断「这条片子是什么形态」，而且只在收片/迁移时跑一次，
    结果写进档案的 ``film_head`` —— 下游（时间轴、播放、保存成片）一律读它。
    早先这同一笔账在后端算一遍、前端算一遍、播放版再算一遍，三处判据稍有出入
    就是「正向拖一个画面、反向拖另一个画面」这类鬼故事。

    两种形态（都是续接合成的**正常产物**，没有谁有病）：
      · 累积片（「到本段为止的整条」）：片子长到能盖住本段末尾，开头那
        ``clip_from`` 帧是前面几段 → head = clip_from；
      · 单段片（只含承接重叠）：head = 头部承接窗口 trim。

    ``head_trim`` 是这段净账里的**头部**承接帧数（trim_frames，首段 0），必须
    与尾钉（trim_tail）分开：单段片总长 = trim(头) + 净长 + tail(尾)，旧内容
    不只在开头、还可能在末尾（往前接 / 补桥 / 循环 / 断链后残留尾钉）。早先
    ``head = ff - 净长`` 把**头尾两端**的旧帧全算进了开头，带尾钉的段导出时
    裁切窗口整体往后错了一个 tail、末尾也没把尾钉去掉 —— 显式传 trim 后，
    头部只认头部那截。不传（老调用 / 老档案）退回旧算法，无尾钉时两者等价。
    """
    net = max(1, int(clip_to) - int(clip_from))
    ff = int(film_frames or 0)
    if ff <= 0:
        return 0
    if ff >= int(clip_to):
        return max(0, int(clip_from))
    if head_trim is not None:
        return max(0, int(head_trim))
    return max(0, ff - net)


def _seg_cut(r):
    """这一段进成片的裁切窗口 ``(skip, net)`` —— 直接读契约，不算第二遍。

    ``skip`` = 档案里落盘时写死的 ``film_head``。老档案没有这个字段的由
    ``film_summary`` 迁移补写（见那里的补写分支），走到这里必已有值。
    """
    net = max(1, int(r.get("clip_to") or 0) - int(r.get("clip_from") or 0))
    head = r.get("film_head")
    skip = int(head) if head is not None else 0
    return max(0, skip), net


def export_film_plan(name, at=None):
    """「保存成片」动手前的那张清单：拼到哪儿为止 + 哪些段进不了片子。

    字段刻意与 export_plan 同构 —— 前端一套 askExport / exportNotes 两处共用。
    frames / seconds 是**净长口径**（扣掉各段头部承接重叠），与时间轴总长一致，
    不是各段文件长度之和。
    """
    fs = film_summary(name)
    rows = fs["clips"]
    fps = float(fs.get("fps") or 24) or 24.0
    broken = fs.get("broken") or []

    def brief(r):
        return {"index": r["index"], "name": r["name"],
                "frames": r["frames"], "playable": r["playable"]}

    if at:
        pick = next((r for r in rows if r["id"] == at), None)
        if pick is None:
            return {"name": name, "ok": False, "blocker": "no_such_clip",
                    "beyond": [], "pending": [], "broken": broken,
                    "total_clips": len(rows),
                    "message": "没有这一段：%s" % at}
        cut = pick["index"]
    else:
        # 默认拼到最后一段有成片的地方 —— 不是最后一段（它可能压根没跑过）。
        cut = 0
        for r in rows:
            if r["playable"]:
                cut = r["index"]

    picks = [r for r in rows if r["index"] <= cut]
    common = {
        "name": name,
        "beyond": [brief(r) for r in rows if r["index"] > cut],
        "pending": [{"index": r["index"], "name": r["name"],
                     "kind": r["pending"],
                     "text": PENDING_TEXT.get(r["pending"], r["pending"]),
                     "frames": r["frames"]}
                    for r in picks if r.get("pending")],
        "broken": broken,
        "total_clips": len(rows),
    }

    if not any(r["playable"] for r in picks):
        return dict(common, ok=False, blocker="no_film",
                    missing=[brief(r) for r in picks if not r["playable"]],
                    message="这部片子还没有任何一段生成过成片 —— 先跑一次再来保存。")
    missing = [r for r in picks if not r["playable"]]
    if missing:
        # 中间缺段拼不成整条：跳过它会让连接处凭空跳戏，宁可明说。
        return dict(common, ok=False, blocker="missing_clips",
                    missing=[brief(r) for r in missing],
                    message="第 %s 段的成片不在磁盘上（被手动清了？）—— "
                            "中间缺段的片子拼不成整条，先把这几段补跑出来再保存。"
                            % "、".join(str(r["index"]) for r in missing))

    last = picks[-1]
    frames = int(last["clip_to"])          # 净长 = 最后一段的 clip_to
    out_file = "%s_成片.mp4" % _safe_file_part(name)
    # 帧数不够的段要点名：本段在成片里应有的位置（skip+net）超出了这条片子
    # 的实际长度 —— 说明这条片子不是"到这一段为止的整条"（比如接的是一段
    # 外来的源片，或重抽后长度变了）。裁出来的一截位置不对，拼进去就是重播
    # 或跳戏。不说的话用户只会觉得"成片怎么老重播"。
    short = []
    for r in picks:
        skip, net = _seg_cut(r)
        ff = int(r.get("film_frames") or 0)
        if ff and skip + net > ff:
            short.append({"index": r["index"], "name": r["name"],
                          "film_frames": ff, "want": skip + net})
    return dict(common, ok=True, blocker=None,
                at=last["id"], at_index=cut, at_name=last["name"],
                clips=len(picks), frames=frames,
                seconds=(frames / fps) if fps else 0.0,
                path=os.path.join(chain_dir(name), out_file),
                file=out_file, short=short,
                message="")


def _audio_track(path):
    """成片的第一条音轨 → (int16 [n, ch], 采样率)。没有音轨 / 读不出来 → (None, 0)。

    整条解进内存：几分钟片子的音轨也就几十 MB int16，换来后面按采样点精确
    裁头、按段首尾相接的省事。"""
    try:
        import numpy as np
        import av
    except Exception:      # pragma: no cover - 依赖缺失在 export_film 里统一报
        return None, 0
    try:
        with av.open(path) as c:
            if not c.streams.audio:
                return None, 0
            st = c.streams.audio[0]
            sr = int(st.rate or 48000)
            ch = int(st.layout.nb_channels)
            chunks = []
            for fr in c.decode(audio=0):
                a = fr.to_ndarray()
                if a.ndim == 1:
                    a = a[None, :]
                if a.dtype != np.int16:
                    f = a.astype(np.float32)
                    if f.max() > 1.5:          # 整型刻度只是存成了 float
                        f = f / 32768.0
                    a = np.clip(f * 32767.0, -32767, 32767).astype(np.int16)
                if a.shape[0] == 1 and ch > 1:      # 交织打包：[1, n*ch]
                    a = a.reshape(-1, ch)
                elif a.shape[0] == ch:              # 平面：[ch, n]
                    a = a.T
                chunks.append(np.ascontiguousarray(a, dtype=np.int16))
        if not chunks:
            return None, sr
        return np.concatenate(chunks, axis=0), sr
    except Exception:
        return None, 0


def export_film(name, at=None):
    """把时间轴上所有段拼成一条完整成片，写进档案目录，覆盖旧的那份。

    拼接口径与时间轴一致：第 k（k>0）段视频开头 trim 帧是上一段末尾的重绘
    （承接重叠），拼接时把它裁掉（第一段 trim=0 不裁）；音轨按同样的秒数裁。
    画面、声音都要重编码 —— trim 落点几乎不可能是关键帧，流复制会花屏。
    先写 .part 临时文件、拼完再改名：中途失败不会留下半条片子冒充成片。
    """
    plan = export_film_plan(name, at)
    if not plan.get("ok"):
        return plan
    try:
        import numpy as np
        import av
        from fractions import Fraction
    except Exception as e:      # pragma: no cover
        return {"ok": False, "blocker": "no_av",
                "message": "拼接需要 PyAV（ComfyUI 自带），当前环境读不到：%s" % e}

    fs = film_summary(name)
    rows = fs["clips"]
    picks = [r for r in rows if r["index"] <= plan["at_index"]]

    # 画布校验：跨段分辨率不一致（重抽时换过画布）拼出来必花屏 —— 明确报错，
    # 让用户去统一画布，而不是得到一条能播但看着不对的片子。
    dims = []
    for r in picks:
        try:
            with av.open(r["film"]) as c:
                vs = c.streams.video[0]
                dims.append((int(vs.width), int(vs.height),
                             float(vs.average_rate or 0)))
        except Exception:
            dims.append(None)
    bad = [r["index"] for r, d in zip(picks, dims) if d is None]
    if bad:
        return {"ok": False, "blocker": "unreadable_film",
                "message": "第 %s 段的成片文件读不出来（可能没写完 / 被占用）—— "
                           "重新渲染这几段再保存。"
                           % "、".join(map(str, bad))}
    if len(set((d[0], d[1]) for d in dims)) > 1:
        detail = "、".join("第%d段%dx%d" % (r["index"], d[0], d[1])
                          for r, d in zip(picks, dims))
        return {"ok": False, "blocker": "size_mismatch",
                "message": "各段分辨率不一致，拼不成一条片子（%s）—— "
                           "把画布统一后重跑不一致的段。" % detail}

    out_fps = dims[0][2] or float(fs.get("fps") or 24) or 24.0

    # 帧数够不够：某段的成片比它应有的净长还短，裁完就少写帧，视频比音频短
    # —— 又是一条音画错开的片子。动手之前先把话说清楚，别让用户在播放器里
    # 自己去发现声音对不上嘴。film_frames 是档案里记的实测帧数（film_summary
    # 会现探一次并写回），拿它当依据不必再解一遍。
    short = []
    for r in picks:
        skip, net = _seg_cut(r)
        ff = int(r.get("film_frames") or 0)
        if ff and skip + net > ff:
            short.append("第%d段只有%d帧、要%d帧" % (r["index"], ff, skip + net))
    if short:
        return {"ok": False, "blocker": "short_film",
                "message": "有段的成片帧数不够（%s）—— 重跑这几段再保存，"
                           "缺帧拼出来音画会错开。" % "、".join(short)}

    # 无损路径（20261010ba）：各段先掐出「本段净长」那一段（播放版），再用
    # ffmpeg concat **流复制**接起来 —— 拼接这一环零重编码，画面就是各段
    # 生成时的样子。编码只发生在生成播放版那一次，而那一次是为了把掐头落点
    # 做到逐帧精确（流复制掐头只能落在关键帧上，接缝会错开好几帧）。
    # 走不通（没 ffmpeg / 各片段参数对不齐）就退回下面的重编码路径。
    jobs = []
    for r in picks:
        skip, net = _seg_cut(r)
        jobs.append((r["film"], out_fps, skip, net))
    jp = _join_panel()
    if jp is not None:
        res = jp.concat_play_films(jobs, plan["path"])
        if res.get("ok"):
            want = int(plan["frames"])
            got = int(res.get("frames") or 0)
            a_secs = float(res.get("audio_seconds") or 0.0)
            v_secs = float(res.get("video_seconds") or 0.0)
            print("[H3 档案] 保存成片（无损拼接）：%d 段 -> %s（%d/%d 帧 @ %.3f fps，"
                  "流复制）" % (len(picks), plan["path"], got, want, out_fps))
            if got and abs(got - want) > 1:
                print("[H3 档案] ⚠ 成片帧数对不上时间轴：%d 帧 / 应为 %d 帧"
                      % (got, want))
            if v_secs and a_secs and abs(v_secs - a_secs) > 0.05:
                print("[H3 档案] ⚠ 成片音画不等长：画面 %.3fs / 声音 %.3fs"
                      % (v_secs, a_secs))
            return {"ok": True, "path": plan["path"], "file": plan["file"],
                    "frames": got, "seconds": v_secs or (got / out_fps if out_fps else 0.0),
                    "clips": len(picks), "at_index": plan["at_index"],
                    "audio": bool(a_secs), "audio_seconds": round(a_secs, 3),
                    "drift": round(abs(v_secs - a_secs), 3),
                    "expected_frames": want,
                    "lossless": True,
                    "encode": {"video": "copy（流复制 —— 拼接零损失）",
                               "audio": "copy（流复制 —— 拼接零损失）"}}
        print("[H3 档案] 无损拼接没走成（%s），退回重编码：%s"
              % (res.get("why"), res.get("detail") or ""))

    # 音轨：全部段都有、且采样率一致才带声音；否则明说，不出一条音画错位的片子。
    # 这里只**读**音轨并核对采样率，拼接要等画面写完 —— 每段取多少样本取决于
    # 那段实际写出了几帧（见下面的 wrote），不是取决于账面上的净长。
    full_audio = None
    tracks = [_audio_track(r["film"]) for r in picks]
    wavs = [t[0] for t in tracks]
    rates = set(t[1] for t in tracks if t[0] is not None)
    if all(w is None for w in wavs):
        sr = 0
    elif not all(w is not None for w in wavs) or len(rates) != 1:
        return {"ok": False, "blocker": "audio_mismatch",
                "message": "各段的音轨不一致（有的没声音 / 采样率不同）—— "
                           "把没声音的段重跑出来再保存。"}
    else:
        sr = rates.pop()

    out_path = plan["path"]
    tmp_path = out_path + ".part"
    W, H = dims[0][0], dims[0][1]
    out = None
    with _FILM_EXPORT_LOCK:
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            # format 显式给 mp4：临时文件后缀是 .part，PyAV 推断不出容器
            out = av.open(tmp_path, "w", format="mp4")
            vs = out.add_stream("libx264",
                                rate=Fraction(out_fps).limit_denominator())
            vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
            # 画面要重编码（trim 落点几乎不可能落在关键帧，流复制会花屏），
            # 所以这是**二次有损**：段片已经是 h264，再过一遍编码器必然掉一点。
            # crf 12 + preset medium 是"肉眼挑不出"那一档 —— 原来 crf 18/fast
            # 把 1.9 Mbps 的段片压到 868 kbps，掉了一半码率，画质肉眼可见地软。
            # 小画面（576×320 这个量级）跑 medium 也就多几秒，值。
            vs.options = {"crf": "12", "preset": "medium"}
            # 音轨流必须**在第一次 mux 之前**建好（header 还没写）；等视频
            # 全部写完再 add_stream 会得到 "Cannot rebase to zero time"。
            # 音频**数据**仍在视频之后喂（与 join 同款次序），只是流先占坑。
            out_as = None
            if sr:
                ch = int(tracks[0][0].shape[1])
                out_as = out.add_stream("aac", rate=int(sr))
                out_as.layout = "stereo" if ch >= 2 else "mono"
                # 音轨同样是二次有损（AAC→PCM→AAC）。256k 在 32 kHz 立体声下
                # 已远超"听不出差别"的门槛，留足余量，别让音质成为新的疑点。
                out_as.bit_rate = 256000
            vframe = 0
            wrote = []        # 每段**实际**写出几帧 —— 音频按它取样本，见下
            for r in picks:
                skip, net = _seg_cut(r)
                n = 0
                with av.open(r["film"]) as c:
                    s = c.streams.video[0]
                    s.thread_type = "AUTO"
                    for idx, frame in enumerate(c.decode(video=0)):
                        if idx < skip:
                            continue
                        # 净长之外的帧不进成片。这一段漏了会怎样（20261010 实测）：
                        # 第 1/3 段的成片是累积片（226 / 243 帧），被整条倒进去，
                        # 成片 498 帧 20.75 秒，而音频只按净长裁了 10.14 秒 ——
                        # 后半条整段没声音，前半条音画错位，听感就是"糊"。
                        if idx >= skip + net:
                            break
                        arr = frame.to_ndarray(format="rgb24")
                        nf = av.VideoFrame.from_ndarray(
                            np.ascontiguousarray(arr), format="rgb24")
                        nf.pts = vframe
                        for pkt in vs.encode(nf):
                            out.mux(pkt)
                        vframe += 1
                        n += 1
                wrote.append(n)
            for pkt in vs.encode(None):
                out.mux(pkt)

            if out_as is not None:
                # 每段音频严格取"这一段画面实际写出几帧"对应的样本数 ——
                # 音块 = 画面块，逐段对齐。
                # 为什么不按账面净长取：源片的帧数常有 1~2 帧的取整误差
                # （容器/编码器各舍入一次），画面少写一帧、声音照取满的话，
                # 每过一个接缝就漂一点，段一多就听得出来（实测一次差 42ms）。
                # AAC 每段解码出来的 PCM 头尾还带编码器 padding（每段多 ~17ms），
                # 照单全收漂得更厉害 —— 所以只取该取的那一段，一帧不多。
                # 接缝处再加 6ms 淡入：各段波形任意相位硬切相接会咔哒一声。
                pieces = []
                fade = max(0, int(round(sr * 0.006)))
                for i, ((wav, _sr), n) in enumerate(zip(tracks, wrote)):
                    vskip, _net = _seg_cut(picks[i])
                    start = int(round(vskip / out_fps * sr))
                    take = int(round(n / out_fps * sr))
                    piece = wav[start:start + take]
                    if piece.shape[0] < take:   # 音轨比画面短：尾部补静音
                        piece = np.vstack([piece, np.zeros(
                            (take - piece.shape[0], piece.shape[1]), dtype=np.int16)])
                    if fade and i > 0 and piece.shape[0] > fade:
                        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float32)[:, None]
                        piece = piece.astype(np.float32)
                        piece[:fade] *= ramp
                        piece = piece.astype(np.int16)
                    pieces.append(piece)
                wav = np.concatenate(pieces, axis=0)
                full_audio = (wav, sr)
                ch = int(wav.shape[1])
                pts, step = 0, 1024
                for off in range(0, wav.shape[0], step):
                    chunk = wav[off:off + step]
                    if chunk.shape[0] < step:
                        pad = np.zeros((step - chunk.shape[0], ch), dtype=np.int16)
                        chunk = np.vstack([chunk, pad])
                    packed = np.ascontiguousarray(chunk.T).reshape(1, -1)
                    af = av.AudioFrame.from_ndarray(
                        packed, format="s16", layout=out_as.layout)
                    af.sample_rate = int(sr)
                    af.pts = pts
                    pts += step
                    for pkt in out_as.encode(af):
                        out.mux(pkt)
                for pkt in out_as.encode(None):
                    out.mux(pkt)

            out.close()
            out = None
            os.replace(tmp_path, out_path)
        except Exception as e:
            if out is not None:
                try:
                    out.close()
                except Exception:
                    pass
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return {"ok": False, "blocker": "encode_failed",
                    "message": "拼接写片失败（文件被播放器占用？）：%s" % e}

    # 对账：视频帧数必须与时间轴的净长一致、音频秒数必须与视频一致。
    # 这一条就是「音频糊」那次事故的东西 —— 差一点点都会变成声音对不上嘴，
    # 而用户在播放器里只看得到"声音怪"，看不出差在哪。写进日志，一眼能核对。
    want = int(plan["frames"])
    a_secs = (full_audio[0].shape[0] / float(full_audio[1])) if full_audio else 0.0
    v_secs = (vframe / out_fps) if out_fps else 0.0
    drift = abs(v_secs - a_secs) if full_audio else 0.0
    print("[H3 档案] 保存成片：%d 段 -> %s（%d/%d 帧 @ %.3f fps，净长口径）"
          % (len(picks), out_path, vframe, want, out_fps))
    if vframe != want:
        print("[H3 档案] ⚠ 成片帧数对不上时间轴：写了 %d 帧、应为 %d 帧"
              " —— 有段的成片比它应有的净长短" % (vframe, want))
    if full_audio and drift > 0.05:
        print("[H3 档案] ⚠ 成片音画不等长：画面 %.3fs、声音 %.3fs（差 %.3fs）"
              % (v_secs, a_secs, drift))
    return {"ok": True, "path": out_path, "file": plan["file"],
            "frames": vframe, "seconds": v_secs,
            "clips": len(picks), "at_index": plan["at_index"],
            "audio": full_audio is not None,
            "audio_seconds": round(a_secs, 3),
            "drift": round(drift, 3),
            "expected_frames": want,
            "lossless": False,
            "encode": {"video": "libx264 crf=12 preset=medium（重编码，二次有损）",
                       "audio": "aac %d bps（重编码，二次有损）"
                                % (256000 if full_audio else 0)}}


def preview_film(name, ensure=True):
    """整片连续预览片：把当前所有可播段按顺序拼成【一条】mp4（流复制、帧精确），
    供导演台连播 / 跨段拖动播放。前端只面对单 video、单条连续时间轴，跨接缝是
    同一条视频里的普通位置，从根上消除"两个独立 video 交接偶发黑帧"。

    与 export_film 同构但更轻：拼到最后一个可播段、输出固定文件名、按签名缓存
    （段内容/顺序没变就复用，不重拼）。ensure=False 只读取当前缓存状态、不拼接。
    """
    fs = film_summary(name)
    rows = [r for r in (fs.get("clips") or []) if r.get("playable")]
    fps = float(fs.get("fps") or 24) or 24.0
    base = {"name": name, "fps": fps}
    if not rows:
        return dict(base, ok=False, blocker="no_film",
                    message="还没有可播放的段，先渲染一段。")
    frames_total = int(rows[-1]["clip_to"])
    # 签名：参与段的 身份/版本/净长/源片 + 顺序，任一变化即失效重拼。
    sig = "pv1|" + "|".join(
        "%s:%s:%s:%s" % (r.get("id"), r.get("updated"),
                         int(r.get("clip_to") or 0), r.get("film"))
        for r in rows)
    d = chain_dir(name)
    out = os.path.join(d, "_preview.mp4")
    side = out + ".sig"
    cached_sig = None
    if os.path.isfile(out):
        try:
            with open(side, encoding="utf-8") as fh:
                cached_sig = fh.read().strip()
        except OSError:
            cached_sig = None
    if os.path.isfile(out) and cached_sig == sig:
        return dict(base, ok=True, path=out, frames=frames_total,
                    seconds=(frames_total / fps) if fps else 0.0,
                    signature=sig, cached=True, clips=len(rows))
    if not ensure:
        return dict(base, ok=False, blocker="stale",
                    signature=sig, frames=frames_total, clips=len(rows))
    jp = _join_panel()
    if jp is None:
        return dict(base, ok=False, blocker="no_join_panel")
    jobs = []
    for r in rows:
        skip, net = _seg_cut(r)
        jobs.append((r["film"], fps, skip, net))
    # 快速路径：一条 ffmpeg、只重编码一次；走不通（混合音频 / 参数不一致 / 失败）
    # 回退"逐段播放版 + concat 流复制"的稳妥路径。
    res = None
    fast = getattr(jp, "concat_preview_fast", None)
    if fast is not None:
        try:
            res = fast(jobs, out)
        except Exception as exc:
            res = {"ok": False, "why": "fast_exc", "detail": repr(exc)}
    if not (res and res.get("ok")):
        try:
            res = jp.concat_play_films(jobs, out)
        except Exception as exc:
            return dict(base, ok=False, blocker="concat_failed", why="exc",
                        detail=repr(exc))
    if not res.get("ok"):
        return dict(base, ok=False, blocker="concat_failed",
                    why=res.get("why"), detail=res.get("detail"))
    try:
        with open(side, "w", encoding="utf-8") as fh:
            fh.write(sig)
    except OSError:
        pass
    got = int(res.get("frames") or 0) or frames_total
    return dict(base, ok=True, path=out, frames=got,
                seconds=float(res.get("video_seconds") or 0.0)
                        or (got / fps if fps else 0.0),
                signature=sig, cached=False, clips=len(rows),
                audio_seconds=float(res.get("audio_seconds") or 0.0))


# ---------------- 整片预览后台预热（20261011）----------------
_prewarm_running = set()      # 已在后台预热的片名（去重）
_prewarm_timers = {}          # 待启动的防抖计时器


async def _prewarm_task(name):
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, preview_film, name, True)
        if res.get("ok"):
            print("[H3 档案] 整片预览已预热：%s（%d 帧）"
                  % (name, int(res.get("frames") or 0)))
            # T3：顺带在后台把各段默认档雪碧图落盘（失败不影响整片预热）。
            jp = _join_panel()
            warm = getattr(jp, "warm_project_sprites", None) if jp is not None else None
            if warm is not None:
                def _warm():
                    return warm(film_summary(name).get("clips") or [])
                try:
                    n = await loop.run_in_executor(None, _warm)
                    if n:
                        print("[H3 档案] 时间轴缩略图已预生成：%s（%d 段）"
                              % (name, int(n)))
                except Exception:
                    pass
    except Exception as exc:
        print("[H3 档案] 整片预览预热失败：%r" % (exc,))
    finally:
        _prewarm_running.discard(name)


def _launch_prewarm(name):
    _prewarm_timers.pop(name, None)
    if not name or name in _prewarm_running:
        return
    _prewarm_running.add(name)
    try:
        asyncio.ensure_future(_prewarm_task(name))
    except RuntimeError:
        _prewarm_running.discard(name)


def schedule_prewarm(name, delay=1.2):
    """收片 / 全部渲染完成后调用：后台拼好整片预览片（不阻塞、同名去重、短延时
    防抖以合并连续收片）。ffmpeg 是同步 subprocess，统一丢线程池、不堵事件循环。"""
    if not name:
        return
    old = _prewarm_timers.get(name)
    if old is not None:
        old.cancel()
    try:
        loop = asyncio.get_event_loop()
        _prewarm_timers[name] = loop.call_later(max(0.0, delay), _launch_prewarm, name)
    except RuntimeError:
        pass


# ================= 工程库（P3）已于 20261012 应要求移除 =================
# 原工程库浏览 / 命名版本 / 回收站 / 模板新建 / 缩略图 / zip 打包下载相关函数，
# 以及 /cglide/chain/library/*、/thumb、/reveal 路由已整体删除（前端
# csglide_cast_library.js 与「工程库」按钮同步移除）。核心段落档案能力
# （move_clip / rename_chain / remove_clip / sync_check / summary 等）保留。


def move_clip(name, cid, to_index):
    chain = load_chain(name)
    order = chain.get("order", [])
    if cid not in order:
        return chain
    order.remove(cid)
    order.insert(max(0, min(len(order), int(to_index))), cid)
    chain["order"] = order
    save_chain(name, chain)
    return chain


def _repoint_paths(obj, old_root, new_root):
    """递归把容器里以 old_root 开头的绝对路径字符串换成 new_root；返回 (obj, changed)。
    给工程改名后，修复 clip.json 内部 film / cont_saved 等含旧片名的绝对引用。"""
    if isinstance(obj, str):
        if obj.startswith(old_root):
            return new_root + obj[len(old_root):], True
        return obj, False
    if isinstance(obj, dict):
        changed = False
        for k in list(obj.keys()):
            v, ch = _repoint_paths(obj[k], old_root, new_root)
            if ch:
                obj[k] = v
                changed = True
        return obj, changed
    if isinstance(obj, list):
        changed = False
        for i, v in enumerate(obj):
            v2, ch = _repoint_paths(v, old_root, new_root)
            if ch:
                obj[i] = v2
                changed = True
        return obj, changed
    return obj, False


@chainlocked
def rename_chain(name, new_name):
    """给整条片子改名 —— 磁盘上是把 <旧名> 目录整个改成 <新名>。

    只换外壳，里面任何文件都不动：段落 id、潜空间、成片的组织方式全是相对片名
    算的。**必须连磁盘一起改** —— 前端改了名、后端不改，下一次渲染会在新名字下
    另起一个目录，原来那些段全部对不上，看起来就像"片子凭空丢了"。

    新名字已经被另一条片子占了就拒绝：宁可名字没改成，也不能把两条片子并进
    同一个目录。
    """
    old = (name or "").strip()
    new = (new_name or "").strip()
    if not new:
        return {"renamed": False, "reason": "新片名是空的"}
    if old == new:
        return {"renamed": True, "from": old, "to": new, "moved": False}
    src, dst = chain_dir(old), chain_dir(new)
    if not os.path.isdir(src):
        # 旧片名还没有档案目录（一次都没渲染过）—— 没有要搬的东西，改个名而已。
        return {"renamed": True, "from": old, "to": new, "moved": False}
    if os.path.exists(dst):
        return {"renamed": False,
                "reason": "已经有一条片子叫「%s」了，换一个名字" % new}
    # DB 工程：搬目录前先 checkpoint 并关掉连接，释放 db/wal/shm 句柄，
    # 否则 Windows 下移动带打开文件的目录可能失败。
    try:
        if store.has_db(src):
            store.close_db(src)
    except Exception:
        pass
    try:
        os.rename(src, dst)
    except Exception as e:
        return {"renamed": False, "reason": "%s: %s" % (type(e).__name__, e)}
    # DB 工程：meta_json / 版本路径列里的旧绝对路径回写到新目录（与下面 JSON
    # 回写同目的，作用对象换成库）。
    try:
        if store.has_db(dst):
            old_root = os.path.normpath(src)
            new_root = os.path.normpath(dst)
            store.repath_db(dst, old_root, new_root)
    except Exception:
        pass
    # 搬目录只动了外壳：各 clip.json 里 film / cont_saved 等是含**旧片名的绝对路径**
    # （...\h3_chain\<旧名>\clip_0001.mp4）。不回写，它们就指向已消失的旧目录、
    # 段成片全读不到 —— 表现为改名后"全断开、不显示、变待生成"（20261011 根因修复）。
    try:
        old_root = os.path.normpath(chain_dir(old))
        new_root = os.path.normpath(chain_dir(new))
        for fn in os.listdir(dst):
            if not fn.endswith(".json"):
                continue
            jp = os.path.join(dst, fn)
            try:
                with open(jp, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            data, changed = _repoint_paths(data, old_root, new_root)
            if changed:
                with open(jp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    # chain.json 里的名字同步一份：以后按名字找片子时不会翻出旧名。
    try:
        c = load_chain(new)
        c["name"] = new
        save_chain(new, c)
    except Exception:
        pass
    # 「最近登记过的片名」也跟着走，否则 📦/📤 还去旧目录里找。
    try:
        if last_name() == old:
            mark_last(new)
    except Exception:
        pass
    # 旧名作废、新名刷新（前端 store 清旧缓存后订阅新名）
    ws_broadcast("renamed", old, new_name=new)
    ws_broadcast("chain", new)
    return {"renamed": True, "from": old, "to": new, "moved": True}


def clip_files(name, cid):
    """这一段在档案目录里占用的所有文件（段落 json / 成片 / 潜空间 / 波形 / 各版本）。

    判据是 **文件名以 "<cid>." 开头** —— clip_0001.mp4、clip_0001.latent.safetensors、
    clip_0001.wave.json、clip_0001.t2.mp4 全都算它的；而 clip_00010.json 不会被误伤
    （它不以 "clip_0001." 开头）。这样将来多出什么派生文件也自动跟着走，
    不必每加一种文件就回来改一次删除逻辑。

    「删除这一段」在删之前要把这份清单摆给用户看 —— 删是不可逆的，清单是唯一的依据。
    """
    out = []
    try:
        d = chain_dir(name)
    except Exception:
        return out
    if not os.path.isdir(d):
        return out
    prefix = str(cid) + "."
    for fn in sorted(os.listdir(d)):
        if not fn.startswith(prefix):
            continue
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            size = os.path.getsize(p)
        except Exception:
            size = 0
        out.append({"name": fn, "size": size})
    return out


@chainlocked
def remove_clip(name, cid, force=False):
    """把一段从档案里去掉。

    ``force=False``（默认）只允许删**还没有成片**的空段；``force=True`` 是
    「删除这一段」—— 真删，连同磁盘上的段落 json / 成片（含各版本）/ 潜空间 / 波形
    一起清掉，order 与 current 同步更新。

    为什么默认那条拒绝分支还留着：第 k 段的 film 有可能是「前 k 段累积起来的片子」，
    抽掉中间一段，后面每一段的累积成片都对不上了 —— 那不是"删一段"，那是"从成片里
    剪掉一段"，得走剪辑。所以后端不替用户判断"你大概不是这个意思"，而是把判据留在
    这里，由前端**先把要删哪些文件摆出来**、用户点确认之后才带 force 调进来。
    不可逆的事，确认必须发生在看得见后果的地方。
    """
    chain = load_chain(name)
    order = _dedup_order(chain.get("order"))
    if cid not in order:
        # 允许拿「分段 uid」（meta.shot）来删：工程里还没拿到 clip id 的段
        # （登记失败 / 工程被还原过）也能把档案条目删干净，不再留孤儿。
        for c in order:
            if str((load_clip(name, c) or {}).get("shot") or "") == str(cid):
                cid = c
                break
    if cid not in order:
        return {"removed": False, "reason": "档案里没有这一段"}
    meta = load_clip(name, cid) or {}
    if meta.get("film") and not force:
        return {"removed": False, "reason": "这一段已经拍出成片，不能从档案里抽掉"}
    idx = order.index(cid)
    # 同 id 的历史重复项（旧版发号撞号留下的幽灵）一并清掉；顺手把段落 json
    # 已经丢失的空壳段摘掉 —— 只删第一个同 id 项的话，幽灵留在 order 里，
    # 时间轴与全片时长永远降不回去。
    order = [c for c in order if c != cid and load_clip(name, c) is not None]
    chain["order"] = order
    if chain.get("current") == cid:
        chain["current"] = order[-1] if order else ""
    save_chain(name, chain)

    # DB 工程：段落行连同 state / rack 槽位 / 版本台账一起级联删掉。
    try:
        if store.has_db(chain_dir(name)):
            store.delete_clip_doc(chain_dir(name), cid)
            store.log_event(chain_dir(name), "clip_remove", cid,
                            {"index": idx + 1}, "backend")
    except Exception:
        pass

    # 磁盘清理。单个文件删不掉（被占用等）不抛整条失败 —— 把名字带回去让前端说。
    deleted, failed = [], []
    for f in clip_files(name, cid):
        p = os.path.join(chain_dir(name), f["name"])
        try:
            os.remove(p)
            deleted.append(f["name"])
        except Exception as e:
            failed.append("%s（%s）" % (f["name"], e))

    # 紧接着这一段的那一段，它的引导头本来就是从这一段尾巴来的 —— 那一段没了，
    # 它的续接窗口就悬空了，必须重抽。只在它**确实接着上一段**（有 trim 窗口）时才标，
    # 否则是凭空给用户加一条待办。
    note = None
    nxt = order[idx] if idx < len(order) else None
    if nxt:
        try:
            nm = load_clip(name, nxt) or {}
            if int(nm.get("trim_frames") or 0) > 0:
                _raise_pending(name, nxt, "redo")
                note = ("往后紧接着的第 %d 段原来的续接窗口接的就是这一段，"
                        "已标为「要重抽」" % (idx + 1))
        except Exception:
            pass
    ws_broadcast("clip_removed", name, cid)
    return {"removed": True, "id": cid, "index": idx + 1,
            "files": deleted, "failed": failed, "note": note}


def sync_check(name, shots):
    """「工程预设」vs「档案事实」的冲突清单 —— P1-10 的核心。

    一个 shot 的时长（frames）有**两个写入源**，都合法、都可能被用户改：

      1. 导演台面板的「长度」预设 → 存在工程对象里（跟工作流走，没有时间戳）；
      2. 时间轴拖边缘 → 写磁盘档案（带 updated 时间戳）。

    渲染时 `_register_clip` 会用面板预设覆盖档案。所以如果用户拖了时间轴
    （改档案），却没重新在面板设长度，下次渲染就会被面板的旧预设**静默覆盖**
    —— 这正是文档禁止的"静默覆盖"。

    本函数只负责**查**，不负责改：把两边对不上的段列出来，交给前端弹清单让
    用户选「用工程预设」还是「用档案事实」，绝不自己动手覆盖。

    shots: 前端工程对象里的分段数组，每项 { id, frames, synced }。id 是分段
    身份 uid（= 档案里的 shot 字段）；frames 是面板预设（原始 length，未吸附）；
    synced 是前端上次从档案同步回来的时间戳（可省略，省略则只按 frames 是否
    一致判断）。

    预设的吸附规则与渲染侧一致（align_frame_count 向上取 17k+5），所以
    预设 243 和档案 243 不会因为吸附算法不同而误报 —— 吸附逻辑只在这里和
    csglide_cast 各一份，比对前先对齐。
    """
    try:
        from . import csglide_cast as _cast
        _align = getattr(_cast, "align_frame_count", None)
    except Exception:
        _align = None
    if _align is None:
        _align = lambda n: max(5, int(n))       # 兜底：至少给个确定的函数

    rows = {c.get("shot"): c for c in summary(name).get("clips", [])
            if c.get("shot")}
    out = []
    for s in shots or []:
        sid = (s.get("id") or "").strip()
        if not sid:
            continue
        row = rows.get(sid)
        if not row:
            # 工程里有这个分段、档案里还没它（还没跑过第一遍）——不是冲突。
            continue
        raw = s.get("frames")
        if raw is None:
            continue
        try:
            preset = int(_align(int(raw)))
        except (TypeError, ValueError):
            continue
        fact = int(row.get("frames") or 0)
        if preset == fact:
            continue
        # 档案比「前端上次同步」新，说明是时间轴/后端后来改的 —— 真正要提示的
        # 情况。没传 synced 就按"预设与事实不同即冲突"处理。
        synced = s.get("synced")
        out.append({
            "shot": sid,
            "id": row.get("id"),
            "index": (row.get("index") or 0),
            # preset 是吸附后的值（与渲染时一致），fact 是档案里的真实帧数
            "preset": preset,
            "fact": fact,
            "updated": row.get("updated"),
            "stale": (synced is None) or (synced is not None and synced < (row.get("updated") or 0)),
        })
    return {"name": name, "conflicts": out}


def summary(name):
    chain = load_chain(name)
    clips, broken = [], []
    for c in chain.get("order", []):
        m = load_clip(name, c)
        # 段落 json 被手动删了 / 写坏了：静默丢掉会让人以为那段从来没生成过，
        # 于是"我的第 3 段呢"变成无头案。单独列出来交给面板报。
        (clips if m else broken).append(m or c)
    return {
        # 只留还读得到的段；total_frames 也就只统计这些，不会虚高
        "broken": broken,
        "name": name,
        "fps": chain.get("fps", 24),
        "trim_frames": chain.get("trim_frames", DEFAULT_TRIM),
        "prompt_global": chain.get("prompt_global", ""),
        "current": chain.get("current"),
        "locked": bool(chain.get("locked")),
        "total_frames": sum(int(c.get("frames", 0)) for c in clips),
        "clips": clips,
    }


# --------------------------------------------------------------------------
# 历史旧工程（轴头「历史」按钮）：扫描 output/h3_chain/ 一劳永逸
# --------------------------------------------------------------------------

def _dir_size_mb(path):
    """目录占用（MB）。历史列表显示工程大小用，失败不致命。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return round(total / 1048576.0, 1)


def history_list(include_sys=False):
    """历史旧工程清单（轻量）：新 DB 工程 / 老 JSON 工程都走公共加载函数
    （load_chain / load_clip 对 store 透明），只读文档、**不跑 ffprobe**。

    状态口径：
      empty 空工程 · todo 只有潜空间没成片 · miss 部分段有成片 · film 全段成片。

    自检/冒烟残留（_routesmoke、__probe、H3StudioDebug 等下划线开头目录）默认
    隐藏，include_sys=True 才列出（前端「显示系统工程」开关）。
    """
    root = os.path.join(_out_dir(), "h3_chain")
    def _is_sys(d):
        return d.startswith((".", "_")) or d.startswith("H3StudioDebug")
    try:
        names = [d for d in os.listdir(root)
                 if os.path.isdir(os.path.join(root, d))
                 and (include_sys or not _is_sys(d))]
    except Exception:
        names = []
    out = []
    for nm in names:
        try:
            d = chain_dir(nm)
            is_db = store.has_db(d)
            ch = load_chain(nm)
            order = [str(c) for c in (ch.get("order") or [])]
            seg = len(order)
            total_frames = 0
            playable = 0
            pending = False
            w = h = 0
            latest = 0.0
            for cid in order:
                m = load_clip(nm, cid)
                if not m:
                    continue
                total_frames += int(m.get("frames") or 0)
                if m.get("pending"):
                    pending = True
                fp = m.get("film")
                if fp and os.path.isfile(fp):
                    playable += 1
                tp = (m.get("take_params") or {}).get(
                    str(max(1, int(m.get("current_take") or 1)))) or {}
                if not w and int(tp.get("w") or 0):
                    w, h = int(tp["w"]), int(tp.get("h") or 0)
                try:
                    latest = max(latest, float(m.get("updated") or 0))
                except Exception:
                    pass
            if seg == 0:
                status = "empty"
            elif playable == seg:
                status = "film"
            elif playable > 0:
                status = "miss"
            else:
                status = "todo"
            try:
                created = os.path.getctime(d)
            except Exception:
                created = 0
            opened = latest or created
            out.append({
                "name": nm,
                "segCount": seg,
                "totalFrames": total_frames,
                "w": w,
                "h": h,
                "status": status,
                "pending": pending,
                "playable": playable,
                "backend": "db" if is_db else "json",
                "createdAt": round(created, 2),
                "openedAt": round(opened, 2),
                "sizeMB": _dir_size_mb(d),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x.get("openedAt") or 0, reverse=True)
    return {"projects": out, "count": len(out)}


def history_bundle(name):
    """切换工程的完整资料包。
      · DB 工程：project_blob 全量（clips 含 state、racks 媒体架、desk 文案台），
        与当时离开时一模一样；
      · 老 JSON 工程：片账 + 段文档（媒体架/文案台 20261012 才开始入库，
        老工程没有这两份，缺省 —— 前端菜单标注「老工程」）。
    """
    d = chain_dir(name)
    if not os.path.isdir(d):
        return {"error": "没有这个工程：%s" % name}
    if store.has_db(d):
        return store.project_blob(d, name, summary)
    ch = load_chain(name)
    clips = []
    for cid in (ch.get("order") or []):
        m = load_clip(name, str(cid))
        if m is not None:
            clips.append(m)
    return {"name": name, "backend": "json", "chain": ch, "clips": clips}


def delete_project(name):
    """删除整个工程目录（output/h3_chain/<name>），历史菜单「一键删除」用。
    不进回收站、不可恢复 —— 前端已明确不弹确认。

    安全：
      · 名字先剥掉路径分隔符 / 非法字符，并二次确认解析结果仍在 h3_chain 根下，
        防止路径穿越；
      · 系统工程（点 / 下划线开头、H3StudioDebug）不允许从这里删；
      · 目录不存在返回 not_found。
    """
    safe = "".join(c for c in (name or "") if c not in '\\/:*?"<>|').strip()
    if not safe:
        return {"error": "缺少工程名"}
    if safe.startswith((".", "_")) or safe.startswith("H3StudioDebug"):
        return {"error": "系统工程不能从这里删除"}
    base = os.path.realpath(os.path.join(_out_dir(), "h3_chain"))
    d = os.path.realpath(os.path.join(base, safe))
    if os.path.commonpath([base, d]) != base:
        return {"error": "非法工程名"}
    if not os.path.isdir(d):
        return {"error": "没有这个工程：%s" % safe, "name": safe}
    # 删除前先关掉该工程的 SQLite 连接：Windows 上 project.db / -wal / -shm
    # 一旦连接常驻就会被句柄锁住，直接 rmtree 报 PermissionError → 500（真机实测）。
    try:
        store.close_db(d)
    except Exception:
        pass
    size_mb = _dir_size_mb(d)

    def _force_remove(func, path, exc):
        # rmtree 兜底：去掉只读位再删一次（Windows 只读文件 / 句柄瞬时占用）。
        try:
            os.chmod(path, 0o777)
            func(path)
        except Exception:
            pass

    last_err = None
    for _attempt in range(3):
        # 浏览器预览可能正占着 mp4 等句柄：整体重试 3 次，间隔等句柄释放。
        try:
            shutil.rmtree(d, onexc=_force_remove)
            last_err = None
            break
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    if os.path.exists(d):
        return {"error": "删除未完成，可能有文件被占用：%s" % last_err, "name": safe}
    return {"ok": True, "name": safe, "sizeMB": size_mb}


# --------------------------------------------------------------------------
# 路由（前端时间轴用）
# --------------------------------------------------------------------------

try:
    from server import PromptServer

    @PromptServer.instance.routes.get("/cglide/chain/summary")
    async def _route_summary(request):
        # 读盘走线程池：summary 每 1~2 秒被三节点轮询一次，同步 stat/json 读
        # 堵在事件循环上会拖死 ComfyUI 全部 HTTP（面板"反应迟钝"的后端根因）。
        name = request.query.get("name") or "未命名"
        loop = asyncio.get_running_loop()
        return _json(await loop.run_in_executor(None, summary, name))

    @PromptServer.instance.routes.post("/cglide/chain/unique_name")
    async def _route_unique_name(request):
        """「新建整片」用：回一个还没被占用的默认片名，避免重名写进旧档案。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        return _json({"name": await _io(unique_chain_name, body.get("base"))})

    @PromptServer.instance.routes.get("/cglide/chain/clip")
    async def _route_clip(request):
        name = request.query.get("name") or "未命名"
        cid = request.query.get("id") or ""
        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(None, load_clip, name, cid)
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(meta)

    @PromptServer.instance.routes.post("/cglide/chain/update")
    async def _route_update(request):
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        meta = await _io(update_clip, name, cid, body.get("patch") or {})
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(meta)

    @PromptServer.instance.routes.post("/cglide/chain/rename")
    async def _route_rename(request):
        """导演台改片名用。片名就是磁盘上的目录名，改名 = 搬目录（理由见 rename_chain）。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        old = (body.get("name") or "").strip()
        new = (body.get("to") or "").strip()
        if not new:
            return _json({"error": "缺少新片名"}, 400)
        try:
            return _json(await _io(rename_chain, old, new))
        except Exception as e:
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    @PromptServer.instance.routes.post("/cglide/chain/add_clip")
    async def _route_add_clip(request):
        """导演台「＋ 接一段」用：把新分段登记进档案。

        时间轴画的是**档案里的段**（film_summary 走 chain 的 order），而「接
        一段」改的是工程对象（跟工作流走的 shots）。只在工程对象里加一段、
        不登记的话轴上永远不出现 —— 用户点了按钮却什么都没发生，就是这个原因。

        刻意不吸附帧数：档案要记的是真实跑出来的帧数（见 add_clip 的注释）。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        fields = {}
        n = body.get("frames")
        if n:
            try:
                fields["frames"] = max(1, int(n))
            except Exception:
                pass
        if body.get("shot"):
            fields["shot"] = str(body["shot"])
        # 承接窗口 trim：前端按**工程真实段序**算好发来（首段 0、其余头部窗口）。
        # 必须显式提取并透传 —— 旧路由漏了这一项，前端发的 trim_frames 被静默
        # 丢弃，于是首段也落了 add_clip 的 chain 级默认 22。
        tf = body.get("trim_frames")
        if tf is not None:
            try:
                fields["trim_frames"] = max(0, int(tf))
            except Exception:
                pass
        # 段名：显示在时间轴的段块上。键名与 body["name"]（片名）分开，两回事。
        # 不能塞进 fields["name"] 再 add_clip(name, **fields)：那会让 add_clip
        # 同时收到「片名」和「段名」两个 name，直接 TypeError（multiple values）。
        # 段名落盘走 update_clip，与 add_clip 分两步 —— 反正都是写同一个 json。
        clip_name = str(body["clip_name"]).strip()[:60] if body.get("clip_name") else ""
        try:
            def _add():
                _cid = add_clip(name, **fields)
                if clip_name:
                    update_clip(name, _cid, {"name": clip_name})
                return _cid
            cid = await _io(_add)
        except Exception as e:
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
        # add_clip 命中同 uid 的已有段时会返回它的 id —— index 按它在 order
        # 里的**位置**给，不能拿总段数顶替，否则前端会显示错段号。
        order_now = _dedup_order((await _io(load_chain, name)).get("order"), name)
        return _json({"id": cid,
                      "index": (order_now.index(cid) + 1) if cid in order_now
                               else len(order_now)})

    @PromptServer.instance.routes.post("/cglide/chain/reorder")
    async def _route_reorder(request):
        """时间轴拖动排序用：按前端给的 id 顺序重排 chain.order。

        为什么需要后端参与：时间轴画的是**档案里的顺序**（film_summary 走
        chain.order），只在前端把 shots 数组换个次序，下一次 film_summary 回来
        又会按老顺序画 —— 用户看到的是"拖完弹回去了"。所以顺序必须落盘。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        order = body.get("order")
        if not isinstance(order, list):
            return _json({"error": "order 必须是 id 数组"}, 400)
        try:
            return _json(await _io(reorder_clips, name, order))
        except Exception as e:
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    @PromptServer.instance.routes.post("/cglide/chain/remove_clip")
    async def _route_remove_clip(request):
        """删除一段。``force=true`` = 导演台「删除本段」—— 真删（含磁盘文件）。

        不带 force 时后端保留"有成片的段不给删"那条判据（理由见 remove_clip）。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        try:
            return _json(await _io(remove_clip, name, cid, force=bool(body.get("force"))))
        except Exception as e:
            return _json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    @PromptServer.instance.routes.get("/cglide/chain/clip_files")
    async def _route_clip_files(request):
        """这一段要删掉的话，会删到哪些文件（只读，不删任何东西）。

        前端「删除本段」拿它把清单摆出来 —— 删是不可逆的，用户点确认之前
        必须先看见自己将要失去什么。大小一并给出，好判断是不是那块成片。
        """
        name = request.query.get("name") or "未命名"
        cid = request.query.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        loop = asyncio.get_running_loop()
        files, meta = await loop.run_in_executor(
            None, lambda: (clip_files(name, cid), load_clip(name, cid) or {}))
        return _json({
            "id": cid,
            "files": files,
            "total_bytes": sum(int(f.get("size") or 0) for f in files),
            "has_film": bool(meta.get("film")),
            # 有没有潜空间原稿 —— 有的话"删了就重抽不出来"，确认框要说清这一点。
            "has_latent": any(".latent" in f["name"] for f in files),
        })

    @PromptServer.instance.routes.get("/cglide/chain/film")
    async def _route_film(request):
        """整片成片视图：导演台监视器 / 时间轴用它。

        注意：曾出现活服务器把 GET /cglide/chain/film 误送到 POST 处理器的情况
        （返回「请求体不是 JSON」）。为兜底同时注册 /film_summary，前端可切过去。
        """
        name = request.query.get("name") or "未命名"
        loop = asyncio.get_running_loop()
        return _json(await loop.run_in_executor(None, film_summary, name))

    # 与 /film 同义的只读别名 —— 避开偶发的同路径 GET/POST 串线
    @PromptServer.instance.routes.get("/cglide/chain/film_summary")
    async def _route_film_summary(request):
        name = request.query.get("name") or "未命名"
        loop = asyncio.get_running_loop()
        return _json(await loop.run_in_executor(None, film_summary, name))

    @PromptServer.instance.routes.get("/cglide/chain/preview_film")
    async def _route_preview_film(request):
        """整片连续预览片（连播 / 跨段拖动播放用）：确保就绪、返回路径/帧数/签名。
        ensure=0 只读缓存、不拼接。拼接走线程池、不堵事件循环。 """
        name = request.query.get("name") or "未命名"
        ensure = request.query.get("ensure", "1") not in ("0", "false")
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, preview_film, name, ensure)
        return _json(res)

    @PromptServer.instance.routes.get("/cglide/chain/prewarm")
    async def _route_prewarm_get(request):
        """后台预热整片预览片（GET 便捷入口，立即返回、不等待拼接）。"""
        name = request.query.get("name") or "未命名"
        was = name in _prewarm_running
        schedule_prewarm(name)
        return _json({"ok": True, "already_running": was})

    @PromptServer.instance.routes.post("/cglide/chain/prewarm")
    async def _route_prewarm(request):
        """后台预热整片预览片：立即返回、不等待拼接（同名在跑去重）。
        前端在全部渲染完成 / 收片后 fire-and-forget；收片后端也会自行调一次。 """
        name = request.query.get("name")
        try:
            body = await request.json()
            if isinstance(body, dict) and body.get("name"):
                name = body["name"]
        except Exception:
            body = None
        name = name or "未命名"
        was = name in _prewarm_running
        schedule_prewarm(name)
        return _json({"ok": True, "already_running": was})

    @PromptServer.instance.routes.post("/cglide/chain/set_film")
    async def _route_film_set(request):
        """手工登记/改一段的成片路径（面板上「选一个文件当这段的成片」用）。

        路径从 /cglide/chain/film 拆出，避免与 GET 同路径时被误路由。
        仍保留下方 POST /film 兼容入口。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        meta = await _io(set_film, name, cid, body.get("path"),
                         body.get("frames"), body.get("seam"))
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(meta)

    # 注意：不要再在 /cglide/chain/film 上挂 POST。
    # ComfyUI-LG_HotReload 热重载时会按「路径」替换 handler、不区分 method，
    # 同路径的 GET+POST 会被最后一个 method 的 handler 覆盖 —— 这就是
    # GET /film 返回「请求体不是 JSON」的根因。写操作用 /set_film。

    @PromptServer.instance.routes.post("/cglide/chain/adopt_film")
    async def _route_adopt_film(request):
        """把刚渲染出来的视频**收进本片文件夹**，并登记为这一段的成片。

        渲染节点把文件写在哪儿由工作流决定（output 根目录、video 子目录、前缀
        怎么起都是用户自己设的）—— 档案若只记那个位置，一部片子的成片就散在
        output 各处，而潜空间、波形都躺在 h3_chain/<片名>/ 里，三样东西分三个
        地方，"这一段到底有哪些文件"就无从查起。
        所以这里统一口径：拷成 h3_chain/<片名>/clip_000N.mp4 再登记。这样
        「成片 / 潜空间 / 波形」同目录同命名，拷贝一个文件夹就是完整工程。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        fn = str(body.get("filename") or "")
        if not fn:
            return _json({"error": "缺少文件名"}, 400)
        meta = load_clip(name, cid)
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)

        root = _out_dir()
        src = os.path.join(root, str(body.get("subfolder") or ""),
                           os.path.basename(fn))
        if not os.path.isfile(src):
            return _json({"error": "找不到刚渲染出来的文件：%s" % fn}, 404)
        take = max(1, int(meta.get("current_take") or 1))
        dest = clip_film(name, cid, take)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # copy2 走线程池：几百兆的成片不该把事件循环堵住
            await asyncio.get_running_loop().run_in_executor(
                None, shutil.copy2, src, dest)
        except Exception as e:
            return _json({"error": "收片失败：%s" % e}, 500)

        # 这一版是怎么生成的，跟成片一起记下（20261010，版本与参数绑定）：
        #   w/h —— 本次画布。切回这一版时面板画布跟着回滚，不再"画面回去了、
        #          参数还留在新设的值上"，混着跑必然接不上；
        #   cont_saved —— 本次 run 写出的续拍存档（120 秒时间窗内才认）。
        #          下一段续写优先接**这一份**，精确认段，不再靠目录猜。
        tp = dict(meta.get("take_params") or {})
        rec = {}
        try:
            if int(body.get("w") or 0) > 0 and int(body.get("h") or 0) > 0:
                rec["w"] = int(body.get("w"))
                rec["h"] = int(body.get("h"))
        except (TypeError, ValueError):
            pass
        # 参数快照（20261010bb · 抄 obvpm「从片段恢复当时的设置」）：把这一版
        # 生成用的提示词与采样参数一并记下 —— 切回旧版本时能看清当时用的是
        # 什么，不用自己另外记。
        # **seed 不在此列**：它在工作流节点里，导演台拿不到；档案里那个 seed
        # 是恒 0 的占位，抄进来只会让人以为能复现。宁可不记，也不记假的。
        for k in ("prompt_seg", "steps", "cfg", "bridge", "refs"):
            if k in meta:
                rec[k] = meta[k]
        # 续拍存档归位（20261010bb · 抄 obvpm 的 .mctx 侧车）：h3_context/ 里
        # 那份拷进本片文件夹，跟着成片走 —— 拷一个文件夹就是完整工程，不用再
        # 记着「还有一摞档在别的目录」。原位置那份留给存档节点自己清理。
        cs = _last_saved_cont()
        if cs:
            cdst = clip_cont(name, cid, take)
            try:
                os.makedirs(os.path.dirname(cdst), exist_ok=True)
                # copy2 走线程池：几十兆的存档不该把事件循环堵住
                await asyncio.get_running_loop().run_in_executor(
                    None, shutil.copy2, cs, cdst)
                rec["cont_saved"] = cdst
            except Exception as e:
                # 拷不进去也不能把线索丢了：记原位置，续写照样认得段
                print("[H3 档案] 续拍存档没能归位（%s），仍记原位置：%s" % (e, cs))
                rec["cont_saved"] = cs
        if rec:
            tp[str(take)] = rec
            update_clip(name, cid, {"take_params": tp})
        # 实测帧数随成片一起入账（20261010 修）：以前这里不传 frames，
        # film_frames 恒 0 —— 前端的累积/单段判据、连播落段、采样窗口全部
        # 失去地面真相，"整片连播每段播两遍"就是从这来的。
        # 契约（20261010ba）：同一刻把「开头几帧是承接」也算一次写死，
        # 从此时间轴/播放/保存成片都读它，不再各处判断这条片子是什么形态。
        _ff = probe_film_frames(dest)
        _from, _to, _htrim = _span_of(name, cid)
        out = set_film(name, cid, dest, frames=_ff,
                       head=_compute_film_head(_from, _to, _ff, head_trim=_htrim))
        # 收片后后台预热整片预览片（点播放即秒开）；不阻塞本次响应。
        schedule_prewarm(name)
        return _json(out)

    @PromptServer.instance.routes.post("/cglide/chain/resize")
    async def _route_resize(request):
        """拖时间轴改一段的时长。返回本段与下一段的待办状态。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        try:
            frames = int(body.get("frames"))
        except (TypeError, ValueError):
            return _json({"error": "frames 必须是整数"}, 400)
        res = await _io(resize_clip, name, cid, frames)
        if res is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(res)

    @PromptServer.instance.routes.post("/cglide/chain/seam")
    async def _route_seam(request):
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        meta = await _io(set_seam, name, cid, body.get("side"),
                         body.get("score"), body.get("note"))
        if meta is None:
            return _json({"error": "没有这一段，或 side 不是 left/right"}, 404)
        return _json(meta)

    @PromptServer.instance.routes.post("/cglide/chain/current")
    async def _route_current(request):
        """设「当前段」——三个节点共享的编辑目标。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id") or ""
        ch = await _io(set_current, name, cid)
        if ch is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json({"current": ch.get("current")})

    @PromptServer.instance.routes.post("/cglide/chain/lock")
    async def _route_lock(request):
        """锁定 / 解锁一段（锁的是编辑目标）。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        meta = await _io(set_locked, name, cid, body.get("locked"))
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(meta)

    @PromptServer.instance.routes.post("/cglide/chain/pending")
    async def _route_pending(request):
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        meta = await _io(set_pending, name, body.get("id"), body.get("kind"))
        if meta is None:
            return _json({"error": "没有这一段"}, 404)
        return _json(meta)

    # 只读查询**不挂** /cglide/chain/pins，另走 /pins_state（20261010bn）：
    # ComfyUI-LG_HotReload 热重载时按**路径**整条换 handler、**不区分 method**，
    # 同路径的 GET+POST 会被后注册那个（这里是 POST）覆盖 —— GET 于是落进 POST
    # 处理器，回一句「请求体不是 JSON」，前端 pinGroup 拿不到数据，右键菜单里
    # 「承接窗口」那一组（独立/往后接/往前接/补桥/循环）整组不显示，而且只留
    # 一行小灰字，看着就像"这功能没了"。
    # 同一个坑 /cglide/chain/film 踩过一次，当时写操作已挪到 /set_film —— 这里
    # 照同一套办法办：写留在 /pins，读搬到 /pins_state。
    @PromptServer.instance.routes.get("/cglide/chain/pins_state")
    async def _route_pins_state(request):
        """这一段的承接窗口 + 可当源片的段（给时间轴的接法菜单用）。"""
        name = request.query.get("name") or "未命名"
        cid = request.query.get("id") or ""

        def _pins_state_work():
            c = load_clip(name, cid)
            if c is None:
                return None
            ch = load_chain(name)
            _pins = pins_of(name, cid)
            return {
                "id": cid,
                "pins": _pins,
                "mode": pin_mode(_pins,
                                 [str(x) for x in (ch.get("order") or [])]),
                "sources": pin_sources(name, cid),
                "order": [str(x) for x in (ch.get("order") or [])],
            }
        res = await _io(_pins_state_work)
        if res is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(res)

    # 写操作独占 /pins（**不要再往这条路径上挂 GET** —— 理由见 /pins_state 处
    # 的注释：热重载按路径换 handler 不认 method，GET 会被这一条盖掉）。
    @PromptServer.instance.routes.post("/cglide/chain/pins")
    async def _route_pins(request):
        """改写承接窗口。

        两种写法二选一：
          · ``pins`` 直接给列表（[{from,take,window,at}]）；
          · ``mode`` + 源段（extend/prepend 用 ``src``，bridge 用 ``src``
            与 ``src2``，loop 自动取首尾段），后端照 :func:`pins_from_mode`
            翻译 —— 时间轴上点一下接法就走这条路。
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id") or ""
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        def _pins_work():
            if load_clip(name, cid) is None:
                return None
            raw = body.get("pins")
            if isinstance(raw, list):
                _pins = [dict(x) for x in raw]
            else:
                _pins = pins_from_mode(name, cid, body.get("mode"),
                                       int(body.get("window") or 22),
                                       body.get("src") or "",
                                       body.get("src2") or "")
            _meta = set_pins(name, cid, _pins)
            return _pins, _meta
        _worked = await _io(_pins_work)
        if _worked is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        pins, meta = _worked
        if meta is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        # 换接法 = 换一种钉法，旧的剪头/剪尾账必须**当场作废**，不能等下一次
        # 生成来覆盖。理由不是洁癖：``trim_frames > 0`` 是「这一段接了别人的
        # 尾巴」唯一的外部证据（pins 是空列表时，分不清"从来没写过"和"明确
        # 设为独立起头"）。留着旧数，上一段就会为一个已经不存在的承接关系
        # 白钉一截 —— 见 head_parent 的注释。
        # 下一次生成会按新钉法重写这两个数，这里的归零只是把中间态堵死。
        # 只放**要清的那一头**：update_clip 是 meta.update(patch)，塞个 None
        # 进去等于把字段写成 None，另一头的有钉状态就跟着被抹了。
        _clear = {}
        if not any(p.get("at") == "head" for p in pins):
            _clear["trim_frames"] = 0
        if not any(p.get("at") == "tail" for p in pins):
            _clear["trim_tail"] = 0
        try:
            if _clear:
                await _io(update_clip, name, cid, _clear)
        except Exception:
            pass
        ch = await _io(load_chain, name)
        return _json({"id": cid, "pins": pins,
                      "mode": pin_mode(pins, [str(x) for x in
                                              (ch.get("order") or [])])})

    # 接缝成片转场（M3）：读走 /transitions_state、写走 /transitions（GET/POST
    # 分路径 —— 理由同 pins_state：热重载按路径换 handler、不认 method）。
    @PromptServer.instance.routes.get("/cglide/chain/transitions_state")
    async def _route_transitions_state(request):
        name = request.query.get("name") or "未命名"
        return _json({"transitions": await _io(transitions_of, name)})

    @PromptServer.instance.routes.post("/cglide/chain/transitions")
    async def _route_transitions(request):
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id") or ""
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        tt = await _io(set_transition, name, cid, body)
        return _json({"id": cid, "transitions": tt})

    @PromptServer.instance.routes.get("/cglide/chain/takes")
    async def _route_takes(request):
        """这一段抽过的所有版本（以磁盘为准）。面板「版本」菜单吃这个。"""
        name = request.query.get("name") or "未命名"
        cid = request.query.get("id") or ""
        rows = await _io(take_rows, name, cid)
        if rows is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        return _json(rows)

    @PromptServer.instance.routes.post("/cglide/chain/take")
    async def _route_take(request):
        """切换 / 删除某一段的版本。

        body: { name, id, take }            → 切到第 take 版（返回代价判定）
              { name, id, take, drop: true } → 删掉第 take 版
        """
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        cid = body.get("id")
        if not cid:
            return _json({"error": "缺少段落 id"}, 400)
        if body.get("drop"):
            res = drop_take(name, cid, body.get("take"))
        else:
            res = set_take(name, cid, body.get("take"), body.get("trim"))
        if res is None:
            return _json({"error": "没有这一段：%s" % cid}, 404)
        if res.get("ok") is False:
            return _json(res, 400)
        return _json(res)

    @PromptServer.instance.routes.get("/cglide/chain/export_plan")
    async def _route_export_plan(request):
        """导出之前的那张清单：到哪儿为止 + 哪些改动还没进片子。"""
        name = request.query.get("name") or "未命名"
        return _json(export_plan(name, request.query.get("at") or None))

    @PromptServer.instance.routes.post("/cglide/chain/sync_check")
    async def _route_sync_check(request):
        """「工程预设 vs 档案事实」冲突清单（P1-10）。前端把分段预设传过来，
        后端按 shot id 对齐档案，把对不上的段列出来 —— 只查不改。"""
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "请求体不是 JSON"}, 400)
        name = body.get("name") or "未命名"
        shots = body.get("shots")
        if not isinstance(shots, list):
            return _json({"error": "shots 必须是数组"}, 400)
        return _json(sync_check(name, shots))

    @PromptServer.instance.routes.get("/cglide/chain/export")
    async def _route_export(request):
        """把「到某一段为止的成片」作为一个文件下载出去。

        路径只从段落档案里读，不接受调用方传路径 —— 否则这个接口立刻变成
        一个任意读文件的洞。
        """
        from aiohttp import web
        from urllib.parse import quote

        name = request.query.get("name") or "未命名"
        plan = export_plan(name, request.query.get("at") or None)
        if not plan.get("ok"):
            return _json({"error": plan.get("message") or "没有可导出的成片"}, 404)

        path = os.path.abspath(plan.get("path") or "")
        out_root = os.path.abspath(_out_dir())
        # normcase：Windows 上盘符大小写不统一，直接 startswith 会误判成越界
        if (not os.path.isfile(path)
                or not os.path.normcase(path).startswith(
                    os.path.normcase(out_root + os.sep))):
            return _json({"error": "成片已经不在输出目录里了：%s" % path}, 404)

        return web.FileResponse(path, headers={
            "Content-Type": "video/mp4",
            # 中文片名走 filename*，否则浏览器存下来的文件名是一串问号
            "Content-Disposition":
                "attachment; filename=\"film.mp4\"; filename*=UTF-8''%s"
                % quote(plan.get("file") or "film.mp4"),
        })

    @PromptServer.instance.routes.get("/cglide/chain/export_film_plan")
    async def _route_export_film_plan(request):
        """「保存成片」动手前的清单：所有段拼接的方案（净长口径）。"""
        name = request.query.get("name") or "未命名"
        return _json(export_film_plan(name, request.query.get("at") or None))

    @PromptServer.instance.routes.post("/cglide/chain/export_film")
    async def _route_export_film(request):
        """真正动手拼：时间轴上所有段按净长拼接写盘。

        拼接要重编码、耗时随片长，**必须丢进线程池** —— 在事件循环里同步跑
        会把整个 ComfyUI（队列刷新、进度轮询）一起冻住。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = (body or {}).get("name") or "未命名"
        at = (body or {}).get("at") or None
        # get_running_loop：协程内的规范写法（get_event_loop 在新 Python 里已弃用）
        res = await asyncio.get_running_loop().run_in_executor(
            None, lambda: export_film(name, at))
        return _json(res)

    @PromptServer.instance.routes.get("/cglide/chain/export_film_file")
    async def _route_export_film_file(request):
        """把档案里的整片成片读回去 —— 前端「另存为」下载用。

        路径只从片名推导（chain_dir/<片名>/<片名>_成片.mp4），不接受调用方
        传路径，和 /chain/export 同一个安全口径。"""
        from aiohttp import web
        from urllib.parse import quote

        name = request.query.get("name") or "未命名"
        path = os.path.join(chain_dir(name), "%s_成片.mp4" % _safe_file_part(name))
        if not os.path.isfile(path):
            return _json({"error": "档案里还没有成片 —— 先保存一次"}, 404)
        return web.FileResponse(path, headers={
            "Content-Type": "video/mp4",
            # 中文片名走 filename*，否则浏览器存下来的文件名是一串问号
            "Content-Disposition":
                "attachment; filename=\"film.mp4\"; filename*=UTF-8''%s"
                % quote(_safe_file_part(name) + "_成片.mp4"),
        })

    # 历史旧工程（轴头「历史」按钮）：列表 + 整包加载。只读扫描，全部走
    # 线程池，不堵事件循环。
    @PromptServer.instance.routes.get("/cglide/chain/history/list")
    async def _route_history_list(request):
        include_sys = (request.query.get("include_sys") or "") in ("1", "true", "yes")
        res = await _io(history_list, include_sys)
        return _json(res)

    @PromptServer.instance.routes.get("/cglide/chain/history/bundle")
    async def _route_history_bundle(request):
        name = request.query.get("name") or ""
        if not name:
            return _json({"error": "缺少工程名"}, 400)
        res = await _io(history_bundle, name)
        if res.get("error"):
            return _json(res, 404)
        return _json(res)

    @PromptServer.instance.routes.post("/cglide/chain/history/reveal")
    async def _route_history_reveal(request):
        """在资源管理器里打开档案目录（没给片名打开 h3_chain 根）。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = body.get("name") or ""
        def _open():
            path = chain_dir(name) if name else os.path.join(_out_dir(), "h3_chain")
            if not os.path.isdir(path):
                return {"error": "目录不存在"}
            try:
                os.startfile(path)
            except Exception as e:
                return {"error": str(e)}
            return {"ok": True}
        res = await _io(_open)
        return _json(res, 404 if res.get("error") else 200)

    @PromptServer.instance.routes.post("/cglide/chain/history/delete")
    async def _route_history_delete(request):
        """一键删除整个工程目录（无确认，前端不弹框）。当前工程由前端隐藏入口。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        name = body.get("name") or ""
        res = await _io(delete_project, name)
        return _json(res, 400 if res.get("error") else 200)

    def _json(data, status=200):
        from aiohttp import web
        return web.json_response(data, status=status)

    # 状态库 /cglide/store/* 路由（20261012 方案 A）：在同一 try 块内、
    # PromptServer.instance 确认就绪后注册。
    try:
        store.register_routes()
    except Exception as _se:
        print("⚠️ [小语的工作台] SQLite 状态库路由注册失败: %s" % _se)

except Exception as _e:      # pragma: no cover
    print("⚠️ [小语的工作台] H3 段落档案路由注册失败（节点仍可用，仅时间轴面板受影响）: %s" % _e)
