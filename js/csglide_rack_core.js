/* ============================================================
 * H3 媒体架 — 核心常量与无状态工具
 * ------------------------------------------------------------
 * 只放「不认识面板」的东西：常量、DOM 小工具、上传 / 探测 / 格式判定。
 * 任何需要 ui（槽位状态、commit、toast）的函数都不在这里 —— 它们各归
 * 自己的分区文件，避免本文件被反向依赖成一个新的大杂烩。
 * ============================================================ */
import { api } from "../../scripts/api.js";
import { EXT_KIND } from "./csglide_cast.js";

export const NODE_ID = "CSGlideH3MediaRackCS";
export const SUBFOLDER = "cglide";
export const MAX_SLOTS = 64;   // 每类硬上限，与后端 REF_LIMIT 一致
export const MIN_W = 620, MIN_H = 560;

/* 与导演台同一帧率口径：修剪窗口的帧数读出与 17k+5 网格都按它算 */
export const FPS = 24;
export const CHROMA_WARN = "4:2:0";

export function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* 按钮上的一次性反馈：这个面板没有 toast 通道，临时改按钮文字是最轻的提示，
   也免得用户点了没反应以为坏了。 */
export function flashBtn(btn, text) {
  if (!btn || btn.dataset.busy) return;
  const old = btn.textContent;
  btn.dataset.busy = "1";
  btn.textContent = text;
  setTimeout(() => {
    btn.textContent = old;
    delete btn.dataset.busy;
  }, 1200);
}

export function viewURL(file) {
  if (!file) return "";
  const i = file.lastIndexOf("/");
  const sub = i >= 0 ? file.slice(0, i) : "";
  const name = i >= 0 ? file.slice(i + 1) : file;
  return api.apiURL(`/view?filename=${encodeURIComponent(name)}&subfolder=${encodeURIComponent(sub)}&type=input`);
}

export const fmt = (s) => (Math.round(s * 100) / 100).toFixed(2) + "s";

/* 文件类型判定：先信 MIME，MIME 为空（系统没注册该扩展名）时回落到
   EXT_KIND 表。表里也没有就返回空 —— 由调用方 toast 说清「不支持」。

   EXT_KIND 由 csglide_cast.js 共用导入，本节点不再自带副本。曾经这里抄了
   一份窄表（漏 avif/tif/tiff 与大量视频音频扩展名），与 ACCEPT_* 的不一致
   就是「文件对话框选得到、拖进来没反应」的根因——拖拽走 fileKind()，MIME
   为空时回落到该表，表里没有就被静默丢弃。 */
export function fileKind(f) {
  const t = f?.type || "";
  if (t.startsWith("image/")) return "image";
  if (t.startsWith("video/")) return "video";
  if (t.startsWith("audio/")) return "audio";
  const ext = (f?.name || "").split(".").pop().toLowerCase();
  return EXT_KIND[ext] || "";
}

/* 参考视频修剪后会被重新吸附到 17k+5 网格（导演台消费侧），所以任意修剪
   静默变成 5 帧、22 帧等。读出位显示真实存活帧数。 */
export const gridDown = (n) => { let g = Math.floor(n); while (g > 0 && g % 17 !== 5) g--; return g; };
export const gridSpans = (dur) => {
  const out = [];
  for (let g = 5; ; g += 17) {
    const sp = (g - 1) / FPS;
    if (sp > dur) break;
    out.push({ g, sp });
  }
  return out;
};

export async function uploadFile(file) {
  const body = new FormData();
  body.append("image", file, file.name);
  body.append("subfolder", SUBFOLDER);
  body.append("type", "input");
  const res = await api.fetchApi("/upload/image", { method: "POST", body });
  if (res.status !== 200) throw new Error(`上传失败 (${res.status})`);
  const d = await res.json();
  /* 兜底拼 subfolder：个别 ComfyUI 版本的 /upload/image 响应不带 subfolder
     字段（或为空），只回填裸文件名会让 viewURL 丢掉子目录 → 之后每次渲染
     /view 都 404，缩略图永远是裂图。请求里明确传了 SUBFOLDER，成功就按它算。 */
  const sub = d.subfolder || SUBFOLDER;
  return sub ? `${sub}/${d.name}` : d.name;
}

export function pickFile(accept) {
  return new Promise((resolve) => {
    const inp = document.createElement("input");
    inp.type = "file"; inp.accept = accept;
    inp.onchange = () => {
      const list = inp.files ? Array.from(inp.files) : [];
      resolve(list[0] || null);
    };
    inp.click();
  });
}

export function probeDuration(url, isVideo) {
  return new Promise((resolve) => {
    const m = document.createElement(isVideo ? "video" : "audio");
    m.preload = "metadata";
    m.onloadedmetadata = () => resolve(Number.isFinite(m.duration) ? m.duration : 0);
    m.onerror = () => resolve(0);
    m.src = url;
  });
}
