import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { rackNodeOf, rackSlotsOf, emitCastChange } from "./bz_rack.js";
import { blankState, parseInitial } from "./csglide_cast_core.js";
import { el, uploadFile } from "./csglide_ui_kit.js";

/* ============================================================
 * 工程 / 档案冲突检测 与 段落编辑
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installConflict(ui) {
  /* ---- 工程/档案冲突检测（P1-10 的 mtime 校验） --------------------------
     一个分段的时长有两个写入源，都合法、都可能被用户改：导演台面板的「长度」
     预设（跟工作流走）、时间轴拖边缘（写磁盘档案）。渲染时后端会用面板预设
     覆盖档案 —— 所以用户拖了时间轴却没重设面板长度，下次渲染就会被旧预设
     **静默覆盖**，这正是文档禁止的。

     这里在「渲染全部」之前拦一道：把工程里的分段预设发给后端，后端按 shot
     id 对齐档案，把对不上的段列出来，让用户逐条选「用面板」还是「用档案」。
     只查不改，绝不自己动手覆盖。 */

  async function syncCheck() {
    const p = ui.proj();
    const name = (p && p.name) || "";
    if (!name || !p.shots.length) return true;
    const shots = p.shots.map((sh) => ({
      id: sh.id || "",
      frames: sh.state ? sh.state.length : undefined,
      synced: sh.synced,
    }));
    let data;
    try {
      const res = await api.fetchApi("/cglide/chain/sync_check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, shots }),
      });
      data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      /* 冲突检测失败不该挡渲染 —— 它是护栏不是门槛。静默放行。 */
      return true;
    }
    const conflicts = (data.conflicts || []);
    if (!conflicts.length) return true;
    /* 20261013 用户定：**一切以面板为准**。面板就是设置中心，拖时间轴改的段
       也会同步写进面板（applyFrames 两边一起写），所以"面板≠档案"只剩旧数据
       一种情形 —— 没必要弹窗把用户拦下来反问"哪边算数"（那是在问一句废话：
       用户在面板上做的所有设定，当然以面板为主）。直接按面板把档案改齐，
       用一行不挡操作的提示说清改了什么，然后照常渲染。 */
    const done = [];
    for (const c of conflicts) {
      try { await applySyncChoice(p, c, "preset"); } catch (_) { /* 单段失败不挡渲染 */ }
      done.push(`第${c.index}段 ${c.fact}→${c.preset} 帧`);
    }
    ui.lanesFlash?.(`档案时长与面板不一致，已按面板为准写齐：${done.join("、")}`);
    return true;
  }

  /** 弹冲突清单，让用户定哪边算数。返回是否「处理完毕、可以继续渲染」。 */
  function askSync(p, conflicts) {
    return new Promise((resolve) => {
      const wrap = el("div", "gcast-modal");
      const sheet = el("div", "gcast-sheet");
      sheet.append(el("h3", null,
        `有 ${conflicts.length} 段的「面板时长」和「档案」对不上`));
      const body = el("div", "body");
      body.append(el("div", "hint",
        "渲染会用面板预设覆盖档案。下面选哪边算数 —— 不选就点「先不渲染」。"));
      conflicts.forEach((c) => {
        const row = el("div", "gcast-sync-row");
        const t = el("div", "t");
        t.append(el("b", null, `第 ${c.index} 段`),
          el("span", null, `面板 ${c.preset} 帧 · 档案 ${c.fact} 帧`));
        row.append(t);
        body.append(row);
      });
      const foot = el("div", "foot");
      const cancel = el("button", "gcast-btn ghost", "先不渲染");
      const usePreset = el("button", "gcast-btn", "全部用面板");
      usePreset.title = "把这些段的档案时长都改成面板预设（会标成待办）";
      const useFact = el("button", "gcast-btn ghost", "全部用档案");
      useFact.title = "把这些段的面板预设都改成档案里的时长";
      foot.append(cancel, useFact, usePreset);
      sheet.append(body, foot);
      wrap.append(sheet);

      const close = () => { wrap.remove(); };
      cancel.onclick = () => { close(); resolve(false); };
      usePreset.onclick = async () => {
        close();
        await Promise.all(conflicts.map((c) => applySyncChoice(p, c, "preset")));
        resolve(true);
      };
      useFact.onclick = () => {
        close();
        conflicts.forEach((c) => applySyncChoice(p, c, "fact"));
        resolve(true);
      };
      wrap.addEventListener("pointerdown", (ev) => {
        if (ev.target === wrap) { close(); resolve(false); }
      });
      document.body.append(wrap);
      usePreset.focus();
    });
  }

  /** 落实用户对某一段的选择：用面板 → 写档案；用档案 → 改面板。 */
  async function applySyncChoice(p, c, which) {
    const sh = p.shots.find((s) => (s.id || "") === (c.shot || ""));
    if (which === "fact" && sh && sh.state) {
      sh.state.length = c.fact;
      ui.commit();
    } else if (which === "preset") {
      const name = p.name || "";
      try {
        const res = await api.fetchApi("/cglide/chain/resize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, id: c.id, frames: c.preset }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
      } catch (err) {
        ui.lanesFlash("把档案时长改成面板预设失败：" + err.message);
      }
    }
  }

  /* moveShot(i, ±1) 已删（20261009）：它只服务右键菜单里的「← 前移 / 后移 →」，
     一次挪一位。那两个菜单项撤掉之后它就再没有调用者了 —— 留着是一段只会被
     误认为"排序还在"的死代码。排序改成了在轴上拽着段块走，落在
     csglide_cast_monitor.js 的 reorderTo()（工程与档案同改）。 */

  /* One place that flips the skip flag, reachable from the timeline dot,
   * the timeline Alt-click, and the project list dot. */
  function toggleSkip(i) {
    const p = ui.proj();
    const sh = p.shots[i];
    if (!sh) return;
    sh.off = !sh.off;
    ui.commit(); paintSegTools();
  }

  /* The link flag (sh.link === false means "break the chain") marks a seam as
   * NOT continuing from the previous clip. In continue mode, renderAll only
   * adopts the previous render into clips whose link is not explicitly false,
   * so the timeline lets you break one seam while the rest of the film stays
   * chained. */
  /* 断链 / 接上：唯一真值是工程映射 p.transitions（随工作流落盘）并同步后端
     /transitions（播放、导出成片读取）。旧实现只写 sh.link，导出仍连播、且与
     接缝卡片各写一套 —— 现统一走 ui.applyTransition。 */
  async function toggleLink(i) {
    if (ui.run) return;
    const p = ui.proj();
    const sh = p.shots[i];
    if (!sh || i < 1 || !sh.clip) return;
    const broken = !!(p.transitions && p.transitions[sh.clip]
      && p.transitions[sh.clip].type === "unlink");
    if (sh.link !== undefined) delete sh.link;  // 清掉旧的第二真值
    await ui.applyTransition(sh.clip,
      broken ? { type: "cut", overlap: 0 } : { type: "unlink", overlap: 0 });
    ui.commit();
    paintSegTools();
    if (typeof ui.paintLinkMarks === "function") ui.paintLinkMarks();
  }

  async function importShot() {
    const files = await ui.pickFilesForOpen();
    if (!files.length) return;
    ui.busy(files.length > 1 ? `正在导入 ${files.length}…` : "导入中…");
    const failed = [];
    try {
      const p = ui.proj();
      ui.stash();
      ui.snapProject("导入分段前");
      ui.seedFromScreen(p);
      /* name order, so a folder of shot-01 … shot-12 lands in film order */
      files.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
      for (const f of files) {
        try {
          const { state } = await ui.stateFromFile(f);
          p.shots.push({
            id: ui.uid(),
            name: f.name.replace(/\.(h3\.json|h3pack|json|zip)$/i, ""),
            state: parseInitial(JSON.stringify(state)),
          });
        } catch (e) {
          console.warn("[H3 Studio] could not import " + f.name, e);
          failed.push(f.name);
        }
      }
      p.idx = p.shots.length - 1;
      ui.restoreShotRack(p.shots[p.idx]);
      ui.load(JSON.stringify(p.shots[p.idx].state));
      ui.commit();
      if (failed.length) ui.lanesFlash("导入失败 —— " + failed.join(", "));
    } catch (e) {
      ui.lanesFlash("导入失败 —— " + e);
    } finally { ui.busy(null); }
  }

  function projectBlob() {
    const p = ui.proj();
    const out = {
      meta: { app: "H3 Studio project", version: 1, saved: new Date().toISOString() },
      name: p.name || "", idx: p.idx, shots: p.shots,
    };
    /* 媒体架快照（工程级）。素材住在「H3 媒体架」节点里，不进工程文件，所以在这里记
     * 一份当时每个槽位引用的文件名（含裁剪窗口、音轨开关、别名），打开工程
     * 时倒回媒体架就能还原。只记引用、不复制文件——文件得仍在 ComfyUI 的
     * input 目录里；要连文件一起走请用「打包」。
     * 每段自己的架况已随分段存在 shots[i].rack 里（见 stash/rackSnapshot），
     * 打开工程时优先用它；这里的工程级快照保留给旧版本脚本读写，两代互开不丢架。
     * 没接媒体架时整个字段不写：那是「当时没有媒体架」，不是「媒体架空着」。
     * 记成空快照会在打开工程时把用户后来摆好的素材清掉。 */
    if (rackNodeOf(ui.node)) out.rack = { slots: rackSlotsOf(ui.node).slots };
    return new Blob([JSON.stringify(out, null, 1)], { type: "application/json" });
  }

  async function saveProject(asNew) {
    const p = ui.proj();
    ui.stash();
    if (!p.shots.length) { ui.lanesFlash("本工程还没有任何分段"); return; }
    if (asNew) ui.projHandle = null;
    ui.busy("保存工程中…");
    let wrote = false;
    try {
      const blob = projectBlob();
      const stem = (p.name || "h3-project").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-") || "h3-project";
      const suggested = stem + ".h3proj.json";
      if (ui.projHandle) {
        const w = await ui.projHandle.createWritable();
        await w.write(blob); await w.close();
        ui.projLabel = ui.projHandle.name;
        wrote = true;
      } else if (window.showSaveFilePicker) {
        try {
          const h = await window.showSaveFilePicker({
            suggestedName: suggested,
            types: [{ description: "H3 Studio project", accept: { "application/json": [".json"] } }],
          });
          const w = await h.createWritable();
          await w.write(blob); await w.close();
          ui.projHandle = h; ui.projLabel = h.name;
          wrote = true;
        } catch (e) { if (!(e && e.name === "AbortError")) throw e; }
      } else {
        /* No File System Access here, so the file lands in the browser's
         * download folder with no dialog at all. Say so - a silent save
         * reads as a save that did not happen. */
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = suggested;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 10000);
        ui.projLabel = suggested;
        wrote = true;
        ui.lanesFlash("当前浏览器没有保存对话框，工程已作为 \u201c"
              + suggested + "\u201d 下载到你的下载文件夹。");
      }
      /* Snapshot AFTER a write that actually happened, never after an
       * abandoned dialog. Revert then means "back to what I last saved",
       * which is the useful reading whenever nothing destructive has
       * happened since - and if something has, that action took its own
       * snapshot and this one is already gone. */
      if (wrote) {
        ui.projJustSaved = true;
        ui.snapProject("保存工程文件后");
        setTimeout(() => { ui.projJustSaved = false; paintSegTools(); }, 6000);
      }
    } catch (e) {
      ui.lanesFlash("工程保存失败 —— " + e);
    } finally { ui.busy(null); }
  }

  /* Close the project without touching the shot on screen. Clearing the list
   * and wiping the node are different wishes: the usual reason to start a new
   * project is that the shot you are looking at is the first one of it, so it
   * is kept and the next Add makes it Shot 1. "Blank" still gives an empty
   * node when that is what you want. */
  async function newProject() {
    const p = ui.proj();
    const n = p.shots.length;
    const oldName = p.name || "";
    /* 不弹确认（点「新建工程」就新建），旧工程先存一份，「撤销」能带回来。 */
    ui.snapProject("新建工程前");
    /* 拿一个**不冲突**的默认片名 —— 段要登记进档案才上轴，重名会把新片写进
       旧片档案。用户已选「自动默认片名」。 */
    let name = "未命名工程";
    /* 带 6s 超时：连接池紧张时该请求会排队，不超时会让 newProject 永远
       卡在 await、"新建工程"点了没反应。超时就退回固定默认名，不阻塞新建。 */
    try {
      const _uc = new AbortController();
      const _ut = setTimeout(() => _uc.abort(), 6000);
      const res = await api.fetchApi("/cglide/chain/unique_name", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base: "未命名工程" }),
        signal: _uc.signal,
      });
      clearTimeout(_ut);
      const d = await res.json().catch(() => ({}));
      if (d.name) name = d.name;
    } catch { /* 网络异常/超时退回固定默认名 */ }
    /* 全新工程：**代际 gen +1** —— 文案台 / 媒体架轮询到新代际会把各自的
       提示词 / 素材彻底清空（用户报的「新建后联动节点还是旧内容」）。
       重置后立刻补一个空白第一段，时间轴永远保底一段（用户定的逻辑）。 */
    const gen = (p.gen || 0) + 1;
    ui.node.properties.gcast_project = { name, shots: [], idx: -1, gen };
    ui.projHandle = null; ui.projLabel = "";
    /* 面板所有参数归位到空白默认（档位 / 锚点 / 钉帧 / 时长 / 画布 / 参考） */
    ui.st = blankState();
    ui.commit();
    ui.render();
    paintSegTools();
    /* 时间轴归零、清掉旧片缓存（含音频轴）；第一段登记完 registerShot 会
       自己 refresh，这里不重复 refresh。 */
    try { if (ui.monitor) ui.monitor.reset(); } catch { /* 已销毁 */ }
    /* 新代际即时广播：媒体架 / 文案台立刻清空，不再等 1~3 秒轮询（20261012）。 */
    try { emitCastChange({ name, gen, clip: "", reason: "project" }); } catch (_) {}
    /* 自动建一个空白第一段：不继承班底 / 素材 / 提示词 */
    ui.restoreDefaultShot(true);
    ui.lanesFlash(n
      ? `已新建「${name}」—— 旧工程「${oldName || "当前工程"}」的 ${n} 段已关闭，可「撤销」还原`
      : `已新建「${name}」`);
  }

  /* A packed project: one zip, every shot, media inside, dedup shared across
   * shots. The .h3proj.json stays the working file; this is the deliverable.
   * So packing always asks where to put it and never becomes projHandle -
   * otherwise "Save project" would start silently rewriting a huge export. */
  async function packProject() {
    const p = ui.proj();
    ui.stash();
    if (!p.shots.length) { ui.lanesFlash("本工程还没有任何分段"); return; }
    ui.busy("打包工程中…");
    try {
      const shots = ui.clone(p.shots);
      const entries = [];
      const add = ui.makeAssetAdder(entries, new Map(), new Set());
      for (const s of shots) {
        await ui.packSlots(s.state.slots, add);
        /* 每段媒体架快照引用的素材同样进包，与所有段共用 dedup，不重复存储 */
        if (s.rack && s.rack.slots) await ui.packSlots(s.rack.slots, add);
      }
      /* 媒体架素材也进包：工程文件只记引用，文件不跟着走的话，换台电脑打开
       * 还原出来的就是空壳。与 shots 共用同一个 dedup map，两边都引用的素材
       * 只存一份。必须 clone 后再 add——add 会就地把 item.file 改写成包内文件
       * 名，直接用会污染导演台正在展示的那份缓存。 */
      const rack = rackNodeOf(ui.node) ? { slots: ui.clone(rackSlotsOf(ui.node).slots) } : null;
      if (rack) await ui.packSlots(rack.slots, add);

      const enc = new TextEncoder();
      entries.unshift({ name: "project.json", data: enc.encode(JSON.stringify({
        meta: { app: "H3 Studio project", version: 1, packed: true,
                shots: shots.length, saved: new Date().toISOString() },
        name: p.name || "", idx: p.idx, shots,
        ...(rack ? { rack } : {}),
      }, null, 1)) });

      const stem = (p.name || "h3-project").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-") || "h3-project";
      /* Named .h3proj.zip rather than .h3projpack: it IS a zip, and an
       * extension the OS recognises is one less thing to explain to whoever
       * you hand it to. openProject still accepts the old .h3projpack. */
      const suggested = stem + ".h3proj.zip";

      /* The zip writer uses 32-bit offsets and sizes (no ZIP64) and stores
       * entries uncompressed, so the whole archive has to stay under 4 GB.
       * Video reference clips are packed WHOLE - trim points are metadata -
       * so a few masters get there faster than you would think. Better to
       * say so now than to hand someone a silently corrupt file. */
      let total = 0;
      for (const e of entries) total += e.data.length;
      if (total > 3.8e9) {
        const gb = (total / 1e9).toFixed(1);
        ui.lanesFlash(`本工程打包后约 ${gb} GB，已超出打包格式 4 GB 上限 —— 请先裁剪或转码参考视频`);
        return;
      }
      const blob = await ui.zipWrite(entries);

      if (window.showSaveFilePicker) {
        try {
          const h = await window.showSaveFilePicker({
            suggestedName: suggested,
            types: [{ description: "H3 Studio project pack",
                      accept: { "application/zip": [".zip", ".h3projpack"] } }],
          });
          const w = await h.createWritable();
          await w.write(blob); await w.close();
        } catch (e) { if (!(e && e.name === "AbortError")) throw e; }
      } else {
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = suggested;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 20000);
        ui.lanesFlash("当前浏览器没有保存对话框，打包结果已作为 \u201c"
              + suggested + "\u201d 下载到你的下载文件夹。");
      }
    } catch (e) {
      ui.lanesFlash("工程打包失败 —— " + e);
    } finally { ui.busy(null); }
  }

  async function openProject() {
    const picked = await ui.pickFileForOpen();
    if (!picked) return;
    ui.busy("载入工程中…");
    try {
      const isPack = /\.(h3projpack|zip)$/i.test(picked.file.name);
      let d, remap = null;

      if (isPack) {
        const map = await ui.zipRead(await picked.file.arrayBuffer());
        const raw = map.get("project.json");
        if (!raw) throw new Error("打包文件缺少 project.json");
        d = JSON.parse(new TextDecoder().decode(raw));
        /* restore each asset once, however many shots point at it */
        remap = new Map();
        for (const [name, data] of map) {
          if (!name.startsWith("assets/")) continue;
          const base = name.slice(7);
          try {
            remap.set(base, await uploadFile(new File([new Blob([data])], base)));
          } catch (e) {
            console.warn("[H3 Studio] could not restore " + base, e);
          }
        }
      } else {
        d = JSON.parse(await picked.file.text());
      }

      if (!d || !Array.isArray(d.shots)) throw new Error("不是有效的 H3 Studio 工程文件");
      const p = ui.proj();
      /* 不弹确认（打开 = 直接替换当前工程）。「撤销」能把替换前的工程带回来。 */
      ui.snapProject("打开工程前 · " + picked.file.name);
      p.name = typeof d.name === "string" ? d.name : "";
      /* every shot goes through parseInitial, so a project written by an
       * older build cannot smuggle in fields this one does not know.
       * rack 是本版本新增的每段媒体架快照，同样按白名单收下。 */
      p.shots = d.shots.map((s) => ({
        id: (s && s.id) ? String(s.id) : ui.uid(),
        name: (s && typeof s.name === "string") ? s.name : "",
        state: parseInitial(JSON.stringify((s && s.state) || {})),
        rack: (s && s.rack && typeof s.rack === "object" && s.rack.slots) ? s.rack : undefined,
        /* 时间轴字段：跳过 / 链扣 / 颜色随段走，旧工程没有这些字段则取缺省。 */
        off: !!s.off,
        link: s.link === false ? false : undefined,
        colour: (s && typeof s.colour === "string") ? s.colour : undefined,
      }));
      if (remap) {
        const fix = (it) => { if (it && it.file && remap.has(it.file)) it.file = remap.get(it.file); };
        p.shots.forEach((s) => {
          const sl = s.state.slots || {};
          fix(sl.first); fix(sl.last);
          (sl.images || []).forEach(fix);
          (sl.videos || []).forEach(fix);
          (sl.audios || []).forEach(fix);
          /* 每段媒体架快照里存的也是包内文件名，同样要指到上传后的文件 */
          const rk = (s.rack && s.rack.slots) || null;
          if (rk) {
            (rk.images || []).forEach(fix);
            (rk.videos || []).forEach(fix);
            (rk.audios || []).forEach(fix);
          }
        });
        /* 媒体架快照里存的也是包内文件名，同样要指到上传后的文件，否则倒回
         * 媒体架时一个都读不到。 */
        const rk = (d.rack && d.rack.slots) || null;
        if (rk) {
          (rk.images || []).forEach(fix);
          (rk.videos || []).forEach(fix);
          (rk.audios || []).forEach(fix);
        }
      }
      p.idx = p.shots.length ? Math.min(Math.max(0, +d.idx || 0), p.shots.length - 1) : -1;
      /* a pack is an import, not a working file - don't let Save write over it */
      ui.projHandle = isPack ? null : (picked.handle || null);
      ui.projLabel = picked.file.name;
      /* 把工程里记的素材清单倒回媒体架。当前分段有自己的快照就优先用它
       * （每段各归各位）；没有才退回工程级的整体快照（旧版工程的唯一记录）。
       * 两样都没有就一律不动媒体架——否则会把用户现在摆好的素材清掉。
       * 没接媒体架时同样跳过。 */
      const rn = rackNodeOf(ui.node);
      if (rn && rn.gmr) {
        const cur = p.shots[p.idx];
        if (cur && cur.rack) { rn.gmr.load(cur.rack); rn.gmr.commit(); }
        else if (d.rack) { rn.gmr.load(d.rack); rn.gmr.commit(); }
      }
      if (p.idx >= 0) ui.load(JSON.stringify(p.shots[p.idx].state));
      ui.commit();
      paintSegTools();
      ui.lanesFlash(`已打开「${picked.file.name}」（${p.shots.length} 个分段）`);
    } catch (e) {
      ui.lanesFlash("打开工程失败 —— " + e);
    } finally { ui.busy(null); }
  }

  /** 轴头可用状态 + 段头计数 + 总览轴计数，一次刷完。
     它是面板里唯一被到处调用的重绘入口：任何一次改动（换段、跳过、断链、
     保存、渲染开始/结束）都要走它。
     「＋ 接一段 / 🗑 删除 / ⇥ 接上一段」三颗按钮已撤（20261010 用户选方案 D）：
     段级操作改为悬停段块/接缝就地交互（见 csglide_cast_monitor.js 的
     悬停工具条与接缝插入点）。轴头只在工程为空时亮出「＋ 接第一段」。 */
  function paintSegTools() {
    /* 「＋ 接一段」常驻轴头（20261010 用户定）：接段是最高频的动作，
       不该依赖悬停才发现入口。渲染中禁用；无名点它会被 addShot 拦下并
       聚焦工程名（见 csglide_cast_shots.js 的 requireName）。 */
    if (ui.btnAddSeg) {
      ui.btnAddSeg.disabled = !!ui.run;
      ui.btnAddSeg.title = ui.run
        ? "正在渲染 —— 等这一轮跑完"
        : "在末尾接一段（保留当前参考素材与画布）。\n"
          + "没填工程名时会先拦下并把光标放到工程名上 —— 档案按片名建目录，"
          + "没名字登记不进档案";
    }
    /* 轴头撤销 / 重做 / 历史按钮已删（20261012 用户定），对应的状态刷新
       一并撤下 —— 还原点数据本身保留，快捷键 Ctrl+Z / Ctrl+Shift+Z 仍可用。 */
    ui.paintRenderRow();
    ui.paintRunScope();
    ui.paintLinkMarks();
    ui.updateSegChip();
    ui.updateLanesMeta();
    ui.paintProjName?.();
    ui.paintSaveDot?.();
  }


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { syncCheck, askSync, applySyncChoice, toggleSkip, toggleLink, importShot, projectBlob, saveProject, newProject, packProject, openProject, paintSegTools });
}
