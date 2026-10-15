import { api } from "../../scripts/api.js";
import { setCurrent } from "./bz_rack.js";
import { createFilmMonitor } from "./csglide_film.js";
import { el } from "./csglide_ui_kit.js";
import { ghostShotIndexes } from "./csglide_ghost.js";

/* ============================================================
 * 成片监视器 / 时间轴 / 渲染控制 / 渲染范围
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installMonitor(ui) {
  /* ---- 成片监视器（吸收续接合成的预览窗） ----
     续接合成在图上拿不到本段解码画面（导演台 → KSampler → VAE Decode →
     导演台 会成环），所以物理上合不进导演台；合并落在面板这一层：导演台
     与续接合成共用同一个成片监视器组件，看的是同一份段落档案。

     片名 = 工程名（阶段 1 已把两者合一），所以这里直接读工程面板的那个名字，
     不另设输入框——两处打不一致就会各存各的档案，那是最难查的一类坑。 */
  const monWrap = el("div", "gcast-monitor");
  /* 时间轴外挂（v7 式 lanes 行）：分段轨 + 波形 + 播放头横贯整个节点宽度，
     独立于监视器宽度 —— 监视器在双栏左栏里再窄，时间轴也占满整行。
     监视器收起 / 拖出悬浮时这条轴照常可用。

     它是面板上唯一能一眼看完整部片子的地方，段一多就必须独占整行，不能跟
     监视器、设置卡挤在双栏里分宽度。所以给它单独一个头：标题 + 段数/总时长
     + 工程级操作按钮。 */
  const lanesWrap = el("div", "gcast-lanes");
  const lanesHd = el("div", "gcast-lanes-hd");
  const lanesTitle = el("span", "t", "全片总览");
  const lanesMeta = el("span", "mono", "—");
  /* 这一行的工具区：工程级操作（接一段 / 删除 / 导出 / 工程菜单）。所有「动
     的是这部片子」的操作都归到这条轴上 —— 它们管的就是轴上这些段落，摆在这儿
     眼睛不用离开要改的东西。
     工程操作不再散在监视器工具栏与指挥条两头：分两处摆会让人以为「＋」和
     「工程 ▾」是两套不相干的东西，其实一个加段、一个存整片，都是工程级的。
     注意：原来那组缩放按钮（−/＋/⤢）已经不放在这里了 —— 它们控制的是那条
     已撤下的旧时间轴（tl 仍留在内存但没挂进面板，见「两条时间轴合一」），点了
     只会空转。总览轴始终把所有段落按比例铺满整行（renderSegs 按 total_frames
     定宽），没有"缩放"这个概念，摆三个按不动的按钮只会让人以为面板坏了。 */
  const lanesTools = el("div", "tools");
  /* 运行时反馈条：落点在轴头——「全片总览 · N 段」旁边、操作按钮左边
     （用户指定）。提示随轴头常驻可见，又不挤占时间轴的高度；文字过长时
     收成省略号，鼠标悬停可看全文（lanesFlash 会把全文写进 title）。 */
  const flashBar = el("div", "gcast-flash");
  lanesHd.append(lanesTitle, lanesMeta, flashBar, lanesTools);
  lanesWrap.append(lanesHd);
  /* E1：当前段上下文条（段选择 / 重抽 / 接缝 / 主按钮）挂在全局命令行与
     段块轨之间；createFilmMonitor 随后把 band/foot 外挂到它后面。 */
  if (ui.ctxBar) lanesWrap.append(ui.ctxBar);
  ui.lastPickedClip = null;
  /* 片名 = 工程名（阶段 1 已把两者合一）。抽成一个函数：监视器的三个回调
     都要它，写三遍就会在某次改动后少改一处。 */
  const castName = () => {
    const p = ui.node.properties && ui.node.properties.gcast_project;
    return (p && p.name) || (ui.st.chain && ui.st.chain.name) || "";
  };
  /* ---- 锁定 / 解锁一段 ----
     监视器上那颗锁按钮，与段块右键菜单的「🔒 锁定这一段」，走**同一条后端接口、
     同一套状态同步** —— 两处各写一份代码，改一次就会只改一处，于是同一个动作
     在两个入口表现不同（一边钉住了编辑目标、另一边没有）。
     这里只管"改档案 + 同步状态"，刷新交给调用方：监视器的按钮自己会 refresh，
     菜单那边也要 refresh，两者时机不同，塞进来反而多刷一次。 */
  async function setSegLock(clip, locked) {
    const name = castName();
    if (!name) throw new Error("还没有片名");
    if (!clip || !clip.id) throw new Error("这一段没有 id，锁不了");
    const res = await api.fetchApi("/cglide/chain/lock", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, id: clip.id, locked }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
    /* 锁上的那一刻就把编辑目标钉过去；解锁则把当前段交回给它自己 */
    if (locked) await setCurrent(name, clip.id);
    ui.segLocked = !!locked;
    ui.updateSegChip();
    return data;
  }

  /** 段块右键菜单的「🔒 / 🔓 这一段」。clip 来自段块（可能是工程里的分段，
      也可能只在档案里有）—— 所以只认 clip，不认工程下标。 */
  async function toggleSegLock(clip) {
    if (!clip) return;
    try {
      await setSegLock(clip, !clip.locked);
      await ui.monitor.refresh();
    } catch (err) {
      ui.lanesFlash(`锁定失败：${err.message || err}`);
    }
  }

  /* ---- 轴上拖动排序 ----
     「前移 / 后移」两个菜单项已删（20261009）：一次只挪一位，十几段的片子要把
     第 12 段挪到第 2 位得右键十几次。改成在轴上拽着段块走（见 csglide_film.js
     的拖动排序），一次到位，也才是剪辑软件里"挪片段"的手势。

     顺序同时存在两处，必须一起改：
       · 段落档案的 chain.order —— 时间轴画的**就是它**（film_summary 走它）
       · 工程里的 p.shots —— 右栏那张「这一段」卡、渲染范围亮条、跳过 / 断链
         标记都按它的**下标**读
     只改一边，两边当场错位：轴上第 3 块对应的却是面板里的第 5 段，
     paintRunScope 会把亮条画到别的块上。而且这种错位不报错，只是"看着不对"，
     最难查。所以两边同改，且都按 clip id 对齐 —— id 是分段唯一的身份，
     位置才是那个本来就要变的量。 */
  async function reorderTo(ids) {
    const name = castName();
    if (!name) throw new Error("还没有片名");
    if (ui.run) throw new Error("正在渲染 —— 等这一轮跑完再改顺序");
    const p = ui.proj();
    /* 记住重排前的样子。顺序同时存在两处，前端先改、后端再落盘：后端失败
       的话前端已经变了，两边就错位 —— 轴上第 3 块对应面板第 5 段，而且这种
       错位不报错，最难查。所以先存快照，后端失败就原样还原（工程顺序回到
       拖动前；轴上段块本来就没真改过 left，只是 translate 跟手，失败时平移
       一撤就自然弹回旧位置，与还原后的工程重新对得上）。 */
    const before = { shots: p.shots.slice(), idx: p.idx };
    const byClip = new Map();
    p.shots.forEach((sh) => { if (sh.clip) byClip.set(sh.clip, sh); });
    /* 编辑目标跟着**那一段**走，而不是留在同一个下标上：重排之后下标换了
       意思，不跟着搬，"当前段"就跳到别的段上去了。 */
    const curShot = p.shots[p.idx] || null;
    /* 按档案给的 id 顺序重排。工程里还没登记进档案（没有 clip）的分段原样
       挪到末尾 —— 一段都不丢，只是排在后面。 */
    const head = ids.map((id) => byClip.get(id)).filter(Boolean);
    const tail = p.shots.filter((sh) => !(sh.clip && ids.includes(sh.clip)));
    p.shots = head.concat(tail);
    if (curShot) p.idx = Math.max(0, p.shots.indexOf(curShot));
    ui.commit();
    ui.render();
    try {
      const res = await api.fetchApi("/cglide/chain/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, order: ids }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
      return data;
    } catch (e) {
      /* 回滚：把工程顺序还原，并重画面板，让"工程"和"档案"重新对齐旧顺序。 */
      p.shots = before.shots;
      p.idx = before.idx;
      ui.commit();
      ui.render();
      throw e;
    }
  }

  /* ---- 分段登记自愈 ----------------------------------------------------
     工程里有分段、但档案里没有它的条目（登记失败过 / 「还原」带回的段 /
     档案被清过 / 旧工作流载入），轴上就永远画不出它的块 —— 悬停工具条、
     接缝插入点全部无从谈起，用户只看到一条空轴（"啥都没有点哪"）。
     每次轴画完（onAfterSegs 带回档案的段落清单）核对一遍：工程里对不上
     档案的分段，补登记一次。registerShot 成功会自己刷新监视器 → 再画完
     → 再核对，直到全部对上为止。后端 add_clip 按「分段 uid」去重，
     重复登记不会把档案越堆越长。 */
  /* ---- 工程里的「幽灵分段」清理 ----------------------------------------
     一段 = 工程里一个 shot + 档案里一个 clip，一一对应。实际跑下来出现过
     对不上的：工程里留下一个 shot，它的 clip 字段还指着**另一段已经在用的**
     档案条目（删段只清了档案那一侧，或登记时撞到同一个 id）。后果有两层：
       · 轴头「全片总览 · 4 段」数的是工程，轴上只有 3 块（档案只有 3 条）
         —— 写 4 画 3，用户删了半天等于没删；
       · 上面那段自愈救不了它：自愈判"我这段的 clip id 在档案里有没有"，
         两段指的都是同一个已存在的条目，答案永远是"有"，于是这个幽灵
         永远不会被重新登记，也永远画不出来 —— 一次撞号，永久隐形。
     判据收得极紧才敢自动删：**后出现的那一段，它的 clip id 已经被前面
     某一段占用，而且确实存在于档案**。一段不可能同时属于两个分段，这条
     不会误伤正常段。
     只删工程里那条记录，**绝不碰档案** —— 那个 clip 归前面那一段，
     跟着删就把真段子的成片、潜空间一起带走了。删前先 snapProject，
     工程面板「还原」可原样退回。 */
  function sweepGhostShots(rows) {
    const p = ui.proj();
    if (!p.shots.length) return;
    /* 判据本身在 csglide_ghost.js（无依赖，可自测），这里只管动手。 */
    const ghosts = ghostShotIndexes(p.shots, rows);
    if (!ghosts.length) return;
    ui.snapProject(`清理残留分段前（${ghosts.length} 个）`);
    for (let k = ghosts.length - 1; k >= 0; k--) {
      const i = ghosts[k];
      const sh = p.shots[i];
      console.log(`[H3 Studio] 清掉工程里的残留分段「`
        + `${(ui.shotLabel && ui.shotLabel(sh, i)) || sh.name || ("分段 " + (i + 1))}」`
        + `（uid ${sh.id}）—— 它占用的档案条目 ${sh.clip} 已归别的分段`);
      p.shots.splice(i, 1);
    }
    if (!p.shots.length) p.idx = -1;
    else p.idx = Math.min(Math.max(0, +p.idx || 0), p.shots.length - 1);
    ui.commit();
    ui.paintSegTools();
    ui.lanesFlash(`工程里 ${ghosts.length} 个残留分段已清掉（它们的档案条目归别的分段）`
      + "　·　误删可用工程面板「撤销」退回");
  }

  let healTimer = 0;
  function scheduleSelfHeal(rows) {
    clearTimeout(healTimer);
    healTimer = setTimeout(() => {
      healTimer = 0;
      if (ui.run) return;
      if (!ui.castName()) return;          /* 没片名无从登记（另行提示） */
      /* 先摘幽灵：撞号的那一段留在工程里，后面的登记自愈永远算不对。 */
      sweepGhostShots(rows);
      const have = new Set((rows || [])
        .map((r) => r.clip && r.clip.id).filter(Boolean));
      const pending = ui.proj().shots
        .filter((sh) => !sh.clip || !have.has(sh.clip));
      if (!pending.length) return;
      void (async () => {
        let healed = false;
        for (const sh of pending) {
          if (!have.has(sh.clip || "")) sh.clip = "";   /* 档案里已经没有 → 重新登记 */
          /* silent：补登记 N 段不再每段都打一次 film_summary + 全轴重绘，
             循环结束后统一刷一次（20261012 联动提速）。 */
          await ui.registerShot(sh, { silent: true });
          if (sh.clip) healed = true;
        }
        /* syncArchiveOrder 内部已统一 refresh 一次（不再每段各刷）。 */
        if (healed) {
          await ui.syncArchiveOrder();
          ui.broadcastSeg && ui.broadcastSeg("seg");
        }
      })();
    }, 150);
  }

  const monitor = createFilmMonitor({
    title: "🎬 成片监视器",
    getName: castName,
    timelineHost: lanesWrap,
    /* 点中某段 → 把它设为三个节点共享的「当前段」。
       文案台与媒体架轮询读它，于是切段时两边自动跟着切 —— ComfyUI 没有
       跨节点事件机制，走后端这份档案是最小的代价（几十字节的 JSON）。
       锁定的段仍在：**别把锁定段让出去**，否则锁就白锁了。 */
    onPick: (clip, viaUser) => {
      console.log("[联动] onPick 入口: clip=" + (clip && clip.id) + " viaUser=" + viaUser + " locked=" + (clip && clip.locked) + " name=" + castName());
      ui.lastPickedClip = clip;
      /* 段头上的锁跟着走：锁定是"编辑目标被钉住"，指挥条上必须同步出现，否则
         用户在这里看不出为什么切不了段。 */
      ui.segLocked = !!(clip && clip.locked);
      ui.updateSegChip();
      /* 只有**用户亲手点/拖**的那一次才动"当前段"。
         refresh 也会替人选一段（最后一段有成片的那些），那是自动行为；让它也
         去写当前段，用户刚点的那一段过一会儿就被盖回去了。 */
      if (!viaUser) return;
      const name = castName();
      if (name && clip && !clip.locked) {
        /* 网络写入失败要有说法：高亮/切换已经本地生效（乐观），档案那边
           写不进去时说一声，别让"点了没反应"从另一头再长出来。 */
        setCurrent(name, clip.id)
          .catch((e) => ui.lanesFlash?.(`切换当前段没能写进档案：${e && e.message ? e.message : e}`));
      }
      /* 面板跟着换段 —— 这一步原先没有，是"点了段、什么都没发生"的根因：
         轴上点第 3 段，只有轴自己知道（它内部换了选中的那一段），而面板右侧那张
         「这一段」设置卡、首尾帧卡、指挥条上的"第 N 段"、乃至「删除本段」按钮
         操作的都还是**工程里的 p.idx**——两套"当前段"互不相认，于是点完之后
         看起来只有那个紫环动了一下，别的纹丝不动；更糟的是这时点「删除本段」
         删掉的是上一段。
         现在时间轴是唯一的切段入口：点哪一段，面板就搬到哪一段（switchTo 里
         stash / 还原媒体架 / load / commit 全套都在）。
         档里那一段在工程里找不到对应分段时（多半是旧档案残留），既不能硬写
         p.idx 也不能装作没事 —— 说出来，让用户知道这一段只能在这条轴上换版本，
         在这儿改不了它的参数。 */
      if (!clip || clip.locked) return;
      const p = ui.proj();
      let j = -1;
      if (clip.id) j = p.shots.findIndex((sh) => sh.clip === clip.id);
      if (j < 0) j = (clip.index || 1) - 1;
      if (!p.shots[j]) {
        ui.lanesFlash(`第 ${clip.index} 段只在档案里 —— 右键它（或双击改名）即可认领进工程，之后就能就地编辑`);
        return;
      }
      if (j !== p.idx) ui.switchTo(j);
    },
    onLock: (clip, locked) => setSegLock(clip, locked),
    /* 轴上拖着段块改顺序（即原「前移 / 后移」两个菜单项的下场）。
       渲染中禁拖：顺序一变，正在跑的渲染链就和档案里新写的顺序对不上了 ——
       与其拖完再回滚，不如拖不动（并给出为什么）。 */
    canReorder: () => !ui.run,
    onReorder: (ids) => reorderTo(ids),
    /* 段级编辑入口。这两条原本挂在「工程分段条」上（双击改名 / 右键换色），
       第二条轴撤掉之后搬到这里 —— 时间轴只剩这一条，能看见这一段就必须能
       改这一段，否则「看」和「改」分家，用户得记着去别处改。
       下标换算：监视器的 clip.index 从 1 起，工程 shots 从 0 起。阶段 1 已
       把两者合一（工程名 = 片名，分段 = 段落），所以差 1。 */
    onRename: async (clip, hostEl) => {
      /* 孤儿段（只在档案里）双击改名 = 先认领回工程再就地改 —— 原来对不上
         工程下标就悄悄 return，用户看到的就是"双击没反应"。 */
      const p = ui.proj();
      let j = p.shots.findIndex((sh) => sh.clip === clip.id);
      if (j < 0 && clip.id) {
        const row = ui.monitor.clipRowOf?.(clip.id);
        if (row) await ui.claimShot(row);
        j = p.shots.findIndex((sh) => sh.clip === clip.id);
      }
      if (j >= 0) ui.renameShot(j, hostEl);
    },
    /* clip 这一行本身就是档案视图里的一段（带 id / take / takes），整条传进去：
       段级菜单要按它去拉这一段的版本清单，不必再拿下标去换一次 id。 */
    onSegMenu: (clip, e) => {
      try {
        ui.shotMenu((clip.index || 1) - 1, e.clientX, e.clientY, clip);
      } catch (err) {
        /* 菜单没弹出来必须给个说法，别又变回"点了没反应"。 */
        console.error("[H3 Studio] 右键菜单打开失败：", err);
        ui.lanesFlash("右键菜单打开失败：" + (err && err.message ? err.message : err));
      }
    },
    /* 拖边缘改时长。锁定的段不给拖——锁的意义就是「这段别再动了」。 */
    canResize: (clip) => !clip.locked,
    onResize: (clip, frames) => ui.applyFrames(clip.id, (clip.index || 1) - 1, frames),
    /* 空态引导卡的「＋ 接第一段」CTA（M4）：与轴头 / 指挥条的接段是同一动作。 */
    onAddFirstShot: () => ui.addShot(false),
    onMode: (m) => {
      /* 收起 / 拖出后占位高度完全不同，交给 CSS 换档（固定高度是给取景器
         用的，收起时留 300px 空盒子就太傻了）。 */
      monWrap.classList.toggle("is-collapsed", m === "collapsed");
      monWrap.classList.toggle("is-float", m === "floating");
      /* 三态切换后面板内容高度变了，节点要跟着重排。
         延后一帧：此刻 DOM 还没重排完，量到的是旧高度。 */
      ui.node.setDirtyCanvas(true, true);
      requestAnimationFrame(() => { try { ui.fitNode(true); } catch { /* 忽略 */ } });
    },
    /* 段块画完的回调。宿主加在段块上的标记（渲染范围亮条 / 正在渲染脉冲 /
       断链虚线）每次都会被 paintSeg 的 className 整体重写抹掉；以前靠
       MutationObserver 盯子节点变动来补 —— 它只认"增删"，而点一下段、拖一下
       窗口这类重绘一个节点都不增删，补丁压根不触发，标记就此灰飞烟灭，
       表现就是"那些条子一会儿有一会儿没有"。
       改成由画的人喊一声：绘制归它，补标记归我们，两者在同一刻发生。 */
    onAfterSegs: (rows, changed) => {
      /* 标记/轴头/自愈每次都补：它们只遍历 1~3 个段块，成本可忽略；且渲染
         脉冲开始/结束时 film 账不变（changed=false），漏画脉冲会出大事。 */
      paintRunScope();
      paintLinkMarks();
      try { ui.updateLanesMeta && ui.updateLanesMeta(); } catch { /* 尚未装好 */ }
      scheduleSelfHeal(rows);
      /* 整画布标脏（LiteGraph 全节点重排，L3 卡顿源）只在段块真有增删/几何
         内容变化时做。后台 store 推送同一份账（changed=false）时，选中类、脉冲
         都是 classList 变化，浏览器直接合成，不需要重排（20261012 方案C）。 */
      if (changed !== false) {
        try { ui.node.setDirtyCanvas(true, true); } catch { /* 已销毁 */ }
      }
    },
    /* 接缝胶囊读取 / 设置「承接上一段多少帧」（段间过渡帧数）。真值在该段
       state.cont.frames（后端 csglide_cast.py 生成时读 cont.frames）。孤儿段先
       认领；contFramesOf 找不到段时返回 null，让组件回退到成片实际裁剪帧。
       20261012 根治：第 2+ 段若 frames 缺省/为 0 且左缝未断链，按默认续写窗口
       39（出厂默认）显示 —— 后端 pins 为空且未断链时本就自动接上段，旧版菱形
       却画成灰色"硬切"，显示与执行口径不一致。真断链（unlink）仍显示 0。 */
    contFramesOf: (clipId) => {
      const p = ui.proj();
      const j = p.shots.findIndex((s) => s.clip === clipId);
      if (j < 0) return null;
      const n = Math.max(0, Math.round(
        Number(p.shots[j].state && p.shots[j].state.cont && p.shots[j].state.cont.frames) || 0));
      if (n > 0) return n;
      if (j > 0) {
        const broken = !!(p.transitions && p.transitions[clipId]
          && p.transitions[clipId].type === "unlink");
        if (!broken) return 39;
      }
      return 0;
    },
    onContFrames: async (clipId, frames) => {
      let j = ui.proj().shots.findIndex((sh) => sh.clip === clipId);
      if (j < 0) {
        const row = ui.monitor.clipRowOf?.(clipId);
        if (row) {
          await ui.claimShot(row);
          j = ui.proj().shots.findIndex((sh) => sh.clip === clipId);
        }
      }
      if (j < 0) {
        ui.lanesFlash("这一段认领失败 —— 请右键它重试");
        return;
      }
      const p = ui.proj();
      const own = (p.shots[j].state = p.shots[j].state || {});
      own.cont = own.cont || {};
      own.cont.frames = Math.max(0, Math.min(4096, Math.round(Number(frames) || 0)));
      own.cont.mode = own.cont.mode || "latent";
      if (Number(p.idx) === j) ui.st.cont = { ...own.cont };
      ui.commit();
      ui.paintSegTools();
    },
    /* 接缝成片转场（M3）：读 / 写都落在工作流 properties 的独立映射，
       不污染 shots/state、不需后端新端点，随工作流保存、刷新不丢。 */
    getTransition: (clipId) => {
      const p = ui.node.properties && ui.node.properties.gcast_project;
      const m = p && p.transitions;
      return (m && clipId && m[clipId]) || null;
    },
    onTransition: (clipId, t) => ui.applyTransition(clipId, t),
    /* 菱形 / 段块接缝点击：选中该段并定位、高亮右侧卡接缝组（不弹浮层）。 */
    onFocusSeam: (segIndex1based) => ui.focusSeamPanel(segIndex1based),
    /* film.js 传来的 row.index 是 1based 段号；openSeamWindow/readSeamState 按
       0based 工程下标取段（targetIndex==null 用 p.idx），这里必须 −1，否则接缝
       段错位 / 末接缝越界返回 null（"还没登记上轴"）、帧数显示 0。 */
    onSeamWindow: (segIndex1based, ev) => ui.openSeamWindow(segIndex1based - 1, ev),
    /* 方案 B：选中段块居中图标的段操作（复制 / 插段 / 删除）。段块上方不再有
       挂 body 的浮层，故不遮挡接缝胶囊。 */
    onSegAction: (kind, c) => {
      const shots = ui.proj().shots;
      const j = shots.findIndex((sh) => sh.clip === c.id);
      if (kind === "copy") {
        if (j < 0) { ui.lanesFlash("这一段只在档案里，先右键「认领」进工程再复制"); return; }
        /* 完整克隆（提示词/参考/画布/时长/参数全照抄）到末尾，20261012 起
           不再走"接一段（只继承班底、提示词被砍、参考还会被轮询清掉）"。 */
        void ui.duplicateShot(j);
      } else if (kind === "add") {
        if (j >= 0) void ui.insertShot(j);
      } else if (kind === "del") {
        if (j >= 0) { void ui.delShot(j); return; }
        if (c.id) void ui.unregisterShot({ clip: c.id }, true).then((d) => {
          if (d && d.removed) ui.lanesFlash("档案残留段已删（含磁盘文件）");
        });
      }
    },
    /* 裁切剃刀（20261014）：在第 clipId 段内、第 a 净帧处切成两段。
       按 clip id 找工程下标，交给 splitShotAt（按切点 + 39 承接，自动登记 / 重排）。
       只在档案里的孤儿段先提示认领，避免切出对不上工程的段。 */
    onSplitAt: (clipId, a) => {
      if (ui.run) return;
      const p = ui.proj();
      const j = p.shots.findIndex((sh) => sh.clip === clipId);
      if (j < 0) {
        ui.lanesFlash("这一段只在档案里，裁切前先右键把它认领进工程");
        return;
      }
      void ui.splitShotAt(j, a);
    },
  });
  /* 断链/接上的唯一实现（接缝卡片、右键菜单、悬停 ⛓ 都走这里）：
     1) 写工程映射 p.transitions（随工作流落盘）；
     2) 同步后端 /transitions —— 播放与导出成片读它，缺了这步右键断开
        就只改时间轴显示、导出仍连播（旧 toggleLink 写 sh.link 的残缺 bug）。 */
  ui.applyTransition = async (clipId, t) => {
    if (!clipId) return;
    const p = ui.proj();
    if (!p.transitions) p.transitions = {};
    if (!t || t.type === "cut") delete p.transitions[clipId];
    else p.transitions[clipId] = {
      type: t.type,
      overlap: Math.max(0, Math.round(Number(t.overlap) || 0)),
    };
    try { ui.node.setDirtyCanvas(true, true); } catch { /* 已销毁 */ }
    /* 同步断链状态到后端档案（播放 / 导出读取）；失败不阻塞本地 */
    try {
      await api.fetchApi("/cglide/chain/transitions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: castName(), id: clipId,
          type: (t && t.type) || "cut",
          overlap: Math.max(0, Math.round(Number(t && t.overlap) || 0)),
        }),
      });
      /* 断链 / 接续只改 transitions，不改 pins（disp_pin_mode 不变），菱形与
         接缝符号全部实时读 getTransition/contFramesOf —— paintLinkMarks +
         repaintSeams 局部重画即可，**不需要**全量 refresh（旧版每次断链都
         pickFilm 拉档案 + 重建段块/缩略图，是胶囊卡顿主因之一，20261012 根治）。 */
      if (typeof ui.paintLinkMarks === "function") ui.paintLinkMarks();
      try { ui.monitor.repaintSeams && ui.monitor.repaintSeams(); } catch { /* 监视器尚未就绪 */ }
    } catch (e) {
      console.warn("[H3 Studio] 转场同步后端失败：", e);
    }
  };
  monWrap.append(monitor.root);
  /* 监视器在左、设置卡在右（v7 式双栏）。monWrap 在此处声明完成之后才挂进
     studioRow —— 之前会撞 TDZ。 */
  /* 顶部行：监视器（左）+ 参数卡（右），两栏顶对齐。参数卡内容多（首尾帧卡
     展开）时整行自然撑高、把下方全宽时间轴往下推，全貌直接露出。 */
  ui.studioRow.append(monWrap, ui.rightCol);


  /* ---- 两条时间轴合一（真融合，不是藏起来）----
     分段与段落从阶段 1 起就是同一回事（工程名 = 片名，分段 = 段落）。摆两条
     时间轴画同一批段落，只会让人不知道该看哪条 —— 但旧条上那些能力（增删、
     排序、改名、换色、跳过、断链）又不能丢。

     所以做的是**迁移**而不是**折叠**：把旧条上的段落级能力全部搬到监视器
     那条轴上（双击改名 / 右键段操作菜单 / 工具栏增删按钮），然后把旧条这条
     轴整个撤掉。能力一个不少，时间轴只剩一条。

     旧条当时是整块留在内存里的（没挂进面板，但绘制逻辑还在跑）。后来这块
     已整段删除 —— 见「阶段 0 死代码清理」：它早退到只剩 paintSegTools() 一
     条路，等于白跑一趟计算还占了五百行。 */
  /* 段落与工程级操作挂到总览轴那一行，不挂监视器工具栏：它们动的是这条轴上
     的段落，不是那块画面。「＋ / 🗑 / 导出」原先在监视器上，等于把"改片子"和
     "看片子"两件事混在同一个工具栏里。 */
  const laneBtn = (label, cls, title, onClick) => {
    const b = el("button", cls, label);
    b.type = "button";
    b.title = title;
    b.onclick = (e) => { e.stopPropagation(); onClick(e); };
    lanesTools.append(b);
    return b;
  };
  const tsep = () => {
    const s = el("div", "tsep");
    lanesTools.append(s);
    return s;
  };

  /* ===== 轴头工具条：按工作流四组成组（20261011 重构）=================
     [＋接一段] │ [▶全部渲染] [⬇导出成片] │ [工程▾]
     同组动作相邻、组间竖分隔；主色只给「全部渲染」一颗。
     段级操作（删除 / 断链接链 / 复制 / 接缝插入）仍在时间轴上就地悬停完成，
     不进这一排。 */

  /* —— 编辑组 —— */
  const btnAddSeg = laneBtn("＋ 接一段", "gcast-btn",
    "在末尾接一段（保留当前参考素材与画布）",
    () => ui.addShot(false));

  /* —— 裁切剃刀（20261014）：剪辑软件式"点哪切哪"，与「接一段」同属编辑组 ——
     点一下进裁切模式（时间轴鼠标变剃刀、出现吸附竖线），在段块上点一下即切
     两段；再点一次该按钮或按 Esc 退出；开始渲染自动退出。 */
  function setCutMode(on) {
    on = !!on && !ui.run;
    if (ui.cutMode === on) return;
    ui.cutMode = on;
    btnCut.classList.toggle("active", on);
    btnCut.textContent = on ? "✂ 裁切中" : "✂ 裁切";
    try { ui.monitor && ui.monitor.setCutMode && ui.monitor.setCutMode(on); }
    catch (e) { console.warn("[H3 Studio] 裁切模式切换失败：", e); }
    if (on) ui.lanesFlash("裁切模式：在想切开的位置点一下，即可把该段切成两段；Esc 退出");
  }
  ui.setCutMode = setCutMode;
  const btnCut = laneBtn("✂ 裁切", "gcast-btn gcast-cut-btn",
    "进入裁切模式：再在某段想切开的位置点一下，就把该段切成两段（吸附 17k+5 帧、后段承接 39 帧）。\n再点一次本按钮或按 Esc 退出。",
    () => setCutMode(!ui.cutMode));
  /* Esc 退出裁切（捕获：避免画布快捷键同时响应）。 */
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && ui.cutMode) { e.stopPropagation(); setCutMode(false); }
  }, true);
  tsep();

  /* —— 生成 / 交付组 ——
     渲染（重新生成画面）与导出（只拼接、不生成）是一前一后两步，挨在一起。 */
  const btnRunAll = el("button", "gcast-btn run", "▶ 全部渲染");
  btnRunAll.type = "button";
  btnRunAll.title = "依次渲染工程里的每一段（续写模式，渲染整个工程）";
  btnRunAll.onclick = (e) => {
    e.stopPropagation();
    ui.renderAll("continue", "all");
  };
  lanesTools.append(btnRunAll);

  const btnSaveFilm = el("button", "gcast-btn gcast-export-btn", "⬇ 导出成片");
  btnSaveFilm.type = "button";
  btnSaveFilm.title = "把时间轴上所有段拼成一条完整视频存进档案目录"
    + "（连接处自动裁掉承接重叠，与时间轴总长一致）";
  btnSaveFilm.onclick = (e) => {
    e.stopPropagation();
    void ui.saveFilmMerged(btnSaveFilm);
  };
  lanesTools.append(btnSaveFilm);

  const btnPushFilm = el("button", "gcast-btn gcast-export-btn", "🎬 推送整片");
  btnPushFilm.type = "button";
  btnPushFilm.title = "不重跑渲染，把时间轴上的整片直接送到「整片」口连的视频合并节点"
    + "（只加载模型、不采样；视频合并直接采用，不重新编码）";
  btnPushFilm.onclick = (e) => {
    e.stopPropagation();
    void ui.pushFilm(btnPushFilm);
  };
  lanesTools.append(btnPushFilm);

  /* 历史旧工程切换（20261013 用户定）：点开档案目录里的旧工程，一键切回；
     不是操作记录，是 output/h3_chain 下的工程档案。菜单见 csglide_cast_history.js。 */
  const btnHistory = el("button", "gcast-btn", "🕘 历史 ▾");
  btnHistory.type = "button";
  btnHistory.title = "切换到以前的旧工程（档案目录里的整部工程，不是操作记录）";
  btnHistory.onclick = (e) => {
    e.stopPropagation();
    if (ui.openHistoryMenu) ui.openHistoryMenu(btnHistory);
  };
  lanesTools.append(btnHistory);
  tsep();

  /* —— 历史组已删（20261012 用户定）：轴头上不再放撤销 / 重做 / 历史 ▾。
     快捷键 Ctrl+Z / Ctrl+Shift+Z 的工程撤销 / 重做仍保留（见 listeners）。 */

  /* —— 工程组：工程文件的新建 / 打开 / 导入 / 保存 / 打包 —— */
  lanesTools.append(ui.bProj);

  /* 渲染状态。声明位置刻意靠前：paintRenderRow() 读 run，而 render() →
     paintSegTools() 在 buildUI 还没跑到后半段时就会调用它。 */
  ui.run = null;                 /* null when idle; the run's own state when not */
  ui.runBox = null;              /* the overlay, so it can be torn down */

  /* 可用状态：工程里没分段、或正在渲染时禁用。跟随段操作按钮一起刷新，
     所以挂在 paintSegTools() 里 —— 任何一次时间轴重绘都会带上它。 */
  function paintRenderRow() {
    const n = ui.proj().shots.length;
    /* 渲染一开就把裁切剃刀收起来 —— 段块在跑，不能再切。 */
    if (ui.run && ui.cutMode) { try { setCutMode(false); } catch (e) { /* 尚未就绪 */ } }
    btnRunAll.disabled = !n || !!ui.run;
    btnRunAll.title = !n ? "请先添加一些分段"
      : `按顺序排队渲染全部 ${n} 个分段（续写模式：每段喂给下一段当引导，`
        + "最后得到的就是完整成片）。";
    /* 导出成片跟着禁：渲染中成片在变，拼到一半的片子不该被存；正在拼片
       时也别让人再点一次（dataset.busy 由 saveFilmMerged 自己挂牌）。 */
    btnSaveFilm.disabled = !n || !!ui.run || btnSaveFilm.dataset.busy === "1";
    btnSaveFilm.title = !n ? "请先添加一些分段"
      : "把时间轴上所有段拼成一条完整视频存进档案目录"
        + "（连接处自动裁掉承接重叠，与时间轴总长一致）";
    /* 推送整片跟着禁：渲染中不该再排一次；推送自己挂牌时也别重复点。 */
    btnPushFilm.disabled = !n || !!ui.run || btnPushFilm.dataset.busy === "1";
    btnPushFilm.title = !n ? "请先添加一些分段"
      : "不重跑渲染，把时间轴上的整片直接送到「整片」口连的视频合并节点"
        + "（只加载模型、不采样；视频合并直接采用，不重新编码）";
    /* 裁切剃刀：没段 / 渲染中都禁用（跟其它编辑按钮一致）。 */
    btnCut.disabled = !n || !!ui.run;
  }

  /* ---- 渲染范围 ↔ 时间轴联动 ----
     渲染范围固定「整个工程」：落在轴上的段顶上亮一条主色细条，正在跑的那
     一段亮度脉冲 —— 点之前能确认，跑的时候能看见。 */
  function paintRunScope() {
    /* 渲染中不能拖排序（顺序一变就和正在跑的链对不上），光标也要跟着变 ——
       手型光标是一句承诺，兑现不了就别给。这里顺手把那条承诺收回来。 */
    ui.monitor?.syncSortable?.();
    const segs = monitor.root.querySelectorAll(".bzf-seg[data-seg]");
    segs.forEach((s) => {
      const i = (parseInt(s.dataset.seg, 10) || 1) - 1;
      s.classList.toggle("inrun", true);
      s.classList.toggle("running", !!(ui.run && ui.run.i === i));
    });
  }

  /* 断口画在段块的左边缘：这一段独立起头，不承接上一段的尾巴。
     "接没接上"是这条轴最该说清的事之一 —— 段与段之间连不连贯，用户本该在按
     渲染之前就看见，而不是等成片出来才发现。 */
  /* 段 i 是否断链 —— 唯一真值 p.transitions（工程映射），与后端 /transitions 同步。 */
  const isUnlinkedAt = (i) => {
    const p = ui.proj();
    const sh = p.shots[i];
    return i > 0 && !!(sh && sh.clip && p.transitions
      && p.transitions[sh.clip] && p.transitions[sh.clip].type === "unlink");
  };
  function paintLinkMarks() {
    /* band 被 timelineHost 外挂进 lanesWrap，monitor.root 里已经没有段块
       （实测 .bzfilm 内 .bzf-seg = 0）—— 必须在 lanesWrap 里查，否则断链
       的左缘虚线口永远画不上。 */
    lanesWrap.querySelectorAll(".bzf-seg[data-seg]").forEach((s) => {
      const i = (parseInt(s.dataset.seg, 10) || 1) - 1;
      const broken = isUnlinkedAt(i);
      s.classList.toggle("unlinked", broken);
      /* tooltip：断开时追加、接上时撤回（旧版只增不删，重新接上仍挂"断开"）。 */
      if (broken && !/接缝已断链/.test(s.title || "")) {
        s.title = (s.title || "")
          + "\n⇤ 接缝已断链 —— 播放到这里不自动连播（停在段尾）";
      } else if (!broken && /\n⇤ 接缝已断链[^\n]*/.test(s.title || "")) {
        s.title = s.title.replace(/\n⇤ 接缝已断链[^\n]*/g, "");
      }
    });
  }

  /* 原先这里有一个 MutationObserver 盯 .bzf-segs 的子节点变动，用来在重绘后
     补画标记。它看不住"没增删节点"的那类重绘（点段 / 拖窗口），而那恰恰是
     最常用的两次 —— 现已改由上面的 onAfterSegs 在每次画完时补，观察器撤掉。 */

  /* 落地行：文案来源 + 渲染控制合一。这两件都是"看一眼 / 点一下"的动作，
     原来分两行各占一条，中间还夹着模式开关与首尾帧卡 —— 相关的两件事被拆到
     面板两头。 */
  const footSp = el("div");
  footSp.style.flex = "1 1 auto";
  footSp.style.minWidth = "4px";
  /* 不叫 foot：buildUI 后半段另有两个同名的局部变量（段落卡里的 foot），同名
     会遮蔽外层的这一个，改一次 DOM 就可能踩到。 */
  const footRow = el("div", "gcast-foot");
  /* 渲染结果行。跑完一整轮之后，面板必须留下"这一轮到底成了没有"——
     原来中断/失败只往控制台打一行，遮罩一撤界面就回到空闲，用户看不出跑了
     几段、断在第几段、要不要重跑、从哪里接着跑。结果常驻到下一次渲染开始。 */
  const runResult = el("div", "gcast-psrc gcast-runres");
  runResult.style.display = "none";
  function setRunResult(ok, text) {
    runResult.textContent = text;
    runResult.classList.toggle("ok", !!ok);
    runResult.classList.toggle("bad", !ok);
    runResult.style.display = "";
    runResult.title = text;
  }
  footRow.append(runResult, ui.pSrcLine, footSp);

  /* 顺序：指挥条（我是谁/哪一段/什么模式）→ 双栏（监视器 | 设置+首尾帧）
     → 全宽总览轴 → 落地行（文案来源 + 渲染）。
     总览轴独占整行放在双栏下面：段一多它就需要吃满整个节点宽度，放在双栏里
     会被右栏 540px 挤掉近一半；同时"上画面、下时间轴"是剪辑软件的位置直觉。 */
  /* （手势提示框已删，20261010 用户定：这行说明书常驻在轴下没人看 —— 操作
     说明都在悬停 title 与右键菜单里，不需要一整行常驻文字。） */

  ui.root.append(ui.cmdBar, ui.studioRow, lanesWrap, footRow);

  /* 右栏（设置 + 首尾帧）**不做限高**：内容多高，gcast-studio 就自然撑多高、把
     下方全宽时间轴往下推，全貌直接露出（用户明确要的旧行为）。原先"限高=监视器
     高 + 卡内滚动"会把首尾帧卡藏在卡内、还被下方时间轴挡住，已整段撤除，不再
     wrap monitor.refresh，也不挂限高 ResizeObserver。 */
  /* 原先这里挂了一个 ResizeObserver 在旧工程条的 tlView 上重画它。那条轴已
     撤下（连元素带绘制逻辑一起删了，见「阶段 0 死代码清理」），
     ResizeObserver 对脱离文档的元素根本不触发 —— 留着就是永不执行的死代码，
     删掉。段操作按钮的状态由 paintSegTools() 每次改动直接刷新，不依赖尺寸
     变化。 */


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { monWrap, lanesWrap, lanesHd, lanesTitle, lanesMeta, lanesTools, flashBar, castName, setSegLock, toggleSegLock, reorderTo, monitor, laneBtn, tsep, btnAddSeg, btnRunAll, btnSaveFilm, paintRenderRow, paintRunScope, paintLinkMarks, footSp, footRow, runResult, setRunResult });
}
