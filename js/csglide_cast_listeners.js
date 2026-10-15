import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { BRIDGE_DEFAULT_FILE, RATIOS, REF_IMAGE_MAX, REF_MIN, REF_STEP, REF_VIDEO_MAX, bridgeModelLabel, clampBridgeAlpha, clampBridgeMode, clampRefFps, clampRefScale, findRatio, headOverlapFrames, nearestFrames, secsOfFrames, fmtSecs, FPS } from "./csglide_cast_core.js";
import { dragKind, el } from "./csglide_ui_kit.js";
import { bzStore } from "./bz_store.js";
import { projectStore } from "./bz_project_store.js";

/* ============================================================
 * 控件监听：模式 / 画布 / 参考参数
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installListeners(ui) {
  /* -------------------------------------------------------- listeners */

  /* pointerdown rather than click: something in the v2 node renderer can
   * swallow the click before it lands, and stopping propagation here keeps
   * the canvas-forwarding handler on root out of the way. */
  const setMode = (m) => {
    if (ui.st.mode === m) return;
    ui.st.mode = m;
    ui.render();
    ui.commit();
  };
  const wireMode = (btn, mode) => {
    btn.addEventListener("pointerdown", (e) => {
      e.preventDefault(); e.stopPropagation();
      setMode(mode);
    });
    btn.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); });
  };
  wireMode(ui.btnFL, "fl2va");
  wireMode(ui.btnRef, "ref2va");

  /* 在轴下方反馈条常驻一句话（与监视器 lanesFlash 同一根 gcast-flash）。 */
  const flash = (msg) => {
    const h = ui.flashBar;
    if (h) {
      h.textContent = msg; h.title = msg; h.classList.add("on");
      try { ui.node.setDirtyCanvas(true, true); } catch { /* 已销毁 */ }
    } else {
      console.warn("[H3 Studio]", msg);
    }
  };

  /* 画布尺寸 / 宽高比是整片统一项：一改就同步到所有分段存档。否则全部渲染
     逐段 switchTo 时，其他段仍加载各自旧尺寸，重渲后尺寸不变（用户报障）。

     但 H3 续拍的承接窗口是潜空间拼接，latent 不能缩放——全片所有段必须同一
     尺寸。已经按旧尺寸跑出成片/续拍档的段，尺寸一改就全部失效：必须从第 1
     段起重跑，否则续段在 pins 组装时报「源片 NxN、本段 MxM」（20261012 用户
     实测，跑到模型加载完才炸）。所以同步的同时，把旧尺寸成片段数点出来，
     当场告诉用户接下来要做什么，而不是等运行报错。 */
  const syncCanvasToAll = () => {
    const p = ui.proj();
    if (!p || !Array.isArray(p.shots)) return;
    const nw = ui.st.width, nh = ui.st.height;

    /* store 快照里哪些段已有成片；快照还没拉到（null）时退化为"登记过 clip
       且尺寸仍旧"也算，宁可多提示一次，不可漏报。 */
    let playableIds = null;
    try {
      const sf = bzStore.film(p.name);
      if (sf && Array.isArray(sf.clips)) {
        playableIds = new Set(
          sf.clips.filter((c) => c && c.playable).map((c) => c.clip || c.id)
        );
      }
    } catch { /* store 不可用不影响同步本身 */ }

    let stale = 0;
    for (const sh of p.shots) {
      if (!sh.state) continue;
      const isOldSize = sh.state.width !== nw || sh.state.height !== nh;
      if (isOldSize && sh.clip) {
        const rendered = !playableIds || playableIds.has(sh.clip);
        if (rendered) stale++;
      }
      sh.state.width = nw;
      sh.state.height = nh;
      sh.state.ratio = ui.st.ratio;
    }
    if (stale > 0) {
      flash(`⚠ 画布已整片改为 ${nw}×${nh}：有 ${stale} 段是按旧尺寸渲染的，`
        + `续拍潜空间不能缩放。请在时间轴上从第 1 段起重跑（每段重抽一次，`
        + `pins 会自动接新版），全部段重跑完之前直接跑后面的段会报尺寸不符。`);
    }
  };

  /* 设置区改完段级规格后的统一收口（20261012 修「改时长/画布，时间轴不实时
     更新、切段还丢」）：
     ① ui.stash() 把当前面板存进当前 shot.state —— 切段还原 / 渲染登记
        （registerShot 带的 frames 就取自 shot.state.length）读的都是它，
        不存这一下，改完切段会丢、首次登记还带旧帧长；stash 内有 cid 时
        还会防抖落进状态库；
     ② 时长类（syncLength）：段已上轴（有 clip id）就把帧长写进档案 ——
        时间轴段块宽度读的是后端档案里的 frames，只改前端 ui.st，轴永远
        停在旧时长。写完后端广播 clip_resized，监视器订阅 store 自动重画；
        还没登记的段（轴上无块）只存前端，首次登记自然带新帧长。 */
  const syncSegSpec = (syncLength) => {
    ui.stash();
    if (!syncLength) return;
    const p = ui.proj();
    /* 段身份以**卡上正在显示的那一段**为准（20261013）：ui.st.chain.clip 是
       load 进面板时与本段画布/时长**同一份真值**一起带来的；而 p.idx 万一与
       ui.st 错位（自愈/竞态灌入、异步切段），按 p.idx 取 clip 就会把卡上改的
       时长写到**别的段** —— 用户点第 2 段改 10 秒、第 2 段不动，正是这类错位
       的表象。chain.clip 优先，p.idx 只做兜底。 */
    const cidCard = (ui.st.chain && ui.st.chain.clip) || "";
    let idx = p.idx;
    let shot = p.idx >= 0 ? p.shots[p.idx] : null;
    if (cidCard && (!shot || shot.clip !== cidCard)) {
      const j = (p.shots || []).findIndex((s) => s && s.clip === cidCard);
      if (j >= 0) { idx = j; shot = p.shots[j]; }
    }
    const cid = cidCard || (shot && shot.clip) || "";
    const frames = Math.round(Number(ui.st.length) || 0);
    if (!cid || frames < 5 || typeof ui.applyFrames !== "function") return;
    console.log("[联动] 设置卡写时长 → 段=" + cid + " 下标=" + idx
      + " 帧数=" + frames + "（卡上段身份=" + (cidCard || "无") + "，p.idx=" + p.idx + "）");
    Promise.resolve(ui.applyFrames(cid, idx, frames)).then((data) => {
      /* 后端会再吸附一次（snap_frames）；若与前端网格有细微出入，以回写的
         data.to 为准再收口一次，保证 shot.state 与档案帧长完全一致。 */
      if (data && typeof data.to === "number") ui.stash();
    }).catch((e) => {
      ui.lanesFlash?.("时长写进档案失败：" + (e && e.message ? e.message : e));
    });
  };

  ui.selRatio.onchange = (v) => {
    if (v === "custom") { ui.st.ratio = "custom"; syncCanvasToAll(); ui.render(); ui.commit(); syncSegSpec(false); return; }
    const fam = RATIOS.find((r) => r.label === v);
    if (!fam) return;
    ui.st.ratio = v;
    /* keep the rung of the ladder they were already on, if it exists */
    const cur = findRatio(ui.st.width, ui.st.height);
    const rung = cur ? cur.sizes.findIndex((z) => z.w === ui.st.width && z.h === ui.st.height) : 0;
    const pick = fam.sizes[Math.min(Math.max(0, rung), fam.sizes.length - 1)];
    ui.st.width = pick.w; ui.st.height = pick.h; syncCanvasToAll(); ui.render(); ui.commit(); syncSegSpec(false);
  };
  ui.selSize.onchange = (v) => {
    if (v === "custom") return;
    const [w, h] = v.split("x").map(Number);
    ui.st.width = w; ui.st.height = h; syncCanvasToAll(); ui.render(); ui.commit(); syncSegSpec(false);
  };
  const snap = (v) => Math.max(32, Math.round(v / 32) * 32);
  const activeRatio = () => ui.st.ratio === "custom" ? null : RATIOS.find((r) => r.label === ui.st.ratio);
  ui.inW.onchange = () => {
    const fam = activeRatio();
    const w = snap(+ui.inW.value || 768);
    ui.st.width = w;
    /* A preset ratio keeps both fields locked together: adjusting the width
     * drives the height so the aspect ratio never drifts. Only "custom"
     * unlinks them so width and height edit independently. */
    if (fam) ui.st.height = snap(w / (fam.w / fam.h));
    syncCanvasToAll(); ui.render(); ui.commit(); syncSegSpec(false);
  };
  ui.inH.onchange = () => {
    const fam = activeRatio();
    const h = snap(+ui.inH.value || 768);
    ui.st.height = h;
    if (fam) ui.st.width = snap(h * (fam.w / fam.h));
    syncCanvasToAll(); ui.render(); ui.commit(); syncSegSpec(false);
  };

  ui.inLen.onchange = () => {
    /* 回显只给整数秒（见 secsOfFrames），而吸附仍按未取整的秒走——若用户只是
       点进框又点出来（值没变，onchange 照样触发），10.13s 会被读成 10s 再吸附到
       另一格帧数，等于「什么都没干却改了片段长度」。故对未编辑过的回显值短路：
       只有真敲过键盘才重算。 */
    if (ui.inLen.dataset.dirty !== "1") { ui.render(); return; }
    delete ui.inLen.dataset.dirty;
    /* 时长输入是**净长**口径（与时间轴块宽一致，20261013）：用户说 14 秒，
       时间轴这段就该 14 秒。换算只吸一次：目标总长 = 秒×24 + 头部承接重叠，
       吸到最近的 17k+5 格 —— 先吸净长再吸总长会漂移半格（两次取整）。
       存进 st.length 的仍是**生成总长**（后端按它采样）。 */
    const overN = headOverlapFrames(ui.monitor, ui.st);
    const raw = String(ui.inLen.value).trim();
    const wantNet = (raw === "") ? 124 : Math.round((+raw) * FPS);
    ui.st.length = Math.min(719, nearestFrames(wantNet + overN));
    ui.render(); ui.commit();
    /* 段已登记则同步帧长进档案，时间轴段块立刻按新时长重画 */
    syncSegSpec(true);
  };
  /* 时长滑块：拖动中**只局部更新**滑块位置 / 秒框 / 帧读出，绝不全量 render ——
     render 会重建尺寸下拉并整行 reflow，高频拖动就表现为整行一直闪。松手
     （change）才 commit 落盘 + 一次完整 render 回显。
     滑块也是**净长**口径（与输入框/时间轴一致）；st.length 存生成总长。 */
  ui.lenRange.oninput = () => {
    /* 滑块报的也是净长意图：总长 = 净长意图 + 承接重叠，吸一次格。
       读出位显示**实际可达**的净长（= 总长 - 重叠），和块宽永远同一个数。 */
    const overN = headOverlapFrames(ui.monitor, ui.st);
    ui.st.length = Math.min(719, nearestFrames(Math.round(+ui.lenRange.value) + overN));
    const net = Math.max(1, ui.st.length - overN);
    ui.inLen.value = secsOfFrames(net);
    ui.lenNote.innerHTML = `<b title="净长 ${net} 帧 · ${fmtSecs(net / FPS)} —— 与时间轴块宽同口径。`
      + `生成总长 ${ui.st.length} 帧（含 ${overN} 帧承接重叠）。">${net} 帧</b>`
      + (overN ? ` · 总 ${ui.st.length} 帧（含 ${overN} 承接）` : "");
  };
  ui.lenRange.onchange = () => { ui.commit(); ui.render(); syncSegSpec(true); };

  const refSnap = (v, cap) => Math.max(REF_MIN, Math.min(cap, Math.round(v / REF_STEP) * REF_STEP));
  ui.chkRef.onchange = () => {
    ui.st.ref_image_size = ui.chkRef.checked ? "match" : refSnap(Math.max(ui.st.width, ui.st.height), REF_IMAGE_MAX);
    ui.render(); ui.commit();
  };
  ui.inRef.onchange = () => { ui.st.ref_image_size = refSnap(+ui.inRef.value || REF_MIN, REF_IMAGE_MAX); ui.render(); ui.commit(); };
  ui.inScale.onchange = () => { ui.st.ref_scale_hd = clampRefScale(ui.inScale.value); ui.render(); ui.commit(); };
  ui.chkVRef.onchange = () => {
    ui.st.ref_video_size = ui.chkVRef.checked ? "match" : refSnap(Math.max(ui.st.width, ui.st.height), REF_VIDEO_MAX);
    ui.render(); ui.commit();
  };
  ui.inVRef.onchange = () => { ui.st.ref_video_size = refSnap(+ui.inVRef.value || REF_MIN, REF_VIDEO_MAX); ui.render(); ui.commit(); };
  ui.fpsRef.onchange = () => { ui.st.ref_video_fps = clampRefFps(+ui.fpsRef.value); ui.render(); ui.commit(); };

  ui.inBridge.onchange = () => { ui.st.bridge_alpha = clampBridgeAlpha(ui.inBridge.value); ui.render(); ui.commit(); };
  ui.selBridgeMode.onchange = () => { ui.st.bridge_mode = clampBridgeMode(ui.selBridgeMode.value); ui.render(); ui.commit(); };
  ui.selBridgeMode.onpointerdown = (e) => e.stopPropagation();
  ui.selBridgeModel.onchange = () => { ui.st.bridge_adapter = ui.selBridgeModel.value || ""; ui.render(); ui.commit(); };
  ui.selBridgeModel.onpointerdown = (e) => e.stopPropagation();

  /* 语义桥模型清单：启动后查一次，填进模型下拉。工程里存的模型这台机器
   * 没有时归回默认（与后端 sanitize_adapter_name 同口径）。查不到（接口
   * 异常）就只留「默认」一项，不打扰用户。 */
  (async () => {
    try {
      const r = await api.fetchApi("/cglide/bridge_status");
      const j = await r.json();
      const adapters = Array.isArray(j.adapters) ? j.adapters : [];
      const list = adapters.length ? adapters : [BRIDGE_DEFAULT_FILE];
      ui.selBridgeModel.replaceChildren();
      for (const name of list) {
        const o = el("option", null, bridgeModelLabel(name));
        o.value = name === BRIDGE_DEFAULT_FILE ? "" : name;
        ui.selBridgeModel.append(o);
      }
      /* 工程里存的模型不在本机清单里 → 归默认，读出位即所见即所得 */
      if (ui.st.bridge_adapter && !list.includes(ui.st.bridge_adapter)) {
        ui.st.bridge_adapter = "";
        ui.commit();
      }
      ui.selBridgeModel.value = ui.st.bridge_adapter || "";
      if (ui.selBridgeModel.selectedIndex < 0) ui.selBridgeModel.value = "";
    } catch (e) { /* 接口异常：下拉保持默认一项 */ }
    ui.render();
  })();

  /* 编辑器已删（见「阶段 0 死代码清理」）：ta 的 input / pointerdown 两个监听
     连同 @ 补全、高亮、撤销栈一起去掉。面板上没有任何可输入提示词的地方 ——
     词只在文案台写。 */
  /* The DOM widget sits over the canvas and eats wheel and drag. Rather than
   * disabling pointer events (which risks reaching a shared container and
   * freezing the whole graph), re-dispatch the event onto the canvas. */
  const graphCanvas = () => app.canvas?.canvas || document.querySelector("canvas#graph-canvas");

  ui.root.addEventListener("wheel", (e) => {
    /* Anything that can scroll itself keeps the wheel -- but only while it
     * actually has somewhere to go. Returning early is not enough on its own:
     * without stopPropagation the event carries on to the canvas handler and
     * zooms the graph instead of scrolling the prompt. At either end of the
     * scroll, hand it on so a short prompt still zooms like the rest of the
     * panel. No preventDefault -- the browser does the scrolling. */
    /* A scrollable area keeps the wheel outright. Handing it on at the ends
     * sounds tidy but means a wheel-down at scrollTop 0 zooms the graph
     * instead of scrolling -- which is the whole complaint. overscroll-behavior
     * in the CSS stops the scroll chaining onward once it does hit an end. */
    if (e.target.closest("textarea, [contenteditable]")) {
      e.stopPropagation();
      return;
    }
    const cv = graphCanvas();
    if (!cv) return;
    e.preventDefault();
    e.stopPropagation();
    cv.dispatchEvent(new WheelEvent("wheel", {
      deltaX: e.deltaX, deltaY: e.deltaY, deltaMode: e.deltaMode,
      clientX: e.clientX, clientY: e.clientY,
      ctrlKey: e.ctrlKey, shiftKey: e.shiftKey,
      bubbles: true, cancelable: true,
    }));
  }, { passive: false });

  /* NO pointerdown forwarding to the canvas. v2 takes pointer CAPTURE on that
   * press and only releases it from its own state machine, which ignores an
   * untrusted event -- so the canvas keeps panning with the mouse until a
   * real press elsewhere clears it. Confirmed in the event log: every stuck
   * drag began with a re-dispatched pointerdown and ended on the next real
   * press with lostpointercapture. Wheel forwarding above is fine, because
   * wheel does not capture. The node still drags by its title bar.
   */

  /* Miss a slot and the file falls through to ComfyUI, which answers by
   * spawning a LoadAudio node on the graph. The node swallows the whole drop
   * instead: while a file is over it, every slot that could take that file is
   * armed so the target is visible, and a release that lands on none of them
   * is routed to the first empty slot of the right kind rather than escaping. */
  ui.dragDepth = 0;

  function armSlots(kind) {
    ui.root.querySelectorAll("[data-kind]").forEach((n) => {
      n.classList.toggle("gcast-armed", kind === "" || n.dataset.kind === kind);
    });
  }
  function disarm() {
    ui.dragDepth = 0;
    ui.root.querySelectorAll(".gcast-armed, .drop").forEach((n) => {
      n.classList.remove("gcast-armed", "drop");
    });
  }

  ui.root.addEventListener("dragenter", (e) => {
    const k = dragKind(e);
    if (k === null) return;
    e.preventDefault(); e.stopPropagation();
    ui.dragDepth++;
    armSlots(k);
  });
  ui.root.addEventListener("dragover", (e) => {
    if (dragKind(e) === null) return;
    e.preventDefault(); e.stopPropagation();      /* keep it off the canvas */
  });
  ui.root.addEventListener("dragleave", (e) => {
    if (dragKind(e) === null) return;
    if (--ui.dragDepth <= 0) disarm();
  });
  window.addEventListener("dragend", disarm);
  window.addEventListener("drop", disarm);

  ui.root.addEventListener("drop", async (e) => {
    const k = dragKind(e);
    if (k === null) return;
    e.preventDefault(); e.stopPropagation();       /* never reaches the graph */
    const onSlot = e.target.closest("[data-kind]");
    disarm();
    if (onSlot) return;                            /* the slot handled it */
    const f = e.dataTransfer?.files?.[0];
    const kind = f && (ui.fileKind(f) || k);
    if (!f || !kind) return;
    /* 参考素材槽位已外迁媒体架：面板空白处不接收文件，拒绝闪烁提示 */
    ui.flashPanel();
  });


  /* ---- 工程级撤销 / 重做快捷键 ----------------------------------------
     Ctrl+Z 撤销、Ctrl+Shift+Z 或 Ctrl+Y 重做。只在本节点被选中、且焦点不在
     文本输入 / 下拉控件时接管 —— 否则会和 ComfyUI 画布自己的 Ctrl+Z（撤销
     图上的操作）冲突。挂 window 捕获阶段，先于画布处理。 */
  function onUndoKey(e) {
    const k = (e.key || "").toLowerCase();
    const isUndo = (e.ctrlKey || e.metaKey) && k === "z" && !e.altKey;
    const isRedo = ((e.ctrlKey || e.metaKey) && k === "y")
      || (isUndo && e.shiftKey);
    if (!isUndo && !isRedo) return;
    const ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA"
      || ae.tagName === "SELECT" || ae.isContentEditable)) return;
    let selected = [];
    try {
      const sn = app.canvas.selectedNodes;
      if (Array.isArray(sn)) selected = sn;
      else if (app.canvas.selected_nodes) {
        /* 新版前端：selected_nodes 是 {id: node} 映射，不是数组。 */
        selected = Object.values(app.canvas.selected_nodes);
      }
    } catch { /* 无画布 */ }
    if (!selected.includes(ui.node)) return;
    e.preventDefault(); e.stopPropagation();
    if (ui.run) { ui.lanesFlash("渲染中不能撤销 —— 等这一轮跑完（或先停止）"); return; }
    if (isRedo) {
      if (!ui.redoProjectItem()) { ui.lanesFlash("没有可重做的步骤"); return; }
      ui.redoProject();
    } else {
      if (!ui.prevProject()) { ui.lanesFlash("暂无还原点"); return; }
      ui.undoProject();
    }
  }
  window.addEventListener("keydown", onUndoKey, true);

  /* ---- 分段 state 落盘（工程库地基）----
     stash 把面板写进 shot.state（内存）；这里再防抖落到磁盘 clip.json 的 state
     字段，拷走文件夹 / 重启 / 在工程库打开，才带得走完整前端参数。 */
  const persistTimers = {};
  function persistClipState(cid, st) {
    if (!cid) return;
    const name = ui.castName ? ui.castName() : "";
    if (!name) return;
    clearTimeout(persistTimers[cid]);
    /* 写穿透统一经 projectStore 防抖调度：连续编辑时 fire 被覆盖成最新 st，
       提交前 flushWrites 立即触发并等它落库。 */
    projectStore.scheduleSave(name, "state:" + cid, () => {
      return api.fetchApi("/cglide/chain/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, id: cid, patch: { state: st } }),
      }).catch(() => null);
    }, 500);
  }

  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { setMode, wireMode, snap, activeRatio, refSnap, graphCanvas, armSlots, disarm, onUndoKey, persistClipState });
}
