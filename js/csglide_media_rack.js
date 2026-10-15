/* ============================================================
 * H3 Media Rack — 🗂️ H3 媒体架 (H3 Media Rack)
 * ------------------------------------------------------------
 * 参考图片/视频/音频的独立素材节点。槽位编号即 @图片N / @视频N /
 * @音频N 的 N，与导演台面板同义且**永久绑定**：图片落在哪个格，
 * 那个编号就永远指它——本节点没有任何自动搬动图片的逻辑。
 *
 * 视觉与排版复用导演台面板同一份样式表（CAST_CSS）与同一套类名
 * （gcast-slot / gcast-media / gcast-thumb / gcast-cap …），
 * 两个节点的槽位因此完全一致，不会各自漂移。
 *
 * 输出 H3_REFS 接导演台的 refs 输入；接线槽位覆盖面板同编号素材，
 * 空槽位由面板兜底。一个媒体架可以喂多个导演台。素材仍走文件管道
 * （/upload/image → input/cglide/），导演台侧的修剪/抽帧/色度探针
 * 全部照常工作。
 *
 * ---- 本文件只做装配。各分区（顺序 = 依赖次序，不能调）----
 *   csglide_rack_core.js    常量 + 无状态工具（上传/探测/格式判定/网格）
 *   csglide_rack_css.js     媒体架专属样式与注入
 *   csglide_rack_alias.js   别名（文件名即角色名）全套
 *   csglide_rack_media.js   波形解码缓存 + 媒体元素复用
 *   csglide_rack_images.js  参考图片格
 *   csglide_rack_trim.js    修剪条（波形 + 双柄 + 播放 + 帧档）
 *   csglide_rack_cards.js   参考视频 / 参考音频卡片区
 *   csglide_rack_sort.js    按提示词排序
 *   csglide_rack_bar.js     面板条（本段指示 / 排序 / 帧率 / 音频号 / 清空）
 *   csglide_rack_drop.js    节点级拖入路由
 *   csglide_rack_seg.js     按分段记忆 + 存档读写 + 跟随导演台轮询
 * ============================================================ */
import { app } from "../../scripts/app.js";
import { el, MIN_W, MIN_H, NODE_ID, uploadFile } from "./csglide_rack_core.js";
import { injectRackCSS } from "./csglide_rack_css.js";
import { installImages } from "./csglide_rack_images.js";
import { installCards } from "./csglide_rack_cards.js";
import { installSort } from "./csglide_rack_sort.js";
import { installBar } from "./csglide_rack_bar.js";
import { installDrop } from "./csglide_rack_drop.js";
import { installSeg } from "./csglide_rack_seg.js";
import { emitRackChange, BZ_BAG_VERSION } from "./bz_rack.js";

function buildUI(node) {
  /* 共享 bag：本文件与所有分区都通过它拿同一份东西。可变状态（ui.st）在两边
     都是同一个对象，不会各存一份。 */
  const ui = { node, app };
  ui.root = el("div", "gcast gmr");
  ui.st = {
    slots: { images: [], videos: [], audios: [] }, fps: 0, audio_out: 0,
    segs: {}, cur: "",
  };
  let dataWidget = null;

  /* 瞬态提示。凡是「本该有反应却没有」的路径都要在这里发声：拖入被拒、
     上传失败、超限。toastEl 懒建（首次提示才插 DOM），3.6s 后自行淡出。 */
  let toastEl = null, toastTimer = 0;
  ui.toast = (msg, bad) => {
    if (!toastEl) { toastEl = el("div", "gmr-toast"); ui.root.append(toastEl); }
    toastEl.textContent = msg;
    toastEl.classList.toggle("bad", !!bad);
    toastEl.classList.add("on");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove("on"), 3600);
  };

  /* 上传失败要把原因说出来。ComfyUI 的 /upload/image 超限时返回 413
     （--max-upload-size，默认 100 MB），此前这里直接 throw 而无人接住，
     界面看上去就是「拖进去没反应」。 */
  ui.uploadOrToast = async (file) => {
    try {
      return await uploadFile(file);
    } catch (e) {
      const m = String((e && e.message) || e);
      const why = m.includes("413")
        ? "超过 ComfyUI 上传上限（--max-upload-size，默认 100 MB）"
        : m;
      ui.toast(`「${file.name}」导入失败：${why}`, true);
      return null;
    }
  };

  ui.clearSlot = (slot) => { for (const k of Object.keys(slot)) delete slot[k]; };

  ui.commit = () => {
    ui.fixAudioOut?.();
    /* 改动**即时**落进当前段的记忆（segs[cur]）。只靠切段那一刻才存
       （switchSeg 里的 ui.st.segs[cur] = snapshot()）会漏掉最后一次改动：
       摆完图直接点「全部渲染」或刷新页面，那一版素材就根本没进 segs，
       切段回来还是旧的 —— 表现为「摆好了、渲染时却没连上」（20261012）。 */
    if (ui.st && ui.st.cur && typeof ui.snapshot === "function") {
      try { ui.st.segs[ui.st.cur] = ui.snapshot(); } catch (_) {}
    }
    if (!dataWidget) dataWidget = node.widgets?.find((w) => w.name === "rack_data");
    if (dataWidget) dataWidget.value = JSON.stringify(ui.st);
    node.setDirtyCanvas(true, true);
    /* 架内容变了即时广播：导演台/文案台监听后立即同步，不再等 0.8~1.5s
       轮询（监听方各自按 raw/节点 id 去重，广播本身零成本，20261012）。 */
    emitRackChange({ node: node.id });
  };

  /* 手工拖拽模式：节点高度由用户（或创建/加载时的一次初始适配）说了算，内容
     超出靠面板内滚动（.gcast overflow:auto），不再随内容变化自动改高。
     报给框架的 minHeight 只给一个固定小值：前端拖拽用 node.computeSize() 做
     下限钳制，而 computeSize() 累加各 widget 的 minHeight——若这里报内容实测
     峰值，节点就永远拖不到比内容更矮的位置（高度收不动的真正根因）。报小值后，
     布局的 distributeSpace（maxHeight 无限）会让 widget 填满节点主体，收短后
     内容自然滚动、不塌陷。 */
  const MIN_FLOOR_H = 120;
  const RACK_HEAD = () => (typeof LiteGraph !== "undefined" && LiteGraph.NODE_TITLE_HEIGHT) || 30;
  /* 仅在创建/加载时跑一次：把节点长到能装下当前内容（grow-only），给用户一个
     合理起点；此后不再自动改高。 */
  ui.fit = () => {
    requestAnimationFrame(() => {
      const contentH = Math.ceil(ui.root.offsetHeight || 0);
      if (!contentH) return;
      if (node.flags?.collapsed) return;
      const needH = RACK_HEAD() + 2 + contentH + 6;
      if (needH > (node.size?.[1] || 0) + 1) {
        node.setSize?.([node.size[0], Math.ceil(needH)]);
      }
    });
  };
  ui.minH = () => MIN_FLOOR_H;

  /* 分区装配。顺序 = 依赖次序：后装的块在运行时会用到先装块挂到 ui 上的东西，
     而每个块是**装完才挂**，所以这个顺序不能调。 */
  installImages(ui);     // → ui.imgBox / ui.assignImage / ui.renderImages / ui.isReordering
  installCards(ui);      // → ui.vidGrid / ui.audGrid / ui.rebuildMedia / ui.freeMediaSlot / ui.appendMedia
  installSort(ui);       // → ui.sortByPrompt（用到 ui.bSort，由 bar 创建）
  installBar(ui);        // → ui.bar / ui.segChip / ui.bSort / ui.fpsIn / ui.audIn / ui.fixAudioOut
  installDrop(ui);       // 节点级拖入（用到上面两块的漏斗）
  installSeg(ui);        // → ui.load / ui.switchSeg / ui.syncSegChip

  ui.root.append(ui.bar, ui.imgBox,
                 el("div", "gcast-label", "参考视频"), ui.vidGrid,
                 el("div", "gcast-label", "参考音频"), ui.audGrid);

  return {
    root: ui.root,
    /* BAG_VERSION 必须在返回对象上：self-heal 读 node.gmr.BAG_VERSION 判断
       旧闭包。原来只在内部 ui（installSeg 挂的）、返回对象没有 → 恒
       undefined、新 bag 也被误判为旧 bag 而热重建（监听器累积/风暴源头）。 */
    BAG_VERSION: ui.BAG_VERSION,
    st: ui.st,
    load: ui.load,
    /* loadSeg / switchSeg 必须暴露：restoreShotRack 靠 node.gmr.loadSeg 判定新
       bag。原来返回对象漏了它们（只在内部 ui 上），导致 node.gmr.loadSeg 恒
       undefined，**每次都误判成"热重载前的旧实例"、走旧兜底 load(force)**，
       段词/架面联动错乱还误报硬刷新警告。 */
    loadSeg: ui.loadSeg,
    switchSeg: ui.switchSeg,
    commit: ui.commit,
    fit: ui.fit,
    minH: ui.minH,
    destroy() { ui.destroySegPoll(); },
  };
}

app.registerExtension({
  name: "cglide.mediarack",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_ID) return;
    injectRackCSS();

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);

      const data = this.widgets?.find((w) => w.name === "rack_data");
      if (data) {
        /* 隐藏 rack_data widget（同导演台 h3_data 的隐藏手法，兼容 v1/v2） */
        data.origType = data.origType || data.type;
        data.hidden = true;
        data.options = Object.assign({}, data.options, { hidden: true });
        data.computeSize = () => [0, -4];
        data.computeLayoutSize = () => ({ minHeight: 0, maxHeight: 0, minWidth: 0 });
        data.label = "";
        data.tooltip = "";
        data.serializeValue = data.serializeValue || (() => data.value);
        if (data.element) {
          Object.assign(data.element.style, {
            display: "none", visibility: "hidden", pointerEvents: "none",
            position: "absolute", left: "-99999px", top: "0",
            width: "0px", height: "0px", opacity: "0", zIndex: "-1",
          });
          data.element.hidden = true;
          data.element.tabIndex = -1;
        }
      }

      const ui = buildUI(this);
      this.gmr = ui;
      const uiWidget = this.addDOMWidget("gmr_ui", "div", ui.root, { serialize: false, hideOnZoom: false });
      /* minHeight 报固定小值：前端拖拽以 node.computeSize()（=各 widget
         minHeight 之和）做下限钳制，报内容实测峰值会让节点永远拖不到比内容
         更矮。报小值 + maxHeight 无限，布局会把 widget 填满节点主体，收短后
         内容靠 .gcast 滚动，不塌陷。 */
      uiWidget.computeLayoutSize = () => ({ minHeight: ui.minH(), maxHeight: void 0, minWidth: 0 });

      requestAnimationFrame(() => {
        const wrap = ui.root.parentElement;
        if (wrap && wrap.classList.contains("dom-widget")) wrap.style.pointerEvents = "none";
      });

      ui.load(data ? data.value : "");
      ui.fit();
      this.setSize?.([MIN_W, MIN_H]);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const r = onConfigure?.apply(this, arguments);
      const data = this.widgets?.find((w) => w.name === "rack_data");
      if (this.gmr && data) {
        this.gmr.load(data.value);
        const nid = this.id;
        /* 读档（reload / loadGraphData）后架上已有素材，但 commit 不触发、
           不会广播 bz:rack-change，导演台 / 文案台首帧读到的是空架，素材条会
           永久空着。这里等本轮同步配置结束后主动广播一次，让所有监听方按同一
           事件统一刷新（事件驱动的单一数据源，20261013）。监听方都按素材
           集合签名去重，多播一次零成本。
           用 setTimeout 而非 requestAnimationFrame：标签页在后台时 rAF 会被
           暂停、事件就发不出去，setTimeout 仍会执行（可能被节流，但不漏）。 */
        setTimeout(() => {
          try { emitRackChange({ node: nid }); } catch (_) {}
        }, 0);
      }
      return r;
    };
  },
});

/* ============================================================
 * 热重建（HMR 在线升级）
 * ------------------------------------------------------------
 * 前端热替换只重跑模块、不重跑 onNodeCreated —— 已存在媒体架节点的 gmr
 * 永远停在热重载前的旧闭包（缺 switchSeg/loadSeg/hydrateRacks，段记忆
 * 不存在）。Electron 套壳里 Ctrl+F5 还可能被缓存拦掉，用户光靠"刷新"
 * 无法保证 bag 换新。自愈巡检（bz_project_sync）检测到旧 gmr 后直接调
 * 本函数：拆掉旧 DOM widget、按当前代码重新 buildUI 并挂回节点，行为
 * 与刷新后新建的节点一致。rack_data 原样保留，素材不丢。
 * 失败回退 false（调用方保留旧 bag + 走整包 load 兜底）。
 * ============================================================ */
export function rebuildMediaRackUI(node) {
  if (!node || !node.widgets) return false;
  /* 已经是新 bag（接口齐全）就不用重建。 */
  /* 新 bag 识别只认 BAG_VERSION + st.segs（返回对象现在都暴露了）；
     旧判定还要求 loadSeg 函数，但返回对象从不暴露它 → 永远重建。 */
  if (node.gmr && node.gmr.BAG_VERSION === BZ_BAG_VERSION
      && node.gmr.st && node.gmr.st.segs) return true;
  try {
    const old = node.gmr;
    /* 旧 gmr 是 buildUI 的返回对象，只暴露 destroy（内部调 destroySegPoll），
       **没有 destroySegPoll**——原来调 old?.destroySegPoll?.() 等于空操作，
       旧 bag 注册的 onCastChange 监听器从不移除，热重建一次就多挂一个，
       最终一次广播触发 N 次 switchSeg（事件风暴/串段/误删根因）。 */
    try { old?.destroy?.(); } catch (_) {}

    /* 摘掉旧 DOM widget：优先用 ComfyUI 配对的 removeDOMWidget（会同时
       移除 .comfyui-dom-widget 包裹），没有再手动摘。 */
    const oldUiWidget = node.widgets.find((w) => w.name === "gmr_ui");
    if (oldUiWidget) {
      try {
        if (typeof node.removeDOMWidget === "function") {
          node.removeDOMWidget(oldUiWidget);
        } else {
          const i = node.widgets.indexOf(oldUiWidget);
          if (i >= 0) node.widgets.splice(i, 1);
          oldUiWidget.element?.remove?.();
        }
      } catch (_) {}
    }
    /* 再清一遍可能残留的包裹容器（双屏版/HMR 偶发 DOM 残留）。 */
    try {
      const roots = node.domEl?.querySelectorAll ? node.domEl.querySelectorAll(".gmr") : [];
      roots.forEach((r) => {
        if (r.parentElement && r.parentElement.classList.contains("comfyui-dom-widget")) {
          r.parentElement.remove();
        }
      });
    } catch (_) {}

    const data = node.widgets.find((w) => w.name === "rack_data");
    const ui = buildUI(node);
    node.gmr = ui;
    const uiWidget = node.addDOMWidget("gmr_ui", "div", ui.root, { serialize: false, hideOnZoom: false });
    uiWidget.computeLayoutSize = () => ({ minHeight: ui.minH(), maxHeight: void 0, minWidth: 0 });

    requestAnimationFrame(() => {
      const wrap = ui.root.parentElement;
      if (wrap && wrap.classList.contains("dom-widget")) wrap.style.pointerEvents = "none";
    });

    ui.load(data ? data.value : "");
    /* 尺寸归用户：热重建类比 onConfigure（节点已存在、用户已拖过大小），
       不 setSize、不 fit —— DOM widget 会填满节点主体，强行按初始尺寸
       重设会把用户拖好的节点大小冲掉。 */
    node.setDirtyCanvas?.(true, true);
    console.log("[H3 Studio] 媒体架节点已在线热重建（旧 bag 升级为当前版本）node=" + node.id);
    return true;
  } catch (e) {
    console.warn("[H3 Studio] 媒体架热重建失败(保留旧界面):", e);
    return false;
  }
}
