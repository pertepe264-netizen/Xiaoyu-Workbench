import { BRIDGE_ALPHA_MAX, BRIDGE_ALPHA_MIN, BRIDGE_ALPHA_STEP, BRIDGE_MODES, FPS, RATIOS, REDO_ANCHORS, REDO_GROUPS, redoDefaultOf, redoGroupOf, REF_FPS_MAX, REF_FPS_MIN, REF_IMAGE_MAX, REF_MIN, REF_SCALE_MAX, REF_SCALE_MIN, REF_SCALE_STEP, REF_STEP, REF_VIDEO_MAX } from "./csglide_cast_core.js";
import { el, makeSelect } from "./csglide_ui_kit.js";

/* ============================================================
 * 设置卡 与 首尾帧卡
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installSettings(ui) {
  /* ---- settings ---- */
  /* 紧凑表单（方案 B）：标签定宽左对齐 + 控件 + 读出右对齐，行间只用细分隔线。
     设置是「参数表」不是内容块，卡片层级让给续写来源这类真正的内容。 */
  const row = el("div", "gcast-row");
  /* 读出位靠 margin-left:auto 顶到行尾，不需要额外 spacer */
  const field = (label, ctl, read) => {
    const f = el("div", "gcast-field");
    f.append(el("span", "gcast-flabel", label), ctl);
    if (read) f.append(read);
    return f;
  };

  /* 分段胶囊（Apple segmented control）：把互斥的 <select> 包成一整条分段
     按钮。select 作为真值隐藏收进容器（view 回显、其他模块读 .value 不受
     影响）；点按钮 → 写 select.value + 派发 change（走原 onchange）。
     tipFor(value) 可给每段单独的 hover 说明。 */
  const segmented = (sel, tipFor) => {
    const wrap = el("div", "gcast-seg");
    sel.style.display = "none";
    wrap.append(sel);
    const btns = [];
    Array.from(sel.options).forEach((o) => {
      const b = el("button", "gcast-seg-btn", o.textContent);
      b.type = "button"; b.dataset.value = o.value;
      if (tipFor) b.title = tipFor(o.value);
      b.onclick = () => {
        if (sel.disabled) return;
        sel.value = b.dataset.value;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        sync();
      };
      wrap.append(b); btns.push(b);
    });
    const sync = () => {
      btns.forEach((b) => b.classList.toggle("is-on", b.dataset.value === sel.value));
      wrap.classList.toggle("is-disabled", !!sel.disabled);
    };
    wrap.sync = sync; sync();
    return wrap;
  };

  const canvasCtl = el("div", "gcast-ctl");
  const selRatio = makeSelect(ui.node, "宽高比");
  selRatio.setOptions(RATIOS.map((r) => ({ label: r.label, value: r.label, note: `${r.w} x ${r.h}` }))
    .concat([{ label: "自定义", value: "custom" }]));
  const selSize = makeSelect(ui.node, "该比例下的画布尺寸");
  const inW = el("input"); inW.type = "number"; inW.step = 32; inW.min = 32;
  const inH = el("input"); inH.type = "number"; inH.step = 32; inH.min = 32;
  const canvasNote = el("div", "gcast-read");
  canvasCtl.append(selRatio.el, selSize.el, inW, el("span", "gcap dim-x", "\u00d7"), inH);

  const lenCtl = el("div", "gcast-ctl");
  /* 时长预设下拉已删除：滑块粗调 + 秒框精确已够用，下拉多余。 */
  /* 自定义输入以「秒」为单位（曾是帧数）：帧数只有落在 17k+5 网格上才合法，
     用户想的却是「我要 12 秒」。写入统一经 nearestFrames 吸附，故回显的是吸附后
     的真实秒数（如 10.13）——精确帧数在右侧读出位给出，不劳用户自己算网格。 */
  const inLen = el("input"); inLen.type = "number";
  /* step="any"：不要让浏览器自己算小数步长。它的网格是 min + k*step，而 min 与
     step 都是四舍五入后的秒（0.21 / 0.7083），回显又取两位小数——三张网格互不
     重合。按一次跳到最近的浏览器格点、回写后又被吸附回同样的帧数，按钮看起来
     毫无反应（141 帧卡在 5.88s，升一次到 158 就再也上不去）。现在浏览器只负责
     ±1 秒，吸附交给 nearestFrames，全程只有一张网格。
     min 只声明「秒数非负」而不是 5 帧下限：只要存在 min，浏览器一旦算出「当前值
     − 1 低于 min」就整个拒绝步进（既不夹也不触发），22 帧（0.92s）按 ↓ 会静默无
     反应。帧网格的下限统一由 nearestFrames 裁决，浏览器不插手。 */
  inLen.step = "any"; inLen.min = 0;
  inLen.title = "自定义时长（秒）。会吸附到 H3 的 17k+5 帧网格，实际帧数见右侧读出。";
  const lenNote = el("div", "gcast-read");
  /* 时长一行：滑块（弹性占宽、停 5+17k 网格）→ 秒数字框；同一真值 st.length。 */
  const lenRange = el("input", "gcast-range");
  lenRange.type = "range"; lenRange.min = "5"; lenRange.max = "719"; lenRange.step = "17";
  lenRange.title = "拖动粗调片段时长（吸附到帧网格）；精确秒数用右侧输入框。";
  lenCtl.append(lenRange, inLen, el("span", "gcap", "秒"));

  /* 按内容长度自动撑宽：CSS 里的 42px 只放得下 4~5 个字符，「1888」「4096」
     一变长就被浏览器静默截断，用户既看不到自己填了什么、也改不回去。
     数字字号 10px 下每字符约 6px，两侧各留 9px 内边距。
     flex:0 1 auto 里的 flex-shrink=1 会在行内空间不足时把宽度压回去（这正是
     上一版设了 style.width 仍被截断的原因），故这里同时钉死 flex-basis 与
     flex-grow/shrink，让浏览器无法回压。 */
  /* 数字框统一固定宽度（不再按字符数动态变宽 —— 那是"框大框小"的来源）：
     10px 字体下 4 位数（1344）约 26px，52px 足够且最整齐；flex 钉死防压缩。 */
  const autoWidth = (inp) => {
    inp.style.flex = "0 0 52px";
    inp.style.width = "52px";
  };

  const refCtl = el("div", "gcast-ctl");
  const chkRef = el("input"); chkRef.type = "checkbox"; chkRef.title = "跟随画布长边";
  const inRef = el("input"); inRef.type = "number"; inRef.step = REF_STEP; inRef.min = REF_MIN; inRef.max = REF_IMAGE_MAX;
  inRef.title = "图片参考的最长边（px）：等比缩放到长边不超过该值；比它小的图片原样编码，不放大。";
  /* 放大重绘（二采/三采）条件的参考档位倍数：属于「参考素材怎么编码」的参数，
     所以跟参考尺寸同处一行。1 = 直接用上面的档位，不额外编码。 */
  const inScale = el("input");
  inScale.type = "number"; inScale.step = REF_SCALE_STEP; inScale.min = REF_SCALE_MIN; inScale.max = REF_SCALE_MAX;
  inScale.title = "放大重绘条件的参考档位倍数：1 = 跟随左侧档位、不额外编码；"
    + "1.5/2 = 参考素材按更大档位重新编码后交给二采与三采，细节更多。"
    + "倍数越高越慢——每个档位都要重跑一遍视觉编码器。";
  const refNote = el("div", "gcast-read");
  /* 「放大倍数」移到高级区：它属于"调过一次就再也不动"的参数，摆在这一行会
     挤掉本行真正常用内容的空间（见下面的 gcast-adv）。 */
  refCtl.append(chkRef, el("span", "gcap", "跟随画布"), inRef);

  const vrefCtl = el("div", "gcast-ctl");
  const chkVRef = el("input"); chkVRef.type = "checkbox"; chkVRef.title = "跟随画布长边";
  const inVRef = el("input"); inVRef.type = "number"; inVRef.step = REF_STEP; inVRef.min = REF_MIN; inVRef.max = REF_VIDEO_MAX;
  inVRef.title = "视频参考的最长边（px）：等比缩放到长边不超过该值；比它小的视频原样编码，不放大。";
  const vrefNote = el("div", "gcast-read");
  const fpsRef = el("input"); fpsRef.type = "number"; fpsRef.step = 1; fpsRef.min = REF_FPS_MIN; fpsRef.max = REF_FPS_MAX;
  fpsRef.title = "参考视频采样帧率：24=不抽帧；往低调（如 12/8）在时长不变时减少参考帧数、节省显存，但会降低参考锚定精度。";
  const fpsNote = el("span", "gcast-read");
  /* 视频参考一行并进「抽帧帧率」：这两项同属视频参考的采样设置，
     原来单独占一行把卡片撑高、又和上行控件重复了「视频参考」的语义。 */
  /* 同上：「抽帧帧率」移入高级区。 */
  vrefCtl.append(chkVRef, el("span", "gcap", "跟随画布"), inVRef);

  /* 绑定必须放在上面所有输入框都声明完之后：const 声明前引用会抛 TDZ
     （Cannot access before initialization），整个面板连带工作流加载一起中止。 */
  for (const inp of [inW, inH, inLen, inRef, inScale, inVRef, fpsRef]) {
    inp.addEventListener("input", () => { autoWidth(inp); inp.dataset.dirty = "1"; });
  }

  const fRef = field("图片参考", refCtl, refNote);
  const fVRef = field("视频参考", vrefCtl, vrefNote);

  /* MiniMax H3 语义桥：模型选择（semantic_bridge/ 目录下的 .safetensors，
   * 由 bridge_status 拉取）+ 作用范围（全量/掩码）+ 数字强度（0=关，0~1 连续）。
   * 空选 = 默认（MiniMaxH3_SemanticBridge_v1）。 */
  const bridgeCtl = el("div", "gcast-ctl");
  const selBridgeModel = el("select", "gcast-btn");
  selBridgeModel.title = "语义桥模型 —— 插件目录 semantic_bridge/ 里的适配器。\n\n"
    + "默认（MiniMaxH3_SemanticBridge_v1）是经过完整训练的通用版；\n"
    + "其余是按题材蒸馏的风格版（武侠、漫画战斗等），同样的强度下风味更集中。\n\n"
    + "换了模型记得试一下强度：风格版通常 0.2~0.3 就够，加太大容易过头。";
  const selBridgeMode = el("select", "gcast-btn");
  selBridgeMode.title = "语义桥作用范围 —— 选「全量」还是「掩码」？\n\n"
    + "全量（整体）：提示词 + 参考画面一起微调，图文协调。\n"
    + "  适合「首尾帧」：只有首尾两张参考，中间全靠提示词脑补，整体一起调，过渡才连贯、故事才讲得通。\n\n"
    + "掩码（仅提示词）：只微调提示词，参考画面原样不动。\n"
    + "  适合「多参考」：给了多张参考图/视频，想让人物长相、动作、场景严格还原时用它，参考一点不碰。\n\n"
    + "拿不准就选「全量」（效果已实测稳定），强度建议 0.2~0.4。";
  BRIDGE_MODES.forEach(([v, t]) => {
    const o = el("option", null, t); o.value = v; selBridgeMode.append(o);
  });
  const inBridge = el("input");
  inBridge.type = "number"; inBridge.step = BRIDGE_ALPHA_STEP;
  inBridge.min = BRIDGE_ALPHA_MIN; inBridge.max = BRIDGE_ALPHA_MAX;
  inBridge.title = "语义桥强度（0 = 关闭）。它像提示词的「翻译器」：把提示词调成模型更懂的说法，画面更贴你的描述。\n\n"
    + "建议数值：\n"
    + "· 0.2 起步 —— 轻微，先试试\n"
    + "· 0.3 ~ 0.4 —— 明显，推荐区间\n"
    + "· 0.6 以上 —— 容易过头、画面风格漂移，慎用\n\n"
    + "模型缺失时后端会告警并跳过本次。";
  bridgeCtl.append(el("span", "gcap", "模型"), selBridgeModel,
                   el("span", "gcap", "范围"), selBridgeMode,
                   el("span", "gcap", "强度"), inBridge);
  /* 模型下拉限宽 + 防折行：设置列只有 ~450px 可用（限宽防挤出列外）；
     新版前端 select 的过长选中项会折行把行撑高，故锁 nowrap + 溢出隐藏。
     高度与字号不在这里覆盖 —— 统一由 .gcast-field 下的规则给 20px / 10px，
     与画布、时长、参考各行一致（原先写死 22px 就是语义桥一行偏高的原因）。 */
  selBridgeModel.style.maxWidth = "150px";
  selBridgeModel.style.boxSizing = "border-box";
  selBridgeModel.style.overflow = "hidden";
  selBridgeModel.style.whiteSpace = "nowrap";
  const fBridge = field("语义桥", bridgeCtl);

  /* ---- 高级区（默认折叠）----
     语义桥 / 放大重绘倍数 / 抽帧帧率都是"调过一次就再也不动"的参数。它们和
     画布、时长等权重地排在一起时，既抢走新手的注意力，也让老手每次都要在五
     行里找画布。折叠起来，明面只留每段都要动的三行。 */
  const advScaleCtl = el("div", "gcast-ctl");
  advScaleCtl.append(el("span", "gcap", "参考档位倍数"), inScale);
  const advScaleNote = el("div", "gcast-read");
  const fAdvScale = field("放大重绘", advScaleCtl, advScaleNote);

  const advFpsCtl = el("div", "gcast-ctl");
  advFpsCtl.append(fpsRef, el("span", "gcap", "fps"), fpsNote);
  const fAdvFps = field("抽帧", advFpsCtl);

  const adv = el("details", "gcast-adv");
  const advSum = el("summary", null, "高级");
  advSum.append(el("span", null, "　语义桥 · 放大重绘 · 抽帧 · 骨架锚点 · 钉帧"));
  /* 别让点标题这一下穿透到画布上变成拖节点 */
  advSum.addEventListener("click", (e) => e.stopPropagation());
  const advBd = el("div", "adv-bd");
  advBd.append(fAdvScale, fAdvFps, fBridge);
  adv.append(advSum, advBd);

  /* 「重抽档位」与「骨架锚点怎么挑」—— 决定重抽这一段时，旧版本里哪些帧
     被钉死。放进「这一段」设置卡：它们本来就是**逐段**的设置 —— 值随 ui.st 走
     （切段跟着换，见 renderInner 里的回显；commit 写进 h3_data，渲染登记时
     一并落进段落档案）。原先挂在渲染行上，值只在建面板时赋了一次，切段后
     下拉还显示着上一段的档位 —— 看着像全局开关，写的却是当前段，两件事被
     一个控件搅在一起；右键菜单里那份又只有当前段能改。收进卡片，一段一行。 */
  const selRedo = el("select", "gcast-btn");
  REDO_GROUPS.forEach(([g]) => {
    const o = el("option", null, g); o.value = g; selRedo.append(o);
  });
  selRedo.value = redoGroupOf(ui.st.redo_mode || "保留动作");
  selRedo.onchange = () => {
    /* 主档切换：落到该组默认后端档（微调→保留动作 / 重来→重画）。
       细分（只换画质 / 完整重拍）在高级抽屉里切换，不在这里悄悄改。 */
    ui.st.redo_mode = redoDefaultOf(selRedo.value);
    /* 档位变了，哪些控件本档生效统一由 paintRedoLink 刷新（见下方定义）。 */
    paintRedoLink();
    ui.commit();
    /* 立刻落进本段的 shot.state：commit 只写 h3_data 控件，shot.state 要等
       stash 才更新 —— 选完档位不 stash，渲染循环 switchTo 到别的段再切回来
       就会把你刚选的档冲掉（20261013「点渲染后重来变微调」）。 */
    if (ui.stash) ui.stash();
    /* 重抽档位变了，接缝组互锁（完整重拍强制独立、接法/帧数禁用）立即刷新。 */
    if (ui.refreshSeamPanel) void ui.refreshSeamPanel();
  };
  selRedo.title = "重抽这一段时的「改动幅度」：\n"
    + "· 微调 = 保留骨架与头尾，只换细节 / 画质（画质细分在高级里选）；\n"
    + "· 重来 = 不保留骨架，整段重新生成（尾部不钉=完整重拍，在高级里选）。\n\n"
    + "除「完整重拍」外，尾部引导头都会钉死 —— 后面各段不用重跑。";
  /* 重抽强度：下拉 → 全宽分段胶囊（选项摊开、新人一眼可点）。selRedo 作为
     真值隐藏在胶囊内，读写/回显口径不变。 */
  const redoSeg = segmented(selRedo, (g) => {
    const row = REDO_GROUPS.find(([x]) => x === g);
    return row ? `${g}：${row[1].join(" / ")}（细分见高级）` : g;
  });

  /* 精细细分（微调组专属）：常规=保留动作 / 更精细=只换画质。 */
  const selRefine = el("select", "gcast-btn");
  [["常规", "保留动作"], ["更精细", "只换画质"]].forEach(([t, v]) => {
    const o = el("option", null, t); o.value = v; selRefine.append(o);
  });
  selRefine.value = ui.st.redo_mode === "只换画质" ? "只换画质" : "保留动作";
  selRefine.onchange = () => { ui.st.redo_mode = selRefine.value; ui.commit(); if (ui.stash) ui.stash(); };
  selRefine.title = "微调档下的骨架密度：常规=隔 22 帧钉一次（约 6 个锚，动作轨迹留住）；"
    + "更精细=隔 7 帧钉一次（钉住约一半，画面几乎不动、只提画质）。";

  /* 尾部细分（重来组专属）：钉住=重画 / 不钉=完整重拍。 */
  const selTail = el("select", "gcast-btn");
  [["钉住", "重画"], ["不钉·完整重拍", "完整重拍"]].forEach(([t, v]) => {
    const o = el("option", null, t); o.value = v; selTail.append(o);
  });
  selTail.value = ui.st.redo_mode === "完整重拍" ? "完整重拍" : "重画";
  selTail.onchange = () => {
    ui.st.redo_mode = selTail.value;
    ui.commit();
    if (ui.stash) ui.stash();
    /* 完整重拍会影响后续各段是否重跑，接缝摘要立即刷新。 */
    if (ui.refreshSeamPanel) void ui.refreshSeamPanel();
  };
  selTail.title = "重来档下的尾部处理：钉住=尾部引导头保留，后面各段不用重跑；"
    + "不钉·完整重拍=整段从零重拍，⚠ 后面各段要跟着重跑。";

  const selAnchor = el("select", "gcast-btn");
  REDO_ANCHORS.forEach(([v]) => {
    const o = el("option", null, v); o.value = v; selAnchor.append(o);
  });
  selAnchor.value = ui.st.redo_anchor || "等间隔";
  selAnchor.onchange = () => { ui.st.redo_anchor = selAnchor.value; ui.commit(); };
  selAnchor.title = "骨架锚点钉在哪些帧上（高级项）：\n"
    + "等间隔 = 每隔 N 帧钉一帧（简单，但可能钉在静止的过渡帧上）；\n"
    + "按运动强度 = 挑画面变化最大的那些帧钉 —— 锚落在关键动作上，"
    + "连贯性更强。\n\n改动幅度决定锚点数量；「完整重拍」没有骨架，本项不生效。";

  /* 钉死头/尾帧数（M3 · 从右键菜单搬入 Inspector 常驻）：默认随「改动幅度」
     自动，新人不用动；只有要手动指定钉多少帧时才改。 */
  const mkPinInp = (key, tip) => {
    const inp = el("input", "gcast-btn");
    inp.type = "number"; inp.min = "0"; inp.max = "4096"; inp.style.width = "66px";
    inp.title = tip;
    inp.onchange = () => {
      ui.st[key] = Math.max(0, Math.min(4096, Math.round(Number(inp.value) || 0)));
      inp.value = String(ui.st[key]);
      ui.commit();
    };
    return inp;
  };
  const inpPinHead = mkPinInp("pin_head",
    "开头钉死几帧（高级）。一般留 0，由改动幅度自动决定。");
  const inpPinTail = mkPinInp("pin_tail",
    "结尾钉死几帧（高级）：钉住它，重抽后后面各段不用重跑。\n"
    + "默认 22；实际会自动抬到下一段的承接窗口。");
  /* 钉头 / 钉尾移到上下文条「重抽 · 高级」弹层（重抽专属，不再放右卡高级区） */

  /* 重抽档位 → 哪些控件在本档真的生效。onchange 与切段回显（view.js）都走
     这一个函数，避免"设了不生效还不知道"：
       · 骨架锚点：只有带骨架的档（保留动作 / 只换画质）生效；「重画」没有
         骨架（stride=0）、「完整重拍」全放开 → 两档都灰；
       · 钉头 / 钉尾：「完整重拍」时 _attach_pin 直接 return、头尾全不钉 → 灰；
         「重画」只钉头尾，这两个反而是主力，不能灰。 */
  function paintRedoLink() {
    const m = ui.st.redo_mode || "保留动作";
    const grp = redoGroupOf(m);
    const isRefine = grp === "微调";
    const isFull = m === "完整重拍";
    selRefine.disabled = !isRefine;              // 精细细分只对微调组
    selTail.disabled = isRefine;                 // 尾部细分只对重来组
    selAnchor.disabled = !isRefine;              // 锚点只有微调组（有骨架）才生效
    inpPinHead.disabled = inpPinTail.disabled = isFull;  // 完整重拍头尾全不钉
  }
  ui.paintRedoLink = paintRedoLink;
  paintRedoLink();

  /* 接缝（链接/断链、承接帧数）统一在时间轴两段交界的「接缝胶囊」里操作，
     右侧不再放重复入口（20261010 用户定）。 */

  /* 「存为新版本」勾选框已删（20261010 用户定）：重抽**默认总是另存**新版本
     （渲染前由 run.js 无条件勾上存档节点的「另存为版本」），旧版本原样保留，
     重抽变成可反悔的尝试，不再有"覆盖了就回不去"。 */

  /* 重抽档胶囊 redoSeg 直接进时间轴上下文条 redoGrp（E1），不再建右卡 field。 */

  /* 骨架锚点移到上下文条「重抽 · 高级」弹层（与钉头钉尾一起，重抽专属） */

  /* 字段分组：规格（画布/时长）→ 参考（图/视频）→ 重抽 → 接缝。
     组间细分隔线 + 留白把"先定规格、再挂参考、然后管重抽、最后定接缝"的
     顺序立起来。接缝（接法 / 承接帧数 / 断链）20261011 从右键与时间轴浮层
     收进本卡，成为唯一入口；内容由 sync.js 的 refreshSeamPanel 填充。 */
  const grpSpec = el("div", "gcast-fgroup");
  grpSpec.append(field("画布", canvasCtl, canvasNote),
                 field("时长", lenCtl, lenNote));
  const grpRef = el("div", "gcast-fgroup");
  grpRef.append(fRef, fVRef);
  /* E1：重抽档 / 接缝移出本卡（改到时间轴上下文条），卡内只留规格、参考 */
  row.append(grpSpec, grpRef);

  /* 设置卡：常用行在明面，高级项折在下面。 */
  const setCard = el("div", "gcast-card");
  const setHd = el("div", "hd");
  /* 卡片标题 = 当前编辑段的身份（手动命名显示名字，默认显示「第 N 段」）。
     以前写死「这一段」，切了段也看不出卡上改的是谁 —— 用户以为点了轴上的
     第 2 段卡就跟着第 2 段走，实际改没改对段完全无从判断（20261013）。
     真值由视图层 renderInner 按 p.idx / st.chain.clip 每次刷新（ui.segTitle）。 */
  const segTitle = el("span", null, "这一段");
  setHd.append(segTitle,
               el("span", "sub", "规格 · 参考"));
  const setBd = el("div", "bd");
  setBd.append(row);
  setCard.append(setHd, setBd, adv);

  /* ============ E1：时间轴「当前段上下文条」========================
     段选择 + 重抽（改动幅度）+ 接缝（接法 / 连播 / 承接帧）+ 主按钮，横贯
     时间轴宽度、单行；从右卡移出以瘦身。DOM 在 monitor.js 挂进 lanesWrap。 */
  const ctxBar = el("div", "gcast-ctxbar");

  /* 段选择器：第 N 段 ▾ → switchTo（时间轴段块也能切，这里是便捷入口） */
  const segSel = el("select", "gcast-ctx-segsel");
  const fillSegSel = () => {
    const pp = ui.proj();
    segSel.innerHTML = "";
    pp.shots.forEach((sh, k) => {
      const o = el("option", null, `第 ${k + 1} 段`);
      o.value = String(k);
      segSel.append(o);
    });
    segSel.value = String(Math.max(0, pp.idx));
  };
  segSel.title = "切换当前要编辑 / 重抽的段";
  segSel.onchange = () => {
    const j = parseInt(segSel.value, 10);
    if (Number.isFinite(j)) ui.switchTo(j);
  };

  /* 重抽组：改动幅度分段胶囊（redoSeg，selRedo 隐藏其中） */
  const redoGrp = el("div", "gcast-ctx-grp");
  redoGrp.append(el("span", "gcast-ctx-lbl", "重抽"), redoSeg);

  /* 重抽高级参数（骨架锚点 / 钉头 / 钉尾）收进「高级 ▾」弹层，明面不占空间 */
  const redoAdv = el("div", "gcast-seam-chipop gcast-redo-adv");
  const redoAdvBtn = el("button", "gcast-ctx-advbtn", "高级 ▾");
  redoAdvBtn.type = "button";
  redoAdvBtn.title = "重抽高级：骨架锚点、钉头、钉尾（一般保持自动即可）";
  const redoAdvMenu = el("div", "gcast-ctx-advmenu gcast-popmenu");
  const advRefineRow = el("div", "gcast-adv-row");
  advRefineRow.append(el("span", "gcast-adv-lbl", "精细"), selRefine);
  const advTailRow = el("div", "gcast-adv-row");
  advTailRow.append(el("span", "gcast-adv-lbl", "尾部"), selTail);
  const advAnchorRow = el("div", "gcast-adv-row");
  advAnchorRow.append(el("span", "gcast-adv-lbl", "骨架锚点"), selAnchor);
  const advPinRow = el("div", "gcast-adv-row");
  advPinRow.append(el("span", "gcast-adv-lbl", "钉头"), inpPinHead,
                   el("span", "gcast-adv-lbl", "钉尾"), inpPinTail);
  redoAdvMenu.append(advRefineRow, advTailRow, advAnchorRow, advPinRow);
  redoAdv.append(redoAdvBtn, redoAdvMenu);
  /* 点按钮的 pointerdown 不再冒泡到画布（防点「高级 ▾」时画布拖拽/选中）。
     弹层开闭由下面 click 的 toggle 负责；点外部关闭走 sync.js 的捕获监听
     （那里用 closest(".gcast-redo-adv") 把本按钮排除，不会误关）。 */
  redoAdvBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
  redoAdvBtn.onclick = (e) => {
    e.stopPropagation();
    document.querySelectorAll(".gcast-popmenu.is-open").forEach((m) => {
      if (m !== redoAdvMenu) m.classList.remove("is-open");
    });
    redoAdvMenu.classList.toggle("is-open");
  };
  redoGrp.append(redoAdv);

  /* 方案A（20261011）：接缝组已移除 —— 接法 / 连播 / 承接帧全部在时间轴接缝
     胶囊右键卡里（ui.openSeamWindow），上下文条不再占这一组。 */

  /* 主按钮：唯一主动作，计划摘要收进 tooltip；钉在上下文条最右 */
  const btnRedoClip = el("button", "gcast-btn gcast-go gcast-ctx-go");
  btnRedoClip.type = "button";
  btnRedoClip.append(el("span", "gcast-go-ic", "⟳"),
                     el("span", "gcast-go-tx", "重抽这一段"));

  /* 方案A：上下文条只留 段选择 + 重抽 + 主按钮（接缝归胶囊卡） */
  ctxBar.append(segSel, el("span", "gcast-ctx-vsep"),
                redoGrp, btnRedoClip);

  /* ---- slot areas ----
     首尾帧做成卡片而不是裸的一行标签：它受指挥条上的模式开关控制，卡片头能
     当场说清"现在是什么模式、这两个槽位生不生效"。非首尾帧模式时卡收起内容
     但保留卡头，用户始终知道有这两个槽位、为什么不生效。 */
  const flWrap = el("div", "gcast-card");
  const flHd = el("div", "hd");
  flHd.append(el("span", null, "首尾关键帧"));
  const flSub = el("span", "sub", "");
  flHd.append(flSub);
  const flBd = el("div", "bd");
  const flGrid = el("div", "gcast-grid fl");
  flBd.append(flGrid);
  flWrap.append(flHd, flBd);

  /* 右栏：设置卡在上、首尾帧卡在下，两卡共用一条定宽；监视器吃满余宽。 */
  const rightCol = el("div", "gcast-col");
  rightCol.append(setCard, flWrap);

  /* 参考图/视频/音频三模块已外迁至「🗂️ H3 媒体架」节点（refs 端口接线）。
     续写来源也已由监视器里的分段轴承担（段与段之间的接续在那里预览与切换），
     所以面板不再有独立的续写来源卡 —— 只保留左监视器 + 右设置卡的双栏布局。 */
  /* 双栏：监视器在左占满余宽，设置卡（.gcast-row 自带定宽）在右，v7 式布局。 */
  const studioRow = el("div", "gcast-studio");


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { row, field, canvasCtl, selRatio, selSize, inW, inH, canvasNote, lenCtl, inLen, lenNote, lenRange, autoWidth, refCtl, chkRef, inRef, inScale, refNote, vrefCtl, chkVRef, inVRef, vrefNote, fpsRef, fpsNote, fRef, fVRef, bridgeCtl, selBridgeModel, selBridgeMode, inBridge, fBridge, adv, advSum, advBd, advScaleNote, selRedo, redoSeg, selRefine, selTail, selAnchor, inpPinHead, inpPinTail, ctxBar, segSel, fillSegSel, btnRedoClip, setCard, setHd, setBd, segTitle, flWrap, flHd, flSub, flBd, flGrid, rightCol, studioRow });
}
