/* ============================================================
 * 导演台：常量、画布与时长算法、面板状态、分镜解析
 * ------------------------------------------------------------
 * 从 csglide_cast.js 顶层整块搬来。这些都是**纯数据或纯函数**——不碰 DOM、
 * 不碰 node、不依赖面板的任何运行时状态，所以搬出来不需要改一个字。
 * ============================================================ */

export const NODE_ID = "CSGlideCastCS";
export const ASSET_SUBFOLDER = "cglide";
export const FPS = 24;
export const MAX_IMAGES = 9, MAX_VIDEOS = 3, MAX_AUDIOS = 3;

/* 文件格式清单 —— 唯一来源。
 *
 * 三张表（EXT_KIND / ACCEPT_VIDEO / ACCEPT_AUDIO / ACCEPT_IMAGE）全部从下面
 * 这三个数组派生，不存在手抄副本：此前媒体架自带一份抄窄了的 ACCEPT_ 副本，
 * 于是「文件对话框里选得到、拖进来却毫无反应」，改格式清单必须两边同步。
 * 现在加一个扩展名只改这一处，选择器、拖拽判定、样式分支自动跟着走。
 *
 * 为什么必须列出扩展名而不是只写 audio/* 通配符：Windows 把 .mkv 注册为
 * video/x-matroska，Chrome 不会把它归入 video/*，于是拖拽可用的 mkv 在选择器
 * 里是灰的——而 mkv 恰是 H.265 渲染的落盘格式；音频同理，.m4b / .opus / .tta
 * 在很多系统上压根没有注册 MIME，只靠通配符它们一个都选不到。
 */
export const VIDEO_EXTS = ["mkv", "mp4", "m4v", "mov", "webm", "avi", "mpg", "mpeg",
                    "ts", "mts", "m2ts", "wmv", "flv", "ogv", "3gp"];
export const AUDIO_EXTS = ["mp3", "m4a", "m4b", "aac", "ac3", "ogg", "oga", "opus",
                    "spx", "wma", "mka", "amr", "3ga", "ra",
                    "wav", "wave", "flac", "aiff", "aif", "aifc", "caf",
                    "w64", "pcm", "au", "snd", "ape", "wv", "tta"];
export const IMAGE_EXTS = ["png", "jpg", "jpeg", "webp", "avif", "bmp", "tif", "tiff"];

export const acceptOf = (exts, prefix) =>
  prefix + "," + exts.map((e) => "." + e).join(",");

export const ACCEPT_VIDEO = acceptOf(VIDEO_EXTS, "video/*");
export const ACCEPT_AUDIO = acceptOf(AUDIO_EXTS, "audio/*");
export const ACCEPT_IMAGE = acceptOf(IMAGE_EXTS, "image/*");

/* 拖拽时文件带的 MIME 可能是空的（系统没注册这种扩展名），MIME 判定落空之后
 * 回落到这张表按扩展名认类型。表里没有就认不出来。 */
export const EXT_KIND = Object.assign(
  {},
  Object.fromEntries(VIDEO_EXTS.map((e) => [e, "video"])),
  Object.fromEntries(AUDIO_EXTS.map((e) => [e, "audio"])),
  Object.fromEntries(IMAGE_EXTS.map((e) => [e, "image"])),
  { gif: "image" },   /* 动图归图片槽：它会被 PIL 按静态图读，不进音频/视频侧 */
);

/* 可疑图片格式 —— MIME 存在（于是 fileKind() 判为 image、会被收下并上传），
 * 但参考图最终要由**后端 PIL 打开**，这些扩展名在后端根本没有解码器：收下
 * 也只会一路传到模型端才抛异常，或缩略图先裂成 ⚠。
 *
 * 清单来源：ComfyUI 内嵌解释器（python_embeded）PIL 11.3.0 实测
 * `Image.registered_extensions()`——heic / heif / svg / psb / exr / hdr 均无
 * 解码器登记。**反例（不要拉进来）**：psd / tga / dds / ico / jp2 / j2k / eps /
 * wmf / emf 实测**都能解码**，凭印象拉黑会误伤这些正常文件。
 *
 * SVG 单独说一句：浏览器能把它缩略成图，看着"能用"，但 PIL 永远读不了它，
 * 所以照样要拦——它败在后端，不表现在前端。
 * 与媒体架同源：这张表只此一份，媒体架 import 共用，不要再抄副本。 */
export const PIL_BAD_IMAGE = {
  heic: "HEIC", heif: "HEIF", svg: "SVG", psb: "Photoshop 大文档（PSB）",
  exr: "OpenEXR", hdr: "Radiance HDR",
};

/* 传入 File（或任何 { type, name }）：命中可疑格式返回可读名（如 "HEIC"），
 * 否则返回 ""。判定以扩展名为准，MIME 仅在文件名没有扩展名时兜底——某些
 * HEIC 导出不带后缀，这类只能靠 type 认。 */
export function badImageKind(f) {
  const name = (f?.name || "").toLowerCase();
  const t = (f?.type || "").toLowerCase();
  let ext = name.includes(".") ? name.split(".").pop() : "";
  if (!ext) {
    if (t === "image/svg+xml") ext = "svg";
    else if (/^image\/(x-)?(heic|heif)$/.test(t)) ext = "heif";
  }
  return PIL_BAD_IMAGE[ext] || "";
}

/* Ratio families. The first size in each ladder is the canvas H3's own
 * adapt_canvas() would pick; everything below it holds the same aspect on a
 * shorter edge, every axis a multiple of 32. */
export const MAX_PIXELS = 768 * 1344;

export const RATIOS = [
  { label: "21:9", w: 1536, h: 672 },
  { label: "16:9", w: 1344, h: 768 },
  { label: "3:2",  w: 1152, h: 768 },
  { label: "4:3",  w: 1024, h: 768 },
  { label: "1:1",  w: 768,  h: 768 },
  { label: "3:4",  w: 768,  h: 1024 },
  { label: "2:3",  w: 768,  h: 1152 },
  { label: "9:16", w: 768,  h: 1344 },
];

export const SHORT_EDGES = [768, 704, 640, 576, 512, 448, 384, 352, 320];

/* 阶梯按「短边取遍 SHORT_EDGES、长边按比例推、整体不超过 maxPixels」生成。
   shortEdges / maxPixels / maxSide 可覆盖：不同模型的画布上限不一样（H3 是
   768×1344 的面积上限，Qwen 编辑是 4K 级起步），机制共用一份，数值各给各的。
   maxSide 是单边上限，缺省不限（H3 的面积上限已经足够收住长边）；超宽比例
   （21:9 之类）单看面积是收不住的——短边 1920 推出来的长边有 4480。 */
export function sizeLadder(base, shortEdges = SHORT_EDGES, maxPixels = MAX_PIXELS,
                           maxSide = Infinity) {
  const r = base.w / base.h;
  const seen = new Set(), out = [];
  for (const shortEdge of shortEdges) {
    let w, h;
    if (r >= 1) { h = shortEdge; w = Math.max(32, Math.round((shortEdge * r) / 32) * 32); }
    else { w = shortEdge; h = Math.max(32, Math.round((shortEdge / r) / 32) * 32); }
    if (w * h > maxPixels + 1) continue;
    if (w > maxSide || h > maxSide) continue;
    const key = `${w}x${h}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ w, h });
  }
  if (!out.some((o) => o.w === base.w && o.h === base.h)) out.unshift({ w: base.w, h: base.h });
  return out;
}

RATIOS.forEach((r) => { r.sizes = sizeLadder(r); });

/* Named extra presets that are NOT produced by the short-edge ladder.
 * "700阶段" is a MiniMax resolution stage below the native 768p tiers
 * (e.g. 768x448), NOT a 0.7MP label, so it is added explicitly. */
export const EXTRA_SIZES = {
  "16:9": [{ w: 768, h: 448, stage: "700阶段" }],
};
for (const r of RATIOS) {
  for (const s of (EXTRA_SIZES[r.label] || [])) {
    if (!r.sizes.some((z) => z.w === s.w && z.h === s.h)) {
      r.sizes.push({ w: s.w, h: s.h, stage: s.stage });
    }
  }
  r.sizes.sort((a, b) => (b.w * b.h) - (a.w * a.h));
}

export function findRatio(w, h, ratios = RATIOS) {
  for (const r of ratios) if (r.sizes.some((s) => s.w === w && s.h === h)) return r;
  return null;
}

/* Reference sizing: a free LONG-edge value in px (32-aligned), plus a
 * "match" switch that tracks the canvas long edge. Images clamp at 4096;
 * video has no upper cap (Infinity makes the clamp a no-op). "跟随画布" = match. */
export const REF_STEP = 32, REF_MIN = 32;
export const REF_IMAGE_MAX = 4096, REF_VIDEO_MAX = Infinity;
/* 参考视频采样帧率：24=不抽帧；往低调（如 12/8）在时长不变时减少参考帧数、
 * 节省显存，但会降低参考锚定精度。与后端 _clamp_ref_fps 的 4..24 保持一致。 */
export const REF_FPS_MIN = 4, REF_FPS_MAX = 24;
export const clampRefFps = (v) => {
  const n = Math.round(+v);
  return Number.isFinite(n) ? Math.max(REF_FPS_MIN, Math.min(REF_FPS_MAX, n)) : REF_FPS_MAX;
};

/* 「放大重绘」（二采/三采）条件的参考档位倍数。与后端 _ref_hd_scale 同口径：
   1 = 跟随主档位、不额外编码（接入前的既有行为）；>1 = 参考素材按该倍数重新
   编码，细节更多但每级都要重跑一遍视觉编码器。 */
export const REF_SCALE_MIN = 1, REF_SCALE_MAX = 4, REF_SCALE_STEP = 0.25;
export const clampRefScale = (v) => {
  const n = Math.round(+v / REF_SCALE_STEP) * REF_SCALE_STEP;
  return Number.isFinite(n) ? Math.max(REF_SCALE_MIN, Math.min(REF_SCALE_MAX, +n.toFixed(2))) : REF_SCALE_MIN;
};

/* MiniMax H3 语义桥强度：0 = 关闭，0~1 连续可调。与后端 _bridge_alpha 同口径，
 * 非法值落 0。 */
export const BRIDGE_ALPHA_MIN = 0, BRIDGE_ALPHA_MAX = 1, BRIDGE_ALPHA_STEP = 0.05;
export const clampBridgeAlpha = (v) => {
  const n = +v;
  return Number.isFinite(n) ? Math.max(BRIDGE_ALPHA_MIN, Math.min(BRIDGE_ALPHA_MAX, +n.toFixed(2))) : 0;
};

/* 语义桥作用范围：full 全量（整体）/ text 掩码（仅提示词）。非法值落 full。 */
export const BRIDGE_MODES = [["full", "全量"], ["text", "掩码"]];
export const clampBridgeMode = (v) => (v === "text" ? "text" : "full");

/* 语义桥适配器名：空串 = 默认（v1）。加载工程时只做格式校验（.safetensors）；
 * 「这台机器上有没有」由后端 sanitize_adapter_name 兜底回默认，前端拿到
 * 模型清单后若发现选中的不在其中，会归回默认并提示。 */
export const bridgeAdapterValid = (v) =>
  !v || (typeof v === "string" && v.toLowerCase().endsWith(".safetensors"));

/* 下拉里显示的名字：去扩展名、去常见前缀。超过 14 字截断加省略号——
 * 新版前端的 select 会把过长的选中项折行，把设置行撑高（行高必须固定）。 */
export const BRIDGE_DEFAULT_FILE = "MiniMaxH3_SemanticBridge_v1.safetensors";
export const bridgeModelLabel = (name) => {
  if (!name || name === BRIDGE_DEFAULT_FILE) return "默认（v1）";
  const base = name
    .replace(/\.safetensors$/i, "")
    .replace(/^semantic_bridge_/i, "");
  return base.length > 14 ? base.slice(0, 14) + "…" : base;
};

/* Normalise a stored value (from an old dropdown, or a free number) back to
 * "match" or a snapped/clamped number. Old tier strings like "512" degrade to
 * a plain number transparently; "max" still maps to the cap. */
export const normRefSize = (v, cap, fallback) => {
  if (v === "match") return "match";
  if (v === "max") return Number.isFinite(cap) ? cap : REF_IMAGE_MAX;
  const n = Number(v);
  if (Number.isFinite(n) && n > 0) {
    return Math.max(REF_MIN, Math.min(cap, Math.round(n / REF_STEP) * REF_STEP));
  }
  return fallback;
};

/* Paint one ref-size control: checkbox on ⇄ "跟随画布" plus a number input.
 * The input always stays live so its native up/down steppers work exactly like
 * the canvas width/height; editing it while following auto-flips to a fixed
 * size. A note describes the effective long edge. */
export const renderRefControl = (chk, input, note, value, canvasLong, cap) => {
  const isMatch = value === "match";
  chk.checked = isMatch;
  input.value = isMatch ? canvasLong : value;
  /* 只读出有效长边：跟随状态已由勾选框 + "跟随画布" 小标题表达，note 再写一遍
     既重复又会让视频卡那一行放不下（会被省略号截断）。 */
  note.innerHTML = `<b>长边 ${isMatch ? canvasLong : value}px</b>`;
};

/* Frame counts on the 17k+5 grid, one entry per integer second band.
   These are quick-pick presets only — NOT a hard cap; the custom input beside
   the dropdown accepts any length and re-aligns it to the grid. */
export const LENGTH_PRESETS = [124, 243, 362, 481, 600, 719];  // ≈ 5/10/15/20/25/30s

export const ALL_TOKENS = [
  "@first", "@last",
  ...Array.from({ length: MAX_IMAGES }, (_, i) => `@图片${i + 1}`),
  ...Array.from({ length: MAX_VIDEOS }, (_, i) => `@视频${i + 1}`),
  ...Array.from({ length: MAX_VIDEOS }, (_, i) => `@视频音频${i + 1}`),
  ...Array.from({ length: MAX_AUDIOS }, (_, i) => `@音频${i + 1}`),
];

/* 新建节点的初始高度。它是初值，80ms 后 fitNode() 会按真实内容收掉留白——
   所以取一个"略高于实测峰值"的值即可，取大了新建时会先闪一个超高节点。
   1330 是七段纵向平铺那个年代的实测值；改成指挥条 + 双栏 + 总览轴 + 落地行
   四段之后，实测峰值约 660（fl2va 模式下首尾帧卡展开）。 */
export const MIN_H_REF = 700;    // initial node height; the node itself is user-resizable below this
/* 节点最窄下限：设置卡（右栏 540）+ 监视器（左栏）至少留出可用的观看宽度。
   低于此值右侧设置卡会被挤没、左侧监视器也窄到没法看——这是「允许收窄」的
   合理终点，原 900 是拍脑袋的保守值，把用户能收窄的幅度白白扣掉了。 */
export const MIN_PANEL_W = 560;
/* 手动拖拽模式下报给框架的最小面板高度。前端拖拽以 node.computeSize()
   （=各 widget minHeight 之和）做下限钳制，报内容实测峰值会让节点永远拖不到
   比内容更矮。固定小值 + maxHeight 无限，收短后内容靠 .gcast 滚动，不塌陷。 */
export const MIN_FLOOR_H = 120;
/* Hard safety cap: no matter what a measurement returns, the node is never
   grown past this. A runaway fitNode (or a stale minHeight) used to stretch
   the bottom to infinity; this bounds it so the worst case is a large but
   finite panel the user can drag down. The prompt scrolls inside it, so a
   tall cap here costs nothing. */
export const MAX_NODE_H = 6000;
export const PROMPT_H_REF = 160, PROMPT_H_FL = 300;   // fl2va has only two slots, so the space goes to the prompt

export const alignFrames = (n) => { n = Math.max(5, Math.round(n)); while (n % 17 !== 5) n++; return n; };
/* 最近的 17k+5 帧。alignFrames 只往上顶，用在「已有帧数、把它顶到合法」的地方；
   而自定义时长是「用户报一个秒数、吸附到最近的合法格」，方向该由远近决定。
   这个区别在上下按钮上是决定性的：按钮每次只把秒数改 1，一律往上顶会一次跨两格
   （141 帧 5.88s 按上 → 165 帧再顶到 175），取最近格点才是一格（→158）。 */
export const nearestFrames = (n) =>
  5 + 17 * Math.max(0, Math.round((Math.max(5, Math.round(n)) - 5) / 17));
export const fmtSecs = (s) => (Math.round(s * 100) / 100).toFixed(2) + "s";
/* 秒 ↔ 帧：自定义时长输入以「秒」为单位。帧数只有落在 17k+5 网格上才合法，
   而用户想说的是「我要 12 秒」——吸附交给 nearestFrames。
   回显只给整数秒（显示层简化，不让 10.13 这类小数挤占本就 42px 的窄框），
   精确值由右侧读出位以「N 帧 · 10.13s」给出，内部换算仍走未取整的
   frames/24——显示取整不参与任何计算，两处换算集中在这里，避免口径漂移。
   空/非法输入回退 124 帧（与旧行为一致）；数值合法但越界（在下限附近按 ↓ 时浏览器
   会给出 0）交给 nearestFrames 夹到下限 5——不能回退 124，否则按 ↓ 会突然跳回 5.17s。
   NaN 必须先挡（alignFrames/nearestFrames 都不认 NaN）。 */
export const secsOfFrames = (f) => Math.round(f / FPS);
export const framesOfSecs = (s) => {
  if (s == null || String(s).trim() === "") return 124;      // 空 → 回落默认
  const v = +s;
  return Number.isFinite(v) ? nearestFrames(v * FPS) : 124;  // 越界由 nearestFrames 夹到 ≥5
};

/* 本段当前的承接重叠帧数（头部 trim）。导演台「时长」卡与时间轴块宽要统一
   按**净长**口径换算（20261013：卡上 345 帧/14 秒、轴上块却 12 秒，用户当然
   认为对不上 —— 块画的是净长，头部 56 帧是承接上一段尾巴的重叠，显示在
   上一段那侧）。取值优先读档案里这一段的 trim_frames（渲染登记时定的实际
   值，监视器缓存里有）；档案还没有就退回本段承接窗口设置 cont.frames。
   第 1 段 / 未承接 = 0。 */
export const headOverlapFrames = (monitor, st) => {
  let t = 0;
  try {
    const cid = (st && st.chain && st.chain.clip) || "";
    const row = (cid && monitor && monitor.clipRowOf) ? monitor.clipRowOf(cid) : null;
    if (row) t = Math.round(Number(row.trim_frames) || 0);
  } catch (_) { t = 0; }
  if (!t) t = Math.max(0, Math.round(Number(st && st.cont && st.cont.frames) || 0));
  return Math.max(0, t);
};

/* ---------------------------------------------------------------- state */

/* 重抽档位 / 锚点选法 —— 段级的生成参数。
   值与后端 banzhang_h3_pin.REDO_STRIDE 一一对应（单位是**帧**，H3 潜空间
   一格 ≈ 3.4 帧，所以间隔小于 4 帧做不到、会被吸附成整段钉住）：
     保留动作 = 隔 22 帧钉一次（约 6 个锚，动作轨迹留住，只换细节）
     只换画质 = 隔 7 帧钉一次（约一半钉住）
     重画     = 不钉骨架，只钉头尾（内容全部重新生成）
   定义在模块级是因为两边都要用：「这一段」设置卡拿它渲染下拉选项，白名单
   parseInitial 拿它做校验 —— 不校验就会「选了档位、存一次、重载又回到默认」，
   那是最难查的一类：看着选项在，其实从没生效过。 */
export const REDO_MODES = [
  ["保留动作", "隔 22 帧钉一次 —— 动作轨迹留住，只换细节"],
  ["只换画质", "隔 7 帧钉一次 —— 画面几乎不动，只提升质感"],
  ["重画", "不钉骨架，只钉头尾 —— 内容全部重新生成"],
  ["完整重拍", "什么都不钉，整段从零重拍 —— 尾部对不上旧版，后面各段要跟着重跑"],
];
export const REDO_ANCHORS = [
  ["等间隔", "每隔 N 帧钉一帧。可能钉在静止的过渡帧上。"],
  ["按运动强度", "挑动作变化最大的那些帧钉 —— 锚落在关键动作上，连贯性更强。"],
];
export const REDO_MODE_VALUES = REDO_MODES.map(([v]) => v);
export const REDO_ANCHOR_VALUES = REDO_ANCHORS.map(([v]) => v);
/* 重抽 UI 简化（20261012）：4 个后端档位在面板上合并成 2 个主档。
   redo_mode 仍是 4 档字符串（REDO_MODE_VALUES），存储 / 白名单 / 后端 stride
   全不动；面板只暴露「微调 / 重来」两档，细分（只换画质 / 完整重拍）收进
   高级抽屉。redoGroupOf 把档案档位翻译回主档名，redoDefaultOf 反向给主档
   一个默认落点 —— 写入档位永远在这两个函数之间往返，不会凭空多出第五档。 */
export const REDO_GROUPS = [
  ["微调", ["保留动作", "只换画质"]],
  ["重来", ["重画", "完整重拍"]],
];
export function redoGroupOf(mode) {
  for (const [g, modes] of REDO_GROUPS) if (modes.indexOf(mode) >= 0) return g;
  return "微调";
}
export function redoDefaultOf(group) {
  /* 20261013 用户定：「重来」组的出厂档 = **完整重拍**（高级 · 尾部 = 不钉）——
     重来就是整段从零重拍，不该默认还钉着尾巴。「微调」组维持保留动作。 */
  if (group === "重来") return "完整重拍";
  const row = REDO_GROUPS.find(([g]) => g === group);
  return row ? row[1][0] : "保留动作";
}
/* 钉死的头 / 尾帧数上限。H3 的片段长度远小于它 —— 这个上限只挡输入框手滑
   （多按两个 0 把浮点撑爆），不是业务边界。 */
export const PIN_FRAMES_MAX = 4096;

export function blankState() {
  const mk = (n) => Array.from({ length: n }, () => ({}));
  return {
    mode: "ref2va",
    /* 出厂默认画布：576×320（16:9 阶梯短边 320 档，findRatio 可匹配）。 */
    width: 576, height: 320,
    ratio: "16:9",        /* locked aspect-ratio family; "custom" unlinks width/height */
    length: 243,
    /* 重抽档位 / 骨架锚点选法。渲染登记时写进段落档案，画布上的「🧷 H3 钉帧」
       节点默认「跟随档案」读它 —— 档位只在一处选，不会两处填不一致。
       保留动作 = 隔 22 帧钉一次；只换画质 = 隔 7 帧；重画 = 只钉头尾；
       完整重拍 = 什么都不钉（骨架、头尾全放开），整段从零重拍。
       出厂默认 = 完整重拍（20261013 用户定：高级 · 尾部默认「不钉」，
       redoDefaultOf("重来") 同口径）。 */
    redo_mode: "完整重拍",
    redo_anchor: "等间隔",
    /* 本段钉死的头 / 尾帧数（潜空间帧）。尾部 22 帧是「重抽这一段、后面各段
       一个都不用重跑」的物理判据 —— 下一段的引导头就是它。0 = 不钉。
       （档位为「完整重拍」时骨架/头尾全不钉，这两个数不参与。） */
    pin_head: 0,
    pin_tail: 0,
    ref_image_size: "match",
    ref_video_size: "768",
    ref_video_fps: 24,
    /* 「放大重绘」（二采/三采共用）条件的参考档位倍数。1 = 跟随主档位。 */
    ref_scale_hd: 1,
    /* MiniMax H3 语义桥：跨架构语义蒸馏适配器。alpha=0 完全关闭；>0 时按
     * bridge_mode 选范围：full 全量（整条条件序列一起平移，图文语义一致）/
     * text 掩码（只动文本 token，视觉块原样保留、参考画面更稳）。
     * bridge_adapter 选插件目录 semantic_bridge/ 下的模型：空串 = 默认（v1）。
     * 默认关闭，需手动开启。 */
    bridge_adapter: "",
    bridge_alpha: 0,
    bridge_mode: "full", /* 作用范围：full 全量 / text 掩码（仅文本 token） */
    pace: 2.5,          /* legacy: the speech-budget slider is gone, kept so old .h3.json still parses */
    prompt: "",
    slots: { first: {}, last: {}, images: mk(MAX_IMAGES), videos: mk(MAX_VIDEOS), audios: mk(MAX_AUDIOS) },
    /* CONTINUE FROM: the previous clip's tail, anchored at frame 0 of this one.
     * Deliberately NOT a reference slot - it takes no @token, never appears in
     * the presentation, and works in both modes. Continuing is orthogonal to
     * first/last vs omni, so it is a section rather than a third mode. */
    cont: {},
    /* 段落档案归属 —— 与「工程」合一，只有一套概念：
     *   name = 工程名（= 片名，output/h3_chain/<片名>/）
     *   shot = 本分段的唯一 id，后台按它认段（重抽复用同一段，不重复新建）
     *   clip = 段落 id，通常留空交给后台按 shot 匹配
     * name 空 = 不归档，导演台照旧单段工作、不产生任何档案文件。 */
    chain: { name: "", clip: "", shot: "" },
  };
}

/* Whitelisted parse. Anything not named here is dropped on reload — on purpose.
 * Adding a new stored field means adding it HERE too, or it vanishes silently. */
export function parseInitial(raw) {
  const out = blankState();
  let d = {};
  try { d = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {}); } catch (e) { d = {}; }
  if (!d || typeof d !== "object") d = {};

  if (d.mode === "fl2va") out.mode = "fl2va";
  if (Number.isFinite(+d.width)) out.width = +d.width;
  if (Number.isFinite(+d.height)) out.height = +d.height;
  if (d.ratio === "custom" || RATIOS.some((r) => r.label === d.ratio)) {
    out.ratio = d.ratio;
  } else {
    /* Pre-ratio saves: recover the family from the canvas size, falling back
     * to "custom" when the dimensions match no preset ladder. */
    out.ratio = (findRatio(out.width, out.height) || {}).label || "custom";
  }
  if (Number.isFinite(+d.length)) out.length = alignFrames(+d.length);
  out.ref_image_size = normRefSize(d.ref_image_size, REF_IMAGE_MAX, "match");
  out.ref_video_size = normRefSize(d.ref_video_size, REF_VIDEO_MAX, "768");
  out.ref_video_fps = clampRefFps(d.ref_video_fps);
  out.ref_scale_hd = clampRefScale(d.ref_scale_hd);
  /* 语义桥适配器名：只收语义桥卡片下拉里出现过的名字，其余一律归为默认。
     保存的工程/工作流拿到别的机器上跑时，那台机器不一定有同名模型——
     归默认比渲染时炸掉友好；缺了哪个读出位会提示。 */
  out.bridge_adapter = bridgeAdapterValid(d.bridge_adapter) ? String(d.bridge_adapter) : "";
  out.bridge_alpha = clampBridgeAlpha(d.bridge_alpha);
  out.bridge_mode = clampBridgeMode(d.bridge_mode);
  if (Number.isFinite(+d.pace) && +d.pace > 0) out.pace = Math.min(4, Math.max(1, +d.pace));
  if (typeof d.prompt === "string") out.prompt = d.prompt;

  const s = (d.slots && typeof d.slots === "object") ? d.slots : {};
  const image = (o) => (o && o.file) ? { file: String(o.file) } : {};
  const timed = (o, withAudio) => {
    if (!o || !o.file) return {};
    const r = { file: String(o.file) };
    if (Number.isFinite(+o.start)) r.start = +o.start;
    if (Number.isFinite(+o.end)) r.end = +o.end;
    if (Number.isFinite(+o.dur)) r.dur = +o.dur;
    if (withAudio && o.audio) r.audio = true;
    return r;
  };

  out.slots.first = image(s.first);
  out.slots.last = image(s.last);
  for (let i = 0; i < MAX_IMAGES; i++) out.slots.images[i] = image((s.images || [])[i]);
  for (let i = 0; i < MAX_VIDEOS; i++) out.slots.videos[i] = timed((s.videos || [])[i], true);
  for (let i = 0; i < MAX_AUDIOS; i++) out.slots.audios[i] = timed((s.audios || [])[i], false);

  const c = (d.cont && typeof d.cont === "object") ? d.cont : {};
  if (c.mode === "latent") {
    /* 潜空间续写：上一片段的采样潜空间。来源二选一：引用的存档文件
       （latent_file，相对 output 目录），或 context_latent 端口接线。
       frames = 续写画面窗口；声音始终延续（这正是该档位的意义）。 */
    out.cont = { mode: "latent" };
    if (Number.isFinite(+c.frames)) out.cont.frames = +c.frames;
    if (c.latent_file) out.cont.latent_file = String(c.latent_file);
    /* 源视频输出（相对 output 的成片路径）：潜空间档下兼作「Glide 续接合成」
       的源片，随工程保存/载入，后端从「源视频」端口吐出。
       老工程曾把它存在 cont.file（档位共用期的残留），载入时归位。 */
    if (c.latent_video) out.cont.latent_video = String(c.latent_video);
    else if (c.file) out.cont.latent_video = String(c.file);
    /* UI-only（后端白名单丢弃）：预览用的同批视频输出，仅展示。 */
    if (c.latent_preview && typeof c.latent_preview === "object" && c.latent_preview.name) {
      out.cont.latent_preview = {
        name: String(c.latent_preview.name),
        subfolder: String(c.latent_preview.subfolder || ""),
        type: String(c.latent_preview.type || "output"),
      };
    }
  } else if (c.file) {
    out.cont = { file: String(c.file) };
    /* 续接窗口。文件档的窗口真正生效在 start/end 跨度上，frames 是
       「还没有跨度时」的兜底；两个都要留，否则重载一次窗口就丢了。 */
    if (Number.isFinite(+c.frames) && +c.frames > 0) out.cont.frames = +c.frames;
    if (Number.isFinite(+c.start)) out.cont.start = +c.start;
    if (Number.isFinite(+c.end)) out.cont.end = +c.end;
    if (Number.isFinite(+c.dur)) out.cont.dur = +c.dur;
    if (c.audio) out.cont.audio = true;
    if (c.flatten) out.cont.flatten = true;
    /* UI-only, and dropped by the Python whitelist: what the guide's chroma is,
       so the 4:2:0 note survives a reload instead of vanishing until the file
       is re-adopted. */
    if (c.chroma) out.cont.chroma = String(c.chroma);
    if (c.codec) out.cont.codec = String(c.codec);
  }

  /* 段级生成参数：重抽档位 / 锚点选法 / 钉死的头尾帧数。
     必须走白名单收下 —— 这三个原本只有档位两个在面板上，却从来没被 parseInitial
     读回：存档里写了、重载就回默认值，面板上看着选了、实际下一轮又变回「保留动作」。
     值域外的输入一律退回默认，别把脏值喂给后端。 */
  if (REDO_MODE_VALUES.indexOf(String(d.redo_mode)) >= 0) out.redo_mode = String(d.redo_mode);
  if (REDO_ANCHOR_VALUES.indexOf(String(d.redo_anchor)) >= 0) out.redo_anchor = String(d.redo_anchor);
  if (Number.isFinite(+d.pin_head)) {
    out.pin_head = Math.max(0, Math.min(PIN_FRAMES_MAX, Math.round(+d.pin_head)));
  }
  if (Number.isFinite(+d.pin_tail)) {
    out.pin_tail = Math.max(0, Math.min(PIN_FRAMES_MAX, Math.round(+d.pin_tail)));
  }

  /* 段落档案：只收 name / clip 两个字符串，其余一律丢弃。
     老工程没有这个字段 → 保持空白，行为逐字不变（不归档）。 */
  const ch = (d.chain && typeof d.chain === "object") ? d.chain : {};
  out.chain = {
    name: typeof ch.name === "string" ? ch.name : "",
    clip: typeof ch.clip === "string" ? ch.clip : "",
    shot: typeof ch.shot === "string" ? ch.shot : "",
  };
  return out;
}

/* Same ordinal rules as the tokenizer: images, then videos (a soundtrack's
 * <Audio j> lands immediately before its <Video k>), then standalone audio.
 * Ordinals are 1-based per type and count only filled slots.
 * slots: 展示用的合并槽位视图（面板 + 媒体架），缺省用面板自身槽位。
 * 数组长度动态——媒体架槽位按需生长，空位/未选中位传 null 即可跳过。 */
export function presentation(st, slots) {
  const rows = [], tags = {};
  if (st.mode === "fl2va") {
    let i = 0;
    if (st.slots.first.file) { i++; tags["@first"] = `<Picture ${i}>`; rows.push({ tag: `<Picture ${i}>`, from: "first frame", kind: "image" }); }
    if (st.slots.last.file)  { i++; tags["@last"]  = `<Picture ${i}>`; rows.push({ tag: `<Picture ${i}>`, from: "last frame",  kind: "image" }); }
    return { rows, tags };
  }
  const view = slots || st.slots;
  let i = 0, j = 0, k = 0;
  view.images.forEach((s, n) => {
    if (!s || !s.file) return; i++;
    tags[`@图片${n + 1}`] = `<Picture ${i}>`;
    rows.push({ tag: `<Picture ${i}>`, from: `图片 ${n + 1}`, kind: "image", token: `@图片${n + 1}` });
  });
  view.videos.forEach((s, n) => {
    if (!s || !s.file) return;
    if (s.audio) {
      j++; tags[`@视频音频${n + 1}`] = `<Audio ${j}>`;
      rows.push({ tag: `<Audio ${j}>`, from: `视频 ${n + 1} 声音`, kind: "audio", token: `@视频音频${n + 1}` });
    }
    k++; tags[`@视频${n + 1}`] = `<Video ${k}>`;
    rows.push({ tag: `<Video ${k}>`, from: `视频 ${n + 1}`, kind: "video", token: `@视频${n + 1}` });
  });
  view.audios.forEach((s, n) => {
    if (!s || !s.file) return; j++;
    tags[`@音频${n + 1}`] = `<Audio ${j}>`;
    rows.push({ tag: `<Audio ${j}>`, from: `音频 ${n + 1}`, kind: "audio", token: `@音频${n + 1}` });
  });
  return { rows, tags };
}

/* ---- 媒体架合并视图 + 严格按需过滤（与 csglide_cast.py 同规则镜像） ----
   上游定位（媒体架 / 外接文本源）收口在 bz_rack.js，媒体架与 Qwen 编辑台
   都在用；这里再导出一遍，那两个文件的 import 不用动。 */
export { promptNodeOf, upstreamTextOf } from "./bz_rack.js";

/* 「当前段」的规范色 —— 定义在成片监视器里（时间轴是它最醒目的一处），这里
   只是转出去给媒体架用。三个节点必须同一个值，所以只准有一份定义。 */
export { CUR_SEG, CUR_SEG_DIM } from "./csglide_film.js";

/* 提示词提到的 @标签 才送；一个都没提就一个都不送（严格按需）。
   未提到的位置保留为 null——编号位置不变，presentation 只数已填槽位。 */
export function filterUsedByPrompt(slots, prompt) {
  const nums = (re) => new Set([...prompt.matchAll(re)].map((m) => +m[1] - 1));
  const keep = {
    images: nums(/@图片(\d+)/g),
    videos: new Set([...nums(/@视频音频(\d+)/g), ...nums(/@视频(\d+)/g)]),
    audios: nums(/@音频(\d+)/g),
  };
  /* 超过各类型上限时按编号取前 N 个，其余丢弃：<Picture 10> / <Video 4> 这类
     标签模型不认，送进去只会静默失效（图没生效却没报错）。截断规则与
     csglide_cast.py 的 _filter_refs_by_prompt 必须一致。 */
  const caps = { images: MAX_IMAGES, videos: MAX_VIDEOS, audios: MAX_AUDIOS };
  for (const k of Object.keys(keep)) {
    if (keep[k].size > caps[k]) {
      keep[k] = new Set([...keep[k]].sort((a, b) => a - b).slice(0, caps[k]));
    }
  }
  const any = Object.values(keep).some((s) => s.size > 0);
  const out = {};
  for (const k of ["images", "videos", "audios"]) {
    /* 独立音频不参与按需过滤：与 csglide_cast.py 的 _filter_refs_by_prompt 同口径。
       官方 MiniMaxH3ReferenceToVideo 对 ref_audios 无条件发送，导演台原先也过滤，
       于是「摆好了音频、提示词没写 @音频N」就静默丢弃——用户只看到音频没生效。
       音频是配乐/音色这类整体氛围参考，剧本里未必逐句提到，过滤它省不了多少
       显存却极易误伤。仍受 MAX_AUDIOS 上限约束。 */
    if (k === "audios") {
      const all = slots[k].map((s, i) => (s && s.file ? i : -1)).filter((i) => i >= 0);
      const use = new Set(all.slice(0, MAX_AUDIOS));
      out[k] = slots[k].map((s, i) => (use.has(i) ? s : null));
      continue;
    }
    out[k] = any ? slots[k].map((s, i) => (keep[k].has(i) ? s : null)) : [];
  }
  return out;
}

/* ---- prompt check ---------------------------------------------------
 * H3 always generates a soundtrack for the full duration, so whatever the
 * prompt leaves unsaid gets invented -- which is where mumbling comes from.
 * Counting words against a pace was the wrong measure: a well-paced line
 * still leaves seconds of unassigned audio at the end of the shot. What
 * actually stopped it in testing was structural, so this looks for the
 * structure: named fields, a speaker ID, mouths closed after the line, and
 * a soundscape that covers the whole clip.
 */


/* Shot markers, official format: "[Shot 2] At 00:03.500," -- Shot 1 carries no
 * timestamp and starts at zero. A shot runs until the next one starts, or to
 * the end of the clip. "At 3.5s," and "At 00:03," are tolerated too, since the
 * guide's MM:SS.mmm is not what people type by hand.
 *
 * The END of a shot is never stated, so it is always inferred. That is the
 * format's own rule, not a guess: shots are contiguous inside one generation. */
export const SHOT_RE = /\[\s*shot\s*(\d+)\s*\]([^\n]{0,90})/gi;
export const AT_MMSS = /\bat\s+(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?/i;
export const AT_SECS = /\bat\s+(\d{1,3}(?:[.,]\d+)?)\s*s\b/i;

export function parseShots(prompt, total) {
  const p = prompt || "";
  const marks = [];
  let m;
  SHOT_RE.lastIndex = 0;
  while ((m = SHOT_RE.exec(p)) !== null) {
    const tail = m[2] || "";
    const tailAt = m.index + (m[0].length - tail.length);
    const a = AT_MMSS.exec(tail), b = a ? null : AT_SECS.exec(tail);
    let at = null, tsFrom = null, tsTo = null;
    if (a) {
      at = (+a[1]) * 60 + (+a[2]) + (a[3] ? +("0." + a[3].padEnd(3, "0")) : 0);
      tsFrom = tailAt + a.index; tsTo = tsFrom + a[0].length;
    } else if (b) {
      at = +String(b[1]).replace(",", ".");
      tsFrom = tailAt + b.index; tsTo = tsFrom + b[0].length;
    }
    marks.push({ n: +m[1], at, tsFrom, tsTo, afterBracket: tailAt, idx: m.index });
  }
  if (!marks.length) return [];
  if (marks[0].at == null) marks[0].at = 0;

  return marks.map((s, i) => {
    const next = marks[i + 1];
    const end = (next && next.at != null) ? next.at : total;
    return {
      n: s.n,
      start: s.at,
      end: s.at == null ? null : Math.max(s.at, end),
      untimed: s.at == null,
      past: s.at != null && s.at >= total,
      /* character ranges, so the timeline can edit the prompt back */
      tsFrom: s.tsFrom, tsTo: s.tsTo, afterBracket: s.afterBracket,
      idx: s.idx, bodyFrom: s.tsTo != null ? s.tsTo : s.afterBracket,
      bodyTo: next ? next.idx : null,
    };
  });
}


/* ------------------------------------------------------------------ css */

/* 样式在 csglide_cast.css.js —— 整块搬走，这里只留引入。 */
