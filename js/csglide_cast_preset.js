import { app } from "../../scripts/app.js";
import { pickFile, uploadFile, viewURL } from "./csglide_ui_kit.js";

/* ============================================================
 * 预设文件：保存 / 打包 / 载入
 * ------------------------------------------------------------
 * 从 csglide_cast.js 的 buildUI 里整块搬出来。
 *
 * 跨块的东西一律走 ui.<名>：ui 是 buildUI 建的那一个共享 bag。可变状态
 * 在它上面只有一个字段，所以本文件与别的模块改的是同一份。
 * 本块自己声明的东西保持裸名（函数声明要靠提升），末尾统一挂到 ui 上，
 * 顺序与原来在 buildUI 里的先后一致 —— 搬出去之后，谁先声明谁后声明没变。
 * ============================================================ */

export function installPreset(ui) {
  /* ---- preset files ------------------------------------------------
   *
   * Everything happens in the browser: a .json holds the state and points
   * at media by filename, a .h3pack is a zip with the media inside. The
   * media itself is read back out of ComfyUI through /view and pushed
   * back in through /upload on import, so no server route is needed.
   *
   * Reference images are re-encoded to JPEG inside a pack -- they are
   * heading into a VAE, so the artifacts are irrelevant and the saving is
   * large. First/last KEYFRAMES are deliberately left alone: those are the
   * chunk-continuity frames, and JPEG damage there propagates into every
   * generation built on top of them.
   */

  const JPEG_QUALITY = 0.92;

  /* 单段预设那套「记住上次写到哪个文件」的状态（fileHandle / fileIsPack /
     fileLabel）随它一起删了：整片那一路记在 ui.projHandle / ui.projLabel 上，
     各管各的，不必再保留一份没人读的。 */

  /* -- minimal zip, stored (media is already compressed) -- */
  const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
      t[n] = c >>> 0;
    }
    return t;
  })();

  function crc32(buf) {
    let c = 0xFFFFFFFF;
    for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
    return (c ^ 0xFFFFFFFF) >>> 0;
  }

  function zipWrite(entries) {
    const enc = new TextEncoder();
    const parts = [], central = [];
    let offset = 0;

    for (const e of entries) {
      const name = enc.encode(e.name);
      const data = e.data;
      const crc = crc32(data);

      const lh = new DataView(new ArrayBuffer(30));
      lh.setUint32(0, 0x04034b50, true);
      lh.setUint16(4, 20, true);        // version needed
      lh.setUint16(8, 0, true);         // stored
      lh.setUint32(14, crc, true);
      lh.setUint32(18, data.length, true);
      lh.setUint32(22, data.length, true);
      lh.setUint16(26, name.length, true);
      parts.push(new Uint8Array(lh.buffer), name, data);

      const cd = new DataView(new ArrayBuffer(46));
      cd.setUint32(0, 0x02014b50, true);
      cd.setUint16(4, 20, true);
      cd.setUint16(6, 20, true);
      cd.setUint16(10, 0, true);
      cd.setUint32(16, crc, true);
      cd.setUint32(20, data.length, true);
      cd.setUint32(24, data.length, true);
      cd.setUint16(28, name.length, true);
      cd.setUint32(42, offset, true);
      central.push(new Uint8Array(cd.buffer), name);

      offset += 30 + name.length + data.length;
    }

    let cdSize = 0;
    for (const c of central) cdSize += c.length;
    const eo = new DataView(new ArrayBuffer(22));
    eo.setUint32(0, 0x06054b50, true);
    eo.setUint16(8, entries.length, true);
    eo.setUint16(10, entries.length, true);
    eo.setUint32(12, cdSize, true);
    eo.setUint32(16, offset, true);

    return new Blob([...parts, ...central, new Uint8Array(eo.buffer)],
                    { type: "application/zip" });
  }

  async function zipRead(buffer) {
    const dv = new DataView(buffer);
    const bytes = new Uint8Array(buffer);
    let eocd = -1;
    for (let i = dv.byteLength - 22; i >= 0 && i > dv.byteLength - 66000; i--) {
      if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error("不是有效的 zip 文件");

    const count = dv.getUint16(eocd + 10, true);
    let p = dv.getUint32(eocd + 16, true);
    const dec = new TextDecoder();
    const out = new Map();

    for (let i = 0; i < count; i++) {
      const nameLen = dv.getUint16(p + 28, true);
      const extraLen = dv.getUint16(p + 30, true);
      const commentLen = dv.getUint16(p + 32, true);
      const method = dv.getUint16(p + 10, true);
      const compSize = dv.getUint32(p + 20, true);
      const local = dv.getUint32(p + 42, true);
      const name = dec.decode(bytes.subarray(p + 46, p + 46 + nameLen));

      const lNameLen = dv.getUint16(local + 26, true);
      const lExtraLen = dv.getUint16(local + 28, true);
      const start = local + 30 + lNameLen + lExtraLen;
      let data = bytes.subarray(start, start + compSize);

      if (method === 8) {
        const ds = new DecompressionStream("deflate-raw");
        const blob = new Blob([data]).stream().pipeThrough(ds);
        data = new Uint8Array(await new Response(blob).arrayBuffer());
      } else if (method !== 0) {
        throw new Error(`不支持的 zip 压缩方式：${name}`);
      }
      out.set(name, data);
      p += 46 + nameLen + extraLen + commentLen;
    }
    return out;
  }

  /* -- media helpers -- */

  async function toJpeg(blob) {
    try {
      const bmp = await createImageBitmap(blob);
      const c = document.createElement("canvas");
      c.width = bmp.width; c.height = bmp.height;
      c.getContext("2d").drawImage(bmp, 0, 0);
      const out = await new Promise((r) => c.toBlob(r, "image/jpeg", JPEG_QUALITY));
      bmp.close?.();
      return out;
    } catch (e) { return null; }
  }

  const bytesOf = async (blob) => new Uint8Array(await blob.arrayBuffer());

  function busy(text) {
    /* 存盘过程中锁住「整片 ▾」，别让人在写到一半时再点一次；进度就写在按钮
       自己身上 —— 原来的写法是把字打到指挥条那格「预设文件名」里，那格随
       「本段预设」整套删掉了，而"正在保存工程"这句话必须还有个去处：
       它和触发它的按钮待在一起最不容易被漏看。 */
    if (!ui.bProj) return;
    ui.bProj.disabled = !!text;
    ui.bProj.style.opacity = text ? ".5" : "1";
    ui.bProj.textContent = text || "工程 ▾";
  }

  /* -- build a pack: state plus every referenced file -- */
  /* Collects media into zip entries, rewriting each item's filename to point
   * at the copy inside the pack. The dedup map is passed in rather than owned,
   * so a whole project can share ONE map across every shot: a reference sheet
   * used in ten shots is fetched once and stored once. */
  function makeAssetAdder(entries, seen, used) {
    return async function add(item, keepOriginal) {
      if (!item || !item.file) return;
      const src = item.file;
      if (seen.has(src)) { item.file = seen.get(src); return; }

      let blob;
      try { blob = await (await fetch(viewURL(src))).blob(); }
      catch (e) { console.warn("[H3 Studio] could not read " + src, e); return; }

      let name = src.split("/").pop();
      if (!keepOriginal && /\.(png|jpe?g|webp|bmp)$/i.test(name)) {
        const j = await toJpeg(blob);
        if (j && j.size < blob.size) {
          blob = j;
          name = name.replace(/\.[^.]+$/, "") + ".jpg";
        }
      }
      let unique = name, n = 2;
      while (used.has(unique)) {
        unique = name.replace(/(\.[^.]+)$/, `_${n++}$1`);
      }
      used.add(unique);
      entries.push({ name: "assets/" + unique, data: await bytesOf(blob) });
      seen.set(src, unique);
      item.file = unique;
    };
  }

  async function packSlots(slots, add) {
    if (!slots) return;
    await add(slots.first, true);                 // keyframes stay lossless
    await add(slots.last, true);
    for (const it of slots.images || []) await add(it, false);
    for (const it of slots.videos || []) await add(it, false);
    for (const it of slots.audios || []) await add(it, false);
  }

  async function buildPack() {
    const state = JSON.parse(JSON.stringify(ui.st));
    const entries = [];
    const add = makeAssetAdder(entries, new Map(), new Set());

    await packSlots(state.slots, add);

    const enc = new TextEncoder();
    entries.unshift({ name: "state.json", data: enc.encode(JSON.stringify(state, null, 1)) });
    entries.unshift({ name: "meta.json", data: enc.encode(JSON.stringify({
      app: "H3 Studio", version: 1,
      mode: state.mode, width: state.width, height: state.height,
      length: state.length, saved: new Date().toISOString(),
    }, null, 1)) });

    return zipWrite(entries);
  }

  async function pickFileForOpen() {
    if (window.showOpenFilePicker) {
      try {
        const [h] = await window.showOpenFilePicker({
          types: [{ description: "H3 Studio preset or pack",
                    accept: { "application/json": [".json"], "application/zip": [".h3pack", ".h3projpack", ".zip"] } }],
        });
        const f = await h.getFile();
        return { file: f, handle: h };
      } catch (e) {
        if (e && e.name === "AbortError") return null;
      }
    }
    const f = await pickFile(".json,.h3pack,.h3projpack,.zip");
    return f ? { file: f, handle: null } : null;
  }

  /* Read a .h3.json or .h3pack into a plain state object. 抽成独立函数，
     「导入分段」（conflict.js）与「打开整片」走同一条解析路径 —— 包括
     .h3pack 里的素材还原。 */
  async function stateFromFile(file) {
    const isPack = /\.(h3pack|zip)$/i.test(file.name);
    if (!isPack) {
      const parsed = JSON.parse(await file.text());
      /* 工程文件是 {meta, name, idx, shots}——顶层没有 state，下面的白名单会
       * 把每个字段静默重置为默认值。认出它并指到该去的按钮，而不是假装载入成功。 */
      if (Array.isArray(parsed.shots)) {
        throw new Error("这是工程文件（含多个分段），请到「工程」面板里用「打开…」载入它");
      }
      return { state: parsed.state || parsed, isPack: false };
    }
    const map = await zipRead(await file.arrayBuffer());
    /* 打包工程存的是 project.json 不是 state.json。少了这条，下面的报错会让人
     * 以为文件坏了，实际只是按钮用错了。 */
    if (map.has("project.json")) {
      throw new Error("这是打包的工程文件，请到「工程」面板里用「打开…」载入它");
    }
    const raw = map.get("state.json");
    if (!raw) throw new Error("打包文件缺少 state.json");
    const state = JSON.parse(new TextDecoder().decode(raw));

    /* push the bundled media back into ComfyUI's input folder so the
     * Python side can find it, then repoint the state at the uploads */
    const remap = new Map();
    for (const [name, data] of map) {
      if (!name.startsWith("assets/")) continue;
      const base = name.slice(7);
      const blob = new Blob([data]);
      try {
        remap.set(base, await uploadFile(new File([blob], base)));
      } catch (e) {
        console.warn("[H3 Studio] could not restore " + base, e);
      }
    }
    const fix = (item) => {
      if (item && item.file && remap.has(item.file)) item.file = remap.get(item.file);
    };
    const sl = state.slots || {};
    fix(sl.first); fix(sl.last);
    (sl.images || []).forEach(fix);
    (sl.videos || []).forEach(fix);
    (sl.audios || []).forEach(fix);
    return { state, isPack: true };
  }

  /* Same dialog, several files at once - importing a finished film one shot
   * at a time was the tedious part. */
  async function pickFilesForOpen() {
    if (window.showOpenFilePicker) {
      try {
        const hs = await window.showOpenFilePicker({
          multiple: true,
          types: [{ description: "H3 Studio preset or pack",
                    accept: { "application/json": [".json"], "application/zip": [".h3pack", ".h3projpack", ".zip"] } }],
        });
        const out = [];
        for (const h of hs) out.push(await h.getFile());
        return out;
      } catch (e) {
        if (e && e.name === "AbortError") return [];
      }
    }
    return (await pickFile(".json,.h3pack,.h3projpack,.zip", true)) || [];
  }

  /* 本段预设整套已删（20261009 用户定）。原本在这里的 saveAs / saveOver /
     doLoad / paintPresetName / plainBlob / defaultName / writeOut / buildPack
     全部是「把**当前这一段**单独存成预设文件」这一件事的零件：
       · 每段配置本来就随工程存在 node.properties 里，重开工作流还在；
       · 跨片子搬运的场合，用户是连节点一起拷的；
       · 它的存在还让「默认预设文件名」那格常年在指挥条上占地方。
     整片的存取（保存 / 另存 / 打包 / 打开 / 导入）走 conflict.js 的工程分支，
     与本文件下面这些 zip / 素材归拢的零件共用，所以只删单段那一路。 */


  /* 挂出去：别的块通过 ui.<名> 取。放在最后 —— 等同于原来在这一块
     结束时才声明完，之前的块拿不到，之后的块拿得到。 */
  Object.assign(ui, { JPEG_QUALITY, CRC_TABLE, crc32, zipWrite, zipRead, toJpeg, bytesOf, busy, makeAssetAdder, packSlots, buildPack, pickFileForOpen, stateFromFile, pickFilesForOpen });
}
