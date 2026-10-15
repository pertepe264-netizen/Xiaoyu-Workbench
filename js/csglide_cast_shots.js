import { rackNodeOf, rackSlotsOf, emitCastChange, deskNodeOf } from "./bz_rack.js";
import { blankState } from "./csglide_cast_core.js";
import { fieldText } from "./csglide_prompt_check.js";

/* ============================================================
 * 分段 与 上一份副本（.bak）
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installShots(ui) {
  /* ---- shots: several shots in one project --------------------------
   *
   * A film is a list of shots that mostly share their references, so a file
   * per shot loses the relationship and repeats the work. A project holds
   * the shot states together.
   *
   * The single-shot Save / Save as / Save packed / Load buttons are NOT
   * touched: every existing .h3.json and .h3pack keeps loading exactly as
   * before, and a project is a separate file with its own two buttons.
   *
   * Shots live in node.properties, which LiteGraph serialises with the
   * workflow. A project therefore survives a browser reload without ever
   * being written to disk -- the file is for moving it somewhere else, not
   * for not losing it.
   *
   * Media is referenced by filename, as in .h3.json. There is no packed
   * project yet: the files have to still be in ComfyUI's input folder.
   */

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const uid = () => Math.random().toString(36).slice(2, 9);
  ui.projHandle = null;
  ui.projLabel = "";

  function proj() {
    ui.node.properties = ui.node.properties || {};
    const p = ui.node.properties.gcast_project;
    if (!p || !Array.isArray(p.shots)) {
      ui.node.properties.gcast_project = { name: "", shots: [], idx: -1 };
    }
    return ui.node.properties.gcast_project;
  }

  /* ---- project restore history: the latest 10 points ------------------
   *
   * There is exactly ONE copy of a project in node.properties. New, Open,
   * Import, Delete, Rename, Render-all, Split and ghost-sweep each rewrite
   * it, and a mis-click could otherwise cost the whole film. Before any
   * such action the current project is pushed onto gcast_project_hist as
   * a restore point. The ring keeps the newest 10 points (oldest dropped).
   *
   *   Undo  -> jump to the newest point;
   *   Redo  -> come back after an undo;
   *   History menu -> all points listed; any one restores the WHOLE
   *   project to that moment.
   *
   * Before restoring, the current project is itself saved as a
   * "before restore" point, so every jump has a way back.
   *
   * Kept in properties (works everywhere, survives reload, no directory
   * handle needed). Points hold project config only - clip list, prompts,
   * rack references - never disk files; those stay on disk.
   *
   * Deliberately NOT taken on the autosave that fires when you switch
   * clip: it runs constantly while browsing and would flush real points.
   */
  const HIST_MAX = 10;
  const REDO_MARK = "还原前";
  /* "未来栈"里的点 why 以 REDO_MARK 开头。撤销时当前状态打上这个标记进
     未来栈；重做时剥掉一层、原始 why 复活回过去栈。 */
  const isRedoMark = (why) => String(why || "").startsWith(REDO_MARK);
  const markRedo = (why) => REDO_MARK + (why ? "（" + why + "）" : "");
  function unmarkRedo(why) {
    const s = String(why || "");
    if (!s.startsWith(REDO_MARK)) return s;
    const m = s.match(/^还原前（([\s\S]*)）$/) || s.match(/^还原前\s*([\s\S]*)$/);
    return m ? m[1] : "";
  }

  function projSnapshot() {
    const p = ui.node.properties && ui.node.properties.gcast_project;
    if (!p || !Array.isArray(p.shots) || !p.shots.length) return null;
    return { name: p.name || "", idx: p.idx, shots: clone(p.shots) };
  }

  /* The history bag: node.properties.gcast_project_hist = { items:[old…new] }.
   * On first touch the old single gcast_project_prev is migrated in once. */
  function histBag(ensure) {
    ui.node.properties = ui.node.properties || {};
    let h = ui.node.properties.gcast_project_hist;
    const old = ui.node.properties.gcast_project_prev;
    if (!h || !Array.isArray(h.items)) {
      if (!ensure) return { items: [] };
      h = { items: [] };
      ui.node.properties.gcast_project_hist = h;
    }
    if (old && Array.isArray(old.shots) && old.shots.length) {
      h.items.push(old);
      delete ui.node.properties.gcast_project_prev;
    }
    return h;
  }

  /* Push the current project as a restore point. A point identical to the
   * last is skipped. */
  function snapProject(why) {
    const snap = projSnapshot();
    if (!snap) return;                     // an empty project has nothing to lose
    const h = histBag(true);
    /* 新动作 = 时间线分叉：撤销留下的"未来"点（REDO）从此作废 —— 标准
       撤销/重做语义里必须清空未来栈，否则重做会跳回一条已不属于当前
       时间线的状态。REDO 点连续排在末尾，从尾 pop。 */
    while (h.items.length && isRedoMark(h.items[h.items.length - 1].why)) {
      h.items.pop();
    }
    const last = h.items[h.items.length - 1];
    if (last && last.name === snap.name
      && JSON.stringify(last.shots) === JSON.stringify(snap.shots)) return;
    h.items.push({
      why: why || "", at: Date.now(),
      name: snap.name, idx: snap.idx, shots: snap.shots,
    });
    while (h.items.length > HIST_MAX) h.items.shift();
  }

  /* Newest PAST-stack point (undo target). Redo points may sit at the tail
   * (the future stack) — scan backwards past every marked entry. */
  function prevProject() {
    const items = histBag(false).items;
    for (let k = items.length - 1; k >= 0; k--) {
      const b = items[k];
      if (!isRedoMark(b.why) && Array.isArray(b.shots) && b.shots.length) return b;
    }
    return null;
  }

  /* Redo target: the newest point whose why starts with the restore mark
   * (the point an undo leaves behind). */
  function redoProjectItem() {
    const items = histBag(false).items;
    const b = items[items.length - 1];
    return (b && isRedoMark(b.why)) ? b : null;
  }

  function agoText(ms) {
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return "刚刚";
    const m = Math.round(s / 60);
    if (m < 60) return m + " 分钟前";
    const h = Math.round(m / 60);
    return h < 24 ? h + " 小时前" : Math.round(h / 24) + " 天前";
  }

  function prevWhen(b) {
    return agoText(b.at) + (b.why ? " · " + b.why : "");
  }

  /* Restore one history entry:
   * 1. the current project is pushed as a "before restore" point (an empty
   *    project leaves nothing);
   * 2. the chosen entry is removed from history (it becomes "now");
   * 3. the whole project is replaced by it and the active clip's rack and
   *    params follow. Disk files are never touched. */
  function restoreHistoryEntry(target) {
    const h = histBag(true);
    const index = h.items.indexOf(target);
    if (index < 0) return false;
    stash();
    const b = h.items[index];
    const forward = isRedoMark(b.why);     // target sits in the redo/future stack
    /* Take the target out first; it is about to become "now". */
    h.items.splice(index, 1);
    const now = projSnapshot();
    if (now) {
      const entry = { at: Date.now(), name: now.name, idx: now.idx, shots: now.shots };
      if (forward) {
        /* Redo: the present goes back onto the PAST stack — its why is the
           target's why with one mark stripped. Insert just before the
           remaining redo points so they stay contiguous at the tail. */
        entry.why = unmarkRedo(b.why);
        let k = h.items.findIndex((i) => isRedoMark(i.why));
        if (k < 0) k = h.items.length;
        h.items.splice(k, 0, entry);
      } else {
        /* Undo: the present enters the FUTURE stack; push marked at tail. */
        entry.why = markRedo(b.why);
        h.items.push(entry);
      }
    }
    while (h.items.length > HIST_MAX) h.items.shift();
    ui.node.properties.gcast_project = { name: b.name || "", idx: b.idx, shots: clone(b.shots) };
    const p = proj();
    p.idx = p.shots.length ? Math.min(Math.max(0, +p.idx || 0), p.shots.length - 1) : -1;
    if (p.idx >= 0) {
      restoreShotRack(p.shots[p.idx]);
      ui.load(JSON.stringify(p.shots[p.idx].state));
    }
    ui.commit();
    ui.paintSegTools();
    return true;
  }

  /* Undo: jump to the newest restore point. */
  function undoProject() {
    const b = prevProject();
    if (!b) return;
    const n = b.shots.length;
    const when = prevWhen(b);
    if (!restoreHistoryEntry(b)) return;
    ui.lanesFlash(`已还原到 ${when} 的工程（${n} 段）—— 点「重做」可退回`);
  }

  /* Redo: return to the project an undo left from. */
  function redoProject() {
    const b = redoProjectItem();
    if (!b) return;
    const n = b.shots.length;
    if (!restoreHistoryEntry(b)) return;
    ui.lanesFlash(`已重做，工程回到还原前（${n} 段）`);
  }

  /* 这份架况里到底有没有素材（图片/视频/音频任一有 file 就算）。
     「segs[key] 存在」不等于「有素材」——空快照也是对象，靠它判断会不会
     把本来该灌回的素材挡在门外，这一段就永远空着。 */
  function rackHasFile(rack) {
    const s = (rack && rack.slots) || {};
    for (const k of ["images", "videos", "audios"]) {
      const arr = Array.isArray(s[k]) ? s[k] : [];
      if (arr.some((x) => x && x.file)) return true;
    }
    return false;
  }

  /* 媒体架的分段快照。素材住在「H3 媒体架」节点里，现在跟着分段一起记：
   * 每段存一份当时的完整架况（槽位引用 + 裁剪窗 + 别名 + 帧率 + 音频输出
   * 编号），切换、打开、渲染时倒回，各段互不串台。只记引用不复制文件——
   * 要连文件一起走请用「打包」。没接媒体架时返回 null、字段不写：那是
   * 「当时没有媒体架」，不是「媒体架空着」。 */
  /* 把媒体架段记忆里的某段整理成标准快照（无料/不存在返回 null）。 */
  function segMemSnap(rn, key) {
    const mst = rn.gmr && rn.gmr.st;
    if (!key || !mst || !mst.segs) return null;
    const v = mst.segs[key];
    if (!v || !rackHasFile(v)) return null;
    return {
      slots: (v.slots && typeof v.slots === "object")
        ? JSON.parse(JSON.stringify(v.slots))
        : { images: [], videos: [], audios: [] },
      fps: v.fps || 0,
      audio_out: v.audio_out || 0,
    };
  }

  /* 从 rack_data widget 序列化值构造「当前架面」快照（无料返回 null）。 */
  function widgetSnap(rn) {
    const raw = (rn.widgets && rn.widgets.find((x) => x.name === "rack_data")?.value) || "";
    if (!raw) return null;
    try {
      const d = JSON.parse(raw);
      if (!d || typeof d !== "object") return null;
      /* 只取「这一段」的槽位快照，剥离 segs / cur —— 那是媒体架的全局状态
         （各段记忆 + 当前编辑段），不是某一段的素材。整份塞进 shots[i].rack
         再经 loadSeg 灌回，会把全局 segs/cur 当成段快照回写，各段快照被互相
         污染成同一份（20261012「点第1段显示第3段」的串台根因）。 */
      const snap = {
        slots: (d.slots && typeof d.slots === "object")
          ? JSON.parse(JSON.stringify(d.slots))
          : { images: [], videos: [], audios: [] },
        fps: d.fps || 0,
        audio_out: d.audio_out || 0,
      };
      /* 段级素材防丢（20261012「渲染后每段只剩片级首图」）：widget 序列化值
         可能偏旧（热重载旧实例、load 签名去重等），媒体架**内存**里当前段
         的槽位可能比 widget 全。只在媒体架当前认的段 == 导演台当前段时补齐
         （防止把别的段素材错补进来），只补不删。 */
      try {
        const mst = rn.gmr && rn.gmr.st;
        const curClip = (ui.st && ui.st.chain && ui.st.chain.clip) || "";
        if (mst && curClip && String(mst.cur) === String(curClip)) {
          const mem = (mst.segs && mst.segs[mst.cur]) || null;
          mergeRackSlots(snap.slots, mem ? mem.slots : mst.slots);
        }
      } catch (e) { console.warn("[H3 Studio] rackSnapshot 内存补齐失败(忽略)：", e); }
      /* 空架况（一个素材都没有）不存为快照 —— 空 object 一旦写进
         shots[i].rack，restoreShotRack 就会把它当成"这一段就是空的"权威副本，
         在用户刚摆图还没切段时把图盖掉。复制段走的是整份 shot 克隆，不经过这里。 */
      if (!rackHasFile(snap)) return null;
      return snap;
    } catch (_) { return null; }
  }

  function rackSnapshot(cid) {
    const rn = rackNodeOf(ui.node);
    if (!rn) return null;
    const keyCid = cid ? String(cid) : "";
    /* 带段身份（stash 切段用）：onPick 后 set_current 广播会让媒体架**先于**
       导演台 switchTo 把架面切到新段 —— 此时再读"当前架面" widget，读到的是
       新段的图，却写进旧段的 shots[i].rack；下一次切回旧段，restoreShotRack
       拿着新段的图把旧段盖住（20261013 日志铁证：段2 被存成段3 的图，两图
       对调、快速切段后三段全变成同一张）。segs[cid] 是媒体架按段身份记的账、
       switchSeg 离开旧段时第一时间写入，与架面现在停在哪无关，是竞态免疫通道。 */
    if (keyCid) {
      const mst = rn.gmr && rn.gmr.st;
      /* 架面已停在别的段：widget 是别的段的，绝不能碰，只信段记忆；记忆空就
         如实返回 null（"这一段没素材"），不能拿别段/空架面填。 */
      if (mst && String(mst.cur) !== keyCid) return segMemSnap(rn, keyCid);
      /* 架面正好停在该段：widget 可能比段记忆新（刚摆图、切段尚未发生），读
         widget，再用段记忆只补不删；widget 没值时段记忆也能救回来。 */
      const segMem = segMemSnap(rn, keyCid);
      const snap = widgetSnap(rn);
      if (snap && segMem) mergeRackSlots(snap.slots, segMem.slots);
      return snap || segMem;
    }
    /* 无 cid（addShot / 复制 / 渲染等）：架面就停在目标段，读当前架面。 */
    return widgetSnap(rn);
  }

  /* 槽位补齐：dst（widget 序列化值）里**空着**、src（媒体架内存段记忆/当前
     架面）同槽位**有 file** 时，把 src 的条目深拷贝补进 dst；dst 已有的条目
     一律不动，src 多出的尾部条目追加。只补不删、按槽位对齐 —— widget 值
     偏旧时抢救段级素材用，绝不反向覆盖。 */
  function mergeRackSlots(dst, src) {
    if (!dst || !src) return;
    for (const kind of ["images", "videos", "audios"]) {
      if (!Array.isArray(dst[kind])) dst[kind] = [];
      const s = Array.isArray(src[kind]) ? src[kind] : [];
      for (let i = 0; i < s.length; i++) {
        const sv = s[i];
        if (!sv || !sv.file) continue;
        if (i < dst[kind].length) {
          if (!dst[kind][i] || !dst[kind][i].file) dst[kind][i] = JSON.parse(JSON.stringify(sv));
        } else {
          dst[kind].push(JSON.parse(JSON.stringify(sv)));
        }
      }
    }
  }

  /* 把分段存的快照倒回媒体架。没有快照就一动不动——老工程、建段时没接
   * 媒体架的分段都没有这个字段，绝不能拿空快照把用户现在摆好的素材清掉。
   * 20261012：走 loadSeg 连同段身份一起认（segs[key]=快照、cur=key）——
   * 旧实现只 load 快照，cur 被置空，1~2 秒后媒体架轮询发现档案 current 是
   * 新段、自己又没存它的快照，照清空不误，参考图"复制/接段后闪一下消失"。 */
  /* 媒体架热重载旧实例警示只弹一次（本次页面生命周期内）。 */
  let warnedStaleRack = false;
  function restoreShotRack(shot) {
    const rn = rackNodeOf(ui.node);
    if (!rn || !rn.gmr || !shot || !shot.rack || typeof shot.rack !== "object") return;
    const key = shot.clip || shot.id;
    const st = rn.gmr.st || {};
    const mem = st.segs ? st.segs[key] : null;
    const onKey = st.cur === key;
    const memHasFile = !!(mem && rackHasFile(mem));
    const snapHasFile = rackHasFile(shot.rack);
    /* 媒体架自己已经记着这一段 **且那段记忆里确实有素材**、人也在本段 →
       以媒体架为准，不拿工程快照覆盖。工程快照（shots[i].rack）可能是素材
       串台时期存下的脏副本 —— 无条件灌回会把用户摆好的素材又盖成旧图，
       渲染三段全用同一张参考图（20261012 渲染日志铁证：三段参考图都是
       0039.png）。
       还必须**人就在这一段上**（st.cur === key）：架子的 cur 可能被别处
       切走 —— store 订阅（后端 current 变化 / 另一个标签页点的段）和
       setCurrent 广播都不受渲染循环支配。只判记忆不判 cur，架子停在别段时
       这一行直接 return，渲染就会拿着别段的素材提交。 */
    if (onKey && memHasFile) return;
    /* 空快照（字段在、三类槽位一个 file 都没有）永不灌入：它是建段时架子
       空着记下的，用户却可能刚在这一段摆了图、还没来得及切段 stash。空灌
       零收益，只会把刚摆的素材清掉，随后到达的 switch 广播还会把空架面
       固化进段记忆（20261012 日志铁证：摆图→onPick 一圈，图被空快照盖没、
       segs 里也变空）。
       - 人在本段：架面以当前操作为准，一动不动；
       - 人不在段但记忆有料：切回本段让记忆上屏（switchSeg 先存好当前段）；
       - 人不在段、记忆也空：本来就是空，什么都不用做。 */
    if (!snapHasFile) {
      if (!onKey && memHasFile && typeof rn.gmr.switchSeg === "function") {
        try { rn.gmr.switchSeg(key); } catch (e) {
          console.warn("[联动] 导演台 restoreShotRack 切回段失败(忽略):", e);
        }
      }
      return;
    }
    const fi = (() => { try { const s = (shot.rack && shot.rack.slots) || {}; const imgs = s.images || []; return (imgs[0] && imgs[0].file) ? imgs[0].file.split(/[\\/]/).pop() : "(空)"; } catch (_) { return "(err)"; } })();
    console.log("[联动] 导演台 restoreShotRack: key=" + key + " shot.id=" + (shot && shot.id) + " rack首图=" + fi);
    try {
      if (memHasFile && typeof rn.gmr.switchSeg === "function") {
        /* 记忆有料但人不在段上 → 切回本段：switchSeg 先存好当前段、再灌本段记忆 */
        rn.gmr.switchSeg(key);
      } else if (typeof rn.gmr.loadSeg === "function") {
        rn.gmr.loadSeg(key, shot.rack);
      } else {
        /* 旧实例兜底：媒体架节点是热重载前创建的（热重载只重注册扩展，不重跑
           onNodeCreated，node.gmr 停在旧 bag：有 load 无 loadSeg/switchSeg）。
           这条路径不认段身份，是段级素材被污染的直接诱因，所以：
           ①load 必须 force=true —— 否则撞签名去重（load 内 sig===lastLoadSig
           直接 return），灌快照被静默跳过，架面停在上一段；
           ②红字常驻提示硬刷新（只提示一次，不刷屏），混搭状态下继续摆素材
           仍可能丢，必须让用户知道要 Ctrl+F5。 */
        rn.gmr.load(shot.rack, true);
        if (typeof rn.gmr.commit === "function") rn.gmr.commit();
        if (!warnedStaleRack) {
          warnedStaleRack = true;
          const msg = "检测到媒体架节点是热重载前的旧实例（缺段记忆接口），段素材联动可能错乱 —— 请按 Ctrl+F5 硬刷新页面后再摆素材/渲染";
          console.warn("[H3 Studio]", msg);
          try {
            const h = ui.flashBar;
            if (h) { h.textContent = msg; h.title = msg; h.classList.add("on"); }
          } catch (_) {}
        }
      }
    } catch (e) {
      /* 倒回素材只是"把架子摆到位"的辅助动作，炸在这里会让整轮「全部渲染」
         停在半路（20261012 NotFoundError 铁证：第 2 段处中断）。失败就记一笔
         继续跑，本段该用的素材仍以媒体架当前架况提交，不牵连后面几段。 */
      console.warn("[联动] 导演台 restoreShotRack 失败(已忽略，不影响渲染):", e);
    }
  }

  /* 当前段/工程身份变化的即时广播：媒体架、文案台、Qwen 台监听后立刻切段，
     不再等 0.8~1.5 秒轮询（轮询保留为 3 秒兜底）。 */
  function broadcastSeg(reason) {
    try {
      const p = proj();
      const shot = p.shots[p.idx];
      emitCastChange({
        name: ui.castName() || "",
        clip: (shot && shot.clip) || "",
        shot: (shot && shot.id) || "",
        idx: p.idx, gen: p.gen || 0,
        reason: reason || "seg",
      });
    } catch (_) { /* 广播失败退回轮询，不挡操作 */ }
  }

  /* Autosave. The shot being left is written back before anything else
   * happens, so browsing the list can never cost an edit. */
  function stash() {
    const p = proj();
    if (p.idx >= 0 && p.shots[p.idx]) {
      p.shots[p.idx].state = clone(ui.st);
      const cid = (ui.st && ui.st.chain && ui.st.chain.clip) || p.shots[p.idx].clip;
      /* 媒体架跟段走：必须按**段身份**取快照（20261013 两图对调根因）。
         媒体架收到 set_current 广播会先于导演台 switchTo 把架面切到新段，
         此时读"当前架面"会读到新段的图、写进旧段 shot.rack；传 cid 后
         rackSnapshot 从媒体架段记忆 segs[cid] 取，免疫架面停在哪。 */
      const snap = rackSnapshot(cid);
      if (snap) p.shots[p.idx].rack = snap;
      else delete p.shots[p.idx].rack;
      /* 有 clip id 就把完整面板防抖落盘（工程库 / 重启可恢复） */
      if (cid && ui.persistClipState) ui.persistClipState(cid, p.shots[p.idx].state);
    }
  }

  /* Vocabulary, kept straight on purpose: a CLIP is one generation, and a clip
   * can contain several SHOTS - the [Shot N] markers the model cuts on. The
   * project list talks about clips; the timeline strip talks about shots.
   * Internals and the file format still say "shots" so old projects load. */
  const shotLabel = (s, i) => s.name || `分段 ${i + 1}`;


  /* A new shot in a film usually reuses the same cast and location, so it
   * starts from the current one. The prompt does not carry over -- except
   * subject_definitions, which describes the references that just came with
   * it and would only be retyped. */
  function inheritedState() {
    const s = clone(ui.st);
    const subs = fieldText(ui.effPrompt(), "subject_definitions");
    s.prompt = subs ? `subject_definitions:\n${subs}\n\n` : "";
    return s;
  }

  /* 新分段的段身份必须是**新的**：clip（档案里的 id）与 shot（分段 uid）都
     不能从上一段继承过来。
     clip 尤其危险 —— 后端认段的优先级是「clip id → shot id → 新建」，顶着
     上一段的 clip id 去登记/渲染，等于把上一段的档案整段覆盖掉。
     抽成一个函数：新建分段有两条路径（接一段 / 把屏幕内容变成第 1 段），
     漏掉任何一条都会留下同一个坑。 */
  function detachChain(s) {
    s.chain = { name: (s.chain && s.chain.name) || "", clip: "", shot: "" };
    return s;
  }

  /* The first Add turns what is on screen into Shot 1 rather than losing it. */
  function seedFromScreen(p) {
    if (p.shots.length === 0 && p.idx < 0) {
      const s = detachChain(clone(ui.st));
      normalizeNewShotCont(s);
      const shot = { id: uid(), name: "", state: s };
      const snap = rackSnapshot();
      if (snap) shot.rack = snap;
      p.shots.push(shot);
      p.idx = 0;
    }
  }

  /* 新分段默认潜空间续写（工程链路的常规档位）；继承了明确文件来源的
   * （用户手动引用过的文件窗口）保持文件档不动。只用于新建分段，
   * 载入老工程不走这里——老工程没有续写信息时仍按文件档，行为逐帧不变。 */
  function normalizeNewShotCont(s, hasPrev) {
    if (!(s.cont && s.cont.file)) {
      s.cont = Object.assign({}, s.cont, { mode: "latent" });
    }
    if (hasPrev) {
      /* 第 2+ 段：默认顺序续写，头部承接窗口 39 帧（出厂默认，与后端
         csglide_cast cont.frames 缺省值、registerShot 给档案的 trim_frames 同口径）。
         旧版这里不写 frames，时间轴菱形按 0 画成灰色"硬切"，但后端仍按
         缺省续写 —— 显示与执行不一致，正是"接缝灰着像没接上"的误会源。
         latent_file 是上段的续拍存档身份，不能继承（渲染前 cast_run 会按
         段序重新认领本段该接的那一版）。 */
      delete s.cont.latent_file;
      if (!(Number(s.cont.frames) > 0)) s.cont.frames = 39;
    }
  }

  /* 无工程名先拦下（消灭"点了没反应"）：档案按片名建目录，没名字就登记不进
     档案，轴上永远不会出现块 —— 与其"加了段却看不见"，不如先让你把名字填了。
     拦下的同时把焦点直接交到工程名输入框上，用户不用自己找那一格。 */
  function requireName() {
    if ((ui.castName() || "").trim()) return true;
    ui.lanesFlash("还没填工程名 —— 已把光标放到工程名上，填好再点「＋」，段会立刻登记上轴");
    ui.focusProjName?.();
    return false;
  }

  function addShot(blank) {
    /* 整条链包进 try：这条按钮是「点了到底有没有反应」的最高频质疑点，
       任何一步炸掉都不能再静默 —— 静默 = 用户以为面板坏了，而控制台他没开。 */
    if (!requireName()) return;
    try {
      const p = proj();
      stash();
      snapProject("接一段前");
      seedFromScreen(p);
      const s = detachChain(blank ? blankState() : inheritedState());
      /* seedFromScreen 已保证工程里至少有一段，所以新段恒为第 2+ 段，
         默认潜空间顺序续写 39 帧（出厂默认承接窗口）。 */
      normalizeNewShotCont(s, true);
      const shot = { id: uid(), name: "", state: s };
      /* 新分段继承当前媒体架（与"继承班底"同一逻辑：班底跟着段走） */
      const snap = rackSnapshot();
      if (snap) shot.rack = snap;
      p.shots.push(shot);
      p.idx = p.shots.length - 1;
      /* 立刻把继承的架况灌进媒体架并认段（旧实现漏这一步，要等 1~2 秒
         轮询，且轮询发现新段无记忆会把参考清空 —— 20261012）。 */
      ui.restoreShotRack(shot);
      ui.load(JSON.stringify(s));
      ui.commit();
      ui.paintSegTools();
      broadcastSeg("seg");
      /* 立刻给一句可见反馈。它同时是一块试金石：点了连这句都没有，
         说明浏览器里跑的还是旧脚本 —— 强刷一次（Ctrl+F5）。 */
      ui.lanesFlash(`已接第 ${p.shots.length} 段 —— 正在登记档案…`);
      /* 登记进档案 —— 时间轴画的是**档案里的段**（film_summary 走 chain 的
         order），只往 p.shots 里塞一个对象，轴上什么都不会出现。 */
      void ui.registerShot(shot);
    } catch (e) {
      console.error("[H3 Studio] 接一段失败：", e);
      ui.lanesFlash("接一段失败：" + (e && e.message ? e.message : e)
        + "（详情在浏览器控制台 F12）");
    }
  }


  /** 在第 i 段后面插一段（接缝「＋」的落点，方案 D）。
      新段克隆左边那段做底（班底/画布/时长照抄），续写默认潜空间 39 帧 ——
      它本来就是"接着左边那段继续演"的产物。add_clip 恒追加在档案末尾，
      插在中段必须把档案顺序与工程对齐一次（syncArchiveOrder），否则轴上
      错位。 */
  async function insertShot(i) {
    if (!requireName()) return;
    try {
      const p = proj();
      stash();
      snapProject(`插入分段前 第 ${i + 1} 段`);
      const base = p.shots[i] || null;
      const s = detachChain(clone(base ? (base.state || {}) : blankState()));
      s.cont = { mode: "latent", frames: 39 };
      const shot = { id: uid(), name: "", state: s };
      const snap = rackSnapshot();
      if (snap) shot.rack = snap;
      p.shots.splice(i + 1, 0, shot);
      p.idx = i + 1;
      ui.restoreShotRack(shot);
      ui.load(JSON.stringify(s));
      ui.commit();
      ui.paintSegTools();
      ui.lanesFlash(`已插入第 ${i + 2} 段（承接第 ${i + 1} 段）—— 正在登记档案…`);
      await ui.registerShot(shot);
      await ui.syncArchiveOrder();
      ui.paintSegTools();
    } catch (e) {
      console.error("[H3 Studio] 插入分段失败：", e);
      ui.lanesFlash("插入分段失败：" + (e && e.message ? e.message : e)
        + "（详情在浏览器控制台 F12）");
    }
  }


  /* 文案台节点（导演台上游的那台）。 */
  function deskOf() {
    const dn = deskNodeOf(ui.node);
    return dn && dn.deskUI ? dn : null;
  }
  /* 文案台把源段段词抄到目标段身份（复制段提前上屏用）。 */
  function copyDeskSeg(srcCid, dstCid) {
    if (!dstCid) return;
    try {
      const dn = deskOf();
      if (dn && typeof dn.deskUI.copySeg === "function") {
        dn.deskUI.copySeg(srcCid ? String(srcCid) : "", String(dstCid));
      }
    } catch (e) { console.warn("[H3 Studio] 文案台复制段词失败(忽略):", e); }
  }
  /* 文案台临时段身份（shot.id）升级为真实 clip id。 */
  function claimDeskSeg(tempId, clipId) {
    if (!clipId) return;
    try {
      const dn = deskOf();
      if (dn && typeof dn.deskUI.claimSeg === "function") {
        dn.deskUI.claimSeg(tempId ? String(tempId) : "", String(clipId));
      }
    } catch (e) { console.warn("[H3 Studio] 文案台段身份升级失败(忽略):", e); }
  }

  /**
   * 完整复制第 i 段到工程末尾（时间轴段块悬浮条的 ⧉ 与右键菜单「复制这一段」。
   *
   * 与 addShot(继承班底) 的区别：那是"新开一段"，提示词故意只留班底定义；
   * 这是"原样克隆一段做同款连拍"——**提示词全文、画布、时长、全部生成参数、
   * 参考图照抄**（菜单/按钮提示词承诺的就是"照抄"，20261012 对齐实现）。
   * 新身份必须 detach：clip/shot uid 全新，否则会顶掉原段档案。
   * 续接档不继承：复制段物理上接在全片末尾，cont 重置为潜空间 39 帧头钉，
   * 尤其不能带上原段的 latent_file（那是别的段的续拍档，带上会认错档）。
   */
  async function duplicateShot(i) {
    if (!requireName()) return;
    try {
      const p = proj();
      const base = p.shots[i] || null;
      if (!base) return;
      stash();
      snapProject(`复制分段前 第 ${i + 1} 段`);
      /* 完整克隆 state：不走 inheritedState()（它会把 prompt 砍成只剩班底） */
      const s = detachChain(clone(base.state || blankState()));
      normalizeNewShotCont(s);
      /* 复制段恒在末尾：只要不是工程唯一段，就按"往后接"潜空间 39 帧头钉 */
      if (p.shots.length > 0) s.cont = { mode: "latent", frames: 39 };
      const shot = { id: uid(), name: "", state: s };
      /* 参考照抄：优先用被复制段自己存的架快照，没有再拍当前架 */
      const snap = base.rack ? clone(base.rack) : rackSnapshot();
      if (snap) shot.rack = snap;
      p.shots.push(shot);
      p.idx = p.shots.length - 1;
      /* 连同段身份一起灌架：复制后参考图必须立刻在架上，且不被随后轮询清掉 */
      ui.restoreShotRack(shot);
      ui.load(JSON.stringify(s));
      ui.commit();
      ui.paintSegTools();
      /* 文案台段词提前抄到临时段身份（shot.id），随下面第一次广播立刻上屏，
         不用等 add_clip 网络往返 —— 消除"慢一拍"。 */
      copyDeskSeg(base.clip, shot.id);
      broadcastSeg("seg");
      ui.lanesFlash(`已完整复制第 ${i + 1} 段到末尾（提示词/参考/画布/时长/参数全照抄）—— 正在登记档案…`);
      /* registerShot 拿到真实 clip id 后会把段词从临时身份迁移到 clip id（claimSeg）。 */
      await ui.registerShot(shot);
      ui.paintSegTools();
    } catch (e) {
      console.error("[H3 Studio] 复制分段失败：", e);
      ui.lanesFlash("复制分段失败：" + (e && e.message ? e.message : e)
        + "（详情在浏览器控制台 F12）");
    }
  }

  /** 把「只在档案里」的段**认领**回工程（20261010）。
      块在轴上就该能就地编辑 —— 认领 = 建一个绑定该 clip id 的工程分段
      （时长 / 名字 / 续接窗口照档案抄），之后双击改名、右键菜单、悬停工具条
      全部就地可用。后端认段优先 clip id，认领段渲染时照常对上同一段档案。 */
  async function claimShot(clipRow) {
    try {
      if (!clipRow || !clipRow.id) return null;
      const p = proj();
      const already = p.shots.findIndex((sh) => sh.clip === clipRow.id);
      if (already >= 0) return p.shots[already];
      stash();
      snapProject(`认领分段前 第 ${clipRow.index} 段`);
      const s = detachChain(blankState());
      s.length = Math.max(5, Math.round(Number(clipRow.frames) || 124));
      const trimN = Math.max(0, Math.round(Number(clipRow.trim_frames) || 0));
      s.cont = trimN ? { mode: "latent", frames: trimN } : { mode: "latent" };
      s.chain = { name: s.chain.name || "", clip: clipRow.id, shot: String(clipRow.shot || "") };
      const autoName = `第 ${clipRow.index} 段`;
      const shot = {
        id: uid(),
        name: clipRow.name && clipRow.name !== autoName ? clipRow.name : "",
        state: s,
        clip: clipRow.id,
      };
      const snap = rackSnapshot();
      if (snap) shot.rack = snap;
      const at = Math.min(Math.max(0, (Number(clipRow.index) || 1) - 1), p.shots.length);
      p.shots.splice(at, 0, shot);
      p.idx = at;
      ui.restoreShotRack(shot);
      ui.load(JSON.stringify(s));
      ui.commit();
      ui.paintSegTools();
      ui.lanesFlash(`已认领第 ${clipRow.index} 段进工程 —— 改名 / 右键 / 续接帧数现在就地可改`);
      /* 认领后档案顺序要跟工程对齐（reorder 会重画轴）—— **等它画完再返回**：
         双击改名的输入框挂在这之后，不然会被这次重绘当场抹掉（"回车没反应"）。 */
      await ui.syncArchiveOrder();
      ui.paintSegTools();
      return shot;
    } catch (e) {
      console.error("[H3 Studio] 认领分段失败：", e);
      ui.lanesFlash("认领分段失败：" + (e && e.message ? e.message : e));
      return null;
    }
  }

  /** 删光后自动补一段**默认初始设置**的新段（20261010 用户定：时间轴永远
      保底一段）。不走 addShot —— 那条路的 seedFromScreen 会把屏幕上刚删掉
      的内容又变成第 1 段；这里要的是一张白纸：默认画布 / 默认时长 / 无参考，
      照常登记上轴。 */
  function restoreDefaultShot(silent) {
    try {
      const p = proj();
      const s = detachChain(blankState());
      normalizeNewShotCont(s);
      const shot = { id: uid(), name: "", state: s };
      p.shots.push(shot);
      p.idx = 0;
      ui.load(JSON.stringify(s));
      ui.commit();
      ui.paintSegTools();
      if (!silent) ui.lanesFlash("已补回一段默认设置的新段 —— 时间轴永远保底一段");
      void ui.registerShot(shot);
    } catch (e) {
      console.error("[H3 Studio] 补回默认段失败：", e);
    }
  }

  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { clone, uid, proj, projSnapshot, histBag, snapProject, prevProject, redoProjectItem, agoText, prevWhen, restoreHistoryEntry, undoProject, redoProject, rackSnapshot, restoreShotRack, broadcastSeg, stash, shotLabel, inheritedState, detachChain, seedFromScreen, normalizeNewShotCont, addShot, insertShot, duplicateShot, claimShot, restoreDefaultShot, copyDeskSeg, claimDeskSeg });
}
