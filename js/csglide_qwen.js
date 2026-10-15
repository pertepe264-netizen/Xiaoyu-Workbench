/* ============================================================
 * 🎨 Qwen 编辑台 (Qwen Studio) — Qwen Image 2.1 图片编辑
 * ------------------------------------------------------------
 * 视觉与排版复用导演台同一份样式表（CAST_CSS）与同一套类名
 * （gcast-modebar / gcast-field / gcast-pwrap / gcast-chips …），
 * 只换主色（紫）以区别于 H3 家族。
 *
 * 参考图不走面板槽位：素材统一由 🗂️ H3 媒体架 经 refs 端口喂入，
 * 槽位编号与媒体架格子永久绑定——图片落在哪格，@图片N 就指它。
 * 面板把已接素材展示成提示词名下的 chips（别名优先），点击即插入引用。
 *
 * 与导演台的一处**有意不同**：参考图一律全送，不需要在提示词里
 * 点名——图片编辑改的就是这些图。@图片N 只用来在文字里区分它们。
 *
 * 外接提示词与导演台同款（模式 A）：prompt_text 接线即外部文本整体
 * 接管**正面**提示词（负面仍归面板），面板转只读镜像 + 徽章，断线即恢复。
 * ============================================================ */
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { CAST_CSS, makeSelect, sizeLadder, findRatio, promptNodeOf, upstreamTextOf } from "./csglide_cast.js";
import { makePromptEditor } from "./csglide_prompt_editor.js";
import { onRackChange } from "./bz_rack.js";

const NODE_ID = "CSGlideQwenCastCS";
const MIN_W = 620, MIN_H = 620;
const STEP = 32, MAX_SIDE = 4096;

/* 画布比例。每族基数是该比例的**顶档**（4K 级），逐档递减的阶梯由 sizeLadder
   生成；面积上限 = 顶档自身（4096×2304，16:9 的 4K DCI 尺寸）。
   Qwen Image 2.1 的编辑输出可以到 4K，再往上面积翻倍，注意力与耗时是平方级
   上去的，所以不再放宽。
   注意 32 对齐这条硬约束：3840×2160 的 2160 不是 32 的倍数（2160/32=67.5），
   送进后端会被吸附成 2144——面板上写 2160、实际跑 2144 是静默不一致。所以
   4K 档取 32 对齐且**比例精确**的组合（16:9 = 4096×2304，正好是 16/9）。 */
const MAX_PIXELS = 4096 * 2304;
/* 短边阶梯（必须全是 32 的倍数）。3072 / 2592 / 2368 只会落在方图与 4:3、3:2
   上——其余比例用这些短边算出的长边面积超顶，会被面积上限滤掉。 */
const SHORT_EDGES = [3072, 2592, 2368, 2304, 1920, 1728, 1440, 1280, 1152,
                     1024, 896, 768, 640, 512, 384, 320];
const RATIOS = [
  { label: "21:9", w: 4032, h: 1728 },
  { label: "16:9", w: 4096, h: 2304 },
  { label: "3:2",  w: 3552, h: 2368 },
  { label: "4:3",  w: 3456, h: 2592 },
  { label: "1:1",  w: 3072, h: 3072 },
  { label: "3:4",  w: 2592, h: 3456 },
  { label: "2:3",  w: 2368, h: 3552 },
  { label: "9:16", w: 2304, h: 4096 },
];
RATIOS.forEach((r) => {
  r.sizes = sizeLadder(r, SHORT_EDGES, MAX_PIXELS, MAX_SIDE);
  r.sizes.sort((a, b) => (b.w * b.h) - (a.w * a.h));
});

const snap = (v) => Math.max(STEP, Math.min(MAX_SIDE, Math.round((+v || 0) / STEP) * STEP));

/* 参考尺寸（最长边上限）的档位与缺省值；缺省与后端 REF_CAP_DEFAULT 同值 */
const REF_CAPS = [0, 512, 768, 1024, 1280, 1536, 2048, 2560, 3072, 4096];
const REF_CAP_DEFAULT = 1024;

/* 跟随图片1 时画布长边的缺省值（px）。0 = 不限：用图片原尺寸（4096 硬顶仍在）。 */
const REF_LONG_DEFAULT = 1024;

/* 只补导演台样式表里没有的：别名 chips 的缩略图 + 提示词区的高度策略 */
const QWEN_CSS = `
.gq { --h3-accent:#a97bff; --h3-accent-dim:#a97bff26; }
/* 下拉菜单与 @ 补全列表挂在 body 上，拿不到 .gq 的局部变量，主色走 data-mode 传递 */
.gcast-menu[data-mode="qwen"] { --h3-accent:#a97bff; }
.gcast-ac[data-mode="qwen"] { --h3-accent:#a97bff; --h3-accent-dim:#a97bff26; }
/* 导演台表单行里的数字框定宽 46px，是给 4 卡的窄列调的；画布尺寸要放得下
   4 位数（4096）加右侧的微调箭头，所以加宽并左对齐。spinner 保留——它是
   尺度的微调入口（每格 32，与后端对齐粒度一致）。 */
.gq .gcast-field .gcast-ctl input[type="number"] { width:88px; padding:0 2px 0 6px; text-align:left; }
/* 导演台的 .gcast-row 是横排侧栏（flex:1 1 240px 管宽度）；本面板把它直接
   放进纵向根容器，同一条 flex 就变成管高度——画布行被纵向撑开吃掉全部剩余
   空间，缝隙出现在画布与提示词之间、下方内容被挤出节点底。这里改回内容高，
   让「吃剩余高度」的只剩提示词编辑区（与导演台的意图一致）。 */
.gq .gcast-row { flex:0 0 auto; max-width:none; }
/* 提示词是本节点的主要内容，吃掉节点剩余高度：节点拖多高编辑区就多大，
   同时给一个可用下限，免得节点收矮后编辑区只剩一条缝 */
.gq .gcast-pwrap { flex:1 1 auto; min-height:0; }
.gq .gcast-ed.prompt { flex:1 1 auto; min-height:190px; }
/* 负面提示词是次要输入，固定矮高度，不跟正提示词抢节点高度 */
.gq .gcast-ed.neg { flex:0 0 auto; min-height:54px; }
.gq-hint { color:var(--h3-dim); font-size:10.5px; line-height:1.45; }
`;

let cssInjected = false;
function injectCSS() {
  if (cssInjected) return;
  if (!document.getElementById("gcast-css")) {
    const base = document.createElement("style");
    base.id = "gcast-css";
    base.textContent = CAST_CSS;
    document.head.append(base);
  }
  const s = document.createElement("style");
  s.id = "gq-css";
  s.textContent = QWEN_CSS;
  document.head.append(s);
  cssInjected = true;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function viewURL(file) {
  if (!file) return "";
  const i = file.lastIndexOf("/");
  const sub = i >= 0 ? file.slice(0, i) : "";
  const name = i >= 0 ? file.slice(i + 1) : file;
  return api.apiURL(`/view?filename=${encodeURIComponent(name)}&subfolder=${encodeURIComponent(sub)}&type=input`);
}

/* 别名（命名引用）与后端 csglide_qwen.py 同一套口径：全角空格归一、
   单字不注册、含 @ 或反斜杠不行、撞内置词不行。这里只负责「取首个合法
   别名」给 chips 做显示与插入；真正的展开在后端做（后端是权威）。 */
const ALIAS_SPLIT = /[,，、;；]+/;
const ALIAS_MIN = 2, ALIAS_MAX = 40;
const BUILTIN_ALIAS = /^(?:图片\d+|视频音频\d+|视频\d+|音频\d+)$/i;
const normSpace = (s) => (s ? s.replace(/\u3000/g, " ") : s);
function aliasPick(v) {
  const raw = Array.isArray(v) ? v.filter((x) => typeof x === "string").join(",") : v;
  if (typeof raw !== "string" || !raw.trim()) return "";
  for (const p of raw.split(ALIAS_SPLIT)) {
    const a = normSpace(p.trim());
    const at = a.startsWith("@");
    const name = at ? a.slice(1) : a;
    if (name.length >= ALIAS_MIN && name.length <= ALIAS_MAX
      && !/[@\\]/.test(name) && !BUILTIN_ALIAS.test(name)) {
      return (at ? "@" : "") + name;
    }
  }
  return "";
}

function buildUI(node) {
  const root = el("div", "gcast gq");
  const st = {
    prompt: "", negative_prompt: "",
    canvas: { w: 1024, h: 1024 },   // 画布始终是明确尺寸，缺省官方默认 1024²
    canvas_from_ref: false,         // 画布跟随图片1尺寸（后端取图片1原尺寸）
    ref_long: REF_LONG_DEFAULT,     // 跟随时的画布长边（px），只缩不放；0 = 用图片原尺寸
    ref_cap: REF_CAP_DEFAULT,       // 参考图最长边上限（px）；0 = 原尺寸（不限）
  };
  let dataWidget = null;

  const commit = () => {
    if (!dataWidget) dataWidget = node.widgets?.find((w) => w.name === "qwen_data");
    if (dataWidget) dataWidget.value = JSON.stringify(st);
    node.setDirtyCanvas(true, true);
  };

  /* ---------------- header ---------------- */
  const head = el("div", "gcast-modebar");
  head.append(el("div", "gcast-label", "Qwen Image 2.1 · 图片编辑"));
  const headRead = el("div", "gcast-need");
  head.append(headRead);

  /* ---------------- 参考素材：来自 🗂️ H3 媒体架（refs 端口）----------------
     面板不自带上传槽位——素材统一在媒体架管理，一个媒体架可以喂多个节点。
     槽位编号与媒体架永久绑定：图片落在哪个格，@图片N 就指它。已接素材
     展示成提示词名下的 chips（别名优先），点击即把引用写进提示词。 */
  function rackNodeOf(n) {
    const inputs = n.inputs || [];
    const inp = inputs.find((s) => s && s.type === "H3_REFS" && s.link != null)
      || inputs.find((s) => s && s.name === "refs" && s.link != null);
    const graph = (typeof app !== "undefined" && app && app.graph) || null;
    if (inp && graph && graph.links != null) {
      const link = graph.links[inp.link];
      if (link) {
        const up = graph.getNodeById ? graph.getNodeById(link.origin_id) : null;
        if (up) return up;
      }
    }
    return n.getInputNode ? n.getInputNode("refs") : null;
  }

  let rackCache = { raw: null, images: [] };
  function rackImages() {
    const w = rackNodeOf(node);
    const raw = (w && w.widgets && w.widgets.find((x) => x.name === "rack_data")?.value) || "";
    if (raw === rackCache.raw) return rackCache.images;
    let d = {};
    try { d = JSON.parse(raw); } catch (_) { d = {}; }
    const s = (d && d.slots) || {};
    rackCache = { raw, images: Array.isArray(s.images) ? s.images : [] };
    return rackCache.images;
  }

  const chips = el("div", "gcast-chips");
  const refRead = el("div", "gcast-read");

  function renderChips() {
    chips.replaceChildren();
    let ord = 0;
    rackImages().forEach((slot, i) => {
      if (!slot || !slot.file) return;
      ord += 1;
      const token = `@图片${i + 1}`;
      const name = aliasPick(slot.alias);        // 显示与插入优先用别名
      const chip = el("button", "gcast-chip");
      const th = el("img");
      th.src = viewURL(slot.file);
      chip.append(th, el("span", "lbl", name || token), el("span", "sub", `<image${ord}>`));
      chip.title = `插入 ${name || token}（即 ${token}，编码器看到 <image${ord}>）\n${slot.file}`;
      chip.onclick = () => insertToken(name || token);
      chips.append(chip);
    });
    if (!ord) {
      chips.append(el("span", "gq-hint",
        "未接媒体架——把 🗂️ H3 媒体架 的输出接到「参考素材」端口，素材会出现在这里。"));
    }
    refRead.textContent = ord ? `已接 ${ord} 张` : "未接";
  }

  /* 记录表：媒体架槽位 → 高亮 / @补全 / 悬停预览共用同一份口径
     （与后端 alias_table 对称：别名不带 @，写 @别名 的槽位是 strict）。
     一条素材一条记录：别名只是这条素材的友好名，不再额外生成「规范名」记录，
     否则菜单里一条素材会占两行、看起来像素材翻倍。 */
  function vocab() {
    const out = [];
    rackImages().forEach((slot, i) => {
      if (!slot || !slot.file) return;
      const name = `@图片${i + 1}`;
      const al = aliasPick(slot.alias);
      const bare = al ? al.replace(/^@/, "") : "";
      out.push({
        name,                              // 规范引用，永远可写
        alias: bare,                       // 友好名（可能为空）
        strict: al ? al.startsWith("@") : false,
        file: slot.file,
        label: bare || name,               // 菜单里显示的名字：有别名用别名
        note: bare ? name : slot.file.split("/").pop(),
      });
    });
    return out;
  }

  /* 媒体架变动后：chips 与两个编辑器的高亮一起跟上 */
  function refsChanged() {
    renderChips();
    edPos.refresh();
    edNeg.refresh();
  }

  /* 20261012 方案B 后这里是全包里**唯一保留的定时器**（2.5s）：
     媒体架变化已由 rack-change 事件即时驱动（见下）；本 tick 只剩一个客观
     无事件源的脏检查——**外接上游文本节点改字**（LiteGraph 不提供跨节点
     widget changed 广播）。内部全部按原文签名去重，没变就是几次字典/字符串
     比对，折叠或页面隐藏时直接早退，不再有任何 DOM 成本。 */
  let rackSeen = null, extSeen = null;
  function pollOnce() {
    if (node.flags?.collapsed || document.hidden) return;
    rackImages();
    if (rackCache.raw !== rackSeen) {
      rackSeen = rackCache.raw;
      refsChanged();
    }
    const up = promptNodeOf(node);
    const key = up ? "W\u0001" + upstreamTextOf(up) : "";
    if (key !== extSeen) { extSeen = key; paintExt(); }
  }
  const rackPoll = setInterval(pollOnce, 2500);
  /* 媒体架 commit 即时广播：chips/高亮立刻刷新，零等待。
     只认自己接线的那台媒体架（20261012 联动提速）。 */
  const offRackEvt = onRackChange((d) => {
    if (node.flags?.collapsed) return;
    const w = rackNodeOf(node);
    if (w && d.node != null && w.id !== d.node) return;
    pollOnce();
  });

  /* ---------------- 设置：画布 / 参考尺寸 ---------------- */
  const row = el("div", "gcast-row");
  const field = (label, ctl, read) => {
    const f = el("div", "gcast-field");
    f.append(el("span", "gcast-flabel", label), ctl);
    if (read) f.append(read);
    return f;
  };

  const canvasCtl = el("div", "gcast-ctl");
  const selRatio = makeSelect(node, "画布比例", "qwen");
  const selSize = makeSelect(node, "该比例下的画布尺寸", "qwen");
  const inW = el("input"); inW.type = "number"; inW.step = STEP; inW.min = STEP; inW.max = MAX_SIDE;
  const inH = el("input"); inH.type = "number"; inH.step = STEP; inH.min = STEP; inH.max = MAX_SIDE;
  inW.title = inH.title = "画布宽/高（像素，按 32 对齐）。↑↓ 微调，每格 32；改这里等于切到自定义尺寸。";
  const canvasNote = el("div", "gcast-read");
  canvasCtl.append(selRatio.el, selSize.el, inW, el("span", "gcap dim-x", "\u00d7"), inH);
  const fCanvas = field("画布", canvasCtl, canvasNote);
  row.append(fCanvas);

  /* ---------------- 设置：跟随图片1（画布来源）----------------
     独立的开关，不混进上面的比例下拉——比例下拉管的是「画面形状」，
     这一项管的是「尺寸从哪来」。勾上后：取图片1的原图比例，按下面填的
     画布长边等比缩小，就不用手敲宽高了。 */
  const followCtl = el("div", "gcast-ctl");
  const cbFollow = el("input"); cbFollow.type = "checkbox";
  cbFollow.title = "勾选后画布不再手填：运行时取图片1（第一个参考图槽位）的原图比例，"
    + "再按右侧的画布长边等比缩小。";
  const cbLabel = el("span", "gq-cblbl", "画布用图片1尺寸");
  cbLabel.style.cssText = "font-size:11px; white-space:nowrap; color:var(--h3-txt);";
  const inLong = el("input"); inLong.type = "number";
  inLong.step = STEP; inLong.min = 0; inLong.max = MAX_SIDE;
  inLong.title = "画布长边（像素，按 32 对齐）。↑↓ 微调，每格 32。"
    + "只缩不放：图片比这个值大就等比缩到这个值，比它小就按原尺寸出图；填 0 = 不限。";
  const followNote = el("div", "gcast-read");
  followCtl.append(cbFollow, cbLabel, el("span", "gcap dim-x", "长边"), inLong,
    el("span", "gcap dim-x", "px"));
  row.append(field("跟随图片1", followCtl, followNote));

  function paintFollow() {
    cbFollow.checked = st.canvas_from_ref;
    inLong.disabled = !st.canvas_from_ref;
    inLong.value = String(st.ref_long);
    followNote.innerHTML = st.canvas_from_ref
      ? (st.ref_long
        ? `<b>长边 ≤ ${st.ref_long} px</b> · 更大的图等比压下来`
        : `<b>原尺寸</b> · 不缩放（4096 硬顶）`)
      : `未启用 · 画布用上面的尺寸`;
  }

  /* ---------------- 设置：参考尺寸（最长边上限）----------------
     这一项管的是**参考图怎么进编码器**：素材是 4K 大图时按上限等比压下来，
     比上限小的原样送（不放大——放大只多花显存换不出细节）。数值是**最长边**，
     不是短边、也不是面积：一张 4000×3000 配 1024，编出来是 1024×768。 */
  const refCtl = el("div", "gcast-ctl");
  const selRef = makeSelect(node, "参考图最长边上限", "qwen");
  const inRef = el("input"); inRef.type = "number"; inRef.step = STEP; inRef.min = 0; inRef.max = MAX_SIDE;
  inRef.title = "参考图最长边上限（像素，按 32 对齐）。↑↓ 微调，每格 32。"
    + "超过上限的图等比压到上限，没超过的按原尺寸送；两侧都不放大。";
  const refNote = el("div", "gcast-read");
  refCtl.append(selRef.el, inRef);
  const fRef = field("参考尺寸", refCtl, refNote);
  row.append(fRef);

  selRef.setOptions(REF_CAPS.map((v) => ({
    label: v ? `${v} px` : "原尺寸",
    value: String(v),
    note: v ? "最长边" : "不限",
  })));

  /* 画布永远是一个明确尺寸：比例档位或自定义，缺省官方默认 1024² */
  selRatio.setOptions(RATIOS.map((r) => ({ label: r.label, value: r.label, note: `${r.w} x ${r.h}` }))
    .concat([{ label: "自定义", value: "custom" }]));

  function paintCanvas() {
    /* 跟随模式下画布由运行时决定：比例/尺寸/宽高全部停摆并置灰，避免
       「上面显示了 1024²、实际跑的是图片1尺寸」这种两处打架的错觉。 */
    selRatio.disabled = selSize.disabled = inW.disabled = inH.disabled = st.canvas_from_ref;
    if (st.canvas_from_ref) {
      inW.value = ""; inH.value = "";
      canvasNote.innerHTML = `<b>跟随图片1</b> · 长边 ${
        st.ref_long ? `≤ ${st.ref_long} px` : "不限"}，运行时算出`;
      return;
    }
    const w = st.canvas.w, h = st.canvas.h;
    const fam = findRatio(w, h, RATIOS);
    selRatio.value = fam ? fam.label : "custom";
    if (fam) {
      const options = fam.sizes.map((z) => ({ label: `${z.w} x ${z.h}`, value: `${z.w}x${z.h}` }));
      const key = `${w}x${h}`;
      /* 手动改宽高后落在阶梯之外时，把它作为一项补进下拉，别让读出空白 */
      if (!options.some((o) => o.value === key)) {
        options.push({ label: `${w} x ${h}`, value: key, note: "自定义" });
      }
      selSize.setOptions(options);
      selSize.value = key;
      selSize.disabled = false;
    } else {
      selSize.setOptions([{ label: "自定义尺寸", value: "custom" }]);
      selSize.value = "custom";
      selSize.disabled = true;
    }
    inW.value = String(w);
    inH.value = String(h);
    const mp = (w * h / 1e6).toFixed(2);
    canvasNote.innerHTML = `<b>${mp} MP</b> · ${(w / h).toFixed(2)}:1`;
    /* 阶梯封顶 4K UHD 面积，但自定义输入放得更大。不拦，只提醒：面积翻一倍，
       注意力与耗时是平方级上去的。 */
    if (w * h > MAX_PIXELS) {
      canvasNote.insertAdjacentHTML("beforeend", ' · <span class="gcast-warn">面积偏大</span>');
    }
  }

  /* 参考尺寸读出：这一项不改变画布，只决定参考图进编码器时的最大边长。
     没接媒体架时无从谈起，就直接说明白——免得用户以为设置没生效。 */
  function paintRef() {
    const cap = st.ref_cap;
    selRef.value = String(cap);
    inRef.value = String(cap);
    inRef.disabled = !cap;                 // 0 = 原尺寸，此时输入框没有意义
    refNote.innerHTML = cap
      ? `<b>最长边 ≤ ${cap} px</b> · 更大的图等比压下来，更小的原样送`
      : `<b>原尺寸</b> · 不设上限（4K 大图会照原样进编码器）`;
  }

  /* 画布/参考尺寸改动只影响尺寸相关的控件与读出；chips 与编辑器不动 */
  const refresh = () => { paintFollow(); paintCanvas(); paintRead(); paintRef(); };

  selRatio.onchange = (v) => {
    if (v === "custom") { st.canvas = { w: snap(inW.value), h: snap(inH.value) }; refresh(); commit(); return; }
    const fam = RATIOS.find((r) => r.label === v);
    if (!fam) return;
    /* 换比例时留在原来那一档（面积最接近的一档），不要每次跳回最大 */
    const cur = st.canvas.w * st.canvas.h;
    const rung = fam.sizes.reduce((best, z, i) =>
      Math.abs(z.w * z.h - cur) < Math.abs(fam.sizes[best].w * fam.sizes[best].h - cur) ? i : best, 0);
    st.canvas = { w: fam.sizes[rung].w, h: fam.sizes[rung].h };
    refresh(); commit();
  };
  selSize.onchange = (v) => {
    if (v === "custom") return;
    const [w, h] = v.split("x").map(Number);
    st.canvas = { w, h };
    refresh(); commit();
  };
  /* 预设比例下宽高锁在一起：改宽带动高，比例不会漂；只有自定义才各自独立 */
  const activeRatio = () => findRatio(st.canvas.w, st.canvas.h, RATIOS);
  inW.onchange = () => {
    const fam = activeRatio();
    const w = snap(inW.value);
    st.canvas = { w, h: fam ? snap(w / (fam.w / fam.h)) : snap(inH.value) };
    refresh(); commit();
  };
  inH.onchange = () => {
    const fam = activeRatio();
    const h = snap(inH.value);
    st.canvas = { w: fam ? snap(h * (fam.w / fam.h)) : snap(inW.value), h };
    refresh(); commit();
  };
  /* 上下箭头微调：键盘 ↑↓ 自己接。原生在部分浏览器上要等 blur 才派 change，
     那样比例锁、读出与快照全都不跟；自己走一遍 onchange，每格 32（与后端
     对齐粒度一致）。鼠标点 spinner 走原生 → change → 同一条路径。 */
  const nudge = (box) => (e) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    e.stopPropagation();
    box.value = String(snap((+box.value || 0) + (e.key === "ArrowUp" ? STEP : -STEP)));
    box.onchange();
  };
  inW.addEventListener("keydown", nudge(inW));
  inH.addEventListener("keydown", nudge(inH));

  /* 参考尺寸：0 在下拉里是「原尺寸」，输入框里也认 0（清空后打 0 即可回到不限） */
  const refCapOf = (v) => { const n = +v; return n > 0 ? snap(n) : 0; };
  selRef.onchange = (v) => { st.ref_cap = refCapOf(v); refresh(); commit(); };
  inRef.onchange = () => { st.ref_cap = refCapOf(inRef.value); refresh(); commit(); };
  inRef.addEventListener("keydown", nudge(inRef));

  /* 跟随图片1：0 = 用原尺寸画布（上限由 4096 硬顶兜着），与「参考尺寸」同口径 */
  const longCapOf = (v) => { const n = +v; return n > 0 ? snap(n) : 0; };
  cbFollow.onchange = () => { st.canvas_from_ref = cbFollow.checked; refresh(); commit(); };
  inLong.onchange = () => { st.ref_long = longCapOf(inLong.value); refresh(); commit(); };
  inLong.addEventListener("keydown", nudge(inLong));

  /* ---------------- 提示词 / 负面提示词 ----------------
     用富文本编辑器而不是 textarea：框内要标出 @引用 的命中状态、要能 @ 弹
     补全、要能悬停看素材——这三件都是纯文本控件做不到的。 */
  const pWrap = el("div", "gcast-pwrap");
  pWrap.style.cssText = "display:flex; flex-direction:column; gap:6px;";
  const pHead = el("div", "gcast-phead");
  pHead.append(el("div", "gcast-label", "提示词"), refRead);
  /* 外接提示词徽章：prompt_text 接线时显示，提示词框同时转只读镜像 */
  const extBadge = el("span", "gcast-extbadge", "外部提示词 · 只读");
  extBadge.style.display = "none";
  extBadge.title = "正面提示词由外部文本节点提供，此处仅预览；断开连线即恢复面板编辑";
  pHead.append(extBadge);
  const nHead = el("div", "gcast-phead");
  nHead.append(el("div", "gcast-label", "负面提示词"));

  const mkEditor = (cls, placeholder, key) => makePromptEditor({
    cls, placeholder, mode: "qwen", view: viewURL, tokens: vocab,
    onChange: (t) => { st[key] = t; commit(); },
  });
  const edPos = mkEditor("prompt",
    "描述要怎么改这张图。写 @图片1 / @图片2 来区分多张参考图。", "prompt");
  const edNeg = mkEditor("neg", "不想出现的东西（可留空）", "negative_prompt");

  /* 外接提示词（模式 A）：prompt_text 接线即外部文本整体接管正面提示词。
     编辑器转只读镜像——DOM 里放的就是外部值，高亮与悬停预览照常工作；
     st.prompt 的存储永不被污染，断线即恢复面板原文。负面提示词不受影响。 */
  function paintExt() {
    const up = promptNodeOf(node);
    const pw = !!up;
    extBadge.style.display = pw ? "" : "none";
    edPos.setReadonly(pw);
    edPos.setText(pw ? upstreamTextOf(up) : st.prompt);
  }

  pWrap.append(pHead, chips, edPos.node, nHead, edNeg.node);

  const hint = el("div", "gq-hint",
    "参考图全部送出，不必在提示词里点名；@图片N 只是用来在文字里区分它们（会转写成 <imageN>）。"
    + "别名（媒体架槽位里起的名字）可直接当引用写；\\别名 表示不展开。");
  pWrap.append(hint);

  /* 插入点跟随「最后编辑过的那个框」：点 chip 时焦点落在按钮上，用
     activeElement 判断会一律插进正提示词。 */
  let lastEd = edPos;
  edPos.node.addEventListener("focus", () => { lastEd = edPos; });
  edNeg.node.addEventListener("focus", () => { lastEd = edNeg; });
  const insertToken = (t) => { if (!lastEd.readonly()) lastEd.insert(t); };

  function paintRead() {
    headRead.replaceChildren();
    if (st.canvas_from_ref) {
      headRead.append(el("span", null, "输出画面 "), el("b", null, "跟随图片1"));
      return;
    }
    const w = st.canvas.w, h = st.canvas.h;
    headRead.replaceChildren();
    headRead.append(el("span", null, "输出画面 "), el("b", null, `${w}×${h}`),
                    el("span", null, ` · 潜空间 ${w / 16}×${h / 16}`));
  }

  root.append(head, row, pWrap);

  const RACK_HEAD = () => (typeof LiteGraph !== "undefined" && LiteGraph.NODE_TITLE_HEIGHT) || 30;
  function fit() {
    requestAnimationFrame(() => {
      if (node.flags?.collapsed) return;
      /* root 是 height:100%，offsetHeight 只会等于节点现有高度（循环自证）；
         内容超高时溢出进滚动区，scrollHeight 才是真实内容高度。 */
      const contentH = Math.max(root.offsetHeight || 0, root.scrollHeight || 0);
      if (!contentH) return;
      const needH = RACK_HEAD() + 2 + contentH + 6;
      if (needH > (node.size?.[1] || 0) + 1) node.setSize?.([node.size[0], Math.ceil(needH)]);
    });
  }

  function render() {
    renderChips();
    refresh();         // 画布控件 + 尺寸读出
  }

  function load(raw) {
    let d = {};
    try { d = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {}); } catch (_) { d = {}; }
    st.prompt = typeof d.prompt === "string" ? d.prompt : "";
    st.negative_prompt = typeof d.negative_prompt === "string" ? d.negative_prompt : "";
    /* 旧存档里的 images（面板槽位时代）直接丢弃：参考图现在只认媒体架 */
    /* 画布缺省/非法 → 官方默认 1024²；单边合法单边不合法不取半张 */
    const cv = (d.canvas && typeof d.canvas === "object") ? d.canvas : {};
    const cw = Math.round(Number(cv.w) || 0);
    const ch = Math.round(Number(cv.h) || 0);
    st.canvas = (cw >= STEP && ch >= STEP)
      ? { w: snap(cw), h: snap(ch) }
      : { w: 1024, h: 1024 };
    st.canvas_from_ref = !!d.canvas_from_ref;
    /* 跟随长边：缺省/非法 → 1024（后端同口径夹进 [32,4096]，0 = 原尺寸） */
    const lg = Math.round(Number(d.ref_long));
    st.ref_long = Number.isFinite(lg) && lg >= 0 ? (lg > 0 ? snap(lg) : 0) : REF_LONG_DEFAULT;
    /* 参考尺寸：缺省 / 非法 → 与后端 REF_CAP_DEFAULT 同值；0 = 原尺寸。
       旧存档里这项叫 resolution（当时按面积锚定），数值当最长边上限用依然合理。 */
    const capRaw = d.ref_cap != null ? Number(d.ref_cap)
      : (d.resolution != null ? Number(d.resolution) : REF_CAP_DEFAULT);
    st.ref_cap = Number.isFinite(capRaw) ? refCapOf(capRaw) : REF_CAP_DEFAULT;
    edPos.setText(st.prompt);
    edNeg.setText(st.negative_prompt);
    render();
    paintExt();        // 存档里 prompt_text 已接线时，直接进只读镜像
    fit();
  }

  return {
    root, load, fit, render,
    minH: () => 120,
    destroy: () => { clearInterval(rackPoll); try { offRackEvt(); } catch (_) {} edPos.destroy(); edNeg.destroy(); },
  };
}

app.registerExtension({
  name: "cglide.qwenstudio",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_ID) return;
    injectCSS();

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);

      const pinLabel = (slot) => {
        if (!slot) return;
        const key = (slot.name || slot.localized_name || "").toLowerCase().replace(/[\s_]+/g, "");
        if (key === "clip" || key === "剪辑") slot.label = "CLIP";
        else if (key === "vae" || key === "变分自编码器") slot.label = "VAE";
        else if (key === "refs") slot.label = "参考素材";
        else if (key === "prompttext" || key === "外接提示词") slot.label = "外接提示词";
        else if (key === "正面条件") slot.label = "正面条件";
        else if (key === "潜空间") slot.label = "潜空间";
      };
      for (const inp of this.inputs || []) pinLabel(inp);
      for (const out of this.outputs || []) pinLabel(out);

      const data = this.widgets?.find((w) => w.name === "qwen_data");
      if (data) {
        /* 隐藏 data widget：v1/v2 前端各自的隐藏口径都设一遍（同导演台） */
        data.origType = data.origType || data.type;
        data.hidden = true;
        data.options = Object.assign({}, data.options, { hidden: true });
        data.computeSize = () => [0, -4];
        data.computeLayoutSize = () => ({ minHeight: 0, maxHeight: 0, minWidth: 0 });
        data.label = "";
        data.tooltip = "";
        data.serializeValue = data.serializeValue || (() => data.value);
        if (data.element) {
          Object.assign(data.element.style, {
            display: "none", visibility: "hidden", pointerEvents: "none",
            position: "absolute", left: "-99999px", top: "0",
            width: "0px", height: "0px", opacity: "0", zIndex: "-1",
          });
          data.element.hidden = true;
          data.element.tabIndex = -1;
        }
      }

      const ui = buildUI(this);
      this.gq = ui;
      const uiWidget = this.addDOMWidget("qwen_ui", "div", ui.root, { serialize: false, hideOnZoom: false });
      /* minHeight 报固定小值：前端拖拽以 computeSize()（各 widget minHeight 之和）
         做下限钳制，报内容实测峰值会让节点永远收不短。 */
      uiWidget.computeLayoutSize = () => ({ minHeight: ui.minH(), maxHeight: void 0, minWidth: 0 });

      requestAnimationFrame(() => {
        const wrap = ui.root.parentElement;
        if (wrap && wrap.classList.contains("dom-widget")) wrap.style.pointerEvents = "none";
      });

      ui.load(data ? data.value : "");
      this.setSize?.([MIN_W, MIN_H]);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const r = onConfigure?.apply(this, arguments);
      const data = this.widgets?.find((w) => w.name === "qwen_data");
      if (this.gq && data) this.gq.load(data.value);
      return r;
    };

    /* 编辑器的补全列表与预览浮窗挂在 body 上（不能挂在节点里，否则一弹出
       就把节点内排版顶开）；节点删除时一并收走，不然它们会留在页面上。 */
    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      const r = onRemoved?.apply(this, arguments);
      this.gq?.destroy?.();
      return r;
    };

    /* 画布行是两个下拉 + 两个数字框，收得比 MIN_W 更窄就会互相挤掉 */
    const onResize = nodeType.prototype.onResize;
    nodeType.prototype.onResize = function (size) {
      if (size[0] < MIN_W) size[0] = MIN_W;
      return onResize?.apply(this, arguments);
    };
  },
});
