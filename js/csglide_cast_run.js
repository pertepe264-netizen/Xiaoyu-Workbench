import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { el } from "./csglide_ui_kit.js";
import { setCurrent } from "./bz_rack.js";
import { projectStore } from "./bz_project_store.js";
import { redoGroupOf } from "./csglide_cast_core.js";

/* ============================================================
 * 渲染全部
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installRun(ui) {
  /* ======================================================== Render all
   * Queue every clip in the project in order. In "continue" mode each render
   * is adopted into the next clip's CONTINUE FROM slot, so the graph's own
   * Glide Join stitches as it goes and the LAST render is the finished piece
   * -- there is no separate assembly pass to build.
   *
   * The loop drives the panel rather than the graph: it switches clips, fills
   * the slot, commits, queues, waits. Everything it does is something you
   * could do by hand, which is what makes it safe to interrupt.
   */
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const VIDEO_OUT_RE = /\.(mkv|mp4|m4v|mov|webm|avi|mpg|mpeg|ts|m2ts|wmv|flv|ogv)$/i;
  /* run / runBox 已上移到 buildUI 靠前的位置（见渲染控制行旁）：paintSegTools()
     在 3733 行就会被 render() 调到，而它经 paintRenderRow() 读 ui.run —— 声明留在
     这一行会撞 TDZ（Cannot access 'run' before initialization），整个面板在
     加载时直接抛错。 */
  async function queueOnce() {
    const p = await app.graphToPrompt();
    /* 提交前写穿透兜底：等三节点所有在途落库（防抖+网络）完成，保证提交
       那一刻前端缓存 = 后端，杜绝"改完立刻提交、后端是旧值"。渲染是分钟级，
       这里多等几百毫秒无感。 */
    await projectStore.flushWrites(ui.castName ? ui.castName() : "");
    /* 提交前权威对齐：把三个节点的段数据按当前段身份覆盖到提交对象，
       杜绝"渲染三段同图/同词"（不依赖 widget 时序）。 */
    const clipNow = (ui.st && ui.st.chain && ui.st.chain.clip) || "";
    const touched = projectStore.applyToPrompt(p, ui.node, clipNow);
    if (touched.length) {
      console.log("[中央] 提交已对齐到段 " + clipNow + "：" + touched.join("/")
        + " 画布=" + ui.st.width + "x" + ui.st.height);
    }
    const res = await api.queuePrompt(0, p);
    const id = res && (res.prompt_id || res.promptId);
    if (!id) throw new Error("服务端未返回任务 ID");
    return id;
  }

  /* Polling rather than socket events: it survives a dropped websocket, a tab
     that was backgrounded, and every frontend version. A render is a minute or
     more, so a second between checks costs nothing. */
  async function waitForPrompt(id) {
    for (;;) {
      if (ui.run && ui.run.cancel) throw new Error("cancelled");
      await sleep(1200);
      let entry = null;
      try {
        const r = await api.fetchApi(`/history/${id}`);
        if (r.status === 200) {
          const h = await r.json();
          entry = h && h[id];
        }
      } catch (e) { /* server busy or restarting: try again */ }
      if (!entry) continue;
      const s = entry.status || {};
      if (s.status_str === "error") {
        /* ComfyUI 原生「中断当前」会把该 prompt 记为 error、messages 含
           execution_interrupted —— 这算手动停止（不是真失败），让整个循环停下，
           不再提交下一段。 */
        const interrupted = JSON.stringify(entry).includes("execution_interrupted");
        throw new Error(interrupted ? "cancelled" : "渲染失败，请查看控制台");
      }
      if (s.completed || s.status_str === "success") return entry;
    }
  }

  /* Glide Video reports ONE entry per render: the browser-playable proxy, as
     type "temp", with the real file named in a "master" field beside it. So the
     master never appears as a filename anywhere in the history, and picking
     "the newest video" can only ever find the proxy. Read master when it is
     offered; fall back to filename for anything else that writes video. */
  const PREVIEW_RE = /(^|[._-])preview\.[^.]+$/i;

  function videoFromHistory(entry) {
    const outs = (entry && entry.outputs) || {};
    let best = null;
    let proxy = null;
    for (const nodeId of Object.keys(outs)) {
      const bag = outs[nodeId] || {};
      for (const key of Object.keys(bag)) {
        const arr = bag[key];
        if (!Array.isArray(arr)) continue;
        for (const item of arr) {
          if (!item) continue;
          /* The master sits in output/ even when the proxy it came with is a
             temp file, so the type of the entry does not describe it. */
          if (typeof item.master === "string" && VIDEO_OUT_RE.test(item.master)) {
            best = { name: item.master, subfolder: item.subfolder || "", type: "output" };
            continue;
          }
          if (typeof item.filename !== "string") continue;
          if (!VIDEO_OUT_RE.test(item.filename)) continue;
          const hit = {
            name: item.filename,
            subfolder: item.subfolder || "",
            type: item.type || "output",
          };
          if (PREVIEW_RE.test(item.filename)) proxy = hit; else best = hit;
        }
      }
    }
    if (!best && proxy) {
      /* Only a proxy was produced. Better than stopping, but say so - the
         guide will be subsampled and the join will pay for it. */
      console.warn("[H3 Studio] Render all: only a preview proxy was found for "
                 + "this render, chaining from it");
      return proxy;
    }
    return best;
  }

  /* 渲染浮层（.gcast-run）脱离画布定位、贴住双栏视觉矩形。
     v2 前端把节点 DOM 整体放在 .dom-widget 容器里：该容器 position:fixed
     且带 transform:matrix(scale)（画布缩放），宽度还由框架逐帧同步、缩放时
     会在并排/窄档间重排。浮层若挂在双栏内用 absolute/inset:0，就处在这个
     transform 定位上下文里 —— 缩放/平移时遮罩与卡片跟着变换盒错位、卡片
     也被一起放大。改挂 document.body 后脱离画布变换，再以 rAF 读双栏
     getBoundingClientRect() 的**视觉**坐标（视口 px，fixed 直接可用、无需
     除 scale，与接缝胶囊卡/段工具条同构方案），遮罩始终精确盖双栏可视区、
     卡片保持自然字号，缩放/平移/节点移动/面板重排都实时跟随。 */
  /* 返回：0 = 双栏已不在 DOM（应摘除浮层）；1 = 几何无变化；2 = 本次有重定位。
     只写发生变化的样式，避免每帧强制重排。 */
  function placeRunBox() {
    const box = ui.runBox;
    const sr = ui.studioRow;
    if (!box || !sr || !sr.isConnected) return 0;
    const r = sr.getBoundingClientRect();
    const L = Math.round(r.left), T = Math.round(r.top),
          W = Math.max(1, Math.round(r.width)), H = Math.max(1, Math.round(r.height));
    const st = box.style;
    let changed = 1;
    const set = (k, v) => { if (st[k] !== v) { st[k] = v; changed = 2; } };
    set("left", L + "px");
    set("top", T + "px");
    set("width", W + "px");
    set("height", H + "px");
    return changed;
  }

  function paintRun() {
    if (!ui.run) {
      if (ui.runBox) {
        try { ui.runBox._stopFollow?.(); } catch { /* 无跟随循环 */ }
        ui.runBox.remove(); ui.runBox = null;
      }
      ui.root.classList.remove("running");
      ui.paintRenderRow();          /* 渲染结束，「全部渲染」重新可用 */
      ui.paintRunScope();           /* 轴上"正在跑"那段的脉冲随之撤掉 */
      ui.updateLanesMeta();         /* 轴头计数从 3 / 8 回到 8 段 */
      return;
    }
    if (!ui.runBox) {
      ui.runBox = el("div", "gcast-run");
      const card = el("div", "card");
      card.append(el("div", "ttl", "正在渲染工程"));
      card.append(el("div", "msg"));
      card.append(el("div", "sub"));
      const cancel = el("button", "gcast-btn ghost", "停止全部");
      cancel.title = "立即中断当前正在渲染的分段，并取消后面所有分段；已完成的分段会保留。";
      cancel.onclick = async (e) => {
        e.stopPropagation();
        if (!ui.run) return;
        ui.run.cancel = true;
        /* 立即中断当前正在后端执行的 prompt：光设 cancel 只让前端不再等，
           当前任务在后端仍会跑完，用户会看到"点了停止它还在跑"。 */
        try { await api.interrupt(); } catch { /* 已结束/无任务，忽略 */ }
        /* 清空队列里残留的 pending（兜底：防并发或其他来源已排队的任务）。 */
        try {
          await api.fetchApi("/queue", { method: "DELETE",
            body: JSON.stringify({ delete: [], clear: true }) });
        } catch { /* 旧版无该路由，忽略 */ }
        paintRun();
      };
      card.append(cancel);
      ui.runBox.append(card);
      /* 挂在 document.body（脱离画布 transform 缩放盒），不是双栏内：
         遮罩仍只盖双栏可视区（监视器+设置，跑的时候不该改参数），不盖总览轴
         —— 轴上画着"哪几段在跑、跑到第几段"，盖住它就只剩一个转圈对话框。
         覆盖范围由 rAF 按双栏视觉矩形实时给出（见 placeRunBox）。 */
      document.body.appendChild(ui.runBox);
      placeRunBox();              /* 挂载即定位，避免首帧在左上角闪一下 */
      let raf = 0, tick = 0, idleFrames = 0;
      const stopLoop = () => { if (raf) { cancelAnimationFrame(raf); raf = 0; } };
      const loop = () => {
        raf = requestAnimationFrame(loop);
        tick++;
        /* 自适应帧率：缩放/平移/面板重排进行中（几何在变）每帧跟随，零滞后；
           几何连续约 0.5s 无变化（正常渲染静置）降到每 3 帧检测一次，省空转。 */
        if (idleFrames > 30 && (tick % 3 !== 0)) return;
        const moved = placeRunBox();
        if (moved === 0) {
          /* 双栏已不在 DOM（节点被删除/重建）：浮层绝不能残留在 body 上。 */
          stopLoop();
          if (ui.runBox) { try { ui.runBox.remove(); } catch { /* 无 */ } ui.runBox = null; }
          try { ui.root?.classList.remove("running"); } catch { /* 无 */ }
          return;
        }
        idleFrames = moved === 2 ? 0 : idleFrames + 1;
      };
      raf = requestAnimationFrame(loop);
      ui.runBox._stopFollow = stopLoop;
      ui.root.classList.add("running");
    }
    const p = ui.proj();
    const name = p.shots[ui.run.i] ? ui.shotLabel(p.shots[ui.run.i], ui.run.i) : `分段 ${ui.run.i + 1}`;
    ui.runBox.querySelector(".msg").textContent =
      ui.run.cancel ? `正在收尾 ${name}…` : `${name} —— ${ui.run.k + 1} / ${ui.run.total}`;
    ui.runBox.querySelector(".sub").textContent =
      ui.run.note || (ui.run.mode === "continue" ? "经「续写来源」串联" : "各分段独立渲染")
      + (ui.run.skipped ? `，已跳过 ${ui.run.skipped} 段` : "");
    /* 进度同时落在轴上：正在跑的那一段脉冲，轴头计数变成 3 / 8。
       遮罩层盖在面板正中会把轴挡住，而"跑到第几段"恰恰是要盯着轴看的事。 */
    ui.paintRunScope();
    ui.updateLanesMeta();
  }

  async function renderAll(mode, scope) {
    if (ui.run) return;
    const p = ui.proj();
    if (!p.shots.length) {
      ui.lanesFlash("本工程还没有任何分段 —— 先在总览轴上点「＋ 接一段」");
      return;
    }
    scope = scope || "all";

    /* 工程/档案冲突检测：拖时间轴改过的段，面板预设可能还是旧的，渲染会把它
       静默覆盖掉。先摆出来让用户选哪边算数，再往下排队。用户取消就不渲染。 */
    if (!(await ui.syncCheck())) return;

    /* 上一轮的结果行作废：本轮还没开始，留着"完成 3 段"会让人以为已经跑过了。 */
    ui.runResult.style.display = "none";
    ui.runResult.textContent = "";
    /* 「存为新版本」已改为**默认启用**（20261010 用户定，勾选框删除）：
       渲染前无条件把画布上存档节点的「另存为版本」勾上 —— 重抽永远另存，
       旧版本原样保留，不再有"覆盖了就回不去"。一次性的动作放在入队前做，
       而不是等执行 —— 存档节点只看自己控件的值。 */
    if (!ui.setSaveNewTake(true)) {
      /* 区分「一个存档都没接」和「只接了 💾 续拍存档」：后者是极常见的接线，
         而续拍存档只负责下一段的潜空间续写承接，**不管版本归档**。统一说
         "画布上没有存档节点"会让人以为自己接错了、甚至去拆掉正在用的
         续拍存档（20261012）。 */
      const hasCont = (app.graph && (app.graph._nodes || app.graph.nodes) || [])
        .some((n) => n && n.type === "MiniMaxH3MotionContextSaveLatent");
      console.warn("[H3 Studio] 画布上没有「📦 H3 段落存档」节点 —— 本次无法另存新版本，"
        + "重抽会直接覆盖当前版本。"
        + (hasCont
          ? "（已接的「💾 H3 续拍存档」只负责下一段的续写承接，不管版本归档，"
            + "两者不是一回事，不用换掉它；想让重抽保留旧版，再接一个"
            + "「📦 H3 段落存档」即可。）"
          : "（想让重抽保留旧版，请接一个「📦 H3 段落存档」。）"));
    }

    /* Free undo: the project as it stands goes into the same slot New and Open
       use, so Revert in the project panel brings it back. */
    ui.snapProject("全部渲染前");
    ui.stash();
    /* 渲染期权威令牌：渲染循环成为唯一权威，self-heal 立即冻结"后端→前端"
       自动灌入，杜绝渲染期间用后端旧值覆盖画布（尺寸不生效根因）。 */
    projectStore.beginRender(ui.node);
    const startIdx = p.idx >= 0 ? p.idx : 0;
    /* 中断原因。catch 里拿到 err，finally 里要用——跨这两个块必须有个外层变量，
       否则 finally 就分不清"跑完了"和"抛异常退出的"。 */
    let failMsg = "";

    /* Which clips the scope covers, minus skipped ones. "clip" is an explicit
       ask, so it renders even a skipped clip. */
    const range = scopeRange(scope, p.idx >= 0 ? p.idx : 0);
    const queue = scope === "clip" ? range.slice() : range.filter((i) => !p.shots[i].off);
    if (!queue.length) {
      ui.lanesFlash("该范围内的分段全部被跳过 —— 没有可渲染的段落");
      return;
    }
    const skipped = range.length - queue.length;

    /* 重抽档位是**逐段**存的，用户却是在点渲染前设的 —— 那一档只落到当时
       正在编辑的那一段，其余各段各自留着旧档；渲染依次 switchTo 各段，档位
       条就跟着跳成别段的档（20261013「点渲染后『重来』变『微调』」）。
       本轮按渲染按钮旁**当前显示的那一档**统一跑，写进本轮各段并明确说一声
       —— 想各段不同就逐段切过去设（本轮已存了「全部渲染前」快照，可撤销）。 */
    if (queue.length > 1 && ui.st && ui.st.redo_mode) {
      const runRedo = String(ui.st.redo_mode);
      let n = 0;
      for (const i of queue) {
        const sh = p.shots[i];
        if (sh && sh.state && String(sh.state.redo_mode || "") !== runRedo) {
          sh.state.redo_mode = runRedo;
          n++;
        }
      }
      if (n) {
        ui.commit();
        ui.lanesFlash(`本轮 ${queue.length} 段的重抽档已统一为「${redoGroupOf(runRedo)} · ${runRedo}」`
          + `（改了 ${n} 段）—— 想各段不同，请切到那段单独设`);
      }
    }

    ui.run = { cancel: false, i: queue[0], k: 0, total: queue.length, mode, scope,
            skipped, note: "", made: [] };
    paintRun();
    /* 立刻重画分段列表：把建段/删段/换工程这类按钮在运行期间置灰 */

    let previous = null;
    let stopped = false;
    try {
      for (let k = 0; k < queue.length; k++) {
        const i = queue[k];
        ui.run.i = i; ui.run.k = k; ui.run.note = ""; paintRun();
        switchTo(i);
        /* 渲染必须让文案台/媒体架跟着切到本段：switchTo 只切了导演台面板 +
           媒体架（restoreShotRack 同步），文案台靠广播异步切、且切段读的是
           后端 current —— 手动点段时 onPick 里有 setCurrent 会更新 current，
           渲染循环里没有，current 一直停在渲染前的段，文案台 desk_data 就
           始终输出旧段提示词，三段渲染同词（20261012「三段生成都一样」根因）。
           这里显式补一次 current，并等一拍让文案台切段落盘。 */
        await setCurrent(ui.castName(), p.shots[i].clip || "");
        await sleep(50);
        /* 是否与上段接续：断链真值已统一到 transitions[cid]=unlink（胶囊
           「连播」开关、后端 _continuation_latent 都读它）；shot.link===false
           是旧工程遗留的第二真值，一并认下（20261012 接缝根治）。 */
        const _cid0 = p.shots[i].clip || "";
        const _unlinked = !!(_cid0 && p.transitions && p.transitions[_cid0]
          && p.transitions[_cid0].type === "unlink");
        const linked = !(i > 0 && (p.shots[i].link === false || _unlinked));

        if (linked && i > 0 && mode === "continue") {
          if (ui.st.cont.mode === "latent") {
            /* 潜空间续写：承接上一分段的采样存档（「续拍存档」每个分段
               跑完都会自动落一个新文件，抓最新的就是上一分段的）。 */
            ui.run.note = "正在引用上一分段的续拍存档\u2026"; paintRun();
            const got = await ui.adoptLastLatent();
            if (!got) {
              throw new Error("没找到续拍存档 — 工作流里是否接了「H3 续拍存档」"
                            + "（采样输出 → 存档，编号 0）？\n\n"
                            + "也可以把该分段的续写档位切回「文件续写」，"
                            + "用上一段的成片当源片。");
            }
            ui.st.cont.latent_file = got.path;
            /* 源片优先用上一分段这次渲染的成片（连续渲染里两者必然同源），
               抓不到才退回「最新输出」。 */
            ui.applyLatentAdoption(ui.st.cont,
              { path: got.path, preview: previous || got.preview });
          } else {
            if (!previous) throw new Error("前序片段未产出视频，无法续写");
            ui.run.note = "正在截取前序渲染的尾部\u2026"; paintRun();
            await ui.adoptInto(ui.st.cont, previous);
          }
          ui.render(); ui.commit(); ui.stash();
        }

        /* 排队前自检：缺素材就拦在前端，报清是哪段缺哪个标签、怎么补 */
        ui.run.note = "检查引用素材…"; paintRun();
        const miss = ui.missingRefs();
        if (miss.length) {
          throw new Error(
            `提示词引用的 ${miss.join("、")} 没有对应素材（面板槽位与媒体架均为空）。\n\n`
            + `媒体架现在按分段记忆：切到本分段，把素材放回媒体架即可，它会随该分段自动保存。`);
        }

        ui.run.note = "已入队，等待渲染\u2026"; paintRun();
        const id = await queueOnce();
        const entry = await waitForPrompt(id);
        const out = videoFromHistory(entry);
        if (!out) {
          throw new Error("该次渲染未产出视频文件 \u2014 工作流里是否连接了 Glide Video（或其他视频输出）？");
        }
        previous = out;
        ui.run.made.push(out.name);
        console.log(`[H3 Studio] Render all: clip ${i + 1}/${p.shots.length} \u2192 ${out.name}`);
        /* 收片：把这次的产物收进本片文件夹（h3_chain/<片名>/clip_000N.mp4）
           并登记为这一段的成片。不管它，档案里的 film 就一直是空的 ——
           刚跑出来的片子，时间轴和监视器却说"还没有成片"，只能手工去挑文件。
           渲染节点把文件写在哪儿由工作流决定，档案必须自己把它收拢到同一处，
           否则「成片 / 潜空间 / 波形」三样东西散在三个地方。 */
        ui.run.note = "收片…"; paintRun();
        await ui.adoptFilm(p.shots[i], out);

        if (ui.run.cancel) { stopped = true; break; }
      }
    } catch (err) {
      stopped = true;
      if (String(err && err.message) === "cancelled") {
        failMsg = "已手动停止";
        console.log("[H3 Studio] Render all: stopped");
      } else {
        failMsg = String((err && err.message) || err);
        /* 中断仍然不弹窗（20261008 用户要求移除全部此类弹窗）。但"原因只进控制
           台"意味着面板上什么都没有——用户跑完 8 段、第 3 段炸了，看到的是一个
           干干净净的空闲面板，无从判断要不要重跑。所以原因另写进结果行。 */
        console.error("[H3 Studio] Render all stopped:", err,
          `—— 第 ${ui.run.i + 1} 个分段处中断，已完成 ${ui.run.made.length} 个分段并保留`);
      }
    } finally {
      const made = ui.run ? ui.run.made.slice() : [];
      const at = ui.run ? ui.run.i : startIdx;
      ui.run = null;
      /* 渲染结束：交回编辑态，前端恢复权威（此时各段已是后端新值，且
         L434 switchTo 会把面板归位）。 */
      projectStore.endRender(ui.node);
      paintRun();
      /* 结果行：成败、完成了几段、断在哪一段、为什么。跑完一轮之后这是面板上
         唯一能回答"刚才到底怎么样了"的地方，所以常驻到下一次渲染开始。 */
      if (failMsg) {
        ui.setRunResult(false, `中断于第 ${at + 1} 段 · 已完成 ${made.length} 段`
          + `（${failMsg}）`);
      } else if (made.length) {
        ui.setRunResult(true, `完成 ${made.length} 段 · 最后一段 ${made[made.length - 1]}`);
      }
      /* Back to the clip the run started from, so the panel is where it was */
      if (p.shots[startIdx]) switchTo(startIdx);
      ui.paintSegTools();
      if (!stopped && made.length) {
        console.log(`[H3 Studio] Render all: done, ${made.length} clip(s), `
                  + `final file ${made[made.length - 1]}`);
      }
      /* 重抽的结果**一律直接采纳**（20261010 用户定）：不再弹「采纳 / 丢弃 /
         再抽一次」那个对比框 —— 跑了就是要新的。旧版本仍然留在磁盘上，要回去
         用段块右键里的版本清单切，那条路没动。

         这一轮跑完必须重拉一次档案：收片（adoptFilm）改的是**后端**，前端时间轴
         手里还攥着跑之前那份 film_summary —— 不重拉，段块上就还是重抽前的画面。
         段块画面的签名带档案 updated（见 csglide_film.js 的 renderStrip），重拉
         之后签名才会变，图才会重取。 */
      /* 刷新是一次网络来回，失败不该把整轮渲染的收尾打断 —— 画面晚一点刷出来，
         也比在 finally 里抛个错让人以为渲染失败强。 */
      await ui.monitor.refresh().catch(() => {});
    }
  }

  /* Which clips a scope covers, in project order. */
  function scopeRange(scope, a) {
    const p = ui.proj();
    const n = p.shots.length;
    if (!n) return [];
    a = Math.min(Math.max(0, a | 0), n - 1);
    let s = 0, e = n - 1;
    if (scope === "clip") { s = a; e = a; }
    else if (scope === "from") { s = a; }
    const out = [];
    for (let i = s; i <= e; i++) out.push(i);
    return out;
  }

  function switchTo(i) {
    const p = ui.proj();
    if (i === p.idx || !p.shots[i]) return;
    ui.stash();
    p.idx = i;
    /* 先倒回该段的媒体架快照，再加载面板：load 里的 normalizeRefs / 别名表都靠
     * mergedSlots() 读媒体架——顺序反了就会用上一段的旧架况去归一与展开别名，
     * 出现"媒体架还原了但提示词引用对不上槽位"。 */
    ui.restoreShotRack(p.shots[i]);
    ui.load(JSON.stringify(p.shots[i].state));
    ui.commit();
    ui.paintSegTools();
    /* 切段即时广播：媒体架/文案台零延迟跟上（未登记段 clip 为空，它们
       会等登记成功广播 / 3 秒轮询兜底，20261012）。 */
    ui.broadcastSeg && ui.broadcastSeg("switch");
  }

  /* 删除一段 —— **真删**：轴上的块、工程里的分段、以及磁盘上的成片 / 潜空间 /
     波形 / 段落 json 一起去掉。只有这一种语义，没有"只从轴上拿掉"或"只从工程
     拿掉"这回事；右键菜单里那个同名的入口也已经撤掉，删除只有一个去处。

     删之前必须确认一次，而且确认框里要列出**会删掉哪些文件**：这是全流程里
     唯一不可逆的操作（工程面板的「还原」只回退分段配置，删掉的文件不回来）。
     清单由后端给，见 ui.confirmDeleteSegment。 */
  async function delShot(i) {
    const p = ui.proj();
    const s = p.shots[i];
    if (!s) return;
    /* 渲染中不给删：正在跑的链正按 p.shots 的下标取段，抽掉一段会让它去跑
       别的段 —— 与其跑错一段再回滚，不如不让删。 */
    if (ui.run) { ui.lanesFlash("正在渲染 —— 等这一轮跑完再删这一段"); return; }
    const label = ui.shotLabel(s, i);
    /* 不弹确认窗（20261009 用户定：点删除就删，不要任何弹窗）。
       删除是唯一不可逆的操作，代价靠事后一条**非阻塞**提示 + 工程面板「撤销」
       兜底：撤销能退回分段配置，删掉的成片/潜空间文件不会回来 —— 这条在
       提示语里说清楚，而不是用弹窗挡人。 */
    ui.snapProject("删除前 " + label);
    console.log(`[H3 Studio] 删除分段「${label}」：工程与档案一起删，`
      + "磁盘文件不可恢复（分段配置可用工程面板「撤销」退回）");
    p.shots.splice(i, 1);
    if (!p.shots.length) p.idx = -1;
    else if (p.idx > i) p.idx--;
    else if (p.idx === i) {
      p.idx = Math.min(i, p.shots.length - 1);
      ui.restoreShotRack(p.shots[p.idx]);
      ui.load(JSON.stringify(p.shots[p.idx].state));
    }
    ui.commit();
    ui.paintSegTools();
    /* force = 真删（连磁盘上的成片 / 潜空间 / 波形一起清）。
       clip id 还没有（登记失败/被还原）也照删 —— 后端按分段 uid 反查档案
       条目；留着不删的话，删到剩一段底栏也还是 6 段的总时长。 */
    const data = await ui.unregisterShot(s, true);
    if (data && data.removed) {
      const n = (data.files || []).length;
      ui.lanesFlash(`已删除第 ${i + 1} 段「${label}」`
        + (n ? `（连同 ${n} 个档案文件）` : "")
        + "　·　删掉的文件不可恢复，分段配置可用「撤销」退回");
    }
    /* 时间轴永远保底一段（20261010 用户定）：删掉最后一段的瞬间自动补一个
       **默认初始设置**的新段 —— 空轴不是合法状态：没有段块，悬停工具条、
       接缝插入点、右键菜单全部没有落点，交互整条断掉。
       （「删空清档案残留」的旧逻辑随之移除：删光马上有新段顶上，档案里的
       残留段用悬停 ✕ 或右键逐个清理。） */
    if (!p.shots.length) ui.restoreDefaultShot();
  }


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { sleep, VIDEO_OUT_RE, queueOnce, waitForPrompt, PREVIEW_RE, videoFromHistory, paintRun, renderAll, scopeRange, switchTo, delShot });
}
