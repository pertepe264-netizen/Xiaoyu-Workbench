# -*- coding: utf-8 -*-
import torch
import gc
import comfy.model_management
import ctypes
from ctypes import wintypes
import os
import sys
import time
import datetime
import threading

from .banzhang_sysinfo import get_gpu_info, get_ram_info
from .banzhang_fast_terminate import _check_interrupt, _safe_force_clear_vram, get_vram_breakdown, aimdo_enabled

# --------------------------------------------------------------------------------
#  👨‍🏫 显存与系统节点
#  提供两个节点：
#    🧹 全能系统净化 —— 卸载模型 + 清缓存 + 回收 aimdo VBAR + 系统内存整理 + 提示音
#    🛡️⚡ 显存守卫   —— 显存预留（物理占位 + 内核封锁）+ 快速中断
#
#  近期变更：
#   - 合并原「显存预留卫士」「全能快速中断」为「显存守卫」，二者已下线。
#   - 显存守卫改为非破坏性：不再卸载模型，可放在采样器之前。
#   - 预留量不再叠加 physical_used，修掉外部占用被重复扣减的 bug。
#   - 净化节点新增 aimdo VBAR 深度回收与分池读数（解释「释放了但读数不降」）。
# --------------------------------------------------------------------------------

# 🟢 1. 定义万能类型
class 万能类型(str):
    def __ne__(self, __value: object) -> bool:
        return False
任意接口 = 万能类型("*")

# 🟢 2. 定义内存结构体 (Windows API 备用)
class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('PageFaultCount', wintypes.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
        ('PrivateUsage', ctypes.c_size_t),
    ]

# 🟢 3. 共享辅助函数：全能显存获取（统一走 banzhang_sysinfo，单位 GB）
def get_robust_gpu_info():
    total, used, source = get_gpu_info()
    gb = 1024 ** 3
    return total / gb, used / gb, source


# 🟢 3b. 系统内存读取
try:
    import psutil as _psutil
except ImportError:
    _psutil = None


def get_process_ram_bytes():
    """本进程占用的物理内存（字节）；读不到返回 None。"""
    if _psutil is None:
        return None
    try:
        return _psutil.Process().memory_info().rss
    except Exception:
        return None


#    Windows 句柄必须显式声明宽度：GetCurrentProcess() 返回的是 64 位伪句柄
#    (HANDLE)-1，不声明 restype 时 ctypes 按 32 位 int 取值，再传给未声明
#    argtypes 的 EmptyWorkingSet，句柄已经不是原来那个值了 —— API 返回失败，
#    且不抛异常，于是「压缩内存」静默失效了很久。
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_kernel32.GetCurrentProcess.argtypes = []
_psapi.EmptyWorkingSet.restype = wintypes.BOOL
_psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]


def trim_process_working_set():
    """把本进程的物理内存占用压到最小。返回 (是否成功, 失败原因)。"""
    ctypes.set_last_error(0)
    try:
        ok = _psapi.EmptyWorkingSet(_kernel32.GetCurrentProcess())
    except Exception as e:
        return False, str(e)
    if ok:
        return True, ""
    return False, f"EmptyWorkingSet 返回失败（错误码 {ctypes.get_last_error()}）"


# 🟢 3d. 彻底丢弃模型（真·释放系统内存）
#    ComfyUI 的 unload_all_models() 语义是「搬走」，不是「扔掉」：
#    权重从显存挪到内存条，ModelPatcher 对象还活在 Python 里。
#    于是显存降了、内存反涨——内存条的占用一分没少，这正是「勾了卸载却只是
#    搬到内存」的由来。
#    要真的释放，必须清掉这些 ModelPatcher 手里的权重存储。用 PyTorch 官方的
#    to_empty(device="meta") 把参数和缓冲区换成不带存储的 meta 张量即可。
#    注意：必须遍历 gc 才能抓全。执行器、各个节点对象都握着引用，只清
#    current_loaded_models 是删不干净的。
#
#    ⚠️ 时机：绝不能在本节点执行时就动手。工作流里净化节点后面往往还挂着
#    别的节点（VAE 解码、保存音频等），权重一被扔成 meta 张量，它们再去加载
#    就报「Cannot copy out of meta tensor; no data!」。
#    所以这里只「登记待办」，等整轮执行结束再由钩子统一释放 —— 放哪都不崩。
_pending_drop = False
_drop_hook_installed = False
_executor_ref = None      # 由钩子顺手记下执行器实例，用来清节点缓存


def _install_prompt_end_hook():
    """给 ComfyUI 执行器挂「本轮结束」回调，只需挂一次。"""
    global _drop_hook_installed
    if _drop_hook_installed:
        return True
    try:
        import execution
    except Exception as e:
        print(f"[净化] ⚠️ 无法挂载延期释放钩子: {e}")
        return False

    cls = getattr(execution, "PromptExecutor", None)
    orig = getattr(cls, "execute_async", None) if isinstance(cls, type) else None
    if orig is None:
        print("[净化] ⚠️ 当前 ComfyUI 的执行器接口不符，延期释放不可用")
        return False

    async def _wrapped(self, *args, **kwargs):
        global _executor_ref
        _executor_ref = self
        try:
            return await orig(self, *args, **kwargs)
        finally:
            _flush_pending_drop()

    cls.execute_async = _wrapped
    _drop_hook_installed = True
    return True


def queue_model_drop():
    """登记「本轮结束后释放模型」。返回 (是否成功, 失败原因)。"""
    global _pending_drop
    if not _install_prompt_end_hook():
        return False, "当前 ComfyUI 的执行器接口不符，无法延期释放"
    _pending_drop = True
    return True, ""


def _flush_pending_drop():
    """本轮执行结束：先立刻清节点缓存，再把释放权重交给后台线程。

    清缓存必须在这一刻同步做完：缓存里存着「加载模型」节点的输出对象，
    模型权重被掏空后它们就成了指向空壳的幽灵。不马上清掉，下一轮 ComfyUI
    会认定那些节点已经跑过、直接复用并跳过加载。
    放后台做又可能撞上用户紧接着提交的下一轮，所以这里只做毫秒级的 reset，
    慢的权重释放才丢给线程。
    """
    global _pending_drop
    if not _pending_drop:
        return
    _pending_drop = False

    cache_cleared = False
    if _executor_ref is not None:
        try:
            _executor_ref.reset()
            cache_cleared = True
        except Exception as e:
            print(f"[净化] ⚠️ 清空节点缓存失败: {e}")

    threading.Thread(target=_do_drop_and_report, args=(cache_cleared,), daemon=True).start()


def _do_drop_and_report(cache_cleared):
    n, why = drop_loaded_models()
    if why:
        print(f"[净化] ⚠️ 模型延期释放失败: {why}")
        return
    tail = ("，节点缓存已清空，下一轮会重新加载模型"
            if cache_cleared else
            "；⚠️ 节点缓存未清空，下一轮可能仍跳过加载（请手动点一下 Clear Cache）")
    print(f"[净化] 🗑️ 本轮已跑完，清空 {n} 个模型对象权重，内存已释放{tail}")


def drop_loaded_models():
    """彻底释放 ComfyUI 持有的模型权重。返回 (处理掉的对象数, 失败说明)。"""
    try:
        import comfy.model_management as mm
        import comfy.model_patcher as mp
    except Exception as e:
        return 0, f"导入内核模块失败: {e}"

    mm.unload_all_models()
    try:
        mm.soft_empty_cache()
    except Exception:
        pass
    for lm in list(mm.current_loaded_models):
        try:
            lm.model_unload()
        except Exception:
            pass
    mm.current_loaded_models.clear()

    patcher_cls = getattr(mp, "ModelPatcher", None)
    if not isinstance(patcher_cls, type):
        return 0, "comfy.model_patcher.ModelPatcher 不可用"

    dropped = 0
    for obj in gc.get_objects():
        if not isinstance(obj, patcher_cls):
            continue
        model = getattr(obj, "model", None)
        if model is None or not hasattr(model, "to_empty"):
            continue
        try:
            model.to_empty(device="meta")
            dropped += 1
        except Exception:
            continue
    gc.collect()
    return dropped, ""


# ⛔ 已下线：禁用 Dynamic VRAM (aimdo)
#    ─────────────────────────────────────────────────────────────────────────
#    这里原本有一对函数（ensure_aimdo_disabled / _aimdo_actually_disabled），
#    做法是把 aimdo 关掉，好让 EXTRA_RESERVED_VRAM 生效：
#        comfy.memory_management.aimdo_enabled = False
#        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcher
#    它们已被删除，请勿再引入。原因（有实测证据，勿凭印象改回去）：
#
#      关掉 aimdo 的副作用是「之后载入的每一个模型都不再分页流式加载」，
#      而是整份搬进显存。用户 2026-10-05 的日志完整记录了这个因果链：
#        · 关之前：Model MiniMaxH3TEModel_ prepared for dynamic VRAM loading.
#                  14969MB Staged（0.1 秒）→ 导演台 14 秒，节点显存 1.3 GB
#        · 关之后：loaded completely; 14970.10 MB loaded, full load: True（21 秒）
#                  → 导演台 2 分 59 秒，节点显存 16.0 GB，OutOfMemoryError
#      也就是说，为了「留 2 GB 给别的程序」，把一台 24 GB 的卡让出了 15 GB。
#
#      而且这个关法还会**反噬它自己的目的**：_release_aimdo_vbar() 开头就是
#      `if not aimdo_enabled(): return 0, 0`，先关掉它 = 让 VBAR 深度回收整段空转。
#      用户日志里「VBAR卸载: 0 个 / 约 0.00 GB」就是这么来的。
#
#    正确做法：保持 aimdo 开启，用它自己的旋钮 ——
#      · 要预留显存 → aimdo_set_headroom()（见下面 🟢 4b）
#      · 要回收驻留页 → _release_aimdo_vbar() / release_model_residency()（见下）
#      · 若发现 aimdo 在运行期被人关掉了 → ensure_aimdo_healthy() 自动开回来
#
#    main.py:276-305 的启动链条（判断 aimdo「本次启动是否启用」的唯一依据）：
#      init_devices() 成功 → 填 comfy_aimdo.control.devctxs
#                          → CoreModelPatcher = ModelPatcherDynamic
#                          → memory_management.aimdo_enabled = True
#      所以 devctxs 非空 ⟺ 本次启动启用了 DynamicVRAM。
#    用 --disable-dynamic-vram 启动时 devctxs 为空 —— 那种情况下不能硬开回来。


# 🟢 4. aimdo (DynamicVRAM) 深度回收
#    ─────────────────────────────────────────────────────────────────────────
#    为什么必须单独处理：
#      启用 DynamicVRAM 后，ComfyUI 用 ModelPatcherDynamic 把权重钉进 aimdo 的
#      ModelVBAR 池（VBAR = 虚拟显存地址空间，aimdo 自己 fault/unpin 那几页）。
#      这些页**不是** PyTorch caching allocator 的块，所以：
#        torch.cuda.empty_cache() —— 清的是一个空池，对 VBAR 一点用没有；
#        unload_all_models()     —— 只能把「已加载的 patcher」摘下来，
#                                   VBAR 内部的驻留页依然在占着显存。
#      这就是「每次都释放 6GB / 15.99GB，剩余的一直不降」的真正原因：
#      不是 cudaMallocAsync 的 reserved 不归还（那是 torch 自己的池），
#      而是有个 torch 根本碰不到的占用方 —— aimdo VBAR。
#
#    释放路径：ModelVBAR.free_memory()  →  vbar_free_memory（卸掉驻留页）
#              最后 aimdo 才可能把页还给驱动。
_dyn_patcher_cls = None


def _dynamic_patcher_class():
    """取出 ModelPatcherDynamic 类（aimdo 关闭时为 None）。"""
    global _dyn_patcher_cls
    if _dyn_patcher_cls is None:
        try:
            import comfy.model_patcher
            _dyn_patcher_cls = getattr(comfy.model_patcher, "ModelPatcherDynamic", None)
        except Exception:
            return None
    return _dyn_patcher_cls


def _release_aimdo_vbar(aggressive: bool = True) -> tuple[int, int]:
    """回收 aimdo VBAR 里已加载但不用的权重页。

    返回 (卸载的 patcher 数, 释放的字节数估计)。aimdo 未启用时直接返回 (0, 0)。
    """
    if not aimdo_enabled():
        return 0, 0
    cls = _dynamic_patcher_class()
    if cls is None:
        return 0, 0

    freed = 0
    n = 0
    try:
        loaded = list(comfy.model_management.current_loaded_models)
    except Exception:
        return 0, 0

    for lm in loaded:
        patcher = getattr(lm, "model", None)
        if not isinstance(patcher, cls):
            continue
        # 先看这块 VBAR 当前驻留了多少
        try:
            vbar = patcher._vbar_get()
            before = vbar.loaded_size() if vbar is not None else 0
        except Exception:
            before = 0

        # 全部驻留页卸掉
        try:
            if vbar is not None:
                vbar.free_memory(1 << 62)
        except Exception:
            pass
        try:
            patcher.unload()
        except Exception:
            pass
        try:
            if vbar is not None:
                after = vbar.loaded_size()
                if after < before:
                    freed += before - after
        except Exception:
            pass
        n += 1

    # 收尾：让 aimdo 立刻回收已释放的页，并复位水位线（否则它会按旧水位继续占）
    if aggressive and n:
        try:
            import comfy_aimdo.model_vbar as mvbar
            mvbar.vbars_analyze()
            mvbar.vbars_reset_watermark_limits()
        except Exception:
            pass
        try:
            import comfy_aimdo.control as aimdo_control
            aimdo_control.analyze()
        except Exception:
            pass
    return n, freed


# 🟢 4b. aimdo 原生预留（替代「关掉 DynamicVRAM」那套写法）
#    ─────────────────────────────────────────────────────────────────────────
#    旧写法为了让 EXTRA_RESERVED_VRAM 生效，直接把 aimdo 关掉：
#        comfy.memory_management.aimdo_enabled = False
#        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcher
#    代价极大：CoreModelPatcher 一旦被指回经典版，**之后载入的每一个模型都不再
#    分页流式加载**，而是整份搬进显存。实测证据（用户 2026-10-05 的日志）：
#        · 关之前：Model MiniMaxH3TEModel_ prepared for dynamic VRAM loading.
#                  14969MB Staged（0.1 秒）→ 导演台 14 秒跑完，节点显存 1.3 GB
#        · 关之后：loaded completely; 14970.10 MB loaded, full load: True（21 秒）
#                  → 导演台 2 分 59 秒，节点显存 16.0 GB，最后 OutOfMemoryError
#    也就是说，为了「留 2 GB 给别的程序」，把一台 24 GB 的卡让出了 15 GB。
#
#    aimdo 自己就有原生预留旋钮，根本不用关它：
#        comfy_aimdo.control.set_simple_vram_headroom(字节)
#    它是软预算：aimdo 会为了保住这个空闲量去驱逐自己的 VBAR 页；按内核自己的
#    说明，「torch 的分配会被计入但不被拒绝」（control.py:215-235）——所以物理
#    占位那一层仍然保留，用来兜住硬保证。两者叠加才是「ComfyUI 最多用到总量 − N」。
#
#    可用性判据必须取 comfy_aimdo.control.devctxs：
#        main.py:276-305 只有 ctl.init_devices() 成功时才填它，并同时把
#        CoreModelPatcher 切到动态版、aimdo_enabled 置 True。
#        所以 devctxs 非空 ⟺ 本次启动确实启用了 DynamicVRAM。
#    不能拿 args.disable_dynamic_vram 之类的参数去猜：用户可能用
#    --disable-dynamic-vram 启动（devctxs 为空 → 不能硬开），也可能被别的节点在
#    运行期关掉（devctxs 还在 → 应该开回来）。这是两种完全不同的情况。
def aimdo_native_ready():
    """aimdo 是否真的初始化过 —— 这是它能不能用的唯一判据。返回 (可用, 原因)。"""
    try:
        import comfy_aimdo.control as _ctl
    except Exception as e:
        return False, "comfy-aimdo 不可用（%s）" % type(e).__name__
    if getattr(_ctl, "lib", None) is None:
        return False, "comfy-aimdo 运行库未加载"
    if not getattr(_ctl, "devctxs", None):
        return False, ("本次启动没有启用 DynamicVRAM（多半用了 --disable-dynamic-vram），"
                       "aimdo 原生预留不可用")
    return True, ""


def aimdo_get_headroom():
    """当前 aimdo 原生预留量（字节）。取不到返回 None。"""
    ok, _ = aimdo_native_ready()
    if not ok:
        return None
    try:
        import comfy_aimdo.control as _ctl
        return int(_ctl.get_simple_vram_headroom())
    except Exception:
        return None


def aimdo_set_headroom(nbytes):
    """设置 aimdo 原生预留量（字节），返回 (是否成功, 原因)。

    注意「调小」不会主动把页填回来：页要等下一次模型载入才回来（与内核说明一致）。
    """
    ok, why = aimdo_native_ready()
    if not ok:
        return False, why
    try:
        import comfy_aimdo.control as _ctl
        _ctl.set_simple_vram_headroom(int(nbytes))
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    # 回读确认，不靠「我设过了」自证 —— 静默失效比报错更难查
    got = aimdo_get_headroom()
    if got is None:
        return False, "设置后读不回预留量"
    if got != int(nbytes):
        return False, "设置后回读不一致（设 %d，读到 %d）" % (int(nbytes), got)
    return True, ""


def ensure_aimdo_healthy():
    """aimdo 本该开着却被人关掉了 → 自动开回来。返回 (是否发生纠正, 说明)。

    这是本次 OOM 的正面修法。运行期关掉 aimdo 的代价是「之后载入的所有模型都退回
    整份全量加载」，而调用它的出发点往往只是临时腾一点显存 —— 完全不值得。
    """
    try:
        import comfy.memory_management
        import comfy.model_patcher
    except Exception as e:
        return False, "导入内核模块失败: %s" % e

    if not hasattr(comfy.memory_management, "aimdo_enabled"):
        return False, ""

    ready, why = aimdo_native_ready()
    if not ready:
        # 本次启动就没启用（例如 --disable-dynamic-vram）→ 不要擅自打开
        return False, ""

    dyn_cls = getattr(comfy.model_patcher, "ModelPatcherDynamic", None)
    if not isinstance(dyn_cls, type):
        return False, ""

    need_fix = (not comfy.memory_management.aimdo_enabled
                or getattr(comfy.model_patcher, "CoreModelPatcher", None) is not dyn_cls)

    if not need_fix:
        return False, ""

    comfy.memory_management.aimdo_enabled = True
    comfy.model_patcher.CoreModelPatcher = dyn_cls

    back_ok = (bool(comfy.memory_management.aimdo_enabled)
               and getattr(comfy.model_patcher, "CoreModelPatcher", None) is dyn_cls)
    if back_ok:
        return True, ("检测到 DynamicVRAM 被提前关掉了（这会让之后载入的模型"
                      "全部退回整份全量加载，15 GB 的文本编码器会整份占满显存），"
                      "已自动开回来")
    return False, "尝试恢复 DynamicVRAM 未生效，请检查是否有其他节点仍在关闭它"


# 🟢 5. 用完就还：把某一个模型占的显存驻留页交还系统（可逆）
#    场景：导演台把提示词交给文本编码器编完就再也不碰它了，可它占的显存会一直
#    挂到工作流结束，后面的扩散模型 + LoRA 就得跟它抢。
#
#    为什么是「丢页」而不是「销毁」：
#      丢页 = partially_unload_ram()（弹 pin 栈 + hostbuf.truncate）+ vbar.free_memory()。
#      权重仍在原 safetensors 里 —— aimdo 给每个张量挂了 _comfy_tensor_file_slice
#      （comfy/utils.py:145-168），下次要用会自动重新读回来。代价是重新读一遍数据。
#      而 to_empty("meta") 那种「彻底销毁」不可逆：CLIPLoader 的输出对象被 ComfyUI
#      缓存下来跨运行复用（model_patcher.py 的 cached_patcher_init 只在
#      deepclone_multigpu 里消费），第二次跑同一个工作流会拿到被掏空的 CLIP 直接报错。
#
#    实测（K: 上 1.71 GB 的 CLIP-L，aimdo 已启用、fast_disk=True）：
#      · partially_unload_ram(1e32) 释放 0 字节 —— fast_disk=True 时权重压根不驻留
#        内存，是「文件→显存」直通（comfy/ops.py:188-191：fast_disk 时连内存 pin 都不建）
#      · partially_unload(None, 1e32) 释放 268.6 MB 显存
#      · 丢页后重新编码比「热的」多 0.221 s / 1.71 GB = 7.7 GB/s（页缓存命中）
#      · 真读盘按 1395 MB/s 算 = 0.72 s/GB
#        → 15.7 GB 的 H3 文本编码器 ≈ 2.0 s（页缓存热）/ ≈ 11.3 s（真读盘）
#      · 用户自己日志可交叉验证：首次编 15 GB 用了 21 s（≈1.4 GB/s，读盘）；
#        紧接着第二次只用了 10 s 上下（页缓存热）—— 与上面的两个边界吻合。
#
#    经典 patcher（DynamicVRAM 被关掉时）：内核没有「丢页」API，只能把权重挪回
#    offload 设备（内存条）。那等于把显存压力换成内存压力，所以先量内存够不够，
#    不够就什么都不做 —— 宁可少还一点，也不能把机器拖进交换。
def release_model_residency(patcher, model_size=None, log=None, label="模型"):
    """把某个 patcher 的驻留页交还系统，返回 (释放的内存字节, 释放的显存字节)。

    label 只用于日志。log 传一个 callable(str) 或不传。
    """
    def _say(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    if patcher is None:
        return 0, 0

    try:
        import comfy.model_management as _mm
    except Exception:
        _mm = None

    vram_before = 0
    if _mm is not None:
        try:
            vram_before = int(_mm.get_free_memory())
        except Exception:
            pass

    try:
        is_dyn = bool(patcher.is_dynamic())
    except Exception:
        is_dyn = False

    ram_freed = 0
    vram_freed = 0

    if is_dyn:
        # ── 动态路径：可逆丢页 ──────────────────────────────────────────
        try:
            ram_freed = int(patcher.partially_unload_ram(1e32) or 0)
        except Exception as e:
            _say("  %s：内存 pin 回收失败（%s），跳过。" % (label, type(e).__name__))
        try:
            vram_freed = int(patcher.partially_unload(None, 1e32) or 0)
        except Exception as e:
            _say("  %s：显存页回收失败（%s），跳过。" % (label, type(e).__name__))
        # 让 aimdo 立刻把刚交还的页真正放掉，并复位水位线
        # （不然它会按旧水位继续占着，读数看不到下降）
        try:
            import comfy_aimdo.model_vbar as _mvbar
            _mvbar.vbars_analyze()
            _mvbar.vbars_reset_watermark_limits()
        except Exception:
            pass
    else:
        # ── 经典路径：只能「搬走」，先量内存 ────────────────────────────
        off = getattr(patcher, "offload_device", None)
        size = int(model_size or 0)
        if not size:
            try:
                size = int(patcher.model_size())
            except Exception:
                size = 0
        avail = None
        try:
            import comfy.system_memory as _sm
            avail = int(_sm.virtual_memory_available())
        except Exception:
            avail = None
        if off is None:
            _say("  %s：拿不到 offload 设备，跳过回收。" % label)
            return 0, 0
        # 需要 size + 512 MB 余量。不够就别搬：那是把显存压力换成内存压力。
        need = size + 512 * 1024 ** 2
        if avail is None or avail < need:
            _say("  %s：DynamicVRAM 未启用，内核没有可逆丢页；把 %.1f GB 挪到内存条"
                 "需要 %.1f GB、当前只剩 %.1f GB —— 已跳过，不冒拖垮内存的风险。"
                 % (label, size / 1e9, need / 1e9, (avail or 0) / 1e9))
            return 0, 0
        try:
            vram_freed = int(patcher.partially_unload(off, 1e32) or 0)
        except Exception as e:
            _say("  %s：权重挪回内存失败（%s），跳过。" % (label, type(e).__name__))
            return 0, 0

    if _mm is not None:
        try:
            _mm.soft_empty_cache()
        except Exception:
            pass

    # 实测空闲显存增量留作交叉验证（官方返回值有时是「估算」，实际可能更少）
    if _mm is not None and vram_before:
        try:
            delta = int(_mm.get_free_memory()) - vram_before
            if delta > vram_freed:
                vram_freed = delta
        except Exception:
            pass

    return max(0, ram_freed), max(0, vram_freed)


# =============================================================================
#  NODE 1: 🧹 全能系统净化
# =============================================================================
class BanZhang_VRAM_Cleaner_Node:
    """
    🧹 全能净化节点 (v7.0)
    """
    def __init__(self):
        pass

    @classmethod
    def INPUT_TYPES(cls):
        current_dir = os.path.dirname(os.path.abspath(__file__))
        sounds_dir = os.path.join(current_dir, "sounds")
        sound_files = ["notify.mp3"]
        if os.path.exists(sounds_dir):
            scanned_files = [f for f in os.listdir(sounds_dir) if f.lower().endswith(('.mp3', '.wav'))]
            if scanned_files:
                sound_files = scanned_files
                if "notify.mp3" in sound_files:
                    sound_files.remove("notify.mp3")
                    sound_files.insert(0, "notify.mp3")
        
        return {
            "required": {
                "提示音_播放开关": ("BOOLEAN", {"default": True, "label_on": "🔊 开启声音", "label_off": "🔇 静音", "tooltip": "任务完成后是否播放提示音"}),
                "提示音_文件": (sound_files, {"default": "notify.mp3", "tooltip": "选择 sounds 文件夹下的音频文件"}),
                "提示音_音量": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.1, "tooltip": "提示音的播放音量"}),

                "显存_卸载模型": ("BOOLEAN", {"default": True, "label_on": "是 (强制卸载)", "label_off": "否 (仅清缓存)", "tooltip": "【重要】任务结束后是否把模型踢出显存？\n默认踢出后模型会搬到内存条上；想连内存一起腾空，请看最下面的「模型_彻底丢弃」。"}),
                "显存_清理缓存": ("BOOLEAN", {"default": True, "label_on": "是 (清理)", "label_off": "否 (保留)", "tooltip": "清理 PyTorch 显存碎片"}),
                
                "内存_清理文件缓存": ("BOOLEAN", {"default": True, "label_on": "是 (GC回收)", "label_off": "否", "tooltip": "Python 垃圾回收"}),
                "内存_清理进程内存": ("BOOLEAN", {"default": True, "label_on": "是 (压缩内存)", "label_off": "否", "tooltip": "压缩物理内存"}),
                "内存_清理DLL": ("BOOLEAN", {"default": True, "label_on": "是 (释放DLL)", "label_off": "否", "tooltip": "Windows 没有安全的运行时卸载 DLL 方式，本开关与「压缩内存」执行同一个动作。"}),
                
                "重试次数": ("INT", {"default": 1, "min": 1, "max": 5, "step": 1, "tooltip": "循环清理次数"}),

                "模型_彻底丢弃": ("BOOLEAN", {"default": False, "label_on": "是 (真删除)", "label_off": "否 (搬到内存)", "tooltip": "配合「显存_卸载模型」使用。\n"
                    "关：模型从显存搬到内存条，占用还在，下次用能秒回来。\n"
                    "开：把权重数据真的扔掉，内存条也一起腾空。\n"
                    "\n"
                    "【什么时候动手】本节点执行时不会立刻释放，而是先排队，\n"
                    "等整轮工作流全部跑完才释放 —— 放哪都不会影响后面的节点。\n"
                    "释放结果看控制台（黑窗口）里的「🗑️ 本轮已跑完」那行。\n"
                    "\n"
                    "代价：下一轮生成要重新从硬盘读模型，会慢一些。"}),
            },
            "optional": {
                "信号触发入口": (任意接口,), 
            }
        }

    RETURN_TYPES = (任意接口, "STRING")
    RETURN_NAMES = ("信号直通出口", "监控面板")
    FUNCTION = "execute_clean_task"
    CATEGORY = "👨‍🏫班长/显存与系统"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("NaN")
    
    DESCRIPTION = """
👨‍🏫 班长的小课堂 - 全能系统净化 (v7.0)
"""

    def get_system_ram_info(self):
        """返回 (已用 GB, 总 GB)，统一走 banzhang_sysinfo。"""
        total, used = get_ram_info()
        gb = 1024 ** 3
        return (used / gb, total / gb)

    # 播放声音线程
    def play_audio_thread(self, file_path, volume):
        try:
            if not os.path.isabs(file_path):
                current_dir = os.path.dirname(os.path.abspath(__file__))
                file_path = os.path.join(current_dir, "sounds", file_path)
            if not os.path.exists(file_path): return
            buffer = ctypes.create_unicode_buffer(260)
            ctypes.windll.kernel32.GetShortPathNameW(file_path, buffer, 260)
            short_path = buffer.value
            alias = "bz_audio_notify"
            mciSendString = ctypes.windll.winmm.mciSendStringW
            mciSendString(f"close {alias}", None, 0, 0)
            open_cmd = f"open \"{short_path}\" type mpegvideo alias {alias}"
            error = mciSendString(open_cmd, None, 0, 0)
            if error:
                open_cmd = f"open \"{short_path}\" alias {alias}"
                mciSendString(open_cmd, None, 0, 0)
            vol_val = int(volume * 1000)
            mciSendString(f"setaudio {alias} volume to {vol_val}", None, 0, 0)
            mciSendString(f"play {alias} wait", None, 0, 0)
            mciSendString(f"close {alias}", None, 0, 0)
        except Exception as e: print(f"❌ [班长声音] 播放出错: {e}")

    def execute_clean_task(self, 提示音_播放开关, 提示音_文件, 提示音_音量, 显存_卸载模型, 显存_清理缓存, 内存_清理文件缓存, 内存_清理进程内存, 内存_清理DLL, 重试次数=1, 模型_彻底丢弃=False, 信号触发入口=None):
        start_time = datetime.datetime.now().strftime("%H:%M:%S")

        # 0. aimdo 状态确认（保持开启）
        #    旧版这里先 ensure_aimdo_disabled()，理由是「让权重不再回流到 VBAR 池」。
        #    那是反的，而且会反噬它自己的目的 —— 两个独立的问题：
        #      ① _release_aimdo_vbar() 开头就是 `if not aimdo_enabled(): return 0, 0`，
        #         先关掉它 = 让下面那段 VBAR 深度回收整段空转。用户日志里
        #         「VBAR卸载: 0 个 / 约 0.00 GB」就是这么来的。
        #      ② aimdo 一旦被关，之后载入的每个模型都退回整份全量加载
        #         （15.7 GB 文本编码器整份进显存），这是 2026-10-05 那次 OOM 的起点。
        #    正确做法：保持 aimdo 开启，用它的原生接口清页；发现被人关了就开回来。
        if 显存_卸载模型:
            _fixed, _fix_note = ensure_aimdo_healthy()
            if _fixed:
                print("[净化] ⚙️ " + _fix_note)

        # 1. 记录清理前状态
        #    注意：这里改用 get_vram_breakdown()，把 aimdo 池与 torch 池分开记，
        #    否则物理读数是一个混了 VBAR 的总数，清理前后对比看不出真实效果。
        before = get_vram_breakdown()
        gpu_used_before, gpu_total = before["used"] / 1024**3, before["total"] / 1024**3
        if gpu_total == 0: gpu_total = 24.0

        ram_used_before, ram_total = self.get_system_ram_info()
        proc_before = get_process_ram_bytes()

        print(f"\n🧹 [班长净化 v7.0] 任务开始")

        clean_status = "✅ 成功"
        error_msg = ""
        trim_error = ""
        vbar_unloaded, vbar_freed = 0, 0
        drop_queued, drop_error = False, ""

        if torch.cuda.is_available():
            torch.cuda.synchronize()

        # 2. 执行大扫除
        for i in range(重试次数):
            try:
                模型管理器 = comfy.model_management

                if 内存_清理文件缓存:
                    gc.collect()
                    gc.collect()

                if 显存_卸载模型:
                    模型管理器.unload_all_models()
                    try: 模型管理器.free_memory(100 * 1024**3, torch.device("cuda"))
                    except:
                        try: 模型管理器.free_memory(torch.device("cpu"), torch.device("cuda"))
                        except: pass
                    if hasattr(模型管理器, "cleanup_models"):
                        try: 模型管理器.cleanup_models()
                        except: pass

                    # ── 关键：清 aimdo VBAR 的驻留页 ──
                    # unload_all_models 只摘 patcher，VBAR 里的页还在占显存。
                    # 这一步才是「把剩下的那几 GB 真正要回来」的动作。
                    try:
                        un, fr = _release_aimdo_vbar(aggressive=True)
                        vbar_unloaded += un
                        vbar_freed += fr
                    except Exception as e:
                        print(f"[净化] ⚠️ VBAR 回收出错: {e}")

                    gc.collect()

                if 显存_清理缓存:
                    模型管理器.soft_empty_cache()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                        torch.cuda.ipc_collect()

                if 内存_清理进程内存 or 内存_清理DLL:
                    ok, why = trim_process_working_set()
                    if not ok:
                        trim_error = why

                if i < 重试次数 - 1: time.sleep(0.2)

            except Exception as 错误:
                clean_status = "⚠️ 部分完成"
                error_msg = str(错误)

        if torch.cuda.is_available():
            torch.cuda.synchronize()

        time.sleep(0.5)

        # 3. 登记「本轮跑完后释放模型」
        #    必须等本轮结束：后面可能还有节点要用这些模型。
        if 模型_彻底丢弃:
            drop_queued, drop_error = queue_model_drop()

        # 4. 记录清理后状态
        after = get_vram_breakdown()
        gpu_used_after = after["used"] / 1024**3
        ram_used_after, _ = self.get_system_ram_info()
        proc_after = get_process_ram_bytes()

        gpu_diff = gpu_used_before - gpu_used_after
        ram_diff = ram_used_before - ram_used_after

        # 4. 播放提示音
        if 提示音_播放开关 and 提示音_文件:
            t = threading.Thread(target=self.play_audio_thread, args=(提示音_文件, 提示音_音量))
            t.daemon = True
            t.start()

        # 5. 生成报告 (v7.0 —— 加了 VBAR 归因和「数字为什么不动」的说明)
        def format_diff(val):
            if val > 0.001: return f"📉 {abs(val):.2f} G"
            elif val < -0.001: return f"📈 {abs(val):.2f} G"
            else: return f"➖ 无变化"

        sep = "─" * 16
        GB = 1024 ** 3
        # torch 池 vs aimdo 池：用来解释「为什么物理读数没降」
        torch_pool = after["torch_reserved"] / GB
        aimdo_pool = after["aimdo"] / GB
        torch_delta = (after["torch_reserved"] - before["torch_reserved"]) / GB
        aimdo_delta = (after["aimdo"] - before["aimdo"]) / GB

        detail = []
        if torch_pool > 0.01:
            detail.append(f"   torch 池: {before['torch_reserved']/GB:.2f} -> {torch_pool:.2f} GB ({format_diff(torch_delta)})")
        if aimdo_pool > 0.01 or aimdo_delta != 0:
            detail.append(f"   aimdo池: {before['aimdo']/GB:.2f} -> {aimdo_pool:.2f} GB ({format_diff(aimdo_delta)})")
        if vbar_unloaded:
            detail.append(f"   VBAR 卸载: {vbar_unloaded} 个 patcher, 驻留页释放约 {vbar_freed/GB:.2f} GB")
        detail_text = "\n".join(detail)

        # 数字不动时的归因说明——避免再被误读成「净化没生效」
        if abs(gpu_diff) <= 0.001:
            if aimdo_pool > 0.5 or torch_pool > 0.5:
                reason = (
                    f"\n   ℹ️ 物理读数没动，但占用确实换了地方：\n"
                    f"      torch 池 {torch_pool:.2f} GB / aimdo 池 {aimdo_pool:.2f} GB。\n"
                    f"      这两块都由驱动持有，不随 empty_cache 归还——属正常现象。"
                )
            else:
                reason = "\n   ℹ️ 池内已清空；物理读数不动通常是显存被驱动/显示进程缓存持有，稍后自会回落。"
        else:
            reason = ""

        # 内存段：整机口径混着所有进程，只有「ComfyUI 自己」这一行能证明清理生效
        ram_line = f"🧠 RAM : {ram_used_before:.2f} -> {ram_used_after:.2f} GB\n   {format_diff(ram_diff)}"
        if proc_before is not None and proc_after is not None:
            proc_diff = proc_before - proc_after
            ram_line += (f"\n   ComfyUI 进程: {proc_before:.2f} -> {proc_after:.2f} GB"
                         f" ({format_diff(proc_diff)})")
            if abs(ram_diff) <= 0.001 and abs(proc_diff) > 0.05:
                ram_line += "\n   ℹ️ 整机读数是所有进程合计，上面那行才是本节点的实际战果。"
        if trim_error:
            ram_line += f"\n   ⚠️ 内存压缩失败：{trim_error}"

        log_report = (
            f"✅ 净化完成 | 🕒 {start_time}\n"
            f"{sep}\n"
            f"💾 VRAM: {gpu_used_before:.2f} -> {gpu_used_after:.2f} GB\n"
            f"   {format_diff(gpu_diff)}\n"
            f"{sep}\n"
            f"{ram_line}\n"
            f"{sep}\n"
            f"🧩 分池明细:\n"
            f"{detail_text if detail_text else '   (无)'}"
            f"{reason}"
        )
        if error_msg: log_report += f"\n❌ 错误: {error_msg}"
        if 模型_彻底丢弃:
            if drop_queued:
                log_report += "\n🗑️ 模型释放: 已排入队列，本轮工作流跑完后自动清空（控制台可见结果）"
            else:
                log_report += f"\n⚠️ 模型释放: 未能排队 — {drop_error}"

        print(f"✨ [班长净化 v7.0] 结束 | VRAM: {gpu_used_after:.2f} GB | 释放: {gpu_diff:.2f} GB"
              f" | VBAR卸载: {vbar_unloaded} 个 / 约 {vbar_freed/GB:.2f} GB\n")
        return (信号触发入口, log_report)

# =============================================================================
#  NODE 2: 🛡️⚡ 显存守卫（显存预留 + 快速中断）
# =============================================================================
#  合并了原来的「显存预留卫士」与「全能快速中断」两个节点。
#
#  与旧版的差异：
#    1. 非破坏性——不卸载模型、不清缓存。放在采样器前也不会白重载一次模型，
#       因此可以和「快速中断」合并成同一个节点（中断功能必须在模型加载之后）。
#    2. 预留量不再叠加 physical_used。那个数就是NVML 的 mem.used，
#       内核的 get_free_memory() 已经扣过一次，再塞进 EXTRA_RESERVED_VRAM
#       等于让外部占用被扣两遍（旧版的隐藏 bug）。
#    3. 不替换 minimum_inference_memory 函数。内核本身是
#       minimum_inference_memory() = 0.8GB + extra_reserved_memory()，
#       只改EXTRA_RESERVED_VRAM 就能抬高门槛，且天然可还原。
#    4. 预留容量 = 0 时完整回滚：释放物理占位、还原内核预留量与 aimdo 原生预留量。
#    5. 【本次】不再关闭 Dynamic VRAM。旧版靠
#           aimdo_enabled = False + CoreModelPatcher 指回经典版
#       来让 EXTRA_RESERVED_VRAM 生效 —— 代价是之后载入的**每一个**模型都退回
#       整份全量加载（实测 15.7 GB 文本编码器：驻留 1.3 GB → 全量 15 GB → OOM）。
#       现在改用 aimdo 原生预留 set_simple_vram_headroom()，动态显存保持开启。
#       「禁用动态显存」开关随之下线；旧工作流里带的值会被忽略并提示一次。
#       若 aimdo 本次启动就不可用（--disable-dynamic-vram），自动退回内核预留量。
#
_guard_state = {
    "extra_reserved": None,   # comfy.model_management.EXTRA_RESERVED_VRAM 原值
    "aimdo": None,            # comfy.memory_management.aimdo_enabled 原值（仅为旧版遗留状态兜底）
    "core_patcher": None,     # comfy.model_patcher.CoreModelPatcher 原类（同上）
    "aimdo_headroom": None,   # aimdo 原生预留量原值（字节）
    "tensor": None,           # 物理占位张量
}


def _guard_release_tensor():
    """释放物理占位张量。仅丢弃引用 + 清缓存，不碰任何其他显存。"""
    if _guard_state["tensor"] is None:
        return
    _guard_state["tensor"] = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _guard_restore_kernel():
    """把内核变量还原到本节点介入之前的状态。"""
    extra = _guard_state["extra_reserved"]
    if extra is not None:
        # 必须整体 import comfy.model_management：只 import 子模块时，
        # Python 会把 comfy 绑成局部名，导致同名全局不可见（UnboundLocalError）。
        import comfy.model_management
        comfy.model_management.EXTRA_RESERVED_VRAM = extra
        _guard_state["extra_reserved"] = None

    # aimdo 原生预留量还原。调小不会立刻把页填回来（要等下次载入），
    # 但水位线本身必须还原，否则下次设预留时会叠加上去。
    if _guard_state["aimdo_headroom"] is not None:
        ok, why = aimdo_set_headroom(_guard_state["aimdo_headroom"])
        if ok:
            _guard_state["aimdo_headroom"] = None
        else:
            print(f"[显存守卫] ⚠️ 还原 aimdo 原生预留量失败：{why}（下次运行会重试）")

    # 旧版遗留：如果本节点曾经关过 DynamicVRAM，这里还原回去。
    # 新版不再走这条路，_guard_state["aimdo"] 通常是 None，等于空操作。
    if _guard_state["aimdo"] is not None:
        try:
            import comfy.memory_management
            import comfy.model_patcher
            comfy.memory_management.aimdo_enabled = _guard_state["aimdo"]
            comfy.model_patcher.CoreModelPatcher = _guard_state["core_patcher"]
        except Exception as e:
            print(f"[显存守卫] ⚠️ 恢复 Dynamic VRAM 失败: {e}")
        _guard_state["aimdo"] = None
        _guard_state["core_patcher"] = None


class BanZhang_VRAM_Guard_Node:
    """
    🛡️⚡ 显存守卫：给指定程序让出显存 + 点停止能秒响应。

    两件事合成一个节点，所以要放在采样器前面：
      · 快速中断要在模型加载完之后才挂得上 patch
      · 显存预留改成非破坏性（不卸载模型），放在这里不会白重载一次模型
    """

    _patch_notified = False
    _legacy_notified = False

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "预留容量_GB": ("FLOAT", {
                    "default": 2.0,
                    "min": 0.0,
                    "max": 128.0,
                    "step": 0.1,
                    "tooltip": "你想给显卡里留出多少空闲显存，单位 GB。\n"
                               "例如设2 = 无论 ComfyUI 还是别的程序，都只能用到「总量 − 2GB」。\n"
                               "\n"
                               "【怎么填】\n"
                               "· 只是想让浏览器/游戏别被挤爆 → 1.5 ~ 2\n"
                               "· 边生成视频边留显存给别的东西 → 3 ~ 4\n"
                               "· 显卡本来就不大（8G/12G）→ 1 足够，别设太大\n"
                               "\n"
                               "【上限】超过总显存 90% 时会自动封顶。\n"
                               "\n"
                               "【设 0 = 完全不启用预留功能】\n"
                               "释放全部物理占位，并把内核设置还原成启动时的样子。\n"
                               "这时本节点只剩「快速中断」一个作用。\n"
                               "\n"
                               "【实现方式（全自动，无需选择）】\n"
                               "启用动态显存时走 aimdo 原生预留（set_simple_vram_headroom），\n"
                               "动态显存不可用时自动退回内核预留量（EXTRA_RESERVED_VRAM）。\n"
                               "无论哪种，都不会再关掉动态显存。"
                }),
            },
            "optional": {
                "🔣 任意输入": (任意接口, {
                    "tooltip": "接模型（MODEL）。上游模型加载完后再接进来，\n"
                               "这样「点停止秒响应」才生效。\n\n"
                               "不接也能用：此时只执行显存预留，中断功能不启用。\n"
                               "接非模型数据（图片/潜空间等）会原样透传。"
                }),
            },
        }

    RETURN_TYPES = (任意接口,)
    RETURN_NAMES = ("⚡ 智能输出",)
    FUNCTION = "execute_guard"
    CATEGORY = "👨‍🏫班长/显存与系统"
    OUTPUT_NODE = True

    DESCRIPTION = """放在采样器（KSampler）之前：硬保住「预留容量_GB」的空闲显存给其他程序，同时加快点停止的响应。设 0 = 不启用预留，只保留中断。详细取值悬停各参数查看。预留方式全自动：动态显存可用时走 aimdo 原生预留，否则退回内核预留量；不会再关掉动态显存。"""
    def _apply_intrinsic_guard(self, input_data):
        """中断守护：入口检测 + 标准 UNet 的 block 级 patch。

        返回 (透传后的对象, 中断能力描述)。描述必须如实反映实际挂上了什么，
        否则日志会报告「已挂上」但实际没生效。
        """
        try:
            _check_interrupt()
        except Exception:
            _safe_force_clear_vram()
            raise

        is_model = (
            input_data is not None
            and hasattr(input_data, "get_model_object")
            and hasattr(input_data, "add_object_patch")
        )
        if not is_model:
            hint = ("未接模型，本次不启用" if input_data is None
                    else "输入不是模型，本次不启用")
            return input_data, hint

        patched_model = input_data.clone()

        def _block_input_patch(h, _transformer_options):
            try:
                _check_interrupt()
            except Exception:
                _safe_force_clear_vram()
                raise
            return h

        block_patched = False
        try:
            # 只有标准 UNet（openaimodel）会真正读取 input_block_patch，
            # 用 input_blocks 属性精确判定，避免给 FLUX/H3 挂永不触发的 patch。
            if hasattr(patched_model.model, "input_blocks"):
                patched_model.set_model_input_block_patch(_block_input_patch)
                block_patched = True
        except Exception:
            block_patched = False

        if not BanZhang_VRAM_Guard_Node._patch_notified:
            if block_patched:
                print("👨‍🏫 显存守卫：防卡死守护已激活 (block级patch)，点停止将快速响应。")
            else:
                print("👨‍🏫 显存守卫：当前模型不支持 block 级中断注入（DiT 架构），"
                      "已回退为入口 + ComfyUI per-op 检测。若点停止无反应，"
                      "多为单个超长 kernel 无法中途打断。")
            BanZhang_VRAM_Guard_Node._patch_notified = True

        return patched_model, ("已挂上块级检测" if block_patched
                              else "该模型无块级结构，已回退为 ComfyUI 内置检测（响应略慢）")

    def execute_guard(self, 预留容量_GB, **kwargs):
        input_data = kwargs.get("🔣 任意输入", None)
        sep = "─" * 16

        # 旧工作流里可能还带着已下线的「禁用动态显存」开关 —— 忽略它，
        # 只提示一次，免得用户以为这个设置还在起作用。
        if "禁用动态显存" in kwargs and not BanZhang_VRAM_Guard_Node._legacy_notified:
            BanZhang_VRAM_Guard_Node._legacy_notified = True
            print("[显存守卫] ℹ️ 「禁用动态显存」开关已下线：现在改用 aimdo 原生预留，"
                  "不会再关掉动态显存。旧工作流里的这个值已被忽略。")

        # ---- 0a. 自动纠正：把被提前关掉的动态显存开回来 ----
        # 放在最前面。关掉动态显存会让之后载入的每个模型都退回整份全量加载
        # （15 GB 的文本编码器会整份占满显存），而这一步只是恢复启动时的状态，
        # 不改变用户任何显式设置 —— 用 --disable-dynamic-vram 启动时不会被碰。
        fixed, fix_note = ensure_aimdo_healthy()
        if fixed:
            print("[显存守卫] ⚙️ " + fix_note)

        # ---- 0b. 同步显存读数 ----
        if torch.cuda.is_available():
            try:
                torch.cuda.synchronize()
            except Exception:
                pass
        total_vram, physical_used, source = get_robust_gpu_info()
        if total_vram == 0:
            print("[显存守卫] ❌ 无法获取显卡信息，预留功能未执行")
            return (self._apply_intrinsic_guard(input_data)[0],)

        # ---- 1. 先释放旧占位，避免叠加 ----
        _guard_release_tensor()

        native_ok, native_why = aimdo_native_ready()

        # ---- 2. 预留容量=0 → 不启用预留，完整回滚 ----
        if 预留容量_GB <= 0:
            _guard_restore_kernel()
            guarded, guard_hint = self._apply_intrinsic_guard(input_data)
            log = (
                f"🛡️⚡ 预留功能：未启用{sep}\n"
                f"📊 显卡总共 {total_vram:.1f} GB ｜ 当前已用 {physical_used:.2f} GB\n"
                f"{sep}\n"
                f"✅ 物理占位已释放，内核预留量与 aimdo 预留量已还原成介入前的值\n"
                f"ℹ️ 本节点现在只负责「快速中断」：{guard_hint}\n"
                f"ℹ️ 想重新启用预留，把「预留容量_GB」改成大于 0 的数值即可。"
            )
            print("\n🛡️⚡ [显存守卫]\n" + log)
            return (guarded,)

        # ---- 3. 写预留量：优先 aimdo 原生，不可用时退回内核预留量 ----
        # 两条都写不冲突：aimdo 管动态分页模型，EXTRA_RESERVED_VRAM 管非动态模型。
        # 关键是**不再关掉动态显存** —— 那会让所有模型退回整份全量加载。
        if _guard_state["extra_reserved"] is None:
            _guard_state["extra_reserved"] = comfy.model_management.EXTRA_RESERVED_VRAM
        if _guard_state["aimdo_headroom"] is None and native_ok:
            # 记下介入前的值，便于 0 时还原（取不到就记 0，还原成 0 也无害）
            cur = aimdo_get_headroom()
            _guard_state["aimdo_headroom"] = 0 if cur is None else cur

        # 封顶 90%：留一成给 CUDA context 和驱动固定开销
        safe_ceiling = total_vram * 0.9
        final_reserved_gb = min(预留容量_GB, safe_ceiling)
        capped = final_reserved_gb < 预留容量_GB
        reserve_bytes = int(final_reserved_gb * 1024 * 1024 * 1024)

        comfy.model_management.EXTRA_RESERVED_VRAM = reserve_bytes

        aimdo_ok = False
        aimdo_note = ""
        if native_ok:
            aimdo_ok, aimdo_err = aimdo_set_headroom(reserve_bytes)
            if not aimdo_ok:
                aimdo_note = f"⚠️ aimdo 原生预留未生效（{aimdo_err}），已由内核预留量接管"
                print("[显存守卫] ⚠️ " + aimdo_note)
        else:
            aimdo_note = f"ℹ️ 动态显存不可用（{native_why}），本次只用内核预留量"

        # ---- 4. 物理占位（失败不报错，内核/aimdo 那层仍然有效） ----
        phys_ok = False
        phys_note = ""
        try:
            _guard_state["tensor"] = torch.empty(
                reserve_bytes, dtype=torch.uint8, device="cuda"
            )
            phys_ok = True
        except RuntimeError as e:
            phys_note = "⚠️ 未能占住物理显存（显卡已满），但内核/aimdo 预留仍然生效"
            print(f"[显存守卫] ⚠️ 物理占位失败（显存已满），预留仍生效: {e}")

        available_for_comfy = total_vram - final_reserved_gb
        guarded, guard_hint = self._apply_intrinsic_guard(input_data)

        if aimdo_ok:
            mech = "aimdo 原生预留（动态显存正常工作，模型仍分页流式加载）"
        elif native_ok:
            mech = "内核预留量（aimdo 原生预留未生效，已自动降级）"
        else:
            mech = f"内核预留量（{native_why}）"

        log = (
            f"🛡️⚡ 预留功能：已生效{sep}\n"
            f"📊 显卡总共 {total_vram:.1f} GB ｜ 当前已用 {physical_used:.2f} GB\n"
            f"{sep}\n"
            f"🎯 预留空闲 {final_reserved_gb:.2f} GB"
            f"{'（你设的值超过总量 90%，已自动封顶）' if capped else ''}\n"
            f"💡 ComfyUI 可用到 {available_for_comfy:.2f} GB 以内\n"
            f"🧱 物理占位："
            f"{'成功，任何程序都抢不走这 ' + f'{final_reserved_gb:.2f} GB' if phys_ok else '未成功'}\n"
            f"⚙️ 预留方式：{mech}\n"
            f"{sep}\n"
            f"ℹ️ 动态显存：{'保持开启（不再需要为预留而关闭）' if native_ok else '本次启动未启用'}"
            f"{('｜' + fix_note) if fixed else ''}\n"
            f"ℹ️ 快速中断：{guard_hint}"
            f"{'\\n   ' + phys_note if phys_note else ''}"
            f"{'\\n   ' + aimdo_note if aimdo_note else ''}"
        )
        print("\n🛡️⚡ [显存守卫]\n" + log)
        return (guarded,)


# =============================================================================
#  NODE MAPPINGS
# =============================================================================
NODE_CLASS_MAPPINGS = {
    "BanZhang_VRAM_Cleaner": BanZhang_VRAM_Cleaner_Node,
    "BanZhang_VRAM_Guard": BanZhang_VRAM_Guard_Node,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "BanZhang_VRAM_Cleaner": "🧹 全能系统净化",
    "BanZhang_VRAM_Guard": "🛡️⚡ 显存守卫",
}