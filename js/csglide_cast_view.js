import { app } from "../../scripts/app.js";
import { ACCEPT_IMAGE, FPS, MAX_PIXELS, RATIOS, REDO_ANCHOR_VALUES, redoGroupOf, REF_FPS_MAX, REF_IMAGE_MAX, REF_VIDEO_MAX, alignFrames, findRatio, fmtSecs, headOverlapFrames, renderRefControl, secsOfFrames } from "./csglide_cast_core.js";
import { el } from "./csglide_ui_kit.js";

/* ============================================================
 * 面板重绘
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installView(ui) {
  /* ----------------------------------------------------------- render */

  function render() {
    try { renderInner(); }
    catch (e) { console.error("[H3 Studio] render failed:", e); }
  }

  function renderInner() {
    ui.root.dataset.mode = ui.st.mode;
    ui.btnFL.classList.toggle("is-on", ui.st.mode === "fl2va");
    ui.btnRef.classList.toggle("is-on", ui.st.mode === "ref2va");
    /* 指挥条横向紧张（片名 + 段头 + 时长 + 开关 + 五个文件按钮），这里只留短
       名，全称放 title —— 信息一条不少，但不再把指挥条撑到换行。 */
    ui.need.innerHTML = ui.st.mode === "fl2va"
      ? '<span class="mlab">权重</span><span class="mval">fl2va</span>'
      : '<span class="mlab">权重</span><span class="mval">ref2va</span>';
    ui.need.title = ui.st.mode === "fl2va"
      ? "请加载 minimax_h3_fl2va 开头的模型权重"
      : "请加载 minimax_h3_ref2va 开头的模型权重";
    /* 多参考模式下首/末帧卡片是隐藏的，但槽位里可能还留着上一次的图——后端
       一个字都不读。不点出来，用户就以为它还在约束画面。 */
    if (ui.st.mode !== "fl2va" && (ui.st.slots.first.file || ui.st.slots.last.file)) {
      const warn = el("span", "gcast-warn", "⚠ 首/末帧已填但当前模式不生效");
      warn.style.marginLeft = "8px";
      warn.title = "首帧 / 末帧只在「首/尾帧」模式下参与生成。"
                 + "要让它生效请切到「首/尾帧」模式（并加载 fl2va 权重）。";
      ui.need.append(warn);
    }

    const fam = ui.st.ratio === "custom"
      ? null
      : (RATIOS.find((r) => r.label === ui.st.ratio) || findRatio(ui.st.width, ui.st.height));
    ui.selRatio.value = fam ? fam.label : "custom";
    if (fam) {
      const options = fam.sizes.map((z, i) => ({
        label: `${z.w} x ${z.h}`,
        value: `${z.w}x${z.h}`,
        note: z.stage || (i === 0 ? "native" : (z.w * z.h <= 384 * 640 ? "draft" : "")),
      }));
      /* A linked manual edit keeps the ratio but lands off the preset ladder;
       * surface that size in the dropdown instead of leaving the label blank. */
      const curKey = `${ui.st.width}x${ui.st.height}`;
      if (!options.some((o) => o.value === curKey)) {
        options.push({ label: `${ui.st.width} x ${ui.st.height}`, value: curKey, note: "自定义" });
      }
      ui.selSize.setOptions(options);
      ui.selSize.value = curKey;
      ui.selSize.disabled = false;
    } else {
      ui.selSize.setOptions([{ label: "自定义尺寸", value: "custom" }]);
      ui.selSize.value = "custom";
      ui.selSize.disabled = true;
    }
    ui.inW.value = ui.st.width; ui.inH.value = ui.st.height;
    const mp = (ui.st.width * ui.st.height / 1e6).toFixed(2);
    /* 常驻只留 MP（hover 看实际像素比 —— 比例已由左侧下拉表达，不重复占位），
       以及超出 H3 原生面积的必要警告；这样画布行右侧不会被挤到裁切。 */
    const ratioReal = (ui.st.width / ui.st.height).toFixed(3);
    ui.canvasNote.innerHTML = `<b title="实际像素比 ${ratioReal}:1（比例以左侧下拉为准）">${mp} MP</b>`
      + (ui.st.width * ui.st.height > MAX_PIXELS
         ? ` · <span class="gcast-warn" title="实际像素比 ${ratioReal}:1。H3 的原生画布面积上限为 768×1344 = ${(MAX_PIXELS / 1e6).toFixed(2)} MP（短边 768 的原生画布）。当前 ${mp} MP 已超出，模型会按比例降采样后再生成，输出仍是 ${ui.st.width}×${ui.st.height}，等于先缩小再放大，细节会掉。">超出 H3 原生面积</span>`
         : "");

    /* 设置卡标题 = 当前编辑段的身份：手动命了名显示名字，默认显示「第 N 段」。
       这张卡上的画布 / 时长 / 参考全是**逐段**的，标题不写清楚是谁，切了段
       也无从判断卡上改的是谁（20261013「只跟第一段联动」的误会源头之一）。
       段身份按 p.idx（switchTo 已把 ui.st 换成本段），名字优先用工程里手动
       改过的 sh.name。 */
    if (ui.segTitle) {
      try {
        const pp = ui.proj ? ui.proj() : null;
        const k = pp && Array.isArray(pp.shots) ? pp.idx : -1;
        const sh = k >= 0 ? pp.shots[k] : null;
        const nm = String((sh && sh.name) || "").trim();
        ui.segTitle.textContent = nm || (sh ? `第 ${k + 1} 段` : "这一段");
        ui.segTitle.title = nm
          ? `正在编辑：${nm}（第 ${k + 1} 段）—— 卡上的画布/时长/参考都是这一段的`
          : (sh ? `正在编辑第 ${k + 1} 段 —— 卡上的画布/时长/参考都是这一段的` : "");
      } catch (_) { /* 标题只是提示，算不出来就保留原文 */ }
    }

    /* 时长一行统一**净长**口径（20261013）：输入框的「秒」、滑块、读出位都说
       净长 —— 与时间轴块宽同一个数。st.length 存的仍是**生成总长**（后端按它
       采样，含头部承接重叠），这里只做显示换算：净 = 总 - 承接重叠。
       读出位主数字给净长，总长放括号里备查，两边的数从此对得上。 */
    const frames = alignFrames(ui.st.length);
    const overN = headOverlapFrames(ui.monitor, ui.st);
    const netF = Math.max(1, frames - overN);
    ui.inLen.value = secsOfFrames(netF);            // 整数秒（精确值见下方读出）
    delete ui.inLen.dataset.dirty;                  // 本次回显不算「用户编辑过」
    ui.lenNote.innerHTML = `<b title="净长 ${netF} 帧 · ${fmtSecs(netF / FPS)} —— 与时间轴块宽同口径。`
      + `生成总长 ${frames} 帧（${fmtSecs(frames / FPS)}），多出的 ${overN} 帧是承接上一段`
      + `尾部的重叠，画在上一段那一侧。">${netF} 帧</b>`
      + (overN ? ` · 总 ${frames} 帧（含 ${overN} 承接）` : "");
    if (ui.lenRange) ui.lenRange.value = String(Math.max(5, netF));
    /* 指挥条上的时长。它和右侧读出位说的是同一个数，但作用不同：读出位是"我
       刚改的那个值生效成什么了"，指挥条是"这一段有多长"——切段时眼睛不用离开
       面板顶部。精确帧数只在读出位给，指挥条留秒数，不抢横向空间。 */
    /* 指挥条上的时长：同样报净长（与时间轴/读出位同口径）。 */
    const _sv = fmtSecs(netF / FPS).replace(/s$/, "");
    ui.cmdTime.innerHTML = `<span class="mval">${_sv}</span><span class="munit">s</span>`
      + `<span class="mdot">·</span><span class="mval">${netF}</span><span class="munit">f</span>`;
    ui.cmdTime.title = `净长 ${netF} 帧（${fmtSecs(netF / FPS)}）· 生成总长 ${frames} 帧（含 ${overN} 帧承接重叠）`;

    const canvasLong = Math.max(ui.st.width, ui.st.height);
    renderRefControl(ui.chkRef, ui.inRef, ui.refNote, ui.st.ref_image_size, canvasLong, REF_IMAGE_MAX);
    ui.inScale.value = ui.st.ref_scale_hd;
    /* 倍数 >1 时把「重编码后的有效长边」显示出来——这才是二采三采真正拿到的
       参考清晰度，只看左侧档位会以为没变。与后端 _ref_target_long 同口径
       （match → 画布长边 × 倍数）。
       写在倍数自己的读出位（高级区），不再追加到上面「图片参考」那行：控件
       和它的结果必须挨在一起，追加到常用行是"值在这儿、原因在那儿"。 */
    if (ui.st.ref_scale_hd > 1) {
      const base = ui.st.ref_image_size === "match" ? canvasLong : +ui.st.ref_image_size;
      ui.advScaleNote.innerHTML = `有效长边 <b>${Math.round(base * ui.st.ref_scale_hd)}px</b>`;
    } else {
      ui.advScaleNote.innerHTML = `<b>不额外编码</b>`;
    }
    renderRefControl(ui.chkVRef, ui.inVRef, ui.vrefNote, ui.st.ref_video_size, canvasLong, REF_VIDEO_MAX);
    ui.fpsRef.value = ui.st.ref_video_fps;
    /* 数值已显示在输入框里，读出位不再回显"21 fps"这类重复——只保留状态提示；
       单位语义由行首的"抽帧帧率"四字承担。 */
    ui.fpsNote.innerHTML = ui.st.ref_video_fps >= REF_FPS_MAX
      ? `<b>不抽帧</b>`
      : `<b>省显存</b>`;

    /* 宽度按本轮回显的实际字数统一刷新：input 事件只在用户敲键时触发，
       预设下拉改长度、吸附回显、加载工程都会绕过它，窄框下长值就又被截断。 */
    for (const inp of [ui.inW, ui.inH, ui.inLen, ui.inRef, ui.inScale, ui.inVRef, ui.fpsRef]) ui.autoWidth(inp);

    ui.inBridge.value = ui.st.bridge_alpha;
    ui.selBridgeMode.value = ui.st.bridge_mode;
    ui.selBridgeModel.value = ui.st.bridge_adapter || "";
    if (ui.selBridgeModel.selectedIndex < 0) ui.selBridgeModel.value = "";

    /* 重抽档位 / 锚点：随段回显。它们写的就是「这一段」的 state —— 切了段
       还显示上一段的档位，用户照着渲染，跑的却是另一个档（渲染行上那份
       不回显的旧坑，搬进设置卡时一起修掉）。 */
    ui.selRedo.value = redoGroupOf(String(ui.st.redo_mode || "保留动作"));
    /* selRedo 已被分段胶囊包裹：设完真值同步按钮高亮（直接设 .value 不会触发
       change，胶囊不会自己更新）。 */
    if (ui.redoSeg) ui.redoSeg.sync();
    /* E1：上下文条段选择器随当前段 / 段数刷新 */
    if (ui.fillSegSel) ui.fillSegSel();
    /* 细分开关回显：精细(微调组) / 尾部(重来组) 都由 redo_mode 推导。 */
    if (ui.selRefine) ui.selRefine.value = ui.st.redo_mode === "只换画质" ? "只换画质" : "保留动作";
    if (ui.selTail) ui.selTail.value = ui.st.redo_mode === "完整重拍" ? "完整重拍" : "重画";
    ui.selAnchor.value = REDO_ANCHOR_VALUES.indexOf(String(ui.st.redo_anchor)) >= 0
      ? ui.st.redo_anchor : "等间隔";
    /* 档位 → 锚点 / 钉头钉尾哪些生效，统一走 paintRedoLink（settings.js 定义），
       回显与手动切换口径一致，不再只处理锚点。 */
    if (ui.paintRedoLink) ui.paintRedoLink();

    /* 钉帧头/尾：随段回显（M3 搬入 Inspector 常驻）。 */
    if (ui.inpPinHead)
      ui.inpPinHead.value = String(Math.round(Number(ui.st.pin_head) || 0));
    if (ui.inpPinTail)
      ui.inpPinTail.value = String(Math.round(Number(ui.st.pin_tail) || 0));
    /* 接缝状态不在此卡回显：链接/断链、承接帧数统一在时间轴接缝胶囊处查看与修改。 */

    const isFL = ui.st.mode === "fl2va";
    const pw = ui.promptWired();
    /* 面板上只有这一行文案来源标记。接了就报段号，没接就直说该怎么接 ——
       不再提供第二处编辑入口，也就不必再为「两边改了哪个生效」兜底。 */
    if (pw) {
      ui.pSrcLine.textContent = "文案来自文案台 · 第 " + (ui.proj().idx + 1) + " 段";
      ui.pSrcLine.classList.remove("gcast-warnline");
    } else {
      /* 分两种情形说：画布上已经有文案台、只是没接线，和压根没有这个节点。
         一句泛泛的"未接文案台"对前者毫无帮助——用户看着画布上明明有那个节点，
         却被告知"未接"，只能自己猜是线没连还是节点不对。 */
      const g = (typeof app !== "undefined" && app && app.graph)
        ? (app.graph._nodes || app.graph.nodes || []) : [];
      const hasDesk = g.some((n) => n && n.type === "CSGlideDeskCS");
      ui.pSrcLine.textContent = hasDesk
        ? "画布上已有「✍️ H3 文案台」，把它的「提示词」输出接到本节点的"
          + "「外接提示词」输入口 —— 词只在文案台写"
        : "未接文案台 —— 拖一个「✍️ H3 文案台」出来，把它的「提示词」输出接到"
          + "本节点的「外接提示词」输入口（词只在文案台写）";
      ui.pSrcLine.classList.add("gcast-warnline");
    }
    /* 外接提示词不需要"镜像"这一步了：面板上没有编辑器，effPrompt() 在接线时
       直接读上游文本（见 effPrompt），读不到才落回 ui.st.prompt。原先那套"把外部
       文本写进隐藏编辑器、再由编辑器反哺 ui.st.prompt"的回路，随着编辑区一起删
       掉了 —— 少一层中间态就少一处可能不一致的地方。 */
    ui.root.style.minHeight = "";
    /* 非首尾帧模式时不整卡消失 —— 卡头留着，当场说清"有这两个槽位、但现在
       不生效"。整卡隐藏会让用户以为槽位根本不存在。 */
    ui.flSub.textContent = isFL ? "@first / @last 参与生成" : "多参考模式下不参与";
    ui.flBd.style.display = isFL ? "" : "none";
    ui.fRef.style.opacity = isFL ? "0.35" : "1";
    ui.fRef.style.pointerEvents = isFL ? "none" : "";
    ui.fVRef.style.opacity = isFL ? "0.35" : "1";
    ui.fVRef.style.pointerEvents = isFL ? "none" : "";

    ui.flGrid.replaceChildren();
    if (isFL) {
      [["first", "首帧", "@first"], ["last", "末帧", "@last"]].forEach(([key, cap, tok]) => {
        const slot = ui.st.slots[key];
        const card = ui.imageSlot(slot, cap, tok);
        ui.wireDrop(card, slot, "image", ACCEPT_IMAGE, tok);
        ui.flGrid.append(card);
      });
    }

    /* 续写来源卡已撤 —— 续写由时间轴上的分段承担。但 st.cont 的数据与运行时
       续写逻辑照旧保留：这里仍要把刚拖入的续写片吸附到尾部窗口，渲染时
       runShot 照常按 ui.st.cont 续写上一段。只去掉面板上的那张卡，不动数据。

       默认窗口是 39 帧（出厂默认），但**用户逐段选过的不许被覆盖** —— 所以先看
       c.frames：选过就按选的来（片段装不下就退到装得下的最大一档），没选过
       才落回 39。少了这一步，段级菜单里选的 56/73 帧会在下次渲染被悄悄改回
       39，选了没反应。 */
    const c = ui.st.cont;
    if (c.mode !== "latent" && c.file && c.dur
        && (c.start || 0) <= 0.001 && (c.end || 0) >= c.dur - 0.001) {
      const spans = ui.gridSpans(c.dur);
      const want = Number(c.frames) || 39;
      const pick = spans.filter((r) => r.g <= want).pop()
        || spans.find((r) => r.g === 39) || spans[spans.length - 1];
      if (pick) {
        c.end = c.dur;
        c.start = Math.max(0, c.dur - pick.sp);
        c.frames = pick.g;            /* 实际落点回填，菜单回显才对得上 */
      }
    }

    ui.paintSegTools();
    /* 接缝组随选中段刷新（异步拉 /pins_state + transitions）。 */
    if (ui.refreshSeamPanel) void ui.refreshSeamPanel();
  }


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { render, renderInner });
}
