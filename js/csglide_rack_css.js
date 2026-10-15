/* ============================================================
 * H3 媒体架 — 样式
 * ------------------------------------------------------------
 * 面板级样式复用导演台那份（CAST_CSS），这里只补媒体架专属的：
 * 节点级拖入高亮、提示条、清空钮、别名输入框、胶囊小设置、本段指示。
 * ============================================================ */
/* 断环（20261013 白屏根因）：本文件原来从 csglide_cast.js 取 CAST_CSS / CUR_SEG，
   但依赖链 cast → bz_project_sync → csglide_media_rack → 本文件 → cast 构成循环：
   入口是 cast 时，模块在 L73 模板求值时访问尚未初始化的 CUR_SEG，
   TDZ ReferenceError 让整条扩展链加载失败、三个 H3 节点全裸（前端不显示）。
   直接从定义处取：CSS 是纯文本无依赖、CUR_SEG 只依赖 bz_store，均无环。 */
import { CSS as CAST_CSS } from "./csglide_cast.css.js";
import { CUR_SEG, CUR_SEG_DIM } from "./csglide_film.js";

export const RACK_CSS = `
.gmr.drop-active { outline:2px dashed var(--h3-accent); outline-offset:-3px; }
.gmr-bar { display:flex; align-items:center; }
/* 右下角清空钮：苹果质感——半透明毛玻璃底盘 + inset 高光描边 + 分层投影，
   叉由两条细杆绘制（比字体字符更利落、不随字体变形），悬停填 Apple Red。 */
.gmr { position:relative; }
.gmr button.gmr-clear { position:absolute; right:8px; bottom:8px; z-index:6;
  width:26px; height:26px; padding:0; border:0; border-radius:50%; cursor:pointer;
  display:flex; align-items:center; justify-content:center;
  background:rgba(255,255,255,.07);
  -webkit-backdrop-filter:blur(10px) saturate(180%);
  backdrop-filter:blur(10px) saturate(180%);
  box-shadow:inset 0 .5px 0 rgba(255,255,255,.16),
             inset 0 0 0 .5px rgba(255,255,255,.10),
             0 1px 2px rgba(0,0,0,.34), 0 4px 12px rgba(0,0,0,.26);
  color:rgba(255,255,255,.62);
  transition:background .18s cubic-bezier(.25,.8,.25,1), color .18s cubic-bezier(.25,.8,.25,1),
             box-shadow .18s cubic-bezier(.25,.8,.25,1), transform .14s cubic-bezier(.25,.8,.25,1); }
.gmr button.gmr-clear::before, .gmr button.gmr-clear::after {
  content:""; position:absolute; left:50%; top:50%;
  width:11px; height:1.6px; border-radius:2px; background:currentColor;
  transition:background .18s cubic-bezier(.25,.8,.25,1); }
.gmr button.gmr-clear::before { transform:translate(-50%,-50%) rotate(45deg); }
.gmr button.gmr-clear::after  { transform:translate(-50%,-50%) rotate(-45deg); }
.gmr button.gmr-clear:hover { color:#fff; background:rgba(255,59,48,.92);
  box-shadow:inset 0 .5px 0 rgba(255,255,255,.34),
             0 2px 6px rgba(255,59,48,.40), 0 6px 16px rgba(255,59,48,.28); }
.gmr button.gmr-clear:active { transform:scale(.9); background:rgba(255,59,48,.78); }
/* 图片槽位不再固定 9 列：按数量流式换行。整格按 1.5× 放大（64→96 宽、48→72 高）
   ——媒体架是独立素材区，原来 64×48 的格子连缩略图都看不清。宽高必须同倍放大：
   缩略图是 width:100% + object-fit:contain，只放大宽会让高继续卡住横图的显示尺寸。 */
.gmr .gcast-grid.img { grid-template-columns:repeat(auto-fill, minmax(96px, 1fr)); }
.gmr .gcast-grid.img .gcast-thumb { height:72px; }
/* 「＋ 添加」格的样式在共用样式表 CAST_CSS 里（Qwen 编辑台同用一份） */
/* 别名输入框：默认隐形（看起来就是一行文字），聚焦才显形——卡片本来就窄，
   不能为了一个偶尔改的字段常驻描边。 */
.gmr-alias { flex:1 1 auto; min-width:0; width:100%; box-sizing:border-box;
  background:transparent; border:1px solid transparent; border-radius:4px;
  color:inherit; font-family:inherit; font-size:10px; padding:1px 3px;
  text-overflow:ellipsis; }
.gmr-alias:hover { border-color:var(--h3-line); }
.gmr-alias:focus { outline:none; border-color:var(--h3-accent); background:var(--h3-bg); }
.gmr-alias.bad { border-color:#ff9f43; color:#ff9f43; }
.gmr-alias.bad:focus { border-color:#ff9f43; }
.gmr-alias::placeholder { color:var(--h3-dim); opacity:.65; }
.gmr-mini { all:unset; pointer-events:auto; cursor:pointer; margin-left:auto;
  font-size:10px; color:var(--h3-dim); padding:2px 7px; border-radius:5px;
  border:1px solid var(--h3-line); }
.gmr-mini:hover { color:var(--h3-accent); border-color:var(--h3-accent); }
/* 面板条上的胶囊小设置（帧率 / 音频输出编号）：标签内嵌在胶囊里，空态只有
   一颗暗标签，聚焦整颗胶囊才描边，尺寸与 gmr-mini 同一档次，不抢视觉。 */
.gmr-chip { display:inline-flex; align-items:center; gap:4px; margin-left:6px;
  padding:2px 8px; border-radius:999px; border:1px solid var(--h3-line);
  background:transparent;
  transition:border-color .15s cubic-bezier(.25,.8,.25,1), background .15s; }
.gmr-chip:hover { border-color:var(--h3-line); }
.gmr-chip:focus-within { border-color:var(--h3-accent); background:var(--h3-bg); }
.gmr-chiplab { font-size:10px; color:var(--h3-dim); user-select:none; cursor:default; }
/* 本段指示：素材是按分段记忆的，得让人看见现在摆的是哪一段的素材。
   颜色与「锁定加发光」这套画法，跟时间轴上那一段的紫环、文案台上那颗胶囊
   是同一个信号的三种画法 —— 规范色从成片监视器导入，不在这里另抄一份。 */
.gmr-segchip { margin-left:6px; padding:2px 8px; border-radius:999px;
  font-size:10px; color:var(--h3-dim); border:1px dashed var(--h3-line);
  white-space:nowrap; flex:none; }
.gmr-segchip.on { color:${CUR_SEG}; border-style:solid;
  border-color:${CUR_SEG_DIM}; background:${CUR_SEG_DIM}; }
.gmr-segchip.on.locked { box-shadow:0 0 7px 1px ${CUR_SEG_DIM}; border-width:2px;
  padding:1px 7px; }
/* 覆盖共用表里的 .gcast input[type=number] 基础样式（62px 宽、带底色边框，
   特异性比单类名高，不提级选择器就会把胶囊里的输入框撑回大块头——实测教训）。
   三层类名 (0,3,0) 压过 (0,2,1)。 */
.gmr .gmr-chip .gmr-num {
  all:unset; pointer-events:auto; display:inline; width:30px; box-sizing:border-box;
  height:auto; padding:0; border:0; border-radius:0; background:transparent;
  color:var(--h3-txt); font-family:inherit; font-size:11px; text-align:center;
  -moz-appearance:textfield; appearance:textfield; }
/* 隐藏 number 输入的上下箭头：占地方且与胶囊风格不符 */
.gmr .gmr-chip .gmr-num::-webkit-outer-spin-button,
.gmr .gmr-chip .gmr-num::-webkit-inner-spin-button {
  -webkit-appearance:none; margin:0; }
.gmr .gmr-chip .gmr-num:focus-visible { outline:none; border:0; }
/* 瞬态提示条：拖入被拒、上传失败这类「什么都没发生」必须说出来——静默丢弃
   是最难排查的一类问题，用户只会得出「这图导不进来」的结论。
   底部居中，右侧留出清除钮（26px + 8px）的位子，两者不会压到一起。 */
.gmr-toast { position:absolute; left:50%; bottom:10px; transform:translate(-50%,6px);
  max-width:calc(100% - 88px); box-sizing:border-box; z-index:8;
  padding:5px 10px; border-radius:7px; text-align:center;
  font-size:11px; line-height:1.45;
  background:rgba(32,32,34,.95); color:#eaeaec;
  border:1px solid rgba(255,255,255,.16);
  -webkit-backdrop-filter:blur(10px) saturate(180%);
  backdrop-filter:blur(10px) saturate(180%);
  box-shadow:0 4px 14px rgba(0,0,0,.42);
  opacity:0; pointer-events:none;
  transition:opacity .18s cubic-bezier(.25,.8,.25,1), transform .18s cubic-bezier(.25,.8,.25,1); }
.gmr-toast.on { opacity:1; transform:translate(-50%,0); }
.gmr-toast.bad { background:rgba(58,28,28,.96); color:#ffb3b3;
  border-color:rgba(160,82,82,.9); }
`;

let cssInjected = false;
export function injectRackCSS() {
  if (cssInjected) return;
  const s = document.createElement("style");
  s.id = "gmr-css";
  /* 面板样式表原样注入（gcast-css 自带幂等标记），再叠媒体架专属的少量规则 */
  if (!document.getElementById("gcast-css")) {
    const base = document.createElement("style");
    base.id = "gcast-css";
    base.textContent = CAST_CSS;
    document.head.append(base);
  }
  s.textContent = RACK_CSS;
  document.head.append(s);
  cssInjected = true;
}
