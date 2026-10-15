/* ============================================================
 * H3 媒体架 — 节点级拖入
 * ------------------------------------------------------------
 * 落在面板空白处（不在某一格里）的文件按类型路由到第一个空位：
 * 落格，不搬动已有素材。

 * 拒绝原因必须说出来。识别不出类型的文件此前被静默丢弃（if (k) 为假直接
 * continue）：用户只看到「拖进来没反应」，无从判断是格式不支持还是程序坏了。
 * 分两档说清：①认得出、是图片、但后端 PIL 读不了的（HEIC/SVG…）→ 转格式；
 * ②根本不认识的 → 不支持。两条并作一条 toast——共用一个提示条，分两次调用
 * 会互相覆盖，只剩下后说的那句。
 * ============================================================ */
import { badImageKind } from "./csglide_cast.js";
import { hasRef, relFromEvent } from "./bz_media_ref.js";
import { MAX_SLOTS, fileKind } from "./csglide_rack_core.js";

export function installDrop(ui) {
  /* 按类型落到第一个空位（落格，不搬动已有素材） */
  async function routeToFree(kind, file) {
    if (kind === "image") {
      let i = ui.st.slots.images.findIndex((s) => !s.file);
      if (i < 0 && ui.st.slots.images.length < MAX_SLOTS) {
        ui.st.slots.images.push({});
        i = ui.st.slots.images.length - 1;
      }
      if (i >= 0) await ui.assignImage(i, file);
      else ui.toast(`图片槽位已达上限 ${MAX_SLOTS} 个，请先清理再导入`, true);
      return;
    }
    const k = kind === "video" ? "videos" : "audios";
    /* 卡片列表 / 网格 / 添加格都在 cards 分区内部，这里只按 kind 要结果 */
    let c = ui.freeMediaSlot(k);
    if (!c) c = ui.appendMedia(k);   // 满则返回 null
    if (!c) {
      ui.toast(`${kind === "video" ? "视频" : "音频"}槽位已达上限 ${MAX_SLOTS} 个，请先清理再导入`, true);
      return;
    }
    await c.fill(file);
  }
  ui.routeToFree = routeToFree;

  ui.root.addEventListener("dragover", (e) => {
    if (![...e.dataTransfer.items].some((it) => it.kind === "file") && !hasRef(e)) return;
    e.preventDefault();
    ui.root.classList.add("drop-active");
  });
  ui.root.addEventListener("dragleave", (e) => {
    if (!ui.root.contains(e.relatedTarget)) ui.root.classList.remove("drop-active");
  });
  ui.root.addEventListener("drop", async (e) => {
    if (ui.isReordering?.()) return;          /* 内部换位拖拽不在此处理 */
    ui.root.classList.remove("drop-active");
    /* 已在 input 目录里的素材：直接记引用，不进上传通道 */
    const ref = relFromEvent(e);
    if (ref) {
      e.preventDefault(); e.stopPropagation();
      const k = fileKind({ name: ref });
      if (k) await routeToFree(k, ref);
      else ui.toast(`不支持的格式：${ref}`, true);
      return;
    }
    if (!e.dataTransfer.files?.length) return;
    e.preventDefault(); e.stopPropagation();
    const bads = [], rejected = [];
    for (const f of e.dataTransfer.files) {
      const k = fileKind(f);
      if (k) { await routeToFree(k, f); continue; }   /* 图片分支内部已拦可疑格式 */
      const b = badImageKind(f);
      if (b) bads.push(`${f.name}（${b}）`);
      else rejected.push(f.name);
    }
    const parts = [];
    if (bads.length) parts.push(`无法作为参考图、请先转成 PNG/JPG：${bads.join("、")}`);
    if (rejected.length) parts.push(`不支持的格式：${rejected.join("、")}`);
    if (parts.length) ui.toast(parts.join("；"), true);
  });
}
