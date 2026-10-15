# -*- coding: utf-8 -*-
"""Topaz Starlight pipeline - pure functions (testable outside ComfyUI).

原 ComfyUI-TopazStarlight/topaz_pipeline.py 并入班长全家桶 (banzhang_topaz_starlight.py 调用)。

Dual-engine support (2026-08-19):
  - neuroserver/      = 1.6.1 engine  -> 星光 2.5 (slp-25), fallback for Astra trio
  - neuroserver171/   = 1.7.1 engine  -> 星光 2.6 (slp-26), Astra family incl. Fast
Engine is chosen per model_id. Each engine uses its own ffmpeg (1.7.1 needs
its bundled build; 1.6.1 works with bin/ or system ffmpeg).
Astra-family models require >= 9 input frames on BOTH engines (short-clip path
is broken in both; verified 2026-08-19).
"""
import os, subprocess, sys, shutil, threading, queue, re
from pathlib import Path

# <ComfyUI>/custom_nodes/ComfyUI-Banzhang-All/banzhang_topaz_pipeline.py
# 与原独立节点目录同级深度: parent.parent.parent 仍是 <ComfyUI> 根
_NODE_DIR  = Path(__file__).resolve().parent
_ENGINE    = Path(__file__).resolve().parent.parent.parent / 'topaz_engine'

_TVMD = _NODE_DIR / 'tvmd'          # 干净授权目录 (按需自动创建, 1KB)

# 模型 -> 引擎目录 / ffmpeg 目录
MODEL_ENGINE = {
    'slp-26':      ('neuroserver171', 'bin171'),   # 星光 (2.5条目=2.6权重) -> 1.7.1 引擎
    'astra':       ('neuroserver171', 'bin171'),   # Astra 家族 -> 1.7.1 引擎
    'astrahq':     ('neuroserver171', 'bin171'),
    'astrasharp':  ('neuroserver171', 'bin171'),
    'astrafast':   ('neuroserver171', 'bin171'),
}
ASTRA_MODELS = ('astra', 'astrahq', 'astrasharp', 'astrafast')
MIN_FRAMES_ASTRA = 9   # Astra 家族最短帧数 (两版引擎均验证: <9 帧走坏路径)

NS_ENC = '-c:v h264_nvenc -profile:v high -pix_fmt yuv420p -g 30 -preset p7 -tune hq -rc constqp -qp 18 -rc-lookahead 20 -spatial_aq 1 -aq-strength 15 -b:v 0 -bf 0'


# --------------------------------------------------------------------------
# 进度解析 / 可中断泵 (2026-09-08 并入班长全家桶时新增)
# --------------------------------------------------------------------------

class TopazUserInterrupt(Exception):
    """用户在 ComfyUI 里点了取消: 引擎进程已被终止, 由上层转成 InterruptProcessingException."""


# 引擎 stdout 进度行解析 (2026-09-08 真机实测确认的官方格式 + 兜底约定):
#   实测主格式 (每条更新发两遍, 内容一致):
#     {"timestamp": "...", "level": "INFO", "message": {"status": "RUNNING", "frame": 2, "progress": 22, "message": ""}}
#     INFO:interface:{"status": "RUNNING", "frame": 2, "progress": 22, "message": ""}
#     其中 progress = 全局百分比 0~100 整数, frame = 当前帧号 (0 基); 这两种行都 > 60 字符
#   兜底约定: "stage_progress": 0.42 (topserving 0~1) / 42% / frame 12 of 240 / 12/240
_PROGRESS_PATTERNS = (
    re.compile(r'"stage_progress"\s*:\s*([0-9]*\.?[0-9]+)'),
    re.compile(r'"progress"\s*:\s*(\d{1,3})\b'),
    re.compile(r'"frame"\s*:\s*(\d{1,6})\b'),
    re.compile(r'(\d{1,3}(?:\.\d+)?)\s*%'),
    re.compile(r'\b(\d{1,6})\s*(?:/|of\b|out of\b)\s*(\d{1,6})\b', re.I),
)


def _parse_progress(line, total):
    """从引擎输出行解析进度, 返回已完成帧数 (int) 或 None.

    带 JSON 键名的模式 (前三) 不限行长——键名本身就是强上下文, 且实测引擎行 >60 字符;
    无上下文模式 (百分比/分数) 仅在 <=60 字符的短行解析, 防长日志误报.
    """
    if total <= 0:
        return None
    m = _PROGRESS_PATTERNS[0].search(line)
    if m:
        v = float(m.group(1))
        if 0.0 <= v <= 1.0:
            return int(v * total)
    m = _PROGRESS_PATTERNS[1].search(line)
    if m:
        v = int(m.group(1))
        if 0 <= v <= 100:
            return int(v / 100.0 * total)
    m = _PROGRESS_PATTERNS[2].search(line)
    if m:
        return min(int(m.group(1)) + 1, total)   # 引擎帧号 0 基
    if len(line) <= 60:
        m = _PROGRESS_PATTERNS[3].search(line)
        if m:
            v = float(m.group(1))
            if 0.0 < v <= 100.0:
                return int(v / 100.0 * total)
        for m in _PROGRESS_PATTERNS[4].finditer(line):
            num, den = int(m.group(1)), int(m.group(2))
            if 0 < num <= den and den <= total * 5:
                return int(num / den * total)
    return None


# 引擎分块参数 (2026-09-09 生产日志实测: 362 帧 -> 4 块 121/121/121/62, 末块起点 300 = 3x100)
CHUNK_SIZE = 121      # 分块窗口帧数
CHUNK_STRIDE = 100    # 分块步进 (每完成一块推进的帧数)
CHUNK_PHASE_MARK = 'Phase 1: VAE Encode'      # 每块开始各打印一次, 用作分块计数
CHUNK_DONE_RE = re.compile(r'Saved (\d+) frames to')

# 引擎 stdout 中文化: (模式, 中文模板) —— None 表示丢弃该行
_TRANSLATE = (
    (re.compile(r'Module import completed in ([\d.]+)s'), '引擎模块加载完成（{0}s）'),
    (re.compile(r'Model build completed in ([\d.]+)s'), '模型构建完成（{0}s）'),
    (re.compile(r'Creating new runner: DiT=(\S+), VAE=(\S+)'), '创建推理器：DiT={0} / VAE={1}'),
    (re.compile(r'AstraPrecisionTwentyFive initialized successfully'), '星光模型初始化成功'),
    (re.compile(r'Loading VAE'), '加载 VAE'),
    (re.compile(r'LazyVideoReader: (\d+) frames, (\d+)x(\d+), fps=([\d.]+)'), '读取视频：{0} 帧 {1}x{2} @{3}fps'),
    (re.compile(r'Input: (\d+)x(\d+), target box: (\d+)x(\d+) → final resolution: (\d+)x(\d+)'),
     '输入 {0}x{1} → 输出 {4}x{5}'),
    (re.compile(r'Frame range: skip=\d+, load_cap=(\d+)'), '帧范围：读取上限 {0}'),
    (re.compile(r'Phase 1: VAE Encode'), '━━ 阶段1：VAE 编码 ━━'),
    (re.compile(r'Phase 2: Upscale'), '━━ 阶段2：扩散放大 ━━'),
    (re.compile(r'Phase 3: VAE Decode'), '━━ 阶段3：VAE 解码 ━━'),
    (re.compile(r'Input frames: torch\.Size\(\[(\d+)'), '本块输入 {0} 帧'),
    (re.compile(r'Decoding latent shape: torch\.Size\(\[(\d+), (\d+), (\d+), (\d+)\]\)'),
     '本块潜空间：{0}x{1}x{2}x{3}'),
    (re.compile(r'Decoded shape \(CTHW\): torch\.Size\(\[(\d+), (\d+), (\d+), (\d+)\]\)'),
     '本块解码完成：{1} 帧 {2}x{3}'),
    (re.compile(r'EulerSampler:\s*(\d+)%'), '扩散采样 {0}%'),
    (re.compile(r'Chunk (\d+): got (\d+) frames instead of (\d+)'), '分块{0}：末块 {1} 帧（不足整块 {2}）'),
    (re.compile(r'\[Profile\] All (\d+) chunks total: ([\d.]+)s \(encode=([\d.]+)s, dit=([\d.]+)s, '
                r'decode=([\d.]+)s, post=([\d.]+)s\) \| Peak VRAM: encode=([\d.]+)GB, unet=([\d.]+)GB, '
                r'decode=([\d.]+)GB'),
     '性能统计：{0} 块共 {1}s（编码{2}/扩散{3}/解码{4}/后期{5}）｜显存峰值：编码{6}G 扩散{7}G 解码{8}G'),
    (re.compile(r'No audio stream .*skipping audio mux'), '输入无音轨，跳过音频合成'),
    (re.compile(r'Saved (\d+) frames to'), '已保存 {0} 帧'),
    (re.compile(r'total processing time: ([\d.]+)s'), '引擎总耗时 {0}s'),
    (re.compile(r'single_shot_process status: TopServingErrorCode\.OK'), '任务状态：成功'),
    (re.compile(r'single_shot_process status: TopServingErrorCode\.(\w+)'), '任务状态：{0}'),
    (re.compile(r'Floating license checked out successfully'), '浮动授权签出成功'),
    (re.compile(r'has valid license: True'), '授权校验通过'),
    (re.compile(r'Auth file does not exist: .*auth\.tpz'), '未找到授权文件 auth.tpz：输出将带水印'),
    (re.compile(r'Watermark required'), None),   # 与上一条重复
    (re.compile(r'Initial CUDA memory: ([\d.]+)GB free / ([\d.]+)GB total'), '显存：空闲 {0}G / 总共 {1}G'),
    (re.compile(r'FFMPEGVideoWriter: using user-supplied encoding options'), '输出编码：h264_nvenc（引擎内置参数）'),
    (re.compile(r'\[VariablePool\] (.*)'), '显存池：{0}'),
)

# 噪音行: 环境信息与参数回显 (多处重复打印), 直接丢弃
_SUPPRESS = (
    re.compile(r'^sys\.path:'),
    re.compile(r'^TOPAZ_RUNNER_DIR:'),
    re.compile(r'^TOPAZ_MODEL_STORE:'),
    re.compile(r'^USE_DML:'),
    re.compile(r'^Windows Version:'),
    re.compile(r'^platform:'),
    re.compile(r'^Machine ID:'),
    re.compile(r'^INFO:local_model_service:(input_uri|output_uri|model parameters|run once|'
               r'in working directory|input_path|output_path|uploaded to):'),
    re.compile(r'^INFO:topserving\S*:(Detected platform|Available GPUs|GPU name for|'
               r'GPU manufacture|torch version|ProgressActor initialized)'),
    re.compile(r'^INFO:.*Getting model for request:'),
    re.compile(r'^INFO:models\S*:ffmpeg_encoding:'),
)


def _translate_line(line):
    """引擎 stdout 行 -> 中文行; 返回 None 表示丢弃; 未知行原样返回 (保住报错排查能力)."""
    for pat, rep in _TRANSLATE:
        m = pat.search(line)
        if m:
            return None if rep is None else rep.format(*m.groups())
    for pat in _SUPPRESS:
        if pat.search(line):
            return None
    return line


def _terminate_tree(proc):
    """杀掉进程树 (引擎内部会派生 ffmpeg/ffprobe; 仅 terminate 主进程会留孤儿继续占显存)."""
    if proc.poll() is None:
        if sys.platform == 'win32':
            try:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass


def _pump(proc, total, log=print, progress_cb=None, interrupt_check=None):
    """泵出引擎 stdout: 解析进度 -> progress_cb; 轮询中断 -> 终止引擎并抛 TopazUserInterrupt.

    按字符读取以同时兼容 \\n 与 \\r 两种进度刷新方式; 已识别的进度行不写日志避免刷屏.
    日志经 _translate_line 中文化 (噪音行丢弃, 未知行原样转发);
    生产运行实测无 JSON 进度行, 故用「阶段1」出现次数按分块推算已完成帧.
    interrupt_check(): 返回 True 表示用户请求取消 (非抛出契约, 保持本模块不依赖 comfy).
    """
    def _check_interrupt():
        if interrupt_check is not None and interrupt_check():
            _terminate_tree(proc)
            raise TopazUserInterrupt('用户中断: 星光引擎进程树已终止')

    q = queue.Queue()

    def _reader():
        try:
            buf = ''
            while True:
                ch = proc.stdout.read(1)
                if not ch:
                    break
                if ch == '\n' or ch == '\r':
                    if buf:
                        q.put(buf)
                    buf = ''
                else:
                    buf += ch
            if buf:
                q.put(buf)
        finally:
            q.put(None)   # EOF 哨兵

    threading.Thread(target=_reader, daemon=True).start()

    total_chunks = 1 if total <= CHUNK_SIZE else (total - CHUNK_SIZE + CHUNK_STRIDE - 1) // CHUNK_STRIDE + 1
    done_max = 0
    chunks_started = 0

    def _feed(done):
        """单调推进进度: 取历史最大值, 防止 JSON 进度与分块推算两路信号互相回退."""
        nonlocal done_max
        done = min(max(0, int(done)), total)
        if done > done_max:
            done_max = done
            progress_cb(done)

    n_lines = 0
    while True:
        try:
            line = q.get(timeout=0.5)
        except queue.Empty:
            _check_interrupt()   # 引擎静默阶段 (如加载模型) 也能响应取消
            continue
        if line is None:
            break
        line = line.strip()
        if not line:
            continue
        n_lines += 1
        if n_lines % 25 == 0:
            _check_interrupt()   # 行流密集时也保证轮询
        if progress_cb is not None:
            done = _parse_progress(line, total)
            if done is not None:
                _feed(done)
                continue   # 已识别的进度行不落日志 (无论是否刷新了最大值)
            # 分块兜底: 生产运行实测不输出 JSON 进度行, 改用「阶段1」出现次数推算已完成帧
            if CHUNK_PHASE_MARK in line:
                chunks_started += 1
                _feed((chunks_started - 1) * CHUNK_STRIDE)
            elif CHUNK_DONE_RE.search(line):
                _feed(total)
        zh = _translate_line(line)   # None = 噪音行丢弃
        if zh is None:
            continue
        log('  [Topaz] ' + zh)

def _ffmpeg_for(model_id):
    """返回 (ffmpeg.exe, ffprobe.exe) 绝对路径, 按模型选引擎配套 ffmpeg."""
    _, ff_dir = MODEL_ENGINE.get(model_id, ('neuroserver', 'bin'))
    cand = [
        _ENGINE / ff_dir / 'ffmpeg.exe',
        _ENGINE / 'bin' / 'ffmpeg.exe',
        Path(r'D:\APP\ffmpeg\bin\ffmpeg.exe'),
    ]
    for c in cand:
        if c.is_file():
            return str(c), str(c.with_name('ffprobe.exe'))
    w = shutil.which('ffmpeg')
    if w:
        return w, w.replace('ffmpeg.exe', 'ffprobe.exe')
    raise RuntimeError('ffmpeg not found - install ffmpeg or put it on PATH')


# --------------------------------------------------------------------------
# engine detection
# --------------------------------------------------------------------------

def _find_bundled(model_id='slp-26'):
    eng_dir, _ = MODEL_ENGINE.get(model_id, ('neuroserver171', 'bin171'))
    ns = _ENGINE / eng_dir / 'neuroserver.exe'
    if ns.is_file():
        tvmd = _ENGINE / 'tvmd'
        return {
            'name': 'bundled(%s)' % eng_dir,
            'ns': str(ns),
            'model_store': str(_ENGINE / 'models'),
            'tvai_dir': str(tvmd) if (tvmd / 'VR.lic').is_file() else str(_TVMD),
            'lic': str(_TVMD / 'VR.lic') if not (tvmd / 'VR.lic').is_file() else str(tvmd / 'VR.lic'),
        }
    return None


def _find_installed():
    """Detect a user-installed Topaz Video (registry InstallDir + common paths)."""
    candidates = []
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, r'Software\Topaz Labs LLC\Topaz Video'),
                          (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Topaz Labs LLC\Topaz Video')):
            try:
                with winreg.OpenKey(hive, sub) as k:
                    val, _ = winreg.QueryValueEx(k, 'InstallDir')
                    if val:
                        candidates.append(Path(val))
            except OSError:
                pass
    except Exception:
        pass
    for base in (Path(os.environ.get('ProgramFiles', 'C:\\Program Files')),
                 Path(os.environ.get('ProgramFiles(x86)', 'C:\\Program Files (x86)')),
                 Path('C:/Program Files'), Path('D:/')):
        candidates.append(base / 'Topaz Labs LLC' / 'Topaz Video')

    for cand in candidates:
        ns = cand / 'neuroserver' / 'neuroserver.exe'
        if not ns.is_file():
            continue
        model_store = None
        for m in (cand / 'models' / 'models', cand / 'models',
                  Path('C:/ProgramData/Topaz Labs LLC/Topaz Video/models')):
            if (m / 'slp25').is_dir():
                model_store = str(m)
                break
        if model_store is None:
            continue
        return {
            'name': 'installed@%s' % cand,
            'ns': str(ns),
            'model_store': model_store,
            'tvai_dir': None,
            'lic': None,
        }
    return None


def _resolve_engine(model_id='slp-26'):
    mode = os.environ.get('TOPAZ_ENGINE_MODE', '').lower()
    if mode == 'detected':
        e = _find_installed() or _find_bundled(model_id)
    else:  # bundled first
        e = _find_bundled(model_id) or _find_installed()
    if e is None:
        raise RuntimeError(
            '未找到星光引擎: 请把 topaz_engine 放在 ComfyUI 目录下, '
            '或在本机安装 Topaz Video (节点会自动识别)')
    return e


def _ensure_tvmd():
    if not (_TVMD / 'VR.lic').is_file():
        _TVMD.mkdir(parents=True, exist_ok=True)
        for cand in (_ENGINE / 'tvmd',):
            if (cand / 'VR.lic').is_file():
                shutil.copy(cand / 'VR.lic', _TVMD / 'VR.lic')
                shutil.copy(cand / 'VR.lic', _TVMD / 'Topaz.lic')
                break


def engine_info(model_id='slp-26'):
    e = _resolve_engine(model_id)
    return e['name'], e['ns'], e['model_store']


def _node_ffmpeg():
    """节点自用 ffmpeg (写临时视频/读回输出): 固定用 bin/ 全功能版 (软解h264).
    bin171 是 1.7.1 引擎特供 (禁软解, 带 tvai_up), 不能用于节点读写."""
    cand = [_ENGINE / 'bin' / 'ffmpeg.exe', _ENGINE / 'bin171' / 'ffmpeg.exe',
            Path(r'D:\APP\ffmpeg\bin\ffmpeg.exe')]
    for c in cand:
        if c.is_file():
            return str(c), str(c.with_name('ffprobe.exe'))
    w = shutil.which('ffmpeg')
    if w:
        return w, w.replace('ffmpeg.exe', 'ffprobe.exe')
    raise RuntimeError('ffmpeg not found - install ffmpeg or put it on PATH')


def write_frames_to_video(frames_uint8, fps, path, qp=14, interrupt_check=None):
    """frames_uint8: (B,H,W,3) uint8 RGB -> h264 mp4 (nvenc, high quality).

    分块喂 stdin (避免 frames_uint8.tobytes() 全量拷贝); 每块轮询中断.
    """
    ffmpeg, _ = _node_ffmpeg()
    b, h, w, c = frames_uint8.shape
    cmd = [ffmpeg, '-y', '-loglevel', 'error',
           '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}', '-r', str(fps), '-i', '-',
           '-c:v', 'h264_nvenc', '-preset', 'p7', '-tune', 'hq',
           '-rc', 'constqp', '-qp', str(qp), '-pix_fmt', 'yuv420p', path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    frame_size = h * w * c
    total = b * frame_size
    step = 1 << 20   # 1MB / 块
    try:
        mv = memoryview(frames_uint8).cast('B')   # 零拷贝字节视图 (调用方需保证数组连续)
        off = 0
        while off < total:
            if interrupt_check is not None and interrupt_check():
                raise TopazUserInterrupt('用户中断: 输入编码阶段已终止')
            nxt = min(off + step, total)
            try:
                p.stdin.write(mv[off:nxt])
            except (BrokenPipeError, OSError):
                break
            off = nxt
    except BaseException:
        _terminate_tree(p)
        raise
    try:
        p.stdin.close()
    except Exception:
        pass
    p.wait()
    if p.returncode != 0:
        err = p.stderr.read() if p.stderr else b''
        raise RuntimeError(f'ffmpeg encode failed: {err.decode(errors="replace")[:300]}')


def run_starlight(in_path, out_path, scale, frames, w, h, strength=1.0, model_id='slp-26', log=print,
                  max_gpu_mem=16, progress_cb=None, interrupt_check=None):
    """Run the Topaz neuroserver on a video file (engine chosen per model_id).

    progress_cb(done_frames): 引擎进度回调 (解析自 stdout, 已去重保单调), 可为 None.
    interrupt_check(): 返回 True 则终止引擎并抛 TopazUserInterrupt, 可为 None.
    """
    if model_id in ASTRA_MODELS and frames < MIN_FRAMES_ASTRA:
        raise RuntimeError(
            f'Astra 系列模型需要至少 {MIN_FRAMES_ASTRA} 帧输入 (当前 {frames} 帧), '
            '两版引擎的短视频路径均存在缺陷。请用更长的视频 (或提高帧率)。')

    eng = _resolve_engine(model_id)
    ffmpeg, ffprobe = _ffmpeg_for(model_id)
    env = os.environ.copy()
    # 神经服务器内部也会调 ffmpeg/ffprobe，子进程 PATH 必须能找到 (1.7.1 必须自带版)
    env['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + env.get('PATH', '')
    env['TOPAZ_MODEL_STORE'] = eng['model_store']
    if eng['tvai_dir']:
        env['TVAI_MODEL_DIR'] = eng['tvai_dir']
        env['TOPAZLABS_LICENSE'] = eng['lic']
    else:
        env.pop('TVAI_MODEL_DIR', None)
        env.pop('TOPAZLABS_LICENSE', None)
    if model_id == 'slp-26':
        # 1.7.1 GUI 原版参数: 星光带 softness(默认1), Astra 家族不带
        filters = '[{"model": "%s", "enhancement_strength": %s, "softness": 1}]' % (model_id, strength)
    else:
        filters = '[{"model": "%s", "enhancement_strength": %s}]' % (model_id, strength)
    log('  [Topaz] 模型: %s' % model_id)
    # 输出尺寸取整并对齐到偶数（h264/yuv420p 要求偶数宽高）
    ow = int(round(w * scale)); ow += ow % 2
    oh = int(round(h * scale)); oh += oh % 2
    cmd = [eng['ns'], '--once',
           '--input-path', in_path,
           '--output-path', out_path,
           '--start-frame-idx', '0',
           '--end-frame-idx', str(frames),
           '--max-gpu-mem', str(max_gpu_mem),
           '--filters', filters,
           '--output-width', str(ow),
           '--output-height', str(oh),
           '--upscale-factor', str(scale),
           '--ffmpeg-encoding', NS_ENC]
    log('  [Topaz] 引擎: %s' % eng['name'])
    log('  [Topaz] 模型目录: %s' % eng['model_store'])
    log('  [Topaz] 开始处理 ...')
    proc = subprocess.Popen(cmd, env=env, cwd=str(Path(eng['ns']).parent),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace')
    _pump(proc, frames, log=log, progress_cb=progress_cb, interrupt_check=interrupt_check)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f'neuroserver failed (exit {proc.returncode} / '
                           f'0x{proc.returncode & 0xFFFFFFFF:08X})')
    if not os.path.isfile(out_path):
        raise RuntimeError('neuroserver produced no output file')


def read_video_to_frames(path, interrupt_check=None):
    """mp4 -> (B,H,W,3) uint8 RGB numpy array.

    流式解码到预分配数组 (避免 capture_output 全量缓存导致大输出 OOM); 每块轮询中断.
    """
    import numpy as np
    ffmpeg, ffprobe = _node_ffmpeg()
    probe = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height,nb_frames',
                            '-of', 'csv=p=0', path], capture_output=True, text=True)
    parts = probe.stdout.strip().split(',')
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise RuntimeError(f'ffprobe failed: {probe.stderr.strip()[:300]}')
    w, h = int(parts[0]), int(parts[1])
    frame_size = w * h * 3
    n_probed = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    proc = subprocess.Popen([ffmpeg, '-loglevel', 'error', '-i', path,
                             '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    step = 1 << 20   # 1MB / 块
    try:
        if n_probed > 0:
            # 已知帧数: 直接写入预分配数组, 峰值内存 = 输出本身 (无中间缓存)
            out = np.empty((n_probed, h, w, 3), dtype=np.uint8)
            buf = memoryview(out).cast('B')
            remaining = n_probed * frame_size
            off = 0
            while remaining > 0:
                if interrupt_check is not None and interrupt_check():
                    raise TopazUserInterrupt('用户中断: 输出解码阶段已终止')
                chunk = proc.stdout.read(min(remaining, step))
                if not chunk:
                    break
                buf[off:off + len(chunk)] = chunk
                off += len(chunk)
                remaining -= len(chunk)
            if off < n_probed * frame_size:   # 实际帧少于探测值, 裁掉尾部
                out = out[:off // frame_size]
            return out
        # 未知帧数: 流式收集到块列表再拼接 (罕见路径, 仅当 mp4 无 nb_frames 索引)
        chunks = []
        total = 0
        while True:
            if interrupt_check is not None and interrupt_check():
                raise TopazUserInterrupt('用户中断: 输出解码阶段已终止')
            chunk = proc.stdout.read(step)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        n = total // frame_size
        out = np.empty((n, h, w, 3), dtype=np.uint8)
        if n:
            buf = memoryview(out).cast('B')
            off = 0
            for chunk in chunks:
                buf[off:off + len(chunk)] = chunk
                off += len(chunk)
        return out
    except BaseException:
        _terminate_tree(proc)
        raise
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        proc.wait()
