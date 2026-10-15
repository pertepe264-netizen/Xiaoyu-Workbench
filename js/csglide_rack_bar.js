/* ============================================================
 * H3 媒体架 — 面板条
 * ------------------------------------------------------------
 * 一行装四样：本段指示、按提示词排序、帧率/音频输出编号两颗胶囊、清空钮。
 * 帧率与音频编号是节点级设置，随 rack_data 一起序列化（commit 序列化整个 st）。

 * 「音频输出编号」的自愈也在这里——它认的是那个输入框，挪不出去。
 * ============================================================ */
import { el, MAX_SLOTS } from "./csglide_rack_core.js";

export function installBar(ui) {
  const st = ui.st;

  /* ---- 一键清空：移除媒体架里的全部素材（图片/视频/音频）。只清素材，
     不碰设置类状态；与导演台原「一键清空」同一语义：一次点击即生效，不弹确认。
     小圆形红叉钮，固定在面板右下角。 ---- */
  const bClear = el("button", "gmr-clear");
  bClear.setAttribute("aria-label", "清空所有素材");
  bClear.title = "清空所有素材：图片、视频、音频";
  bClear.onclick = () => {
    st.slots.images = [];
    st.slots.videos = [];
    st.slots.audios = [];
    ui.rebuildMedia();
    ui.renderImages(); ui.commit();
  };

  /* ---- 按提示词排序：把素材搬到提示词写定的编号上（实现见 csglide_rack_sort.js） ---- */
  const bSort = el("button", "gmr-mini", "按提示词排序");
  bSort.title = "按导演台提示词里写死的编号，重排本架素材\n"
    + "· 认的是提示词里显式写的 @图片N / @视频N / @音频N，以及它左右最近的那个人名/物名\n"
    + "· 命中靠架上现有的别名与文件名，提示词里写了个架上没有的名字 → 不动那一格\n"
    + "· 多个节点（比如分段）同时接本架时按画布顺序取，编号被先写的那个占住\n"
    + "例：<Subject 1> 为 @图片3 中的陈九一 → 把别名叫陈九一的那张图搬到第 3 格\n"
    + "顺序 = 编号，所以 改完导演台提示词，来这里点一下就对齐了。";
  bSort.onclick = () => ui.sortByPrompt?.();

  /* 胶囊小控件：标签内嵌，空态不占视觉，聚焦才描边。 */
  const mkNum = (label, title, onchg) => {
    const chip = el("label", "gmr-chip");
    chip.title = title;
    const inp = el("input", "gmr-num");
    inp.type = "number";
    inp.onchange = onchg;
    chip.append(el("span", "gmr-chiplab", label), inp);
    return { chip, inp };
  };
  const fpsCtl = mkNum("帧率",
    "参考视频自定帧率：留空/0 = 跟随导演台面板的抽帧帧率；填数（如 24）= 该媒体架所有参考视频按此帧率抽帧（导演台与素材取帧都生效）",
    () => {
      const v = parseFloat(fpsCtl.inp.value);
      st.fps = v > 0 ? v : 0;
      if (!st.fps) fpsCtl.inp.value = "";
      ui.commit();
    });
  fpsCtl.inp.min = "0"; fpsCtl.inp.max = "60"; fpsCtl.inp.step = "0.1";

  const audCtl = mkNum("音频",
    "原始音频输出编号：输出第 N 个视频槽位（@视频N）的原始音轨（按槽位起止修剪），供外部视频合成使用。0 = 不输出",
    () => {
      const v = parseInt(audCtl.inp.value, 10);
      st.audio_out = v > 0 ? v : 0;
      if (!st.audio_out) audCtl.inp.value = "";
      /* 手动改编号 = 用户重新指定素材，同步自愈基准，否则 commit 里的
         fixAudioOut 会把这次修改当成"槽位被换"改回去 */
      setAudioBaseline(st.audio_out > 0 ? st.slots.videos[st.audio_out - 1] : null);
      ui.commit();
    });
  audCtl.inp.min = "0"; audCtl.inp.max = String(MAX_SLOTS); audCtl.inp.step = "1";
  const fpsIn = fpsCtl.inp, audIn = audCtl.inp;   /* load() 恢复时沿用这两个引用 */

  /* 本段指示：让用户一眼看出「架子上的素材属于哪一段」——看不见的话，
     切段后素材悄悄换了一批，用户会以为素材丢了。 */
  const segChip = el("span", "gmr-segchip", "全片共用");
  segChip.title = "素材按分段记忆：切到哪一段，架子上就是哪一段的素材";

  const bar = el("div", "gmr-bar");
  bar.append(el("div", "gcast-label", "素材"), segChip,
             bSort, fpsCtl.chip, audCtl.chip, bClear);

  /* 音频输出编号自愈。编号按「第几个视频槽位」引用，素材删除/拖拽换位后
     可能悬空或指错素材。每次 commit 前校一遍：
     · 指向的槽位仍是原文件 → 记住它，不动；
     · 槽位空了或被换成别的文件 → 在全架按文件名找原文件，找到就把编号跟到
       新位置（toast 告知）；找不到（素材真被删/被替换）→ 清零并提示。
     绝不静默地把音频换成另一条素材。手动改编号时会同步基准，不会被误改回。 */
  let lastAudioFile = "";
  function setAudioBaseline(slot) { lastAudioFile = (slot && slot.file) || ""; }
  function fixAudioOut() {
    const slot = st.slots.videos[st.audio_out - 1];
    const file = (slot && slot.file) || "";
    if (file && (!lastAudioFile || file === lastAudioFile)) {
      lastAudioFile = file;
      return;
    }
    if (!st.audio_out) { lastAudioFile = ""; return; }
    if (lastAudioFile) {
      const i = st.slots.videos.findIndex((s) => s && s.file === lastAudioFile);
      if (i >= 0) {
        st.audio_out = i + 1;
        audIn.value = String(st.audio_out);
        ui.toast(`音频输出编号已自动改为 ${st.audio_out}：原槽位已空，素材挪到了新位置`);
        return;
      }
    }
    st.audio_out = 0;
    audIn.value = "";
    lastAudioFile = "";
    ui.toast("音频输出编号指向的视频已不在架上，已自动清零（0 = 不输出）", true);
  }

  ui.bar = bar;
  ui.segChip = segChip;
  ui.bSort = bSort;
  ui.fpsIn = fpsIn;
  ui.audIn = audIn;
  ui.fixAudioOut = fixAudioOut;
  ui.setAudioBaseline = setAudioBaseline;
}
