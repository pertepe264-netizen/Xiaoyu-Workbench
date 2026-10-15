"""MiniMax H3 Preview Override — kjnodes-style live preview for MiniMax H3.

合并自 ComfyUI-MiniMaxH3-PreviewOverride（班长全家桶集成版，原 node_id 保持不变）。

Port of KJNodes' "Model Preview Override" for the MiniMax H3 AV model.

Instead of the default 512px first-frame preview, this node attaches an
OUTER_SAMPLE wrapper to the model. On every sampler step the H3 *video* latent
(the first stream of the NestedTensor) is decoded and pushed to a DOM widget
on this node via the "minimax_h3_preview_override" websocket event, at full
sampler resolution (capped by max_resolution).

Decoding paths:
  0. Optional tiny VAE (TAEHV/TAESD from MiniMax H3 Tiny VAE Loader) -> true-RGB
     previews via vae.decode(). Only tiny decoders are accepted: a full VAE (e.g.
     minimax_h3_video_vae) is IGNORED, because decoding it every step while the DiT
     is resident blows up VRAM (that was the original VRAM-killer).
  1. Animated Latent2RGB     -> preview_frames temporal slices decoded with the
                                H3 latent_rgb_factors (24ch), played back as
                                WebP/MP4 at preview_fps (preview_frames > 1).
  2. Single Latent2RGB frame -> one JPEG per step (preview_frames = 1).

For full-quality frames use the regular VAEDecode after sampling.

The payload schema matches KJNodes' so the same JS widget conventions apply:
boundary-0 message carries the sigma schedule, step messages carry the image.
"""

import base64
import io as pyio
import logging
import queue
import threading
import time

import numpy as np
import torch

import comfy.patcher_extension
import comfy.utils
import folder_paths
import latent_preview
from comfy_api.latest import io
from PIL import Image, ImageOps

try:
    from server import PromptServer
except ImportError:
    PromptServer = None


def _suppressed_preview_image(self_, preview_format, x0):
    return None


class _AsyncPreviewEncoder:
    """Off-thread encoder. Bounded FIFO drops-on-full so the sampler never blocks on us."""

    _STOP = object()

    def __init__(self, max_in_flight=2):
        self.q = queue.Queue(maxsize=max_in_flight)
        self.thread = threading.Thread(target=self._run, name="minimax_h3_preview_encoder", daemon=True)
        self.thread.start()

    def submit(self, fn):
        try:
            self.q.put_nowait(fn)
            return True
        except queue.Full:
            return False

    def _run(self):
        while True:
            item = self.q.get()
            if item is self._STOP:
                return
            try:
                item()
            except Exception:
                logging.exception("[MiniMax H3 Preview Override] async encoder error")

    def shutdown(self, drain_timeout=5.0):
        try:
            self.q.put(self._STOP, timeout=drain_timeout)
        except queue.Full:
            pass
        self.thread.join(timeout=drain_timeout)


def _get_core_previewer(load_device, latent_format):
    # Walk past custom-node hooks on get_previewer to reach the unwrapped core function.
    fn = latent_preview.get_previewer
    seen = set()
    while hasattr(fn, "__wrapped__") and id(fn) not in seen:
        seen.add(id(fn))
        fn = fn.__wrapped__
    return fn(load_device, latent_format)


def _video_part(x0, latent_shapes):
    """Extract the video stream from an H3 latent.

    Nested latents are packed flat ([B, 1, N]) before sampling, and an OUTER_SAMPLE
    wrapper that replaces the callback receives the *packed* tensor at step boundaries
    (the sampler's own unpack wrapper is bypassed). Handle both the already-unpacked
    NestedTensor and the flat-packed tensor here.
    """
    if getattr(x0, "is_nested", False):
        return x0.unbind()[0]
    if latent_shapes is not None and len(latent_shapes) > 1:
        return comfy.utils.unpack_latents(x0, latent_shapes)[0]
    return x0


def _audio_part(x0, latent_shapes):
    """取出 H3 联合潜空间里的音频流（与 _video_part 同源，取第 2 条）。

    H3 是音视联合扩散，每一步的 x0 里同时含画面和声音，所以不用等到采样结束
    就能听出这一句台词对不对。
    """
    if getattr(x0, "is_nested", False):
        parts = x0.unbind()
        return parts[1] if len(parts) > 1 else None
    if latent_shapes is not None and len(latent_shapes) > 1:
        parts = comfy.utils.unpack_latents(x0, latent_shapes)
        return parts[1] if len(parts) > 1 else None
    return None


def _audio_stream_scale(model):
    """采样期间音频流被放大的倍数；取音频流必须除回去。

    H3 是音视联合扩散，但两条流各自有 sigma 偏移（shift 12.0 / audio_shift
    3.0）。为了让整条打包潜空间能按单一调度跑，ComfyUI 在**进入采样时**把音频
    流乘上 audio_scale = shift / audio_shift = 4.0，**出采样时**才除回去
    （comfy/samplers.py inner_sample 的 process_latent_in / process_latent_out）。

    采样回调拿到的 x0 是「采样中」的视图，音频流还是放大 4 倍的值。直接拿去
    解码，VAE 里的 z * std + mean 会被推出分布——相关性实测从 1.0 掉到 0.47，
    峰值硬削到 1.0，听起来就是一片噪声。

    非联合模型（latent_shapes 只有一条）返回 1.0，无副作用。
    """
    fn = getattr(model, "audio_scale", None)
    if not callable(fn):
        return 1.0
    try:
        s = float(fn())
    except Exception:
        return 1.0
    if s != s or abs(s) < 1e-6:      # NaN / 0
        return 1.0
    return s


def _wav_bytes(waveform, sample_rate):
    """波形 → 16-bit PCM WAV 字节（纯标准库，不依赖 torchaudio / soundfile）。"""
    import io as _io
    import wave

    w = waveform.detach().to("cpu").float()
    if w.dim() == 3:
        w = w[0]                      # [B, C, L] → [C, L]
    w = w.transpose(0, 1).clamp(-1.0, 1.0)   # [C, L] → [L, C]
    pcm = (w * 32767.0).round().to(torch.int16).contiguous().numpy()
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(int(pcm.shape[1]))
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _cpu_audio_model(vae):
    """把音频 VAE 弄到 CPU 上（只搬一次，之后一直留在 CPU）。

    为什么不走 comfy.sd.VAE.decode / vae_decode_audio：那条路在显存不够时会
    回退到 decode_tiled_——那是给图像 VAE 写的 2D 分块器，而 H3 音频解码输出
    是 3D [B, 2, L]，里面 mask.shape[3] 直接越界抛 IndexError，这一帧就没声音。

    CPU 解码实测 2.4~3.2 倍实时（10 秒音频约 4.2 秒），够快，于是干脆整段都解，
    不截窗口——再绕开显卡，采样中途就完全没有显存风险了。

    搬模型走 ComfyUI 自己的 patcher.detach()，账目对得上；下次别的节点用到它，
    load_models_gpu 会把它搬回显卡，所以是会自动恢复的。
    """
    model = getattr(vae, "first_stage_model", vae)
    if model is None:
        return None
    try:
        if next(model.parameters()).device.type == "cpu":
            return model
    except StopIteration:
        return model
    try:
        vae.patcher.detach()
    except Exception:
        pass
    try:
        if next(model.parameters()).device.type != "cpu":
            model.to("cpu")
    except Exception:
        pass
    return model


class _AsyncAudioDecoder:
    """后台线程解码音频。采样线程只把 latent 拷到 CPU 就返回，一毫秒都不等。

    队列只留最新一条：旧步骤的声音没人听，堆着只会让新声音迟到。
    """

    def __init__(self):
        self.q = queue.Queue(maxsize=1)
        self.thread = threading.Thread(
            target=self._run, name="bz_h3_audio_decoder", daemon=True)
        self.thread.start()

    def submit(self, job):
        try:
            while True:
                self.q.get_nowait()          # 丢掉还没解的旧任务
        except queue.Empty:
            pass
        try:
            self.q.put_nowait(job)
        except queue.Full:
            pass

    def _run(self):
        while True:
            job = self.q.get()
            try:
                self._decode(job)
            except Exception as e:
                logging.warning(
                    f"[MiniMax H3 PreviewOverride] 音频预览解码失败（不影响采样）: {e}")

    def _decode(self, job):
        if PromptServer is None or PromptServer.instance is None:
            return
        model = _cpu_audio_model(job["vae"])
        if model is None:
            return
        with torch.no_grad():
            wav_t = model.decode(job["z"])
        data = _wav_bytes(wav_t, job["sr"])
        if not data:
            return
        # H3 音频 latent 是 40 帧/秒
        secs = float(job["z"].shape[-1]) / 40.0 if job["z"].dim() >= 1 else 0.0
        PromptServer.instance.send_sync(
            "banzhang_h3_audio_preview",
            {
                "node_id": job["node_id"],
                "wav": base64.b64encode(data).decode("ascii"),
                "sample_rate": int(job["sr"]),
                "seconds": round(secs, 1),
                "step": job["step"],
                "total": job["total"],
            },
            PromptServer.instance.client_id,
        )


_AUDIO_WORKER = _AsyncAudioDecoder()


_tae_decode_warned = False


def _tae_decode_to_pil(decoder, video_5d, max_frames=None):
    """True-RGB preview via a tiny decoder (TAEHV / 扁平 TAESD 式), kjnodes-style.

    decoder.decode_video returns [n, 3, H, W]: uint8 for TinyVAEDecoder,
    0..1 float for TAEHVDecoder. Any failure returns [] so the caller falls back to
    Latent2RGB; the warning is logged once, not per step.
    """
    global _tae_decode_warned
    if video_5d.ndim != 5:
        return []
    try:
        t_total = video_5d.shape[2]
        n = t_total
        indices = None
        if max_frames is not None and 0 < max_frames < t_total:
            n = int(max_frames)
            indices = np.linspace(0, t_total - 1, n).round().astype(int).tolist()
        out = decoder.decode_video(video_5d[:1], indices)
        if not torch.is_tensor(out):
            return []
        # [n, 3, H, W] -> [n, H, W, 3]
        frames = out.detach().float()
        if frames.max().item() <= 1.5:  # TAEHV 输出 0..1
            frames = frames.mul_(255.0)
        u8 = frames.clamp_(0, 255).to(torch.uint8).permute(0, 2, 3, 1).cpu().numpy()
        return [Image.fromarray(u8[i]) for i in range(u8.shape[0])]
    except Exception as e:
        if not _tae_decode_warned:
            _tae_decode_warned = True
            logging.warning(f"[MiniMax H3 PreviewOverride] Tiny VAE decode failed, falling back to Latent2RGB: {e}")
        return []


def _vae_approx_list():
    """TAE files available in ComfyUI/models/vae_approx for the vae_name combo."""
    try:
        options = sorted(folder_paths.get_filename_list("vae_approx"))
    except Exception:
        options = []
    return options or ["MiniMax-H3-TAE.safetensors"]


def _load_tiny_vae(vae_name):
    """Load a tiny decoder from models/vae_approx via the KJNodes-style loader.

    comfy.sd.VAE cannot build these files: core's TAEHV derives patch_size from the
    channel count and has no entry for H3's 24 channels, and flat TAESD-style files
    (MiniMax-H3-TAE) aren't recognised at all — core silently produced None there and
    the node kept falling back to Latent2RGB (the "blurry preview" bug).
    """
    from .banzhang_mmh3_tinyvae import load_tiny_vae_decoder

    decoder = load_tiny_vae_decoder(vae_name)
    if decoder is None:
        raise RuntimeError(f"tiny VAE decoder '{vae_name}' unavailable")
    return decoder


def _decode_video_frames_l2rgb(x0_5d, latent_format, max_frames, stride=1):
    """Decode T temporal frames of a 5D video latent via latent_rgb_factors -> list[PIL].

    Mirrors KJNodes' helper: linear project [B,24,T,H,W] with the format's
    latent_rgb_factors, then scale -1..1 -> 0..255 (same math as core preview_to_image).
    """
    if x0_5d.ndim != 5:
        return []
    rgb_factors = getattr(latent_format, "latent_rgb_factors", None)
    if rgb_factors is None:
        return []
    try:
        reshape = getattr(latent_format, "latent_rgb_factors_reshape", None)
        if reshape is not None:
            x0_5d = reshape(x0_5d)
        bias = getattr(latent_format, "latent_rgb_factors_bias", None)
        factors = torch.tensor(rgb_factors, device=x0_5d.device, dtype=x0_5d.dtype).transpose(0, 1)
        bias_t = torch.tensor(bias, device=x0_5d.device, dtype=x0_5d.dtype) if bias is not None else None
        x = x0_5d[0]
        if stride > 1:
            x = x[:, ::stride]
        t_total = x.shape[1]
        if max_frames > 0 and max_frames < t_total:
            indices = np.linspace(0, t_total - 1, max_frames).round().astype(int).tolist()
            x = x[:, indices]
        x = x.movedim(0, -1)
        rgb = torch.nn.functional.linear(x, factors, bias=bias_t)
        rgb.add_(1.0).mul_(127.5).clamp_(0, 255)
        rgb_cpu = rgb.to(torch.uint8).cpu().numpy()
        return [Image.fromarray(rgb_cpu[i]) for i in range(rgb_cpu.shape[0])]
    except Exception:
        return []


# PyPI PyAV wheels typically lack NVENC; probe once at import.
def _probe_nvenc():
    try:
        import av  # noqa
        av.Codec("h264_nvenc", "w")
        return True
    except Exception:
        return False


_NVENC_AVAILABLE = _probe_nvenc()

# NVENC H.264 rejects sub-145x49 inputs at avcodec_open2 — fall back to WebP for small frames.
_NVENC_MIN_W = 145
_NVENC_MIN_H = 49

_nvenc_warned = False


def _encode_mp4_nvenc(frames, fps, max_res):
    """Fragmented MP4 so the browser can decode mid-download. Returns (None, 0, 0) on
    failure (including too-small-for-NVENC), so the caller falls through to WebP."""
    global _nvenc_warned
    if not frames:
        return None, 0, 0
    try:
        import av
    except Exception:
        return None, 0, 0
    pil_frames = []
    for f in frames:
        pf = f if f.mode == "RGB" else f.convert("RGB")
        if max_res and max_res > 0 and (pf.width > max_res or pf.height > max_res):
            pf = ImageOps.contain(pf, (max_res, max_res), Image.LANCZOS)
        pil_frames.append(pf)
    # yuv420p requires even dimensions.
    w0, h0 = pil_frames[0].width, pil_frames[0].height
    out_w, out_h = w0 & ~1, h0 & ~1
    if (out_w, out_h) != (w0, h0):
        pil_frames = [pf.resize((out_w, out_h), Image.LANCZOS) for pf in pil_frames]
    if out_w < _NVENC_MIN_W or out_h < _NVENC_MIN_H:
        return None, 0, 0
    option_candidates = [
        {"preset": "p1", "rc": "vbr", "cq": "23"},
        {"preset": "p1"},
    ]
    last_err = None
    for opts in option_candidates:
        buf = pyio.BytesIO()
        try:
            container = av.open(
                buf, mode="w", format="mp4",
                options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
            )
            stream = container.add_stream("h264_nvenc", rate=int(max(1, fps)))
            stream.width = out_w
            stream.height = out_h
            stream.pix_fmt = "yuv420p"
            stream.options = opts
            for pf in pil_frames:
                for pkt in stream.encode(av.VideoFrame.from_image(pf)):
                    container.mux(pkt)
            for pkt in stream.encode():
                container.mux(pkt)
            container.close()
            return base64.b64encode(buf.getvalue()).decode("ascii"), out_w, out_h
        except Exception as e:
            last_err = e
            continue
    if not _nvenc_warned:
        _nvenc_warned = True
        logging.warning(f"[MiniMax H3 PreviewOverride] NVENC MP4 encode failed, using WebP fallback: {last_err}")
    return None, 0, 0


def _encode_animated_webp(frames, fps, quality, max_res):
    if not frames:
        return None, 0, 0
    pil_frames = []
    for f in frames:
        pf = f if f.mode == "RGB" else f.convert("RGB")
        if max_res and max_res > 0 and (pf.width > max_res or pf.height > max_res):
            pf = ImageOps.contain(pf, (max_res, max_res), Image.LANCZOS)
        pil_frames.append(pf)
    duration_ms = max(1, int(round(1000 / max(1, fps))))
    buf = pyio.BytesIO()
    try:
        pil_frames[0].save(
            buf,
            format="WEBP",
            save_all=True,
            append_images=pil_frames[1:],
            duration=duration_ms,
            loop=0,
            quality=quality,
            method=4,
        )
    except Exception as e:
        logging.warning(f"Animated WebP encode failed: {e}")
        return None, 0, 0
    return base64.b64encode(buf.getvalue()).decode("ascii"), pil_frames[0].width, pil_frames[0].height


class _H3PreviewOverrideWrapper:
    def __init__(self, max_resolution, node_id, jpeg_quality, suppress_default,
                 preview_frames=1, preview_fps=12, vae=None,
                 audio_vae=None, audio_preview=False, audio_preview_from=60):
        self.max_resolution = max_resolution
        self.node_id = str(node_id) if node_id is not None else None
        self.jpeg_quality = jpeg_quality
        self.suppress_default = suppress_default
        self.preview_frames = preview_frames
        self.preview_fps = preview_fps
        self.frames = []
        # 音频预览：采样中把当前步的声音解出来推给节点面板，手动点播、不自动播放。
        # 全程异常兜底——音频预览失败绝不影响采样出片。
        self.audio_vae = audio_vae
        self.audio_preview = bool(audio_preview) and audio_vae is not None
        self.audio_preview_from = audio_preview_from
        # Only tiny decoders (TAEHV / flat TAESD-style) are allowed per step; the loader
        # returns one of those or None. A full video VAE would eat VRAM every step.
        self.vae = vae
        self._tiny_vae = vae is not None
        if vae is not None:
            logging.info(
                f"[MiniMax H3 PreviewOverride] tiny decoder {type(vae).__name__}: "
                f"{getattr(vae, 'latent_channels', '?')}ch, upscale x{getattr(vae, 'upscale_ratio', '?')}"
            )

    def __call__(self, executor, noise, latent_image, sampler, sigmas, denoise_mask,
                 callback, disable_pbar, seed, latent_shapes):
        guider = executor.class_obj
        model_patcher = guider.model_patcher
        latent_format = model_patcher.model.latent_format

        # Preflight: a tiny decoder must match the model's video latent channels (H3 = 24).
        # A mismatched TAE (e.g. the 128-channel LTX2 tae) physically cannot decode H3 latents
        # — disable it with one clear warning instead of failing every step.
        if self._tiny_vae:
            try:
                want = latent_shapes[0][1] if latent_shapes and len(latent_shapes) > 0 \
                    else getattr(latent_format, "latent_channels", None)
                have = getattr(self.vae, "latent_channels", None)
                if want is not None and have is not None and have != want:
                    logging.warning(
                        f"[MiniMax H3 PreviewOverride] Loaded TAE has {have} latent channels but "
                        f"the model needs {want} — it cannot decode these latents. TAE previews "
                        f"disabled, using Latent2RGB. (LTX/Wan TAEs are incompatible with H3; "
                        f"a 24-channel H3 TAE is required.)"
                    )
                    self._tiny_vae = False
            except Exception:
                pass

        # 音频预览：采样中把当前步的声音解出来推给节点面板，手动点播、不自动播放。
        # 全程异常兜底——音频预览失败绝不影响采样出片。
        # （三个值在 __init__ 里已存为实例属性，这里直接用，不在 __call__ 重复赋值）
        _AUDIO_EVERY = 3

        def _maybe_send_audio(step, x0, total_steps_):
            if not (self.audio_preview and node_id is not None and PromptServer is not None):
                return
            cur = step + 1
            try:
                if total_steps_ and total_steps_ > 0:
                    start = max(1, int(total_steps_ * float(self.audio_preview_from) / 100.0))
                    if cur < start and cur != total_steps_:
                        return
                    if cur % _AUDIO_EVERY != 0 and cur != total_steps_:
                        return
                z = _audio_part(x0, latent_shapes)
                if z is None:
                    return
                # 采样中的音频流是放大过的，不除回去解码就是噪声（见 _audio_stream_scale）
                scale = _audio_stream_scale(model_patcher.model)
                if scale != 1.0:
                    z = z / scale
                # 整段都解，不截窗口：解码跑在后台 CPU 线程上，不占显卡也不拖采样
                _AUDIO_WORKER.submit({
                    "vae": self.audio_vae,
                    "node_id": node_id,
                    "z": z.detach().to("cpu", torch.float32),
                    "sr": int(getattr(self.audio_vae, "audio_sample_rate", 32000)),
                    "step": cur,
                    "total": total_steps_,
                })
            except Exception as e:
                logging.warning(f"[MiniMax H3 PreviewOverride] 音频预览失败（不影响采样）: {e}")

        # H3's MiniMaxH3AV has no taesd_decoder_name, so the core previewer is Latent2RGB.
        previewer = _get_core_previewer(model_patcher.load_device, latent_format)
        # Explicit Latent2RGB built from the format's own factors (24ch video stream).
        fallback_previewer = None
        try:
            rgb_factors = getattr(latent_format, "latent_rgb_factors", None)
            if rgb_factors is not None:
                fallback_previewer = latent_preview.Latent2RGBPreviewer(
                    rgb_factors,
                    getattr(latent_format, "latent_rgb_factors_bias", None),
                    getattr(latent_format, "latent_rgb_factors_reshape", None),
                )
        except Exception:
            pass

        original_callback = callback
        node_id = self.node_id
        max_res = self.max_resolution
        quality = self.jpeg_quality
        self.frames = []

        sigmas_list = sigmas.detach().cpu().tolist() if sigmas is not None else []

        # Pre-seed so step 1 has a measurable Δ (model's first transformation noise -> x0).
        initial_video_cpu = None
        try:
            if sigmas is not None and len(sigmas) > 0:
                s0 = sigmas[0].to(noise.device) if hasattr(sigmas[0], "to") else sigmas[0]
                seeded = noise * s0  # flat packed tensor at OUTER_SAMPLE level
                initial_video_cpu = _video_part(seeded, latent_shapes).detach().float().cpu()
        except Exception as e:
            logging.warning(f"[MiniMax H3 PreviewOverride] initial seed Δ pre-fill failed: {e}")

        state = {"last_video_gpu": None, "last_time": None, "step_ms_window": []}
        total_steps_init = max(0, len(sigmas_list) - 1)

        # Boundary-0 message: sigma schedule (required by JS) plus optional noise preview.
        if node_id is not None and PromptServer is not None:
            init_payload = {
                "node_id": node_id,
                "step": 0,
                "total": total_steps_init,
                "sigma": sigmas_list[0] if sigmas_list else None,
                "sigmas": sigmas_list,
            }
            try:
                pil_init = None
                if initial_video_cpu is not None and initial_video_cpu.ndim == 5:
                    if fallback_previewer is not None:
                        out = fallback_previewer.decode_latent_to_preview(initial_video_cpu)
                        if isinstance(out, Image.Image):
                            pil_init = out
                if pil_init is not None:
                    if pil_init.mode != "RGB":
                        pil_init = pil_init.convert("RGB")
                    if max_res and max_res > 0 and (pil_init.width > max_res or pil_init.height > max_res):
                        pil_init = ImageOps.contain(pil_init, (max_res, max_res), Image.LANCZOS)
                    ibuf = pyio.BytesIO()
                    pil_init.save(ibuf, format="JPEG", quality=quality)
                    init_payload["image"] = base64.b64encode(ibuf.getvalue()).decode("ascii")
                    init_payload["w"] = pil_init.width
                    init_payload["h"] = pil_init.height
            except Exception as e:
                logging.warning(f"Initial noise preview failed (sigmas still sent): {e}")
            PromptServer.instance.send_sync("minimax_h3_preview_override", init_payload, PromptServer.instance.client_id)

        encoder = _AsyncPreviewEncoder()
        animate_video = self.preview_frames > 1
        anim_frames = self.preview_frames
        anim_fps = self.preview_fps

        def new_callback(step, x0, x, total_steps_):
            if previewer is not None or fallback_previewer is not None:
                try:
                    video = _video_part(x0, latent_shapes)
                    if not isinstance(video, torch.Tensor):
                        raise TypeError(f"unexpected x0 type: {type(video).__name__}")

                    pil_frames = []
                    if self._tiny_vae and video.ndim == 5:
                        pil_frames = _tae_decode_to_pil(
                            self.vae, video, max_frames=anim_frames if animate_video else 1,
                        )
                    if not pil_frames and animate_video and video.ndim == 5:
                        pil_frames = _decode_video_frames_l2rgb(video, latent_format, anim_frames)
                    if not pil_frames:
                        for prev in (previewer, fallback_previewer):
                            if prev is None:
                                continue
                            try:
                                out = prev.decode_latent_to_preview(video)
                            except Exception as e:
                                if prev is previewer:
                                    logging.warning(f"Active previewer raised, trying Latent2RGB fallback: {e}")
                                continue
                            if isinstance(out, Image.Image):
                                pil_frames = [out]
                                break
                            elif prev is previewer:
                                logging.warning(
                                    f"[MiniMax H3 PreviewOverride] {type(previewer).__name__} returned "
                                    f"{type(out).__name__} instead of PIL.Image — falling back to Latent2RGB."
                                )

                    if not pil_frames:
                        if original_callback is not None:
                            original_callback(step, x0, x, total_steps_)
                        return

                    pil_first = pil_frames[0]
                    if pil_first.mode != "RGB":
                        pil_first = pil_first.convert("RGB")
                        pil_frames[0] = pil_first
                    # Kept so a second run could reuse captured frames if needed.
                    self.frames.append(pil_first)

                    if node_id is not None and PromptServer is not None:
                        # Δ over the video stream only; computed on GPU to avoid a per-step
                        # full-latent CPU copy (keeps one latent in VRAM instead).
                        delta_v = None
                        try:
                            video_cur = video.detach()
                            prev = state["last_video_gpu"]
                            if prev is not None and prev.shape == video_cur.shape:
                                diff = video_cur.float().sub(prev.float())
                                delta_v = (diff.norm() / max(1, diff.numel()) ** 0.5).item()
                            state["last_video_gpu"] = video_cur
                        except Exception as e:
                            logging.warning(f"[MiniMax H3 PreviewOverride] Δ failed: {e}")

                        now = time.perf_counter()
                        step_ms = None
                        if state["last_time"] is not None:
                            step_ms = (now - state["last_time"]) * 1000.0
                            w = state["step_ms_window"]
                            w.append(step_ms)
                            if len(w) > 8:
                                w.pop(0)
                        state["last_time"] = now
                        avg_step_ms = (sum(state["step_ms_window"]) / len(state["step_ms_window"])) if state["step_ms_window"] else None
                        sigma_val = sigmas_list[step] if 0 <= step < len(sigmas_list) else None
                        sent_step = step + 1

                        def _encode_and_send(
                            pil_frames=pil_frames, step_ms=step_ms, avg_step_ms=avg_step_ms,
                            sigma_val=sigma_val, sent_step=sent_step, total_steps_=total_steps_,
                            delta_v=delta_v,
                        ):
                            if len(pil_frames) > 1:
                                b64, w_, h_, mime = None, 0, 0, None
                                if _NVENC_AVAILABLE:
                                    b64, w_, h_ = _encode_mp4_nvenc(pil_frames, anim_fps, max_res)
                                    if b64:
                                        mime = "video/mp4"
                                if not b64:
                                    b64, w_, h_ = _encode_animated_webp(pil_frames, anim_fps, quality, max_res)
                                    mime = "image/webp"
                            else:
                                pil_send = pil_frames[0]
                                if max_res and max_res > 0 and (pil_send.width > max_res or pil_send.height > max_res):
                                    pil_send = ImageOps.contain(pil_send, (max_res, max_res), Image.LANCZOS)
                                buf = pyio.BytesIO()
                                pil_send.save(buf, format="JPEG", quality=quality)
                                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                                w_, h_ = pil_send.width, pil_send.height
                                mime = "image/jpeg"

                            if not b64:
                                return

                            PromptServer.instance.send_sync(
                                "minimax_h3_preview_override",
                                {
                                    "node_id": node_id,
                                    "image": b64,
                                    "mime": mime,
                                    "w": w_,
                                    "h": h_,
                                    "step": sent_step,
                                    "total": total_steps_,
                                    "sigma": sigma_val,
                                    "sigmas": None,
                                    "delta": delta_v,
                                    "step_ms": step_ms,
                                    "avg_step_ms": avg_step_ms,
                                    "fps": anim_fps if mime in ("video/mp4", "image/webp") else None,
                                },
                                PromptServer.instance.client_id,
                            )

                        encoder.submit(_encode_and_send)
                except Exception as e:
                    logging.warning(f"[MiniMax H3 PreviewOverride] step preview failed: {e}")
            _maybe_send_audio(step, x0, total_steps_)
            if original_callback is not None:
                original_callback(step, x0, x, total_steps_)

        # Suppress every concrete decode_latent_to_preview_image so only this node's
        # preview updates while sampling (progress bar still advances).
        prev_methods = []
        if self.suppress_default:
            targets = [latent_preview.LatentPreviewer]
            stack = list(latent_preview.LatentPreviewer.__subclasses__())
            while stack:
                cls = stack.pop()
                targets.append(cls)
                stack.extend(cls.__subclasses__())
            for cls in targets:
                if "decode_latent_to_preview_image" in cls.__dict__:
                    prev_methods.append((cls, cls.__dict__["decode_latent_to_preview_image"]))
                    cls.decode_latent_to_preview_image = _suppressed_preview_image
        try:
            state["last_time"] = time.perf_counter()
            return executor(noise, latent_image, sampler, sigmas, denoise_mask, new_callback,
                            disable_pbar, seed, latent_shapes=latent_shapes)
        finally:
            encoder.shutdown(drain_timeout=5.0)
            for cls, prev in prev_methods:
                cls.decode_latent_to_preview_image = prev
            # Don't keep the last sampled latent pinned on GPU after the run.
            state["last_video_gpu"] = None


class MiniMaxH3PreviewOverride(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="MiniMaxH3PreviewOverride",
            display_name="班长 MiniMax H3 预览覆盖",
            category="👨‍🏫班长/MiniMax H3",
            description=(
                "在节点上为 MiniMax H3 开启逐帧实时预览面板。默认预览只有 512px 且仅显示第一帧；"
                "本节点把 H3 视频 latent 每步解码为全分辨率画面（可选动画）推送到节点内置控件，"
                "并附带 σ/Δ 曲线与步耗时统计。"
            ),
            inputs=[
                io.Model.Input("model", display_name="模型", tooltip="接入预览覆盖的 MiniMax H3 模型（建议接在 MiniMaxH3SigmaShift 之后）。"),
                io.Int.Input(
                    "max_resolution",
                    display_name="最大分辨率",
                    default=960,
                    min=0,
                    max=8192,
                    step=8,
                    tooltip="预览画面最长边（像素）。0 = 不缩放，按采样原始分辨率输出。",
                ),
                io.Int.Input(
                    "jpeg_quality",
                    display_name="预览画质",
                    default=80,
                    min=30,
                    max=100,
                    step=1,
                    tooltip="预览传输使用的 JPEG/WebP 画质。",
                ),
                io.Boolean.Input(
                    "suppress_default_preview",
                    display_name="抑制默认预览",
                    default=True,
                    tooltip="采样时隐藏采样器节点上的标准预览，只更新本节点的画面。进度条不受影响。",
                ),
                io.Int.Input(
                    "preview_frames",
                    display_name="预览帧数",
                    default=60,
                    min=1,
                    max=1024,
                    step=1,
                    tooltip="每步从 H3 视频 latent 中抽取的帧数，用于动画预览。"
                            "1 = 单帧（最快）。>1 = 动画 WebP（有 NVENC 时为 MP4），按下方帧率回放。",
                ),
                io.Int.Input(
                    "preview_fps",
                    display_name="预览帧率",
                    default=4,
                    min=1,
                    max=60,
                    step=1,
                    tooltip="动画预览的回放帧率。预览帧数=1 时无效。",
                ),
                io.Combo.Input(
                    "vae_name",
                    display_name="VAE文件",
                    options=_vae_approx_list(),
                    default="MiniMax-H3-TAE.safetensors",
                    tooltip="从 models/vae_approx 选择小型自编码器文件，用于每步真 RGB 解码。"
                            "优先使用 MiniMax-H3-TAE.safetensors（H3 官方小型解码器，真 RGB 最清晰）；"
                            "已放入 ComfyUI/models/vae_approx/。",
                ),
                io.Boolean.Input(
                    "audio_preview",
                    display_name="音频预览",
                    default=False,
                    tooltip="采样过程中把当前步的声音解出来，在节点里手动试听。"
                            "H3 是音视联合扩散，每一步的 x0 里都带着声音，"
                            "所以不用等跑完就能听出这一句台词对不对。\n"
                            "必须把 H3 的音频 VAE 接到「音频VAE」口才会生效；未接或不支持时自动跳过，不影响出片。",
                ),
                io.Int.Input(
                    "audio_preview_from",
                    display_name="音频预览起始进度(%)",
                    default=60,
                    min=0,
                    max=100,
                    step=5,
                    tooltip="进度到百分之多少之后才开始解音频。前期全是噪声，听了也没意义，"
                            "所以默认从 60% 开始，每 3 步更新一次，末步必更新。",
                ),
                io.Vae.Input(
                    "audio_vae",
                    display_name="音频VAE",
                    optional=True,
                    tooltip="H3 的音频 VAE。接上并打开「音频预览」后才能在采样中试听。",
                ),
            ],
            outputs=[io.Model.Output(display_name="模型", tooltip="附加了预览覆盖的模型。")],
            hidden=[io.Hidden.unique_id],
            is_experimental=True,
        )

    @classmethod
    def execute(cls, model, max_resolution, jpeg_quality, suppress_default_preview,
                preview_frames, preview_fps,
                vae_name="MiniMax-H3-TAE.safetensors",
                audio_preview=False, audio_preview_from=60, audio_vae=None) -> io.NodeOutput:
        tiny_vae = None
        if vae_name:
            try:
                tiny_vae = _load_tiny_vae(vae_name)
            except Exception as e:
                logging.warning(
                    f"[MiniMax H3 PreviewOverride] could not load tiny VAE '{vae_name}': {e} "
                    f"— falling back to Latent2RGB previews. Place the TAE file in "
                    f"ComfyUI/models/vae_approx/."
                )
        m = model.clone()
        m.add_wrapper_with_key(
            comfy.patcher_extension.WrappersMP.OUTER_SAMPLE,
            "minimax_h3_preview_override",
            _H3PreviewOverrideWrapper(
                max_resolution, cls.hidden.unique_id, jpeg_quality, suppress_default_preview,
                preview_frames, preview_fps, tiny_vae,
                audio_vae, audio_preview, audio_preview_from,
            ),
        )
        return io.NodeOutput(m)


# 班长全家桶集成：通过 NODE_CLASS_MAPPINGS 注册 V3 节点（ComfyUI 对 io.ComfyNode
# 子类在注册/执行/object_info 全链路均有 V3 分支支持，无需 comfy_entrypoint）。
NODE_CLASS_MAPPINGS = {
    "MiniMaxH3PreviewOverride": MiniMaxH3PreviewOverride,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MiniMaxH3PreviewOverride": "班长 MiniMax H3 预览覆盖",
}
