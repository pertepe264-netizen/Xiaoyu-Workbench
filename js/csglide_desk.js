/* ============================================================
 * ✍️ H3 文案台 面板
 * ------------------------------------------------------------
 * 提示词从导演台里搬出来，单独成一个节点。三块内容：
 *
 *   顶部：@标签 chips   —— 媒体架里有什么素材，点一下就插进光标处
 *   中部：片子级 / 本段 —— 片子级是全片统一的基调，本段是这一段发生的事
 *   底部：预设          —— 常用的几句，点一下追加
 *
 * 为什么严格按这个顺序分两块：片子级在前、本段在后。反过来写会让每段开头
 * 都在重复世界观，模型的注意力被固定前缀拉走，段与段的差别被稀释。
 *
 * 段身份从导演台节点直接读（同一个 gcast_project 快照），不从后端反查——
 * 文案台在图上排在导演台前面执行，那时档案里的 current 还是上一段。
 * ============================================================ */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { rackSlotsOf, rackNodeOf, castProject, currentOf, currentClipOf,
         onCastChange, onRackChange, BZ_BAG_VERSION } from "./bz_rack.js";
import { bzStore } from "./bz_store.js";
import { saveDesk } from "./bz_project_sync.js";
import { projectStore } from "./bz_project_store.js";
import { CUR_SEG, CUR_SEG_DIM } from "./csglide_film.js";
/* 提示词检查规则。词在这边写，检查就在这边跑 —— 搬到导演台去只会让人看到
   警告却改不了词。规则只有一份，三个节点共用。 */
import { promptCheck } from "./csglide_prompt_check.js";
/* 通用提示词编辑器：高亮 / @补全 / 悬停预览都在它里面，文案台不再自己在
   textarea 上叠高亮层、手写几何命中（那套没有 @ 补全，且容易与文字层错位）。 */
import { makePromptEditor } from "./csglide_prompt_editor.js";
/* 编辑器样式（.gcast-ed / .tok / .gcast-ac / .gcast-toktip）与导演台同源，
   只从纯样式文件取字符串，不拖入导演台主逻辑，避免副作用与循环依赖。 */
import { CSS as CAST_CSS } from "./csglide_cast.css.js";
/* 引用归一化 / 别名展开 / "实际会发送哪些参考"的唯一权威纯函数（与后端
   csglide_cast.normalize_refs / expand_aliases 对称）。chip 亮灭读它，保证
   文案台点亮 == 后端真发。 */
import { effectiveTags } from "./csglide_ref_text.js";

const NODE_ID = "CSGlideDeskCS";
/* 与媒体架同一套缩放口径：floor 是报给框架的最小高度（不是内容高），
   节点实际高度靠 setSize + fit 给一个能装下内容的起点。 */
const MIN_W = 460, MIN_H = 520, MIN_FLOOR_H = 120;

const CSS_ID = "bz-desk-css";
const CSS = `
.bzdesk {
  --bz-bg:#191919; --bz-panel:#212121; --bz-raise:#2b2b2b; --bz-line:#3b3b3b;
  --bz-well:#131313;
  --bz-txt:#e3e3e3; --bz-dim:#979797;
  --bz-accent:#58d1ff; --bz-accent-dim:#58d1ff26;
  /* 「当前段」的规范色从成片监视器导入，不在本文件另抄一份 —— 三个节点必须
     用同一个值，手抄副本迟早会漂。 */
  --bz-violet:${CUR_SEG}; --bz-violet-dim:${CUR_SEG_DIM};
  font-family: ui-sans-serif, system-ui, "Segoe UI", sans-serif;
  color: var(--bz-txt); font-size: 12px; line-height: 1.35;
  pointer-events: auto; position: relative; width: 100%; min-width: 0;
  background: var(--bz-bg); border: 1px solid var(--bz-line); border-radius: 10px;
  padding: 8px; box-sizing: border-box;
  display: flex; flex-direction: column; gap: 7px;
  user-select: none; -webkit-user-select: none;
  /* 填满节点主体、内容超出时在面板内滚动 —— 与媒体架同一机制：节点拖短
     不会把内容挤到框外，而是这里出滚动条。 */
  height: 100%; overflow: auto; overflow-x: hidden;
}
.bzdesk * { box-sizing: border-box; }

.bzdesk-bar {
  display: flex; align-items: center; gap: 8px; min-width: 0; flex: none;
  font-size: 11.5px; color: var(--bz-dim); white-space: nowrap; overflow: hidden;
}
.bzdesk-bar .d-title { color: var(--bz-txt); font-weight: 500; flex: none; }
/* 当前段指示 —— 与时间轴上那一段的紫环、媒体架上那颗胶囊是**同一个信号**的
   三种画法：同一个规范色，同一个"锁定加发光"的加强方式。 */
.bzdesk-bar .d-segchip {
  padding: 1px 7px; border-radius: 4px; background: var(--bz-violet-dim);
  border: 1px solid var(--bz-violet); color: var(--bz-violet); font-size: 11px;
  overflow: hidden; text-overflow: ellipsis; flex: none;
}
.bzdesk-bar .d-segchip.locked {
  box-shadow: 0 0 7px 1px var(--bz-violet-dim);
  border-width: 2px; padding: 0 6px;
}
.bzdesk-bar .d-sp { flex: 1 1 auto; min-width: 4px; }
.bzdesk-bar .d-btn {
  all: unset; cursor: pointer; flex: none; font-family: inherit;
  padding: 2px 8px; border-radius: 4px; background: var(--bz-raise);
  border: 1px solid var(--bz-line); font-size: 11px; color: var(--bz-txt);
}
.bzdesk-bar .d-btn:hover { border-color: var(--bz-accent); color: var(--bz-accent); }
.bzdesk-bar .d-btn:disabled { opacity: .4; cursor: default; }

/* ---- @标签 chips ---- */
.bzdesk-chips {
  display: flex; flex-wrap: wrap; gap: 5px; flex: none;
  padding: 5px; background: var(--bz-well);
  border: 1px solid var(--bz-line); border-radius: 6px;
  max-height: 170px; overflow: auto;
}
/* 逐条照搬导演台 .gcast-chip：圆形 18px 缩略图、等宽 10.5px、别名为主 /
   规范 token 缩小作副标；.on = 提示词里提到了 = 真的会送进参考。 */
.bzdesk-chip {
  all: unset; cursor: pointer;
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 9px 2px 2px; border-radius: 20px;
  background: var(--bz-raise); border: 1px solid var(--bz-line);
  font-family: ui-monospace, Consolas, monospace; font-size: 10.5px;
  color: var(--bz-txt); max-width: 190px; transition: .13s;
}
.bzdesk-chip:hover { border-color: var(--bz-accent); }
.bzdesk-chip.on { border-color: var(--bz-accent); background: var(--bz-accent-dim); }
.bzdesk-chip.on .lbl, .bzdesk-chip.on .sub { color: var(--bz-accent); }
.bzdesk-chip .sub { opacity: .5; font-size: 9.5px; letter-spacing: .2px; }
.bzdesk-chip img, .bzdesk-chip video {
  width: 18px; height: 18px; border-radius: 50%; object-fit: cover;
  background: #0d0d0d; flex: none;
}
.bzdesk-chip .glyph {
  width: 18px; height: 18px; border-radius: 50%; flex: none;
  display: grid; place-items: center; background: var(--bz-bg);
  font-size: 9px; color: var(--bz-accent);
}
.bzdesk-chip .lbl { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bzdesk-empty { color: var(--bz-dim); font-size: 11px; padding: 2px 4px; }

/* ---- 编辑区 ---- */
.bzdesk-sec { display: flex; flex-direction: column; gap: 3px; flex: none; }
.bzdesk-sec .d-lab {
  font-size: 9.5px; letter-spacing: .13em; text-transform: uppercase;
  color: var(--bz-dim); display: flex; align-items: center; gap: 6px;
}
.bzdesk-sec .d-lab .hint { text-transform: none; letter-spacing: 0; opacity: .7; }
/* 提示词检查。每条一行，只报不拦 —— 它不是校验门，是"写的时候顺手看一眼"。
   告警用琥珀（有事没做），不用红色：这些都不是错误，不会让渲染失败。 */
.bzdesk-sec .d-check {
  display: flex; flex-direction: column; gap: 3px;
  background: var(--bz-well); border: 1px solid var(--bz-line);
  border-radius: 7px; padding: 5px 7px;
}
.bzdesk-sec .d-crow { display: flex; align-items: baseline; gap: 7px; font-size: 11px; }
.bzdesk-sec .d-crow b { color: var(--bz-accent); font-weight: 500; min-width: 34px;
  flex: 0 0 auto; }
.bzdesk-sec .d-crow.warn span { color: #e0a34a; }
.bzdesk-sec .d-crow.ok span { color: var(--bz-dim); }
/* ---- 提示词编辑器（makePromptEditor，自带 @ 补全 / 高亮 / 悬停预览） ----
   编辑器节点必须是 .gcast-pwrap > .gcast-ed（基础样式在导演台 CAST_CSS），
   这里只把外观对齐文案台：深井底色、青色主色、圆角；高度片子级矮、本段吃满。
   不再有 textarea 透明文字 + 高亮层的双层结构（那套没有 @ 补全、还会错位）。 */
.bzdesk-sec .gcast-pwrap {
  width: 100%; box-sizing: border-box;
  background: var(--bz-well); border: 1px solid var(--bz-line); border-radius: 6px;
}
.bzdesk-sec .gcast-pwrap:focus-within { border-color: var(--bz-accent); }
.bzdesk-sec .gcast-pwrap > .gcast-ed {
  width: 100%; box-sizing: border-box;
  background: transparent; border: none; border-radius: 6px;
  padding: 6px 7px; font-family: inherit; font-size: 12px; line-height: 1.5;
  /* 把编辑器内部用的 --h3-* 变量重指到文案台主题，高亮/占位符/主色就与
     本面板一致，不必在 CAST_CSS 里另抄一份颜色。 */
  --h3-bg: var(--bz-well); --h3-txt: var(--bz-txt); --h3-dim: var(--bz-dim);
  --h3-accent: var(--bz-accent); --h3-accent-dim: var(--bz-accent-dim);
  --h3-line: var(--bz-line);
}
/* 片子级：固定矮框，内容多了在框内滚动 */
.bzdesk-sec.global .gcast-pwrap > .gcast-ed { min-height: 46px; max-height: 140px; }
/* 本段吃满剩余高度：节点拖高就多显示几行，不必手动拉输入框 */
.bzdesk-sec.d-segsec { flex: 1 1 auto; min-height: 0; }
.bzdesk-sec.d-segsec .gcast-pwrap { flex: 1 1 auto; min-height: 120px; display: flex; }
.bzdesk-sec.d-segsec .gcast-pwrap > .gcast-ed {
  flex: 1 1 auto; min-height: 120px; max-height: none;
}

/* ---- 预设 ---- */
.bzdesk-presets { display: flex; flex-wrap: wrap; gap: 5px; flex: none; }
.bzdesk-preset {
  all: unset; cursor: pointer; font-family: inherit; font-size: 11px;
  display: inline-flex; align-items: center; gap: 2px;
  padding: 2px 3px 2px 8px; border-radius: 999px; background: var(--bz-raise);
  border: 1px solid var(--bz-line); color: var(--bz-txt); max-width: 240px;
}
.bzdesk-preset .lbl { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.bzdesk-preset:hover { border-color: var(--bz-violet); color: var(--bz-violet); }
/* ✕ 必须有实心可点区域：裸 span 小字点不中，点偏就落在胶囊上变成"追加" */
.bzdesk-preset .x {
  flex: none; cursor: pointer; padding: 0 6px; border-radius: 999px;
  color: var(--bz-dim);
}
.bzdesk-preset .x:hover { color: #f09595; }
`;

function injectCSS() {
  /* 编辑器基础样式（导演台 / qwen 也注入同一份，id 去重，全局只一份）。
     先于 desk CSS 注入：下面的 desk 覆盖规则才能压过它。 */
  if (!document.getElementById("gcast-css")) {
    const base = document.createElement("style");
    base.id = "gcast-css";
    base.textContent = CAST_CSS;
    document.head.appendChild(base);
  }
  if (document.getElementById(CSS_ID)) return;
  const s = document.createElement("style");
  s.id = CSS_ID;
  s.textContent = CSS;
  document.head.appendChild(s);
}

const DEFAULT_PRESETS = [
  "镜头缓慢推进，主体保持在画面中央",
  "手持感轻微晃动，环境光偏冷",
  "固定机位，人物从画面左侧入画",
];

function blankState() {
  return {
    chain: { name: "", clip: "", shot: "" },
    global: "",            // 片子级
    segs: {},              // { 段身份: 本段提示词 }
    alias: false,          // 别名模式：名字直接当引用（关 = 只认 @图片N 写法）
    presets: DEFAULT_PRESETS.slice(),
  };
}

/** 素材文件 → 预览地址。与导演台/媒体架同一套 /view 口径：文件名可能带
 *  子目录（媒体架上传落在 input/cglide/），必须拆出来填 subfolder，否则 404。 */
function viewURL(file) {
  const f = String(file || "");
  if (!f) return "";
  const i = f.lastIndexOf("/");
  const sub = i >= 0 ? f.slice(0, i) : "";
  const name = i >= 0 ? f.slice(i + 1) : f;
  return api.apiURL(`/view?filename=${encodeURIComponent(name)}&subfolder=${encodeURIComponent(sub)}&type=input`);
}

app.registerExtension({
  name: "cglide.glidedesk",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_ID) return;
    injectCSS();

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      const node = this;

      let st = blankState();
      let dataWidget = node.widgets?.find((w) => w.name === "desk_data");
      if (dataWidget) {
        try { st = Object.assign(blankState(), JSON.parse(dataWidget.value || "{}")); }
        catch { st = blankState(); }
        /* 同导演台：h3_data / desk_data 只作值载体，不摆在画布上 */
        dataWidget.hidden = true;
        dataWidget.options = Object.assign({}, dataWidget.options, { hidden: true });
        dataWidget.computeSize = () => [0, -4];
        dataWidget.computeLayoutSize = () => ({ minHeight: 0, maxHeight: 0, minWidth: 0 });
        dataWidget.label = "";
        dataWidget.tooltip = "";
        dataWidget.serializeValue = dataWidget.serializeValue || (() => dataWidget.value);
        if (dataWidget.element) {
          Object.assign(dataWidget.element.style, {
            display: "none", visibility: "hidden", pointerEvents: "none",
            position: "absolute", left: "-99999px", top: "0",
            width: "0px", height: "0px", opacity: "0", zIndex: "-1",
          });
          dataWidget.element.hidden = true;
        }
      }

      const root = document.createElement("div");
      root.className = "bzdesk";
      root.innerHTML = `
        <div class="bzdesk-bar">
          <span class="d-title">✍️ 文案台</span>
          <span class="d-segchip" style="display:none"></span>
          <span class="d-sp"></span>
          <button class="d-btn d-follow" type="button"
            title="跟随导演台：切到哪个分段，这里就写哪一段的词">跟随：开</button>
          <button class="d-btn d-alias" type="button"
            title="别名模式：媒体架登记过的名字（小明 / @小明）直接当引用，在本段里高亮，点标签插进去的也是名字而不是 @图片N">别名：关</button>
          <button class="d-btn d-save" type="button"
            title="把当前这句话存成预设">存预设</button>
        </div>
        <div class="bzdesk-chips"></div>
        <div class="bzdesk-sec">
          <div class="d-lab">预设 <span class="hint">点一下追加到本段末尾</span></div>
          <div class="bzdesk-presets"></div>
        </div>
        <div class="bzdesk-sec global">
          <div class="d-lab">片子级 <span class="hint">全片基调，每段都带 · 写 @ 也会送，引用建议写在本段</span></div>
          <div class="gcast-pwrap d-global-pwrap"></div>
        </div>
        <div class="bzdesk-sec d-segsec">
          <div class="d-lab">本段 <span class="hint">这一段要发生的事 · 输入 @ 弹出素材补全</span></div>
          <div class="gcast-pwrap d-segwrap"></div>
        </div>
        <div class="bzdesk-sec d-checksec" style="display:none">
          <div class="d-lab">提示词检查 <span class="hint">只报不拦，改词就在上面</span></div>
          <div class="d-check"></div>
        </div>
      `;

      const el = {
        /* 两个编辑区是 .gcast-pwrap 容器，makePromptEditor 的 node 挂进去。
           类名彼此区分开（片子级 .d-global-pwrap / 本段 .d-segwrap），
           靠顺序活着的 querySelector 改一次 DOM 就翻车。 */
        segChip: root.querySelector(".d-segchip"),
        follow: root.querySelector(".d-follow"),
        alias: root.querySelector(".d-alias"),
        save: root.querySelector(".d-save"),
        chips: root.querySelector(".bzdesk-chips"),
        globalWrap: root.querySelector(".d-global-pwrap"),
        segWrap: root.querySelector(".d-segwrap"),
        presets: root.querySelector(".bzdesk-presets"),
        checkSec: root.querySelector(".d-checksec"),
        check: root.querySelector(".d-check"),
      };

      let follow = true;
      let lastGen = null;      // 上次见到的导演台「代际」；变了 = 新建整片 → 清空
      let curKey = "";         // 当前本段写的是哪一段（段身份）
      let chipSig = "";
      let curRow = null;       // 当前段那一行档案（段号 / 锁定）
      const chipEls = new Map();   // 规范 token → chip 按钮（打字时只改 class）

      /* ---- 文案落 SQLite 工程库（20261012 方案 A）----
         commit 是所有编辑（打字 / 切段 / 预设 / 别名）的唯一收口，防抖 700ms
         整份落 /cglide/store/desk。hydrate 灌库后 2s 内的 commit 不回写，
         防止"刚读出又写回"的回声把库里的新数据盖掉。 */
      let deskSaveTimer = 0;
      let deskHydrateUntil = 0;
      function scheduleDeskSave() {
        if (Date.now() < deskHydrateUntil) return;
        const p = castProject(node);
        const name = (p && p.name) || (st.chain && st.chain.name) || "";
        if (!name) return;
        clearTimeout(deskSaveTimer);
        /* 写穿透统一经 projectStore 防抖调度：fire 时才取最新 st 落库，
           提交前 flushWrites 立即触发并等它。 */
        projectStore.scheduleSave(name, "desk", () => {
          return saveDesk(name, JSON.parse(JSON.stringify(st)));
        }, 700);
      }

      const keyOf = () => {
        /* 段身份优先用 clip id（后端认段就是这个）；新段还没回传 clip 时用
           分段的 uid 顶上——两者都不会在新段第一次跑时变成空的。 */
        return (st.chain && (st.chain.clip || st.chain.shot)) || "";
      };

      const commit = () => {
        syncFromCast();
        if (!dataWidget) dataWidget = node.widgets?.find((w) => w.name === "desk_data");
        if (dataWidget) dataWidget.value = JSON.stringify(st);
        node.setDirtyCanvas(true, true);
        scheduleDeskSave();    // 防抖落工程库（老工程后端 409 静默，无副作用）
      };

      /* ---- 跟随导演台：段身份与片名都从它那儿来 ----
         认段优先用**三个节点共享的 current**（后端那份当前段），而不是导演台
         面板上的选中段 —— 差别就在锁定：锁定后导演台把 current 钉在那一段，
         于是这里继续写锁定的段，不跟着点来点去跳。这正是「锁编辑目标」的意思。 */
      async function syncFromCast() {
        if (!follow) return;
        const p = castProject(node);
        if (!p) return;
        if (p.name) st.chain.name = p.name;
        /* 20261012 方案B：current 与段行优先读 bzStore 快照（零 HTTP），
           缓存还没到才退回接口。 */
        const sf = bzStore.film(p.name);
        let id = sf ? (sf.current || sf.head || "") : "";
        if (!id) { try { id = (await currentOf(p.name)) || ""; } catch { id = ""; } }
        if (!id) id = p.clip || p.shot || "";   // 导演台还没认段时退回面板快照
        if (id) st.chain.clip = id;
        if (p.shot) st.chain.shot = p.shot;
        /* 段号与锁定状态从**档案那一行**读，而不是拿导演台面板的 idx 去拼：
           面板 idx 是"导演台上选中了哪一段"，锁定后它会跟着点走，而档案里的
           current 才是三个节点共同的编辑目标。拼错了就会出现「时间轴说写第
           2 段，文案台说第 3 段」。 */
        if (sf && id) {
          curRow = (sf.clips || []).find((c) => c.id === id) || null;
        } else {
          try { curRow = await currentClipOf(p.name); } catch { curRow = null; }
        }
      }

      function refreshSegChip() {
        const p = castProject(node);
        const id = keyOf();
        if (!id || !p) {
          el.segChip.style.display = "none";
          el.segChip.classList.remove("locked");
          el.segChip.title = "";
          return;
        }
        el.segChip.style.display = "";
        const n = (curRow && curRow.index) || ((p.index ?? 0) + 1);
        const locked = !!(curRow && curRow.locked);
        /* 🔒 与紫环说的是同一件事（编辑目标被钉住），三处必须一起出现 ——
           只在时间轴上显示锁，用户在这里看不出为什么切不了段。 */
        el.segChip.textContent = (locked ? "🔒 " : "") + `第 ${n} 段`;
        el.segChip.classList.toggle("locked", locked);
        el.segChip.title = `片「${p.name || "未命名"}」· 段落 ${st.chain.clip || st.chain.shot || "未登记"}`
          + (locked ? "\n已锁定：再点别的段，这里继续写这一段" : "");
      }

      /* ---- @标签 chips：媒体架里有什么就摆什么 ---- */
      /* 别名为主、规范 token 作副标 —— 与导演台同一套：chip 上认得出是谁，
         点进去插的是别名，展开成 <Picture n> 只在发送时发生。 */
      /* 别名规则与导演台同口径：多个称呼用逗号分隔；重名时先出现者胜；
         @小明 必须带 @ 才引用，小明 裸写就引用。 */
      const ALIAS_SPLIT = /[,，、;；]+/;
      const normSpace = (s) => (s ? s.replace(/\u3000/g, " ") : s);
      function aliasListOf(s) {
        const v = s && s.alias;
        const arr = Array.isArray(v) ? v
          : (typeof v === "string" && v.trim() ? v.split(ALIAS_SPLIT) : []);
        return arr.map((x) => String(x).trim()).filter(Boolean);
      }

      /** 媒体架当前的素材清单（chips 与高亮层共用同一份）。 */
      function rackRows() {
        const { slots } = rackSlotsOf(node);
        const rows = [];
        const push = (kind, token, s, label) => {
          const list = aliasListOf(s);
          rows.push({ kind, token, file: s.file, label: (list[0] || s.name || label), alias: list[0] || "", aliases: list });
        };
        (slots.images || []).forEach((s, i) => {
          if (s && s.file) push("image", `@图片${i + 1}`, s, `图片${i + 1}`);
        });
        (slots.videos || []).forEach((s, i) => {
          if (!s || !s.file) return;
          if (s.audio) push("audio", `@视频音频${i + 1}`, s, `${s.name || `视频${i + 1}`} · 音轨`);
          push("video", `@视频${i + 1}`, s, `视频${i + 1}`);
        });
        (slots.audios || []).forEach((s, i) => {
          if (s && s.file) push("audio", `@音频${i + 1}`, s, `音频${i + 1}`);
        });
        return rows;
      }

      /** 给通用编辑器喂词表：一条素材一条记录。
          · name   规范名 @图片N，永远是主名（必须带 @ 才引用）
          · alias  友好名（不带 @），只在「别名模式」开时给 —— 关时只认 @图片N，
                    与 paintChips / 旧高亮层同一口径
          · strict 该别名登记成 @小明（带 @ 才引用）
          · file   空 → 编辑器把这条引用标成「指向空槽位」警示色 */
      function editorTokens() {
        const useAlias = !!st.alias;
        return rackRows().map((r) => {
          let alias = "", strict = false;
          if (useAlias) {
            for (const raw of r.aliases) {
              const at = raw.charAt(0) === "@";
              const nm = normSpace(at ? raw.slice(1) : raw).trim();
              if (nm.length < 2 || /[@\\]/.test(nm)) continue;
              if (/^(?:图片|视频音频|视频|音频)\d+$/.test(nm)) continue;
              alias = nm; strict = at; break;
            }
          }
          return {
            name: r.token, alias, canon: r.token, file: r.file || "",
            strict, label: alias || r.label, note: r.token,
          };
        });
      }

      function renderChips() {
        const rows = rackRows();

        /* 别名开关也进签名：开/关切换时 chip 插入的东西变了，必须重建 */
        const sig = (st.alias ? "A|" : "") + rows.map((r) => r.token + r.file + r.label).join("|");
        /* 「亮不亮」随打字变，所以只比对素材集合；亮灭另走 paintChips。 */
        if (sig === chipSig) { paintChips(); return; }
        chipSig = sig;
        chipEls.clear();
        el.chips.replaceChildren();
        if (!rows.length) {
          const s = document.createElement("span");
          s.className = "bzdesk-empty";
          s.textContent = "参考素材请放入 H3 媒体架，并把媒体架的「参考素材」接到本节点";
          el.chips.append(s);
          /* 素材清空：编辑器词表/高亮也跟着重算 */
          edGlobal.refresh(); edSeg.refresh();
          return;
        }
        for (const r of rows) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "bzdesk-chip";
          if (r.kind === "image") {
            const im = document.createElement("img");
            im.src = viewURL(r.file);
            b.append(im);
          } else if (r.kind === "video") {
            const v = document.createElement("video");
            v.src = viewURL(r.file);
            v.muted = true; v.preload = "metadata"; v.playsInline = true;
            b.append(v);
          } else {
            const g = document.createElement("span");
            g.className = "glyph";
            g.textContent = "♪";
            b.append(g);
          }
          const l = document.createElement("span");
          l.className = "lbl";
          l.textContent = r.label;
          b.append(l);
          const sub = document.createElement("span");
          sub.className = "sub";
          sub.textContent = r.token;
          b.append(sub);
          /* 保住光标：mousedown 默认行为会把焦点从 textarea 抢走 */
          b.addEventListener("mousedown", (e) => e.preventDefault());
          /* 点击 = 每次都在光标处插一个引用（同一张图可引用多次，与 qwen 面板、
             设计注释"点一下就插进光标处"一致）。不再做"再点移除"开关——那会让
             第二次点击把刚插的引用删掉，表现为"引用一次后再也引用不了"。
             取消引用直接在文本里删 @图片N 即可。 */
          b.addEventListener("click", () => insert((st.alias && r.alias) ? r.alias : r.token));
          b.dataset.alias = r.alias || "";
          el.chips.append(b);
          chipEls.set(r.token, b);
        }
        paintChips();
        /* 素材集合变了：编辑器补全词表与 @ 高亮跟着重算 */
        edGlobal.refresh(); edSeg.refresh();
      }

      /* 素材条就绪兜底：媒体架往往比文案台晚 configure（764 扩展建图慢），
         而读档不触发 commit、首帧读到的是空架。主通道是媒体架 onConfigure 后
         广播的 bz:rack-change（见 onRackChange → renderChips），这里只做有限
         轮询兜底，防止事件丢失 / 节点先后错位导致素材条永久空。
         · 已渲染出 chip → 成功，停止；
         · 媒体架有素材但本面板没渲染 → 继续刷（renderChips 自带签名去重，
           空刷/重复刷不重建 DOM）；
         · 媒体架还没建 / 确实没素材 → 继续等到窗口结束（最长约 12s，覆盖
           慢建图）。节点创建与每次读档（onConfigure）都重新启动一轮。 */
      let chipKickTimer = null;
      let chipKickRetry = 0;
      function kickChipsUntilReady() {
        if (node.flags?.collapsed) return;
        renderChips();
        if (root.querySelectorAll(".bzdesk-chip").length > 0) {
          chipKickRetry = 0;
          return;
        }
        if (chipKickRetry++ >= 40) { chipKickTimer = null; return; }
        if (chipKickTimer) clearTimeout(chipKickTimer);
        chipKickTimer = setTimeout(() => {
          chipKickTimer = null;
          kickChipsUntilReady();
        }, 300);
      }

      /** 亮 = 提示词里提到了 = 真的会送进参考；暗 = 没提，摆了也不会送。
          判定直接走共享纯函数 effectiveTags：先按与后端同一套规则归一化
          （<Picture N>、图片N、image 1 等都算显式引用），再按「别名模式」开关
          决定是否展开裸角色名。于是 chip 点亮的素材 == 导演台/后端实际发送的
          参考，杜绝"标签没亮、视频却被参考图带跑"。实时读编辑器，不依赖回写时机。 */
      function paintChips() {
        const all = [edGlobal.getText(), edSeg.getText()].join("\n");
        const { slots } = rackSlotsOf(node);
        const tags = effectiveTags(all, slots, st.alias);
        chipEls.forEach((b, token) => {
          const on = tags.has(token);
          b.classList.toggle("on", on);
          b.title = on
            ? `${token} · 已在提示词里（点一下可在光标处再插一次；取消就删掉文本里的 ${token}）`
            : `${token} · 点一下插到光标处（同一素材可引用多次）`;
        });
      }

      /* chip 点击只做「插入」（见上方绑定），同一张图可反复引用；
         亮/灭（chip 上的 .on）由 paintChips 按文本里出现次数自动反映，
         不再提供"再点一下移除"的开关。要取消引用，直接在文本里删 @图片N。 */

      /* ---- 两个提示词编辑器（片子级 / 本段）----
         高亮、@ 补全、悬停预览、纯文本回读都由 makePromptEditor 内置，文案台
         不再自绘高亮层、手写几何命中。词表共用 editorTokens（随媒体架/别名
         开关变化），onChange 直接拿纯文本；外部 setText 不回声。 */
      const onGlobalChange = (t) => { st.global = t; paintChips(); paintCheck(); commit(); };
      const onSegChange = (t) => {
        const k = keyOf();
        if (k) st.segs[k] = t;
        paintChips();
        paintCheck();
        commit();
      };
      const edGlobal = makePromptEditor({
        cls: "d-global", mode: "desk", view: viewURL, tokens: editorTokens,
        placeholder: "例：写实电影感，35mm 胶片颗粒，自然光",
        onChange: onGlobalChange,
      });
      const edSeg = makePromptEditor({
        cls: "d-seg", mode: "desk", view: viewURL, tokens: editorTokens,
        placeholder: "例：@图片1 转过身，看向窗外，@音频1 起",
        onChange: onSegChange,
      });
      el.globalWrap.appendChild(edGlobal.node);
      el.segWrap.appendChild(edSeg.node);

      /** 最后聚焦的编辑器；没聚焦过就插到本段（与 qwen 面板同一手法）。 */
      let lastEd = edSeg;
      edGlobal.node.addEventListener("focus", () => { lastEd = edGlobal; });
      edSeg.node.addEventListener("focus", () => { lastEd = edSeg; });

      /** 读编辑器内当前光标字符偏移（chip mousedown 已 preventDefault 保住焦点，
          此刻编辑器仍聚焦；TreeWalker 累加文本节点，与通用编辑器同口径）。 */
      function edCaret(ed) {
        const node = ed.node;
        const s = window.getSelection();
        if (!s || !s.rangeCount) return ed.getText().length;
        const r = s.getRangeAt(0);
        if (!node.contains(r.startContainer)) return ed.getText().length;
        let total = 0;
        const w = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = w.nextNode())) {
          if (n === r.startContainer) return total + r.startOffset;
          total += n.textContent.length;
        }
        return ed.getText().length;
      }

      /** 插入到最后聚焦的编辑器光标处，前后按需补空格；没聚焦过插到本段。 */
      function insert(text) {
        const ed = lastEd || edSeg;
        if (ed.readonly()) return;
        const v = ed.getText();
        const pos = edCaret(ed);
        const needL = pos > 0 && !/\s$/.test(v.slice(0, pos)) ? " " : "";
        const needR = pos < v.length && !/^\s/.test(v.slice(pos)) ? " " : "";
        ed.insert(needL + text + needR);
        ed.focus();
      }

      function renderPresets() {
        el.presets.replaceChildren();
        (st.presets || []).forEach((p, i) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "bzdesk-preset";
          b.title = "追加到本段末尾";
          const t = document.createElement("span");
          t.className = "lbl";
          t.textContent = p;
          const x = document.createElement("span");
          x.className = "x";
          x.textContent = "✕";
          x.title = "删掉这条预设";
          /* 删除绑在 pointerdown：一点就删、✕ 随即移出 DOM，松开鼠标时
             click 落不到胶囊上 —— 不会出现「点删除反而把预设插进本段」。 */
          x.addEventListener("pointerdown", (e) => {
            e.stopPropagation();
            e.preventDefault();
            st.presets.splice(i, 1);
            renderPresets();
            commit();
          });
          b.append(t, x);
          b.addEventListener("click", () => {
            /* 追加到本段末尾（不是光标处）；setText 不触发 onChange，手动收口 */
            const cur = edSeg.getText();
            const next = (cur ? cur.replace(/\s*$/, "") + "\n" : "") + p;
            edSeg.setText(next, next.length);
            onSegChange(next);
          });
          el.presets.append(b);
        });
        if (!(st.presets || []).length) {
          const s = document.createElement("span");
          s.className = "bzdesk-empty";
          s.textContent = "还没有预设 —— 写好一句话点「存预设」";
          el.presets.append(s);
        }
      }

      /* 打字收口已由两个编辑器的 onChange（onGlobalChange / onSegChange）负责，
         不再需要 textarea 时代的 onEdit 轮询两个框。 */

      /* ---- 提示词检查 ----
         这一块原先挂在导演台。但词在哪，检查就该在哪：在导演台看到"台词超时 /
         缺字段"却改不了词，只能记住警告再跑来改，警告等于白报。
         规则也不抄第二份（共用 csglide_prompt_check.js）—— 抄了迟早两边算出
         不一样的结论。
         片段时长这里拿不到（画布与帧数在导演台），传 0：共享模块会跳过时长那
         一项，不去报一个必然是红的"台词超出片段时长"。 */
      function paintCheck() {
        const text = [edGlobal.getText(), edSeg.getText()].join("\n").trim();
        const rows = promptCheck(text, 0);
        el.check.replaceChildren();
        rows.forEach((r) => {
          const item = document.createElement("div");
          item.className = "d-crow " + (r.state === "warn" ? "warn" : "ok");
          const b = document.createElement("b");
          b.textContent = r.label;
          const s = document.createElement("span");
          s.textContent = r.text;
          item.append(b, s);
          el.check.append(item);
        });
        /* 没查出问题就整块收掉：空着一条边框占位比没有更糟，用户会以为
           "有东西我没看见"。 */
        el.checkSec.style.display = rows.length ? "" : "none";
      }

      /** 切段：把当前框里的词先存进旧段，再换上新段的词。
          optimisticClip：导演台 cast 事件里**直接带来的目标段**。事件在
          setCurrent 的 POST 往返之前就到，此刻 bzStore.current 还是旧段，
          只靠 syncFromCast 读 store 首跳必然切不动（日志铁证：点第 2 段时
          bzStore.current=clip_0003 → 首跳停在旧段，要等 POST 回声才切）。
          事件 payload 是导演台刚广播的权威意图，直接采用；store 回声到达后
          仍会再跑一次 switchSeg 做后端权威校正（含锁定段被钉住的场景）。 */
      async function switchSeg(optimisticClip) {
        /* 导演台「新建整片」：代际 gen 变了，就把文案台整体清空（片子级 global、
           各段词 segs、段身份），只保留个人预设短语 presets。lastGen===null 的
           首次只记录、不清 —— 否则打开一部旧工程会把人存的词误清掉。 */
        const cp = castProject(node);
        if (cp) {
          const gen = cp.gen || 0;
          if (lastGen === null) {
            lastGen = gen;
          } else if (gen !== lastGen) {
            lastGen = gen;
            const keepPresets = st.presets;
            st = blankState();
            st.presets = keepPresets;
            curKey = "";
            edGlobal.setText("");
            edSeg.setText("");
            commit();
            renderPresets();
          }
        }
        await syncFromCast();
        let k = optimisticClip ? String(optimisticClip) : keyOf();
        /* 乐观首跳：store 还停在旧段、syncFromCast 把 st.chain.clip 写成了旧值，
           这里必须改写成事件带来的目标段，否则随后 commit 落盘的 desk_data
           chain.clip 仍是旧段，渲染提交会拿错提示词。 */
        if (optimisticClip && k) {
          st.chain.clip = k;
          if (!st.chain.shot) st.chain.shot = k;
        }
        if (k === curKey) { refreshSegChip(); return; }   // 已在本段，去重不重复刷
        if (curKey) st.segs[curKey] = edSeg.getText();
        curKey = k;
        console.log("[联动] 文案台 switchSeg 切换:", k, "旧curKey=" + (st.segs[curKey] !== undefined ? "有存档" : "无"));
        /* 没认到段就**不要**清空 —— 画布上还没有导演台时（或跟随关了）
           用户照样能在里面写词，一清空就把人正在写的东西抹了。 */
        if (k) edSeg.setText(st.segs[k] || "");
        refreshSegChip();
        paintChips();
        paintCheck();        // 换段 = 换了一段词，检查跟着换
        /* 切段必须落盘 desk_data：后端读的是 widget 里的 chain.clip 来认段，
           只改内存 st 不写 widget，渲染提交（graphToPrompt）拿到的还是旧段
           —— 三段渲染全用同一段的提示词（20261012 渲染日志铁证：
           第 2 段渲染时文案台输出「段 clip_0003」）。 */
        commit();
      }

      /** 中央工程状态层（projectStore）按段身份读本段段词。
          若此刻正编辑该段，先把编辑器最新文本收口再返回。 */
      function getSegWords(key) {
        if (!key) return "";
        key = String(key);
        if (curKey && curKey === key) st.segs[key] = edSeg.getText();
        return String(st.segs[key] || "");
      }

      /** 与后端 csglide_desk.compose_text **逐字同口径**的「片子级 + 本段」组合：
          片子级（global）在前、本段（seg）在后，去掉行尾空白并给相邻 @标签补空格。
          这是导演台外接文案台时**真正会发送**的文本。导演台 effPrompt 不再用
          "最长字符串 widget" 启发式（那会把整段 desk_data JSON 当成提示词，于是
          预览/自检把别的段的 @图片N 也算进来）。key 为段 clip id；不传只出片子级。
          按段身份直接读 st.segs[key]，不依赖文案台 UI 此刻停在哪一段（广播切段
          是异步的，渲染循环里导演台已切段、文案台还没跟上时也读得对）。 */
      function composedWords(key) {
        // global 全片唯一，编辑器里始终是它，先收口一次拿到最新输入。
        if (edGlobal) st.global = edGlobal.getText();
        let seg = "";
        if (key) {
          const k = String(key);
          if (curKey && curKey === k) st.segs[k] = edSeg.getText();
          seg = String(st.segs[k] || "");
        }
        const clean = (t) => String(t || "").replace(/\r\n/g, "\n")
          .replace(/[ \t]+\n/g, "\n").trim();
        const parts = [clean(st.global), clean(seg)].filter(Boolean);
        return parts.join("\n").replace(
          /(@(?:first|last|图片\d+|视频音频\d+|视频\d+|音频\d+))(?=@)/g, "$1 ");
      }

      /** 复制段时把源段段词照抄到新段（导演台 duplicateShot 调用）。
          缺这一步，复制段切过来时编辑器读 st.segs[新cid] 是空的 —— 文案没复制。
          若此刻正编辑源段，先把编辑器最新文本收口；已在目标段则同步上屏。 */
      function copySegWords(srcCid, dstCid) {
        if (!dstCid) return false;
        srcCid = srcCid ? String(srcCid) : "";
        dstCid = String(dstCid);
        if (curKey && curKey === srcCid) st.segs[srcCid] = edSeg.getText();
        const words = srcCid ? String(st.segs[srcCid] || "") : "";
        st.segs[dstCid] = words;
        if (curKey === dstCid) edSeg.setText(words);
        refreshSegChip();
        commit();
        return true;
      }

      /** 临时段身份（shot.id）升级成真实 clip id（registerShot 拿到 id 后调用）。
          把先前抄到临时身份的段词搬到 clip id；若此刻正停在临时身份，连人一起切。
          没有临时段词也不影响（addShot 新段本来就是空）。 */
      function claimSegWords(tempId, clipId) {
        if (!clipId) return false;
        tempId = tempId ? String(tempId) : "";
        clipId = String(clipId);
        if (curKey && curKey === tempId) st.segs[tempId] = edSeg.getText();
        let words = "";
        if (tempId && st.segs[tempId] != null) words = String(st.segs[tempId] || "");
        if (tempId && tempId !== clipId) delete st.segs[tempId];
        st.segs[clipId] = words;
        if (curKey === tempId) { curKey = clipId; edSeg.setText(words); }
        refreshSegChip();
        commit();
        return true;
      }

      /* 输入 / 高亮 / 悬停预览 / 滚动全部由两个 makePromptEditor 实例内置：
         输入经 onGlobalChange / onSegChange 收口，@ 补全、.tok 高亮、悬停缩略图
         都在编辑器内部，文案台不再挂 textarea 事件、自建 toktip 几何命中。 */

      el.follow.addEventListener("click", () => {
        follow = !follow;
        el.follow.textContent = follow ? "跟随：开" : "跟随：关";
        el.follow.title = follow
          ? "跟随导演台：切到哪个分段，这里就写哪一段的词"
          : "已关闭跟随：段身份不再随导演台变化，适合手动写别的地方";
        commit();
      });
      /* 别名模式：开 = 名字（小明）直接当引用，在本段里高亮、标签插入名字；
         关 = 只认 @图片N。真正把名字换成 <Picture n> 是导演台后端做的，
         这个开关只管文案台怎么显示和怎么写。 */
      const paintAlias = () => {
        el.alias.textContent = `别名：${st.alias ? "开" : "关"}`;
      };
      el.alias.addEventListener("click", () => {
        st.alias = !st.alias;
        paintAlias();
        renderChips();       // 重新插入：开/关切换后 chip 点击插入的写法变了
        commit();
      });

      el.save.addEventListener("click", () => {
        const t = edSeg.getText().trim();
        if (!t) return;
        if (!st.presets.includes(t)) st.presets.push(t);
        renderPresets();
        commit();
      });

      /* 联动零轮询（20261012 方案B）：
         · 导演台本地切段/新建 → bz:cast-seg-change 即时响应；
         · 媒体架 commit → bz:rack-change 即时刷 chips；
         · 后端档案 current/段账变化（含跨标签页、工作流执行中登记）
           → bzStore film 事件即时 switchSeg。
         renderChips 与 switchSeg 各自带签名去重，重复触发零重建。 */
      /* 快速连点时 cast/store 事件会瞬间到达多个，switchSeg 是 async
         （内含 await syncFromCast），并发执行会交错读写 curKey / st.segs，
         把旧段的词存进新段。用一条链把切段严格串行化（20261012 连点串段）。 */
      let segChain = Promise.resolve();
      const enqueueSwitch = (clip) => {
        segChain = segChain.then(() => switchSeg(clip)).catch(() => {});
        return segChain;
      };
      const pollOnce = async (optimisticClip) => {
        if (node.flags?.collapsed) return;
        renderChips();
        await enqueueSwitch(optimisticClip);
      };
      /* store 订阅随片名切换（新建/打开工程 cast 事件里重新挂） */
      let storeName = "";
      let offStore = null;
      const ensureSub = () => {
        const cp = castProject(node);
        const name = (cp && cp.name) || "";
        if (name === storeName) return;
        if (offStore) { try { offStore(); } catch (_) {} offStore = null; }
        storeName = name;
        if (!name) return;
        offStore = bzStore.subscribe(name, (d) => {
          if (node.flags?.collapsed || d.immediate) return;
          void enqueueSwitch();
        });
      };
      ensureSub();
      const offCast = onCastChange((d) => {
        console.log("[联动] 文案台收到", d.reason || "?", "name=" + (d.name || ""), "clip=" + (d.clip || ""), "折叠=" + !!node.flags?.collapsed);
        if (node.flags?.collapsed) { console.warn("[联动] 文案台跳过: 节点折叠"); return; }
        const cp = castProject(node);
        if (!cp || d.name !== cp.name) { console.warn("[联动] 文案台跳过: 名字不匹配 d.name=" + (d.name || "") + " cp.name=" + (cp && cp.name || "无")); return; }
        ensureSub();
        /* 事件带了目标段就直接乐观切过去（首跳不等 store）。复制/新建段第一次
           广播时 clip 还没登记，但带了临时 shot.id —— 用它先切、段词已提前抄好，
           不用等 add_clip 网络往返（消除"慢一拍"）。 */
        void pollOnce(d.clip || d.shot || "");
      });
      const offRack = onRackChange((d) => {
        if (node.flags?.collapsed) return;
        const w = rackNodeOf(node);
        if (w && d.node != null && w.id !== d.node) return;
        renderChips();
      });

      const onRemoved = nodeType.prototype.onRemoved;
      node.onRemoved = function () {
        if (offStore) { try { offStore(); } catch (_) {} offStore = null; }
        try { offCast(); offRack(); } catch (_) {}
        /* 停掉素材条就绪兜底轮询，避免节点销毁后定时器残留 */
        if (chipKickTimer) { clearTimeout(chipKickTimer); chipKickTimer = null; }
        /* 摘掉补全列表 / 悬停浮窗与 document 监听，避免节点销毁后残留 */
        try { edGlobal.destroy(); edSeg.destroy(); } catch (_) {}
        return onRemoved?.apply(this, arguments);
      };

      const w = node.addDOMWidget("desk_ui", "div", root, {
        serialize: false, hideOnZoom: false,
      });
      /* 报固定小值 minHeight（不是内容高）：前端拖拽以 node.computeSize()
         （各 widget minHeight 之和）做下限钳制，报内容峰值节点就永远拖不矮。
         报小值 + maxHeight 无限，布局让面板填满节点主体，收短后内容在面板里
         滚动、不塌陷。 */
      w.computeLayoutSize = () => ({ minHeight: MIN_FLOOR_H, maxHeight: void 0, minWidth: 0 });

      /* 建节点/读档后长一次，给个装得下内容的起点（只增不减）；此后不再自动
         改高，高度全归用户。scrollHeight 取内容实高，offsetHeight 兜底。 */
      const fit = () => {
        requestAnimationFrame(() => {
          if (node.flags?.collapsed) return;
          const contentH = Math.ceil(Math.max(root.scrollHeight || 0, root.offsetHeight || 0));
          if (!contentH) return;
          const head = (typeof LiteGraph !== "undefined" && LiteGraph.NODE_TITLE_HEIGHT) || 30;
          const needH = head + 2 + contentH + 6;
          if (needH > (node.size?.[1] || 0) + 1) node.setSize?.([node.size[0], Math.ceil(needH)]);
        });
      };
      /* DOM widget 的包裹层设 none，点击才能落回画布；面板根恢复 auto，
         内部子元素照常可点（与媒体架同一手法）。 */
      requestAnimationFrame(() => {
        const wrap = root.parentElement;
        if (wrap && wrap.classList.contains("dom-widget")) wrap.style.pointerEvents = "none";
      });

      edGlobal.setText(st.global || "");
      paintAlias();
      switchSeg();
      renderChips();
      renderPresets();
      paintChips();
      paintCheck();
      /* 立刻再同步一次：castProject 要等画布上导演台节点建好才拿得到，
         建面板这一刻往往还没有。用 setTimeout 而非 rAF：标签页在后台时
         rAF 被暂停，素材条兜底会整段不启动（setTimeout 后台仍执行）。 */
      setTimeout(async () => {
        await switchSeg();
        /* 素材条就绪：先刷一次，再由 kickChipsUntilReady 兜底到媒体架就绪；
           主通道是媒体架 onConfigure 广播的 bz:rack-change（见 onRackChange）。 */
        kickChipsUntilReady();
      }, 0);
      node.setSize?.([MIN_W, MIN_H]);
      fit();

      /* 工程库启动灌库（hydrate）：用库里的文案整份替换内存，再重绘。
         chain（片名/段身份）以导演台与后端当前值为准，不被库存档里的旧
         chain 带偏——只恢复 global/segs/alias/presets 这些文案内容。 */
      const hydrateDesk = (desk) => {
        if (!desk || typeof desk !== "object") return false;
        deskHydrateUntil = Date.now() + 2000;   // 抑制 hydrate 触发的 commit 回写
        const incoming = JSON.parse(JSON.stringify(desk));
        const chain = Object.assign({}, st.chain || {}, incoming.chain || {});
        st = Object.assign(blankState(), incoming, { chain });
        if (!dataWidget) dataWidget = node.widgets?.find((w) => w.name === "desk_data");
        if (dataWidget) dataWidget.value = JSON.stringify(st);
        curKey = "";
        lastGen = null;
        edGlobal.setText(st.global || "");
        paintAlias();
        renderPresets();
        switchSeg();
        renderChips();
        paintChips();
        paintCheck();
        console.log("[状态库] 文案台 hydrate 完成: 段词=" + Object.keys(st.segs || {}).length
          + " 全局=" + ((st.global || "").length) + "字");
        return true;
      };

      node.deskUI = {
        root,
        BAG_VERSION: BZ_BAG_VERSION,
        hydrate: hydrateDesk,
        refresh() { renderChips(); switchSeg(); paintChips(); },
        segWords: getSegWords,
        composedWords,
        copySeg: copySegWords,
        claimSeg: claimSegWords,
      };

      /* ---- 刷新网页 / 读档时恢复内容 ----
         onNodeCreated 建面板时 desk_data 还是空的（工作流保存值要等 configure
         才填进来），不在这里重读一次，面板就是空白；再一编辑还会把空白写回
         desk_data，把人存的内容真抹掉。与导演台 / 媒体架同一套做法。 */
      const onConfigure = nodeType.prototype.onConfigure;
      node.onConfigure = function () {
        const r = onConfigure?.apply(this, arguments);
        if (dataWidget) {
          try { st = Object.assign(blankState(), JSON.parse(dataWidget.value || "{}")); }
          catch { st = blankState(); }
        }
        curKey = "";              // 旧段身份作废，让 switchSeg 重新认段
        lastGen = null;           // 代际也重新认识，避免读档后被误判为「新建」
        edGlobal.setText(st.global || "");
        paintAlias();             // 别名开关要跟着存档一起回来
        switchSeg();              // 会按恢复出的段身份把「本段」填回 textarea
        renderPresets();
        paintChips();
        paintCheck();
        /* 读档（reload / loadGraphData）后媒体架通常比文案台晚 configure：
           立即刷一次，再交给 kickChipsUntilReady 兜底（媒体架 onConfigure 也会
           广播 bz:rack-change 走主通道）。否则素材条在慢建图后永久空着。
           用 setTimeout 而非 rAF，后台标签页也能启动。 */
        setTimeout(() => kickChipsUntilReady(), 0);
        return r;
      };
      return r;
    };
  },
});
