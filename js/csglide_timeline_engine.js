/* ============================================================
 * csglide_timeline_engine.js — 时间轴视图引擎（方案 A 视图中枢 · B 引擎内核）
 * ------------------------------------------------------------
 * 把过去散落在 csglide_film.js 里的「帧↔像素坐标 / 缩放 / 容器尺寸 / 重画」
 * 收口为一个无依赖、可复用的纯引擎：
 *
 *   · 唯一坐标：帧→x 只有 xOf()，帧长→宽只有 wOf()，x→帧只有 frameAtX()；
 *     播放头 / 刻度 / 段块 / 接缝 / 波形 / 缩略图全部共用这一把尺子。
 *   · 唯一缩放：fit() 适配填满、zoomAt() 以鼠标下那一帧为锚点缩放，
 *     缩放是单一参数 pxPerFrame，所有层在下一帧一次重画。
 *   · 唯一 resize：内部一个 ResizeObserver 只认滚动容器 clientWidth——
 *     节点/浮动窗被拖宽才重 fit；拖段接缝只改内部 track 的 scrollWidth，
 *     不触发 refit（精准区分"拖宽容器"与"改段长"）。
 *   · 一个 render 总线：requestRender() 用 rAF 合帧，一帧最多重画一次，
 *     resize/缩放/数据更新再密也只产生一次 layout。
 *
 * 固定「像素/帧」模型（专业剪辑软件口径）：
 *   left = 帧 × pxPerFrame；track 宽 = 总帧 × pxPerFrame；超出可视宽→横滚。
 *
 * 纯 ES module，不依赖 ComfyUI；导演台监视器与续接合成都可实例化使用。
 * ============================================================ */

const MIN_PX_PER_FRAME = 0.02;   // 每帧最小像素（防除零 / 防塌缩）
const MAX_PX_PER_FRAME = 8;      // 每帧最大像素（24fps 下约 192px/秒，足够看细节）

export function createTimelineEngine(cfg = {}) {
  const band = cfg.band;                         // 滚动容器（clientWidth = 可视宽）
  const getTotalFrames = cfg.getTotalFrames || (() => 0);
  const onRender = cfg.onRender || null;         // 几何重画回调（宿主的 layout）
  const isCollapsed = cfg.isCollapsed || null;

  let pxPerFrame = 0;      // 每帧占多少布局像素；0 = 尚未建立尺子
  let fitted = false;      // 是否在"数据+布局就绪"时 fit 过（防早期脏值锁定）
  let lastFitTotal = 0;    // 上次适配时的总帧数（总长变了才自动重 fit）
  let lastBandW = 0;       // 上次容器可视宽（宽变才 refit）
  let ro = null;
  let rafId = 0;
  let queued = false;

  const totalNow = () =>
    Math.max(1, Math.round(Number(getTotalFrames()) || 0));
  const viewportW = () => band.clientWidth || 1;

  /* 重算 pxPerFrame，让整条片刚好填满可视宽。
     数据未就绪（无段 / total<2）或布局未就绪（clientWidth<60）都不许锁定，
     否则早期一次空渲染会把尺子锁成脏值（段块放大成几万像素 / 挤成 2px）。 */
  function fit() {
    const total = totalNow();
    const vw = band.clientWidth;
    if (!total || total < 2 || vw < 60) {
      pxPerFrame = 0;
      fitted = false;
      lastFitTotal = 0;
      return false;
    }
    pxPerFrame = Math.max(MIN_PX_PER_FRAME, vw / total);
    fitted = true;
    lastFitTotal = total;
    return true;
  }
  function ensureScale() { if (!fitted) fit(); }

  const isFitted = () => fitted;
  const getPx = () => pxPerFrame;
  const getLastFitTotal = () => lastFitTotal;

  /* 唯一坐标换算（全时间轴所有层共用，禁止其它地方再手算 帧×像素）。 */
  const xOf = (frame) => (Number(frame) || 0) * pxPerFrame;
  const wOf = (frames) => (Number(frames) || 0) * pxPerFrame;
  const frameAtX = (contentX) =>
    (pxPerFrame > 0 ? (contentX / pxPerFrame) : 0);

  /* render 总线：rAF 合帧，一帧最多重画一次；回调异常隔离，不炸调用方。 */
  function requestRender() {
    if (queued) return;
    queued = true;
    rafId = requestAnimationFrame(() => {
      queued = false;
      if (isCollapsed && isCollapsed()) return;
      try { if (onRender) onRender(); }
      catch (e) { console.warn("[timeline-engine] render 回调异常：", e); }
    });
  }

  /* 容器可视宽变化（拖节点/浮动窗/窗口缩放）→ 重 fit，并锚定重排前左缘帧，
     拖宽拖窄时画面不会横向跳回开头。只认 clientWidth，拖段长不改它。 */
  function refitForWidth(vw) {
    vw = Math.round(Number(vw) || 0);
    if (vw === lastBandW || vw < 60) return false;
    const anchorFrame = pxPerFrame > 0 ? band.scrollLeft / pxPerFrame : 0;
    lastBandW = vw;
    fit();
    if (pxPerFrame > 0) {
      requestAnimationFrame(() => {
        if (band.isConnected) band.scrollLeft = Math.max(0, anchorFrame * pxPerFrame);
      });
    }
    return true;
  }

  const clampPx = (v) =>
    Math.max(MIN_PX_PER_FRAME, Math.min(MAX_PX_PER_FRAME, v));

  /* 以鼠标位置（clientX，视口坐标）下那一帧为锚点缩放。
     缩到"整条片不再超出可视宽"时吸附回 fit；放大时保持锚点帧在鼠标下。 */
  function zoomAt(factor, clientX) {
    ensureScale();
    if (!fitted) { requestRender(); return; }
    const rect = band.getBoundingClientRect();
    const localX = clientX == null
      ? band.clientWidth / 2
      : (clientX - rect.left);
    const anchorFrame = (band.scrollLeft + localX) / pxPerFrame;
    const total = totalNow();
    const vw = band.clientWidth;
    const next = clampPx(pxPerFrame * factor);
    if (next * total <= vw + 0.5) {
      fit();                       // 缩回填满就吸附到 fit
    } else {
      pxPerFrame = next;
      fitted = true;
      lastFitTotal = total;
    }
    requestAnimationFrame(() => {
      if (band.isConnected) {
        band.scrollLeft = Math.max(0, anchorFrame * pxPerFrame - localX);
      }
    });
    requestRender();
  }

  /* 换片/换工程：尺子归零，等新数据就绪后由 ensureScale 重建。 */
  function reset() {
    pxPerFrame = 0;
    fitted = false;
    lastFitTotal = 0;
  }

  /* 唯一 resize 入口：内部 ResizeObserver + window resize。 */
  function handleResize() {
    if (isCollapsed && isCollapsed()) return;
    refitForWidth(band.clientWidth);
    requestRender();
  }
  if (typeof ResizeObserver !== "undefined" && band) {
    ro = new ResizeObserver(handleResize);
    ro.observe(band);
  }
  function onWinResize() { handleResize(); }
  window.addEventListener("resize", onWinResize);
  lastBandW = band ? Math.round(band.clientWidth || 0) : 0;

  function destroy() {
    if (ro) { try { ro.disconnect(); } catch (_) { /* 已断开 */ } ro = null; }
    window.removeEventListener("resize", onWinResize);
    if (rafId) cancelAnimationFrame(rafId);
    queued = false;
  }

  return {
    fit,
    ensureScale,
    isFitted,
    getPx,
    getLastFitTotal,
    viewportW,
    xOf,
    wOf,
    frameAtX,
    zoomAt,
    refitForWidth,
    requestRender,
    reset,
    destroy,
  };
}
