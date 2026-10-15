import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { el } from "./csglide_ui_kit.js";

/* ============================================================
 * 工程文件菜单 / 还原点历史菜单 / 导出成片（整轴拼接）
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installReview(ui) {
  /* （重抽后的「采纳 / 丢弃 / 再抽一次」对比框已删，20261010 用户定：重抽的
     结果一律直接采纳，不弹窗。旧版本仍然留在磁盘上 —— 要回去用段块右键里的
     版本清单切（csglide_cast_takes.js 的 switchTake），那条路没动。） */

  /* 菜单定位 + 点到菜单与锚点之外即收起。pointerdown 判定、延迟一拍挂，
     避免打开菜单的那一下立刻把自己关掉。 */
  function placeMenu(menu, x, y, anchor) {
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    document.body.append(menu);
    const rm = (ev) => {
      if (menu.contains(ev.target)) return;
      menu.remove();
      document.removeEventListener("pointerdown", rm, true);
    };
    setTimeout(() => document.addEventListener("pointerdown", rm, true), 0);
  }

  /** 工程文件菜单（轴头「工程 ▾」）。按风险分三组：生命周期（整体替换）/
      导入（追加）/ 导出文件（备份换机）；底部是不可点的状态行。 */
  function projMenu(x, y) {
    const p = ui.proj();
    const menu = el("div", "gcast-menu");
    menu.dataset.mode = ui.st.mode;
    const item = (label, title, fn, disabled) => {
      const b = el("button", null, label);
      b.type = "button";
      b.title = title || "";
      if (disabled) { b.disabled = true; b.style.opacity = ".4"; }
      else b.onclick = () => { menu.remove(); fn(); };
      menu.append(b);
      return b;
    };
    const sep = () => menu.append(el("div", "gcast-msep"));

    /* 渲染中禁用整组：动到工程会打乱正在跑的渲染循环。 */
    const busy = !!ui.run;

    /* 第一组：工程生命周期（整体替换，旧工程自动存还原点、可撤销）。
       「新建工程」已移到节点右上角（20261011），菜单里不再重复。 */
    item("打开工程…", "打开工程文件（.h3proj.json / .h3proj.zip）替换当前工程；原工程自动存为还原点",
      () => ui.openProject(), busy);
    sep();
    /* 第二组：追加，不动现有段。 */
    item("导入分段…", "把已保存工程文件里的分段追加到当前工程（不替换）",
      () => ui.importShot(), busy);
    sep();
    /* 第三组：导出到磁盘文件。配置本就自动存在节点里，这些不是"保存进度"，
       是导出文件做备份 / 换机。 */
    item("保存工程文件", ui.projLabel
      ? "覆盖写回 " + ui.projLabel + "（导出配置到磁盘文件）"
      : "把工程配置导出为一个文件（备份 / 换机用）",
      () => ui.saveProject(false), busy);
    item("工程另存…", "把工程配置写到一个新文件", () => ui.saveProject(true), busy);
    item("工程打包…", "工程配置 + 全部参考素材导出为一个 .zip（换电脑不丢素材，共用素材只存一份）",
      () => ui.packProject(), busy);
    sep();
    /* 状态行（不可点）。 */
    menu.append(el("div", "gcast-msep-label", `${p.shots.length} 段 · 工程名即片名`));
    menu.append(el("div", "gcast-msep-label",
      p.name ? `档案：output/h3_chain/${p.name}/` : "还没填工程名 —— 填了才会归档"));

    placeMenu(menu, x, y, ui.bProj);
  }

  /* 工程还原点历史菜单已删（20261012 用户定）：轴头「历史 ▾」按钮撤下，
     菜单随之孤立移除；还原点数据与快捷键撤销 / 重做仍保留。 */

  /** 改名需要一个**看得见**的承载元素，也就是时间轴里那一段的块。
      段块现在住在 lanesWrap（band 被 timelineHost 外挂过去），且类名是
      .bzf-seg[data-seg]（旧工程分段条 .bzf-ui.seg 早已撤下）。收起监视器时
      改不了名，就在菜单里明说，而不是假装成功了。 */
  function menuAnchorFor(i) {
    return ui.lanesWrap.querySelector(`.bzf-seg[data-seg="${i + 1}"]`);
  }

  /* ---- 导出成片（整轴拼接，20261010；20261011 增续渲串联）------------
     导出读的是磁盘上**已经生成好**的各段成片，把它们按净长拼成一条完整视频
     写进档案目录，不是重新渲染一遍。点下去前先把两件事摆出来：
       1. 片子到哪儿为止 —— 后面的段没生成，成片就到不了那里；
       2. 改动进没进片子 —— 段上挂着待办，成片还是改之前那一版。
     用户三选一：先不导出 / 仍要这一版 / 先续渲再导出（自动从未就绪段开始
     续写渲染，跑完自动导出）。 */

  async function chainJSON(url) {
    const res = await api.fetchApi(url);
    if (res.status === 404) {
      /* 404 = 后端没有这个接口。八成是刚更新过插件、只刷新了页面：
         前端 JS 刷新浏览器就生效，后端 Python 必须重启 ComfyUI 才会注册上新路由。 */
      throw new Error("后端没有这个接口（404）—— 如果刚更新过插件，重启一次 ComfyUI "
        + "再试（刷新页面只更新前端，后端要重启才生效）");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }
  const planURL = (name) =>
    "/cglide/chain/export_film_plan?name=" + encodeURIComponent(name);

  /** 把导出计划翻成几行人话。一行一个事实，不写「可能」「建议」这种软话。 */
  function exportNotes(plan) {
    const out = [];
    const miss = (plan.beyond || []).filter((r) => !r.playable);
    if (miss.length) {
      out.push(`后面还有 ${miss.length} 段没生成（`
        + miss.map((r) => `第 ${r.index} 段`).join("、")
        + `）—— 成片只到第 ${plan.at_index} 段为止，这些不在片子里。`);
    }
    const pend = plan.pending || [];
    if (pend.length) {
      out.push(`有 ${pend.length} 段改过但还没重跑：`
        + pend.map((r) => `第 ${r.index} 段 ${r.text || r.kind}`).join("；")
        + " —— 成片用的是改之前那一版。");
    }
    if ((plan.broken || []).length) {
      out.push(`有 ${plan.broken.length} 段的档案读不出来（被手动删了或写坏了）`
        + " —— 它们也不在这条片子里。");
    }
    /* 位置对不上的段：本段新画面在这条成片里应有的位置已经超出了片子实际
       长度 —— 说明这条片子不是「到这一段为止的整条」（接了外来源片 / 重抽后
       长度变了）。裁出来的一截位置不对，拼进去不是重播就是跳戏。 */
    const short = plan.short || [];
    if (short.length) {
      out.push(`有 ${short.length} 段的成片对不上时间轴上的位置（`
        + short.map((r) => `第 ${r.index} 段：成片只有 ${r.film_frames} 帧，`
            + `本段要取 ${r.want} 帧处`).join("；")
        + `）—— 这几段重跑一次再来导出。`);
    }
    return out;
  }

  /** 第一个未就绪段（0-based）：续渲的起点。 */
  function firstBadIndex(plan) {
    const idxs = [];
    (plan.missing || []).forEach((r) => idxs.push(r.index));
    (plan.beyond || []).filter((r) => !r.playable).forEach((r) => idxs.push(r.index));
    (plan.pending || []).forEach((r) => idxs.push(r.index));
    (plan.broken || []).forEach((r) => idxs.push(r.index));
    (plan.short || []).forEach((r) => idxs.push(r.index));
    if (!idxs.length) {
      /* 兜底：老后端（无 missing 字段）报 no_film 时，从第 1 段起。 */
      return plan.blocker === "no_film" ? 0 : -1;
    }
    return Math.min(...idxs) - 1;
  }

  /** 导出成片：读计划 → 有问题给三选一对话框（可续渲）→ 拼接写盘。 */
  async function saveFilmMerged(btn) {
    const name = ui.castName();
    if (!name) {
      ui.lanesFlash("还没有片名 —— 先在指挥条的工程名上填一个");
      return;
    }
    let plan;
    try {
      plan = await chainJSON(planURL(name));
    } catch (err) {
      ui.lanesFlash("读不到这部片子的档案：" + err.message);
      return;
    }
    let notes = plan.ok ? exportNotes(plan) : [];
    /* 阻断态（一部成片都没有 / 中间缺段）：转成一条清单，同样给三选一 ——
       「先续渲再导出」从第一个未就绪段跑起、跑完自动导出。其余 blocker
       （no_such_clip 等 URL 异常）直接提示。 */
    if (!plan.ok) {
      if (plan.blocker === "no_film" || plan.blocker === "missing_clips") {
        notes = [plan.message || "还没有可导出的成片"];
      } else {
        ui.lanesFlash(plan.message || "还没有可导出的成片");
        return;
      }
    }
    if (notes.length) {
      const choice = await askExport(plan, notes, "导出");
      if (choice === "cancel") return;
      if (choice === "export") {
        if (!plan.ok) {
          ui.lanesFlash("这一版还没有任何成片可导出 —— 请选「先续渲再导出」");
          return;
        }
      }
      if (choice === "render") {
        const from = firstBadIndex(plan);
        const fromIdx = from >= 0 ? from : 0;
        ui.lanesFlash(`先续渲再导出 —— 从第 ${fromIdx + 1} 段开始，跑完自动导出`);
        if (ui.proj().idx !== fromIdx) ui.switchTo(fromIdx);
        await ui.renderAll("continue", "from");
        await ui.monitor.refresh().catch(() => {});
        /* 重读计划：全部就绪才静默导出；仍有未完成段（中断 / 失败）就停下，
           不再弹一次窗。 */
        let plan2;
        try { plan2 = await chainJSON(planURL(name)); }
        catch (err) { ui.lanesFlash("续渲后读不到档案：" + err.message); return; }
        if (!plan2.ok) { ui.lanesFlash(plan2.message || "还没有可导出的成片"); return; }
        const notes2 = exportNotes(plan2);
        if (notes2.length) {
          ui.lanesFlash("续渲未全部完成 —— 处理后再点「导出成片」");
          return;
        }
        plan = plan2;
      }
    }
    await doExport(btn, plan, name);
  }

  /** 计划已就绪后的实际导出：弹另存为 → 后端拼接 → 写盘 / 下载。
      档案目录同时留一份副本（output/h3_chain/<片名>/<片名>_成片.mp4）。 */
  async function doExport(btn, plan, name) {
    const defaultName = plan.file || (name + "_成片.mp4");
    /* 先弹「另存为」再拼接：选好位置，拼完直接写过去。取消 = 不导出；
       环境不支持（Firefox / 非 localhost）降级为浏览器下载。 */
    let handle = null;
    if (window.showSaveFilePicker) {
      try {
        handle = await window.showSaveFilePicker({
          suggestedName: defaultName,
          types: [{ description: "MP4 视频", accept: { "video/mp4": [".mp4"] } }],
        });
      } catch (err) {
        if (err && err.name === "AbortError") return;   // 用户点了取消
        handle = null;                                   // 其它失败 → 降级
      }
    }

    const oldText = btn ? btn.textContent : "";
    if (btn) {
      btn.dataset.busy = "1";
      btn.disabled = true;
      btn.textContent = "拼片中…";
    }
    let merged = null;      // 拼接结果；非空 = 档案副本已生成
    try {
      const res = await api.fetchApi("/cglide/chain/export_film", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      if (res.status === 404) {
        throw new Error("后端还没有拼接接口 —— 重启一次 ComfyUI 再来"
          + "（刷新页面只更新前端，后端要重启才生效）");
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      if (data.ok === false) throw new Error(data.message || "拼接失败");
      merged = data;
      const secs = (data.seconds || 0).toFixed(1);

      /* 拼好的片已在档案里，现在送到用户选的位置。 */
      let where;
      if (handle) {
        const fileRes = await api.fetchApi("/cglide/chain/export_film_file?name="
          + encodeURIComponent(name));
        if (fileRes.status === 404) {
          throw new Error("后端还没有读回接口 —— 重启一次 ComfyUI 再试");
        }
        if (!fileRes.ok) throw new Error("读回成片失败（HTTP " + fileRes.status + "）");
        const w = await handle.createWritable();
        try {
          await fileRes.body.pipeTo(w);      // 流式写盘：不整块占内存
        } catch (e) {
          try { await w.abort(); } catch (_) {}   // 写一半失败：解锁文件别占着
          throw e;
        }
        where = handle.name || defaultName;
      } else {
        /* 降级：触发浏览器下载，存哪由浏览器说了算。 */
        const a = el("a");
        a.href = api.apiURL("/cglide/chain/export_film_file?name="
          + encodeURIComponent(name));
        a.download = defaultName;
        document.body.append(a);
        a.click();
        a.remove();
        where = "浏览器下载（" + defaultName + "）";
      }
      ui.lanesFlash(`成片已导出：${where}`
        + `（${data.frames} 帧 · ${secs} 秒 · ${data.clips} 段`
        + (data.audio === false ? " · 无音轨" : "")
        + `；档案副本：${data.path}）`);
    } catch (err) {
      ui.lanesFlash("导出成片失败：" + err.message
        + (merged ? "（拼接本身已完成，档案副本在：" + merged.path + "）" : ""));
    } finally {
      if (btn) {
        btn.dataset.busy = "";
        btn.textContent = oldText || "⬇ 导出成片";
        ui.paintSegTools();
      }
    }
  }

  /** 导出前的对话框。用真正的对话框而不是 confirm：清单有好几行，
      confirm 会把换行压成一个方块，用户根本读不完就点了确定。
      返回三选一：'cancel' / 'export'（仍要这一版）/ 'render'（先续渲）。 */
  function askExport(plan, notes, verb) {
    verb = verb || "导出";
    return new Promise((resolve) => {
      const wrap = el("div", "gcast-modal");
      const sheet = el("div", "gcast-sheet");
      const secs = (plan.seconds || 0).toFixed(1);
      const ready = !!(plan.ok && plan.at_index);
      sheet.append(el("h3", null, ready
        ? `${verb}到第 ${plan.at_index} 段为止`
          + (plan.total_clips > plan.at_index ? `（共 ${plan.total_clips} 段）` : "")
        : `还没有可${verb}的成片（共 ${plan.total_clips || 0} 段）`));
      const body = el("div", "body");
      if (ready) {
        body.append(el("div", "hint",
          `这一版是第 1–${plan.at_index} 段合起来的：`
          + `${plan.frames} 帧 · ${secs} 秒。`));
      }
      notes.forEach((t) => body.append(el("div", "gcast-export-note", t)));
      const foot = el("div", "foot");
      const cancel = el("button", "gcast-btn ghost", "先不导出");
      const go = el("button", "gcast-btn ghost", `仍要${verb}这一版`);
      if (!ready) go.style.display = "none";   // 无成片可导出，不给这个选项
      const renderFirst = el("button", "gcast-btn", "先续渲再导出");
      renderFirst.title = "关闭本窗，自动从第一个未生成 / 待重跑段开始续写渲染，"
        + "跑完后自动导出成片";
      foot.append(cancel, go, renderFirst);
      sheet.append(body, foot);
      wrap.append(sheet);
      const done = (v) => { wrap.remove(); resolve(v); };
      cancel.onclick = () => done("cancel");
      go.onclick = () => done("export");
      renderFirst.onclick = () => done("render");
      wrap.addEventListener("pointerdown", (ev) => {
        if (ev.target === wrap) done("cancel");
      });
      document.body.append(wrap);
      renderFirst.focus();
    });
  }

  /* 导出按钮"未就绪"小黄点徽标机制已于 20261012 应要求移除：未就绪清单在
     点击「导出成片」弹出的确认窗里照常逐条列出（askExport），不影响导出
     与"先续渲再导出"流程，只是按钮上不再常驻数字角标。 */


  /* ---- 推送整片到下游（不重跑，20261013）-----------------------------
     「整片」口连到下游（视频合并 / 超分 → 视频合并）后，点按钮把**磁盘上已
     存好的整片文件**直接喂给下游，**导演台 / 媒体架 / 文案台 / CLIP / VAE 一个
     都不跑**（连 VAE、文本编码器都不加载）。步骤：
       1. 调 /cglide/chain/preview_film?ensure=1 让后端把**时间轴所有段按净长**
          拼成完整片（_preview.mp4，与监视器连播/保存成片同一条；段没变复用
          缓存，只流复制不重渲染），这一步不依赖导演台执行；
       2. graphToPrompt 出全图，往里面注入一个临时 BanzhangVisualLoader
          （视频口 index 3）直接加载这个完整片文件；
       3. 从整片口「正向」追踪整条链（超分 → 视频合并 OUTPUT_NODE），不含导演台；
       4. 链上引到「导演台整片口」的改接加载器，引到链外的（含 KSampler、
          CLIP/VAE 等）全部删除，只留整片链 + widget 标量；
       5. 反向收上游闭包（加载器无上游）、校验含 OUTPUT_NODE（否则 400
          "Prompt has no outputs"），queuePrompt 提交 { output: sub, workflow }。
     最终子图只有：加载器 + 超分(可选) + 视频合并。 */

  /* 判断 graphToPrompt 输出里某个值是不是连线引用（[源节点id, 输出口序号]）。 */
  const isRef = (v, out) =>
    Array.isArray(v) && v.length === 2 && !!out[v[0]];

  async function pushFilm(btn) {
    const castNode = ui.node;
    const outputs = castNode.outputs || [];
    const slotIdx = outputs.findIndex((o) => {
      const nm = (o.name || "").toLowerCase().replace(/[\s_]+/g, "");
      return nm === "整片" || nm === "wholefilm";
    });
    if (slotIdx < 0) {
      ui.lanesFlash("节点上没有「整片」口 —— 重启 ComfyUI 让后端节点定义刷新后再试");
      return;
    }
    /* 找连在整片口上的下游节点（去重）。 */
    const targets = [];
    for (const linkId of (outputs[slotIdx].links || [])) {
      const link = castNode.graph.links[linkId];
      if (link && link.target_id != null && !targets.includes(String(link.target_id))) {
        targets.push(String(link.target_id));
      }
    }
    if (!targets.length) {
      ui.lanesFlash("整片口还没连出去 —— 把「🎬 整片」连到下游节点的视频口（视频合并 / 超分 等）");
      return;
    }
    const oldText = btn ? btn.textContent : "";
    if (btn) { btn.dataset.busy = "1"; btn.disabled = true; btn.textContent = "推送中…"; }
    try {
      /* 整片 = 时间轴上**所有段按净长拼成的完整片**（档案目录下的 _preview.mp4，
         与监视器连播 / 「保存成片」是同一条片子），不是某一段自己的 clip
         （clip_000x.mp4 只是单段、约 10 秒）。让后端 preview_film 现拼 / 取缓存：
         ensure=1 时段内容或顺序变了就重拼（流复制 + 一次重编码，几秒），没变
         直接复用缓存。这一步只读盘拼接、不渲染、也不执行导演台。 */
      const chainName = (ui.st && ui.st.chain && ui.st.chain.name) || "";
      if (!chainName) {
        ui.lanesFlash("还没有工程名 —— 先在导演台里建 / 选一个工程再推送");
        return;
      }
      let whole = "";
      let wholeFrames = 0;
      try {
        const pvUrl = "/cglide/chain/preview_film?name="
          + encodeURIComponent(chainName) + "&ensure=1";
        const pvRes = await api.fetchApi(pvUrl);
        if (!pvRes.ok) throw new Error("HTTP " + pvRes.status);
        const pv = await pvRes.json();
        if (!pv.ok || !pv.path) {
          throw new Error(pv.message || pv.blocker || "整片尚未拼好");
        }
        whole = pv.path;
        wholeFrames = parseInt(pv.frames, 10) || 0;
      } catch (e) {
        ui.lanesFlash("整片拼不出来（" + e.message + "）—— 先渲染 / 播放至少一段再推送");
        return;
      }
      const pm = whole.match(/^(.*)[\\/]([^\\/]+)$/);
      if (!pm) { ui.lanesFlash("整片路径无法解析：" + whole); return; }
      const folder = pm[1];
      const fileOnly = pm[2];

      const p = await app.graphToPrompt();
      const out = p.output || p;
      /* ---- 注入一个「媒体加载器」直接读整片文件，顶替导演台整片口 ----
         这样子图里压根没有导演台 / 媒体架 / 文案台 / CLIP / VAE，也就不会
         加载 VAE、文本编码器、处理参考图（那 14 秒的重活）。加载器只把磁盘
         上的整片包成 VIDEO（VideoFromFile），毫秒级。 */
      const LOADER_ID = "__bz_pushfilm_loader__";
      const LOADER_VIDEO_SLOT = 3;   // BanzhangVisualLoader: 图像0/遮罩1/文件名2/视频3/音频4
      out[LOADER_ID] = {
        class_type: "BanzhangVisualLoader",
        inputs: {
          "📂 文件夹路径": folder,
          "📄 指定文件": fileOnly,
          "🔃 排序方式": "时间:最新优先",
          "🔍 缩略图尺寸": 60,
          "📁 含子目录": false,
        },
      };
      /* ---- 收集「整片链」（正向，只从整片口出发，不碰导演台其他输出）----
         整片可能经超分等处理节点再到视频合并（OUTPUT_NODE），所以沿输出正向
         追踪到终点。绝不能从导演台的正面条件等口正向走（那会拉进 KSampler）。 */
      const gnodes = app.graph._nodes || [];
      const gbyid = {};
      for (const n of gnodes) gbyid[String(n.id)] = n;
      /* 整片链：只从整片口直接下游开始，**不包含导演台**（它由加载器顶替）。 */
      const forward = new Set();
      const fq = [...targets];
      while (fq.length) {
        const nid = String(fq.shift());
        if (forward.has(nid)) continue;
        forward.add(nid);
        const n = gbyid[nid];
        if (!n) continue;
        for (const o of (n.outputs || [])) {
          for (const lid of (o.links || [])) {
            const l = castNode.graph.links[lid];
            if (l && l.target_id != null) fq.push(String(l.target_id));
          }
        }
      }
      /* 改链上节点的输入：
         · 引到「导演台整片口」的 → 改接到注入的加载器视频口（整片文件）；
         · 引到导演台其他口（正面条件等）或整片链之外节点的 → 删除（避免
           拉进 KSampler / CLIP / VAE 等重链）；
         · 链内引用保留。 */
      for (const nid of forward) {
        const e = out[nid];
        if (!e || !e.inputs) continue;
        for (const k of Object.keys(e.inputs)) {
          const v = e.inputs[k];
          if (!isRef(v, out)) continue;
          const src = String(v[0]);
          const slot = Number(v[1]);
          if (src === String(castNode.id) && slot === Number(slotIdx)) {
            e.inputs[k] = [LOADER_ID, LOADER_VIDEO_SLOT];
          } else if (forward.has(src)) {
            /* 链内引用，保留 */
          } else {
            console.log("[推送整片] 剥离 %s 的外部输入 %s（整片链之外）", nid, k);
            delete e.inputs[k];
          }
        }
      }
      /* 反向收集上游闭包：加载器（无上游）+ 整片链保留的输入。 */
      const keep = new Set([LOADER_ID]);
      const queue = [...forward];
      while (queue.length) {
        const nid = String(queue.shift());
        if (keep.has(nid)) continue;
        keep.add(nid);
        const e = out[nid];
        if (!e) continue;
        for (const k of Object.keys(e.inputs || {})) {
          const v = e.inputs[k];
          if (isRef(v, out)) queue.push(String(v[0]));
        }
      }
      /* 子图必须有 OUTPUT_NODE，否则后端报 "Prompt has no outputs"。整片只
         连到超分（非 OUTPUT_NODE）且超分输出悬空时，正是这个坑。加载器本身
         不是 OUTPUT_NODE，要排除它。 */
      const hasOutput = [...keep].some((nid) =>
        nid !== LOADER_ID && gbyid[nid]?.constructor?.nodeData?.output_node === true);
      if (!hasOutput) {
        throw new Error("整片链路末端没有输出节点 —— 把超分的「视频」输出连到视频合并，"
          + "或直接把整片连到视频合并节点");
      }
      /* 裁出子图。 */
      const sub = {};
      for (const nid of keep) if (out[nid]) sub[nid] = out[nid];
      console.log("[推送整片] 整片文件：" + whole
        + (wholeFrames ? "（" + wholeFrames + " 帧完整片）" : ""));
      console.log("[推送整片] 整片链=" + [...forward].join("/"));
      console.log("[推送整片] 子图节点：" + Object.keys(sub).join("/")
        + "（不含导演台/媒体架/文案台/CLIP/VAE，不重跑）");
      /* queuePrompt 第二个参数必须是 { output, workflow }：它内部解构 output
         当 prompt、workflow 塞进 extra_pnginfo。workflow 沿用原图（子图不含
         连线信息，只影响保存元数据，不影响执行）。 */
      const res = await api.queuePrompt(0, { output: sub, workflow: p.workflow });
      const id = res && (res.prompt_id || res.promptId);
      if (!id) throw new Error("服务端未返回任务 ID");
      const lenTxt = wholeFrames ? `${wholeFrames} 帧完整片 · ` : "";
      ui.lanesFlash(`已推送 ${lenTxt}到 ${targets.length} 个下游节点（不重跑渲染）· 任务 ${id}`);
    } catch (err) {
      ui.lanesFlash("推送整片失败：" + err.message);
    } finally {
      if (btn) {
        btn.dataset.busy = "";
        btn.textContent = oldText || "🎬 推送整片";
        if (ui.paintSegTools) ui.paintSegTools();
      }
    }
  }

  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, {
    projMenu, placeMenu, menuAnchorFor, chainJSON, exportNotes,
    firstBadIndex, saveFilmMerged, doExport, askExport, pushFilm,
  });
}
