// 👨‍🏫 小语的工作台 - 顶栏「显存 / 内存」监控（精简胶囊版）
// 设计目标：占地小、颜色直观、悬停看细节、断线不装死
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const EVENT_NAME = "vram_reserved.monitor";
const STALE_MS = 10000;      // 超过 10 秒没数据 = 视为离线
const DEFAULT_WARN = 85;     // 红色告警阈值（%），可在 ComfyUI 设置里改

const GB = 1024 * 1024 * 1024;
let warnThreshold = DEFAULT_WARN;
let lastData = null;
let lastUpdate = 0;
let root = null;

const CSS = `
.bz-mon {
  display:inline-flex; flex-direction:column; justify-content:center; gap:3px;
  height:auto; min-height:26px; padding:4px 9px; margin:0 4px;
  border-radius:8px;
  background:rgba(22,22,26,.88);
  border:1px solid rgba(255,255,255,.08);
  box-shadow:0 1px 4px rgba(0,0,0,.35);
  cursor:pointer; user-select:none; flex-shrink:0;
  transition:border-color .2s ease, opacity .25s ease;
}
.bz-mon:hover { border-color:rgba(255,215,0,.5); }
.bz-mon-item { display:flex; align-items:center; gap:5px; height:11px; }
.bz-mon-label {
  font-size:10px; font-weight:600; letter-spacing:.4px;
  color:#FFD700; opacity:.85;
}
.bz-mon-bar {
  position:relative; width:28px; height:4px;
  border-radius:999px; background:rgba(255,255,255,.12);
  overflow:hidden;
}
.bz-mon-fill {
  display:block; width:0; height:100%; border-radius:999px;
  background:linear-gradient(90deg,#FFD700,#FFA000);
  box-shadow:0 0 6px rgba(255,190,0,.45);
  transition:width .6s ease, background .3s ease;
}
.bz-mon-fill.warn {
  background:linear-gradient(90deg,#FF6B6B,#FF1744);
  box-shadow:0 0 7px rgba(255,40,60,.55);
  animation:bz-mon-pulse 1.4s ease-in-out infinite;
}
@keyframes bz-mon-pulse { 0%,100%{opacity:1} 50%{opacity:.5} }
.bz-mon-val {
  font:10px/1 ui-monospace,Consolas,"Courier New",monospace;
  color:#E8E8E8; min-width:44px; white-space:nowrap;
}
.bz-mon.offline { opacity:.45; }
.bz-mon.offline .bz-mon-fill {
  background:#666; box-shadow:none; animation:none; width:100% !important;
}
.bz-mon.compact { padding:4px 6px; }
.bz-mon.compact .bz-mon-label,
.bz-mon.compact .bz-mon-val { display:none; }
`;

function injectCSS() {
    if (document.getElementById("bz-mon-style")) return;
    const s = document.createElement("style");
    s.id = "bz-mon-style";
    s.textContent = CSS;
    document.head.appendChild(s);
}

function buildItem(label) {
    const el = document.createElement("div");
    el.className = "bz-mon-item";
    el.innerHTML =
        `<span class="bz-mon-label">${label}</span>` +
        `<span class="bz-mon-bar"><i class="bz-mon-fill"></i></span>` +
        `<span class="bz-mon-val">--</span>`;
    return {
        el,
        fill: el.querySelector(".bz-mon-fill"),
        val: el.querySelector(".bz-mon-val"),
    };
}

const vram = buildItem("显存");
const ram = buildItem("内存");

const pctOf = (used, total) => (total > 0 ? ((used / total) * 100).toFixed(0) + "%" : "--");

function setItem(item, used, total) {
    const ok = total > 0;
    const pct = ok ? Math.min(100, (used / total) * 100) : 0;
    item.fill.style.width = pct.toFixed(1) + "%";
    item.fill.classList.toggle("warn", ok && pct >= warnThreshold);
    item.val.textContent = ok
        ? `${(used / GB).toFixed(1)}/${(total / GB).toFixed(0)}G`
        : "--/--";
}

function updateTooltip(offline) {
    if (!root) return;
    if (offline) {
        root.title = "⚠️ 已超过 10 秒没收到服务器数据，显示的可能不是实时值。\n（点击可折叠/展开）";
        return;
    }
    if (!lastData) {
        root.title = "显存 / 内存监控：等待服务器数据…\n（点击可折叠/展开）";
        return;
    }
    const d = lastData;
    const t = new Date(lastUpdate).toLocaleTimeString("zh-CN", { hour12: false });
    root.title =
        `显存  ${(d.vram_used / GB).toFixed(1)} / ${(d.vram_total / GB).toFixed(1)} GB  (${pctOf(d.vram_used, d.vram_total)})\n` +
        `内存  ${(d.ram_used / GB).toFixed(1)} / ${(d.ram_total / GB).toFixed(1)} GB  (${pctOf(d.ram_used, d.ram_total)})\n` +
        `更新于 ${t}\n` +
        `（点击可折叠/展开；变红阈值可在设置里调）`;
}

function markOffline() {
    if (!root || root.classList.contains("offline")) return;
    root.classList.add("offline");
    vram.val.textContent = "离线";
    ram.val.textContent = "离线";
    updateTooltip(true);
}

function apply(data) {
    lastData = data;
    lastUpdate = Date.now();
    if (!root) return;
    root.classList.remove("offline");
    setItem(vram, data.vram_used || 0, data.vram_total || 0);
    setItem(ram, data.ram_used || 0, data.ram_total || 0);
    updateTooltip(false);
}

function mount() {
    const tryMount = () => {
        const target = app.menu?.settingsGroup?.element;
        if (target) { target.before(root); return true; }
        const queueBtn = document.getElementById("queue-button");
        if (queueBtn) { queueBtn.insertAdjacentElement("afterend", root); return true; }
        return false;
    };
    if (!tryMount()) {
        const iv = setInterval(() => { if (tryMount()) clearInterval(iv); }, 500);
    }
}

app.registerExtension({
    name: "BanZhang.VRAM.Monitor",

    setup() {
        injectCSS();

        root = document.createElement("div");
        root.className = "bz-mon";
        root.appendChild(vram.el);
        root.appendChild(ram.el);
        root.addEventListener("click", () => root.classList.toggle("compact"));
        updateTooltip(false);
        mount();

        api.addEventListener(EVENT_NAME, (e) => {
            if (e?.detail) apply(e.detail);
        });

        // 断线检测：超过 10 秒没更新就变灰标注「离线」
        setInterval(() => {
            if (lastUpdate && Date.now() - lastUpdate > STALE_MS) markOffline();
        }, 1000);

        // 设置项（注册失败也不影响监控本身）
        try {
            app.ui.settings.addSetting({
                id: "BanZhang.VRAMMonitor.WarnThreshold",
                name: "显存监控：变红告警阈值 (%)",
                type: "number",
                defaultValue: DEFAULT_WARN,
                onChange: (v) => {
                    const n = Number(v);
                    if (!Number.isNaN(n) && n > 0) {
                        warnThreshold = n;
                        if (lastData) apply(lastData);
                    }
                },
            });
        } catch (e) {
            console.warn("[班长] 阈值设置项注册失败(不影响使用)", e);
        }

        try {
            app.ui.settings.addSetting({
                id: "BanZhang.VRAMMonitor.Enabled",
                name: "显存监控：显示顶栏状态条",
                type: "boolean",
                defaultValue: true,
                onChange: (v) => {
                    if (root) root.style.display = v ? "inline-flex" : "none";
                },
            });
        } catch (e) {
            console.warn("[班长] 显示开关设置项注册失败(不影响使用)", e);
        }
    },
});
