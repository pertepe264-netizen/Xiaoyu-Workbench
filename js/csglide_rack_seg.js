/* ============================================================
 * H3 媒体架 — 按分段记忆素材 与 存档读写
 * ------------------------------------------------------------
 * segs[段身份] 存这一段自己的槽位，cur 是当前正在编辑的那一段。切段时先把
 * 当前槽位存进 segs[旧段]，再把 segs[新段] 灌回 st.slots —— 于是「切到哪
 * 一段，架子上就是哪一段的素材」。段身份来自导演台（三个节点共享的 current），
 * 见 bz_rack.js。
 * ============================================================ */
import { castProject, castShotRack,
         onCastChange, emitRackChange, BZ_BAG_VERSION } from "./bz_rack.js";
import { bzStore } from "./bz_store.js";
import { saveRack } from "./bz_project_sync.js";
import { projectStore } from "./bz_project_store.js";
import { MAX_SLOTS } from "./csglide_rack_core.js";

export function installSeg(ui) {
  /* 前端 bag 版本标记：热重载自愈巡检（bz_project_sync.startSelfHeal）
     靠它识别本节点是不是热重载前的旧闭包。 */
  ui.BAG_VERSION = BZ_BAG_VERSION;

  function snapshot() {
    return JSON.parse(JSON.stringify({
      slots: ui.st.slots, fps: ui.st.fps, audio_out: ui.st.audio_out,
    }));
  }

  /* ---- 段槽位落 SQLite 工程库（20261012 方案 A）----
     媒体架是 rack 的唯一权威写入方：摆图 / 删槽 / 裁剪 / 切段收口后，防抖
     700ms 把该段快照 POST /cglide/store/rack。片名从导演台工程快照取。
     hydrate 灌库后 2s 内的 commit 不回写（防止"刚从库读出又写回"的回声）。 */
  const rackSaveTimers = {};
  let hydrateQuietUntil = 0;
  function scheduleRackSave(cid) {
    if (!cid || Date.now() < hydrateQuietUntil) return;
    const p = castProject(ui.node);
    const name = (p && p.name) || "";
    if (!name) return;
    clearTimeout(rackSaveTimers[cid]);
    /* 写穿透统一经 projectStore 防抖调度：防抖结束取最新段记忆落库，提交前
       flushWrites 会立即触发并等它，杜绝"摆图后立刻提交、后端还是旧架况"。 */
    projectStore.scheduleSave(name, "rack:" + cid, () => {
      /* 以段记忆为准：切段时 segs[cid] 已收口；正在编辑的段 segs[cur] 也被
         commit 实时更新过。空快照不落（没有素材的段不占库行）。 */
      const snap = ui.st.segs[cid];
      return (snap && rackHasFile(snap)) ? saveRack(name, cid, snap) : null;
    }, 700);
  }

  function emptyRack() {
    return { slots: { images: [], videos: [], audios: [] }, fps: 0, audio_out: 0 };
  }

  /* 诊断用：这段素材的**全部**图片（原来只打第一张，第 2、3 段各自多摆的
     那张图因此长期看不见 —— 20261012）。用 + 连起来，空槽显示为空。 */
  const firstImg = (rack) => {
    try {
      const s = (rack && rack.slots) || {};
      const imgs = Array.isArray(s.images) ? s.images : [];
      const names = imgs.map((x) => (x && x.file) ? String(x.file).split(/[\\/]/).pop() : "(空)");
      return names.length ? names.join("+") : "(空)";
    } catch (_) { return "(err)"; }
  };

  /* 这份架况里到底有没有素材（三类槽位任一有 file 就算）。空快照也是个
     对象，只看"有没有这个键"会被空快照骗过 —— 借到空快照时绝不能写进
     segs[key]，否则这一段就被永久固化成"没有素材"，参考图再连不上。 */
  const rackHasFile = (rack) => {
    const s = (rack && rack.slots) || {};
    for (const k of ["images", "videos", "audios"]) {
      const arr = Array.isArray(s[k]) ? s[k] : [];
      if (arr.some((x) => x && x.file)) return true;
    }
    return false;
  };

  /* 两份架况是不是同一套素材（只比文件名：别名、裁剪窗不影响"是不是同一段"
     的判断）。用来识破"cur 指着 A、架面摆着 B"的串台残留。 */
  const sameSlots = (a, b) => {
    const sig = (r) => {
      const s = (r && r.slots) || {};
      return ["images", "videos", "audios"]
        .map((k) => (Array.isArray(s[k]) ? s[k] : []).map((x) => (x && x.file) || "").join(","))
        .join("#");
    };
    return sig(a) === sig(b);
  };

  /** 换到另一段：旧段的素材先存好（不然一去不回），新段的素材灌回来。 */
  function switchSeg(key) {
    if (!key) return false;
    key = String(key);
    if (key === ui.st.cur) {
      /* 人已在本段 ≠ 架面一定对：串台残留时 cur 指着 A、架面却摆着 B 的素材
         （旧代码 renderImages 抛异常后跳过 cur 恢复，就把 B 的素材留给了 A；
         之后每次 commit 还把 B 写进 A 的分段记忆 —— 20261012 存档里段1 的
         记忆混进了段2 的图，铁证）。架面与该段记忆不一致就重灌，不能直接把
         错样 return 留着。 */
      const mem = ui.st.segs[key];
      if (mem && rackHasFile(mem)) {
        if (sameSlots(mem, ui.st)) {
          return false;   // 已在本段、架面一致，去重不重复刷
        }
        /* 校正串台：直接重灌本段记忆。不走下面的"存旧段"——cur 就是 key，
           那一步会把架面上的错样存进本段记忆，越存越脏。 */
        console.warn("[联动] 媒体架 switchSeg 校正串台: 架面与本段记忆不一致，重灌", key);
        ui.st.cur = key;
        try { load(mem, true); }
        finally { ui.st.cur = key; }
        ui.st.segs[key] = snapshot();   // 校正后的架面收口进段记忆
        syncSegChip();
        ui.commit();
        scheduleRackSave(key);          // 串台校正结果落工程库
        emitRackChange({ name: ui.castName ? ui.castName() : "",
                         clip: key, reason: "fixup" });
        return true;
      }
      /* 本段还没有记忆 → 落到下面正常流程（借工程快照 / 空架）。此时 cur===key，
         "存旧段"就是把当前架面记为本段记忆——架面对着呢就是捡回记忆，架面不
         对也只能以它为准，没有更真的事实来源。 */
    }
    const keep = ui.st.segs;
    let s = (key && keep[key]) || null;
    let borrowed = false;
    if (!s) {
      const borrowedRack = castShotRack(ui.node, key);
      /* 只有真有素材的快照才借 —— 工程里那段 rack 可能是"空快照污染"
         时期写下的（rackSnapshot 拿不到 rack_data 时曾返回空对象），借它
         会把这一段永久记成空，之后 restoreShotRack 也补不回来。 */
      if (borrowedRack && rackHasFile(borrowedRack)) { s = borrowedRack; keep[key] = s; borrowed = true; }
    }
    console.log("[联动] 媒体架 switchSeg 切换:", key, "首图=" + firstImg(s), "borrowed=" + borrowed, "旧cur=" + ui.st.cur,
      "segs=" + Object.keys(keep).map((k) => k + "→" + firstImg(keep[k])).join(" "));
    /* 已知段（非借快照）→ 引用切段：每段 slots 物理单一份，不 clone/revive。
       mountRack 内部离开旧段用引用收口、再把 st.slots 指向段记忆数组。 */
    if (s && !borrowed && projectStore.mountRack(ui.node, key)) {
      /* 同步节点设置控件（不走 load，这里补上）。 */
      if (ui.fpsIn) ui.fpsIn.value = ui.st.fps > 0 ? ui.st.fps : "";
      if (ui.audIn) ui.audIn.value = ui.st.audio_out > 0 ? ui.st.audio_out : "";
      if (ui.setAudioBaseline) {
        ui.setAudioBaseline(ui.st.audio_out > 0 ? ui.st.slots.videos[ui.st.audio_out - 1] : null);
      }
      syncSegChip();
      ui.renderImages();
      ui.rebuildMedia();
      ui.fit();
      return true;
    }
    /* 新段 / 借快照：离开旧段 clone 收口，load revive 建立本段独立数组。 */
    if (ui.st.cur) {
      const oldCid = ui.st.cur;
      ui.st.segs[oldCid] = snapshot();
      scheduleRackSave(oldCid);        // 离开旧段：旧段最后一版架况落库
    }
    ui.st.cur = key;
    /* force=true：切段必须强制刷新架面；try/finally 保证 cur 身份恢复。 */
    try { load(s || emptyRack(), true); }
    finally { ui.st.segs = keep; ui.st.cur = key; }
    syncSegChip();
    ui.commit();
    if (borrowed) {
      emitRackChange({ name: ui.castName ? ui.castName() : "",
                       clip: key, reason: "borrow" });
    }
    return true;
  }

  /**
   * 导演台专用：把某段的素材快照**连同段身份一起**灌进来（segs[key]=snap、
   * cur=key）。旧 restoreShotRack 只 gmr.load(snap)，cur 被快照（无 cur 字段）
   * 置空，1~2 秒后轮询发现档案 current 是新段、segs 里又没它，照清空不误 ——
   * 复制/插入段后参考图闪一下就没了。走这里，轮询命中同 key 直接 return。
   */
  function loadSeg(key, snap) {
    if (!key) return false;
    key = String(key);
    console.log("[联动] 媒体架 loadSeg: key=" + key + " 灌入首图=" + firstImg(snap) + " 当前cur=" + ui.st.cur);
    const keep = ui.st.segs;
    if (ui.st.cur && ui.st.cur !== key) {
      /* 离开旧段：引用收口（物理单一），不 clone。 */
      projectStore.captureRack(ui.node);
      scheduleRackSave(ui.st.cur);
    }
    /* 双保险：即便上游塞进带 segs/cur 的整份状态，这里也只取槽位快照，
       全局 segs/cur 绝不进入某一段的 segs[key]，杜绝段快照互相污染。 */
    keep[key] = snap
      ? { slots: (snap.slots && typeof snap.slots === "object") ? snap.slots : { images: [], videos: [], audios: [] },
          fps: snap.fps || 0, audio_out: snap.audio_out || 0 }
      : emptyRack();
    ui.st.cur = key;
    /* load() 内部若整体替换 ui.st.segs / ui.st.cur（段快照带了这两个字段时），
       try/finally 保证下面两行一定执行，cur 绝不会被丢成空字符串。 */
    try { load(keep[key], true); }
    finally { ui.st.segs = keep; ui.st.cur = key; }
    syncSegChip();
    ui.commit();
    scheduleRackSave(key);   // 导演台灌入的段架况（复制/插入段等）同步落库
    emitRackChange({ name: ui.castName ? ui.castName() : "",
                     clip: key, reason: "load_seg" });
    return true;
  }

  /**
   * 工程库启动灌库（hydrate）：把库里各段架况整批灌进 segs，再让架面停在
   * 当前段。灌库期间抑制落盘回声 2s。只灌真有素材的段，空段不覆盖记忆。
   * 返回灌入的段数。旧 bag 没有本函数，由 bz_project_sync 走整包 load 兜底。
   */
  function hydrateRacks(racks, curCid) {
    hydrateQuietUntil = Date.now() + 2000;
    let n = 0;
    Object.keys(racks || {}).forEach((cid) => {
      const rk = racks[cid];
      if (rk && rk.slots && rackHasFile(rk)) {
        ui.st.segs[String(cid)] = {
          slots: JSON.parse(JSON.stringify(rk.slots || { images: [], videos: [], audios: [] })),
          fps: rk.fps || 0, audio_out: rk.audio_out || 0,
        };
        n++;
      }
    });
    curCid = curCid ? String(curCid) : "";
    if (curCid) {
      if (ui.st.cur === curCid) {
        const mem = ui.st.segs[curCid];
        if (mem) {
          try { load(mem, true); } finally { ui.st.cur = curCid; }
          syncSegChip();
          ui.commit();
        }
      } else {
        switchSeg(curCid);   // 正常切段流程（会从刚灌好的 segs 取架况）
      }
    }
    console.log("[状态库] 媒体架 hydrateRacks 灌入 " + n + " 段，当前段=" + (curCid || "空"));
    return n;
  }

  /* 段号与锁定状态来自档案那一行（见 bz_rack.currentClipOf），不是拿导演台
     面板的 idx 拼出来的：锁定后面板 idx 会跟着点走，而三个节点共同的编辑目标
     是档案里的 current。三处各拼一遍就会各说一个段号。 */
  async function syncSegChip() {
    const chip = ui.segChip;
    if (!chip) return;
    const p = castProject(ui.node);
    if (!ui.st.cur || !p) {
      chip.textContent = "全片共用";
      chip.classList.remove("on", "locked");
      chip.title = "还没认段时，素材是全片共用的一套";
      return;
    }
    /* 段号按「媒体架当前编辑段」（ui.st.cur）在 store 快照里反查 —— **不读
       后端 current**：setCurrent 先 await 写档、optimisticCurrent 之后才到，
       切段瞬间读到的 current 还是旧段，chip 就显示旧段号且再无时机刷新
       （20261012「点第 1 段显示第 3 段」的根因）。素材与段号从此同一个真值。 */
    let curRow = null;
    try {
      const sf = bzStore.film(p.name);
      if (sf && Array.isArray(sf.clips)) {
        curRow = sf.clips.find((c) => c && c.id === ui.st.cur) || null;
      }
    } catch (_) { curRow = null; }
    const n = (curRow && curRow.index) || ((p.index ?? 0) + 1);
    const locked = !!(curRow && curRow.locked);
    /* 段号只在导演台那边有；档案里只有 id，显示不出来就说"已按段记忆" */
    chip.textContent = (p.clip === ui.st.cur || p.shot === ui.st.cur)
      ? (locked ? "🔒 " : "") + `第 ${n} 段`
      : (locked ? "🔒 " : "") + "本段";
    chip.classList.add("on");
    chip.classList.toggle("locked", locked);
    chip.title = locked
      ? "素材按分段记忆 · 这一段已锁定，再点别的段，架子上仍是这一段"
      : "素材按分段记忆：切到哪一段，架子上就是哪一段的素材";
  }

  /* ---------------- state io ---------------- */
  /* 内容签名：同一份快照被重复 load（导演台灌一次、轮询又灌一次）时，跳过
     renderImages/rebuildMedia 的全量 DOM 重建 —— 缩略图 <img> 不再重复解码，
     这是"切一次段卡一下"的前端主因之一（20261012）。 */
  let lastLoadSig = "";
  function load(raw, force) {
    let d = {};
    try { d = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {}); } catch (_) { d = {}; }
    const slots = d.slots && typeof d.slots === "object" ? d.slots : {};
    let sig = "";
    try {
      sig = JSON.stringify({ s: slots, f: d.fps, a: d.audio_out,
                             segs: d.segs || null, cur: d.cur || "" });
    } catch (_) { sig = String(Date.now()); }
    if (!force && sig === lastLoadSig) return;
    lastLoadSig = sig;
    /* 槽位数量随存档动态重建。数量与卡片在同一遍里一起重建，makeMedia
     * 闭包引用的 slot 对象与 st.slots 始终同引用（原地回填教训）。
     * 尾部空格不生成格子——旧存档的固定 9/3/3 空位自动收缩。 */
    const revive = (kind) => {
      const saved = Array.isArray(slots[kind]) ? slots[kind] : [];
      while (saved.length && !(saved[saved.length - 1] && saved[saved.length - 1].file)) saved.pop();
      return saved
        .slice(0, MAX_SLOTS)
        .map((s) => (s && typeof s === "object") ? { ...s } : {});
    };
    ui.st.slots.images = revive("images");
    ui.st.slots.videos = revive("videos");
    ui.st.slots.audios = revive("audios");
    /* 节点级设置恢复：帧率 / 音频输出编号（老存档没有这两个字段 = 全默认） */
    const f = Number(d.fps);
    ui.st.fps = f > 0 ? f : 0;
    const a = parseInt(d.audio_out, 10);
    ui.st.audio_out = a > 0 ? Math.min(a, MAX_SLOTS) : 0;
    ui.fpsIn.value = ui.st.fps > 0 ? ui.st.fps : "";
    ui.audIn.value = ui.st.audio_out > 0 ? ui.st.audio_out : "";
    /* 恢复存档后同步音频编号的自愈基准：当前指向的文件 */
    ui.setAudioBaseline(ui.st.audio_out > 0 ? ui.st.slots.videos[ui.st.audio_out - 1] : null);
    /* 分段记忆随存档一起回来（老存档没有这两个字段 = 全片共用一套素材）
       ⚠ 只有 raw 里**真的带了**才覆盖 —— 塞进来的常常是**某一段的素材快照**
       （shots[i].rack / segs[key]，里面只有 slots/fps/audio_out），它没有
       segs/cur 是**应该的**，不是"清空"。无条件写会让 cur 变成 ""，下一次
       切段时 `if (ui.st.cur) segs[旧段] = snapshot()` 整段跳过 —— 上一段刚
       摆好的素材当场蒸发（20261012「另外两段的图存不住」的根因）。 */
    if (d.segs && typeof d.segs === "object" && !Array.isArray(d.segs)) ui.st.segs = d.segs;
    if (typeof d.cur === "string" && d.cur) ui.st.cur = d.cur;
    console.log("[联动] 媒体架 load 恢复: cur=" + ui.st.cur + " slots首图=" + firstImg(ui.st)
      + " segs=" + Object.keys(ui.st.segs).map((k) => k + "→" + firstImg(ui.st.segs[k])).join(" "));
    syncSegChip();
    ui.renderImages();
    ui.rebuildMedia();
    ui.fit();
  }

  /* 跟随导演台（20261012 方案B：零轮询）：
     · 导演台本地切段/新建工程 → bz:cast-seg-change 事件即时响应；
     · 后端档案 current 变化（含其他标签页、工作流执行中的登记）
       → bz_store 的 film 快照事件即时响应。
     两条都是事件驱动，不再有任何 setInterval 轮询。 */
  let lastGen = null;
  let storeName = "";
  let offStore = null;
  function ensureStoreSub() {
    const p = castProject(ui.node);
    const name = (p && p.name) || "";
    if (name === storeName) return;
    if (offStore) { try { offStore(); } catch (_) {} offStore = null; }
    storeName = name;
    if (!name) return;
    offStore = bzStore.subscribe(name, (d) => {
      if (ui.node.flags?.collapsed || !d.film) return;
      /* 后端权威 current：与架上当前段不一致就切（导演台还没认段的空段不切） */
      const id = String(d.film.current || d.film.head || "");
      /* ⚠ 必须是 ui.st.cur：裸 st 在本作用域不存在，ES 模块严格模式下必抛
         ReferenceError。它在 store 事件派发链里抛出会炸掉同事件后续所有订阅者
         （文案台/时间轴），并中断 setCurrent 里 optimisticCurrent 之后的
         cast 事件广播 ——「运行完成后点段全不联动」的根因（20261012）。 */
      if (id && id !== ui.st.cur) switchSeg(id);
    });
  }
  ensureStoreSub();

  /* 事件即时通道：导演台广播切段/新建工程 → 立刻换素材或清空。 */
  const offCast = onCastChange((d) => {
    console.log("[联动] 媒体架收到", d.reason || "?", "name=" + (d.name || ""), "clip=" + (d.clip || ""), "折叠=" + !!ui.node.flags?.collapsed);
    if (ui.node.flags?.collapsed) { console.warn("[联动] 媒体架跳过: 节点折叠"); return; }
    const p = castProject(ui.node);
    if (!p || !p.name || d.name !== p.name) { console.warn("[联动] 媒体架跳过: 名字不匹配 d.name=" + (d.name || "") + " cp.name=" + (p && p.name || "无")); return; }
    if (d.reason === "project") {
      lastGen = d.gen || lastGen || 0;
      load(emptyRack(), true);
      /* 新工程 = 从零开始，分段记忆一并清零。load() 现在只在 raw 真的带了
         segs/cur 时才动它们，所以这里必须显式清 —— 否则会带着上一部片子的
         段记忆进入新工程。 */
      ui.st.segs = {};
      ui.st.cur = "";
      ui.commit();
      ensureStoreSub();
      return;
    }
    ensureStoreSub();
    if (d.clip) switchSeg(String(d.clip));
    else syncSegChip();
  });

  ui.snapshot = snapshot;
  ui.switchSeg = switchSeg;
  ui.loadSeg = loadSeg;
  ui.syncSegChip = syncSegChip;
  ui.load = load;
  ui.hydrateRacks = hydrateRacks;

  /* 包装 commit：媒体架任何改动（摆图/删槽/裁剪/帧率/音频号）都收口在
     commit，原逻辑之后把**当前编辑段**防抖落库。installSeg 晚于 buildUI
     里 commit 的定义，这里包一层安全。 */
  const _origCommit = ui.commit;
  ui.commit = function () {
    const r = _origCommit.apply(this, arguments);
    if (ui.st && ui.st.cur) {
      /* 实时收口：经中央 projectStore 把当前架面写入段记忆 segs[cur]，
         不等切段离开才快照——消除"摆图后、切段前 segs[cur] 偏旧"的窗口。
         （切段时 switchSeg 仍会 clone 成最终权威副本，内容一致。） */
      projectStore.captureRack(ui.node);
      scheduleRackSave(ui.st.cur);
    }
    return r;
  };

  ui.destroySegPoll = () => {
    Object.keys(rackSaveTimers).forEach((k) => clearTimeout(rackSaveTimers[k]));
    if (offStore) { try { offStore(); } catch (_) {} offStore = null; }
    offCast();
  };
}
