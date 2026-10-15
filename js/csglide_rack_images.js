/* ============================================================
 * H3 媒体架 — 参考图片区
 * ------------------------------------------------------------
 * 与面板同结构：流式换行的 gcast-grid.img。槽位编号即 @图片N 的 N，
 * 唯一允许改变图片位置的操作是手动拖拽换位——本节点没有任何自动搬动
 * 图片的逻辑（自动排序只在用户点「按提示词排序」时发生）。

 * 对外挂到 ui 上：imgBox（含标题的整盒）、assignImage（统一导入漏斗）、
 * renderImages（重渲）、isReordering（节点级拖放要据此让路）。
 * ============================================================ */
import { ACCEPT_IMAGE, badImageKind } from "./csglide_cast.js";
import { hasRef, relFromEvent } from "./bz_media_ref.js";
import { el, MAX_SLOTS, fileKind, pickFile, viewURL } from "./csglide_rack_core.js";
import { aliasInput, setAutoAlias } from "./csglide_rack_alias.js";

export function installImages(ui) {
  const imgGrid = el("div", "gcast-grid img");
  /* 与面板 refWrap 同构：图片九格收进带边框的 gcast-imgbox 盒 */
  const imgBox = el("div", "gcast-imgbox");
  imgBox.append(el("div", "gcast-label", "参考图片"), imgGrid);
  let dragImageIndex = null;

  ui.imgGrid = imgGrid;
  ui.imgBox = imgBox;
  ui.assignImage = assignImage;
  ui.renderImages = renderImages;
  ui.isReordering = () => dragImageIndex != null;

  /* 图片导入的唯一漏斗：点击选文件、落格、节点级拖入三条路全汇到这里。
     src 是字符串 = 文件已在 input 目录里（从媒体加载器拖来）的相对路径，
     直接落槽、不再上传一遍 —— 与 fill() 及导演台同名入口同口径。少了这一
     支，字符串会被当 File 塞进 FormData（append 报 "parameter 2 is not of
     type 'Blob'"，文件名读出 undefined）。 */
  async function assignImage(index, src) {
    const label = typeof src === "string" ? src : src.name;
    /* 可疑格式先拦：HEIC/SVG/PSB 这类文件 MIME 齐全，能过 fileKind() 的关，
       但后端 PIL 没有对应解码器，收下只会一路传到模型端才抛异常。所有图片
       导入路径都汇到这里的漏斗，拦一道就全覆盖。
       字符串来源只给得了文件名，故按 {name} 归一后再判——已经在 input 目录里
       的 HEIC 同样读不了，不该因为「绕过上传」就放行。 */
    const bad = badImageKind(typeof src === "string" ? { name: src } : src);
    if (bad) { ui.toast(`「${label}」是 ${bad}，无法作为参考图，请先转成 PNG/JPG`, true); return; }
    const slot = ui.st.slots.images[index];
    const ref = typeof src === "string" ? src : await ui.uploadOrToast(src);
    if (!ref) return;   // 失败已提示。必须提前返回——否则下一行会把 null 写进槽位、清掉已有素材。
    ui.clearSlot(slot);
    slot.file = ref;
    setAutoAlias(slot, ref);            // 文件名主干即别名，剧本里直接写它
    renderImages(); ui.commit();
    /* 诊断：放图要能当场看出「进了哪一段的第几格」。三段摆完若仍然串台，
       靠这一行就能分清是"根本没放进去"还是"放进了错的段"（20261012）。 */
    console.log("[联动] 媒体架 放图: 段=" + (ui.st.cur || "(未认段)")
      + " 第" + (index + 1) + "格 = " + ref);
  }

  /* 图片卡 keyed 增量（20261012 方案C）：旧实现每次 renderImages 都
     replaceChildren + 给所有 <img> 重设 src，同段内任何一次小操作（改别名、
     单次落图、外部刷新）都会让全部缩略图重新解码，是媒体架卡顿/闪烁主因。
     现在按「段身份 + 槽号 + 文件」复用卡片：file 没变一个 DOM 都不碰，
     <img> 不重设 src、别名输入框编辑状态不丢；段切换（cur 变）整池作废。 */
  const imgCardCache = new Map();   // i -> { file, card }
  let imgCacheCur = null;

  function buildCard(i, slot, arr) {
      const card = el("div", "gcast-slot" + (slot.file ? " filled" : ""));
      const thumb = el("div", "gcast-thumb");
      if (slot.file) {
        const img = el("img");
        img.src = viewURL(slot.file);
        img.loading = "lazy";       /* 离屏不加载，滚动到位才取 */
        img.decoding = "async";    /* 解码不卡主线程 */
        img.draggable = false;      /* 保留卡片自身的拖拽换位 */
        /* 裂图兜底：文件不在本机 input 目录（工作流从别的实例搬来/文件被清）
           或浏览器解不了该格式（HEIC 等）时，/view 会 404 或解码失败——裸裂图
           看不出原因，换成一目了然的缺失提示，文件名留在 tooltip 里可排查。 */
        img.onerror = () => {
          if (!thumb.isConnected) return;   // 槽位已重渲，别覆盖新内容
          const box = el("div", "gcast-broken");
          box.append(el("span", "gcast-broken-ico", "⚠"));
          box.append(el("span", "gcast-broken-txt", "预览缺失 · 重新导入"));
          thumb.replaceChildren(box);
        };
        thumb.append(img);
      } else {
        thumb.append(el("div", "gcast-empty", "+"));
      }
      thumb.append(el("span", "gcast-num", String(i + 1)));
      card.append(thumb);

      const cap = el("div", "gcast-cap");
      /* 文件名换成可编辑的别名：拖入时已按文件名主干自动登记，这里改一下即可
         （剧本里写这个名字就引用本素材）。文件名仍在 tooltip 里备查。 */
      if (slot.file) cap.append(aliasInput(slot, ui.commit, () => renderImages()));
      cap.append(el("div", "spacer"));
      if (slot.file) {
        const x = el("button", "gcast-x", "×");
        x.title = "清除槽位";
        x.onclick = (e) => {
          e.stopPropagation(); ui.clearSlot(slot);
          /* 尾格清空即收缩（编号永不漂移）；中间格清空后保留占位 */
          if (i === arr.length - 1) arr.pop();
          renderImages(); ui.commit();
        };
        cap.append(x);
      } else if (i === arr.length - 1) {
        const del = el("button", "gcast-x", "－");
        del.title = "删除这个空槽位";
        del.onclick = (e) => {
          e.stopPropagation();
          arr.pop(); renderImages(); ui.commit();
        };
        cap.append(del);
      }
      card.append(cap);
      card.title = slot.file || `点击或拖入图片（@图片${i + 1}）`;

      card.addEventListener("click", async () => {
        if (!slot.file) {
          const f = await pickFile(ACCEPT_IMAGE);
          if (f) await assignImage(i, f);
        }
      });
      /* 手动拖拽换位 —— 唯一允许改变图片位置的操作 */
      card.draggable = true;
      card.addEventListener("dragstart", (e) => {
        dragImageIndex = i;
        card.classList.add("gcast-dragging");
        e.dataTransfer.effectAllowed = "move";
        try { e.dataTransfer.setData("text/plain", String(i)); } catch (_) {}
      });
      card.addEventListener("dragend", () => {
        dragImageIndex = null;
        card.classList.remove("gcast-dragging");
        imgGrid.querySelectorAll(".gcast-reorder-drop")
          .forEach((n) => n.classList.remove("gcast-reorder-drop"));
      });
      card.addEventListener("dragover", (e) => {
        if (dragImageIndex == null || dragImageIndex === i) return;
        e.preventDefault(); e.stopPropagation();
        card.classList.add("gcast-reorder-drop");
      });
      card.addEventListener("dragleave", () => card.classList.remove("gcast-reorder-drop"));
      card.addEventListener("drop", async (e) => {
        if (dragImageIndex == null || dragImageIndex === i) return;
        e.preventDefault(); e.stopPropagation();
        card.classList.remove("gcast-reorder-drop");
        const arr = ui.st.slots.images;
        const t = arr[dragImageIndex]; arr[dragImageIndex] = arr[i]; arr[i] = t;
        dragImageIndex = null;
        renderImages(); ui.commit();
      });
      /* 外部文件直接落这一格 */
      card.addEventListener("dragover", (e) => {
        if (![...e.dataTransfer.items].some((it) => it.kind === "file") && !hasRef(e)) return;
        e.preventDefault(); e.stopPropagation();
        card.classList.add("drop");
      });
      card.addEventListener("drop", async (e) => {
        if (dragImageIndex != null) return;
        e.preventDefault(); e.stopPropagation();
        card.classList.remove("drop");
        /* 已在 input 目录里的素材（从媒体加载器拖来）：直接记引用，不再上传。
           交给 assignImage 而不是在这里再抄一遍落槽逻辑——「字符串还是 File」
           的分支只在漏斗里判一次，两个入口就不会再各自漏一支。 */
        const ref = relFromEvent(e);
        if (ref) {
          if (fileKind({ name: ref }) !== "image") {
            ui.toast(`这一格只收图片：${ref}`, true);
            return;
          }
          await assignImage(i, ref);
          return;
        }
        const files = [...(e.dataTransfer.files || [])];
        /* 同一格落多张图时优先挑可用的那张；若只剩可疑格式，也交给 assignImage，
           由它把「是 HEIC，请转 PNG/JPG」这类真实原因说出来，而不是含糊地回
           一句"只收图片"——它确实是图片，只是后端读不了。 */
        const imgs = files.filter((x) => fileKind(x) === "image");
        const f = imgs.find((x) => !badImageKind(x)) || imgs[0];
        if (f) await assignImage(i, f);
        else {
          /* 落到这一格却不是图片：MIME 缺失的 HEIC（系统没注册）会走到这里，
             此时 fileKind() 已返回空、进不了上面的 imgs。仍按可疑格式报因由。 */
          const hit = files.map((x) => [x, badImageKind(x)]).find(([, b]) => b);
          if (hit) ui.toast(`「${hit[0].name}」是 ${hit[1]}，无法作为参考图，请先转成 PNG/JPG`, true);
          else if (files.length) ui.toast(`这一格只收图片：${files.map((x) => x.name).join("、")}`, true);
        }
      });
      return card;
  }

  function renderImages() {
    const arr = ui.st.slots.images;
    /* 段身份变了（切段/新建/打开工程）：旧卡片闭包绑定的是旧段 slot 对象，
       整池作废，避免编辑写到错段。 */
    const cur = ui.st.cur || "";
    if (cur !== imgCacheCur) {
      imgCacheCur = cur;
      imgCardCache.clear();
      imgGrid.replaceChildren();
    }
    /* 顺序校正用 insertBefore 而非 replaceChildren(fragment)：位置本来就对的
       卡片一个都不碰 —— 别名输入框正在打字时触发的重渲不会让它失焦。

       ⚠ 参照物必须**每一轮重新取 imgGrid.children[i]**，不能用一个沿途自增
       的 anchor 指针：被换掉的旧卡是当场 remove() 掉的，若它正好是 anchor，
       anchor 从此不再是 imgGrid 的子节点，下一次 insertBefore(card, anchor)
       直接抛 NotFoundError —— 切段 / 渲染倒回重复 load 时整条渲染链就断在
       这里（20261012「全部渲染跑到第 2 段就中断」的根因）。 */
    arr.forEach((slot, i) => {
      const file = slot.file || "";
      let hit = imgCardCache.get(i);
      /* 除「段身份变了整池作废」外，再校验 slot 对象身份。存在「cur 不变、
         st.slots 被 load(revive) 整体换成新数组/新对象」的路径（复制段后的事件
         竞态）—— 只判 cur 会让旧卡片闭包绑着游离旧对象，删除点下去作用于旧
         对象、真实 st.slots 与落库纹丝不动，而「＋ 添加」走最新数组，表现为
         「只能增不能删」。slot 引用真变了就重建，引用没变仍复用（性能不退）。 */
      if (!hit || hit.file !== file || hit.slot !== slot) {
        if (hit && hit.card.parentNode) hit.card.remove();
        hit = { file, slot, card: buildCard(i, slot, arr) };
        imgCardCache.set(i, hit);
      }
      const card = hit.card;
      /* children[i] 一定是 imgGrid 的子节点；i 越界时是 undefined → null
         （insertBefore 的第二参数传 null 等价于 append）。 */
      const at = imgGrid.children[i] || null;
      if (at !== card) imgGrid.insertBefore(card, at);
    });
    /* 槽位收缩：多余的旧卡带走（addtile 也在这轮被顺带顶掉，下面重建） */
    for (const k of Array.from(imgCardCache.keys())) {
      if (k >= arr.length) {
        const hit = imgCardCache.get(k);
        if (hit && hit.card.parentNode) hit.card.remove();
        imgCardCache.delete(k);
      }
    }
    /* 归位完成后，第 arr.length 位及其后的都是旧残留（旧 addtile / 被换掉的
       卡），从尾部整串移除，再补新 addtile。 */
    while (imgGrid.children.length > arr.length) {
      const last = imgGrid.lastChild;
      if (!last) break;
      last.remove();
    }
    if (arr.length < MAX_SLOTS) {
      const add = el("div", "gcast-addtile", "＋ 添加");
      add.title = "添加一个图片槽位";
      add.onclick = () => { arr.push({}); renderImages(); ui.commit(); };
      imgGrid.append(add);
    }
  }
}
