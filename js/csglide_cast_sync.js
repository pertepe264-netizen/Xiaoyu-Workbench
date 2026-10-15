import { api } from "../../scripts/api.js";
import { el } from "./csglide_ui_kit.js";

/* ============================================================
 * 前端工程 ↔ 后端档案 的段同步
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installSync(ui) {
  /* ---- 前端工程 ↔ 后端档案 的段同步 ----
     时间轴（监视器）画的是**档案里的段**：film_summary 走 chain 的 order。
     而「接一段 / 删除这一段」改的是工程对象（跟工作流走的 p.shots）。两者
     本来只在渲染时对齐一次 —— 于是点了「接一段」，轴上什么都不出现，直到
     跑一次渲染。这里把这一步补上：加段就在档案里登记一段，删段就把空段去掉。

     登记失败不挡操作（没填工程名时根本无从登记），只记一条日志：加段本身是
     本地动作，不该因为后端没准备好就做不了。 */
  async function registerShot(shot, opts) {
    const silent = !!(opts && opts.silent);
    const name = ui.castName();
    if (!shot || shot.clip) return;
    if (!name) {
      /* 没有工程名就无从登记 —— 档案是按片名建的目录。接段入口（addShot）
         现在会在无名时先拦下并聚焦工程名，走到这里的多半是自愈之外的旧路径。
         必须说出来：不然用户看到的就是"点了按钮什么都没发生"。 */
      lanesFlash("还没填工程名 —— 填好片名后分段会自动登记上轴");
      return;
    }
    try {
      /* trim_frames 按**工程真实段序**显式发，不能让后端 add_clip 无脑继承
         chain 级默认（39）—— 首段会被错给 39；而且"接一段"时新段先登记、
         seedFromScreen 的首段后登记（ghost heal），后端按登记顺序/order 位置
         判断段序也会错。前端这里是唯一拿得到真实段序的地方：
           · 工程第 1 段 → trim=0（不承接任何段）
           · 第 2+ 段 → 头部承接窗口：优先 head pin 的窗口；否则读本段
             state.cont.frames（新建段出厂默认 39），都没有再兜底 39。 */
      const shots = ui.proj().shots;
      const si = shots.indexOf(shot);
      let trimFrames = 0;
      if (si > 0) {
        const hp = (shot.pins || []).find((p) => p.at === "head");
        const contN = Number(shot.state && shot.state.cont && shot.state.cont.frames) || 0;
        trimFrames = hp ? hp.window : (contN || 39);
      }
      const res = await api.fetchApi("/cglide/chain/add_clip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          /* 分段 uid 也一并登记：后端认段的第二优先级就是它，先写上，
             第一次渲染就能对到这一段，而不是又新建一段。 */
          shot: shot.id,
          frames: (shot.state && shot.state.length) || 0,
          trim_frames: trimFrames,
          /* 这个名字会显示在时间轴的段块上。空着也没关系，后端会给「第 N 段」
             兜底，而且那种兜底名前端不显示（编号已经有了，重复一遍白占地方）。 */
          clip_name: shot.name || "",
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      if (data.id) {
        shot.clip = data.id;
        /* 登记成功也要说一声：轴上的新块马上会出现，这句确认了「登记」这一步
           没有悄悄掉链子 —— 出问题时能把故障层位（前端 / 登记 / 绘制）一刀切开。 */
        if (!silent) lanesFlash(`第 ${data.index || "?"} 段已登记进档案`);
        /* 拿到真实 clip id 后，立刻用它给媒体架"认段"：新段登记时后端会把
           档案 current 指过来，媒体架轮询一旦发现新 key 而自己没快照就会清空
           （复制/接段后参考图消失的竞态）。这里把 shot.rack 连同 clip id 一起
           灌好，并即时广播，媒体架/文案台零延迟切到新段（20261012）。 */
        const p = ui.proj();
        if (p.shots[p.idx] === shot) {
          try { ui.restoreShotRack(shot); } catch (_) {}
          /* 文案台临时段身份（shot.id）升级为真实 clip id，把提前抄好的段词搬
             过来，再广播 —— 文案台切到 clip id 时段词已就位、不会先空一拍。 */
          try { ui.claimDeskSeg && ui.claimDeskSeg(shot.id, shot.clip); } catch (_) {}
          ui.broadcastSeg && ui.broadcastSeg("seg");
        }
      }
      ui.commit();
      /* silent = 自愈批量补登记：循环里每段都 refresh 会连环打 film_summary、
         全轴重绘 N 遍（卡顿源之一）；由调用方在循环结束后统一刷一次。 */
      if (!silent) await ui.monitor.refresh();
    } catch (e) {
      console.warn("[H3 Studio] 新分段没能登记到档案（不影响出图）：", e);
      lanesFlash("登记档案失败：" + (e && e.message ? e.message : e)
        + " —— 分段已加进工程，稍后重跑一次渲染也能对上");
    }
  }

  /* 把刚渲染出来的视频收进本片文件夹，并登记为该段的成片。
     渲染节点写在哪儿由工作流决定（output 根目录 / video 子目录 / 文件名前缀
     都是用户自己设的），档案若只记那个位置，一部片子的成片就散在 output 各处；
     而潜空间与波形都躺在 h3_chain/<片名>/ 里。统一在后端收口：拷成
     clip_000N.mp4 再登记。收片失败不影响出图，只记一条日志。 */
  async function adoptFilm(shot, out) {
    const name = ui.castName();
    if (!name || !shot || !shot.clip || !out || !out.name) return;
    try {
      const res = await api.fetchApi("/cglide/chain/adopt_film", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, id: shot.clip,
          filename: out.name, subfolder: out.subfolder || "",
          /* 本次画布随成片一起记进版本参数（take_params，20261010 版本绑
             参数）：切版本时面板跟着回滚，潜空间续写认段也靠它。 */
          w: (shot.state && shot.state.width) || 0,
          h: (shot.state && shot.state.height) || 0,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (e) {
      console.warn("[H3 Studio] 成片没能收进档案（不影响出图）：", e);
    }
  }

  /* 段名写进档案。时间轴读的是档案（film_summary 的 name 取自段落元数据），
     只改前端的话轴上永远显示「第 N 段」，看起来就像改名没生效。 */
  async function pushShotName(shot) {
    const name = ui.castName();
    if (!name || !shot) return;
    /* 还没登记过（没有 clip id）：名字会随登记那一次一起写进去，不必另发一次。 */
    if (!shot.clip) return;
    try {
      const res = await api.fetchApi("/cglide/chain/update", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, id: shot.clip,
          patch: { name: shot.name || "", shot: shot.id },
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await ui.monitor.refresh();
    } catch (e) {
      console.warn("[H3 Studio] 段名没能写进档案（不影响出图）：", e);
    }
  }

  /* （segFiles / confirmDeleteSegment 已删，20261009 用户定：点删除就删，
     不要任何弹窗。后端 /chain/clip_files 这条只读接口仍在 —— 它把"这一段
     占着哪些文件"这件事说清楚，将来要做删除预览或审计时直接用它，不必重写。）

  /* 把一段从档案里删掉。force = 真删（含磁盘文件）。
     权限在后端：不带 force 时它仍然拒绝删有成片的段（累积成片链的理由）。
     导演台的「删除本段」走 force=true —— 用户已经明确"删除 = 到处都删"，
     且不弹确认（改由事后一条非阻塞提示 + 工程面板「还原」兜底）。 */
  async function unregisterShot(shot, force) {
    const name = ui.castName();
    if (!name || !shot) return null;
    /* clip id 还没拿到（登记失败 / 工程被还原过）就退回分段 uid：
       后端按 meta.shot 反查档案条目，孤儿段不再删不掉。 */
    const cid = shot.clip || shot.id;
    if (!cid) return null;
    try {
      const res = await api.fetchApi("/cglide/chain/remove_clip", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, id: cid, force: !!force }),
      });
      const data = await res.json().catch(() => ({}));
      if (data.removed) await ui.monitor.refresh();
      else if (data.reason) lanesFlash(`没删掉：${data.reason}`);
      /* 文件没删干净的（被占用等）要说出来 —— 静默留一堆孤儿，下次备份就带上了 */
      if (data.failed && data.failed.length) {
        lanesFlash(`档案条目已删，但有 ${data.failed.length} 个文件没删掉：`
          + data.failed.join("、"));
      } else if (data.note) {
        lanesFlash(data.note);
      }
      return data;
    } catch (e) {
      console.warn("[H3 Studio] 分段没能从档案里删掉：", e);
      lanesFlash(`删除档案条目失败：${(e && e.message) ? e.message : e}`);
      return null;
    }
  }

  /** 在轴下方那行手势提示的位置临时说一句话。
      选这里而不是弹窗：它就在轴的正下方，说的正是轴上的事，看完自动收回，
      不打断手上正在做的事。 */
  function lanesFlash(msg) {
    /* 写到始终可见的反馈条（gcast-flash），不再藏进 <details> 手势提示里 ——
       后者默认收起，提示全不可见，"点了没反应"多半是它造成的。 */
    const h = ui.flashBar;
    if (!h) return;
    h.textContent = msg;
    h.title = msg;          /* 轴头那格过窄时会收省略号，全文悬停可看 */
    h.classList.add("on");
    /* 反馈条出现/收起都会改面板内容高度，顺手把画布标脏，让前端立刻同步
       DOM widget 布局 —— 不然这条提示自己也要等下一次点击才露面。 */
    try { ui.node.setDirtyCanvas(true, true); } catch { /* 已销毁 */ }
    /* 不再 5 秒自动消失：提示写完就常驻，直到下一条提示把它顶掉。
       自动消失是「啥都没显示」的根因 —— 做完操作没盯着轴头看，它已经
       缩回去了，用户永远只看到空档。反正在轴头只占一行、不挡任何东西，
       常驻没有代价。 */
  }

  /* 段级操作的归宿。原来这些散在「工程分段条」的三个事件监听里（双击改名、
     右键换色、Alt+单击跳过），跟那条轴绑死。第二条轴撤掉之后它们必须有个
     新家 —— 就收在这里，由监视器段落块的双击 / 右键调起，同时工程列表里的
     那些入口也走同一批函数，不会出现两处行为不一致。 */

  /** 就地改名。hostEl 是承载输入框的那一块（监视器的段落块或工程条的分段块）。 */
  function renameShot(i, hostEl) {
    const p = ui.proj();
    const s = p.shots[i];
    if (!s || !hostEl) return;
    const d = el("input");
    d.value = s.name || "";
    d.placeholder = `分段 ${i + 1}`;
    /* 输入框落在段块**顶部的色条**里（编号/名字那一条）—— 整块换成输入框
       会把「待生成」占位和缩略图一起清掉，色条才是放名字的地方。宿主传进
       来的可能是整块，先找到色条再替换；传进来就是色条则原样用。 */
    const hd = hostEl.querySelector ? hostEl.querySelector(".bzf-seg-hd") : null;
    (hd || hostEl).replaceChildren(d);
    d.focus(); d.select();
    /* 强制这一段重画（20261010）：paintSeg 有签名闸门 —— 名字没变时签名
       不变、色条不恢复，输入框就一直留在原地，看起来像"回车没反应"。
       清掉签名让下一次重绘把色条画回来。 */
    const forceRepaint = () => {
      if (hostEl.__bzfSig !== undefined) delete hostEl.__bzfSig;
      void ui.monitor?.refresh?.();
    };
    const done = () => {
      s.name = d.value.trim();
      ui.commit(); ui.paintSegTools();
      forceRepaint();
      /* 名字也要写进档案：时间轴画的**是档案里的段**，档案里没有 name，轴上就
         永远显示「第 N 段」—— 双击改完名，轴上没变，用户只会以为改名失效。 */
      void pushShotName(s);
    };
    const cancel = () => forceRepaint();
    d.onclick = (ev) => ev.stopPropagation();
    d.onblur = done;
    d.onkeydown = (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") { ev.preventDefault(); done(); }
      if (ev.key === "Escape") { ev.preventDefault(); cancel(); }
    };
  }

  /* 续接窗口（= 段间过渡帧数）的合法格点是 H3 的 **17k+5**：
     5, 22, 39, 56, 73, 90, 107, 124, …
     两个档位（文件 / 潜空间）走的是同一串数：文件档 snap_guide_run 向下取
     17k+5，潜空间档 _MC_RUN_GRID 是 (124,107,90,73,56,39,22,5,1) —— 重合，
     所以这里一份就够。
     窗口越长 → 连贯性越强、显存越大、越慢；而且这一段被钉住的帧越多，
     重抽时能自由改动的范围就越小。这是取舍，不是越大越好。 */
  const GUIDE_GRID_STEP = 17;
  /* 自由输入 → 格点。**向下取**（与后端 snap_guide_run 一致）：取最近会把
     用户填的数抬上去，后端再往下吸一次，填的和生效的就差了 17 帧 ——
     这种"差一点"最难自查。0 单独放行，它表示"不承接上一段"。 */
  function snapGuideFrames(n) {
    n = Math.round(Number(n) || 0);
    if (n <= 0) return 0;
    if (n <= 5) return 5;
    return Math.floor((n - 5) / GUIDE_GRID_STEP) * GUIDE_GRID_STEP + 5;
  }

  /* 重抽档位 / 锚点选法的可选值与说明定义在模块级（REDO_MODES / REDO_ANCHORS）
     —— 白名单 parseInitial 也要拿它做校验，而那是模块作用域的函数。
     这一组原本只长在画布上的「🔒 H3 钉帧遮罩」节点里；搬回段级菜单的理由：
     档位是「登记这一刻」决定的，导演台运行时就把它写进段落档案，节点只是照
     档案执行 —— 选择权理应在段上，而不是为了改一个数去画布上找节点。 */

  /** 改一段的时长。时间轴拖动与段菜单「直接改帧数」共用同一条后端路由 ——
      档案落盘、下一段的待办标记、面板时长同步都发生在那里；两处各走一条路
      迟早会不一致。 */
  async function applyFrames(clipId, index, frames) {
    const name = ui.castName();
    if (!name) throw new Error("还没有片名 —— 先在「工程」面板里填一个");
    const p = ui.proj();
    const res = await api.fetchApi("/cglide/chain/resize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, id: clipId, frames }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    /* 改的是**当前段** → 吸附后的帧数要回写面板状态并重画右上「这一段」卡：
       档案已改而面板还显示旧时长，用户会以为没改成功。按「段 id 精确对号」或
       「下标对号」任一命中即算当前段（p.idx 在工程 JSON 里可能是字符串，
       === 比下标会永远失配）。非当前段不用动：切段时各自从档案读回。 */
    const idx = Number(p.idx);
    const curShot = p.shots && p.shots[p.idx];
    const isCur = (curShot && curShot.clip === clipId)
      || (idx >= 0 && index === idx);
    /* 后端"长度没变"（changed:false）时不带 to 字段 —— 但请求值本身就是
       吸附后的档案值，照样能用来对齐面板（不然"面板 90、档案 243、又拖回
       243"这种局面面板永远追不上，下次渲染还是会拿 90 覆盖档案）。 */
    const finalTo = (typeof data.to === "number") ? data.to
      : ((data.changed === false) ? Math.round(Number(frames) || 0) : null);
    if (isCur && finalTo) {
      ui.st.length = finalTo;
      ui.commit();
      ui.render();
    }
    /* ⚠ 时间轴拖的**任何一段**都必须同步进它自己的面板预设（shot.state.length）
       —— 20261013「拖成 10 秒、全部渲染后弹回 3.9/7.8/7.8」的根因：
       渲染登记时后端拿**面板预设**（h3_data 的 length）覆盖档案 frames；
       以前只改档案不改面板，凡是拖的时候不是当前段的，面板里还是旧帧数，
       下一次渲染就把拖好的时长**静默覆盖回去**。两边写齐，冲突不再产生
       （syncCheck 弹窗从"每次渲染都要对账"退回纯保险）。 */
    if (finalTo && Array.isArray(p.shots)) {
      const owner = p.shots.find((s) => s && s.clip === clipId);
      if (owner && owner.state && owner.state.length !== finalTo) {
        owner.state.length = finalTo;
        ui.commit();
      }
    }
    /* 加长但这一段没有「📦 段落存档」原稿：20261013 起后端**不再拒绝**，而是
       标成整段重抽（redo）。改成了就必须把代价说清 —— 否则用户拉长了却不知道
       后面一段也要跟着重跑，等渲染完才发现全片变了。 */
    if (data.no_stock) {
      lanesFlash(`第 ${index + 1} 段已加长到 ${data.to} 帧（要整段重抽）：`
        + (data.reason || ""));
    } else if (data.refused) {
      /* 仍保留被拒通道（后端将来若再加硬拦，这里不至于静默）。 */
      lanesFlash(`第 ${index + 1} 段不能加长：${data.reason || ""}`);
    }
    return data;
  }

  /** 拆分一段：把第 i 段（N 帧 = 段自身总帧，含段首承接上段的重叠）拆成两段，
      前段保留、后段续接前段（OVERLAP 帧承接），时间轴新画面守恒
      （(a-ov) + (N-a) = N-ov）。拆完两段都要重抽。
      atNetFrame（可选）= 裁切剃刀点对应的**段自身帧**切点（含段首承接重叠）：
        · 给了 → 按切点拆（吸附 17k+5 网格，像剪辑软件的剃刀工具）；
        · 不给 → 对半兜底（其它入口）。
      20261014 根治"只留下一半"：克隆前段 state 必须 detachChain 断开段身份 ——
      否则前段 state.chain.clip 被带过来，load→commit 把前段 clip id 回写新段，
      registerShot 的"已登记"守卫就误判跳过 add_clip，新段永不登记、又被幽灵
      清理当撞号删掉（现场只剩被改短的前段）。同时补上 await 登记 + 顺序重排。 */
  async function splitShotAt(i, atNetFrame) {
    const p = ui.proj();
    const s = p.shots[i];
    if (!s || !!ui.run) return;
    const own = (s.state = s.state || {});
    /* 拆分重叠 = 新段承接前段的窗口，出厂默认与「接一段 / 插一段」一致（39 帧）。 */
    const OVERLAP = 39;
    const MIN_PART = 5;            // 每段净长至少留 5 帧（后端帧网格最小档）
    const N = snapGuideFrames(Number(own.length) || 0);
    const hasPoint = Number.isFinite(Number(atNetFrame)) && Number(atNetFrame) > 0;
    let a;
    if (hasPoint) {
      /* 裁切剃刀：前段净长 = 切点吸附到 17k+5；后段净长 = N - a。两边都得留够
         MIN_PART，否则这一刀会拆出空段（没有意义）。 */
      a = snapGuideFrames(Number(atNetFrame));
      if (a < MIN_PART) {
        lanesFlash(`切点太靠近段首 —— 至少给前段留 ${MIN_PART} 帧`);
        return;
      }
      if (N - a < MIN_PART) {
        lanesFlash(`切点太靠近段尾 —— 至少给后段留 ${MIN_PART} 帧`);
        return;
      }
    } else {
      /* 兜底对半：段太短对半会拆出残段。 */
      if (N < OVERLAP + 10) {
        lanesFlash(`这一段太短（< ${OVERLAP + 10} 帧），拆不成两段`);
        return;
      }
      a = snapGuideFrames(Math.round(N / 2));
    }
    /* 后段总帧 b = 剩余净长 + 承接重叠。a / OVERLAP / N 都落在 17k+5 网格，
       故 b 自动落网格且 b - OVERLAP = N - a（帧账守恒）。拆完可拖段边缘微调。 */
    const b = snapGuideFrames(N - a + OVERLAP);
    if (a < MIN_PART || (b - OVERLAP) < MIN_PART) {
      lanesFlash("切点不合法 —— 两段都得留至少 5 帧");
      return;
    }

    ui.snapProject(`拆分前 第 ${i + 1} 段`);
    /* 后段 = 克隆前段（班底/画布/提示词照抄），但**必须断开段身份**：
       detachChain 把 chain.clip/shot 清空，新段才是一张白纸，后端才会发新 clip
       id（顶着前段 clip 去登记会覆盖前段，且被前端守卫判成"已登记"而跳过）。 */
    const tailState = ui.detachChain(JSON.parse(JSON.stringify(own)));
    tailState.length = b;
    /* 后段默认潜空间续接前段 OVERLAP 帧；清掉文件/旧续拍存档身份（cast_run 会按
       段序重新认领该接的那版存档），避免把前段的来源也照抄过来。 */
    tailState.cont = Object.assign({}, tailState.cont, { mode: "latent" });
    delete tailState.cont.file;
    delete tailState.cont.latent_file;
    tailState.cont.frames = OVERLAP;
    const tail = { id: ui.uid(), name: "", state: tailState };
    if (s.rack) tail.rack = JSON.parse(JSON.stringify(s.rack));
    own.length = a;                            // 前段改时长
    p.shots.splice(i + 1, 0, tail);
    p.idx = i + 1;
    ui.restoreShotRack(tail);
    ui.load(JSON.stringify(tailState));
    ui.commit();
    ui.paintSegTools();
    lanesFlash(`已拆成第 ${i + 1} 段（${a} 帧）+ 第 ${i + 2} 段（${b} 帧，承接 ${OVERLAP} 帧）`
      + " —— 两段都要重抽，正在登记…");
    /* 落库：前段改时长；后段 await 登记为新段；再按工程顺序对齐档案。
       登记/重排失败要明确报错，不能再"点了像没反应、最后只剩一半"。 */
    try {
      if (s.clip) {
        await applyFrames(s.clip, i, a);
      }
      await registerShot(tail);
      /* add_clip 恒追加在档案末尾；拆中间段时新段必须按工程顺序重排一次，
         轴上的段序才与工程一致（拆最后段也调一次，幂等无副作用）。 */
      await syncArchiveOrder();
      ui.paintSegTools();
    } catch (e) {
      console.error("[H3 Studio] 拆分登记失败：", e);
      lanesFlash("拆分登记失败：" + (e && e.message ? e.message : e)
        + "（两段已在工程里，可用工程面板「撤销」退回）");
    }
  }

  /** 把菜单挂到页面上，并挂上「点到别处就收起」。两段菜单（正常段 / 档案孤儿段）共用。 */
  function openMenu(menu, x, y) {
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    document.body.append(menu);
    const rm = (ev) => {
      if (!menu.contains(ev.target)) {
        menu.remove();
        document.removeEventListener("pointerdown", rm, true);
      }
    };
    setTimeout(() => document.addEventListener("pointerdown", rm, true), 0);
    return menu;
  }

  /** 段落右键菜单：这一段能做的所有事都在这里。
      下标来自段块的段号减一（档案的 index 从 1 起，工程 shots 从 0 起）。
      工程、档案两边的段**按 clip id 对号**：对得上就出完整菜单；对不上
      （只存在于档案里的孤儿段）先**认领**回工程再出菜单 —— 块在轴上就该
      能就地编辑，认领后改名 / 续接帧数 / 复制 / 悬停工具条全部可用。 */
  /* ================= 接缝面板（20261011 · 方案 B）=================
     把原右键 pinGroup（接法）+ 断链 + 时间轴转场卡（承接帧数）收敛到右侧
     「这一段」卡的接缝组，成为唯一入口、随选中段刷新。
       读：/pins_state（接法 + 窗口）、p.transitions（左接缝播放）；
       写：/pins（接法 + 窗口）、applyTransition（左接缝播放）。
     不设任何快捷键（避免与 ComfyUI 画布冲突）。 */
  /* 接法语义（20261012 根治：与重抽档位彻底解耦）：
     · source 改名为「自动接续」—— pins 清空，后端按时间轴顺序自动承接上一段
       尾部（csglide_chain.head_parent 的 fallback_prev），普通续片就用它，
       不再清零 cont.frames；
     · 真正"与上段断开"由行尾「连播」开关（unlink）承担，语义唯一；
     · 重抽档位（完整重拍等）只管"钉不钉本段旧版"，不再禁用/篡改这些按钮。 */
  const SEAM_MODES = [
    ["source", "自动接续", "默认：按时间轴顺序承接上一段的尾巴（普通续片用这个）。\n"
      + "想和上段彻底断开，用行尾的「连播」开关断链，不要用这里。"],
    ["extend", "往后接", "本段开头钉住上一段的尾巴，接着往后演（与自动接续同方向，可显式指定源段/帧数）"],
    ["prepend", "往前接", "本段末尾钉住下一段的头，演完正好接上（本段头部不承接，头部帧数不适用）"],
    ["bridge", "补桥", "头尾各钉一段，中间留给模型填 —— 重做中间那一截"],
    ["loop", "循环", "头钉最后一段尾、尾钉第一段头，首尾相接"],
  ];
  const SEAM_STEPS = [0, 5, 22, 39, 56, 73, 90, 107, 124];
  const SEAM_WIN_CHIPS = [5, 22, 39, 56];
  let lastSeam = { i: -1, mode: "source", win: 22, leftBroken: false };

  const stepUp = (n) => { for (const s of SEAM_STEPS) if (s > n) return s; return n + 17; };
  const stepDown = (n) => { for (let k = SEAM_STEPS.length - 1; k >= 0; k--)
    if (SEAM_STEPS[k] < n) return SEAM_STEPS[k]; return 0; };

  async function readPinsInfo(name, cid) {
    const res = await api.fetchApi(
      `/cglide/chain/pins_state?name=${encodeURIComponent(name)}`
      + `&id=${encodeURIComponent(cid)}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
    return d;
  }
  async function writePinsInfo(name, cid, mode, win, a, b) {
    const res = await api.fetchApi("/cglide/chain/pins", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, id: cid, mode, src: a || "", src2: b || "", window: win }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.error) throw new Error(d.error || `HTTP ${res.status}`);
    return d;
  }
  /* 写 pins 后把头部承接意图同步进 shots[].state.cont.frames，时间轴菱形
     （contFramesOf）才能立即跟随，不必等生成回写 trim。口径（20261012 根治）：
     head 钉（extend/bridge/loop）= window；prepend 头不承接 = 0；
     source（自动接续）= 保留当前窗口、缺省恢复 39，绝不清零。 */
  function syncShotContFrames(cid, mode, win) {
    const p = ui.proj();
    const j = p.shots.findIndex((sh) => sh.clip === cid);
    if (j < 0) return;
    const hasHead = mode === "extend" || mode === "bridge" || mode === "loop";
    /* 头部承接帧数 cont.frames 的写法（20261012 二次根治：source 也要跟写）：
       · 有头钉（往后接/补桥/循环）：写该接法窗口；
       · 往前接 prepend：头确实不承接 → 0；
       · 自动接续 source：直接写传入窗口。默认 39 的兜底已由 readSeamState 的
         cont.frames 缺省承担，这里照写即可（旧版读旧值 cur 导致改不动）。 */
    let n;
    const w = Math.max(0, Math.round(Number(win) || 0));
    if (hasHead) n = w;
    else if (mode === "prepend") n = 0;
    else {
      n = w;
    }
    const own = (p.shots[j].state = p.shots[j].state || {});
    own.cont = own.cont || {};
    own.cont.frames = n;
    own.cont.mode = own.cont.mode || "latent";
    if (Number(p.idx) === j) ui.st.cont = { ...own.cont };
    ui.commit();
  }

  const seamNote = (t, cls) =>
    el("div", "gcast-seam-note" + (cls ? " " + cls : ""), t);

  /* 读某一段的接缝状态（供右卡面板与右键胶囊弹层共用，避免各写一遍读取逻辑）。
     remote 可选：传入最近一次 POST /pins 的响应（或缓存的 pins_state）时直接用
     它组装，**不再发 GET** —— 胶囊里连点 chips / 接法时每次重绘卡都省一次网络
     往返（20261012 卡顿根治）；不传或传 null 才走网络读取。 */
  async function readSeamState(targetIndex, remote) {
    const p = ui.proj();
    const i = (targetIndex == null) ? p.idx : targetIndex;
    const s = p.shots[i];
    if (!s) return null;
    const cid = s.clip || "";
    const isFirst = i <= 0;
    /* 重抽档位读**目标段自己**的 state（每段独立存档），不再只认当前选中段。
       20261012 根治后它只用于卡片顶部提示，不再禁用任何接缝控件。 */
    const full = ((s.state || {}).redo_mode || "保留动作") === "完整重拍";
    let mode = "source", win = 39, srcA = "", srcB = "", order = [];
    let usedRemote = remote || null;
    if (cid && !usedRemote) {
      try {
        usedRemote = await readPinsInfo(ui.castName(), cid);
      } catch (e) { usedRemote = null; /* 读不出按 source/默认 */ }
    }
    if (usedRemote) {
      const d = usedRemote;
      mode = d.mode || "source"; order = d.order || [];
      const hp = (d.pins || []).find((x) => x.at === "head");
      const tp = (d.pins || []).find((x) => x.at === "tail");
      win = Math.round(Number((hp || tp) && (hp || tp).window) || 39);
      srcA = (hp || {}).from || ""; srcB = (tp || {}).from || "";
    }
    /* effMode = 后端真实接法，完整重拍不再伪装成 source（旧版胶囊里看着像
       "独立"，其实 pins 还在，是最误导人的一处）。 */
    const effMode = mode;
    /* 头部承接帧数：source（自动接续）时读 state.cont.frames（默认续写窗口），
       让用户在不写 pins 的情况下也能调 5/22/39/56。 */
    const contFrames = Math.max(0, Math.round(Number((s.state || {}).cont?.frames)
      || (isFirst ? 0 : 39)));
    if (effMode === "source") win = isFirst ? 0 : (contFrames || 39);
    /* 头部帧数控件什么时候不可用：首段（没有左接缝）；往前接（头不承接）。
       完整重拍不再禁用 —— 它与接缝正交。 */
    const headWinEditable = !isFirst && effMode !== "prepend";
    const pos = order.indexOf(cid);
    const prevC = pos > 0 ? order[pos - 1] : "";
    const nextC = pos >= 0 && pos < order.length - 1 ? order[pos + 1] : "";
    const leftBroken = !!(i > 0 && cid && p.transitions
      && p.transitions[cid] && p.transitions[cid].type === "unlink");
    return { i, cid, full, isFirst, effMode, win, contFrames, headWinEditable,
             srcA, srcB, prevC, nextC, order, leftBroken, _remote: usedRemote };
  }

  /* 胶囊卡「接法 + 连播」（方案A 20261011：接缝全部收进胶囊，上下文条不再有）。
     5 接法 + 连播开关，3 列网格；任一改动后调 rerender() 重绘当前卡。 */
  function buildModeRow(c, rerender) {
    const box = el("div", "gcast-seam-modebox");
    box.append(el("span", "gcast-seam-grouplbl", "接缝接法"));
    /* 完整重拍只影响"重抽时钉不钉本段旧版"，与接缝接法正交（后端承接节点
       根本不读重抽档位）—— 明示一行，杜绝"灰了=不能接"的误读（20261012）。 */
    if (c.full) {
      const tip = el("div", "gcast-seam-note",
        "本段重抽档为「完整重拍」：只表示不钉本段旧版、整段重生成；接缝接法与承接帧数照常可设、照常生效。");
      tip.style.opacity = ".75";
      box.append(tip);
    }
    const name = ui.castName();
    const { cid } = c;
    const prevC = c.prevC, nextC = c.nextC;
    /* 单个接法按钮：常驻「自动接续」与高级「特殊接法」复用同一套创建逻辑。 */
    const mkModeBtn = ([m, label, tip]) => {
      /* 首段没有上一段：需要"头钉上段"的接法无意义，禁用并说明（不是因为
         重抽档位，纯粹因为没有源段）。 */
      const needPrev = (m === "extend" || m === "bridge" || m === "loop");
      const disFirst = c.isFirst && needPrev;
      const b = el("button", "gcast-seam-mbtn" + (c.effMode === m ? " is-on" : ""), label);
      b.type = "button";
      b.title = tip + (disFirst ? "\n（第一段没有上一段可接）"
        : (m === "source" ? "" : "\n承接帧数在下面设置；重抽这一段后生效。"));
      if (disFirst) b.disabled = true;
      b.onclick = async () => {
        if (ui.run) { ui.lanesFlash("正在渲染，等这一轮跑完再改接缝"); return; }
        if (!cid) { ui.lanesFlash("这一段还没登记上轴，先渲染一次"); return; }
        try {
          let a = c.srcA, b2 = c.srcB;
          if (m === "extend") a = a || prevC;
          else if (m === "prepend") b2 = b2 || nextC;
          else if (m === "bridge") { a = a || prevC; b2 = b2 || nextC; }
          const dResp = await writePinsInfo(name, cid, m, c.win, a, b2);
          syncShotContFrames(cid, m, c.win);
          /* 有 head 钉的接法（extend/bridge/loop）左接缝必然连播，自动取消断链 */
          const hasHead = m === "extend" || m === "bridge" || m === "loop";
          const p = ui.proj();
          const nowBroken = !!(p.transitions && p.transitions[cid] && p.transitions[cid].type === "unlink");
          if (hasHead && nowBroken) { try { await ui.applyTransition(cid, { type: "cut", overlap: 0 }); } catch {} }
          /* 20261012 卡顿根治：只局部重画菱形（帧数/断链实时读本地 state），
             不再全量 refresh（拉档案+重建段块+缩略图请求风暴）。POST 响应直接
             喂给卡片重绘，省掉 fill 里的又一次 GET。特殊接法的段块符号
             （disp_pin_mode）由后台静默 refresh 对账，不阻塞点击反馈。 */
          ui.monitor.repaintSeams && ui.monitor.repaintSeams();
          await rerender({ mode: dResp.mode || m, pins: dResp.pins || [] });
          if (m !== "source") { void Promise.resolve(ui.monitor.refresh("store")).catch(() => {}); }
          ui.lanesFlash(m === "source"
            ? "接法：自动接续 —— 按时间轴顺序承接上一段（要彻底断开请用行尾「连播」断链）"
            : `接法：${label}（窗口 ${c.win} 帧）—— 重抽这一段后生效`);
        } catch (e) { ui.lanesFlash(`接法写入失败：${e.message || e}`); }
      };
      return b;
    };
    /* 常驻网格：自动接续（source，SEAM_MODES 首位）+ 连播开关。 */
    const grid = el("div", "gcast-seam-modegrid");
    grid.append(mkModeBtn(SEAM_MODES[0]));
    /* 连播开关（i>0 才有左接缝）。与重抽档位完全无关，
       任何档位下都可切换（旧版完整重拍时置灰没有代码依据）。 */
    if (c.i > 0) {
      const linkBtn = el("span", "gcast-seam-link" + (c.leftBroken ? "" : " on"));
      linkBtn.append(el("span", "tr"), el("span", null, "连播"));
      linkBtn.title = (c.leftBroken ? "当前已断链：点我恢复与上一段连续播放、接续画面"
                                    : "连播中：左接缝连续播放；点我与上一段断链（不接续、不连播）")
        + "\n（断链才是真正的「与上段分开」，与重抽档位无关）";
      linkBtn.onclick = async (e) => {
        e.stopPropagation();
        if (ui.run) { ui.lanesFlash("正在渲染，等跑完再改连播"); return; }
        const willLink = !!c.leftBroken;   /* 当前断→恢复 cut；当前连→断 unlink */
        try {
          /* applyTransition 内部已做 paintLinkMarks + repaintSeams 局部重画
             （20261012 起不再全量 refresh），这里只重绘卡片本身（零 GET，用缓存）。 */
          await ui.applyTransition(cid, willLink ? { type: "cut", overlap: 0 } : { type: "unlink"});
          await rerender();
          ui.lanesFlash(willLink ? "左接缝已恢复连播（自动接续上一段）" : "左接缝已断链 —— 本段与上段彻底分开");
        } catch (err) { ui.lanesFlash("连播切换失败：" + (err && err.message ? err.message : err)); }
      };
      grid.append(linkBtn);
    }
    box.append(grid);
    /* 特殊接法折叠（高级）：往后接 / 往前接 / 补桥 / 循环，低频不占明面。 */
    const adv = el("details", "gcast-seam-adv");
    const advSum = el("summary", null, "特殊接法");
    advSum.addEventListener("click", (e) => e.stopPropagation());
    const advGrid = el("div", "gcast-seam-modegrid");
    SEAM_MODES.slice(1).forEach((row) => advGrid.append(mkModeBtn(row)));
    adv.append(advSum, advGrid);
    box.append(adv);
    return box;
  }

  /* 构建「承接帧数编辑器」：第一行 − 输入 ＋，第二行常用帧数 chips **直接常驻**
     （20261011 用户定：不再藏在 ≡ 弹层里）。由右键标尺胶囊弹出，上下文条不占块。 */
  function buildWinRow(c, rerender) {
    /* 头部承接帧数控件只在两种情况下不可用（20261012 根治）：
       · 首段：没有左接缝；
       · 往前接 prepend：本段头部不承接（它是尾钉后段）。
       完整重拍不再禁用、自动接续(source)下也可直接调（只写 cont.frames，
       不写 pins，普通顺序片的默认续写窗口就是它）。 */
    const winDis = !c.headWinEditable;
    const disReason = c.isFirst ? "第一段没有左接缝，无需承接上一段"
      : (c.effMode === "prepend" ? "往前接：本段头部不承接，头部帧数不适用（尾缝帧数在尾段那边设）"
        : "");
    const winRow = el("div", "gcast-seam-win" + (winDis ? " is-dis" : ""));
    winRow.append(el("span", "gcast-seam-grouplbl", "承接帧数"));
    if (winDis) winRow.title = disReason;
    const line1 = el("div", "gcast-seam-winline");
    const minus = el("button", "gcast-seam-step", "−");
    minus.type = "button"; minus.disabled = winDis || c.win <= 0;
    minus.title = winDis ? disReason : "";
    const winInp = el("input", "gcast-seam-inp");
    winInp.type = "number"; winInp.min = "0"; winInp.max = "4096";
    winInp.value = String(c.win);
    winInp.disabled = winDis;
    winInp.title = winDis ? disReason
      : "头部承接上一段尾部的帧数（潜空间整步：5/22/39/56…）。自动接续下直接生效，不用写接法。";
    const plus = el("button", "gcast-seam-step", "＋");
    plus.type = "button"; plus.disabled = winDis;
    plus.title = winDis ? disReason : "";
    const applyWin = async (raw) => {
      if (ui.run) { ui.lanesFlash("正在渲染，等跑完再改帧数"); return; }
      let n = Math.max(0, Math.min(4096, Math.round(Number(raw) || 0)));
      if (n > 0) n = Math.max(5, 5 + 17 * Math.floor((n - 5) / 17));
      try {
        let respPatch = null;
        if (c.effMode === "source") {
          /* 自动接续：只更新本段默认续写窗口 cont.frames，不碰 pins。
             后端 pins 为空且未断链时按时间轴自动接上段，窗口读的就是它。
             纯本地操作：零 POST、零 GET、零全量刷新（chips 连点也不卡）。 */
          syncShotContFrames(c.cid, "source", n);
        } else {
          let a = c.srcA, b2 = c.srcB;
          if (c.effMode === "extend") a = a || c.prevC;
          else if (c.effMode === "prepend") b2 = b2 || c.nextC;
          else if (c.effMode === "bridge") { a = a || c.prevC; b2 = b2 || c.nextC; }
          const dResp = await writePinsInfo(ui.castName(), c.cid, c.effMode, n, a, b2);
          syncShotContFrames(c.cid, c.effMode, n);
          respPatch = { mode: dResp.mode || c.effMode, pins: dResp.pins || [] };
        }
        /* 菱形帧数实时读 contFramesOf（本地 state），局部重画即可 —— 旧版每次
           点 −/＋/chips 都全量 refresh，连点几下就卡几秒（20261012 根治）。 */
        ui.monitor.repaintSeams && ui.monitor.repaintSeams();
        if (rerender) await rerender(respPatch);
        else { const wm = winRow.closest(".gcast-seam-winmenu"); if (wm) wm.remove(); }
        /* 特殊接法改了 pins 窗口：段块符号后台静默对账，不阻塞点击。 */
        if (respPatch) { void Promise.resolve(ui.monitor.refresh("store")).catch(() => {}); }
      } catch (e) { ui.lanesFlash(`承接帧数写入失败：${e.message || e}`); }
    };
    minus.onclick = () => applyWin(stepDown(c.win));
    plus.onclick = () => applyWin(stepUp(c.win));
    winInp.onkeydown = (e) => e.stopPropagation();
    winInp.onchange = () => applyWin(winInp.value);
    line1.append(minus, winInp, plus);
    /* 常用承接帧数：直接一行常驻，点哪档设哪档，当前档高亮 */
    const chipRow = el("div", "gcast-seam-chips");
    SEAM_WIN_CHIPS.forEach((n) => {
      const b = el("button", "gcast-seam-chip", String(n));
      b.type = "button"; b.disabled = winDis;
      if (winDis) b.title = disReason;
      if (n === c.win && !winDis) b.classList.add("is-on");
      b.onclick = (e) => { e.stopPropagation(); applyWin(n); };
      chipRow.append(b);
    });
    winRow.append(line1, chipRow);
    return winRow;
  }

  /* 右键标尺接缝胶囊：在鼠标处弹帧数编辑小卡（−框＋≡） */
  ui.openSeamWindow = async (targetIndex, ev) => {
    /* 再点同一段的胶囊 = 收回（toggle）。先找有没有同一段的卡开着。 */
    const same = Array.from(document.querySelectorAll(".gcast-seam-winmenu"))
      .find((m) => m.dataset.seamIndex === String(targetIndex));
    if (same) {
      if (same._stopFollow) same._stopFollow();
      same.remove();
      return;
    }
    const c = await readSeamState(targetIndex);
    if (!c) { ui.lanesFlash("这一段还没登记上轴，先渲染一次"); return; }
    /* 关旧卡（含停掉它的跟随 rAF）—— 上面没命中同段，说明是别的段，关掉再开新段 */
    document.querySelectorAll(".gcast-seam-winmenu").forEach((m) => {
      if (m._stopFollow) m._stopFollow();
      m.remove();
    });
    /* 接缝 q 只用来跟随定位，卡**不再作为 q 子节点**：band overflow:hidden 会把
       向下展开的卡底部（承接帧 −框＋/chips）裁掉。卡改挂 overflow visible 的
       .gcast-lanes，用 rAF 每帧对齐 q 视觉位置 —— 横向滚动 / 缩放 / refresh 天然
       跟随，不再 fixed 一次性定位（旧法一缩放就和胶囊错开、跑偏）。 */
    let q = null;
    if (c.cid) {
      q = Array.from(document.querySelectorAll(".bzf-rseam"))
        .find((x) => x.dataset.clipId === c.cid);
    }
    const lanes = document.querySelector(".gcast-lanes");
    const host = lanes || document.body;
    const menu = el("div", "gcast-seam-winmenu gcast-popmenu is-open");
    menu.dataset.seamIndex = String(targetIndex);  // 供「再点同段收回」的 toggle 判断
    /* 任一接法 / 连播 / 帧数改动后重绘整张卡（卡保持打开、即时反馈）。
       20261012 卡顿根治：缓存打开时读到的 pins_state，交互后用 POST 响应增量
       合并（patch），重绘**零 GET** —— 旧版每次重绘都再发一次 pins_state 请求，
       连点 chips 时网络往返串行排队，卡明显迟钝。段序 order 在卡开着期间不会变
       （重排 / 换工程会先关卡），缓存安全。 */
    let cachedRemote = c._remote || null;
    const fill = async (patch) => {
      if (patch) {
        cachedRemote = Object.assign({},
          cachedRemote || { mode: "source", pins: [], order: c.order || [] }, patch);
        if (!(cachedRemote.order || []).length) cachedRemote.order = c.order || [];
      }
      const cc = await readSeamState(c.i, cachedRemote);
      if (!cc) return;
      cachedRemote = cc._remote || cachedRemote;
      menu.replaceChildren(buildModeRow(cc, fill), el("hr", "gcast-seam-vsep"), buildWinRow(cc, fill));
      rememberSeam(cc);
    };
    menu.append(buildModeRow(c, fill), el("hr", "gcast-seam-vsep"), buildWinRow(c, fill));
    rememberSeam(c);
    host.appendChild(menu);

    let raf = 0, alive = true;
    /* 卡贴齐视觉菱形（rs-d）底部，而非 q 热区底部 —— 热区向下扩了透明区，
       用 q.bottom 会让卡离胶囊太远。marker 惰性缓存：paintSeams 重画 q.innerHTML
       会换掉旧菱形节点，失效（isConnected=false）时重查一次。 */
    let marker = q ? (q.querySelector(".rs-d") || q) : null;
    let lastLeft = null, lastTop = null;
    const place = () => {
      if (!q || !q.isConnected) return false;
      if (!marker || !marker.isConnected) marker = q.querySelector(".rs-d") || q;
      const mb = marker.getBoundingClientRect();
      let left, top;
      if (lanes) {
        const lb = lanes.getBoundingClientRect(), qb = q.getBoundingClientRect();
        /* lanes 在 ComfyUI 画布内被 transform scale：getBoundingClientRect 是缩放后
           视觉值，而 absolute 的 style top/left 是布局 px —— 必须除以 scale，否则
           画布一缩放卡就偏下，且缩放越大偏得越多（旧法卡离胶囊远的根因）。 */
        const scale = lanes.offsetWidth ? lb.width / lanes.offsetWidth : 1;
        left = `${(qb.left + qb.width / 2 - lb.left) / scale}px`;
        top = `${(mb.bottom - lb.top) / scale + 6}px`;
      } else {
        const qb = q.getBoundingClientRect();
        left = `${qb.left + qb.width / 2}px`;
        top = `${mb.bottom + 6}px`;
      }
      /* 只在位置真的变了才写 style：每帧无条件写 left/top 会每帧触发 reflow，
         卡静止不动也一直强制重排 —— 这是「弹窗很卡」的主因。 */
      if (left !== lastLeft || top !== lastTop) {
        menu.style.left = left;
        menu.style.top = top;
        lastLeft = left;
        lastTop = top;
      }
      return true;
    };
    if (lanes) menu.style.transform = "translateX(-50%)";
    else { menu.style.position = "fixed"; menu.style.transform = "translateX(-50%)"; }
    /* 挂载后先同步对齐一次，避免卡在左上角闪一帧。 */
    try { place(); } catch { /* 首帧 rAF 兜底 */ }
    /* 跟随轮询降频到 ~30fps（每 2 帧对齐一次）：卡是静态弹层，30fps 跟随缩放/
       滚动肉眼无差，布局读取（getBoundingClientRect）与 rAF 空转开销直接减半 ——
       卡开着期间旧版 60fps 每帧 3 次 rect 读取，时间轴本身在动画时会与它互相
       触发重排，是「卡开着整个节点发沉」的来源之一（20261012 根治）。 */
    let tick = 0;
    const loop = () => {
      if (!alive) return;
      tick++;
      if ((tick & 1) === 0) {
        if (!place()) { alive = false; menu.remove(); return; }
      }
      raf = requestAnimationFrame(loop);
    };
    menu._stopFollow = () => { alive = false; cancelAnimationFrame(raf); };
    raf = requestAnimationFrame(loop);
  };

  async function refreshSeamPanel() {
    /* 点弹层外部即关闭（只绑一次）。必须在下面 slot 早返回之前绑定 —— 方案A 后
       seamCtl 已删、slot 恒 null，若放在 return 之后，胶囊卡会因监听没注册而收不回去。 */
    if (!refreshSeamPanel._outsideBound) {
      refreshSeamPanel._outsideBound = true;
      /* 捕获阶段（capture=true）：v2 画布对 pointerdown 做了 stopPropagation，
         冒泡阶段的 document 监听收不到画布内的点击 → 弹层点外部收不回。 */
      document.addEventListener("pointerdown", (e) => {
        document.querySelectorAll(".gcast-popmenu.is-open").forEach((m) => {
          if (m.classList.contains("gcast-seam-winmenu")) {
            /* 帧数卡是一次性弹层：点外部停跟随并移除（只去 class 元素仍在、会常驻） */
            if (!m.contains(e.target)) {
              if (m._stopFollow) m._stopFollow();
              m.remove();
            }
          } else if (!m.contains(e.target) && !e.target.closest(".gcast-redo-adv")) {
            /* 「高级 ▾」弹层：点弹层外、且不是点「高级 ▾」按钮（按钮归 click toggle）才关 */
            m.classList.remove("is-open");
          }
        });
      }, true);
    }
    /* 方案A（20261011）：接缝 UI 已全部收进右键菱形胶囊卡（ui.openSeamWindow），
       上下文条不再有 seamCtl（全仓无赋值点，恒为空）。这里只负责：
         ① 胶囊卡"点外部关闭"的一次性监听（必须在早返回之前绑定）；
         ② 刷新主按钮计划摘要 updatePlanNote。
       20261012 根治：旧的 slot 非空分支（内含第二处"完整重拍自动降重画档"
       联动）整段删除 —— 它恒不执行，却和胶囊卡各维护一套接缝状态，是规则
       漂移的根源。接缝状态唯一读取入口 = readSeamState，唯一 UI = buildModeRow
       / buildWinRow。 */
    updatePlanNote();
  }

  /* 胶囊卡每次重绘后把读到的真实接缝状态同步给计划摘要（否则 lastSeam 永远
     停在初值，主按钮 tooltip 只能瞎猜）。 */
  function rememberSeam(c) {
    lastSeam = { i: c.i, mode: c.effMode, win: c.win, leftBroken: c.leftBroken };
    updatePlanNote();
  }

  /* 菱形左键（方案A 20261011）：选中接缝右侧那段、并直接打开该接缝胶囊卡
     （接缝全部收进胶囊，不再定位上下文条）。 */
  function focusSeamPanel(segIndex1based) {
    const i = Math.max(0, (parseInt(segIndex1based, 10) || 1) - 1);
    const p = ui.proj();
    if (!p.shots[i]) return;
    if (p.idx !== i) ui.switchTo(i);
    /* switchTo 是同步面板重绘；胶囊挂在总览轴 .gcast-lanes、定位用的菱形也在
       轴上（用户刚点的它，必然已在 DOM），不依赖切段重绘完成 —— 直接打开，
       去掉旧版 120ms 硬延迟（左键菱形「发闷、慢半拍」的直接来源，20261012）。 */
    void ui.openSeamWindow(i, null);
  }

  /* 计划摘要 + 主按钮文案（随选中段 / 重抽档位刷新）。 */
  function updatePlanNote() {
    const p = ui.proj();
    const i = p.idx;
    const redo = ui.st.redo_mode || "保留动作";
    const full = redo === "完整重拍";
    let plan = "";
    if (i >= 0 && p.shots[i]) {
      const s0 = p.shots[i];
      const cid0 = s0.clip || "";
      const leftBroken = !!(i > 0 && cid0 && p.transitions
        && p.transitions[cid0] && p.transitions[cid0].type === "unlink");
      const contFrames = Math.max(0, Math.round(Number((s0.state || {}).cont?.frames)
        || (i > 0 ? 39 : 0)));
      let seamTxt;
      if (i === 0) {
        seamTxt = "首段 · 无左接缝";
      } else if (leftBroken) {
        seamTxt = "与上段断链 · 不接续画面";
      } else if (lastSeam.i === i && lastSeam.mode && lastSeam.mode !== "source") {
        const label = (SEAM_MODES.find((m) => m[0] === lastSeam.mode) || [, lastSeam.mode])[1];
        seamTxt = `${label} · 承接 ${lastSeam.win} 帧`;
      } else {
        /* pins 为空且未断链 = 后端按时间轴自动接上段（head_parent fallback），
           承接窗口读 cont.frames —— 旧版这里误显示"独立·不承接"。 */
        seamTxt = `自动接上段 · 承接 ${contFrames || 39} 帧`;
      }
      /* 完整重拍只管"钉不钉本段旧版"，接缝状态独立成立，不再插"硬切"暗示。 */
      plan = `以「${redo}」重抽第 ${i + 1} 段 · ${seamTxt} · 仅渲染第 ${i + 1} 段`;
    }
    /* E1：计划摘要不再常驻（收进主按钮 tooltip），但仍兼容可能存在的 planNote */
    const box = ui.planNote;
    if (box) {
      box.textContent = plan;
      box.classList.toggle("show", !!plan);
    }
    const btn = ui.btnRedoClip;
    if (btn) {
      const goTx = btn.querySelector(".gcast-go-tx");
      if (goTx) goTx.textContent = full ? "完整重拍这一段" : "重抽这一段";
      btn.title = plan || "重抽当前这一段（仅渲染这一段）";
    }
  }

  async function redoCurrentClip() {
    if (ui.run) return;
    const p = ui.proj();
    const i = p.idx;
    if (i < 0 || !p.shots[i]) { ui.lanesFlash("先选中要重抽的分段"); return; }
    ui.commit();
    ui.lanesFlash(`开始重抽第 ${i + 1} 段…`);
    await ui.renderAll("continue", "clip");
  }

  async function shotMenu(i0, x, y, clipRow) {
    /* async 函数里的同步错误会变成 rejected promise，不会往外抛 —— fire-and-
       forget 的调用方（onSegMenu）根本看不到，表现就是"右键没反应"。整段包
       try-catch，任何一步炸掉都把原因写进轴头反馈条，而不是静默吞掉。 */
    try {
    const p = ui.proj();
    let i = i0;
    let s = p.shots[i];
    if (!s && clipRow && clipRow.id) {
      const j = p.shots.findIndex((sh) => sh.clip === clipRow.id);
      if (j >= 0) { i = j; s = p.shots[j]; }
    }
    if (!s && clipRow && clipRow.id) {
      /* 只在档案里的段：先**认领**回工程再出完整菜单（20261010 用户定：块在
         轴上就该能就地编辑）。认领失败也继续往下走（findIndex 找不到就
         自然 return），但 claimShot 自己会把失败原因写进反馈条。 */
      ui.claimShot(clipRow);
      i = p.shots.findIndex((sh) => sh.clip === clipRow.id);
      s = p.shots[i];
    }
    if (!s) return;
    const menu = el("div", "gcast-menu");
    menu.dataset.mode = ui.st.mode;
    const item = (label, title, fn, disabled) => {
      const b = el("button", null, label);
      b.title = title || "";
      if (disabled) { b.disabled = true; b.style.opacity = ".4"; }
      else b.onclick = () => { menu.remove(); fn(); };
      menu.append(b);
      return b;
    };
    const sep = () => menu.append(el("div", "gcast-msep"));

    const anchor = ui.menuAnchorFor(i);
    item("✎ 改名…",
      anchor ? "给这一段起个名字"
             : "监视器收起时改不了名（输入框要在段落块上就地展开）—— 先展开监视器",
      () => renameShot(i, anchor), !anchor);
    item(s.off ? "◉ 恢复参与渲染" : "○ 跳过这一段",
      s.off ? "让它重新参与渲染" : "渲染时越过这一段（保留配置，可随时恢复）",
      () => ui.toggleSkip(i));
    sep();
    /* 20261011 菜单精简：断链、接法 / 承接帧数（原 pinGroup）已统一收进右侧
       「这一段」卡的接缝组；「只渲染这一段」由该卡卡底主按钮「重抽这一段」
       承担 —— 右键不再重复这些入口，只留段级管理动作。 */
    item(clipRow && clipRow.locked ? "🔓 解锁这一段" : "🔒 锁定这一段",
      "锁定 = 把编辑目标钉在这一段：再点别的段，文案台与媒体架继续写这一段。"
      + "重抽大段内容时用它防手滑。",
      () => ui.toggleSegLock(clipRow), !clipRow);
    item("⧉ 复制这一段到末尾",
      "照这一段现在的配置在片尾**完整克隆**一段（提示词全文 / 参考图 / 画布 / "
      + "时长 / 全部生成参数照抄），方便同一套参数连拍几条",
      () => { void ui.duplicateShot(i); }, !!ui.run);
    /* 「✂ 拆分这一段」右键入口已撤（20261014 用户定）：改用时间轴工具栏
       「✂ 裁切」剃刀工具 —— 点一下进入裁切模式，再在段块上想切开的位置点一下，
       所见即所得，比右键对半拆更接近剪辑软件。 */
    sep();
    /* 引导一句：接缝（接法 · 承接帧数 · 断链）与钉帧都在右侧「这一段」卡里。 */
    const seamHint = el("div", "gcast-msub");
    seamHint.textContent = "接缝（接法 · 承接帧 · 断链）与钉帧在右侧「这一段」卡里";
    seamHint.style.cssText = "font-size:11px;color:var(--h3-dim);padding:3px 4px";
    menu.append(seamHint);

    /* ---- 版本（takes）----
       这一段抽过几次。切版本就是「换一个镜头」：只要新版的尾部窗口与旧版
       一致，下一段就不用重跑 —— 这是「换一段不牵连后面」的物理判据。
       版本清单以磁盘为准，要问后端，所以是异步的：这里**先占好位置**
       （空壳，拉不到就整组撤掉），否则异步补进来的组会掉到菜单最底下。 */
    const takeSlot = el("div", "gcast-msub");
    menu.append(takeSlot);
    ui.paintTakeGroup(takeSlot, clipRow);
    sep();
    /* 「🗑 删除这一段」已从右键菜单撤掉（20261009 用户定）：轴上那一行已经有
       一颗「删除本段」，两处都能删同一段就是功能重复 —— 而删除是全流程唯一
       不可逆的操作，它最不该有两个入口，也不该藏在子菜单里（在菜单里找"改名"
       时手一滑就点到了）。现在删除只有一个去处：轴上那颗按钮，它按当前段工作，
       弹的确认框会把将删掉的文件逐条列出来。
       删除不禁「只剩一段」：删空是合法状态（delShot 里 p.idx = -1 接得住），
       「就这一段、不要了重新起」恰恰是删除最该管的场景。 */
    if (s.colour) {
      const clear = el("button", null, "清除颜色（回到自动）");
      clear.title = "这一段被手动指定过颜色 —— 右键菜单不再提供换色，"
        + "想回到自动配色就点这里；没设过颜色时这项不出现。";
      clear.onclick = () => { delete s.colour; ui.commit(); ui.paintSegTools(); menu.remove(); };
      menu.append(clear);
    }

    openMenu(menu, x, y);
    } catch (e) {
      console.error("[H3 Studio] 右键菜单打开失败：", e);
      lanesFlash("右键菜单打开失败：" + (e && e.message ? e.message : e));
    }
  }

  /** 把工程里的分段顺序同步给档案（chain.order）。
      「接一段」登记恒追加在档案末尾；插段之后工程顺序才是用户的意图 ——
      按 p.shots 的 clip 顺序重排一次，档案与轴就永不各说各话。
      档案里工程对不上的残留段会被后端排到末尾（reorder_clips 的规则）。 */
  async function syncArchiveOrder() {
    const name = ui.castName();
    if (!name) return;
    const ids = ui.proj().shots.map((sh) => sh.clip).filter(Boolean);
    if (!ids.length) return;
    try {
      const res = await api.fetchApi("/cglide/chain/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, order: ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      await ui.monitor.refresh();
    } catch (e) {
      console.warn("[H3 Studio] 档案顺序没能同步（不影响分段本身）：", e);
      lanesFlash("档案顺序没能同步：" + (e && e.message ? e.message : e));
    }
  }

  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { registerShot, adoptFilm, pushShotName, unregisterShot, lanesFlash, renameShot, snapGuideFrames, applyFrames, splitShotAt, splitShot: (i) => splitShotAt(i), syncArchiveOrder, openMenu, shotMenu, refreshSeamPanel, focusSeamPanel, updatePlanNote, redoCurrentClip });

  /* 主按钮：唯一主动作「重抽这一段」。settings 先安装、btnRedoClip 已存在。 */
  if (ui.btnRedoClip) {
    ui.btnRedoClip.onclick = (e) => { e.stopPropagation(); void redoCurrentClip(); };
  }
  /* 安装末尾先填充一次接缝面板（不依赖首次 render 何时触发）。 */
  void ui.refreshSeamPanel();
}
