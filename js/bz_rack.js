/* ============================================================
 * 图遍历助手 —— 媒体架 / 外接提示词的上游定位
 * ------------------------------------------------------------
 * 导演台、文案台、媒体架都要问同一个问题：「我这根线接的是谁？」
 * 各写一份就会在某次前端改版后各错各的，所以收口在这里。
 *
 * 为什么不用 node.getInputNode(name)：按名字查输入口在部分 ComfyUI 前端
 * 版本下不可靠（本地化会改写 name），一旦失效，媒体架素材就一个都进不了
 * chips——症状是「面板空了」，而根因在千里之外。所以统一走
 * 「按端口类型/名字找 input + 手动查 graph.links」，原方法只作兜底。
 * ============================================================ */
import { api } from "../../scripts/api.js";
import { bzStore } from "./bz_store.js";

/**
 * 前端 bag 版本号（节点闭包版本）。**必须定义在本无环基础模块**：
 * 原来定义在 bz_project_sync.js，但 bz_project_sync 又 import 媒体架/导演台，
 * 媒体架经 rack_seg 再 import bz_project_sync 取本常量 → 形成模块循环，
 * 且新 bag 因此被误判、反复热重建。本文件只依赖 scripts + bz_store，
 * 不依赖任何业务模块，所有节点从这里取。改前端状态结构时 +1。
 */
export const BZ_BAG_VERSION = 20261013;

/** 真正的媒体架节点：带 rack_data widget（或已挂 gmr）的那个。
 *  文案台只是透传 H3_REFS，两样都没有 —— 不能拿它当媒体架。 */
function isRackNode(n) {
  if (!n) return false;
  if (n.gmr) return true;
  return !!(n.widgets && n.widgets.some((w) => w && w.name === "rack_data"));
}

/** 沿 H3_REFS 输入口往上走**一层**（不穿透）。 */
function oneUpstream(node) {
  const inputs = node.inputs || [];
  const inp = inputs.find((s) => s && s.type === "H3_REFS" && s.link != null)
    || inputs.find((s) => s && s.name === "refs" && s.link != null);
  const up = upstreamOf(node, inp);
  if (up) return up;
  return node.getInputNode ? node.getInputNode("refs") : null;
}

/** 找 refs（H3_REFS）输入口接的上游节点 —— 就是媒体架。
 *
 *  ⚠ 只走一层是不够的（20261012「渲染时所有段的参考图没连」的根因）：
 *  现在的常见接线是 **媒体架 → 文案台 → 导演台**，文案台对 H3_REFS 是
 *  **透传**（csglide_desk.py: return (参考素材, text)），自己既没有
 *  rack_data widget 也没有 gmr。只走一层拿到的是文案台，于是：
 *    · rackSnapshot() 读不到 rack_data（raw=""）→ 反而写出一份「空快照」，
 *      把该分段真实记下的素材永久覆盖成空（stash 每切一段写一次）；
 *    · restoreShotRack() 因 rn.gmr 为空直接 return → 媒体架永不被倒回，
 *      切段只能靠 switchSeg 去 segs 里找，而 segs 又是从被清空的
 *      shots[i].rack 借来的空快照 → 三段全空 = 「参考图没连」；
 *    · 导演台 mergedSlots() 也永远读不到媒体架槽位。
 *  所以这里沿 H3_REFS 链路一直往上找，直到遇到真正持有 rack_data 的节点。
 */
export function rackNodeOf(node) {
  const one = oneUpstream(node);
  let n = one;
  let guard = 0;
  while (n && guard++ < 8) {
    if (isRackNode(n)) return n;
    n = oneUpstream(n);
  }
  /* 找不到真媒体架就退回一层（旧行为兜底），绝不能返回 null —— 调用方
     拿 null 会当成"没接媒体架"，把工程级 rack 字段整段抹掉。 */
  return one;
}

/** 找 prompt_text 输入口接的上游文本节点。 */
export function promptNodeOf(node) {
  const inputs = node.inputs || [];
  const inp = inputs.find((s) => s && s.name === "prompt_text" && s.link != null);
  const up = upstreamOf(node, inp);
  if (up) return up;
  return node.getInputNode ? node.getInputNode("prompt_text") : null;
}

function upstreamOf(node, inp) {
  const graph = (typeof app !== "undefined" && app && app.graph) || null;
  if (!inp || !graph || graph.links == null) return null;
  const link = graph.links[inp.link];
  if (!link) return null;
  return graph.getNodeById ? graph.getNodeById(link.origin_id) : null;
}

/* rack_data 的解析缓存：按原文缓存 JSON.parse 的结果。
   击键级调用（chips 每次输入都要重算一遍）不能每次都 parse 一遍整个素材架。 */
let rackParseCache = { raw: null, slots: { images: [], videos: [], audios: [] } };

export function rackSlotsOf(node) {
  const w = rackNodeOf(node);
  const raw = (w && w.widgets && w.widgets.find((x) => x.name === "rack_data")?.value) || "";
  if (raw === rackParseCache.raw) return { raw, slots: rackParseCache.slots };
  let d = {};
  try { d = JSON.parse(raw); } catch { d = {}; }
  const s = (d && d.slots) || {};
  const arr = (k) => (Array.isArray(s[k]) ? s[k] : []);
  rackParseCache = {
    raw,
    slots: { images: arr("images"), videos: arr("videos"), audios: arr("audios") },
  };
  return { raw, slots: rackParseCache.slots };
}

/* 外接提示词：文本源节点五花八门、widget 名不统一，「最长字符串 widget 值」
   是最稳的启发式——用户的提示词几乎总是那个节点里最长的字符串。 */
export function upstreamTextOf(n) {
  if (!n) return "";
  let best = "";
  for (const w of n.widgets || []) {
    if (typeof w.value === "string" && w.value.length > best.length) best = w.value;
  }
  if (!best && Array.isArray(n.widgets_values)) {
    for (const v of n.widgets_values) {
      if (typeof v === "string" && v.length > best.length) best = v;
    }
  }
  return best;
}

/* ---- 导演台定位 ------------------------------------------------------
   文案台要知道「现在在写哪一段」，而段身份只存在于导演台节点的工程快照里
   （node.properties.gcast_project）。执行顺序上文案台排在导演台前面，所以
   不能等后端告诉它——只能在前端直接问导演台。 */

export const CAST_NODE_ID = "CSGlideCastCS";
export const DESK_NODE_ID = "CSGlideDeskCS";

/** 画布上所有导演台节点（通常只有一个）。 */
export function castNodes() {
  const g = (typeof app !== "undefined" && app && app.graph) || null;
  if (!g || !g._nodes) return [];
  return g._nodes.filter((n) => n && n.type === CAST_NODE_ID);
}

/** 画布上所有文案台节点（通常只有一个）。 */
export function deskNodes() {
  const g = (typeof app !== "undefined" && app && app.graph) || null;
  if (!g || !g._nodes) return [];
  return g._nodes.filter((n) => n && n.type === DESK_NODE_ID);
}

/** 找文案台节点：优先接在本台（导演台）上游的那台，否则取第一个。 */
export function deskNodeOf(node) {
  const all = deskNodes();
  const wired = all.filter((d) => feeds(d, node));
  const list = wired.length ? wired : all;
  return list[0] || null;
}

/**
 * 导演台当前的「片名 + 分段身份」。
 * 文案台拿它认段：同一段来回切，词不会串；新建分段自动跟着走。
 */
export function castProject(node) {
  /* 优先用接了本节点输出的那台导演台（多导演台时不会认错） */
  const wired = castNodes().filter((c) => feeds(c, node));
  const list = wired.length ? wired : castNodes();
  const c = list[0];
  if (!c) { console.warn("[联动] castProject 失败: 画布上找不到导演台节点"); return null; }
  const p = c.properties && c.properties.gcast_project;
  if (!p || !Array.isArray(p.shots)) { console.warn("[联动] castProject 失败: 导演台 properties.gcast_project 为空或无 shots"); return null; }
  const cur = p.shots[p.idx];
  if (!cur) return { name: p.name || "", shot: "", clip: "", index: p.idx,
                     gen: p.gen || 0, cast: c };
  if (!cur.id) cur.id = Math.random().toString(36).slice(2, 9);
  return { name: p.name || "", shot: cur.id, clip: cur.clip || "", index: p.idx,
           gen: p.gen || 0, cast: c };
}

/* ---- 共享的「当前段」 ------------------------------------------------
   三个节点（导演台 / 文案台 / 媒体架）唯一的共享点。导演台切段时立刻写后端，
   另外两个轮询读它 —— ComfyUI 没有给我们跨节点事件机制，而轮询一个
   几十字节的 JSON 是最小的代价。
   注意这与 castProject() 不是一回事：那个读的是导演台节点**内存里的工程
   快照**（即时、但只有导演台自己有）；这个读的是**磁盘档案**（稍有延迟、
   但三个节点看的是同一份）。两者互为兜底。 */

let curCache = { name: "", id: null, at: 0 };
/* 当前段**那一行档案**（index / name / locked / pending / frames）。
   三个节点要告诉用户"现在写的是哪一段"时都读这一份：各自去拼段号、各自判断
   锁定，就会出现三处说法不一致 —— 那正是"统一锚点"要解决的问题。 */
let curRowCache = { name: "", row: null, at: 0 };

/** 一次读回 current 与它那一行档案。两个缓存共用这一读，不额外打后端。 */
async function _readCurrent(name) {
  const res = await api.fetchApi(`/cglide/chain/summary?name=${encodeURIComponent(name)}`);
  const d = await res.json();
  const id = (d && d.current) || null;
  const row = id
    ? (((d && d.clips) || []).find((c) => c && c.id === id) || null)
    : null;
  curCache = { name, id, at: Date.now() };
  curRowCache = { name, row, at: Date.now() };
  return { id, row };
}

export async function currentOf(name) {
  if (!name) return null;
  /* 2 秒内的重复读直接走缓存：轮询是每 900ms 一次，没必要每次都打后端 */
  if (curCache.name === name && Date.now() - curCache.at < 2000) return curCache.id;
  try {
    return (await _readCurrent(name)).id;
  } catch {
    return curCache.name === name ? curCache.id : null;
  }
}

/** 当前段那一行档案；没有当前段（或档案读不到）时返回 null。 */
export async function currentClipOf(name) {
  if (!name) return null;
  if (curRowCache.name === name && Date.now() - curRowCache.at < 2000) {
    return curRowCache.row;
  }
  try {
    return (await _readCurrent(name)).row;
  } catch {
    return curRowCache.name === name ? curRowCache.row : null;
  }
}

/* 切段请求的两道乱序防护（20261012「快速连点 1→2→3，最后跳回旧段」根因）：
   1) curPostSeq 单调序号：快速连点会并发发出多个 POST，旧请求的响应可能**晚于**
      新请求回来；序号过期的响应一律不更新缓存、不广播、不碰 store。
   2) curPostChain 串行化：同片名的 POST 严格按调用顺序排队发出，后端落盘顺序
      与点击顺序一致（否则即便前端不广播，后端最终 current 也可能是旧段，WS
      回声照样把界面拉回去）。
   乐观更新在**发请求前**就广播：界面零延迟跟最后一次点击，权威值由后端回声
   与 bzStore 对账收敛。 */
let curPostSeq = 0;
let curPostChain = Promise.resolve();

/** 导演台切段时调用：把「当前段」写给大家看。 */
export function setCurrent(name, id) {
  if (!name) { console.warn("[联动] setCurrent 跳过: name 为空"); return Promise.resolve(); }
  const mySeq = ++curPostSeq;
  const clip = id || "";
  /* 立即乐观：缓存 / store / 事件总线全部先切到目标段，不等 POST 往返
     （文案台首跳读旧 store 慢半拍的根因也在这里一并解决）。 */
  curCache = { name, id: clip || null, at: Date.now() };
  curRowCache = { name, row: null, at: 0 };
  bzStore.optimisticCurrent(name, clip, mySeq);
  emitCastChange({ name, clip, reason: "set_current", seq: mySeq });
  /* 串行排队：等前一个 POST 落盘后再发本次，保证后端按点击顺序写。 */
  const run = curPostChain.then(async () => {
    try {
      await api.fetchApi("/cglide/chain/current", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, id: clip }),
      });
      /* 过期响应（连点后本次已不是最新一次点击）：什么都不覆盖。
         store 的乐观值由最新那次请求负责对账。 */
      if (mySeq !== curPostSeq) {
        console.log("[联动] setCurrent 过期响应丢弃: name=" + name + " id=" + clip + " seq=" + mySeq + "<" + curPostSeq);
        return;
      }
      console.log("[联动] setCurrent 成功: name=" + name + " id=" + clip);
      curCache = { name, id: clip || null, at: Date.now() };
      curRowCache = { name, row: null, at: 0 };
      /* 权威对账：拉一次档案，让 store 以后端 current 为准（命中乐观值即收敛，
         不一致也由 store 的乐观保护延迟对账，不会把界面拉回旧段）。 */
      bzStore.reconcileCurrent(name, mySeq);
    } catch (e) { console.warn("[联动] setCurrent 失败(被吞):", name, clip, e); }
  });
  /* 队列本身不能因单次失败断掉：吞掉 rejection 并续链。 */
  curPostChain = run.catch(() => {});
  return run;
}

/* ---- 跨节点事件总线（20261012 联动提速）------------------------------
   导演台 / 媒体架 / 文案台 / Qwen 台同处一个 ComfyUI 前端页面（同 window）。
   以前"当前段变了没、素材架空没空"全靠 0.8~1.5s 轮询互相发现 —— 操作后有
   1 秒级迟钝，每次轮询还全量重建 DOM、打后端。现在变化方即时广播，监听方
   即时响应；轮询保留但拉长到 3~5 秒，只作跨标签页 / 外部改动 / 漏事件的兜底。
   事件不走 ComfyUI websocket（那是后端→前端通道），同页面 window 事件零成本。 */
export const CAST_SEG_EVENT = "bz:cast-seg-change";   /* 当前段/工程身份变化 */
export const RACK_CHANGE_EVENT = "bz:rack-change";    /* 媒体架槽位内容变化 */

export function emitCastChange(detail) {
  const d = detail || {};
  console.log("[联动] 广播", d.reason || "?", "name=" + (d.name || ""), "clip=" + (d.clip || ""), "shot=" + (d.shot || ""));
  try { window.dispatchEvent(new CustomEvent(CAST_SEG_EVENT, { detail: d })); }
  catch (e) { console.warn("[联动] 广播失败(退回轮询)", e); }
}
export function onCastChange(fn) {
  const h = (ev) => { try { fn(ev.detail || {}); } catch (e) {} };
  window.addEventListener(CAST_SEG_EVENT, h);
  return () => window.removeEventListener(CAST_SEG_EVENT, h);
}
export function emitRackChange(detail) {
  try { window.dispatchEvent(new CustomEvent(RACK_CHANGE_EVENT, { detail: detail || {} })); }
  catch (e) {}
}
export function onRackChange(fn) {
  const h = (ev) => { try { fn(ev.detail || {}); } catch (e) {} };
  window.addEventListener(RACK_CHANGE_EVENT, h);
  return () => window.removeEventListener(RACK_CHANGE_EVENT, h);
}

/**
 * 从导演台**内存工程快照**里借某段（缺省=当前段）的媒体架快照 rack。
 * 媒体架轮询发现"档案 current 跳到一个我没记过的新段"时用它兜底 ——
 * 新段刚 addShot、轮询比导演台 restoreShotRack 快的那个竞态窗口里，
 * 旧实现直接把架上清空（参考图"复制后消失"的根因）；现在先问导演台要。
 */
export function castShotRack(node, clipId) {
  const p0 = castProject(node);
  if (!p0 || !p0.cast) return null;
  const proj = p0.cast.properties && p0.cast.properties.gcast_project;
  if (!proj || !Array.isArray(proj.shots)) return null;
  const shot = clipId
    ? proj.shots.find((s) => s && (s.clip === clipId || s.id === clipId))
    : proj.shots[proj.idx];
  return shot && shot.rack ? shot.rack : null;
}

/** downstream 是否直接或间接吃得到 upstream 的输出。 */
function feeds(upstream, downstream) {
  const outs = upstream.outputs || [];
  for (const o of outs) {
    for (const lid of o.links || []) {
      const g = (typeof app !== "undefined" && app && app.graph) || null;
      const link = g && g.links ? g.links[lid] : null;
      if (!link) continue;
      if (link.target_id === downstream.id) return true;
      const mid = g.getNodeById ? g.getNodeById(link.target_id) : null;
      if (mid && mid !== downstream && feeds(mid, downstream)) return true;
    }
  }
  return false;
}
