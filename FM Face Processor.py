#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FM Face Processor (JA/EN, 全自動OCR / ダークモード)
===================================================
日本語・英語をボタンで切替できます (toggle JA/EN with the top-right button).
顔画像と「ID入りスクショ」を1つのフォルダに混ぜて入れて「実行」すると:
  1. 各画像を OCR で見て「IDスクショ」か「顔写真」かを自動仕分け
  2. IDスクショから person ID（例: ID: 50053056）を読み取る
  3. IDスクショと顔写真を撮影時刻の近さでペアにする
  4. 顔写真を アップスケール → 背景除去 → 180x180 透過PNG にして <ID>.png 保存
  5. config.xml を生成（FMフェイスパック形式）

OCRを使わず「ファイル名＝ID」で動かすことも可能（チェックを外す）。

必要ライブラリ:
  py -3.12 -m pip install pillow rembg
  py -3.12 -m pip install rapidocr-onnxruntime   # ← ID自動読み取り（軽量・torch不要）
  # AI高画質化を使う場合のみ: Real-ESRGAN-x4plus.onnx をアプリと同じフォルダに置く（torch不要・onnxruntimeで動作）
  # 見た目をWin11風に: py -3.12 -m pip install sv-ttk （任意）
"""

import io
import os
import re
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.sax.saxutils import quoteattr

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

try:
    import sv_ttk
    HAS_SVTTK = True
except Exception:  # noqa: BLE001
    HAS_SVTTK = False

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
APP_VERSION = "v1.2.0"
# 「ID」に続く数字（FMのperson ID）を拾う
ID_RE = re.compile(r"id[^0-9]{0,6}(\d{2,})", re.IGNORECASE)

# ----- 言語切替（日本語/英語）-----
_LANG = "ja"


def set_lang(lang):
    global _LANG
    _LANG = "en" if lang == "en" else "ja"


def t(ja, en):
    """現在の言語に応じて日本語か英語を返す。"""
    return en if _LANG == "en" else ja


# ==========================================================================
# 画像処理
# ==========================================================================
_esrgan_sess = None
_esrgan_tried = False
_esrgan_lock = threading.Lock()
ESRGAN_FILENAME = "Real-ESRGAN-x4plus.onnx"


def _find_esrgan_model():
    """Real-ESRGANのonnxモデルを、アプリと同じ場所→models/→カレントの順に探す。"""
    names = [ESRGAN_FILENAME, "realesrgan-x4.onnx", "RealESRGAN_x4plus.onnx"]
    bases = []
    try:
        here = Path(__file__).resolve().parent
        bases += [here, here / "models"]
    except Exception:  # noqa: BLE001
        pass
    bases.append(Path.cwd())
    for b in bases:
        for n in names:
            p = b / n
            if p.exists():
                return str(p)
    return None


def _get_esrgan(log):
    """onnxruntime版のRealESRGANを用意。モデルが無ければ None（Lanczosにフォールバック）。"""
    global _esrgan_sess, _esrgan_tried
    with _esrgan_lock:
        if _esrgan_tried:
            return _esrgan_sess
        _esrgan_tried = True
        try:
            import onnxruntime as ort
        except Exception as e:  # noqa: BLE001
            log(t(f"  [i] AI高画質化は使えません（onnxruntime未導入）。通常拡大で続行: {e}",
                  f"  [i] AI upscale unavailable (no onnxruntime). Using normal resize: {e}"))
            return None
        path = _find_esrgan_model()
        if not path:
            log(t("  [i] AI高画質化モデル(.onnx)が見つかりません。通常拡大で続行します。",
                  "  [i] AI upscale model (.onnx) not found. Using normal resize."))
            return None
        try:
            so = _ort_options()
            _esrgan_sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"]) if so \
                else ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            log(t("  [i] AI高画質化（Real-ESRGAN）を使用します。",
                  "  [i] Using AI upscale (Real-ESRGAN)."))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] AI高画質化モデルを読み込めませんでした（通常拡大で続行）: {e}",
                  f"  [!] Could not load AI upscale model (using normal resize): {e}"))
            _esrgan_sess = None
        return _esrgan_sess


def _esrgan_enhance(sess, img_rgb, log):
    """RGBのPIL画像を4倍に超解像して返す。タイル分割で任意サイズに対応。"""
    import numpy as np
    from PIL import Image
    inp = sess.get_inputs()[0]
    shape = inp.shape  # 例: [1,3,'H','W'] または [1,3,256,256]
    th = shape[2] if isinstance(shape[2], int) else None
    tw = shape[3] if isinstance(shape[3], int) else None
    scale = 4
    arr = np.asarray(img_rgb.convert("RGB"), dtype=np.float32) / 255.0
    H, W, _ = arr.shape

    def run_tile(tile):
        ph, pw = tile.shape[0], tile.shape[1]
        oh = th if th else ph
        ow = tw if tw else pw
        padded = np.zeros((oh, ow, 3), np.float32)
        padded[:ph, :pw] = tile
        x = padded.transpose(2, 0, 1)[None]
        y = sess.run(None, {inp.name: x})[0][0].transpose(1, 2, 0)
        y = np.clip(y, 0.0, 1.0)
        return y[:ph * scale, :pw * scale]

    # モデルが固定サイズなら、その大きさでタイル処理。可変なら一括処理。
    tile_h = th if th else H
    tile_w = tw if tw else W
    overlap = 8 if (th and tw) else 0
    out = np.zeros((H * scale, W * scale, 3), np.float32)
    cnt = np.zeros((H * scale, W * scale, 1), np.float32)
    step_h = max(1, tile_h - overlap)
    step_w = max(1, tile_w - overlap)
    for y0 in range(0, H, step_h):
        for x0 in range(0, W, step_w):
            tile = arr[y0:y0 + tile_h, x0:x0 + tile_w]
            res = run_tile(tile)
            oy, ox = y0 * scale, x0 * scale
            rh, rw = res.shape[0], res.shape[1]
            out[oy:oy + rh, ox:ox + rw] += res
            cnt[oy:oy + rh, ox:ox + rw] += 1.0
            if y0 + tile_h >= H and x0 + tile_w >= W:
                break
    cnt[cnt == 0] = 1.0
    out = (out / cnt * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(out)


def upscale(img, factor, model_name, use_ai, log):
    from PIL import Image
    if factor <= 1:
        return img
    if use_ai:
        sess = _get_esrgan(log)
        if sess is not None:
            try:
                out = _esrgan_enhance(sess, img, log)   # 4倍に超解像
                # 目標倍率に合わせて仕上げ（4倍より小さければ縮小）
                tw, th = int(img.width * factor), int(img.height * factor)
                if (out.width, out.height) != (tw, th):
                    out = out.resize((tw, th), Image.LANCZOS)
                return out
            except Exception as e:  # noqa: BLE001
                log(t(f"  [!] AI高画質化中にエラー -> 通常拡大 ({e})",
                      f"  [!] Error during AI upscale -> normal resize ({e})"))
    w, h = img.size
    return img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)


_rembg_sessions = {}
_rembg_lock = threading.Lock()

# ----- 低負荷モード（他のアプリにCPUを譲る）-----
_LOW_POWER = False


def set_low_power(flag):
    """低負荷モードのオン/オフ。新しく作るAIセッションのスレッド数に効く。"""
    global _LOW_POWER
    _LOW_POWER = bool(flag)
    try:
        import cv2
        cv2.setNumThreads(2 if _LOW_POWER else 0)   # 0=自動(全コア)
    except Exception:  # noqa: BLE001
        pass


def _ort_options():
    """onnxruntime のセッションオプション。低負荷モード時はスレッドを絞る。"""
    if not _LOW_POWER:
        return None
    try:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.inter_op_num_threads = 1
        return so
    except Exception:  # noqa: BLE001
        return None


def set_process_priority(low):
    """処理中だけプロセス優先度を下げ、終わったら戻す（Windows）。"""
    try:
        if sys.platform == "win32":
            import ctypes
            BELOW_NORMAL = 0x00004000
            NORMAL = 0x00000020
            h = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(h, BELOW_NORMAL if low else NORMAL)
        elif low:
            os.nice(10)   # POSIXは下げるのみ（戻せないがプロセス終了で消える）
    except Exception:  # noqa: BLE001
        pass


def remove_background(img, model_name, alpha_matting=False, log=None, removebg_key=None):
    """背景除去。remove.bg のAPIキーがあればそちらを優先（髪の品質が高い）。
    失敗時やキー無しはローカルAIで処理。メモリ不足時は軽量モデルで再試行。"""
    if removebg_key:
        from PIL import Image
        try:
            out = _remove_background_removebg(img, removebg_key)
            work = img.convert("RGBA")
            if out.size != work.size:
                # 無料枠はプレビュー解像度（小さめ）で返るため、
                # 透過情報だけを元のサイズに戻して元画像に適用する（座標と画質を保つ）
                big = out.resize(work.size, Image.LANCZOS)
                work.putalpha(big.getchannel("A"))
                # 輪郭ぎわの数pxだけ remove.bg の補正済みカラーを使う
                # （元画像のフチは背景色と混ざって黄ばむため。内側は元画像の高画質を保つ）
                try:
                    import numpy as np
                    import cv2
                    wa = np.asarray(work, dtype=np.uint8).copy()
                    ba = np.asarray(big, dtype=np.uint8)
                    fg = (wa[..., 3] > 0).astype(np.uint8)
                    distin = cv2.distanceTransform(fg, cv2.DIST_L2, 3)
                    band = (distin > 0) & (distin <= max(3.0, min(work.size) * 0.012))
                    wa[..., :3][band] = ba[..., :3][band]
                    work = Image.fromarray(wa, "RGBA")
                except Exception:  # noqa: BLE001
                    pass
                out = _defringe(work)
            if log:
                log(t("  [i] remove.bg で背景を除去しました", "  [i] Background removed via remove.bg"))
            return out
        except Exception as e:  # noqa: BLE001
            if log:
                log(t(f"  [i] remove.bg が使えませんでした（ローカルAIで続行）: {e}",
                      f"  [i] remove.bg unavailable (using local AI): {e}"))
    try:
        return _remove_background_once(img, model_name, alpha_matting)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if ("llocate" in msg or "memory" in msg.lower()) and model_name != "u2net":
            global _rembg_sessions
            _rembg_sessions = {}
            import gc
            gc.collect()
            if log:
                log(t("  [i] メモリ不足のため、軽量モデル(u2net)で再試行します…",
                      "  [i] Out of memory; retrying with the lighter u2net model…"))
            return _remove_background_once(img, "u2net", alpha_matting, max_side=1280)
        raise


def _remove_background_removebg(img, api_key):
    """remove.bg API（プレビュー解像度・無料枠は月50回）で背景除去。"""
    import requests
    from PIL import Image
    work = img.convert("RGB")
    # アップロードは大きすぎても無駄なので長辺1500pxまでに抑える
    if max(work.size) > 1500:
        r = 1500 / float(max(work.size))
        work = work.resize((max(1, int(work.width * r)), max(1, int(work.height * r))),
                           Image.LANCZOS)
    buf = io.BytesIO()
    work.save(buf, "JPEG", quality=95)
    resp = requests.post(
        "https://api.remove.bg/v1.0/removebg",
        headers={"X-API-Key": api_key},
        files={"image_file": ("image.jpg", buf.getvalue())},
        data={"size": "preview"},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    return Image.open(io.BytesIO(resp.content)).convert("RGBA")


def _remove_background_once(img, model_name, alpha_matting=False, max_side=1600):
    from PIL import Image
    from rembg import remove, new_session
    with _rembg_lock:
        sess = _rembg_sessions.get(model_name)
        if sess is None:
            sess = new_session(model_name)
            _rembg_sessions[model_name] = sess
    kw = {}
    if alpha_matting:
        kw = dict(alpha_matting=True,
                  alpha_matting_foreground_threshold=240,
                  alpha_matting_background_threshold=10,
                  alpha_matting_erode_size=5)
    work = img.convert("RGBA")
    # 大きい画像はメモリを食うので、マスク計算用に縮小したコピーで処理し、
    # できたマスク（透明部分）を元の解像度に戻して適用する（ディテールは保つ）。
    small = work
    if max(work.size) > max_side:
        ratio = max_side / float(max(work.size))
        small = work.resize((max(1, int(work.width * ratio)),
                             max(1, int(work.height * ratio))), Image.LANCZOS)
    out = remove(small, session=sess, **kw)
    if not isinstance(out, Image.Image):
        out = Image.open(io.BytesIO(out))
    out = out.convert("RGBA")
    if small is not work:
        # 縮小して処理した場合：アルファ（マスク）だけ元サイズに拡大して、元画像に貼る
        alpha = out.getchannel("A").resize(work.size, Image.LANCZOS)
        work.putalpha(alpha)
        out = work
    return _defringe(out)


def _defringe(img):
    """髪のフチなど半透明部分に混ざった背景色を引き算して、色のにじみを消す。
    背景色は「透明になった領域の元の色」から推定する（単色背景に特に有効）。"""
    try:
        import numpy as np
        from PIL import Image
        arr = np.asarray(img, dtype=np.float32).copy()
        a = arr[..., 3] / 255.0
        bg_mask = a < 0.04
        if bg_mask.sum() < 100:
            return img
        bg = np.median(arr[..., :3][bg_mask], axis=0)    # 背景色の推定（中央値＝文字等に強い）
        semi = (a > 0.02) & (a < 0.98)                   # 半透明のフチ
        if semi.any():
            al = a[semi][:, None]
            col = arr[..., :3][semi]
            # 混合色 = 前景*α + 背景*(1-α) → 前景 = (混合 - 背景*(1-α)) / α
            fixed = (col - bg * (1.0 - al)) / np.maximum(al, 0.10)
            arr[..., :3][semi] = np.clip(fixed, 0, 255)
        # さらに、背景色とほぼ同じ色の「不透明なかけら」が輪郭に貼り付いていたら剥がす
        # （髪のすき間の背景を前景と誤認したケース。小さなかけらだけが対象。
        #   顔や髪のような大きなかたまりは、色が似ていても絶対に剥がさない）
        try:
            import cv2
            dist = np.linalg.norm(arr[..., :3] - bg, axis=-1)
            bglike = ((dist < 40) & (a > 0.02)).astype(np.uint8)
            if bglike.any():
                n, labels, stats, _ = cv2.connectedComponentsWithStats(bglike, connectivity=8)
                if n > 1:
                    trans_d = cv2.dilate((a < 0.04).astype(np.uint8),
                                         np.ones((3, 3), np.uint8))
                    touching = np.unique(labels[(trans_d > 0) & (bglike > 0)])
                    total_px = arr.shape[0] * arr.shape[1]
                    max_speck = max(64, int(total_px * 0.002))   # かけら上限=全体の0.2%
                    peel_ids = [i for i in touching
                                if i > 0 and stats[i, cv2.CC_STAT_AREA] <= max_speck]
                    if peel_ids:
                        peel = np.isin(labels, peel_ids)
                        # 念のため総量も制限（全不透明の2%まで。超えるなら何もしない）
                        if peel.sum() <= max(64, int((a > 0.5).sum() * 0.02)):
                            arr[..., 3][peel] = 0
        except Exception:  # noqa: BLE001
            pass
        return Image.fromarray(arr.astype(np.uint8), "RGBA")
    except Exception:  # noqa: BLE001
        return img


def fit_to_size(img, size, mode):
    from PIL import Image
    img = img.convert("RGBA")
    target = (size, size)
    if mode == "stretch":
        return img.resize(target, Image.LANCZOS)
    if mode == "cover":
        ratio = img.width / img.height
        new_w, new_h = (int(size * ratio), size) if ratio > 1 else (size, int(size / ratio))
        r = img.resize((new_w, new_h), Image.LANCZOS)
        left, top = (new_w - size) // 2, (new_h - size) // 2
        return r.crop((left, top, left + size, top + size))
    work = img.copy()
    work.thumbnail(target, Image.LANCZOS)
    canvas = Image.new("RGBA", target, (0, 0, 0, 0))
    canvas.paste(work, ((size - work.width) // 2, (size - work.height) // 2), work)
    return canvas


_face_cascade = None
_face_cascade_alt = None
_cascade_lock = threading.Lock()
_yunet = None
_yunet_tried = False
_yunet_lock = threading.Lock()

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"


def _find_yunet_model():
    """同梱されたYuNetモデル(.onnx)を探す。スクリプトと同じ場所などを順に確認。"""
    import sys
    cands = []
    try:
        here = Path(__file__).resolve().parent
        cands += [here / YUNET_FILENAME, here / "models" / YUNET_FILENAME]
    except Exception:  # noqa: BLE001
        pass
    try:
        cands.append(Path(sys.argv[0]).resolve().parent / YUNET_FILENAME)
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path.cwd() / YUNET_FILENAME)
    for p in cands:
        try:
            if p.is_file():
                return str(p)
        except Exception:  # noqa: BLE001
            pass
    return None


def _get_yunet(log):
    """YuNet検出器を用意。モデルが無ければ None（Haarにフォールバック）。"""
    global _yunet, _yunet_tried
    with _yunet_lock:
        if _yunet_tried:
            return _yunet
        _yunet_tried = True
        try:
            import cv2
            if not hasattr(cv2, "FaceDetectorYN"):
                log(t("  [i] 高精度の顔検出は使えません（OpenCVが古い）。簡易検出で続行します。",
                      "  [i] High-accuracy face detection unavailable (old OpenCV). Using basic detection."))
                return None
            path = _find_yunet_model()
            if not path:
                log(t("  [i] 顔検出モデル(.onnx)が見つかりません。簡易検出で続行します。",
                      "  [i] Face model (.onnx) not found. Using basic detection."))
                return None
            _yunet = cv2.FaceDetectorYN.create(path, "", (320, 320), 0.6, 0.3, 5000)
            log(t("  [i] 高精度の顔検出（YuNet）を使用します。",
                  "  [i] Using high-accuracy face detection (YuNet)."))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [i] 顔検出モデルを読み込めませんでした（簡易検出で続行）: {e}",
                  f"  [i] Could not load face model (using basic detection): {e}"))
            _yunet = None
        return _yunet


def detect_face_box_yunet(img, log):
    """YuNetで顔を検出し ((x, y, w, h), eye_mid_y) を返す。使えなければ None。
    eye_mid_y: 両目の中点Y座標（ランドマーク取得できない場合は None）。"""
    det = _get_yunet(log)
    if det is None:
        return None
    try:
        import cv2  # noqa: F401
        import numpy as np
        bgr = np.array(img.convert("RGB"))[:, :, ::-1].copy()
        img_h, img_w = bgr.shape[:2]
        det.setInputSize((img_w, img_h))
        _, faces = det.detect(bgr)
        if faces is None or len(faces) == 0:
            return None
        # 上半分の顔を優先（胸のロゴ・柄の誤検出を排除）
        upper = [f for f in faces if (float(f[1]) + float(f[3]) / 2) < img_h * 0.55]
        pool = upper if upper else list(faces)
        # 面積 × 信頼度スコアで選択（スコアは index 4）
        best = max(pool, key=lambda f: float(f[2]) * float(f[3]) * float(f[4]))
        bx, by, bw, bh = float(best[0]), float(best[1]), float(best[2]), float(best[3])
        # YuNetランドマーク配列: [4]=スコア, [5,6]=右目xy, [7,8]=左目xy, [9,10]=鼻, ...
        eye_mid_y = None
        eye_mid_x = None  # 目の水平中点（横ズレ補正用）
        nose_x = None     # 鼻のx座標（目が使えない時の横ズレ補正フォールバック）
        if len(best) >= 11:
            try:
                eye_mid_y = (float(best[6]) + float(best[8])) / 2.0
                eye_mid_x = (float(best[5]) + float(best[7])) / 2.0
                nose_x    =  float(best[9])
            except Exception:
                pass
        elif len(best) >= 9:
            try:
                eye_mid_y = (float(best[6]) + float(best[8])) / 2.0
                eye_mid_x = (float(best[5]) + float(best[7])) / 2.0
            except Exception:
                pass
        return (int(round(bx)), int(round(by)), int(round(bw)), int(round(bh))), eye_mid_y, eye_mid_x, nose_x
    except Exception:
        return None


def _get_face_cascade():
    global _face_cascade
    with _cascade_lock:
        if _face_cascade is None:
            import cv2
            path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            _face_cascade = cv2.CascadeClassifier(path)
        return _face_cascade


def _get_face_cascade_alt():
    global _face_cascade_alt
    with _cascade_lock:
        if _face_cascade_alt is None:
            import cv2
            path = cv2.data.haarcascades + "haarcascade_frontalface_alt2.xml"
            _face_cascade_alt = cv2.CascadeClassifier(path)
        return _face_cascade_alt


def detect_face_box(img, log):
    """顔を検出し ((x, y, w, h), eye_mid_y) を返す。検出できなければ None。
    YuNet（同梱 .onnx）を優先し、使えない場合は Haar にフォールバック（eye_mid_y=None）。"""
    try:
        import cv2  # noqa: F401
        import numpy as np
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] OpenCV未導入のため顔トリミングをスキップ ({e})", f"  [i] OpenCV not installed; skipping face crop ({e})"))
        return None
    yb = detect_face_box_yunet(img, log)
    if yb is not None:
        return yb  # ((x,y,w,h), eye_mid_y)
    try:
        import cv2
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        # 小さい画像は検出用に一時的に拡大（出力には影響しない）
        det_scale = 1.0
        min_dim = min(gray.shape[0], gray.shape[1])
        if min_dim < 700:
            det_scale = 700.0 / min_dim
            gray = cv2.resize(gray, None, fx=det_scale, fy=det_scale,
                              interpolation=cv2.INTER_LINEAR)
        img_h = gray.shape[0]

        casc = _get_face_cascade()
        casc_alt = _get_face_cascade_alt()
        # 1回で取れないことがあるので、検出器と条件を変えながら順に試す
        attempts = [
            (casc,     {"scaleFactor": 1.1,  "minNeighbors": 5}),
            (casc_alt, {"scaleFactor": 1.1,  "minNeighbors": 4}),
            (casc,     {"scaleFactor": 1.05, "minNeighbors": 3}),
            (casc_alt, {"scaleFactor": 1.05, "minNeighbors": 2}),
        ]
        faces = []
        used = -1
        for i, (c, kw) in enumerate(attempts):
            faces = c.detectMultiScale(gray, minSize=(40, 40), **kw)
            if len(faces) > 0:
                used = i
                break
        if len(faces) == 0:
            return None
        # ポートレートでは顔は上側にある。胸/ユニフォーム柄の誤検出を避けるため、
        # 顔の中心が画像の上55%にあるものを優先する。
        upper = [f for f in faces if (f[1] + f[3] / 2) < img_h * 0.55]
        pool = upper if upper else list(faces)
        x, y, w, h = max(pool, key=lambda b: b[2] * b[3])
        # 検出用に拡大していた場合は座標を元のスケールへ戻す
        if det_scale != 1.0:
            x, y, w, h = (x / det_scale, y / det_scale, w / det_scale, h / det_scale)
        return (int(round(x)), int(round(y)), int(round(w)), int(round(h))), None, None, None
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] 顔検出に失敗（全体を使用）: {e}", f"  [i] Face detection failed (using whole image): {e}"))
        return None


def crop_around_face(img, box, size_factor, neck, log=None,
                     eye_mid_y=None, eye_mid_x=None, nose_x=None):
    """顔の高さを基準に正方形クロップ。
    ・eye_mid_y がボックス内20〜46%にある場合: 目を上から40%に配置（FM標準スタイル）
    ・それ以外フォールバック: 顔ボックス上端を上から25%に配置（ひげ誤検出に強い）
    ・ヘアガード: 顔ボックス上端が最低22%の余白を持つよう保証（安全網）
    size_factor: キャンバス高さ = 顔高さ × size_factor（目〜顎距離で正規化）"""
    from PIL import Image
    img = img.convert("RGBA")
    x, y, w, h = box
    cx = x + w / 2.0
    # 水平中心の決定（優先順位）:
    # 1) 目のx中点が有効（ボックス内15〜85%、かつ目yも有効）→ 最精度
    # 2) 鼻xがボックス内15〜85% → 目が使えないひげ顔でのフォールバック
    # 3) ボックス中心（デフォルト）
    valid_eye_x = (eye_mid_x is not None
                   and x + w * 0.15 <= eye_mid_x <= x + w * 0.85
                   and eye_mid_y is not None
                   and y + h * 0.20 <= eye_mid_y <= y + h * 0.46)
    if valid_eye_x:
        cx = eye_mid_x
    elif (nose_x is not None
          and x + w * 0.15 <= nose_x <= x + w * 0.85):
        cx = nose_x
    chin = y + h
    face_center = y + h / 2.0
    canvas_h = size_factor * h

    if False:  # (removed)
        pass
    else:
        # 目座標の妥当性チェック（顔ボックス内の合理的な範囲：上20%〜46%）
        # 46%超は「ひげ・斜め顔でランドマークが下ズレした誤検出」と判定しフォールバックへ
        valid_eye = (eye_mid_y is not None
                     and y + h * 0.20 <= eye_mid_y <= y + h * 0.46)
        if valid_eye:
            # 目〜顎の距離から顔高さを正規化してキャンバスサイズを決定
            # → YuNetのボックスサイズに依存せず、異なる顔でも一貫した比率になる
            # 人体比率: 目は顔上端から40%・顎から60%の位置
            # → 顔高さ推定 = (顎 - 目) / 0.60
            face_h_norm = max((chin - eye_mid_y) / 0.60, h * 0.5)
            canvas_h = size_factor * face_h_norm
            top = eye_mid_y - canvas_h * 0.40
            # 目ベースのクロップ: hair_guard を顔高さ基準にする（canvas_h基準だと eye 40% が崩れる）
            hair_guard = face_h_norm * 0.12
        elif eye_mid_y is not None and eye_mid_y > y + h * 0.70:
            # ランドマーク完全失敗（ひげ誤検出など: 目がボックス高さの70%超）
            # → 成人顔の標準比率で目を推定: 顔ボックス上端から高さの38%地点
            estimated_eye_y = y + h * 0.38
            top = estimated_eye_y - canvas_h * 0.40
            hair_guard = canvas_h * 0.22
        else:
            # フォールバック: 顔ボックス上端（額レベル）を上から25%に配置
            # 目ランドマークより額位置のほうが検出が安定するため
            top = y - canvas_h * 0.25
            hair_guard = canvas_h * 0.22

        # ヘアガード（安全網）: 顔ボックス上端がキャンバス上端から最低 hair_guard の余白を持つよう保証
        if y - top < hair_guard:
            top = y - hair_guard

        bottom = top + canvas_h

    top = int(round(top))
    side = max(1, int(round(bottom - top)))
    left = int(round(cx - side / 2.0))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, (-left, -top))
    return canvas




# ==========================================================================
# OCR（ID自動読み取り）
# ==========================================================================
_ocr_reader = None
_ocr_backend = None  # "rapidocr" / "easyocr"


def get_ocr_reader(log):
    """RapidOCR（torch不要）を優先。無ければEasyOCRにフォールバック。"""
    global _ocr_reader, _ocr_backend
    if _ocr_reader is not None:
        return _ocr_reader
    # 1) RapidOCR（軽量・torch不要・推奨）
    try:
        from rapidocr_onnxruntime import RapidOCR
        log(t("  [i] RapidOCR を初期化中…", "  [i] Initializing RapidOCR…"))
        _ocr_reader = RapidOCR()
        _ocr_backend = "rapidocr"
        return _ocr_reader
    except Exception:  # noqa: BLE001
        pass
    # 2) EasyOCR（入っていれば使う／後方互換）
    try:
        import easyocr
        gpu = False
        try:
            import torch
            gpu = bool(torch.cuda.is_available())
        except Exception:  # noqa: BLE001
            gpu = False
        log(t("  [i] EasyOCR を初期化中…（初回はモデルDLで少し時間がかかります）", "  [i] Initializing EasyOCR… (first run downloads models)"))
        _ocr_reader = easyocr.Reader(["en"], gpu=gpu)
        _ocr_backend = "easyocr"
        return _ocr_reader
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(t(
        "OCRエンジンが見つかりません。コマンドで\n"
        "    py -3.12 -m pip install rapidocr-onnxruntime\n"
        "を実行してください（torch不要で軽量です）。\n"
        "（または「IDを自動読み取り」のチェックを外すと、ファイル名＝IDで動きます）",
        "No OCR engine found. Please run\n"
        "    py -3.12 -m pip install rapidocr-onnxruntime\n"
        "(lightweight, no torch needed).\n"
        '(Or uncheck "Auto-read ID" to use filename-as-ID instead.)'))


# これより横長の画像だけ OCR にかける（顔写真=縦長/正方は即「顔」と判定して高速化）
OCR_MIN_ASPECT = 1.5


def read_text(path, reader):
    import numpy as np
    from PIL import Image
    img = Image.open(path).convert("RGB")
    # OCR高速化のため長辺を1280pxまで縮小（IDの数字は十分読める）
    long_side = max(img.size)
    if long_side > 1280:
        s = 1280 / long_side
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
    arr = np.array(img)
    if _ocr_backend == "rapidocr":
        result, _ = reader(arr)            # [[box, text, score], ...] または None
        if not result:
            return ""
        return " ".join(item[1] for item in result)
    # easyocr
    lines = reader.readtext(arr, detail=0)
    return " ".join(lines)


def classify(path, reader, log):
    """('id', 番号) か ('face', None) を返す。"""
    from PIL import Image
    try:
        w, h = Image.open(path).size
    except Exception:  # noqa: BLE001
        return ("face", None)
    aspect = w / h if h else 1
    # 縦長〜正方形は顔写真とみなして OCR しない（大幅に高速化）
    if aspect < OCR_MIN_ASPECT:
        return ("face", None)
    text = read_text(path, reader)
    m = ID_RE.search(text)
    if m:
        return ("id", m.group(1))
    # 横長で数字列があれば ID とみなす（保険）
    digs = re.findall(r"\d{4,}", text)
    if digs:
        return ("id", max(digs, key=len))
    return ("face", None)


def pair_by_time(faces, ids):
    """各IDスクショに、撮影時刻が最も近い未使用の顔をペアにする。"""
    pairs, used = [], set()
    for idd in sorted(ids, key=lambda x: x["mtime"]):
        best, best_diff = None, None
        for f in faces:
            if f["path"] in used:
                continue
            diff = abs(f["mtime"] - idd["mtime"])
            if best_diff is None or diff < best_diff:
                best, best_diff = f, diff
        if best is not None:
            used.add(best["path"])
            pairs.append((best, idd))
    leftover_faces = [f for f in faces if f["path"] not in used]
    return pairs, leftover_faces


# ==========================================================================
# 一括処理（モード分岐）
# ==========================================================================
def _img_area(path):
    from PIL import Image
    try:
        w, h = Image.open(path).size
        return w * h
    except Exception:  # noqa: BLE001
        return 0


def send_to_trash(path):
    """パスをOSのごみ箱へ送る（復元可能）。追加インストールは不要。"""
    p = str(Path(path).resolve())
    # 1) send2trash があれば利用
    try:
        import send2trash
        send2trash.send2trash(p)
        return True
    except Exception:  # noqa: BLE001
        pass
    # 2) Windows 標準のごみ箱API
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND),
                        ("wFunc", wintypes.UINT),
                        ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR),
                        ("fFlags", ctypes.c_uint16),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE = 3
        FOF_ALLOWUNDO = 0x40
        FOF_NOCONFIRMATION = 0x10
        FOF_SILENT = 0x4
        FOF_NOERRORUI = 0x400
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = p + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return res == 0
    # 3) macOS/Linux で send2trash も無い場合
    raise RuntimeError(t("ごみ箱に送れませんでした（send2trash 未導入）",
                         "Could not move to Recycle Bin (send2trash not installed)"))


def write_config(uids, cfg_path, append=True, newgen=False):
    """config.xml に uid のレコードを書き込む（追記 or 上書き）。
    戻り値: (追加件数, 合計件数)"""
    existing = []
    if append and cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            m = re.search(r'from=["\'](\d+)["\']', line)
            if m:
                existing.append(m.group(1))
    existing_set = set(existing)
    new_uids = [u for u in uids if u not in existing_set]
    prefix = "r-" if newgen else ""
    lines_to_add = [
        f'  <record from="{u}" to="faces/{prefix}{u}"/>\n'
        for u in new_uids
    ]
    if append and cfg_path.exists():
        text = cfg_path.read_text(encoding="utf-8", errors="replace")
        close = text.rfind("</graphics>")
        if close != -1:
            new_text = text[:close] + "".join(lines_to_add) + text[close:]
        else:
            new_text = text.rstrip() + "\n" + "".join(lines_to_add)
        cfg_path.write_text(new_text, encoding="utf-8")
    else:
        header = '<?xml version="1.0" encoding="utf-8"?>\n<graphics>\n'
        footer = "</graphics>\n"
        cfg_path.write_text(header + "".join(lines_to_add) + footer, encoding="utf-8")
    total = len(existing) + len(new_uids) if append else len(new_uids)
    return len(new_uids), total


def dedupe_config(cfg_path):
    """config.xml の重複 from 属性を除去して上書き保存。
    バックアップを .bak に作成する。
    戻り値: (出力パス, 除去件数, 残り件数)"""
    text = cfg_path.read_text(encoding="utf-8", errors="replace")
    seen = set()
    out_lines = []
    removed = 0
    for line in text.splitlines(keepends=True):
        m = re.search(r'from=["\'](\d+)["\']', line)
        if m:
            uid = m.group(1)
            if uid in seen:
                removed += 1
                continue
            seen.add(uid)
        out_lines.append(line)
    bak = cfg_path.with_name(cfg_path.name + ".bak")
    shutil.copy2(cfg_path, bak)
    cfg_path.write_text("".join(out_lines), encoding="utf-8")
    return cfg_path, removed, len(seen)


def process_folder(opts, log, progress=None):
    in_dir = Path(opts["input"]); out_dir = Path(opts["output"])
    if not in_dir.is_dir():
        log(t(f"[!] 入力フォルダがありません: {in_dir}", f"[!] Input folder not found: {in_dir}")); return
    out_dir.mkdir(parents=True, exist_ok=True)

    subdirs = sorted(d for d in in_dir.iterdir() if d.is_dir())
    if subdirs:
        log(t(f"サブフォルダ（ペア）モード: {len(subdirs)} フォルダ", f"Subfolder (pair) mode: {len(subdirs)} folders"))
        _run_subfolder_mode(subdirs, out_dir, opts, log, progress)
        return

    files = sorted(f for f in in_dir.iterdir() if f.suffix.lower() in SUPPORTED_EXT)
    if not files:
        log(t(f"[!] 対応画像が見つかりません: {in_dir}", f"[!] No supported images found: {in_dir}")); return
    if opts["ocr_id"]:
        _run_ocr_mode(files, out_dir, opts, log, progress)
    else:
        _run_filename_mode(files, out_dir, opts, log, progress)


def _run_subfolder_mode(subdirs, out_dir, opts, log, progress):
    """各サブフォルダ＝1ペア。中のIDスクショからIDを読み、顔写真を加工する。"""
    reader = None
    if opts["ocr_id"]:
        try:
            reader = get_ocr_reader(log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [i] OCR使用不可（フォルダ名をIDとして使用）: {e}", f"  [i] OCR unavailable (using folder name as ID): {e}"))
            reader = None

    pairs = []  # (face_path, id_number, subname)
    for sub in subdirs:
        imgs = sorted(f for f in sub.iterdir() if f.suffix.lower() in SUPPORTED_EXT)
        if not imgs:
            log(t(f"  [!] {sub.name}: 画像なし、スキップ", f"  [!] {sub.name}: no images, skipped")); continue
        id_num, faces = None, []
        # ファイル名が数字の画像があれば、それをIDとして使う（IDスクショ不要・OCRも省略）
        named = [f for f in imgs if f.stem.isdigit()]
        if named:
            id_num = named[0].stem
            faces = imgs[:]                      # ID名の画像自身も顔として使える
        elif reader is not None:
            for f in imgs:
                kind, num = classify(f, reader, log)
                if kind == "id" and num and id_num is None:
                    id_num = num
                else:
                    faces.append(f)
        else:
            faces = imgs
        if id_num is None and sub.name.isdigit():
            id_num = sub.name
            faces = faces or imgs
        if id_num is None:
            log(t(f"  [!] {sub.name}: IDを読み取れず、スキップ", f"  [!] {sub.name}: could not read ID, skipped")); continue
        if not faces:
            log(t(f"  [!] {sub.name}: 顔写真が見つからず、スキップ", f"  [!] {sub.name}: no face image found, skipped")); continue
        face = max(faces, key=_img_area)  # 一番大きい画像を顔とみなす
        log(t(f"  [{sub.name}] 顔: {face.name}  =>  {id_num}.png", f"  [{sub.name}] face: {face.name}  =>  {id_num}.png"))
        pairs.append((face, id_num, sub.name))

    if not pairs:
        log(t("[!] 有効なペアがありませんでした。", "[!] No valid pairs found.")); return

    cancel = opts.get("_cancel")
    workers = max(1, int(opts.get("workers", 1)))
    total = len(pairs)
    uids, uids_lock = [], threading.Lock()
    seen, seen_lock = set(), threading.Lock()
    done, done_lock = [0], threading.Lock()

    def _process(idx_item):
        idx, (face, uid, subname) = idx_item
        if cancel and cancel.is_set():
            return
        with seen_lock:
            if uid in seen:
                log(t(f"[skip] ID重複: {uid}（{subname}）", f"[skip] duplicate ID: {uid} ({subname})"))
                return
            seen.add(uid)
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {subname}/{face.name} -> {uid}.png")
        if opts.get("_status"): opts["_status"](face.name)
        t0 = time.monotonic()
        try:
            ok = process_one(face, out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}"))
            ok = False
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)"))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(pairs, 1)))
    else:
        for item in enumerate(pairs, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    if progress:
        progress(total, total)
    _finish(uids, out_dir, opts, log)


def _run_filename_mode(files, out_dir, opts, log, progress):
    cancel = opts.get("_cancel")
    workers = max(1, int(opts.get("workers", 1)))
    total = len(files)
    uids, uids_lock = [], threading.Lock()
    seen, seen_lock = set(), threading.Lock()
    done, done_lock = [0], threading.Lock()

    def _process(idx_f):
        idx, f = idx_f
        if cancel and cancel.is_set():
            return
        uid = f.stem
        with seen_lock:
            if uid in seen:
                log(t(f"[skip] UID重複: {uid}", f"[skip] duplicate UID: {uid}"))
                return
            seen.add(uid)
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {f.name} -> {uid}.png")
        if opts.get("_status"): opts["_status"](f.name)
        t0 = time.monotonic()
        try:
            ok = process_one(f, out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}"))
            ok = False
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)"))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(files, 1)))
    else:
        for item in enumerate(files, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    if progress:
        progress(total, total)
    _finish(uids, out_dir, opts, log)


def _run_ocr_mode(files, out_dir, opts, log, progress):
    reader = get_ocr_reader(log)  # easyocr 未導入なら例外
    log(t(f"画像を仕分け中…（{len(files)}枚／横長のスクショのみOCR）", f"Sorting images… ({len(files)} files; OCR only on wide screenshots)"))
    faces, ids = [], []
    for i, f in enumerate(files, 1):
        kind, number = classify(f, reader, log)
        mtime = f.stat().st_mtime
        if kind == "id":
            log(f"  [ID ] {f.name}  ->  ID: {number}")
            ids.append({"path": f, "mtime": mtime, "number": number})
        else:
            log(t(f"  [顔 ] {f.name}", f"  [face] {f.name}"))
            faces.append({"path": f, "mtime": mtime})

    if not ids:
        log(t("[!] IDスクショが1枚も見つかりませんでした。ID入りのスクショを入れてください。", "[!] No ID screenshots found. Please include screenshots that show the ID."))
        return
    if not faces:
        log(t("[!] 顔写真が1枚も見つかりませんでした。", "[!] No face images found."))
        return

    pairs, leftover = pair_by_time(faces, ids)
    log(t(f"\nペアリング結果: {len(pairs)} 組", f"\nPairing result: {len(pairs)} pair(s)"))
    for face, idd in pairs:
        log(t(f"  顔: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png", f"  face: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png"))
    if leftover:
        log(t(f"  [!] 相方の見つからない顔 {len(leftover)} 枚: ",
              f"  [!] {len(leftover)} face(s) with no matching ID: ")
            + ", ".join(x["path"].name for x in leftover))
    log(t("  ※ 取り違えが無いか、上の対応を一度確認してください。\n", "  * Please double-check the pairings above for mismatches.\n"))

    cancel = opts.get("_cancel")
    workers = max(1, int(opts.get("workers", 1)))
    total = len(pairs)
    uids, uids_lock = [], threading.Lock()
    seen, seen_lock = set(), threading.Lock()
    done, done_lock = [0], threading.Lock()

    def _process(idx_pair):
        idx, (face, idd) = idx_pair
        if cancel and cancel.is_set():
            return
        uid = idd["number"]
        with seen_lock:
            if uid in seen:
                log(t(f"[skip] ID重複: {uid}（{face['path'].name}）", f"[skip] duplicate ID: {uid} ({face['path'].name})"))
                return
            seen.add(uid)
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {face['path'].name} -> {uid}.png")
        if opts.get("_status"): opts["_status"](face["path"].name)
        t0 = time.monotonic()
        try:
            ok = process_one(face["path"], out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}"))
            ok = False
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)"))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(pairs, 1)))
    else:
        for item in enumerate(pairs, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    if progress:
        progress(total, total)
    _finish(uids, out_dir, opts, log)


def _finish(uids, out_dir, opts, log):
    log(t(f"\n完了: {len(uids)} 件を出力 -> {out_dir}", f"\nDone: exported {len(uids)} file(s) -> {out_dir}"))
    if opts["make_config"] and uids:
        cfg = out_dir / "config.xml"
        added, total = write_config(uids, cfg, opts.get("config_append", True),
                                    opts.get("newgen", False))
        tag = t("（newgen: r- 付き）", " (newgen: r- prefix)") if opts.get("newgen", False) else ""
        if opts.get("config_append", True):
            log(t(f"config.xml を更新{tag}: 新規 {added} 件 / 合計 {total} 行 -> {cfg}",
                  f"Updated config.xml{tag}: {added} new / {total} total lines -> {cfg}"))
        else:
            log(t(f"config.xml を生成{tag}: {total} 行 -> {cfg}",
                  f"Generated config.xml{tag}: {total} lines -> {cfg}"))
    # ログファイルを保存
    lines = opts.get("_log_lines")
    if lines is not None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = out_dir / f"processing_log_{ts}.txt"
        try:
            log_path.write_text("\n".join(lines), encoding="utf-8")
            log(t(f"ログを保存しました: {log_path.name}", f"Log saved: {log_path.name}"))
        except Exception as e:  # noqa: BLE001
            log(t(f"[!] ログ保存失敗: {e}", f"[!] Log save failed: {e}"))


# ==========================================================================
# テーマ
# ==========================================================================
PALETTES = {
    "dark": {"bg": "#1b1c1e", "panel": "#27282b", "fg": "#e8e8ea", "sub": "#9aa0a6",
             "accent": "#4f8cff", "accent_hi": "#6ea0ff", "border": "#3a3b3f",
             "trough": "#27282b", "active": "#34353a", "logbg": "#161718", "logfg": "#d6e2ff"},
    "light": {"bg": "#f4f5f7", "panel": "#ffffff", "fg": "#1a1c1e", "sub": "#5f6368",
              "accent": "#2563eb", "accent_hi": "#1d4ed8", "border": "#d4d6db",
              "trough": "#e6e7ea", "active": "#e9eaee", "logbg": "#fbfbfc", "logfg": "#222831"},
}


def set_titlebar_dark(window, dark=True):
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:  # noqa: BLE001
        pass


# ==========================================================================
# GUI
# ==========================================================================
# 顔の大きさプリセット（言語非依存の内部キー＋表示ラベル）
ZOOM_SIZE = [2.20, 1.65]  # 標準/FM標準
ZOOM_NECK = [0.10, 0.05]  # 参考値（現バージョンでは直接使用しない）
ZOOM_LABELS = {
    "ja": ["標準", "FM標準"],
    "en": ["Normal", "FM Standard"],
}


class App(tk.Tk):
    SETTINGS_PATH = Path.home() / ".fm_face_processor.json"

    def __init__(self):
        super().__init__()
        self.title(f"FM Face Processor {APP_VERSION}")
        self.geometry("720x860")
        self.minsize(640, 760)
        self.q = queue.Queue()
        self.mode = "dark"
        self.lang = "ja"
        self.zoom_idx = 1            # 既定: FM標準
        self.last_out = None
        self.log_lines = []          # ログ本文を保持（言語切替時に再描画はしないが保持用）
        self.log_has_content = False # 実処理のログが出たらTrue（案内文だけならFalse）
        self.style = ttk.Style()
        self._init_vars()
        self._preload_settings()     # 言語・テーマ・各値をウィジェット生成前に読み込む
        set_lang(self.lang)
        self.outer = None
        self._build()
        self.apply_theme(self.mode)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---------- tk変数（1度だけ作る。再ビルドでも値が残る）----------
    def _init_vars(self):
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar(value="processed")
        self.size_var = tk.IntVar(value=180)
        self.scale_var = tk.DoubleVar(value=4.0)
        self.fit_var = tk.StringVar(value="contain")
        self.model_var = tk.StringVar(value="isnet-general-use")
        self.matting_var = tk.BooleanVar(value=False)
        self.ocr_var = tk.BooleanVar(value=True)
        self.facecrop_var = tk.BooleanVar(value=True)
        self.upscale_var = tk.BooleanVar(value=True)
        self.ai_var = tk.BooleanVar(value=False)
        self.bg_var = tk.BooleanVar(value=True)
        self.cfg_var = tk.BooleanVar(value=True)
        self.append_var = tk.BooleanVar(value=True)
        self.newgen_var = tk.BooleanVar(value=False)
        self.preview_var = tk.BooleanVar(value=False)
        self.lowpower_var = tk.BooleanVar(value=False)
        self.workers_var = tk.IntVar(value=2)
        self.save_log_var = tk.BooleanVar(value=True)
        self.removebg_var = tk.StringVar(value="")
        self.use_removebg_var = tk.BooleanVar(value=False)
        self.ow_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)

    # ---------- 設定の保存・復元 ----------
    def _collect_settings(self):
        return {
            "mode": self.mode, "lang": self.lang, "zoom_idx": self.zoom_idx,
            "input": self.in_var.get(), "output": self.out_var.get(),
            "size": self.size_var.get(), "scale": self.scale_var.get(),
            "fit": self.fit_var.get(), "model": self.model_var.get(),
            "matting": self.matting_var.get(), "ocr": self.ocr_var.get(),
            "facecrop": self.facecrop_var.get(), "upscale": self.upscale_var.get(),
            "ai": self.ai_var.get(), "bg": self.bg_var.get(), "cfg": self.cfg_var.get(),
            "append": self.append_var.get(), "newgen": self.newgen_var.get(),
            "preview": self.preview_var.get(),
            "lowpower": self.lowpower_var.get(),
            "workers": self.workers_var.get(),
            "save_log": self.save_log_var.get(),
            "removebg_key": self.removebg_var.get().strip(),
            "use_removebg": self.use_removebg_var.get(),
            "overwrite": self.ow_var.get(),
            "debug": self.debug_var.get(),
        }

    def _preload_settings(self):
        try:
            if not self.SETTINGS_PATH.exists():
                return
            d = json.loads(self.SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        self.mode = d.get("mode", self.mode)
        self.lang = d.get("lang", self.lang)
        self.zoom_idx = min(int(d.get("zoom_idx", self.zoom_idx)), len(ZOOM_SIZE) - 1)

        for key, var in (
            ("input",       self.in_var),
            ("output",      self.out_var),
            ("fit",         self.fit_var),
            ("model",       self.model_var),
            ("size",        self.size_var),
            ("scale",       self.scale_var),
            ("removebg_key", self.removebg_var),
            ("use_removebg", self.use_removebg_var),
            ("matting",     self.matting_var),
            ("ocr",         self.ocr_var),
            ("facecrop",    self.facecrop_var),
            ("upscale",     self.upscale_var),
            ("ai",          self.ai_var),
            ("bg",          self.bg_var),
            ("cfg",         self.cfg_var),
            ("append",      self.append_var),
            ("newgen",      self.newgen_var),
            ("overwrite",   self.ow_var),
            ("preview",     self.preview_var),
            ("lowpower",    self.lowpower_var),
            ("workers",     self.workers_var),
            ("save_log",    self.save_log_var),
            ("debug",       self.debug_var),
        ):
            try:
                var.set(d.get(key, var.get()))
            except Exception:  # noqa: BLE001
                pass

    def _save_settings(self):
        try:
            self.SETTINGS_PATH.write_text(
                json.dumps(self._collect_settings(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _on_removebg_toggle(self):
        """remove.bg チェックボックスのトグル：キー入力欄を表示/非表示"""
        if self.use_removebg_var.get():
            self._apikey_frame.pack(fill="x", pady=(0, 4))
        else:
            self._apikey_frame.pack_forget()

    def _on_close(self):
        self._save_settings()
        self.destroy()

    def _build(self):
        if getattr(self, "outer", None) is not None:
            self.outer.destroy()
        self.outer = outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # ── ヘッダー ────────────────────────────────────────────
        header = ttk.Frame(outer); header.pack(fill="x", pady=(0, 6))
        titlerow = ttk.Frame(header); titlerow.pack(side="left", fill="x", expand=True)
        titles = []
        lbl = ttk.Label(titlerow, text="FM Face Processor", style="Header.TLabel")
        lbl.pack(side="left")
        titles.append(lbl)
        lbl2 = ttk.Label(titlerow, text=f"  {APP_VERSION}", style="Ver.TLabel")
        lbl2.pack(side="left")
        titles.append(lbl2)
        sub = ttk.Label(titlerow,
                        text=t("顔画像＋IDスクショ → 透過PNG ＋ config.xml",
                               "Face + ID screenshot -> transparent PNG + config.xml"),
                        style="Sub.TLabel")
        sub.pack(side="left", padx=(10, 0))
        titles.append(sub)

        btns = ttk.Frame(header); btns.pack(side="right")
        self.lang_btn = ttk.Button(btns,
                                   text="EN" if self.lang == "ja" else "日本語",
                                   command=self._toggle_lang, width=4)
        self.lang_btn.pack(side="right", padx=(4, 0))
        self.theme_btn = ttk.Button(btns, text="", command=self._toggle_theme, width=3)
        self.theme_btn.pack(side="right")

        # ── フォルダ選択 ────────────────────────────────────────
        fld = ttk.LabelFrame(outer, text=t("フォルダ", "Folders"), padding=8)
        fld.pack(fill="x", pady=(0, 6))
        fld.columnconfigure(1, weight=1)
        ttk.Label(fld, text=t("入力", "Input")).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(fld, textvariable=self.in_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(fld, text="…", command=self._pick_in, width=3).grid(row=0, column=2)
        ttk.Label(fld, text=t("出力", "Output")).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(fld, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(fld, text="…", command=self._pick_out, width=3).grid(row=1, column=2)

        prow = ttk.Frame(fld); prow.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(prow, text=t("ペアフォルダを作る", "Make pair folders"),
                   command=self._make_pair_folders).pack(side="left")
        ttk.Button(prow, text=t("入力元画像をゴミ箱へ", "Trash source images"),
                   command=self._delete_source_images).pack(side="left", padx=(8, 0))
        ttk.Button(prow, text=t("config.xml の重複を除去", "Clean duplicate IDs in config.xml"),
                   command=self._dedupe_config_dialog).pack(side="left", padx=(8, 0))

        # ── オプション ──────────────────────────────────────────
        opt = ttk.LabelFrame(outer, text=t("オプション", "Options"), padding=8)
        opt.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(opt); row.pack(fill="x", pady=(0, 4))
        ttk.Label(row, text=t("出力サイズ", "Output size")).pack(side="left")
        ttk.Entry(row, textvariable=self.size_var, width=6).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="px", style="Sub.TLabel").pack(side="left", padx=(2, 16))
        ttk.Label(row, text=t("拡大倍率", "Upscale factor")).pack(side="left")
        ttk.Entry(row, textvariable=self.scale_var, width=5).pack(side="left", padx=(6, 0))
        ttk.Label(row, text="x", style="Sub.TLabel").pack(side="left", padx=(2, 16))
        ttk.Label(row, text=t("フィット", "Fit")).pack(side="left")
        ttk.Combobox(row, textvariable=self.fit_var, width=9, state="readonly",
                     values=["contain", "cover", "stretch"]).pack(side="left", padx=(6, 16))
        ttk.Label(row, text=t("顔の大きさ", "Face size")).pack(side="left")
        self.zoom_cb = ttk.Combobox(row, width=10, state="readonly",
                                    values=ZOOM_LABELS[self.lang])
        self.zoom_cb.current(self.zoom_idx)
        self.zoom_cb.bind("<<ComboboxSelected>>", self._on_zoom)
        self.zoom_cb.pack(side="left", padx=6)

        row2 = ttk.Frame(opt); row2.pack(fill="x", pady=(0, 4))
        ttk.Label(row2, text=t("背景モデル", "BG model")).pack(side="left")
        ttk.Combobox(row2, textvariable=self.model_var, width=20, state="readonly",
                     values=["isnet-general-use", "birefnet-general", "birefnet-general-lite",
                             "birefnet-portrait", "u2net_human_seg", "u2net"]).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(row2, text=t("髪のフチをなめらかにする", "Smooth hair edges (alpha matting)"),
                        variable=self.matting_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(row2, text=t("remove.bg API を使う（髪のフチが綺麗に）",
                                     "Use remove.bg API (cleaner hair edges)"),
                        variable=self.use_removebg_var,
                        command=self._on_removebg_toggle).pack(side="left")

        self._apikey_frame = rowk = ttk.Frame(opt)
        ttk.Label(rowk, text=t("remove.bg APIキー（任意・髪がきれいに）",
                               "remove.bg API key (optional, best hair)")).pack(side="left")
        self._apikey_entry = ttk.Entry(rowk, textvariable=self.removebg_var, width=30, show="●")
        self._apikey_entry.pack(side="left", padx=(6, 0))
        self._apikey_show = tk.BooleanVar(value=False)
        def _toggle_key():
            self._apikey_entry.config(show="" if self._apikey_show.get() else "●")
        ttk.Checkbutton(rowk, text=t("表示", "Show"), variable=self._apikey_show,
                        command=_toggle_key).pack(side="left", padx=(6, 0))
        if self.use_removebg_var.get():
            rowk.pack(fill="x", pady=(0, 4))

        chk = ttk.Frame(opt); chk.pack(fill="both", expand=True)
        chk.columnconfigure(0, weight=1)
        chk.columnconfigure(1, weight=1)
        ttk.Checkbutton(chk, text=t("IDを自動で読み取る", "Auto-read ID"),
                        variable=self.ocr_var).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("顔を中心に切り抜く", "Crop around face"),
                        variable=self.facecrop_var).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("画像を拡大する", "Upscale"),
                        variable=self.upscale_var).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("AI高画質化 (Real-ESRGAN)", "AI upscale (Real-ESRGAN)"),
                        variable=self.ai_var).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("背景を消して透過にする", "Remove background (transparent)"),
                        variable=self.bg_var).grid(row=2, column=0, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("保存前にプレビュー", "Preview before saving"),
                        variable=self.preview_var).grid(row=2, column=1, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("config.xml を作る", "Generate config.xml"),
                        variable=self.cfg_var).grid(row=3, column=0, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("config.xml に書き足す（重複無視）", "Append to config.xml (skip dups)"),
                        variable=self.append_var).grid(row=3, column=1, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("作成済みでも作り直す", "Overwrite existing"),
                        variable=self.ow_var).grid(row=4, column=0, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("生成選手 (newgen) に対応する — ID に r- を付ける",
                                    "Add r- prefix for newgen players"),
                        variable=self.newgen_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("低負荷モード（他のアプリにCPUを譲る・処理は遅くなる）",
                                    "Low-load mode (yields CPU to other apps; slower)"),
                        variable=self.lowpower_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)
        wrow = ttk.Frame(chk); wrow.grid(row=7, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(wrow, text=t("並列処理数", "Parallel workers")).pack(side="left")
        ttk.Spinbox(wrow, textvariable=self.workers_var, from_=1, to=8, width=4,
                    state="readonly").pack(side="left", padx=(6, 0))
        ttk.Label(wrow, text=t("  （1=順番、2以上=同時処理で高速）",
                               "  (1=sequential, 2+=parallel/faster)"),
                  style="Sub.TLabel").pack(side="left")
        ttk.Checkbutton(chk, text=t("ログをファイルに保存する（出力フォルダに processing_log_*.txt）",
                                    "Save log to file (processing_log_*.txt in output folder)"),
                        variable=self.save_log_var).grid(row=8, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("デバッグモード（診断ログを表示）", "Debug mode (show diagnostic logs)"),
                        variable=self.debug_var).grid(row=9, column=0, columnspan=2, sticky="w", pady=2)

        act = ttk.Frame(outer); act.pack(fill="x", pady=(0, 8))
        self.run_btn = ttk.Button(act, text=t("実行", "Run"), style="Accent.TButton", command=self._run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(act, text=t("キャンセル", "Cancel"),
                                     style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(act, text=t("出力フォルダを開く", "Open output folder"),
                                   command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        ttk.Button(act, text=t("ログをすべて削除", "Delete all logs"),
                   command=self._delete_logs).pack(side="left", padx=8)

        self.pb = ttk.Progressbar(outer, mode="determinate"); self.pb.pack(fill="x", pady=(0, 4))
        self.status_lbl = ttk.Label(outer, text="", style="Sub.TLabel")
        self.status_lbl.pack(fill="x", pady=(0, 4))

        logfrm = ttk.Frame(outer); logfrm.pack(fill="both", expand=True)
        self.log = tk.Text(logfrm, height=12, wrap="word", relief="flat",
                           borderwidth=0, font=("Consolas", 10))
        sb = ttk.Scrollbar(logfrm, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        if self.log_lines:
            self.log.insert("end", "\n".join(self.log_lines))
            self.log.see("end")
            self.log_has_content = True
        else:
            self._log(t("顔写真とIDスクショを同じフォルダに入れて「実行」を押してください。",
                        "Put face photos and ID screenshots in one folder, then press Run."))

        self._refresh_theme_btn()
        self.apply_theme(self.mode)

    def apply_theme(self, mode):
        self.mode = mode
        if HAS_SVTTK:
            sv_ttk.set_theme(mode)
        else:
            self._apply_clam("dark" if mode == "dark" else "light")
        self._refresh_theme_btn()

    def _refresh_theme_btn(self):
        if hasattr(self, "theme_btn"):
            self.theme_btn.config(text="☀" if self.mode == "dark" else "🌙")

    def _apply_clam(self, mode):
        s = ttk.Style(self)
        s.theme_use("clam")
        if mode == "dark":
            pal = {"bg": "#1e1e2e", "fg": "#cdd6f4", "sel": "#45475a",
                   "acc": "#89b4fa", "sub": "#6c7086", "entry": "#313244",
                   "btn": "#313244", "btnfg": "#cdd6f4", "danger": "#f38ba8",
                   "hover": "#585b70"}
        else:
            pal = {"bg": "#ffffff", "fg": "#1e1e2e", "sel": "#cdd6f4",
                   "acc": "#1e66f5", "sub": "#9ca0b0", "entry": "#eff1f5",
                   "btn": "#e6e9ef", "btnfg": "#1e1e2e", "danger": "#d20f39",
                   "hover": "#d1d5de"}
        self.configure(background=pal["bg"])
        s.configure(".", background=pal["bg"], foreground=pal["fg"], bordercolor=pal["sel"],
                    troughcolor=pal["sel"], fieldbackground=pal["entry"],
                    selectbackground=pal["sel"], selectforeground=pal["fg"])
        s.configure("TFrame", background=pal["bg"])
        s.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        s.configure("Sub.TLabel", foreground=pal["sub"])
        s.configure("Header.TLabel", font=("", 14, "bold"), foreground=pal["fg"])
        s.configure("Ver.TLabel", foreground=pal["sub"])
        s.configure("TEntry", fieldbackground=pal["entry"])
        s.configure("TButton", background=pal["btn"], foreground=pal["btnfg"],
                    bordercolor=pal["sel"], lightcolor=pal["btn"], darkcolor=pal["btn"])
        s.configure("Accent.TButton", background=pal["acc"], foreground="#ffffff",
                    bordercolor=pal["acc"], lightcolor=pal["acc"], darkcolor=pal["acc"])
        s.configure("Danger.TButton", background=pal["danger"], foreground="#ffffff",
                    bordercolor=pal["danger"], lightcolor=pal["danger"], darkcolor=pal["danger"])
        s.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"])
        s.configure("TCombobox", fieldbackground=pal["entry"], foreground=pal["fg"],
                    selectbackground=pal["sel"], selectforeground=pal["fg"],
                    bordercolor=pal["sel"], lightcolor=pal["sel"], darkcolor=pal["sel"],
                    arrowcolor=pal["fg"])
        s.configure("TSpinbox", fieldbackground=pal["entry"], foreground=pal["fg"],
                    bordercolor=pal["sel"], arrowcolor=pal["fg"])
        s.configure("TLabelframe", background=pal["bg"], bordercolor=pal["sel"])
        s.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["fg"])
        s.configure("Horizontal.TProgressbar", troughcolor=pal["sel"],
                    background=pal["acc"])
        # ── ホバー/アクティブ状態のマップ ──
        s.map("TButton",
              background=[("pressed", pal["sel"]), ("active", pal["hover"])],
              foreground=[("active", pal["fg"])],
              bordercolor=[("active", pal["hover"])])
        s.map("Accent.TButton",
              background=[("pressed", pal["acc"]), ("active", pal["acc"])],
              foreground=[("active", "#ffffff")])
        s.map("Danger.TButton",
              background=[("pressed", pal["danger"]), ("active", pal["danger"])],
              foreground=[("active", "#ffffff")])
        s.map("TCheckbutton",
              background=[("active", pal["bg"])],
              foreground=[("active", pal["fg"])])
        s.map("TCombobox",
              fieldbackground=[("readonly", pal["entry"]), ("focus", pal["entry"])],
              foreground=[("readonly", pal["fg"]), ("focus", pal["fg"])],
              selectbackground=[("focus", pal["sel"])],
              selectforeground=[("focus", pal["fg"])],
              bordercolor=[("focus", pal["acc"])])
        # ── コンボボックスのドロップダウン色（option_add + 開いたときにTclで直接適用）──
        for key, val in (
            ("*TCombobox*Listbox.background", pal["entry"]),
            ("*TCombobox*Listbox.foreground", pal["fg"]),
            ("*TCombobox*Listbox.selectBackground", pal["sel"]),
            ("*TCombobox*Listbox.selectForeground", pal["fg"]),
            ("*TCombobox*Listbox.relief", "flat"),
            ("*TCombobox*Listbox.borderWidth", "0"),
        ):
            self.option_add(key, val, "interactive")
        # ドロップダウンが開いたタイミングでTclから直接スタイル適用
        self._cb_dropdown_pal = pal
        self._patch_combobox_popups(self)
        if hasattr(self, "log"):
            self.log.configure(background=pal["entry"], foreground=pal["fg"],
                               insertbackground=pal["fg"])

    def _patch_combobox_popups(self, parent):
        """全コンボボックスにドロップダウン開閉時のスタイルパッチを適用"""
        for widget in parent.winfo_children():
            if isinstance(widget, ttk.Combobox):
                widget.unbind("<<ComboboxDropdown>>")
                widget.bind("<<ComboboxDropdown>>",
                            lambda _e, w=widget: self._style_cb_popup(w), add="+")
            self._patch_combobox_popups(widget)

    def _style_cb_popup(self, cb):
        """コンボボックスのポップアップlistboxを直接Tclでスタイリング"""
        pal = getattr(self, "_cb_dropdown_pal", None)
        if pal is None:
            return
        try:
            popdown = self.tk.eval(f"ttk::combobox::PopdownWindow {cb}")
            lb = f"{popdown}.f.l"
            self.tk.eval(
                f"{lb} configure"
                f" -background {pal['entry']}"
                f" -foreground {pal['fg']}"
                f" -selectbackground {pal['sel']}"
                f" -selectforeground {pal['fg']}"
                f" -relief flat -borderwidth 0"
            )
        except Exception:  # noqa: BLE001
            pass

    def _toggle_theme(self):
        self.apply_theme("light" if self.mode == "dark" else "dark")

    def _toggle_lang(self):
        self.lang = "en" if self.lang == "ja" else "ja"
        set_lang(self.lang)
        self._build()
        self.after(100, self._poll)

    def _on_zoom(self, _evt=None):
        self.zoom_idx = self.zoom_cb.current()

    def _pick_in(self):
        d = filedialog.askdirectory()
        if d:
            self.in_var.set(d)

    def _pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    def _make_pair_folders(self):
        base = self.in_var.get()
        if not base:
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダを先に選んでください。",
                                     "Please select the input folder first."))
            return
        n = simpledialog.askinteger(t("ペアフォルダを作る", "Make pair folders"),
                                    t("作るフォルダの数:", "Number of folders to create:"),
                                    minvalue=1, maxvalue=200)
        if not n:
            return
        made = 0
        for i in range(1, n + 1):
            p = Path(base) / f"pair_{i:03d}"
            p.mkdir(exist_ok=True)
            made += 1
        messagebox.showinfo(t("完了", "Done"),
                            t(f"{made} 個のフォルダを作成しました。\n各フォルダに「顔写真」と「IDスクショ」を1枚ずつ入れてから実行してください。",
                              f"Created {made} folders.\nPut one face photo and one ID screenshot in each, then run."))

    def _delete_source_images(self):
        base = self.in_var.get()
        if not base:
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダを先に選んでください。",
                                     "Please select the input folder first."))
            return
        p = Path(base)
        if not p.exists():
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダが見つかりません。", "Input folder not found."))
            return
        subs_with_imgs = [d for d in sorted(p.iterdir()) if d.is_dir()]
        targets = []
        for sub in subs_with_imgs:
            imgs = [f for f in sub.iterdir() if f.suffix.lower() in SUPPORTED_EXT]
            targets.extend(imgs)
        if not targets:
            messagebox.showinfo(t("情報", "Info"),
                                t("削除する画像がありません。", "No images found to delete."))
            return
        if not messagebox.askyesno(
                t("元画像をゴミ箱へ", "Trash source images"),
                t(f"{len(targets)} 枚の元画像を入力フォルダ内の各サブフォルダから"
                  " をごみ箱に送ります。\nフォルダ自体は残します。出力フォルダの完成画像は消えません。\n（ごみ箱から元に戻せます）\n\nよろしいですか？",
                  f"Send {len(targets)} image(s)"
                  " inside the input folder to the Recycle Bin.\nFolders are kept. Finished images in the output folder are safe.\n(Can be restored from Recycle Bin)\n\nProceed?")):
            return
        failed = 0
        for f in targets:
            try:
                send_to_trash(f)
            except Exception as e:  # noqa: BLE001
                self._log(t(f"  [!] 削除失敗: {f.name}: {e}", f"  [!] Could not delete: {f.name}: {e}"))
                failed += 1
        done_n = len(targets) - failed
        msg = t(f"{done_n} 枚をごみ箱に送りました。", f"Moved {done_n} image(s) to the Recycle Bin.")
        if failed:
            msg += t(f"\n{failed} 枚は削除できませんでした。", f"\n{failed} image(s) could not be deleted.")
        messagebox.showinfo(t("完了", "Done"), msg)

    def _dedupe_config_dialog(self):
        start = filedialog.askopenfilename(
            title=t("掃除する config.xml を選んでください", "Choose a config.xml to clean"),
            filetypes=[("XML", "*.xml"), ("All", "*.*")])
        if not start:
            return
        try:
            out, removed, kept = dedupe_config(Path(start))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t("エラー", "Error"), str(e))
            return
        if removed == 0:
            messagebox.showinfo(t("重複なし", "No Duplicates"),
                                t(f"config.xml: 重複なし（{kept} 件）。",
                                  f"config.xml: no duplicates ({kept} remain)."))
        else:
            messagebox.showinfo(t("完了", "Done"),
                                t(f"{removed} 件の重複を削除しました（残り {kept} 件）。\n元のファイルは config.xml.bak に保存しました。",
                                  f"Removed {removed} duplicate(s) ({kept} remain).\nThe original was saved as config.xml.bak."))

    def _cancel(self):
        if hasattr(self, "_cancel_event"):
            self._cancel_event.set()
        self.cancel_btn.config(state="disabled")
        self._log(t("[i] キャンセルを要求しました…現在の画像が終わったら停止します。",
                    "[i] Cancel requested… will stop after the current image finishes."))

    def _open_out(self):
        if not self.last_out:
            return
        try:
            if sys.platform == "win32":
                os.startfile(self.last_out)  # noqa: S606
            elif sys.platform == "darwin":
                os.system(f'open "{self.last_out}"')
            else:
                os.system(f'xdg-open "{self.last_out}"')
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning(t("確認", "Notice"),
                                   t(f"フォルダを開けませんでした: {e}", f"Could not open folder: {e}"))

    def _delete_logs(self):
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning(t("ログ削除", "Delete Logs"),
                                   t("出力フォルダが設定されていません。", "Output folder is not set."))
            return
        from pathlib import Path as _Path
        logs = list(_Path(out).glob("processing_log_*.txt"))
        if not logs:
            messagebox.showinfo(t("ログ削除", "Delete Logs"),
                                t("削除するログファイルがありません。", "No log files to delete."))
            return
        if messagebox.askyesno(
                t("ログをすべて削除", "Delete All Logs"),
                t(f"出力フォルダ内のログファイル {len(logs)} 件を削除しますか？\n（この操作は元に戻せません）",
                  f"Delete {len(logs)} log file(s) from the output folder?\n(This cannot be undone)")):
            for lf in logs:
                try:
                    lf.unlink()
                except Exception:
                    pass
            messagebox.showinfo(t("ログ削除", "Delete Logs"),
                                t(f"{len(logs)} 件のログを削除しました。",
                                  f"Deleted {len(logs)} log file(s)."))

    def _log(self, msg):
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log_has_content = True

    def _show_preview(self, b64, ev, holder):
        """保存前プレビューのダイアログ。選択をholderに入れてevをセットする。"""
        import base64
        from PIL import Image as _I, ImageTk
        data = base64.b64decode(b64)
        import io as _io
        img = _I.open(_io.BytesIO(data))
        photo = ImageTk.PhotoImage(img)
        win = tk.Toplevel(self)
        win.title(t("プレビュー", "Preview"))
        win.grab_set()
        lbl = ttk.Label(win, image=photo)
        lbl.image = photo
        lbl.pack(padx=12, pady=12)
        btns = ttk.Frame(win); btns.pack(pady=(0, 12))

        def answer(r):
            holder["r"] = r
            win.destroy()
            ev.set()

        ttk.Button(btns, text=t("保存", "Save"),
                   command=lambda: answer("save")).pack(side="left", padx=4)
        ttk.Button(btns, text=t("スキップ", "Skip"),
                   command=lambda: answer("skip")).pack(side="left", padx=4)
        ttk.Button(btns, text=t("全部保存", "Save all"),
                   command=lambda: answer("all")).pack(side="left", padx=4)
        ttk.Button(btns, text=t("キャンセル", "Cancel"),
                   command=lambda: answer("cancel")).pack(side="left", padx=4)
        win.wait_window()
        if not ev.is_set():
            ev.set()

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    done, total = payload
                    self.pb.configure(maximum=max(total, 1), value=done)
                elif kind == "status":
                    self._current_file = payload
                elif kind == "preview":
                    b64, ev, holder = payload
                    self._show_preview(b64, ev, holder)
                elif kind == "done":
                    elapsed = time.monotonic() - getattr(self, "_run_start", time.monotonic())
                    self.run_btn.config(state="normal", text=t("実行", "Run"))
                    self.cancel_btn.config(state="disabled")
                    self.status_lbl.config(text="")
                    self._current_file = ""
                    self._save_settings()
                    if payload:
                        messagebox.showerror(t("エラー", "Error"), str(payload))
                    else:
                        self.open_btn.config(state="normal")
                        elapsed_msg = t(f"処理が終わりました（{elapsed:.0f}秒）。\nログでペアの対応を確認してください。",
                                        f"Finished in {elapsed:.0f}s.\nCheck the log for the pairings.")
                        messagebox.showinfo(t("完了", "Done"), elapsed_msg)
        except queue.Empty:
            pass
        except Exception as _poll_err:  # noqa: BLE001
            # _poll内で予期しない例外が起きてもポーリングを継続する
            try:
                self._log(f"[!] 内部エラー（処理は継続します）: {_poll_err}")
            except Exception:  # noqa: BLE001
                pass
        try:
            if self.run_btn["state"] == "disabled" and hasattr(self, "_run_start"):
                elapsed = time.monotonic() - self._run_start
                fname = getattr(self, "_current_file", "")
                text = t(f"処理中… {elapsed:.0f}秒経過", f"Processing… {elapsed:.0f}s")
                if fname:
                    text += f"  |  {fname}"
                self.status_lbl.config(text=text)
        except Exception:  # noqa: BLE001
            pass
        self.after(100, self._poll)

    def _run(self):
        if not self.in_var.get():
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダを選んでください。", "Please select the input folder."))
            return
        try:
            idx = self.zoom_idx
            opts = {
                "input":         self.in_var.get(),
                "output":        self.out_var.get() or "processed",
                "size":          int(self.size_var.get()),
                "scale":         float(self.scale_var.get()),
                "ocr_id":        self.ocr_var.get(),
                "face_crop":     self.facecrop_var.get(),
                "face_size":     ZOOM_SIZE[idx],
                "face_neck":     ZOOM_NECK[idx],
                "upscale":       self.upscale_var.get(),
                "ai_upscale":    self.ai_var.get(),
                "bg_removal":    self.bg_var.get(),
                "make_config":   self.cfg_var.get(),
                "config_append": self.append_var.get(),
                "newgen":        self.newgen_var.get(),
                "overwrite":     self.ow_var.get(),
                "fit":           self.fit_var.get(),
                "rembg_model":   self.model_var.get(),
                "alpha_matting": self.matting_var.get(),
                "esrgan_model":  "RealESRGAN_x4plus",
                "removebg_key":  self.removebg_var.get().strip() if self.use_removebg_var.get() else "",
                "debug":         self.debug_var.get(),
            }
        except (tk.TclError, ValueError):
            messagebox.showwarning(t("確認", "Notice"),
                                   t("サイズ・倍率は数値で入力してください。",
                                     "Size and upscale must be numbers."))
            return

        if self.preview_var.get():
            state = {"all": False}

            def preview_cb(pil_img):
                if state["all"]:
                    return "save"
                import base64
                from PIL import Image as _I
                buf = io.BytesIO()
                pil_img.resize((pil_img.width * 2, pil_img.height * 2), _I.NEAREST).save(buf, "PNG")
                ev = threading.Event()
                holder = {}
                self.q.put(("preview", (base64.b64encode(buf.getvalue()).decode("ascii"), ev, holder)))
                ev.wait()
                r = holder.get("r", "save")
                if r == "all":
                    state["all"] = True
                    return "save"
                return r

            opts["_preview"] = preview_cb

        low_power = self.lowpower_var.get()
        opts["workers"] = self.workers_var.get()
        self._cancel_event = threading.Event()
        opts["_cancel"] = self._cancel_event
        opts["_status"] = lambda name: self.q.put(("status", name))
        _log_lines = [] if self.save_log_var.get() else None
        _log_lock = threading.Lock()
        opts["_log_lines"] = _log_lines
        self.last_out = str(Path(opts["output"]).resolve())
        self._run_start = time.monotonic()
        self._current_file = ""

        self.run_btn.config(state="disabled", text=t("処理中…", "Working…"))
        self.cancel_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.pb.configure(value=0, maximum=1)
        self.log.delete("1.0", "end")
        self.log_has_content = False

        def _log_fn(msg):
            self.q.put(("log", msg))
            if _log_lines is not None:
                with _log_lock:
                    _log_lines.append(msg)

        def worker():
            err = ""
            if low_power:
                set_low_power(True)
                set_process_priority(True)
                _log_fn(t("[i] 低負荷モードで実行します（処理は遅めです）",
                           "[i] Running in low-load mode (slower)"))
            try:
                process_folder(opts, _log_fn,
                               progress=lambda d, tot: self.q.put(("progress", (d, tot))))
            except Exception as e:  # noqa: BLE001
                err = e
            finally:
                if low_power:
                    set_process_priority(False)
                    set_low_power(False)
                self.q.put(("done", err))

        threading.Thread(target=worker, daemon=True).start()

if __name__ == "__main__":
       App().mainloop()
