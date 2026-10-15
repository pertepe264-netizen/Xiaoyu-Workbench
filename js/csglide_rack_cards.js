/* ============================================================
 * H3 媒体架 — 参考视频 / 参考音频卡片区
 * ------------------------------------------------------------
 * 与导演台面板的 mediaSlot 逐项同构：波形、双柄修剪、播放修剪区间、悬停
 * 预览、快速帧档、色度徽章文案、音轨勾选行。修剪条本身在
 * csglide_rack_trim.js —— 它不认识卡片，卡片每次重渲都向它要一条新的。

 * 对外挂到 ui 上：vidGrid / audGrid（含各自的「＋ 添加」格）、
 * appendMedia（节点级拖入用）、rebuildMedia（load 与「按提示词排序」共用）。
 * ============================================================ */
import { api } from "../../scripts/api.js";
import { ACCEPT_VIDEO, ACCEPT_AUDIO } from "./csglide_cast.js";
import { hasRef, relFromEvent } from "./bz_media_ref.js";
import { el, MAX_SLOTS, fileKind, pickFile, probeDuration, viewURL } from "./csglide_rack_core.js";
import { aliasInput, setAutoAlias } from "./csglide_rack_alias.js";
import { mediaFor } from "./csglide_rack_media.js";
import { buildTrim } from "./csglide_rack_trim.js";

export function installCards(ui) {
  const vidGrid = el("div", "gcast-grid med");
  const audGrid = el("div", "gcast-grid med");
  const vidCards = [], audCards = [];

  ui.vidGrid = vidGrid;
  ui.audGrid = audGrid;
  ui.rebuildMedia = rebuildMedia;

  function makeMedia(slot, kind, index) {
    const isVideo = kind === "videos";
    const caption = isVideo ? `视频 ${index + 1}` : `音频 ${index + 1}`;
    const token = isVideo ? `@视频${index + 1}` : `@音频${index + 1}`;
    const card = el("div", "gcast-media" + (slot.file ? " filled" : ""));
    let media = null;

    async function fill(src) {
      /* src 是字符串 = 文件已在 input 目录里的相对路径，直接落槽，不再上传一遍 */
      const ref = typeof src === "string" ? src : await ui.uploadOrToast(src);
      if (!ref) return;   // 失败已提示。提前返回，否则 slot.file = null 会清空槽位。
      ui.clearSlot(slot);
      slot.file = ref;
      setAutoAlias(slot, ref);            // 与图片同规则：文件名主干即别名
      const dur = await probeDuration(viewURL(ref), isVideo);
      if (dur) { slot.dur = dur; slot.start = 0; slot.end = dur; }
      if (isVideo) slot.audio = true;
      paint(); ui.commit();
      tagChroma(ref);
    }

    function tagChroma(ref) {
      if (!isVideo) return;
      const i = ref.lastIndexOf("/");
      api.fetchApi("/cglide/probe?type=input"
        + `&filename=${encodeURIComponent(i >= 0 ? ref.slice(i + 1) : ref)}`
        + `&subfolder=${encodeURIComponent(i >= 0 ? ref.slice(0, i) : "")}`)
        .then((r) => (r.status === 200 ? r.json() : null))
        .then((d) => {
          if (!d || slot.file !== ref) return;
          slot.chroma = d.chroma || "";
          slot.codec = d.codec || "";
          if (!slot.dur && d.duration) { slot.dur = d.duration; slot.end = d.duration; }
          paint(); ui.commit();
        })
        .catch(() => {});      /* 徽章是装饰，失败静默 */
    }

    /* 卡销毁：修剪条挂的 ResizeObserver 等资源在这里释放（见 trim.js 的
       __gcastDestroy）。keyed 增量缓存淘汰卡时必须走它，否则观察器随卡
       一起脱离 DOM 却不断开，反复切段会累积成「越切越卡」。 */
    function destroy() {
      try { card.__gcastDestroy && card.__gcastDestroy(); } catch (_) {}
      try { card.remove(); } catch (_) {}
    }

    function paint() {
      /* 重绘会整条替换子树：先断开上一条修剪条的 ResizeObserver ——
         buildTrim 每次都新建观察器，不主动断的话，导入/清除/色度回填
         触发一次 paint 就泄漏一个（与卡销毁同一泄漏点的另一条路径）。 */
      const oldTrim = card.querySelector(".gcast-trim");
      try { oldTrim && oldTrim.__gcastDestroy && oldTrim.__gcastDestroy(); } catch (_) {}
      card.replaceChildren();
      card.classList.toggle("filled", !!slot.file);

      /* cap：编号标题 + @token + 清除 */
      const cap = el("div", "gcast-cap");
      cap.append(el("span", "n", caption));
      if (slot.file) cap.append(el("span", null, token));
      if (slot.file) cap.append(aliasInput(slot, ui.commit, () => paint()));
      cap.append(el("div", "spacer"));
      if (slot.file) {
        const x = el("button", "gcast-x", "×");
        x.title = "清除槽位";
        x.onclick = (e) => {
          e.stopPropagation(); ui.clearSlot(slot);
          /* 尾格清空即收缩（编号永不漂移）；中间格清空后保留占位 */
          const arr = ui.st.slots[kind];
          const list = kind === "videos" ? vidCards : audCards;
          if (arr.indexOf(slot) === arr.length - 1) {
            arr.pop(); const old = list.pop(); old && old.destroy && old.destroy();
          } else {
            paint();
          }
          const newLast = list[list.length - 1];
          if (newLast && !newLast.slot.file) newLast.render();
          ui.commit();
        };
        cap.append(x);
      } else if (index === ui.st.slots[kind].length - 1) {
        const del = el("button", "gcast-x", "－");
        del.title = "删除这个空槽位";
        del.onclick = (e) => {
          e.stopPropagation();
          const arr = ui.st.slots[kind];
          const list = kind === "videos" ? vidCards : audCards;
          arr.pop(); const old = list.pop(); old && old.destroy && old.destroy();
          const newLast = list[list.length - 1];
          if (newLast && !newLast.slot.file) newLast.render();
          ui.commit();
        };
        cap.append(del);
      }
      card.append(cap);

      if (slot.file) {
        if (isVideo) {
          /* 视频缩略图：元素跨 render 复用（mediaFor），移动不重载；
             悬停静音预览，仅当传输未被占用时（muted 时才 hover-play） */
          media = mediaFor(slot, "video", slot.file);
          media.playsInline = true;
          if (media.paused) media.muted = true;
          media.loop = false;
          media.onmouseenter = () => { if (media.muted) media.play().catch(() => {}); };
          media.onmouseleave = () => {
            if (media.muted) { media.pause(); media.currentTime = slot.start || 0; }
          };
          const body = el("div", "gcast-thumb");
          media.style.cssText = "width:100%;height:100%;object-fit:contain;";
          body.append(media);
          card.append(body);
        } else {
          /* 音频没有可见缩略图：隐藏元素供播放，文件名由波形背 */
          media = mediaFor(slot, "audio", slot.file);
          media.style.display = "none";
          card.append(media);
        }
        /* 波形修剪条（带媒体时才有）；销毁钩子挂到卡上，供 destroy()
           与下次 paint() 断旧观察器使用。 */
        const trimW = buildTrim({ slot, isVideo, media, commit: ui.commit });
        if (trimW) {
          card.append(trimW);
          card.__gcastDestroy = trimW.__gcastDestroy || null;
        }
        if (isVideo) {
          const lab = el("label", "gcast-chk");
          const cb = el("input"); cb.type = "checkbox"; cb.checked = slot.audio !== false;
          cb.onchange = () => { slot.audio = cb.checked; ui.commit(); };
          lab.append(cb, el("span", null, "同时把其音轨作为音频参考"));
          card.append(lab);
        }
      } else {
        if (isVideo) {
          const body = el("div", "gcast-thumb");
          body.append(el("div", "gcast-empty", "+"));
          card.append(body);
        } else {
          card.append(el("div", "gcast-wav", "+ 音频"));
        }
      }
    }
    paint();

    /* 空槽点击 / 外部文件拖落（元素级监听只挂一次，paint 只换 children） */
    card.addEventListener("click", async () => {
      if (!slot.file) {
        const f = await pickFile(isVideo ? ACCEPT_VIDEO : ACCEPT_AUDIO);
        if (f) await fill(f);
      }
    });
    card.addEventListener("dragover", (e) => {
      if (![...e.dataTransfer.items].some((it) => it.kind === "file") && !hasRef(e)) return;
      e.preventDefault();
      card.classList.add("drop");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drop"));
    card.addEventListener("drop", async (e) => {
      e.preventDefault(); e.stopPropagation();
      card.classList.remove("drop");
      /* 已在 input 目录里的素材：直接记引用 */
      const ref = relFromEvent(e);
      if (ref) {
        if (fileKind({ name: ref }) !== (isVideo ? "video" : "audio")) {
          ui.toast(`这一格只收${isVideo ? "视频" : "音频"}：${ref}`, true);
          return;
        }
        await fill(ref);
        return;
      }
      const files = [...(e.dataTransfer.files || [])];
      const f = files.find((x) => fileKind(x) === (isVideo ? "video" : "audio"));
      if (f) await fill(f);
      else if (files.length) {
        ui.toast(`这一格只收${isVideo ? "视频" : "音频"}：${files.map((x) => x.name).join("、")}`, true);
      }
    });

    return { card, slot, fill, render: paint, destroy };
  }

  /* 动态追加：槽位按需生长（append-only，编号永不漂移）。
     新卡插在「＋ 添加」格之前，添加格始终居末。 */
  function addTile(label, onAdd) {
    const t = el("div", "gcast-addtile", "＋ " + label);
    t.title = "追加一个空槽位";
    t.onclick = onAdd;
    return t;
  }
  /* 媒体卡 keyed 增量缓存（20261012 媒体架专项）：
     旧实现每次切段（load）都 replaceChildren 两个网格并逐卡 makeMedia +
     buildTrim —— <video> 虽已按文件池化不重载，但修剪条 DOM、波形 canvas、
     ResizeObserver 全部重建，首次切到某段还要当场 fetch 整个媒体文件 +
     WebAudio 解码波形，是媒体架切段迟钝、越切越卡的主因。
     现在按「段身份 cur + 槽号 + 文件」复用卡：file 没变，卡连同修剪条、
     波形、池化视频元素一个 DOM 都不重建；段切换整池作废（旧卡闭包绑的是
     旧段 slot 对象，必须销毁，原则与图片区一致）。按提示词排序改了槽位
     顺序 → 槽号上的 file 变化，相关卡自然重建，「视频 N」标题随之正确。 */
  const mediaCardCache = { videos: new Map(), audios: new Map() };
  let mediaCacheCur = null;

  function destroyEntry(hit) {
    const e = hit && hit.entry;
    if (!e) return;
    try { e.destroy && e.destroy(); } catch (_) {}
  }

  function appendMedia(kind, grid, cards, addT) {
    if (ui.st.slots[kind].length >= MAX_SLOTS) return null;
    const prev = cards[cards.length - 1];
    if (prev && !prev.slot.file) prev.render();   /* 摘掉旧尾格的「－」 */
    const slot = {};
    ui.st.slots[kind].push(slot);
    const m = makeMedia(slot, kind, cards.length);
    cards.push(m);
    /* 同步登记进 keyed 缓存，否则下次 rebuildMedia 会把这张手动卡当残留
       拔掉再重建一张同内容的（手动追加是低频操作，但不白做）。 */
    mediaCardCache[kind].set(cards.length - 1, { file: slot.file || "", entry: m });
    grid.insertBefore(m.card, addT);
    ui.commit();
    return m;
  }
  const vidAdd = addTile("添加视频", () => appendMedia("videos", vidGrid, vidCards, vidAdd));
  const audAdd = addTile("添加音频", () => appendMedia("audios", audGrid, audCards, audAdd));

  /* 给节点级拖入用：卡片列表 / 网格 / 添加格是本分区的内部件，不外露——
     拖入只要「第一个空位」和「再加一个」，不需要看见数组。 */
  const gridOf = (kind) => (kind === "videos" ? vidGrid : audGrid);
  const cardsOf = (kind) => (kind === "videos" ? vidCards : audCards);
  const addTOf = (kind) => (kind === "videos" ? vidAdd : audAdd);
  ui.freeMediaSlot = (kind) => cardsOf(kind).find((x) => !x.slot.file) || null;
  ui.appendMedia = (kind) => appendMedia(kind, gridOf(kind), cardsOf(kind), addTOf(kind));

  function rebuildZone(kind, grid, cards, addT) {
    const arr = ui.st.slots[kind];
    /* 添加格先摘下，卡归位完再挂末位，不参与 insertBefore 锚点计算 */
    if (addT.parentNode) addT.remove();
    let anchor = grid.firstChild;
    arr.forEach((slot, i) => {
      const file = slot.file || "";
      let hit = mediaCardCache[kind].get(i);
      /* 比较用 entry 闭包里 slot 的实时 file：文件落进已有空槽（fill）后
         slot 引用不变、file 更新，命中复用、不白重建；排序后槽位引用换了，
         file 对不上，自然重建。 */
      const hitFile = hit ? (hit.entry.slot.file || "") : null;
      /* 与图片区同：除 file 外再校验 slot 对象身份，防止「cur 不变、st.slots 被
         load 整体换对象」时旧卡闭包绑游离旧对象，导致复制段「只能增不能删」。 */
      const hitSlot = hit ? hit.entry.slot : null;
      if (!hit || hitFile !== file || hitSlot !== slot) {
        if (hit) destroyEntry(hit);
        const m = makeMedia(slot, kind, i);
        hit = { file, entry: m };
        mediaCardCache[kind].set(i, hit);
      }
      cards[i] = hit.entry;
      const card = hit.entry.card;
      if (card.parentNode !== grid || card !== anchor) {
        grid.insertBefore(card, anchor);    /* 位置不对就原地归位，不重建 */
      } else {
        anchor = anchor.nextSibling;
      }
    });
    /* 槽位收缩：淘汰编号越界的卡 */
    for (const k of Array.from(mediaCardCache[kind].keys())) {
      if (k >= arr.length) {
        destroyEntry(mediaCardCache[kind].get(k));
        mediaCardCache[kind].delete(k);
      }
    }
    cards.length = arr.length;
    /* 归位完成后 anchor 及其后兄弟都是旧残留，整串移除，再补添加格 */
    while (anchor) { const nx = anchor.nextSibling; anchor.remove(); anchor = nx; }
    grid.append(addT);
  }

  /* 视频/音频卡增量重建。force=true 用于整池作废后的强制全量（如排序后
     需要确保编号标题全对）；load() 切段走 cur 变化自动作废。 */
  function rebuildMedia(force) {
    const cur = ui.st.cur || "";
    if (force || cur !== mediaCacheCur) {
      mediaCacheCur = cur;
      for (const kind of ["videos", "audios"]) {
        for (const hit of mediaCardCache[kind].values()) destroyEntry(hit);
        mediaCardCache[kind].clear();
      }
      vidGrid.replaceChildren(); audGrid.replaceChildren();
    }
    rebuildZone("videos", vidGrid, vidCards, vidAdd);
    rebuildZone("audios", audGrid, audCards, audAdd);
  }
}
