/* ============================================================
 * H3 媒体架 — 按提示词排序
 * ------------------------------------------------------------
 * 「<Subject 1> 为 @图片3 中的陈九一」这类写法里，编号 3 是写剧本时按文本
 * 顺序定死的，素材落在架上哪一格却是拖出来的——两者不一致时，@图片3 指的
 * 就不是陈九一。本按钮把这层关系反过来用：在 @标签 左右找出架上认得的名
 * 字，把那件素材搬到这个编号所在的格子。

 * 名字是**反查**得来的，不是从句子里分析出来的：候选集就是架上现有的别名
 * 与文件名主干（有限、且全由用户控制），拿它去文本里查命中。因此绝不会认
 * 出一个架上没有的名字，也不需要猜测句子语法——换任何提示词写法都照样成立。
 * 一处 @标签附近有多个候选时，取离它最近的；距离相同取名字更长的（免得
 * 「九一」抢掉更完整的「陈九一」）。

 * 只认**显式写的** @图片N / @视频N / @音频N：只写角色名（别名）的不好办——
 * 别名是「按当前顺序」展开成编号的，先改顺序再按别名判，等于拿结论当依据。
 * ============================================================ */
import { promptNodeOf, upstreamTextOf } from "./csglide_cast.js";
import { flashBtn, MAX_SLOTS } from "./csglide_rack_core.js";
import { ALIAS_MIN, ALIAS_SPLIT, baseName, normSpace } from "./csglide_rack_alias.js";

const TOKEN_RE = {
  images: /@图片(\d+)/g,
  /* @视频音频N 也是视频槽位（与导演台 filterUsedByPrompt 同口径） */
  videos: /@(?:视频音频|视频)(\d+)/g,
  audios: /@音频(\d+)/g,
};
const TOKEN_LABEL = { images: "图片", videos: "视频", audios: "音频" };

/* 架上现有的名字 → 槽位。别名优先，文件名主干兜底（两者常是同一个词）。 */
function nameIndex(kind, st) {
  const m = new Map();
  st.slots[kind].forEach((slot) => {
    if (!slot || !slot.file) return;
    const raw = Array.isArray(slot.alias) ? slot.alias.join("，") : (slot.alias || "");
    for (const nm of String(raw).split(ALIAS_SPLIT).concat(baseName(slot.file))) {
      const name = normSpace(String(nm).trim()).replace(/^@/, "");
      if (name.length >= ALIAS_MIN && !m.has(name)) m.set(name, slot);
    }
  });
  return m;
}

/* @标签 左右认主人：只在它所在的那一行里找，且左右各限 120 字——跨行会把
   上一个/下一个主体的名字认到自己头上，整行不设限则会把同行里另一个
   @标签 的名字抢过来。 */
function ownerOfToken(text, pos, names) {
  const from = Math.max(text.lastIndexOf("\n", pos) + 1, pos - 120);
  let to = text.indexOf("\n", pos);
  if (to < 0) to = text.length;
  to = Math.min(to, pos + 120);
  let best = null;
  for (const [name, slot] of names) {
    for (let k = text.indexOf(name, from); k >= 0 && k < to; k = text.indexOf(name, k + 1)) {
      const dist = Math.abs(k - pos);
      if (!best || dist < best.dist || (dist === best.dist && name.length > best.name.length)) {
        best = { dist, name, slot };
      }
    }
  }
  return best;
}

/* 把 target（0 基编号 → 槽位）落到数组里：被点名的进指定格，其余按原相对
   顺序补进剩下的格子。编号是位置本身，挪了中间的必然带动后面——这是改动
   的本意。返回实际换了位置的个数。 */
function applyOrder(kind, target, st) {
  const arr = st.slots[kind];
  const need = Math.max(...target.keys()) + 1;
  while (arr.length < need) arr.push({});      // 编号超出现有长度：补空位撑住
  const placed = new Set(target.values());
  const out = new Array(arr.length).fill(null);
  for (const [i, slot] of target) out[i] = slot;
  let k = 0;
  for (const s of arr) {
    if (placed.has(s)) continue;
    while (k < out.length && out[k] !== null) k += 1;
    if (k < out.length) out[k] = s; else out.push(s);
  }
  for (let i = 0; i < out.length; i++) if (out[i] === null) out[i] = {};
  let moved = 0;
  for (const [i, slot] of target) if (arr.indexOf(slot) !== i) moved += 1;
  /* 原地写回：视频/音频卡的闭包拿的是 slot 对象而不是数组，换数组引用会挂 */
  arr.length = out.length;
  for (let i = 0; i < out.length; i++) arr[i] = out[i];
  return moved;
}

export function installSort(ui) {
  /* 消费本媒体架的节点（refs 端口接过来的）在用的提示词。按连线反查，不认
     节点类型——导演台换了名字/换了实现照样成立。 */
  function promptTexts() {
    const graph = (ui.app && ui.app.graph) || null;
    if (!graph || !graph.links) return [];
    const eats = (n) => (n.inputs || []).some((s) => {
      if (!s || s.link == null || !(s.type === "H3_REFS" || s.name === "refs")) return false;
      const lk = graph.links[s.link];
      return !!lk && lk.origin_id === ui.node.id;
    });
    const out = [];
    for (const n of graph._nodes || []) {
      if (!n || n.id === ui.node.id || !eats(n)) continue;
      /* 与导演台「实际生效的提示词」同序：prompt_text 接线时外部文本整体接管，
         面板原文不再算数——判反了就会按一份早就作废的旧文本去搬素材。 */
      const up = promptNodeOf ? promptNodeOf(n) : null;
      let t = up && upstreamTextOf ? upstreamTextOf(up) : "";
      if (!t) {
        const w = (n.widgets || []).find((x) => x.name === "h3_data");
        try { t = (JSON.parse((w && w.value) || "{}").prompt) || ""; } catch (_) { t = ""; }
      }
      if (!t && upstreamTextOf) t = upstreamTextOf(n);
      if (t) out.push(String(t));
    }
    return out;
  }

  function sortByPrompt() {
    const texts = promptTexts();
    if (!texts.length) {
      ui.toast("没找到接本媒体架的节点：把「参考素材」接到导演台的 refs 输入再点", true);
      return;
    }
    const done = [];
    let total = 0, miss = 0;
    for (const kind of ["images", "videos", "audios"]) {
      const names = nameIndex(kind, ui.st);
      if (!names.size) continue;
      const target = new Map();
      for (const text of texts) {
        for (const m of text.matchAll(new RegExp(TOKEN_RE[kind].source, "g"))) {
          const idx = parseInt(m[1], 10) - 1;
          if (!(idx >= 0 && idx < MAX_SLOTS)) continue;
          const hit = ownerOfToken(text, m.index, names);
          if (!hit) { miss += 1; continue; }
          if (target.has(idx)) continue;         // 多个节点抢同一编号：先写的赢
          /* 同一件素材被两个编号点名（多半是认错了主人）：也是先写的赢，
             否则它会同时占两格，其余素材集体后移。 */
          if ([...target.values()].includes(hit.slot)) continue;
          target.set(idx, hit.slot);
          done.push({ nm: hit.name, tok: `@${TOKEN_LABEL[kind]}${idx + 1}` });
        }
      }
      if (target.size) total += applyOrder(kind, target, ui.st);
    }
    if (!total) { flashBtn(ui.bSort, miss ? "没认出名字" : "顺序已对齐"); return; }
    /* 排序是显式重排语义：强制整池重建一次，保证「视频 N」标题与卡闭包
       全部按新顺序对齐（低频手动操作，一次全量成本可接受）。 */
    ui.renderImages(); ui.rebuildMedia(true); ui.commit();
    flashBtn(ui.bSort, "已对齐 " + total + " 个");
    const list = done.slice(0, 3).map((d) => `${d.nm}→${d.tok}`);
    ui.toast(`已按提示词重排：${list.join("、")}${done.length > 3 ? ` 等 ${done.length} 项` : ""}`);
  }

  ui.sortByPrompt = sortByPrompt;
}
