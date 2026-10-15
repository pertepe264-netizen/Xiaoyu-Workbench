/* ============================================================
 * 通用提示词编辑器（业务无关）
 * ------------------------------------------------------------
 * 原生 <textarea> 只能装纯文本，框内做不了任何标记；本模块用
 * contenteditable 承载「文本 + 引用标记」，把四件事做成一个件：
 *   · 高亮   —— 命中的 @引用 标成 .tok.has / .tok.miss（空槽位）
 *   · @补全  —— 在插入符处弹出 .gcast-ac 列表，↑↓ / Enter / Tab / Esc
 *   · 悬停预览 —— 鼠标停在引用上弹 .gcast-toktip 缩略图
 *   · 纯文本回读 —— textContent 逐字还原，含换行，不掺 <br>/<div>
 *
 * 只管编辑：词表与素材解析由调用方注入（tokens / view），所以同一份
 * 代码既能服务 Qwen 编辑台，也能服务导演台——不会各自漂移出一份近似实现。
 * 样式复用 CAST_CSS 的 .gcast-ed / .tok / .gcast-ac / .gcast-toktip 类名。
 *
 * 与后端同名口径（csglide_qwen.py / csglide_cast.py 的别名规则）：
 *   · 别名 name 不含 @；strict 别名必须写 @名字 才引用（插入时自动补 @）
 *   · 别名前后空格两种写法都算命中（全角 U+3000 与半角展开为同一组）
 *   · \别名 是「保留文字、不展开」的转义，高亮同样不点亮它
 * ============================================================ */

const esc = (s) => String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* 记录（调用方给）——**一条素材一条记录**：
 *   name   规范引用文字（@图片1），永远是这条记录的主名字
 *   alias  可选的友好名（小明），不带 @；有它时补全与 chips 优先用它
 *   canon  判定素材归属的规范 token，缺省取 name
 *   file   素材文件；空 → 该引用标成「指向空槽位」
 *   strict 别名必须带 @ 才引用（写 @小明）
 *   label  补全列表里显示的名字，缺省别名或 name
 *   note   补全列表右侧的注解（通常给规范 token 的文件名/编号）
 *
 * 别名与规范 token 是**同一条素材的两个写法**，不是两条素材：早先把它们
 * 拆成两条记录，菜单里一条素材就出现两行，看起来像素材翻倍了。
 */
export function makePromptEditor(opts = {}) {
  const placeholder = opts.placeholder || "";
  const mode = opts.mode || "";
  const view = typeof opts.view === "function" ? opts.view : () => "";
  const tokens = typeof opts.tokens === "function" ? opts.tokens : () => [];
  const onChange = opts.onChange;
  /* 只读镜像：外接提示词接管时置位。所有写入口（输入 / 粘贴 / 剪切 / 撤销 /
     chips 插入）都收口在 fire() 与 replaceRange()，挡这两处即写不进去。 */
  let ro = false;

  const ed = el("div", "gcast-ed" + (opts.cls ? " " + opts.cls : ""));
  ed.setAttribute("contenteditable", "plaintext-only");
  /* plaintext-only 只有 Chromium 认；别的浏览器退回 true，靠 Enter 拦截
     保证不生成 <div>——否则 textContent 会掺进换行之外的结构。 */
  const flat = ed.contentEditable === "plaintext-only";
  if (!flat) ed.setAttribute("contenteditable", "true");
  ed.dataset.placeholder = placeholder;
  ed.spellcheck = false;

  /* ---------------- 纯文本 / 光标 ---------------- */

  /* 编辑区只由文本节点与扁平 span 构成，按 DOM 顺序累加文本节点长度
     即得字符偏移——与 textContent 同一套口径，不会各算各的。 */
  function charOffset(node, offset) {
    let total = 0;
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      if (n === node) return total + offset;
      total += n.textContent.length;
    }
    return total;
  }

  /* textContent 而非 innerText：innerText 依赖排版，编辑区高度为 0 时会
     返回空串，把提示词悄悄读没。 */
  const text = () => ed.textContent || "";

  function caret() {
    const s = window.getSelection();
    if (!s || !s.rangeCount) return { s: 0, e: 0 };
    const r = s.getRangeAt(0);
    if (!ed.contains(r.startContainer)) return { s: text().length, e: text().length };
    return { s: charOffset(r.startContainer, r.startOffset),
             e: charOffset(r.endContainer, r.endOffset) };
  }

  function setCaret(pos) {
    /* 未聚焦时绝不动选区：对可编辑元素 addRange 会把焦点抢过来。 */
    if (document.activeElement !== ed) return;
    const w = document.createTreeWalker(ed, NodeFilter.SHOW_TEXT);
    let n, acc = 0, last = null;
    while ((n = w.nextNode())) {
      last = n;
      if (acc + n.textContent.length >= pos) break;
      acc += n.textContent.length;
    }
    const s = window.getSelection();
    const r = document.createRange();
    if (n) { r.setStart(n, Math.max(0, Math.min(pos - acc, n.textContent.length))); r.collapse(true); }
    else if (last) { r.setStart(last, last.textContent.length); r.collapse(true); }
    else { r.selectNodeContents(ed); r.collapse(false); }
    s.removeAllRanges(); s.addRange(r);
  }

  const focused = () => document.activeElement === ed;

  /* 编辑期间随时记下光标：失焦后（点 chip 时焦点在按钮上）插入仍能落到用户
     离开时的位置，而不是一律追加到末尾。 */
  let lastCaret = 0;
  const track = () => { if (focused()) lastCaret = caret().s; };

  /* ---------------- 高亮 ---------------- */

  /* 别名集合随媒体架变化，正则每次现拼，不写死字面量。
     每条记录最多产两个正则（规范名 + 别名），逐条各跑一次：条数不过百，比
     拼一个巨型交替正则 + 命名组更好读，也不必为「是哪一组命中」做额外解包。 */
  function marks(t) {
    const out = [];
    for (const tk of tokens() || []) {
      if (!tk) continue;
      const forms = [];
      /* 规范名（@图片1）与别名（小明）是同一素材的两种写法，两种都点亮 */
      if (typeof tk.name === "string" && tk.name) forms.push({ text: tk.name, need: true });
      if (typeof tk.alias === "string" && tk.alias) {
        forms.push({ text: tk.alias, need: !!tk.strict });
      }
      for (const f of forms) {
        const core = f.text.startsWith("@") ? f.text.slice(1) : f.text;
        if (!core) continue;
        /* need = 必须带 @ 才引用（规范 token、以及 strict 别名） */
        const at = f.need || f.text.startsWith("@");
        /* 边界口径与后端同一套：首尾含 ASCII 字母的成分用字母数字边界，纯中文
           只挡数字——中文没有空格分词，连汉字一起挡会让「看小明走过来」失效。 */
        const asciiEdge = /[A-Za-z]/.test(f.text[0] + f.text[f.text.length - 1]);
        const left = at ? "(?<![\\u0000\\\\A-Za-z0-9])"
          : asciiEdge ? "(?<![\\u0000\\\\A-Za-z0-9])" : "(?<![\\u0000\\\\0-9])";
        const right = asciiEdge && !at ? "(?![A-Za-z0-9])" : "(?![0-9])";
        const scope = at ? "@" : "@?";
        /* 别名里的空格正文里可能被打成全角，展开成 [ \u3000] 两种写法都点亮 */
        const body = esc(core).replace(/ /g, "[ \u3000]");
        const re = new RegExp(left + scope + "(" + body + ")" + right, "g");
        let m;
        while ((m = re.exec(t))) {
          if (!m[0]) { re.lastIndex++; continue; }
          out.push({ i: m.index, len: m[0].length, tok: m[0], tk });
        }
      }
    }
    out.sort((a, b) => a.i - b.i || b.len - a.len);   // 长匹配优先，短名不抢长名
    const segs = [];
    let pos = 0;
    for (const mk of out) {
      if (mk.i < pos) continue;                       // 已被更长的匹配覆盖
      if (mk.i > pos) segs.push({ text: t.slice(pos, mk.i) });
      segs.push({ tok: mk.tok, canon: mk.tk.canon || mk.tk.name,
                  file: mk.tk.file || "" });
      pos = mk.i + mk.len;
    }
    if (pos < t.length) segs.push({ text: t.slice(pos) });
    return segs;
  }

  let sig = "";
  function paint(t, caretPos) {
    const segs = marks(t);
    const s = JSON.stringify(segs);
    if (s === sig) return;                            // 结构没变就不动 DOM
    sig = s;
    const keepScroll = ed.scrollTop;                  // 重建会清滚动位置
    ed.replaceChildren();
    for (const g of segs) {
      if (g.tok === undefined) { ed.append(document.createTextNode(g.text)); continue; }
      const sp = document.createElement("span");
      sp.className = "tok " + (g.file ? "has" : "miss");
      sp.textContent = g.tok;                         // 保留原文，回读逐字不变
      sp.dataset.tok = g.tok;
      sp.dataset.canon = g.canon;
      if (g.file) sp.dataset.file = g.file;
      ed.append(sp);
    }
    ed.scrollTop = keepScroll;
    if (caretPos != null) setCaret(caretPos);
  }

  /* ---------------- @ 补全 ---------------- */

  const ac = el("div", "gcast-ac");
  ac.style.display = "none";
  ac.style.position = "fixed";
  ac.addEventListener("pointerdown", (e) => e.preventDefault());   // 保住编辑区焦点
  document.body.append(ac);   // 挂在 body，弹出时不参与节点内部排版

  let items = [], idx = 0, acStart = -1, acQuery = "", open = false;

  function closeAC() { ac.style.display = "none"; items = []; acStart = -1; open = false; }

  /* 插入符坐标：用一份样式克隆的镜像量文字落点，避免依赖已废弃的 selection 坐标 */
  function caretXY() {
    const cs = getComputedStyle(ed);
    const mirror = el("div");
    ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
     "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
     "borderTopWidth", "borderLeftWidth", "textTransform", "wordSpacing"]
      .forEach((k) => { mirror.style[k] = cs[k]; });
    Object.assign(mirror.style, {
      position: "absolute", left: "-99999px", top: "0", visibility: "hidden",
      whiteSpace: "pre-wrap", wordWrap: "break-word",
      width: ed.clientWidth + "px", boxSizing: "border-box",
    });
    const mark = el("span", null, "\u200b");
    mirror.append(document.createTextNode(text().slice(0, caret().s)), mark);
    document.body.append(mirror);
    const x = mark.offsetLeft, y = mark.offsetTop;
    mirror.remove();
    const box = ed.getBoundingClientRect();
    return { x: box.left + x, y: box.top + y - ed.scrollTop + parseFloat(cs.lineHeight || 18) + 4 };
  }

  /* 补全列表里一条素材只占一行：别名与规范 token 是同一素材的两个写法。
     标签优先显示别名（能认出来是谁），note 里带规范 token（知道会写成什么）。 */
  function drawAC() {
    ac.replaceChildren();
    items.forEach((t, i) => {
      const b = el("button");
      b.setAttribute("aria-selected", String(i === idx));
      if (t.file) { const im = el("img"); im.src = view(t.file); b.append(im); }
      else b.append(el("span", "glyph", "@"));
      b.append(el("span", null, t.label || t.alias || t.name));
      if (t.note) b.append(el("span", "tag", t.note));
      b.addEventListener("pointerdown", (e) => e.preventDefault());
      b.onclick = () => accept(i);
      ac.append(b);
    });
  }

  function openAC(query, start) {
    const q = query.toLowerCase();
    items = [];
    for (const t of tokens() || []) {
      if (!t || !t.name) continue;
      const canon = t.name.replace(/^@/, "").toLowerCase();
      const alias = (t.alias || "").toLowerCase();
      if (q && !canon.includes(q) && !alias.includes(q)
        && !(t.label || "").toLowerCase().includes(q)) continue;
      items.push(t);
    }
    if (!items.length) { closeAC(); return; }
    idx = 0;
    acStart = start;
    acQuery = query;
    ac.dataset.mode = mode;
    drawAC();
    ac.style.display = "";
    const { x, y } = caretXY();
    const w = ac.offsetWidth || 220, h = ac.offsetHeight || 200;
    ac.style.left = Math.max(6, Math.min(x, window.innerWidth - w - 8)) + "px";
    ac.style.top = (y + h > window.innerHeight - 8 ? Math.max(6, y - h - 26) : y) + "px";
    open = true;
  }

  function maybeAC() {
    if (!focused()) { closeAC(); return; }
    const t = text(), c = caret().s;
    const m = /@([^\s@]*)$/.exec(t.slice(0, c));
    if (!m) { closeAC(); return; }
    /* 转义写法 \@ 不该触发补全——与高亮、后端展开三处保持一致 */
    if (t[c - m[0].length - 1] === "\\") { closeAC(); return; }
    openAC(m[1], c - m[0].length);
  }

  /* 写哪个名字，取决于你打的是哪个名字：打了「图片1」就补 @图片1，打了别名
     （或什么都没打）就补别名——否则打 @图片1 回车却插入「小明」，是另一种意外。 */
  function accept(i) {
    const t = items[i];
    if (!t) return;
    const canon = t.name.replace(/^@/, "");
    const q = (acQuery || "").toLowerCase();
    const alias = t.alias || "";
    const useAlias = !!alias && (q === "" || alias.toLowerCase().includes(q)
      || !canon.toLowerCase().includes(q));
    const ins = useAlias
      ? (t.strict ? "@" + alias : alias)
      : (t.name.startsWith("@") ? t.name : "@" + t.name);
    replaceRange(acStart, caret().s, ins);
    ed.focus();
  }

  /* ---------------- 悬停预览 ---------------- */

  const tip = el("div", "gcast-toktip");
  document.body.append(tip);
  const hideTip = () => { tip.style.display = "none"; };

  const tokFrom = (e) => {
    let n = e.target;
    if (n && n.nodeType === 3) n = n.parentElement;
    return n && n.closest ? n.closest(".tok") : null;
  };

  function showTip(sp) {
    const canon = sp.dataset.canon || sp.dataset.tok;
    const file = sp.dataset.file || "";
    tip.replaceChildren();
    if (!file) {
      tip.append(el("span", "cap", canon + " 指向空槽位，请补充参考素材"));
    } else {
      const im = el("img");
      im.src = view(file);
      tip.append(im, el("span", "cap", canon));
    }
    tip.style.display = "block";
    const r = sp.getBoundingClientRect();
    const tw = tip.offsetWidth || 170;
    tip.style.left = Math.min(Math.max(8, r.left), window.innerWidth - tw - 8) + "px";
    tip.style.top = Math.min(r.bottom + 6, window.innerHeight - 140) + "px";
  }

  ed.addEventListener("mouseover", (e) => {
    const sp = tokFrom(e);
    if (sp) showTip(sp); else hideTip();
  });
  ed.addEventListener("mouseout", (e) => {
    const sp = tokFrom(e);
    if (sp && !sp.contains(e.relatedTarget)) hideTip();
  });
  ed.addEventListener("scroll", hideTip);

  /* 点空白处收起补全列表与预览浮窗。不能只靠编辑区的 blur：LiteGraph 在画布
     pointerdown 上 preventDefault（防选中文字/拖拽干扰），焦点因此**不会**离开
     编辑区，blur 永远不触发，浮窗就赖在屏幕上不走。所以在这里按「目标不在编辑区
     也不在浮窗里」兜底收起。 */
  const onDocDown = (e) => {
    const t = e.target;
    if (ed.contains(t) || ac.contains(t)) return;
    closeAC();
    hideTip();
  };
  document.addEventListener("pointerdown", onDocDown, true);

  /* ---------------- 写回与撤销 ---------------- */

  let prevText = "";
  const undo = [], redo = [];
  let lastPush = 0;

  const fire = () => { if (!ro && onChange) onChange(text()); };

  /* 入栈的是「本次编辑前的文本」。600ms 内的连续输入合并成一步，
     免得一个字撤一次。 */
  function remember() {
    if (Date.now() - lastPush < 600) return;
    undo.push(prevText);
    if (undo.length > 200) undo.shift();
    redo.length = 0;
    lastPush = Date.now();
  }

  function replaceRange(start, end, str) {
    if (ro) return;                              // 只读镜像：外部文本只作展示
    const t = text();
    const s = Math.max(0, Math.min(start, t.length));
    const e = Math.max(s, Math.min(end, t.length));
    const next = t.slice(0, s) + str + t.slice(e);
    remember();
    sig = "";                                    // 强制重绘
    ed.textContent = next;
    paint(next, Math.min(s + str.length, next.length));
    closeAC();
    prevText = next;
    fire();
  }

  function applyHistory(t) {
    if (t == null) return;
    const cur = text();
    let i = 0;
    while (i < cur.length && i < t.length && cur[i] === t[i]) i++;   // 光标落在分歧点
    sig = "";
    ed.textContent = t;
    paint(t, i);
    closeAC();
    prevText = t;
    fire();
  }

  let composing = false;
  ed.addEventListener("compositionstart", () => { composing = true; });
  /* compositionend 不能立即 sync()：此刻浏览器还没把拼音替换成汉字，text()
   * 读到的仍是拼音串，paint() 重建 DOM 会把拼音固化、顶掉候选。等下一帧
   * 上屏完成后再重绘。composing 标志也留到那时才清，挡住这期间漏进来的
   * input 事件（部分输入法上屏瞬间 e.isComposing 已为 false）。 */
  ed.addEventListener("compositionend", () => {
    requestAnimationFrame(() => {
      composing = false;
      sync();
    });
  });

  ed.addEventListener("input", (e) => {
    if (e.isComposing || composing) return;      // 输入法拼字期间重建 DOM 会打断候选
    remember();
    sync();
  });

  function sync() {
    const t = text();
    paint(t, focused() ? caret().s : null);
    prevText = t;
    track();
    fire();
    maybeAC();
  }

  ed.addEventListener("keydown", (e) => {
    /* LiteGraph 的 processKey 只跳过 <input>，不认 contenteditable：按键冒泡
       上去会被当成画布快捷键（Ctrl+Z 直接回滚整个工作流），一律挡在节点内。 */
    e.stopPropagation();
    if (ro) return;                // 只读：编辑入口全关，但仍要挡住画布快捷键
    const k = (e.key || "").toLowerCase();
    if (e.ctrlKey || e.metaKey) {
      if (k === "z" || k === "y") {
        e.preventDefault();
        const cur = text();
        if (k === "y" || e.shiftKey) {
          const n = redo.pop();
          if (n == null) return;
          undo.push(cur); applyHistory(n);
        } else {
          const p = undo.pop();
          if (p == null) return;
          redo.push(cur); applyHistory(p);
        }
      }
      return;
    }
    if (open) {
      if (k === "arrowdown" || k === "arrowup") {
        e.preventDefault();
        idx = (idx + (k === "arrowdown" ? 1 : items.length - 1)) % items.length;
        drawAC();
        return;
      }
      if (k === "enter" || k === "tab") { e.preventDefault(); accept(idx); return; }
      if (k === "escape") { e.preventDefault(); closeAC(); return; }
    }
    if (k === "enter" && !flat) {                // 非 Chromium 的退路：手写换行
      e.preventDefault();
      replaceRange(caret().s, caret().e, "\n");
      return;
    }
  });
  ed.addEventListener("keyup", (e) => { e.stopPropagation(); track(); });
  ed.addEventListener("mouseup", track);
  ed.addEventListener("pointerdown", (e) => e.stopPropagation());
  ed.addEventListener("wheel", (e) => e.stopPropagation());
  ed.addEventListener("cut", (e) => e.stopPropagation());
  ed.addEventListener("copy", (e) => e.stopPropagation());
  ed.addEventListener("paste", (e) => {
    e.preventDefault(); e.stopPropagation();
    const dt = e.clipboardData || window.clipboardData;
    const t = dt ? dt.getData("text/plain") : "";
    if (t) replaceRange(caret().s, caret().e, t);
  });
  ed.addEventListener("blur", () => { closeAC(); hideTip(); });

  /* ---------------- 对外 ---------------- */

  return {
    node: ed,

    getText: text,

    readonly: () => ro,

    /* 转只读镜像（外接提示词接管时用）：只关写入，不动选区、不触发 onChange；
       高亮与悬停预览照常可用。 */
    setReadonly(v) {
      const next = !!v;
      if (next === ro) return;
      ro = next;
      ed.setAttribute("contenteditable", ro ? "false" : (flat ? "plaintext-only" : "true"));
      ed.classList.toggle("gcast-ro", ro);
      if (ro) { closeAC(); hideTip(); }
    },

    /* 外部写入（载入存档、撤销外部来源）：不走 onChange，避免回声 */
    setText(t, caretPos) {
      const s = String(t == null ? "" : t);
      if (s === text()) { if (caretPos != null) setCaret(caretPos); return; }
      sig = "";
      ed.textContent = s;
      paint(s, caretPos != null ? caretPos : null);
      closeAC();
      prevText = s;
    },

    /* 在插入点插入（chips 点击走这里）；未聚焦时退回上次记录的光标位置 */
    insert(str) {
      const pos = focused() ? caret().s : Math.min(lastCaret, text().length);
      replaceRange(pos, pos, str);
    },

    /* 词表变了（媒体架增删素材）→ 重绘高亮；补全面板开着就按当前查询重开 */
    refresh() {
      sig = "";
      const q = open ? /@([^\s@]*)$/.exec(text().slice(0, caret().s)) : null;
      paint(text(), focused() ? caret().s : null);
      if (q) maybeAC(); else closeAC();
    },

    focus() { ed.focus(); },

    destroy() {
      document.removeEventListener("pointerdown", onDocDown, true);
      ac.remove();
      tip.remove();
    },
  };
}
