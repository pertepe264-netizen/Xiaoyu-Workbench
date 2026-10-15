// ═══════════════════════════════════════════════════════════
//  🎧 H3 采样中音频试听条
//
//  关键点：不新建 DOM widget，而是插进「minimax_h3_preview.js」已经建好的
//  预览面板（.minimax-h3-pov-panel）里，位置在表头与图表之间。
//
//  为什么必须这样做：预览面板的节点总高度是 onResize 按 16:9 反推的
//  （见 minimax_h3_preview.js 的 upperHeight / onResize）。再挂一个
//  addDOMWidget 会被算进 upperHeight，两个控件的高度推导互相打架 → 面板错位。
//  嵌进既有面板 = 零额外高度，不参与那套推导，也就不会错位。
//
//  另一条硬规则：永不自动播放，必须用户点 ▶。
// ═══════════════════════════════════════════════════════════

import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE = "MiniMaxH3PreviewOverride";

const STYLE_ID = "banzhang-h3-audio-preview-css";
function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = `
.bzap-row{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:3px 8px 4px;
  border-bottom:1px solid #2a2a2a;background:#1c1c1c}
.bzap-btn{flex:0 0 auto;min-width:26px;height:19px;padding:0 6px;font:11px monospace;
  line-height:1;border:1px solid #3a3a3a;border-radius:4px;background:#262626;color:#ccc;
  cursor:pointer}
.bzap-btn:disabled{opacity:.4;cursor:default}
.bzap-btn:not(:disabled):hover{background:#333;border-color:#555}
.bzap-txt{flex:1 1 auto;min-width:0;font:10px monospace;color:#8a8a8a;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bzap-txt.live{color:#9fd6c8}
`;
    document.head.appendChild(s);
}

// 找到由 minimax_h3_preview.js 创建的预览面板容器
function findPanel(node) {
    const w = (node.widgets || []).find((x) => x && x.name === "h3_preview");
    const host = w && (w.element || w.dom);
    if (!host || !host.querySelector) return null;
    return host.querySelector(".minimax-h3-pov-panel") || null;
}

function ensureBar(node) {
    if (node._bzapBar) return node._bzapBar;
    const panel = findPanel(node);
    if (!panel) return null;

    const row = document.createElement("div");
    row.className = "bzap-row";

    const btn = document.createElement("button");
    btn.className = "bzap-btn";
    btn.textContent = "▶";
    btn.disabled = true;
    btn.title = "试听当前步生成的音频（手动点播，不自动播放）";

    const txt = document.createElement("span");
    txt.className = "bzap-txt";
    txt.textContent = "音频预览未开启（需打开开关并接音频VAE）";

    row.appendChild(btn);
    row.appendChild(txt);

    // 插在表头与图表之间：row 是 flex:0 0 auto，graphs 仍是 flex:1，
    // 面板总高度不变，所以不会挤动下面的图表。
    const graphs = panel.querySelector(".minimax-h3-pov-graphs");
    if (graphs) panel.insertBefore(row, graphs);
    else panel.appendChild(row);

    const audio = document.createElement("audio");
    audio.preload = "none";
    audio.style.display = "none";
    row.appendChild(audio);

    // ▶ 播 / ■ 停：同一个按钮来回切。播放结束、被打断、出错都要把按钮复位，
    // 否则会停在 ■ 上变成一个按不动的死按钮。
    const bar = { audio, btn, txt, playing: false };

    function setPlaying(on) {
        bar.playing = !!on;
        btn.textContent = bar.playing ? "■" : "▶";
        btn.title = bar.playing
            ? "停止"
            : "试听当前步生成的音频（手动点播，不自动播放）";
    }

    function stop() {
        try {
            audio.pause();
            audio.currentTime = 0;
        } catch (e) { /* 忽略 */ }
        setPlaying(false);
    }
    bar.stop = stop;

    btn.addEventListener("click", () => {
        if (!audio.src) return;
        if (bar.playing) { stop(); return; }
        try {
            audio.currentTime = 0;
            const p = audio.play();          // 返回 Promise，异步失败也要复位
            if (p && p.catch) p.catch(() => setPlaying(false));
            setPlaying(true);
        } catch (e) {
            setPlaying(false);
        }
    });

    audio.addEventListener("ended", () => setPlaying(false));
    audio.addEventListener("pause", () => setPlaying(false));
    audio.addEventListener("error", () => setPlaying(false));

    node._bzapBar = bar;
    return bar;
}

// 预览面板是另一个扩展建的，挂载时机不确定 → 用 rAF 等几帧
function attachWhenReady(node, tries = 40) {
    const bar = ensureBar(node);
    if (bar || tries <= 0) return;
    requestAnimationFrame(() => attachWhenReady(node, tries - 1));
}

app.registerExtension({
    name: "banzhang.h3.audio.preview",

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE) return;

        const created = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = created?.apply(this, arguments);
            try {
                ensureStyles();
                attachWhenReady(this);
            } catch (e) {
                console.warn("[班长 H3 音频预览] 挂载失败", e);
            }
            return r;
        };

        const configured = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = configured?.apply(this, arguments);
            try { attachWhenReady(this); } catch (e) { /* 忽略 */ }
            return r;
        };
    },

    setup() {
        api.addEventListener("banzhang_h3_audio_preview", (event) => {
            const d = event.detail || {};
            if (!d.wav || d.node_id == null) return;
            let node = app.graph?.getNodeById(String(d.node_id));
            if (!node && /^\d+$/.test(String(d.node_id))) {
                node = app.graph?.getNodeById(Number(d.node_id));
            }
            if (!node) return;
            const bar = node._bzapBar || ensureBar(node);
            if (!bar) return;
            // 新一步的音频到了：换内容。正在播就先停掉（绝不自动播新的），
            // 按钮随之复位成 ▶，用户想听再点。
            if (bar.playing) bar.stop();
            bar.audio.src = "data:audio/wav;base64," + d.wav;
            bar.btn.disabled = false;
            bar.txt.classList.add("live");
            const total = d.total ? ` / ${d.total}` : "";
            const len = d.seconds ? ` · ${d.seconds}s` : "";
            bar.txt.textContent = `第 ${d.step}${total} 步${len} · 点 ▶ 试听`;
        });
    },
});
