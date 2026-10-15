import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// 判断节点当前是否在画布可视区内（同步只覆盖视口内的节点，视口外不解码）。
// 换算必须用官方 DragAndScale 的口径 convertOffsetToCanvas：
// screen = (graph + offset) * scale —— 少乘一次 scale 就会把节点算到屏幕外，
// 画布一缩放同步就等于全空。
function isNodeVisible(node) {
    const canvas = app.canvas;
    if (!canvas?.ds) return true;
    const { scale, offset } = canvas.ds;
    const x = (node.pos[0] + offset[0]) * scale;
    const y = (node.pos[1] + offset[1]) * scale;
    const w = node.size[0] * scale;
    const h = node.size[1] * scale;
    const cw = canvas.canvas?.clientWidth || window.innerWidth;
    const ch = canvas.canvas?.clientHeight || window.innerHeight;
    return x + w > 0 && y + h > 0 && x < cw && y < ch;
}

// ═══════════════════════════════════════════════════════════
// 视频同步控制器
// 只管当前画布视口内的「视频合并」节点预览：视口外一律不参与、不解码。
//   · 帧号是唯一真相：拖动 / 逐帧把帧号换算成时间 seek，精确到帧
//   · 播放交给 video 原生连续解码，起播前统一对齐一次，帧号轴按片段帧率换算
//   · 统一控制条（帧滑块 / 播放暂停 / 逐帧 ±1 / 退出）
//   · 帧号上限取各片段最短者，更长的停在它自己的末帧
//   · 持久开关（菜单项勾选状态持续生效）
//
// 刻意不做「播放时钟追赶」：解码器时钟在浏览器内部，从外面只能求它快一点，
// 残差消不掉，改 rate 还会触发解码速率切换。同步靠对齐 + 寻址，不靠追赶。
// ═══════════════════════════════════════════════════════════
const VideoSyncController = {
    enabled: false,
    videos: [],      // 当前视口内、参与同步的片段
    frame: 0,        // 当前帧号：唯一的真相
    playing: false,  // 播放走 video 原生解码，seek 只发生在起播/暂停/逐帧/拖动
    timer: null,     // 常驻低频监视（视口进出、播放末尾、控制条）
    bar: null,

    // 遍历画布上所有「视频合并」节点的预览视频（含视口外的）
    eachVideo(fn) {
        app.graph._nodes.forEach(n => {
            if (n.type !== "banzhangVideoCombine") return;
            const video = n.widgets?.find(w => w.name === "painter_preview")?.element?.querySelector("video");
            if (video) fn(video, n);
        });
    },

    // 只采集当前视口内的片段：视口外不参与、不解码，滚动进出由 watch() 实时调整
    scan() {
        const list = [];
        this.eachVideo((video, n) => {
            if (!isNodeVisible(n)) return;
            const fps = n._painter_fps || 24;
            // 后端没给帧数（旧工作流 / 编码失败残留）时用时长反推，至少有得对齐
            const frames = n._painter_frames || (video.duration ? Math.round(video.duration * fps) : 0);
            list.push({ node: n, video, fps, frames });
        });
        this.videos = list;
        return list;
    },

    // 帧号轴基准：同一条工作流产出的片段帧率一致，取第一个即可。
    // 万一混了不同帧率，各片段仍按自身 fps 换算时间，只是播放速率被拉平到这条轴上。
    get fps() {
        return this.videos[0]?.fps || 24;
    },

    // 可同步的帧号上限：各片段长度不同，取最短的那个，长的就停在它自己的末帧
    get maxFrame() {
        const ns = this.videos.map(v => v.frames).filter(n => n > 0);
        return ns.length ? Math.max(0, Math.min(...ns) - 1) : 0;
    },

    // 定位 = 帧号广播：按各自 fps 换算成时间 seek。逐帧 / 拖动靠它，精确到帧。
    setFrame(k, force) {
        const max = this.maxFrame;
        this.frame = Math.max(0, Math.min(Math.round(k), max));
        this.videos.forEach(v => this._seek(v, force));
        this.updateBar();
    },

    _seek(v, force) {
        if (v.video.readyState < 1) return;
        const t = this.frame / v.fps;
        if (force || Math.abs(v.video.currentTime - t) > 0.5 / v.fps) v.video.currentTime = t;
    },

    enable() {
        // 同步是个开关不是一次性动作：集合允许为空（比如全在视口外），
        // 滚进来后 watch 会自动补上 —— 不该在这里静默失败，点了菜单没反应最坑
        this.enabled = true;
        this.playing = false;
        this.scan();
        // 同步期间所有片段（含视口外）都停下自播：视口外的连解码都不该有
        this.eachVideo(v => { v.pause(); v.playbackRate = v._bzRate || 1; });
        this.showBar();
        this.timer = setInterval(() => this.watch(), 200);
    },

    disable() {
        this.enabled = false;
        this.playing = false;
        if (this.timer) { clearInterval(this.timer); this.timer = null; }
        this.eachVideo(v => { v.pause(); v.playbackRate = v._bzRate || 1; v.play().catch(() => {}); });
        this.videos = [];
        this.hideBar();
    },

    // 播放交给 video 原生连续解码 —— 「一卡一卡」的根因就是逐帧 seek：
    // seek 到非关键帧必须回退到前一个 IDR 重新解码，多路同时做必然跟不上帧率。
    // 帧号轴统一按片段帧率，其余片段用固定 playbackRate 换算；rate 只在起播时
    // 设一次、中途不动，所以不会触发解码速率切换。
    async play() {
        if (this.playing || !this.videos.length) return;
        this.playing = true;
        const targets = this.videos.filter(v => v.video.readyState >= 1);
        if (!targets.length) { this.playing = false; return; }
        // 停在末帧时按播放，从 0 重来：否则一起来就被末尾判定拉回，闪一下
        if (this.frame >= this.maxFrame) this.setFrame(0, false);
        const frame = this.frame;
        await Promise.all(targets.map(v => this._seekAndWait(v, frame)));
        if (!this.playing) return;        // 等待期间被暂停或关闭
        targets.forEach(v => {
            v.video.playbackRate = this._rate(v);
            v.video.play().catch(() => {});
        });
        this.updateBar();
    },

    // 各片段相对帧号轴的播放速率：轴 24fps 配 30fps 片段即 0.8，再乘用户倍速。
    // 用户倍速必须在这里并入：本控制器持续改写 playbackRate，单独在外面设会被冲掉。
    _rate(v) {
        return (this.fps / (v.fps || 24)) * (v.video._bzRate || 1);
    },

    // 用户改倍速后重算所有片段的速率（只在被调时写，watch 里不碰）
    refreshRate() {
        this.videos.forEach(v => { v.video.playbackRate = this._rate(v); });
    },

    // 等 seek 落地再起播，起播参差就只剩毫秒级；超时兜底避免个别片段拖住整体
    _seekAndWait(v, frame) {
        const t = frame / v.fps;
        if (Math.abs(v.video.currentTime - t) <= 0.5 / v.fps) return Promise.resolve();
        return new Promise(resolve => {
            let done = false;
            const finish = () => {
                if (done) return;
                done = true;
                v.video.removeEventListener("seeked", finish);
                resolve();
            };
            v.video.addEventListener("seeked", finish);
            setTimeout(finish, 400);
            v.video.currentTime = t;
        });
    },

    // 停下原生播放但不回拉位置：拖动 / 逐帧要立刻定位，回拉等于白跑一次 seek
    _stopPlayback() {
        if (!this.playing) return;
        this.playing = false;
        this.videos.forEach(v => { v.video.pause(); v.video.playbackRate = v.video._bzRate || 1; });
    },

    // 暂停 = 停下播放并把公共进度拉回帧号，让所有人重新严格对齐。
    // playbackRate 已按帧率换算，各片段的 currentTime 就是公共进度，任取一个即可。
    pause() {
        if (!this.playing) return;
        this._stopPlayback();
        const v = this.videos[0];
        if (v) this.setFrame(Math.round(v.video.currentTime * this.fps), true);
    },

    togglePlay() {
        if (this.playing) this.pause();
        else this.play();
    },

    // 逐帧：暂停后单步，天然精确到帧
    step(delta) {
        this._stopPlayback();
        this.setFrame(this.frame + delta, true);
    },

    // 拖动帧滑块：直接落到目标帧
    seekFrame(k) {
        this._stopPlayback();
        this.setFrame(k, true);
    },

    // 常驻低频监视（200ms）：视口进出、播放末尾回绕、控制条刷新。
    // 播放期间这里一个字都不往 currentTime / playbackRate 上写，不干扰解码。
    watch() {
        if (!this.enabled) return;
        const prev = this.videos;
        this.scan();
        const nowNodes = new Set(this.videos.map(v => v.node));

        // 滚出视口的：立刻停解码，不再参与同步
        prev.forEach(v => { if (!nowNodes.has(v.node)) v.video.pause(); });

        // 播放态不变量：视口内每个片段都该在播。刚滚进视口的、元数据迟到还没被
        // 起播的，都由 _join 补上；已就位在播的直接返回。
        if (this.playing) this.videos.forEach(v => this._join(v));

        // 播到最短片段的末尾就整体回绕，否则长的还在走、短的已经自己循环了
        const pacer = this.videos[0];
        if (this.playing && pacer) {
            const end = (this.maxFrame + 1) / this.fps;
            if (pacer.video.currentTime >= end) {
                this._stopPlayback();
                this.videos.forEach(v => v.video.pause());
                this.setFrame(0, true);
                return;
            }
        }
        this.updateBar();
    },

    // 让一个片段归位到当前帧并起播；已就位在播的直接返回。
    // 用元素上的标记防重入：seek 最长等 400ms，而 watch 每 200ms 一轮。
    _join(v) {
        const el = v.video;
        if (el._bz_joining || el.seeking || el.readyState < 2 || !el.paused) return;
        el._bz_joining = true;
        this._seekAndWait(v, this.frame).then(() => {
            el._bz_joining = false;
            if (!this.enabled || !this.playing || !el.paused) return;
            if (!isNodeVisible(v.node)) return;   // 等待期间已滚出视口
            el.playbackRate = this._rate(v);
            el.play().catch(() => {});
        });
    },

    showBar() {
        if (this.bar) { this.bar.style.display = "flex"; this.updateBar(); return; }

        const bar = document.createElement("div");
        bar.style.cssText = `
            position:fixed; left:50%; top:52px; transform:translateX(-50%);
            z-index:99999; display:flex; align-items:center; gap:10px;
            padding:6px 12px; border-radius:8px;
            background:rgba(18,18,18,0.92); border:1px solid #333;
            box-shadow:0 2px 12px rgba(0,0,0,0.5);
            font-family:-apple-system,'Microsoft YaHei','Segoe UI',sans-serif;
            font-size:12px; color:#bbb;
        `;
        bar.innerHTML = `
            <style>
                .bz-sb-title { color:#999; font-size:11px; white-space:nowrap; }
                .bz-sb-btn { background:none; border:none; cursor:pointer; font-size:15px; color:#999; padding:0 2px; line-height:1; }
                .bz-sb-btn:hover { color:#ddd; }
                .bz-sb-seek { width:160px; height:4px; cursor:pointer; accent-color:#888; margin:0; }
                .bz-sb-time { min-width:78px; text-align:center; color:#888; font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap; }
            </style>
            <span class="bz-sb-title">🎬 同步</span>
            <button class="bz-sb-btn" data-act="prev" title="上一帧">◀</button>
            <button class="bz-sb-btn" data-act="play" title="播放/暂停">▶️</button>
            <button class="bz-sb-btn" data-act="next" title="下一帧">▶</button>
            <input class="bz-sb-seek" data-act="seek" type="range" min="0" max="0" value="0">
            <span class="bz-sb-time" data-act="time">0 / 0</span>
            <button class="bz-sb-btn" data-act="close" title="退出同步">✕</button>
        `;
        document.body.appendChild(bar);
        this.bar = bar;

        bar.querySelector('[data-act="play"]').addEventListener("click", () => this.togglePlay());
        bar.querySelector('[data-act="prev"]').addEventListener("click", () => this.step(-1));
        bar.querySelector('[data-act="next"]').addEventListener("click", () => this.step(1));
        bar.querySelector('[data-act="close"]').addEventListener("click", () => this.disable());
        bar.querySelector('[data-act="seek"]').addEventListener("input", (e) => this.seekFrame(+e.target.value));
        this.updateBar();
    },

    hideBar() {
        if (this.bar) this.bar.style.display = "none";
    },

    updateBar() {
        if (!this.bar) return;
        const max = this.maxFrame;
        // 播放时进度由解码推进，暂停/逐帧时由 this.frame 决定；
        // playbackRate 已按帧率换算，任取一个片段的 currentTime 就是公共进度
        const pacer = this.videos[0];
        const frame = this.playing && pacer
            ? Math.max(0, Math.min(Math.round(pacer.video.currentTime * this.fps), max))
            : this.frame;
        const titleEl = this.bar.querySelector(".bz-sb-title");
        const playBtn = this.bar.querySelector('[data-act="play"]');
        const seek = this.bar.querySelector('[data-act="seek"]');
        const timeEl = this.bar.querySelector('[data-act="time"]');
        titleEl.textContent = `🎬 同步 · ${this.videos.length}个`;
        playBtn.textContent = this.playing ? "⏸️" : "▶️";
        if (seek.max !== String(max)) seek.max = String(max);
        if (seek.value !== String(frame)) seek.value = String(frame);
        timeEl.textContent = `${frame} / ${max}`;
    },
};

// 暴露到全局，方便控制台手动调进度（如 VideoSyncController.setFrame(120, true)）
window.VideoSyncController = VideoSyncController;

// 版本标记必须打在文件最外层：ComfyUI 批量加载扩展时，只要有别的扩展 404/失败，
// 整批 setup() 都会被跳过，写在 setup 里的日志永远不会出现，等于没有验证手段
console.log("[班长视频合并] 前端 v1.15 已加载（调色参数只保留 🎨 调色台面板入口：新建节点也隐藏参数区同名滑条）");

// 调色参数表：控件名 / 默认值 / 滑条范围 / 显示格式，一处定义，面板、写入、恢复共用
const BZ_TUNE_SPEC = [
    { g: "饱和度", c: "#8ad4a8", rows: [
        { k: "sat", w: "🎨 饱和度", label: "饱和度", min: 0, max: 200, def: 100, scale: 100, unit: "%" },
    ]},
    { g: "泛光 Bloom", c: "#f0c674", rows: [
        { k: "bloom",    w: "✨ 泛光强度", label: "强度", min: 0, max: 100, def: 0,  scale: 100, unit: "%" },
        { k: "bloomThr", w: "✨ 泛光起点", label: "起点 · 多亮才发光", min: 0, max: 95, def: 75, scale: 100, unit: "%" },
        { k: "bloomRad", w: "✨ 泛光范围", label: "范围 · 扩散多远", min: 1, max: 10, step: 0.5, def: 4, scale: 1, unit: "" },
    ]},
    { g: "柔雾 Mist", c: "#a8c8f0", rows: [
        { k: "soft",    w: "🌫️ 柔雾强度", label: "强度 · 柔焦量", min: 0, max: 100, def: 0,  scale: 100, unit: "%" },
        { k: "softRad", w: "🌫️ 柔焦范围", label: "柔焦范围", min: 1, max: 10, step: 0.5, def: 4, scale: 1, unit: "" },
        { k: "haze",    w: "🌫️ 灰雾浓度", label: "灰雾 · 抬黑位", min: 0, max: 100, def: 30, scale: 100, unit: "%" },
    ]},
    { g: "暖晕 Halation", c: "#f0a86a", rows: [
        { k: "warm",    w: "🔥 暖晕强度", label: "强度 · 红橙溢出", min: 0, max: 100, def: 0,  scale: 100, unit: "%" },
        { k: "warmThr", w: "🔥 暖晕起点", label: "起点 · 多亮才出暖晕", min: 0, max: 95, def: 70, scale: 100, unit: "%" },
        { k: "warmRad", w: "🔥 暖晕范围", label: "范围 · 扩散多远", min: 1, max: 10, step: 0.5, def: 5, scale: 1, unit: "" },
    ]},
];

const BZ_TUNE_FLAT = BZ_TUNE_SPEC.flatMap(g => g.rows.map(r => ({ ...r, color: g.c })));

// 上一版存的老属性名 → 新参数名，仅用于迁移用户已经调好的值
const BZ_TUNE_LEGACY = {
    bz_preview_sat: "sat", bz_preview_bloom: "bloom",
    bz_preview_bloom_thr: "bloomThr", bz_preview_bloom_rad: "bloomRad",
    bz_preview_soft: "soft", bz_preview_warm: "warm",
};

function bzTuneDefaults() {
    const t = {};
    BZ_TUNE_FLAT.forEach(r => { t[r.k] = r.def / r.scale; });
    return t;
}

/** 面板样式只注入一次（玻璃质感 + 细滑条，配色随主题走深色） */
function bzInjectTuneStyle() {
    if (document.getElementById("bz-tune-style")) return;
    const s = document.createElement("style");
    s.id = "bz-tune-style";
    s.textContent = `
.bz-tune-panel{position:fixed;z-index:10000;width:238px;box-sizing:border-box;border-radius:12px;overflow:hidden;
background:linear-gradient(180deg,rgba(32,32,37,.97),rgba(21,21,25,.97));border:1px solid rgba(255,255,255,.09);
box-shadow:0 14px 34px rgba(0,0,0,.55),0 2px 6px rgba(0,0,0,.45);backdrop-filter:blur(14px);
font-family:-apple-system,'Microsoft YaHei','Segoe UI',sans-serif;color:#e8e8ea;user-select:none;}
.bz-tune-head{display:flex;align-items:center;gap:7px;padding:9px 11px 8px;border-bottom:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.035);}
.bz-tune-head b{flex:1;font-size:12px;font-weight:600;letter-spacing:.4px;}
.bz-tune-x{background:none;border:none;color:#999;cursor:pointer;font-size:13px;line-height:1;padding:2px 3px;border-radius:4px;}
.bz-tune-x:hover{color:#fff;background:rgba(255,255,255,.1);}
.bz-tune-body{padding:2px 11px 4px;max-height:min(72vh,620px);overflow-y:auto;}
.bz-tune-body::-webkit-scrollbar{width:6px}
.bz-tune-body::-webkit-scrollbar-thumb{background:rgba(255,255,255,.15);border-radius:3px}
.bz-grp{margin-top:9px}
.bz-grp-t{display:flex;align-items:center;gap:6px;font-size:11px;font-weight:600;color:#d2d2d8;margin:0 0 6px}
.bz-grp-t i{width:6px;height:6px;border-radius:50%;display:inline-block;flex:none}
.bz-grp-t u{flex:1;height:1px;background:rgba(255,255,255,.09);text-decoration:none}
.bz-row{display:flex;align-items:center;justify-content:space-between;margin:0 0 2px}
.bz-row span{font-size:10.5px;color:#96969e}
.bz-row b{font-size:10.5px;font-weight:600;font-family:ui-monospace,Consolas,monospace;font-variant-numeric:tabular-nums}
.bz-tune-range{-webkit-appearance:none;appearance:none;width:100%;height:13px;margin:0 0 8px;background:transparent;cursor:pointer;display:block}
.bz-tune-range::-webkit-slider-runnable-track{height:3px;border-radius:2px;
background:linear-gradient(to right,var(--c) 0 var(--p),rgba(255,255,255,.13) var(--p) 100%)}
.bz-tune-range::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;margin-top:-4px;border-radius:50%;
background:#f4f4f6;border:2px solid var(--c);box-shadow:0 1px 3px rgba(0,0,0,.5)}
.bz-tune-range::-moz-range-track{height:3px;border-radius:2px;background:rgba(255,255,255,.13)}
.bz-tune-range::-moz-range-progress{height:3px;border-radius:2px;background:var(--c)}
.bz-tune-range::-moz-range-thumb{width:9px;height:9px;border-radius:50%;background:#f4f4f6;border:2px solid var(--c)}
.bz-tune-foot{display:flex;gap:6px;padding:8px 11px;border-top:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.02)}
.bz-btn{flex:1;padding:5px 0;border-radius:8px;font-size:11px;cursor:pointer;text-align:center;
border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.06);color:#d8d8dc;
font-family:inherit;transition:background .12s}
.bz-btn:hover{background:rgba(255,255,255,.13)}
.bz-btn.main{flex:1.4;background:#3a6ea5;border-color:#4a80bb;color:#fff;font-weight:600}
.bz-btn.main:hover{background:#4a80bb}
.bz-tune-msg{padding:0 11px 9px;font-size:10.5px;line-height:1.4;color:#8a8a92;min-height:13px}
`;
    document.head.appendChild(s);
}

/**
 * 构建/更新「泛光 → 暖晕 → 柔雾」SVG 滤镜（每节点复用同一个 <filter id="bz-bloom-<id>">）
 * CSS filter 做不出「只让亮部发光」（blur 会糊整张图），必须走 SVG：
 *   亮度矩阵取亮度 → 阈值截断 → 抠出原色高光 → 高斯模糊 →（暖晕分支另走一条阈值再染红橙）
 *   → 分别叠加 → 柔焦混底 + 降对比抬黑位
 * 与后端 _apply_tuning 的泛光/暖晕/柔雾分支同构，同样只用单次高斯。
 * t: 调色参数对象（含 width）
 */
function bzBuildBloomFilter(nodeId, t) {
    let defs = document.getElementById("bz-bloom-defs");
    if (!defs) {
        defs = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        defs.id = "bz-bloom-defs";
        defs.setAttribute("width", "0");
        defs.setAttribute("height", "0");
        defs.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;pointer-events:none";
        document.body.appendChild(defs);
    }
    const id = "bz-bloom-" + nodeId;
    let f = defs.querySelector('filter[id="' + id + '"]');
    if (!f) {
        f = document.createElementNS("http://www.w3.org/2000/svg", "filter");
        f.setAttribute("id", id);
        defs.appendChild(f);
    }

    const bloom = +t.bloom || 0;
    const warm = +t.warm || 0;
    const soft = +t.soft || 0;
    const W = t.width || 400;
    // 阈值线性截断：out = slope*in + intercept（低于起点的被压成 0）
    const cut = (thr) => {
        const span = Math.max(0.05, 1 - thr);
        return [(1 / span).toFixed(4), (-thr / span).toFixed(4)];
    };
    const [bs, bi] = cut(t.bloomThr == null ? 0.75 : +t.bloomThr);
    const [ws, wi] = cut(t.warmThr == null ? 0.70 : +t.warmThr);
    // stdDeviation 按预览宽度换算，保证与后端「相对画面比例」一致
    const sdB = ((+t.bloomRad || 4) * 0.005 * W).toFixed(2);
    const sdW = ((+t.warmRad || 5) * 0.005 * W).toFixed(2);
    // 柔焦与后端同一套系数：混入比例 / 半径（按宽度取比例）/ 灰雾降对比抬黑位
    const mix = soft * 0.5;
    const sdS = ((+t.softRad || 4) * 0.0022 * W).toFixed(2);
    const haze = +t.haze || 0;
    const cSlope = (1 - haze * 0.18).toFixed(4);
    const cIntercept = (haze * 0.055 - (1 - haze * 0.18) * 0.5 + 0.5).toFixed(4);

    const parts = [];
    parts.push(`<feColorMatrix in="SourceGraphic" type="matrix" result="lum" values="0.299 0.587 0.114 0 0  0.299 0.587 0.114 0 0  0.299 0.587 0.114 0 0  0 0 0 1 0"/>`);
    // ── 泛光分支 ──
    parts.push(`<feComponentTransfer in="lum" result="maskB">
        <feFuncR type="linear" slope="${bs}" intercept="${bi}"/>
        <feFuncG type="linear" slope="${bs}" intercept="${bi}"/>
        <feFuncB type="linear" slope="${bs}" intercept="${bi}"/>
    </feComponentTransfer>`);
    parts.push(`<feComposite in="SourceGraphic" in2="maskB" operator="arithmetic" k1="1" k2="0" k3="0" k4="0" result="brightB"/>`);
    parts.push(`<feGaussianBlur in="brightB" stdDeviation="${sdB}" result="glowB"/>`);
    parts.push(`<feComposite in="SourceGraphic" in2="glowB" operator="arithmetic" k1="0" k2="1" k3="${bloom}" k4="0" result="mB"/>`);
    // ── 暖晕分支：独立阈值与半径，只把溢出光染成红橙（红几乎全留、蓝衰减最狠）──
    parts.push(`<feComponentTransfer in="lum" result="maskW">
        <feFuncR type="linear" slope="${ws}" intercept="${wi}"/>
        <feFuncG type="linear" slope="${ws}" intercept="${wi}"/>
        <feFuncB type="linear" slope="${ws}" intercept="${wi}"/>
    </feComponentTransfer>`);
    parts.push(`<feComposite in="SourceGraphic" in2="maskW" operator="arithmetic" k1="1" k2="0" k3="0" k4="0" result="brightW"/>`);
    parts.push(`<feGaussianBlur in="brightW" stdDeviation="${sdW}" result="glowW0"/>`);
    parts.push(`<feColorMatrix in="glowW0" type="matrix" result="glowW" values="1.35 0 0 0 0  0 0.60 0 0 0  0 0 0.28 0 0  0 0 0 1 0"/>`);
    parts.push(`<feComposite in="mB" in2="glowW" operator="arithmetic" k1="0" k2="1" k3="${warm}" k4="0" result="mW"/>`);
    // ── 柔雾：混底柔焦 + 降对比抬黑位（放最后，连光晕一起柔化才像镜头整体柔光）──
    // 灰雾单独改动（柔雾强度增量为 0）也要走这段对比度调整，否则灰雾滑条「没反应」
    if (soft > 0.005 || haze > 0.005) {
        parts.push(`<feGaussianBlur in="mW" stdDeviation="${sdS}" result="softBlur"/>`);
        parts.push(`<feComposite in="mW" in2="softBlur" operator="arithmetic" k1="0" k2="${(1 - mix).toFixed(4)}" k3="${mix.toFixed(4)}" k4="0" result="softMix"/>`);
        parts.push(`<feComponentTransfer in="softMix">
            <feFuncR type="linear" slope="${cSlope}" intercept="${cIntercept}"/>
            <feFuncG type="linear" slope="${cSlope}" intercept="${cIntercept}"/>
            <feFuncB type="linear" slope="${cSlope}" intercept="${cIntercept}"/>
        </feComponentTransfer>`);
    }

    f.setAttribute("x", "-15%");
    f.setAttribute("y", "-15%");
    f.setAttribute("width", "130%");
    f.setAttribute("height", "130%");
    f.setAttribute("color-interpolation-filters", "sRGB");
    f.innerHTML = parts.join("\n");
}

/** 按当前参数拼出 CSS filter 串（饱和度用 CSS，泛光/暖晕/柔雾走 SVG 滤镜）
 *  注意：传入的应是「改动量」（相对基线的增量），见 refreshFilter 的基线增量模型 */
function bzFilterCss(nodeId, t) {
    // 不能写 (+t.sat || 1)：sat=0 会被 || 短路成 1，饱和度拉到 0 就「没反应」
    const s = +t.sat;
    let f = (Number.isFinite(s) && Math.abs(s - 1) >= 0.005) ? `saturate(${s})` : "";
    if ((+t.bloom || 0) > 0.005 || (+t.warm || 0) > 0.005 || (+t.soft || 0) > 0.005 || (+t.haze || 0) > 0.005) {
        bzBuildBloomFilter(nodeId, t);
        f = (f + ` url(#bz-bloom-${nodeId})`).trim();
    }
    return f;
}

/**
 * 👨‍🏫班长: 视频合并前端增强插件 (V5.0 结构化信息面板)
 * 
 * 信息面板结构:
 * ┌─ 标题栏（折叠按钮 | 📋 视频详情 | 复制全部 ─┐
 * ├─ 结构化字段行（文件/尺寸/帧数/时长/帧率/体积/编码）─┤
 * └─ 底部文件路径行（📁路径 | 📂打开目录 ─┘
 */

// 当前正在进行的那一次「应用并导出」。后端按批广播进度，按钮每 200ms 读一次
// 这里显示百分比；导出结束（成功或失败）立刻置空，避免下一段视频读到旧进度。
// 用 src 匹配：画布上可以有多个视频合并节点同时存在，广播是全局的。
let _bakeWatch = null;

app.registerExtension({
    name: "banzhang.VideoCombine",
    async setup() {
        // 节点开始执行时清位：本次运行尚未产出视频，防止失败/空跑的耗时覆盖上次真实值
        api.addEventListener("executing", ({detail}) => {
            // 兼容新版 ComfyUI: executing payload 为 {node, display_node, prompt_id} 字典
            const nodeId = (detail && typeof detail === "object") ? detail.node : detail;
            if (!nodeId) return;
            const node = app.graph.getNodeById(nodeId);
            if (!node || node.type !== "banzhangVideoCombine") return;
            node._bz_run_has_output = false;
        });

        // 调色导出进度：后端每批广播一次已处理帧数，这里只更新缓存值，
        // 显示由「应用并导出」按钮的定时器统一读，避免事件回调直接改 DOM
        api.addEventListener("banzhang.bake.progress", ({ detail }) => {
            if (!_bakeWatch || !detail || detail.src !== _bakeWatch.src) return;
            _bakeWatch.done = detail.done;
            _bakeWatch.total = detail.total;
        });

        // 监听累计执行时间事件：只更新缓存值，渲染统一由 updateInfoPanel 完成
        api.addEventListener("Banzhang-DevUtils.ExecutionTime.executed", ({detail}) => {
            if (!detail.formatted_cumulative_time) return;
            const node = app.graph.getNodeById(detail.node);
            if (!node || node.type !== "banzhangVideoCombine") return;

            // 本次运行未产出视频（如输入断开导致报错）时不覆盖上次累计耗时
            if (node._bz_run_has_output !== true) return;

            node.bz_et_formatted_cumulative_time = detail.formatted_cumulative_time;

            // 累计耗时来自运行时事件，不随工作流序列化；这里同步缓存到 properties，
            // 以便刷新/重启后 onConfigure 恢复常显
            if (node.properties) node.properties["bz_et_cumulative_time"] = detail.formatted_cumulative_time;

            // 已渲染过详情面板则整体重渲染（保证高度与累计行一致）
            if (node._painter_last_detail && node._bzUpdateInfoPanel) {
                node._bzUpdateInfoPanel(node._painter_last_detail);
            }
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "banzhangVideoCombine") return;

        const CONTROLS_HEIGHT = 44;
        const DEFAULT_PREVIEW_H = 240;  // 画面默认高度：未拖拽过时用（拖大后存 widget._bz_fixed_h）
        const MIN_VIDEO_H = 64;         // 画面最小高度：布局下限锚点，也是拖节点时的下限
        // 正在从工作流恢复（onConfigure）：此间一切自动重排都不许动节点尺寸
        let _bzConfiguring = false;
        const INFO_HEADER_H = 30;
        const INFO_ROW_H = 22;
        const INFO_PATH_ROW_H = 30;
        const INFO_PADDING = 16;   // 上下 padding 合计

        // 面板高度按字段数静态推算。
        // 不能用 scrollHeight 实测：computeSize 会在 onConfigure / 刷新等
        // DOM 尚未挂载的时机被调用，此时 scrollHeight 恒为 0，面板会被压成 0 高而消失。
        function calcInfoHeight(fieldCount, hasPath, collapsed) {
            if (collapsed) return INFO_HEADER_H;
            return INFO_HEADER_H + fieldCount * INFO_ROW_H + (hasPath ? INFO_PATH_ROW_H : 0) + INFO_PADDING;
        }

        const getExtraMenuOptions = nodeType.prototype.getExtraMenuOptions;
        nodeType.prototype.getExtraMenuOptions = function (_, options) {
            getExtraMenuOptions?.apply(this, arguments);
            options.push({
                content: "🔄 同步预览所有视频",
                checked: VideoSyncController.enabled,
                callback: () => {
                    VideoSyncController.enabled ? VideoSyncController.disable() : VideoSyncController.enable();
                }
            });
        };

        // ────────────────────────────────────────────
        // 尺寸模型（根治版）：只有两个真值
        //   ① widget._bz_fixed_h —— 画面窗口高度，用户拖节点边框的意图，随工作流保存
        //   ② 节点高度          —— 由官方 arrange 按「内容底边」算出，是 ① 的**函数**
        //
        // 画面走官方「弹性分支」computeLayoutSize：min = 最小可用高、max = 期望高。
        //   · 节点够高 → 拿满 max，画面恒为 _bz_fixed_h，折叠/展开都不变
        //   · 节点不够 → 先压到 min，再由 arrange 把节点撑高（画面不会被挤没）
        //   · max 是常数、不引用 node.size → 不存在「引用自身尺寸」的自增暴涨
        //
        // 反向换算（节点高 → 画面高）只在**用户正在拖边框**时发生，判据是官方
        // LGraphCanvas.resizing_node（仅拖拽期间置位，见 index-D1rfh02W.js:130644）。
        // 算式：画面高 = 节点高 − 除画面外的一切；「除画面外的一切」取自 node.computeSize()
        // （官方口径，且它按 min 计、与画面高度无关 → 稳定、无反馈回路）。
        // 于是所有自动重排都不写画面高度，**不需要任何标记位/启发式**。
        // ────────────────────────────────────────────
        function previewWidgetOf(node) {
            return node.widgets?.find(w => w.name === "painter_preview");
        }

        // 预览区期望总高 = 画面 + 控制条 + 详情面板
        function previewWantH(node, widget) {
            return (widget._bz_fixed_h || DEFAULT_PREVIEW_H) + CONTROLS_HEIGHT + (node._painter_info_h || 0);
        }

        function attachPreviewLayout(node, widget) {
            // DOMWidgetImpl 自带 computeLayoutSize，这里整体接管。
            // computeSize 置空是必须的：官方分支判断为「computeSize 优先」，
            // 留着它就会走固定高度分支，永远轮不到弹性分支。
            widget.computeSize = void 0;
            widget.computeLayoutSize = () => ({
                minHeight: MIN_VIDEO_H + CONTROLS_HEIGHT + (node._painter_info_h || 0),
                maxHeight: previewWantH(node, widget),
                minWidth: 0,
                maxWidth: 0,
            });
        }

        // arrange 之后每个 widget 的 y / computedHeight 就是官方的权威布局结果，
        // 直接读底边。自己按公式复算必然和官方差几个 padding —— 那正是「越改越歪」的来源。
        function contentBottom(node) {
            let bottom = 0;
            for (const w of node.widgets ?? []) {
                const h = w.computedHeight ?? 0;
                if (h <= 0) continue;
                bottom = Math.max(bottom, (w.y ?? 0) + h);
            }
            return bottom;
        }

        // 重排：先让画面拿到期望高度（空间不够就把节点加高），再把节点收到内容底边。
        // 全程不碰 widget._bz_fixed_h —— 画面尺寸恒定，变化的只有节点总高。
        function refit(node) {
            if (_bzConfiguring || !node.graph) return;
            node.setDirtyCanvas(true, true);
            if (typeof node.arrange !== "function") return;

            const widget = previewWidgetOf(node);
            if (widget) {
                for (let i = 0; i < 3; i++) {
                    node.arrange();
                    const deficit = previewWantH(node, widget) - (widget.computedHeight ?? 0);
                    if (deficit <= 0.5) break;
                    node.setSize([node.size[0], node.size[1] + deficit]);
                }
            } else {
                node.arrange();
            }

            // 官方只撑不缩：折叠/收起详情后必须自己把节点收到内容底边，否则留一片空白
            const bottom = contentBottom(node);
            if (bottom > 0 && Math.round(node.size[1]) !== Math.round(bottom)) {
                node.setSize([node.size[0], bottom]);
            }
            node.setDirtyCanvas(true, true);
        }

        // ────────────────────────────────────────────
        // 隐藏 widget 的高度归零
        // 官方 #arrangeWidgets（index-D1rfh02W.js:126195）遍历 this.widgets 时
        // 完全没有 w.hidden 判断：隐藏控件照样按 NODE_WIDGET_HEIGHT + 4
        // 计入 fixedWidgetHeight，末尾 if (y2 > bodyHeight) setSize(...) 再把节点撑回去。
        // 于是「仅画面模式」折叠后底部仍留一大片空白，且 refit 收缩无效（下一帧又被撑回）。
        // 根治：让隐藏 widget 走官方「弹性分支」并被分配 0 高度。
        // 不动 widgets 数组结构 —— 避免打乱 widgets_values 的下标恢复。
        // ────────────────────────────────────────────
        function applyWidgetHiddenLayout(w, hidden) {
            if (hidden) {
                if (w._bz_size_saved) return;
                w._bz_size_saved = { cs: w.computeSize, cls: w.computeLayoutSize };
                // 官方分支顺序是 computeSize 优先，必须先让出才能落到 computeLayoutSize。
                // 置 undefined（而非 delete）：原型上若挂着 computeSize，delete 删不掉，
                // 官方 `if (w.computeSize)` 仍会命中固定分支，隐藏就失效。
                w.computeSize = void 0;
                w.computeLayoutSize = () => ({ minHeight: 0, maxHeight: 0, minWidth: 0, maxWidth: 0 });
            } else if (w._bz_size_saved) {
                w.computeSize = w._bz_size_saved.cs;   // 原值可能就是 undefined，照原样还原
                if (w._bz_size_saved.cls) w.computeLayoutSize = w._bz_size_saved.cls;
                else delete w.computeLayoutSize;
                delete w._bz_size_saved;
            }
        }

        // ────────────────────────────────────────────
        // 仅画面模式：折叠所有参数控件与详情面板，只保留视频画面
        // 状态持久化到 properties["bz_video_only"]，刷新后保持
        // ────────────────────────────────────────────
        function applyVideoOnlyMode(node, collapsed, refitSize = true) {
            node._bz_video_only = collapsed;
            if (node.properties) node.properties["bz_video_only"] = collapsed;

            node.widgets?.forEach(w => {
                if (w.name === "painter_preview" || w.type === "hidden") return;
                w.hidden = collapsed;
                applyWidgetHiddenLayout(w, collapsed);
            });

            const container = node.widgets?.find(w => w.name === "painter_preview")?.element;
            const panel = container?.querySelector(".painter-info-panel");
            if (panel) panel.style.display = collapsed ? "none" : "";
            // 面板进出布局后重算缓存高度：refit 依赖它，隐藏时必须归零
            applyPanelHeight(node, panel);
            const btn = container?.querySelector(".bz-fold-btn");
            if (btn) {
                btn.textContent = collapsed ? "⌄" : "⌃";
                btn.title = collapsed ? "展开参数与详情" : "折叠参数，仅显示画面";
            }

            // 参数区/详情面板显隐切换 → 预览区高度恒定不变，节点整体高矮由官方 arrange 决定
            // （参数区隐藏则节点变矮，展开则变高，画面尺寸自始至终不动）
            if (refitSize) refit(node);
        }

        // 官方口径下「除画面外的一切」= 画面顶边 + 排在画面之后的其他控件高度。
        // 直接读官方上一次排版的 widget.y，不自己套公式：
        // node.computeSize() 比 arrange 多算 +8+6（widgets_height 尾部的 padding），
        // 用它当基准会凭空差出 14px，画面每次都比拖拽意图矮一截。
        function chromeH(node, widget) {
            const top = widget.y ?? 0;
            let below = 0;
            for (const w of node.widgets ?? []) {
                if (w === widget) continue;
                if ((w.y ?? 0) >= top) below += (w.computedHeight ?? 0);
            }
            return top + below;
        }

        // 节点尺寸 → 画面高度的反向换算：**只在用户拖边框时**发生。
        // 判据是官方 LGraphCanvas.resizing_node —— 它仅在拖拽期间指向当前节点，
        // 官方 arrange 撑高、自家 refit 收缩、刷新恢复三种 setSize 都不会置位，
        // 因此不存在误判，也就不需要任何标记位。
        nodeType.prototype.onResize = function (size) {
            if (app.canvas?.resizing_node !== this) return;
            const widget = previewWidgetOf(this);
            if (!widget) return;

            let chrome;
            if (widget.computedHeight > 0) {
                chrome = chromeH(this, widget);
            } else {
                // 还没排过版：退回官方 computeSize（按画面最小高计算，与画面高度无关，无反馈）
                const minH = this.computeSize?.()?.[1];
                if (!minH) return;
                chrome = minH - MIN_VIDEO_H;
            }
            const h = Math.round(size[1] - chrome - CONTROLS_HEIGHT - (this._painter_info_h || 0));
            if (h < MIN_VIDEO_H) return;
            widget._bz_fixed_h = h;
            // 存进 properties 随工作流保存；刷新后恢复，画面尺寸不丢
            if (this.properties) this.properties["bz_preview_h"] = h;
        };

        nodeType.prototype.onExecuted = function (message) {
            const data = message?.painter_output?.[0];
            if (data?.filename) {
                // 本次运行产出了视频：更新预览与详情，并允许累计耗时事件更新缓存
                this._bz_run_has_output = true;
                this.properties["painter_output_cache"] = data;
                updateVideoPreview(this, data);
                if (message?.detail_info?.[0]) {
                    this.properties["detail_info_cache"] = message.detail_info[0];
                    updateInfoPanel(this, message.detail_info[0]);
                }
            } else {
                // 本次未产出视频（失败/空跑/无输入）：预览与详情面板保持原状不动
                this._bz_run_has_output = false;
            }
            this.setDirtyCanvas(true, true);
        };

        // onConfigure 的实现体：恢复工作流时节点尺寸以存档为准，期间禁止一切自动重排
        // （下方 updateInfoPanel 内部会调 refit，靠这个开关挡掉）
        function configureNode(node) {
            // 兼容旧工作流：ComfyUI 给缺少「输出模式」的老节点补值时可能填空串/非法值，
            // 会在 prompt 校验阶段报 "Value not in list"，此处归一到默认值
            const modeWidget = node.widgets?.find(w => w.name === "🔀 输出模式");
            if (modeWidget) {
                const legal = ["自动", "强制视频", "强制图片"];
                if (!legal.includes(modeWidget.value)) {
                    modeWidget.value = "自动";
                }
            }
            // 先恢复累计耗时（运行态数据），否则渲染详情时会因实例属性为空而跳过该行
            if (node.properties?.["bz_et_cumulative_time"]) {
                node.bz_et_formatted_cumulative_time = node.properties["bz_et_cumulative_time"];
            }
            // 恢复锁定的画面高度（用户上次拖拽的结果），必须在建预览 widget 之前写好，
            // 否则布局会先用默认值算出错误高度
            if (node.properties?.["bz_preview_h"]) {
                node._bz_restore_h = node.properties["bz_preview_h"];
            }
            if (node.properties?.["painter_output_cache"]) {
                // deferred=true：恢复预览走错峰队列，不在启动早期抢连接
                updateVideoPreview(node, node.properties["painter_output_cache"], true);
            }
            if (node.properties?.["detail_info_cache"]) {
                updateInfoPanel(node, node.properties["detail_info_cache"]);
            }
            // 恢复仅画面模式（隐藏参数控件与详情面板，但保留工作流里存的节点尺寸）
            if (node.properties?.["bz_video_only"]) {
                applyVideoOnlyMode(node, true, false);
            }
        }

        nodeType.prototype.onConfigure = function () {
            // 恢复期间一律不许自动重排改尺寸：工作流里存了多大就是多大
            _bzConfiguring = true;
            try {
                configureNode(this);
            } finally {
                _bzConfiguring = false;
            }
        };

        // 暴露给 setup 事件监听，用于累计耗时事件到达后整体重渲染
        nodeType.prototype._bzUpdateInfoPanel = function (infoText) {
            updateInfoPanel(this, infoText);
        };

        // ────────────────────────────────────────────
        // 解析详情文本 → 结构化字段
        // 后端格式: "📂 文件名称 : value" → 在第一个冒号处切分
        // ────────────────────────────────────────────
        function parseDetailText(text) {
            const fields = [];
            const lines = text.split('\n');
            for (const line of lines) {
                const trimmed = line.trim();
                // 跳过分隔线和空行
                if (/^[━─]+$/.test(trimmed) || !trimmed) continue;

                // 在第一个冒号处切分为 label 和 value
                const colonIdx = trimmed.search(/[:：]/);
                if (colonIdx > 0) {
                    const label = trimmed.substring(0, colonIdx).trim();
                    const value = trimmed.substring(colonIdx + 1).trim();
                    fields.push({ label, value });
                } else {
                    // 没有冒号的行作为纯文本
                    fields.push({ label: "", value: trimmed });
                }
            }
            return fields;
        }

        // ────────────────────────────────────────────
        // 构建信息面板 HTML
        // ────────────────────────────────────────────
        function buildInfoPanelHTML(fields) {
            const mainFields = [];
            let pathValue = "";
            const warnings = [];

            for (const f of fields) {
                if (f.label.includes("输出路径")) {
                    pathValue = f.value;
                } else if (f.label.includes("⚠") || f.label.includes("警告")) {
                    warnings.push(f.value || f.label);
                } else {
                    mainFields.push(f);
                }
            }

            let html = "";

            // --- 字段行 ---
            for (const f of mainFields) {
                html += `<div class="pi-row"><span class="pi-label">${f.label}</span><span class="pi-value">${f.value}</span></div>`;
            }

            // --- 路径行 ---
            if (pathValue) {
                html += `<div class="pi-path-row">`;
                html += `<span class="pi-path-icon" title="${pathValue}">📁</span>`;
                html += `<span class="pi-path-text">${pathValue}</span>`;
                html += `<span class="pi-path-actions">`;
                html += `<button class="pi-btn" data-action="open-folder" data-path="${pathValue}" title="打开视频所在文件夹">📂 打开目录</button>`;
                html += `</span></div>`;
            }

            // --- 警告行 ---
            for (const w of warnings) {
                html += `<div class="pi-warning">${w}</div>`;
            }

            return { html, pathValue, fieldCount: mainFields.length + (pathValue ? 1 : 0) + warnings.length };
        }

        // ────────────────────────────────────────────
        // 更新信息面板
        // ────────────────────────────────────────────
        function updateInfoPanel(node, infoText) {
            let widget = node.widgets?.find(w => w.name === "painter_preview");
            if (!widget) return;

            const container = widget.element;
            let panel = container.querySelector(".painter-info-panel");

            const fields = parseDetailText(infoText);
            node._painter_last_detail = infoText;

            // 帧率与帧数由后端随 painter_output 直给（见 updateVideoPreview）。
            // 早先从详情面板文本里正则抠「帧率」，抠不到就默认 24，各片段真实
            // 帧率不一致时同步基准整体错位。

            // 注入累计执行时间（从工作流开始到本节点完成）
            if (node.bz_et_formatted_cumulative_time) {
                fields.splice(0, 0, { label: "⏱️ 累计耗时", value: node.bz_et_formatted_cumulative_time });
            }

            const { html, pathValue, fieldCount } = buildInfoPanelHTML(fields);
            // 面板高度的推算依据：DOM 未挂载时也能算，折叠/展开复用
            node._painter_field_count = fieldCount;
            node._painter_has_path = !!pathValue;

            if (!panel) {
                panel = document.createElement("div");
                panel.className = "painter-info-panel";
                panel.style.cssText = `
                    width:100%; background:#0c0c0c; border-top:1px solid #2a2a2a;
                    box-sizing:border-box; overflow:hidden; flex:0 0 auto;
                    font-family:-apple-system,'Microsoft YaHei','Segoe UI',sans-serif;
                    font-size:11px; line-height:1.4; color:#aaa;
                    user-select:text;
                `;
                container.appendChild(panel);
            }

            // --- 构建完整 HTML ---
            const collapsed = node._painter_info_collapsed || false;
            const bodyDisplay = collapsed ? "none" : "block";
            const foldIcon = collapsed ? "▸" : "▾";

            panel.innerHTML = `
                <style>
                    .pi-header {
                        height:${INFO_HEADER_H}px; display:flex; align-items:center;
                        padding:0 10px; background:#141414; gap:8px;
                        cursor:pointer; user-select:none;
                        border-bottom:1px solid #1f1f1f;
                    }
                    .pi-header:hover { background:#1a1a1a; }
                    .pi-fold-icon { font-size:10px; color:#666; width:14px; text-align:center; transition:transform 0.15s; }
                    .pi-title { flex:1; font-size:12px; color:#bbb; font-weight:500; }
                    .pi-copy-all {
                        background:none; border:none; cursor:pointer; color:#777;
                        font-size:14px; padding:0 2px; opacity:0.5; transition:opacity 0.15s;
                    }
                    .pi-copy-all:hover { opacity:1; color:#ccc; }

                    .pi-body {
                        padding:8px 12px; display:${bodyDisplay};
                    }
                    .pi-row {
                        display:flex; align-items:baseline; padding:3px 0; gap:8px;
                    }
                    .pi-label {
                        color:#777; white-space:nowrap; min-width:0; flex-shrink:0;
                    }
                    .pi-value {
                        color:#ccc; font-weight:500; word-break:break-all;
                    }
                    .pi-path-row {
                        display:flex; align-items:center; gap:6px; padding:4px 0;
                        margin-top:4px; border-top:1px solid #1f1f1f; padding-top:6px;
                        font-size:10px; color:#888;
                    }
                    .pi-path-icon { flex-shrink:0; }
                    .pi-path-text {
                        flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
                    }
                    .pi-path-actions { display:flex; gap:4px; flex-shrink:0; }
                    .pi-btn {
                        background:#1e1e1e; border:1px solid #333; border-radius:3px;
                        color:#999; cursor:pointer; font-size:12px; padding:2px 6px;
                        transition:all 0.12s; line-height:1;
                    }
                    .pi-btn:hover { background:#2a2a2a; color:#ddd; border-color:#555; }
                    .pi-btn:active { background:#333; }

                    .pi-warning {
                        color:#f0a040; padding:3px 0; font-size:10px;
                    }

                    /* 跨浏览器滚动条 */
                    .pi-body::-webkit-scrollbar { width:4px; }
                    .pi-body::-webkit-scrollbar-track { background:transparent; }
                    .pi-body::-webkit-scrollbar-thumb { background:#333; border-radius:2px; }
                    .pi-body::-webkit-scrollbar-thumb:hover { background:#555; }

                    /* 复制成功提示 */
                    .pi-toast {
                        position:absolute; bottom:40px; left:50%; transform:translateX(-50%);
                        background:rgba(0,0,0,0.85); color:#4f4; padding:4px 12px;
                        border-radius:4px; font-size:11px; pointer-events:none;
                        opacity:0; transition:opacity 0.2s;
                    }
                    .pi-toast.show { opacity:1; }
                </style>
                <div class="pi-header" data-action="toggle-fold">
                    <span class="pi-fold-icon">${foldIcon}</span>
                    <span class="pi-title">📋 视频详情</span>
                    <button class="pi-copy-all" data-action="copy-all" title="复制全部">📋</button>
                </div>
                <div class="pi-body">${html}</div>
                <div class="pi-toast">✅ 已复制</div>
            `;

            // --- 事件绑定 ---
            bindPanelEvents(panel, node, infoText, pathValue);

            // 唯一画面模式下隐藏整个面板（面板可能因新事件重建，需重新应用）
            panel.style.display = node._bz_video_only ? "none" : "";
            applyPanelHeight(node, panel);

            // 详情面板高度变了 → 预览区 minHeight 随之变 → 重排让官方重新分配高度
            refit(node);
        }

        // 面板高度：由 calcInfoHeight 静态推算后写回 style.height。
        // DOM 与节点布局共用同一个值，既不会留空白，也不会因实测为 0 而消失。
        function applyPanelHeight(node, panel) {
            if (!panel || panel.style.display === "none" || node._bz_video_only) {
                node._painter_info_h = 0;   // 面板不参与布局：高度不计入节点
                return;
            }
            const h = calcInfoHeight(
                node._painter_field_count || 0,
                node._painter_has_path || false,
                node._painter_info_collapsed
            );
            node._painter_info_h = h;
            panel.style.height = `${h}px`;
        }

        // ────────────────────────────────────────────
        // 面板交互事件
        // ────────────────────────────────────────────
        function bindPanelEvents(panel, node, infoText, pathValue) {
            // 折叠/展开
            const header = panel.querySelector(".pi-header");
            header.addEventListener("click", (e) => {
                if (e.target.closest("[data-action='copy-all']")) return;
                node._painter_info_collapsed = !node._painter_info_collapsed;
                // 重新渲染（简单切换 body 的 display）
                const body = panel.querySelector(".pi-body");
                const foldIcon = panel.querySelector(".pi-fold-icon");
                if (node._painter_info_collapsed) {
                    body.style.display = "none";
                    foldIcon.textContent = "▸";
                } else {
                    body.style.display = "block";
                    foldIcon.textContent = "▾";
                }
                // body 显示状态已改 → 面板高度随之变化 → 重排
                applyPanelHeight(node, panel);
                refit(node);
            });

            // 复制全部
            const copyAllBtn = panel.querySelector("[data-action='copy-all']");
            copyAllBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                // 累计耗时是运行态数据（渲染时注入，不在 infoText 中），复制时补上
                let copyText = infoText;
                if (node.bz_et_formatted_cumulative_time) {
                    copyText = `⏱️ 累计耗时 : ${node.bz_et_formatted_cumulative_time}\n` + infoText;
                }
                navigator.clipboard.writeText(copyText).then(() => showToast(panel));
            });

            // 打开视频所在文件夹（调用后端在系统文件管理器中打开）
            panel.querySelectorAll("[data-action='open-folder']").forEach(btn => {
                btn.addEventListener("click", async (e) => {
                    e.stopPropagation();
                    const p = btn.dataset.path;
                    try {
                        const resp = await api.fetchApi(`/banzhang_api/open_folder`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ path: p }),
                        });
                        const data = await resp.json();
                        if (!data?.success) showToast(panel, "❌ 打开失败");
                    } catch (err) {
                        showToast(panel, "❌ 打开失败");
                    }
                });
            });
        }

        function showToast(panel, msg = "✅ 已复制") {
            const toast = panel.querySelector(".pi-toast");
            if (!toast) return;
            toast.textContent = msg;
            toast.classList.add("show");
            clearTimeout(toast._timer);
            toast._timer = setTimeout(() => toast.classList.remove("show"), 1500);
        }

        // ────────────────────────────────────────────
        // 视频预览组件
        // ────────────────────────────────────────────
        // 恢复预览（onConfigure）的加载队列：页面刷新/读档时，工作流里的多个视频
        // 合并节点（本工作流 4 个）会同时恢复预览，各占 1 个同域连接；装了大量
        // 扩展的环境下，启动早期连接池本就被扩展 JS 下载占满，4 个视频同时加载会
        // 把连接耗尽，连带 hydrate 的 project 请求超时、段素材恢复不出来。
        // 恢复一律排队：等页面空闲后串行、错峰加载，把连接让给更关键的 hydrate；
        // 渲染完成（onExecuted）不排队，立即播放。
        const _restoreQueue = [];
        let _restoreScheduled = false;
        function queueRestore(fn) {
            _restoreQueue.push(fn);
            if (_restoreScheduled) return;
            _restoreScheduled = true;
            const pump = () => {
                const next = _restoreQueue.shift();
                if (next) {
                    try { next(); } catch (_) {}
                    setTimeout(pump, 350);   // 每段错峰，给其它请求留连接
                } else {
                    _restoreScheduled = false;
                }
            };
            const start = () => setTimeout(pump, 200);
            if (window.requestIdleCallback) window.requestIdleCallback(start, { timeout: 3000 });
            else setTimeout(start, 1500);
        }

        function updateVideoPreview(node, data, deferred = false) {
            if (!data?.filename) return;  // 无有效视频数据不更新预览
            let widget = node.widgets?.find(w => w.name === "painter_preview");

            if (!widget) {
                const container = document.createElement("div");
                container.style = "position:relative; display:flex; flex-direction:column; background:transparent; overflow:hidden; width:100%; border-radius:0 0 8px 8px; border:1px solid #333; box-sizing:border-box;";
                widget = node.addDOMWidget("painter_preview", "preview", container);
                // 先取回上次锁存的画面高度，再挂布局，避免首帧用默认值算错
                if (node._bz_restore_h) widget._bz_fixed_h = node._bz_restore_h;
                attachPreviewLayout(node, widget);

                const video = document.createElement("video");
                // min-height:0 → 容器变矮时压缩画面而不是把控制条挤出容器
                video.style = "width:100%; flex:1 1 auto; min-height:0; display:block; object-fit:contain; background:#000; cursor:pointer;";
                video.loop = true;
                video.muted = true;

                video.onclick = (e) => {
                    e.preventDefault(); e.stopPropagation();
                    if (video.paused) video.play();
                    else video.pause();
                };

                const bar = document.createElement("div");
                bar.className = "bz-controls-bar";
                // flex:0 0 固定 → 控制条永不参与压缩，任何高度下都完整可见
                // position:relative 让饱和度弹窗以控制条为定位参照，稳稳浮在时间轴正上方
                // overflow 必须是 visible：设成 hidden 会把饱和度弹窗裁进 44px 高的控制条里
                // （且 overflow-x/y 一 hidden 一 visible 时，浏览器会把 visible 算成 auto，同样裁剪）
                bar.style = `position:relative; flex:0 0 ${CONTROLS_HEIGHT}px; height:${CONTROLS_HEIGHT}px; display:flex; align-items:center; background:rgba(20,20,20,0.95); padding:0 12px; gap:10px; color:#eee; font-size:13px; border-top:1px solid #333; box-sizing:border-box; overflow:visible; width:100%;`;

                const playBtn = document.createElement("button");
                playBtn.innerText = "⏸️";
                playBtn.style = "background:none; border:none; cursor:pointer; font-size:18px; padding:0; display:flex; align-items:center; flex-shrink:0;";

                const seek = document.createElement("input");
                seek.type = "range"; seek.min = 0; seek.max = 100; seek.value = 0;
                seek.style = "flex:1; cursor:pointer; height:4px; accent-color:#666; background:#333; border-radius:2px; appearance:none; outline:none; min-width:0; margin:0;";

                const timeText = document.createElement("span");
                timeText.innerText = "0:00";
                timeText.style = "font-family:'Consolas',monospace; min-width:35px; text-align:center; color:#aaa; font-size:12px; flex-shrink:0;";

                const muteBtn = document.createElement("button");
                muteBtn.innerText = "🔇";
                muteBtn.style = "background:none; border:none; cursor:pointer; font-size:18px; padding:0; display:flex; align-items:center; flex-shrink:0;";

                video.onplay = () => { playBtn.innerText = "⏸️"; };
                video.onpause = () => { playBtn.innerText = "▶️"; };

                playBtn.onclick = (e) => {
                    e.stopPropagation();
                    video.paused ? video.play() : video.pause();
                };

                video.ontimeupdate = () => {
                    if (video.duration) {
                        const per = (video.currentTime / video.duration) * 100;
                        seek.value = per;
                        const m = Math.floor(video.currentTime / 60);
                        const s = Math.floor(video.currentTime % 60);
                        timeText.innerText = `${m}:${s < 10 ? '0' : ''}${s}`;
                        seek.style.background = `linear-gradient(to right, #777 ${per}%, #333 ${per}%)`;
                    }
                };

                seek.oninput = (e) => {
                    e.stopPropagation();
                    if (video.duration) {
                        video.currentTime = (seek.value / 100) * video.duration;
                    }
                };

                muteBtn.onclick = (e) => {
                    e.stopPropagation();
                    video.muted = !video.muted;
                    muteBtn.innerText = video.muted ? "🔇" : "🔊";
                    if (!video.muted && video.volume === 0) video.volume = 1.0;
                };

                // ── 对比按钮：按住 = 「调色台点了重置」状态的画面（上次成片原样，无滤镜叠加），
                //    松开 = 恢复当前调色预览（改动量重新叠回；改动量为零时保持干净，不会双重调色）──
                const cmpBtn = document.createElement("button");
                cmpBtn.innerText = "👁";
                cmpBtn.title = "按住对比：显示成片原样（重置状态画面），松开恢复当前调色预览";
                cmpBtn.style = "background:none; border:none; cursor:pointer; font-size:16px; padding:0; display:flex; align-items:center; flex-shrink:0; opacity:0.85; border-radius:4px;";
                let _cmpOn = false;
                function setCompare(on) {
                    if (_cmpOn === on) return;   // pointerup 与 lostpointercapture 会连发，去重
                    _cmpOn = on;
                    cmpBtn.style.background = on ? "rgba(255,255,255,0.18)" : "none";
                    if (on) {
                        // 按住：干净成片画面——调色已烧进像素，滤镜必须全部摘掉
                        clearFilter();
                    } else {
                        // 松开：恢复当前调色预览（基线增量模型下天然安全）
                        refreshFilter();
                    }
                }
                // pointer capture：按住后拖出按钮再松开也能正确复位（比 mouseleave 可靠）
                cmpBtn.addEventListener("pointerdown", (e) => {
                    e.preventDefault(); e.stopPropagation();
                    try { cmpBtn.setPointerCapture(e.pointerId); } catch (_) {}
                    setCompare(true);
                });
                cmpBtn.addEventListener("pointerup", (e) => { e.stopPropagation(); setCompare(false); });
                cmpBtn.addEventListener("pointercancel", () => setCompare(false));
                cmpBtn.addEventListener("lostpointercapture", () => setCompare(false));

                // ── 饱和度：CSS 滤镜实时生效，零重编码 ──
                const tuneBtn = document.createElement("button");
                tuneBtn.className = "bz-tune-btn";
                tuneBtn.innerText = "🎨";
                tuneBtn.title = "调色台：饱和度 / 泛光 / 柔雾 / 暖晕（拖动实时预览，点「应用并导出」直接写入本地视频）";
                tuneBtn.style = "background:none; border:none; cursor:pointer; font-size:16px; padding:0; display:flex; align-items:center; flex-shrink:0;";

                bzInjectTuneStyle();

                // 预览参数初值 = 出厂默认：节点已不再烧入调色，成片就是母版原色
                video._bz = bzTuneDefaults();
                // 上次在调色台调过的值存在节点属性里，恢复它，别让用户白调
                if (node.properties?.bz_preview_tune) {
                    BZ_TUNE_FLAT.forEach(r => {
                        video._bz[r.k] = node.properties.bz_preview_tune[r.k] ?? video._bz[r.k];
                    });
                }
                // 兼容上一版存在节点属性里的旧字段
                Object.entries(BZ_TUNE_LEGACY).forEach(([oldKey, k]) => {
                    const v = node.properties?.[oldKey];
                    if (v != null) video._bz[k] = v;
                });
                // 「基线增量」模型的基线：当前缓存成片里烧入的参数（节点不调色时即出厂默认）。
                // 滑条=基线时改动量为零、滤镜为空，预览与成片一致；拖滑条只叠加改动量
                video._bzBase = { ...video._bz };

                // 面板挂到 body：留在节点里会被画布裁掉，也做不到贴到节点旁边
                const tunePop = document.createElement("div");
                tunePop.className = "bz-tune-panel";
                tunePop.style.display = "none";
                tunePop.innerHTML = `
                    <div class="bz-tune-head">
                        <span style="font-size:13px">🎨</span>
                        <b>调色台</b>
                        <button class="bz-tune-x" data-role="close" title="关闭">✕</button>
                    </div>
                    <div class="bz-tune-body">
                        ${BZ_TUNE_SPEC.map(g => `
                        <div class="bz-grp">
                            <div class="bz-grp-t"><i style="background:${g.c}"></i>${g.g}<u></u></div>
                            ${g.rows.map(r => `
                            <div class="bz-row"><span>${r.label}</span><b data-role="v-${r.k}" style="color:${g.c}"></b></div>
                            <input class="bz-tune-range" data-role="r-${r.k}" type="range"
                                   min="${r.min}" max="${r.max}" step="${r.step || 1}" style="--c:${g.c}">
                            `).join("")}
                        </div>`).join("")}
                    </div>
                    <div class="bz-tune-foot">
                        <button class="bz-btn" data-role="reset" title="恢复出厂默认">重置</button>
                        <button class="bz-btn main" data-role="apply" title="把当前调色直接写进本地视频，不经过 ComfyUI 运行队列">应用并导出</button>
                    </div>
                    <div class="bz-tune-msg" data-role="msg"></div>
                `;
                document.body.appendChild(tunePop);

                const tuneUI = {};
                BZ_TUNE_FLAT.forEach(r => {
                    tuneUI[r.k] = {
                        spec: r,
                        valEl: tunePop.querySelector(`[data-role="v-${r.k}"]`),
                        rangeEl: tunePop.querySelector(`[data-role="r-${r.k}"]`),
                    };
                });

                function showMsg(text, color) {
                    const m = tunePop.querySelector('[data-role="msg"]');
                    m.textContent = text;
                    m.style.color = color;
                }

                // 面板底部常显当前调色数值（= 点「应用并导出」最终会写进视频的那套值）
                function syncTuneMsg() {
                    const g = (k) => video._bz?.[k] ?? 0;
                    showMsg(`当前 饱和 ${Math.round(g("sat") * 100)}% · 泛光 ${Math.round(g("bloom") * 100)}%`
                          + ` · 柔雾 ${Math.round(g("soft") * 100)}% · 暖晕 ${Math.round(g("warm") * 100)}%`, "#8a8a92");
                }

                // 预览被改过但还没「应用并导出」时，用醒目的黄字提醒：不导出，成片不会变
                let _dirty = false;
                function markUnsaved(on = true) {
                    _dirty = on;
                    if (on) {
                        showMsg("⚠️ 预览 = 当前成片画面 + 本次改动量的近似叠加；点「应用并导出」后预览即与成片一致", "#f0c674");
                    } else {
                        syncTuneMsg();
                    }
                }

                // ── 调色预览：「基线增量」模型 ──
                // 预览源是已烧入基线参数的成片，滤镜只能叠加「滑条相对基线的改动量」：
                // 滑条=基线 → 增量为零 → 滤镜为空 → 预览与成片逐像素一致（双重调色的根治方案）；
                // 拖滑条 → 只把改动量叠上去，实时看到「这次调了多少」。只影响预览，绝不写节点控件。
                function refreshFilter() {
                    const cur = video._bz || bzTuneDefaults();
                    const base = video._bzBase;
                    const d = base ? {
                        sat: cur.sat / Math.max(0.05, base.sat || 1),   // 饱和度用比值，升/降都能近似
                        bloom: Math.max(0, cur.bloom - (base.bloom || 0)),
                        warm: Math.max(0, cur.warm - (base.warm || 0)),
                        soft: Math.max(0, cur.soft - (base.soft || 0)),
                        haze: Math.max(0, cur.haze - (base.haze || 0)),
                    } : {
                        sat: cur.sat, bloom: cur.bloom, warm: cur.warm, soft: cur.soft, haze: cur.haze,
                    };
                    const f = bzFilterCss(node.id, {
                        ...d,
                        bloomThr: cur.bloomThr, bloomRad: cur.bloomRad,
                        warmThr: cur.warmThr, warmRad: cur.warmRad,
                        softRad: cur.softRad,
                        width: video.clientWidth || video.videoWidth || 400,
                    });
                    video.style.filter = f;
                    // 只有真有滤镜内容才算「叠加改动量」状态（改动量为零时 f 是空串）
                    video._bzFilterOn = !!f;
                    const still = widget.element.querySelector("img.bz-still");
                    if (still) still.style.filter = f;
                }

                // 新成片到达时必须清滤镜：调色已经烧进视频像素里，再叠一遍就是双重调色
                // ——这正是「节点预览比播放器里的成片浓一倍」的根源。清掉后预览与成片一致，
                // 只有用户在调色台里改动参数时才重新叠滤镜做「改动量预览」。
                function clearFilter() {
                    video.style.filter = "";
                    video._bzFilterOn = false;
                    const still = widget.element.querySelector("img.bz-still");
                    if (still) still.style.filter = "";
                }
                // 换源分支在闭包外执行，把清理函数挂到 video 元素上供其调用
                video._bzClearFilter = clearFilter;

                function syncRow(k) {
                    const { spec, valEl, rangeEl } = tuneUI[k];
                    const v = video._bz[k];
                    valEl.textContent = spec.scale === 1 ? String(v) : Math.round(v * spec.scale) + spec.unit;
                    rangeEl.value = String(v * spec.scale);
                    rangeEl.style.setProperty("--p", ((v * spec.scale - spec.min) / (spec.max - spec.min) * 100).toFixed(1) + "%");
                }

                // 改任一参数：只更新预览画面 + 面板数字，绝不写节点控件
                function setTune(k, v) {
                    video._bz[k] = v;
                    syncRow(k);
                    refreshFilter();
                    markUnsaved();
                }

                BZ_TUNE_FLAT.forEach(r => {
                    tuneUI[r.k].rangeEl.addEventListener("input", (e) => {
                        e.stopPropagation();
                        setTune(r.k, parseFloat(e.target.value) / r.scale);
                    });
                });

                // ── 贴到节点旁边：DOM 小部件浮在画布上层，节点在被缩放/平移的画布里，
                // 只能按画布变换换算屏幕坐标；右边放不下就贴左边。画布一动就跟着走 ──
                let followRaf = 0;
                function placeTunePop() {
                    // 直接用节点的屏幕坐标：ComfyUI 节点 DOM 本身就在画布图层里，
                    // getBoundingClientRect 拿到的就是它最终显示的位置，不用自己拿 ds 换算
                    // （自算 scale/offset 会因为画布缩放系数、工具栏高度、滚动等对不齐，错位就是这么来的）
                    const el = widget.element;
                    if (!el || !el.isConnected) return;
                    const nr = el.getBoundingClientRect();
                    // 面板跟着画布一起缩放：画布放大节点变大，面板也同步放大，视觉比例一致
                    const sc = app.canvas?.ds?.scale || 1;
                    tunePop.style.transformOrigin = "top left";
                    tunePop.style.transform = `scale(${sc})`;
                    const pw = (tunePop.offsetWidth || 238) * sc;
                    const ph = (tunePop.offsetHeight || 320) * sc;
                    const gap = 8 * sc;
                    let x = nr.right + gap;                                   // 默认贴节点右侧
                    if (x + pw > window.innerWidth - 4) x = nr.left - pw - gap; // 右边放不下改贴左侧
                    x = Math.min(Math.max(4, x), Math.max(4, window.innerWidth - pw - 4));
                    let y = Math.min(nr.top, Math.max(4, window.innerHeight - ph - 4));
                    tunePop.style.left = Math.round(x) + "px";
                    tunePop.style.top = Math.round(Math.max(4, y)) + "px";
                }
                function startFollow() {
                    if (followRaf) return;
                    const tick = () => {
                        // 节点被删了就收摊：停循环、摘监听、移除面板，避免残留
                        if (!app.graph.getNodeById(node.id)) {
                            followRaf = 0;
                            tunePop._bzCleanup?.();
                            return;
                        }
                        if (tunePop.style.display === "none") {
                            followRaf = 0;
                            return;
                        }
                        placeTunePop();
                        followRaf = requestAnimationFrame(tick);
                    };
                    followRaf = requestAnimationFrame(tick);
                }
                const onDocDown = (e) => {
                    if (tunePop.style.display !== "block") return;
                    if (tunePop.contains(e.target) || tuneBtn.contains(e.target)) return;
                    tunePop.style.display = "none";
                };
                document.addEventListener("mousedown", onDocDown, true);
                tunePop._bzCleanup = () => {
                    document.removeEventListener("mousedown", onDocDown, true);
                    tunePop.remove();
                };

                tuneBtn.onclick = (e) => {
                    e.stopPropagation();
                    if (tunePop.style.display === "block") {
                        tunePop.style.display = "none";
                        return;
                    }
                    if (_dirty) markUnsaved(true); else syncTuneMsg();
                    tunePop.style.display = "block";
                    placeTunePop();
                    startFollow();
                };
                tunePop.querySelector('[data-role="close"]').onclick = (e) => {
                    e.stopPropagation();
                    tunePop.style.display = "none";
                };

                tunePop.querySelector('[data-role="reset"]').addEventListener("click", (e) => {
                    e.stopPropagation();
                    // 重置 = 全参数回出厂默认（滑条归零，预览滤镜清空）
                    video._bz = bzTuneDefaults();
                    BZ_TUNE_FLAT.forEach(r => syncRow(r.k));
                    refreshFilter();
                    markUnsaved(false);
                    showMsg("已恢复出厂默认", "#8a8a92");
                });

                // 「应用并导出」：把当前调色直接烘进本地源视频。
                // 全程不进 ComfyUI 执行队列——不重跑上游节点、不写入任何节点控件，
                // 因此反复调色导出既快，也不会把参数焊死在节点上。
                tunePop.querySelector('[data-role="apply"]').addEventListener("click", async (e) => {
                    e.stopPropagation();
                    const btn = tunePop.querySelector('[data-role="apply"]');
                    if (btn.disabled) return;

                    // 数据源用母版路径（成片在磁盘上的位置）；兜底从预览地址里抠 path 参数
                    const master = video._bzMaster || (() => {
                        const m = (video.src || "").match(/[?&]path=([^&]+)/);
                        return m ? decodeURIComponent(m[1]) : "";
                    })();
                    if (!master) {
                        showMsg("⚠️ 找不到源视频文件：请先运行一次工作流生成成片，再回来调色", "#f0a0a0");
                        return;
                    }

                    // 面板参数名 → 后端调色键名，后端按 TUNE_DEFAULTS 的键取值
                    const tune = {};
                    BZ_TUNE_FLAT.forEach(r => { tune[r.w] = video._bz[r.k]; });

                    const 原按钮文字 = btn.textContent;
                    btn.disabled = true;
                    btn.textContent = "导出中…";
                    showMsg("正在把调色写入视频，请稍候…", "#f0c674");

                    // 进度订阅：后端广播 → _bakeWatch → 按钮文字
                    _bakeWatch = { src: master, done: 0, total: 0 };
                    const 进度计时器 = setInterval(() => {
                        if (!_bakeWatch) return;
                        const { done, total } = _bakeWatch;
                        const pct = total > 0 ? Math.min(99, Math.round(done / total * 100)) : 0;
                        btn.textContent = pct > 0 ? `导出中 ${pct}%` : "导出中…";
                    }, 200);
                    try {
                        const resp = await api.fetchApi("/banzhang_api/bake_video", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ src: master, tune: tune }),
                        });
                        // 先把响应文本读出来，能定位问题：404 时 ComfyUI 返回的是 HTML，
                        // resp.json() 会抛难懂的 JSON 解析错误
                        const text = await resp.text();
                        let r;
                        try {
                            r = JSON.parse(text);
                        } catch (e) {
                            showMsg(`❌ 接口未就绪（HTTP ${resp.status}）。请完全退出 ComfyUI 再重启，新接口才生效`, "#f0a0a0");
                            return;
                        }
                        if (!r.success) {
                            showMsg("❌ " + (r.msg || "导出失败"), "#f0a0a0");
                            return;
                        }
                        // 新成片已把调色烧进像素：基线归位到当前滑条值，改动量清零，预览与成片一致
                        video._bzBase = { ...video._bz };
                        if (typeof video._bzClearFilter === "function") video._bzClearFilter();
                        if (r.fps) node._painter_fps = r.fps;
                        if (r.frame_count) node._painter_frames = r.frame_count;
                        video.src = api.apiURL(`/banzhang_api/video_file?path=${encodeURIComponent(r.path)}&t=${Date.now()}`);
                        video.play().catch(() => {});
                        _dirty = false;
                        showMsg("✅ 已导出到源文件旁：" + r.filename, "#8ad4a8");
                        node.setDirtyCanvas(true, true);
                    } catch (err) {
                        showMsg("❌ 导出请求失败：" + err, "#f0a0a0");
                    } finally {
                        clearInterval(进度计时器);
                        _bakeWatch = null;
                        btn.disabled = false;
                        btn.textContent = 原按钮文字;
                    }
                });

                // 恢复上次调过的值（节点属性随工作流保存）
                BZ_TUNE_FLAT.forEach(r => syncRow(r.k));
                refreshFilter();

                video.onloadedmetadata = () => {
                    // 节点尺寸不随视频变：视频已用 object-fit:contain 自适应预览区，
                    // 此处只负责在「叠加改动量」状态下按真实宽度重算滤镜（模糊半径与预览宽度成比例）；
                    // 干净状态（改动量为零）不自动叠滤镜，避免双重调色
                    if (video._bzFilterOn) refreshFilter();
                };

                // 折叠按钮：仅画面模式开关（悬浮在视频右上角，状态持久化）
                const foldBtn = document.createElement("button");
                foldBtn.className = "bz-fold-btn";
                foldBtn.textContent = node._bz_video_only ? "⌄" : "⌃";
                foldBtn.title = node._bz_video_only ? "展开参数与详情" : "折叠参数，仅显示画面";
                foldBtn.style = "position:absolute; top:8px; right:10px; z-index:20; background:rgba(0,0,0,0.45); border:1px solid rgba(255,255,255,0.18); border-radius:4px; color:#ccc; cursor:pointer; font-size:13px; line-height:1; padding:4px 8px;";
                foldBtn.onclick = (e) => {
                    e.stopPropagation();
                    applyVideoOnlyMode(node, !node._bz_video_only);
                };

                bar.append(playBtn, seek, timeText, muteBtn, cmpBtn, tuneBtn);
                container.append(video, bar, foldBtn);

                // 预览区刚诞生：节点还没为它留位置，重排一次让它长到能装下画面。
                // 刷新恢复路径上 refit 会被 _bzConfiguring 挡掉 → 存档尺寸原样保留。
                refit(node);
            }

            const v = widget.element.querySelector("video");
            const bar = widget.element.querySelector(".bz-controls-bar");
            // 帧率与帧数取后端真值，同步预览按帧号寻址全靠它
            if (data.fps) node._painter_fps = data.fps;
            if (data.frame_count) node._painter_frames = data.frame_count;
            // 自定义路径保存时 output 无文件，走后端接口直接读取；否则走 /view
            const src = data.abs_path
                ? api.apiURL(`/banzhang_api/video_file?path=${encodeURIComponent(data.abs_path)}&t=${Date.now()}`)
                : api.apiURL(`/view?filename=${data.filename}&subfolder=${data.subfolder}&type=${data.type}&t=${Date.now()}`);

            // 图片模式：本次产出是 PNG/JPG，<video> 放不出来，改为渲染 <img>
            const isImage = /\.(png|jpe?g|webp)$/i.test(data.filename || "");
            let img = widget.element.querySelector("img.bz-still");
            // 新画面到达：成片不再由节点烧入调色，基线即出厂默认。
            // 此后滑条只要等于基线，改动量就是零、滤镜为空，预览与成片逐像素一致；
            // 拖动滑条时只叠加「改动量」，从根上杜绝「成片+完整滤镜」的双重调色
            {
                if (v) v._bzBase = bzTuneDefaults();
                // 母版路径 = 这份成片在磁盘上的真实位置。后续每次「应用并导出」都基于它重算，
                // 因此反复调色不会叠加上一次的结果（非破坏性，调多少次都回得去）。
                // 注意：导出后不更新它——母版始终是节点产出的那一份。
                if (v && data.src_path) v._bzMaster = data.src_path;
            }
            if (isImage) {
                if (!img) {
                    img = document.createElement("img");
                    img.className = "bz-still";
                    img.style = "width:100%; flex:1 1 auto; min-height:0; display:block; object-fit:contain; background:#000;";
                    widget.element.insertBefore(img, widget.element.firstChild);
                }
                img.style.display = "block";
                // 恢复时延迟加载图片（错峰，避免占连接），渲染完成立即
                const loadStill = () => { img.src = src; };
                if (deferred) queueRestore(loadStill); else loadStill();
                // 图片模式：产出的图已烧入调色，预览不能叠滤镜（与视频分支同理）
                img.style.filter = "";
                if (v) v._bzFilterOn = false;
                if (v) v.style.display = "none";
                if (bar) bar.style.display = "none";
            } else {
                if (img) img.style.display = "none";
                // 恢复必须写回明确值（video=block / bar=flex），清空会把原内联样式一并抹掉，
                // 控制条会退化成 block 导致按钮竖着堆叠
                if (v) v.style.display = "block";
                if (bar) bar.style.display = "flex";
                // 清滤镜（不依赖 src，先做）：新成片已把调色烧进像素，预览绝不能再叠
                // 一遍滤镜，否则预览=成片+滤镜（双重调色）、播放器里看成片永远比节点
                // 预览淡一半。只有在调色台里改动参数时才叠加滤镜做「改动量预览」。
                if (typeof v._bzClearFilter === "function") v._bzClearFilter();
                else { v.style.filter = ""; v._bzFilterOn = false; }
                // 恢复时延迟加载视频（错峰，避免占连接），渲染完成立即
                const loadVideo = () => {
                    v.src = src;
                    v.play().catch(e => console.log("等待交互以开始播放..."));
                };
                if (deferred) queueRestore(loadVideo); else loadVideo();
            }

            node.setDirtyCanvas(true, true);
        }
    }
});
