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
  # ドラッグ＆ドロップ: py -3.12 -m pip install tkinterdnd2 （任意・未導入でも全機能使える）
"""

import csv
import io
import os
import re
import shutil
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

# ドラッグ＆ドロップ（任意）: py -3.12 -m pip install tkinterdnd2
# 未導入でも従来どおり「…」ボタンから選べるので、機能はすべて使える。
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAS_DND = True
except Exception:  # noqa: BLE001
    TkinterDnD = None
    DND_FILES = None
    HAS_DND = False

SUPPORTED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"}
APP_VERSION = "v2.0.0"
# 「ID」に続く数字（FMのperson ID）を拾う
# FMのperson IDは5桁以上。2桁以上を拾うと "Real Madrid 2024 Unique ID 1928374651"
# のような文字列で年号や背番号を先に拾ってしまうため下限を上げてある。
ID_RE = re.compile(r"id[^0-9]{0,6}(\d{5,})", re.IGNORECASE)

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
        # 端のタイルはモデル入力サイズに満たない。0（＝黒）で埋めると
        # 受容野の広いRRDBが黒を巻き込んで端が暗く濁るので、辺の色を複製して埋める。
        padded = np.zeros((oh, ow, 3), np.float32)
        padded[:ph, :pw] = tile
        if ph < oh:
            padded[ph:, :pw] = tile[-1:, :]
        if pw < ow:
            padded[:, pw:] = padded[:, pw - 1:pw]
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
        # 最後のタイルは画像の端にぴったり寄せる（はみ出し分をパディングで埋めない）
        y0 = min(y0, max(0, H - tile_h))
        for x0 in range(0, W, step_w):
            x0 = min(x0, max(0, W - tile_w))
            tile = arr[y0:y0 + tile_h, x0:x0 + tile_w]
            res = run_tile(tile)
            oy, ox = y0 * scale, x0 * scale
            rh, rw = res.shape[0], res.shape[1]
            out[oy:oy + rh, ox:ox + rw] += res
            cnt[oy:oy + rh, ox:ox + rw] += 1.0
            if x0 + tile_w >= W:
                break
        if y0 + tile_h >= H:
            break
    cnt[cnt == 0] = 1.0
    out = (out / cnt * 255.0).clip(0, 255).astype(np.uint8)
    return Image.fromarray(out)


def _fill_transparent(img):
    """透明部分を近くの前景色で埋めたRGB画像を返す（超解像時の黒にじみ対策）。"""
    try:
        import numpy as np
        import cv2
        from PIL import Image
        arr = np.asarray(img.convert("RGBA"), dtype=np.uint8)
        a = arr[..., 3]
        if int(a.min()) > 250:
            return img.convert("RGB")
        rgb = np.ascontiguousarray(arr[..., :3])
        mask = (a < 250).astype(np.uint8)
        filled = cv2.inpaint(rgb, mask, 3, cv2.INPAINT_TELEA)
        return Image.fromarray(filled, "RGB")
    except Exception:  # noqa: BLE001
        return img.convert("RGB")


def upscale(img, factor, model_name, use_ai, log):
    from PIL import Image
    if factor <= 1:
        return img
    if use_ai:
        sess = _get_esrgan(log)
        if sess is not None:
            try:
                # Real-ESRGANはRGBしか扱えずアルファを落とすので、退避しておく
                alpha = img.getchannel("A") if img.mode == "RGBA" else None
                # 透明部のRGBは0（黒）なので、そのまま渡すと輪郭に黒がにじむ。
                # 前景の色で埋めてから超解像し、あとでアルファを戻す。
                out = _esrgan_enhance(sess, _fill_transparent(img) if alpha else img, log)
                # 目標倍率に合わせて仕上げ（4倍より小さければ縮小）
                tw, th = int(img.width * factor), int(img.height * factor)
                if (out.width, out.height) != (tw, th):
                    out = out.resize((tw, th), Image.LANCZOS)
                if alpha is not None:
                    out = out.convert("RGBA")
                    out.putalpha(alpha.resize(out.size, Image.LANCZOS))
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


def is_already_cutout(img, min_ratio=0.05):
    """入力画像が既に背景除去済み（透過PNG）かどうかを判定する。
    透過済みの画像を再度背景除去にかけると、透明部分が黒として扱われ、
    黒髪や暗い服が背景と区別できずに削られてしまうため、その前段で使う。"""
    try:
        if img.mode != "RGBA":
            return False
        alpha = img.getchannel("A")
        if alpha.getextrema()[0] > 250:      # 完全不透明＝ただのRGBA画像
            return False
        transparent = sum(alpha.histogram()[:8])
        return transparent > img.width * img.height * float(min_ratio)
    except Exception:  # noqa: BLE001
        return False


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
                out = work
            # サイズが同じで返ってきた場合も同じ後処理を通す（小さい入力だけ品質が変わるのを防ぐ）
            out = _defringe(out, src=img)
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
            import gc
            with _rembg_lock:
                # 再束縛すると、ロック内で辞書を触っている別スレッドと食い違う
                _rembg_sessions.clear()
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
    # 背景色の推定には「除去前」の画像を渡す（rembg は透明部のRGBを0にするため）
    return _defringe(out, src=img)


def _defringe(img, src=None):
    """髪のフチなど半透明部分に混ざった背景色を引き算して、色のにじみを消す。
    背景色は「透明になった領域の元の色」から推定する（単色背景に特に有効）。

    src には背景除去『前』の画像を渡すこと。背景除去『後』の画像は、
    透明になった部分のRGBが (0,0,0) にクリアされている場合があり
    （rembg の naive_cutout、および元から透過PNGだった入力）、
    それを背景色として採用すると「黒＝背景」と誤認して黒髪に穴が開く。"""
    try:
        import numpy as np
        from PIL import Image
        arr = np.asarray(img, dtype=np.float32).copy()
        a = arr[..., 3] / 255.0
        bg_mask = a < 0.04
        if bg_mask.sum() < 100:
            return img
        # 背景色の参照元。除去前の画像があればそちらを使う（RGBが残っているため）
        ref = arr[..., :3]
        if src is not None:
            try:
                if src.size == img.size:
                    ref = np.asarray(src.convert("RGBA"), dtype=np.float32)[..., :3]
            except Exception:  # noqa: BLE001
                pass
        # 背景色は「前景のすぐ外側」から推定する。遠くの領域や、
        # crop_around_face が付ける透明パディングまで混ぜると推定が狂うため。
        ring = bg_mask
        try:
            import cv2 as _cv2
            near = _cv2.dilate((a >= 0.04).astype(np.uint8), np.ones((13, 13), np.uint8))
            r = bg_mask & (near > 0)
            if r.sum() >= 100:
                ring = r
        except Exception:  # noqa: BLE001
            pass
        bg_pixels = ref[ring]
        # RGBが完全に0の画素は背景色ではなく「透明パディング / 0クリア跡」なので除外
        keep = bg_pixels.max(axis=1) > 0
        if int(keep.sum()) >= 50:
            bg_pixels = bg_pixels[keep]
        # 参照できる背景がほぼ真っ黒＝実際の背景色ではない（0クリア済み or 元から透過）。
        # 背景色を推定できないので、色補正もかけら剥がしも行わない（穴あき防止）。
        if float(bg_pixels.max()) < 8.0:
            return img
        bg = np.median(bg_pixels, axis=0)                # 背景色の推定（中央値＝文字等に強い）
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
    # Image.thumbnail は縮小しかしない（小さい画像が透明枠付きのまま出てしまう）ので
    # 拡大・縮小の両方に効く明示的なリサイズを使う
    r = min(size / img.width, size / img.height)
    work = img if r == 1.0 else img.resize(
        (max(1, round(img.width * r)), max(1, round(img.height * r))), Image.LANCZOS)
    canvas = Image.new("RGBA", target, (0, 0, 0, 0))
    # mask を渡すとアルファ同士が乗算されて（out.A = A*A/255）フチが二重に薄くなるため渡さない。
    # 貼り先は完全透明なので単純コピーでよい。
    canvas.paste(work, ((size - work.width) // 2, (size - work.height) // 2))
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
        # スレッド安全性: YuNet検出器インスタンスは共有のため、setInputSize〜detectを
        # 1つのロックで保護する（並列実行時の "Unknown C++ exception" 対策）
        with _yunet_lock:
            det.setInputSize((img_w, img_h))
            _, faces = det.detect(bgr)
        if faces is None or len(faces) == 0:
            return None
        # 上半分の顔を優先（胸のロゴ・柄の誤検出を排除）
        upper = [f for f in faces if (float(f[1]) + float(f[3]) / 2) < img_h * 0.55]
        pool = upper if upper else list(faces)
        # YuNetの1行は15要素:
        #   [0..3]  = x, y, w, h
        #   [4,5]   = 右目 x,y      [6,7]  = 左目 x,y     [8,9] = 鼻 x,y
        #   [10,11] = 右口角 x,y    [12,13]= 左口角 x,y
        #   [14]    = 信頼度スコア
        # ※ スコアは index 4 ではない。4〜13 はランドマーク10要素。
        def _score(f):
            return float(f[14]) if len(f) >= 15 else 1.0
        # 面積 × 信頼度スコアで選択
        best = max(pool, key=lambda f: float(f[2]) * float(f[3]) * _score(f))
        bx, by, bw, bh = float(best[0]), float(best[1]), float(best[2]), float(best[3])
        eye_mid_y = None
        eye_mid_x = None  # 目の水平中点（横ズレ補正用）
        nose_x = None     # 鼻のx座標（目が使えない時の横ズレ補正フォールバック）
        if len(best) >= 10:
            try:
                eye_mid_x = (float(best[4]) + float(best[6])) / 2.0   # 右目x, 左目x
                eye_mid_y = (float(best[5]) + float(best[7])) / 2.0   # 右目y, 左目y
                nose_x    =  float(best[8])                           # 鼻x
            except Exception:
                pass
        elif len(best) >= 8:
            try:
                eye_mid_x = (float(best[4]) + float(best[6])) / 2.0
                eye_mid_y = (float(best[5]) + float(best[7])) / 2.0
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
        # スレッド安全性: Haarカスケード検出器も共有インスタンスのため、
        # detectMultiScale 呼び出し全体をロックで保護する
        with _cascade_lock:
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


# 目をキャンバス上端から何割の位置に置くか。
# 既存FMフェイスパック（180px版24枚）の実測中央値が 49.4% だったため 0.49。
EYE_RATIO = 0.49

# 髪の上端に最低限確保する余白（キャンバス高さに対する割合）。
# 参考パックの頭頂余白は 0〜2% だったので 0.02。
CROWN_MARGIN = 0.02


def _silhouette_top_y(img, x_lo, x_hi):
    """透過画像なら、指定した水平範囲にある被写体シルエットの最上端yを返す。
    髪の実際の上端を知るために使う（顔ボックスは髪の量を含まないため）。
    背景除去前の不透明な画像では判定できないので None を返す。"""
    try:
        import numpy as np
        a = np.asarray(img.convert("RGBA"))[..., 3]
        if int(a.min()) > 250:            # 透明部分が無い＝まだ背景除去前
            return None
        lo = max(0, int(x_lo))
        hi = min(a.shape[1], int(x_hi) + 1)
        if hi - lo < 8:
            return None
        rows = np.nonzero((a[:, lo:hi] > 40).any(axis=1))[0]
        if rows.size == 0:
            return None
        return float(rows.min())
    except Exception:  # noqa: BLE001
        return None


def _silhouette_center_x(img, top, bottom, x_lo=None, x_hi=None):
    """透過画像なら、指定した縦範囲にある被写体シルエットの水平中心を返す。
    横向き・振り向きの顔では目の中点が頭のシルエット中心から大きくズレるため
    （目基準だと頭が片側に寄って見切れる）、透過があるときはこちらを優先する。
    x_lo/x_hi で探索範囲を顔の周辺に限定できる（上げた腕や別人を拾わないため）。
    背景除去前の不透明な画像では判定できないので None を返す。"""
    try:
        import numpy as np
        a = np.asarray(img.convert("RGBA"))[..., 3]
        if int(a.min()) > 250:            # 透明部分が無い＝まだ背景除去前
            return None
        tt = max(0, int(top))
        bb = min(a.shape[0], int(bottom))
        if bb - tt < 8:
            return None
        cols = (a[tt:bb] > 40).any(axis=0)
        lo = 0 if x_lo is None else max(0, int(x_lo))
        hi = a.shape[1] if x_hi is None else min(a.shape[1], int(x_hi) + 1)
        if hi - lo < 8:
            return None
        xs = np.nonzero(cols[lo:hi])[0]
        if xs.size < 8:
            return None
        return float(lo + xs.min() + lo + xs.max()) / 2.0
    except Exception:  # noqa: BLE001
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
            top = eye_mid_y - canvas_h * EYE_RATIO
            anchor_y, anchor_ratio = eye_mid_y, EYE_RATIO
            # 目ベースのクロップ: hair_guard を顔高さ基準にする（canvas_h基準だと eye 40% が崩れる）
            hair_guard = face_h_norm * 0.12
        elif eye_mid_y is not None and eye_mid_y > y + h * 0.70:
            # ランドマーク完全失敗（ひげ誤検出など: 目がボックス高さの70%超）
            # → 成人顔の標準比率で目を推定: 顔ボックス上端から高さの38%地点
            estimated_eye_y = y + h * 0.38
            top = estimated_eye_y - canvas_h * EYE_RATIO
            anchor_y, anchor_ratio = estimated_eye_y, EYE_RATIO
            hair_guard = canvas_h * 0.22
        else:
            # フォールバック: 顔ボックス上端（額レベル）を上から25%に配置
            # 目ランドマークより額位置のほうが検出が安定するため
            top = y - canvas_h * 0.25
            anchor_y, anchor_ratio = float(y), 0.25
            hair_guard = canvas_h * 0.22

        # 頭頂ガード: 透過があれば実際の髪の上端を見て、切れないだけキャンバスを広げる。
        # 顔ボックス基準の hair_guard は髪の量を見ていないので、
        # ボリュームのある髪型（マッシュ・アフロ等）では頭頂が水平に削れる。
        #   条件: 髪の上端 - top >= canvas_h * CROWN_MARGIN
        #   top = anchor_y - canvas_h * anchor_ratio なので
        #   → canvas_h >= (anchor_y - 髪の上端) / (anchor_ratio - CROWN_MARGIN)
        # キャンバスを広げる方向にだけ効かせる（目の位置比率は EYE_RATIO のまま崩さない）
        _fcx = x + w / 2.0
        sil_top = _silhouette_top_y(img, _fcx - w * 1.3, _fcx + w * 1.3)
        # sil_top <= 1 は「元写真の時点で頭が上端で切れている」ケース。
        # そこに余白を作ろうとすると際限なくキャンバスが膨らむので諦める。
        if sil_top is not None and sil_top > 1 and anchor_ratio > CROWN_MARGIN + 0.05:
            # 暴走防止に拡大は1.35倍まで（帽子・挙げた手などを拾った場合の保険）
            need = min((anchor_y - sil_top) / (anchor_ratio - CROWN_MARGIN), canvas_h * 1.35)
            if need > canvas_h:
                if log is not None:
                    log(t(f"  [i] 頭頂が切れるためキャンバスを拡大: {canvas_h:.0f} -> {need:.0f}px",
                          f"  [i] Enlarging canvas to avoid cutting the crown: {canvas_h:.0f} -> {need:.0f}px"))
                canvas_h = need
            top = anchor_y - canvas_h * anchor_ratio
        else:
            # 透過が無い（背景除去前）場合の安全網: 顔ボックス上端に最低 hair_guard の余白
            if y - top < hair_guard:
                top = y - hair_guard

        bottom = top + canvas_h

    top = int(round(top))
    side = max(1, int(round(bottom - top)))
    # 透過があるなら、頭のシルエット中心で左右を取り直す（見切れ防止）。
    # 探索は顔ボックス中心 ±1.3×box幅 に限定（上げた腕や隣の人物を拾わないため）
    _fcx = x + w / 2.0
    sil_cx = _silhouette_center_x(img, top, min(bottom, chin),
                                  x_lo=_fcx - w * 1.3, x_hi=_fcx + w * 1.3)
    if sil_cx is not None:
        if log is not None and abs(sil_cx - cx) > side * 0.02:
            log(t(f"  [i] 水平中心をシルエット基準に補正: {cx:.0f} -> {sil_cx:.0f}",
                  f"  [i] Horizontal center corrected to silhouette: {cx:.0f} -> {sil_cx:.0f}"))
        cx = sil_cx
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
    except Exception as e:  # noqa: BLE001
        # 壊れた画像を顔として扱うと、実在するIDのペア枠を食い潰してしまう
        log(t(f"  [!] 画像を開けません（スキップ）: {Path(path).name}: {e}",
              f"  [!] Cannot open image (skipped): {Path(path).name}: {e}"))
        return ("skip", None)
    aspect = w / h if h else 1
    # 縦長〜正方形は顔写真とみなして OCR しない（大幅に高速化）
    if aspect < OCR_MIN_ASPECT:
        return ("face", None)
    text = read_text(path, reader)
    # 最初のマッチではなく候補の中で最長のものを採用する
    # （OCRは画面上の全テキストを連結して返すので、年号や背番号が先に来ることがある）
    cands = ID_RE.findall(text)
    if cands:
        return ("id", max(cands, key=len))
    # 横長で数字列があれば ID とみなす（保険）
    digs = re.findall(r"\d{5,}", text)
    if digs:
        return ("id", max(digs, key=len))
    return ("face", None)


# ペアと認める撮影時刻の最大差（秒）。これを超えたら「相方なし」として扱う。
MAX_PAIR_GAP_SEC = 120.0


def pair_by_time(faces, ids):
    """IDスクショと顔写真を撮影時刻でペアにする。
    戻り値: (pairs, 相方の無い顔, 相方の無いID)

    「各IDについて一番近い顔を取る」貪欲法だと、余分なIDスクショが1枚あるだけで
    無関係な顔を先に掴み、それ以降のペアが芋づる式に1つずつズレて
    他人の顔が別のIDで保存されてしまう。
    そのため時刻順を保ったまま全体の時間差合計が最小になる組み合わせをDPで選ぶ。
    どちらか一方を余らせるコストを MAX_PAIR_GAP_SEC/2 とすることで、
    時間差が MAX_PAIR_GAP_SEC 以上離れたペアは自動的に「相方なし」になる。"""
    fs = sorted(faces, key=lambda x: x["mtime"])
    ds = sorted(ids, key=lambda x: x["mtime"])
    n, mm = len(ds), len(fs)
    skip = MAX_PAIR_GAP_SEC / 2.0
    INF = float("inf")
    # dp[i][j]: ID を i 件、顔を j 枚まで処理したときの最小コスト
    dp = [[INF] * (mm + 1) for _ in range(n + 1)]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(mm + 1):
            cur = dp[i][j]
            if cur == INF:
                continue
            if i < n and cur + skip < dp[i + 1][j]:
                dp[i + 1][j] = cur + skip
            if j < mm and cur + skip < dp[i][j + 1]:
                dp[i][j + 1] = cur + skip
            if i < n and j < mm:
                c = cur + abs(ds[i]["mtime"] - fs[j]["mtime"])
                if c < dp[i + 1][j + 1]:
                    dp[i + 1][j + 1] = c
    # 経路復元
    pairs, unpaired_ids, leftover_faces = [], [], []
    i, j = n, mm
    while i > 0 or j > 0:
        cur = dp[i][j]
        if i > 0 and j > 0 and abs(dp[i - 1][j - 1] + abs(ds[i - 1]["mtime"] - fs[j - 1]["mtime"]) - cur) < 1e-9:
            pairs.append((fs[j - 1], ds[i - 1])); i -= 1; j -= 1
        elif i > 0 and abs(dp[i - 1][j] + skip - cur) < 1e-9:
            unpaired_ids.append(ds[i - 1]); i -= 1
        else:
            leftover_faces.append(fs[j - 1]); j -= 1
    pairs.reverse(); unpaired_ids.reverse(); leftover_faces.reverse()
    return pairs, leftover_faces, unpaired_ids


# ==========================================================================
# 一括処理（モード分岐）
# ==========================================================================
ID_MAP_FILENAME = "ids.csv"
ID_EDITOR_MAX_ROWS = 400        # 一覧に並べる上限（多すぎるとウィンドウ生成が重い）


def id_map_path(in_dir):
    return Path(in_dir) / ID_MAP_FILENAME


def read_id_map(in_dir):
    """入力フォルダの ids.csv を {ファイル名: ID} で返す。無ければ空dict。
    「ファイル名, ID」の2列。1行目がヘッダなら読み飛ばす。
    Excelで開いて編集されることを想定して BOM 付きUTF-8も受ける。"""
    path = id_map_path(in_dir)
    if not path.exists():
        return {}
    mapping = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            for i, row in enumerate(csv.reader(fp)):
                if len(row) < 2:
                    continue
                name, uid = row[0].strip(), row[1].strip()
                if not name or not uid:
                    continue
                if i == 0 and not uid.isdigit():
                    continue                      # ヘッダ行
                mapping[name] = uid
    except Exception:  # noqa: BLE001
        return {}
    return mapping


def write_id_map(in_dir, mapping):
    """{ファイル名: ID} を ids.csv に保存する。IDが空の行は書かない。"""
    path = id_map_path(in_dir)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["filename", "id"])
        for name, uid in mapping.items():
            uid = str(uid).strip()
            if uid:
                w.writerow([name, uid])
    return path


def out_filename(uid, opts):
    """出力PNGのファイル名。newgen 指定なら "r-" を付ける。
    config.xml の from= はファイル名と一致していなければならないため、
    プレフィックスはファイル名と from= の両方に付ける（to= のパスは数値IDのまま）。"""
    prefix = "r-" if opts.get("newgen", False) else ""
    return f"{prefix}{uid}.png"


def _portrait_score(path):
    """顔写真らしさのスコア。縦長〜正方形を優先し、同程度なら面積が大きい方を選ぶ。
    FMのIDスクショは横長かつ大面積なので、面積だけで選ぶとスクショが勝ってしまう。"""
    from PIL import Image
    try:
        w, h = Image.open(path).size
    except Exception:  # noqa: BLE001
        return (0, 0)
    aspect = (w / h) if h else 99.0
    return (0 if aspect >= OCR_MIN_ASPECT else 1, w * h)


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



# config.xml のレコード解析
RECORD_RE = re.compile(r'<record\s+[^>]*?/>')
RECORD_FROM_RE = re.compile(r'from=["\']([^"\']*)["\']')
RECORD_TO_RE = re.compile(r'to=["\'][^"\']*?/person/([^/"\']+)/portrait["\']')


def _strip_id_prefix(v):
    for pre in ("face_", "r-"):
        if v.startswith(pre):
            return v[len(pre):]
    return v


def _record_key(rec):
    """レコードが指している person ID を返す。
    重複は名前ではなく指し先で判定する。既製パックの from="face_12345" と
    自前の from="12345" は名前が違うだけで同じ選手を指しているため。"""
    mt = RECORD_TO_RE.search(rec)
    if mt:
        return _strip_id_prefix(mt.group(1))
    mf = RECORD_FROM_RE.search(rec)
    return _strip_id_prefix(mf.group(1)) if mf else None


def _read_config_text(cfg_path):
    """config.xml をエンコーディングを推定して読む。
    errors="replace" で読むと UTF-16 / Shift-JIS の既存ファイルが文字化けし、
    そのまま UTF-8 で書き戻して内容を破壊してしまうため。"""
    raw = cfg_path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError(t("config.xml の文字コードを判別できません（UTF-8 で保存し直してください）",
                       "Cannot determine the encoding of config.xml (please re-save it as UTF-8)"))


def _timestamped_backup(path):
    """既存ファイルを上書き前に退避し、作成したバックアップのPathを返す。"""
    path = Path(path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = path.with_name(f"{path.name}.{stamp}.bak")
    serial = 2
    while bak.exists():
        bak = path.with_name(f"{path.name}.{stamp}_{serial}.bak")
        serial += 1
    shutil.copy2(path, bak)
    return bak


def _atomic_write_text(path, content, encoding="utf-8"):
    """同じフォルダの一時ファイルへ書いてから置換し、途中終了での破損を防ぐ。"""
    path = Path(path)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        with tmp.open("w", encoding=encoding, newline="") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def _legacy_removebg_secret_items(settings):
    """旧版の設定にあるremove.bg認証項目だけを、値を表示せず抽出する。"""
    if not isinstance(settings, dict):
        return {}
    kept = {}
    for key, value in settings.items():
        normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
        if normalized.startswith("removebg") and (
                "key" in normalized or "token" in normalized):
            kept[key] = value
    return kept


def _atomic_save_png(img, path):
    """PNGを一時ファイルへ完成させてから置換する。"""
    path = Path(path)
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        img.save(tmp, "PNG")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


def write_config(uids, cfg_path, append=True, newgen=False):
    """config.xml に uid のレコードを書き込む（追記 or 上書き）。
    実際のFMリソースマネージャが認識する fmXML 互換形式
    （<record><list id="maps">...</list></record>、
    to="graphics/pictures/person/{id}/portrait"）で書き出す。
    戻り値: (追加件数, 合計件数, バックアップPathまたはNone)"""
    prefix = "r-" if newgen else ""
    existing = []
    text = ""
    if append and cfg_path.exists():
        text = _read_config_text(cfg_path)
        # from= の値は "12345" とは限らない（既製パックは "face_12345"、newgenは "r-12345"）。
        # 数字だけを要求すると重複を検出できず、同じIDが二重に入る。
        for rec in RECORD_RE.findall(text):
            k = _record_key(rec)
            if k is not None:
                existing.append(k)
    existing_set = set(existing)
    new_uids = []
    for u in uids:
        if str(u) in existing_set:
            continue
        new_uids.append(u)
    lines_to_add = [
        "\t\t<record from=%s to=%s/>\n" % (
            quoteattr(f"{prefix}{u}"),
            quoteattr(f"graphics/pictures/person/{u}/portrait"))
        for u in new_uids
    ]
    # 追記できるのは <list id="maps"> を持つ正しい形の既存ファイルだけ。
    # 空ファイルや壊れたファイルに追記すると、ルート要素の無い不正なXMLが出来上がり、
    # FMがパック全体を読まなくなる（しかも以降ずっとそこに追記し続ける）。
    close = -1
    if append and cfg_path.exists() and text.strip():
        close = text.rfind("</list>")
        if close == -1:
            close = text.rfind("</graphics>")  # 旧形式の config.xml からの移行用フォールバック
    backup = None
    if close != -1:
        new_text = text[:close] + "".join(lines_to_add) + text[close:]
        if new_text != text:
            _atomic_write_text(cfg_path, new_text)
    else:
        # 明示的な上書き、または壊れた既存ファイルの再生成では、元を必ず退避する。
        if cfg_path.exists() and cfg_path.stat().st_size > 0:
            backup = _timestamped_backup(cfg_path)
        existing = []
        header = (
            '<record>\n'
            '\t<!-- resource manager options -->\n\n'
            '\t<!-- dont preload anything in this folder -->\n'
            '\t<boolean id="preload" value="false"/>\n\n'
            '\t<!-- turn off auto mapping -->\n'
            '\t<boolean id="amap" value="false"/>\n\n'
            '\t<!-- logo mappings -->\n'
            '\t<!-- the following XML maps pictures inside this folder into other positions\n'
            '\t\t\t in the resource system, which allows this folder to be dropped into any\n'
            '\t\t\t place in the graphics folder and still have the game pick up the graphics\n'
            '\t\t\t files from the correct places\n'
            '\t-->\n\n'
            '\t<list id="maps">\n'
            '\t\t<!-- Auto generated by fmXML -->\n'
        )
        footer = '\t</list>\n</record>\n'
        _atomic_write_text(cfg_path, header + "".join(lines_to_add) + footer)
    total = len(existing) + len(new_uids) if append else len(new_uids)
    return len(new_uids), total, backup


def dedupe_config(cfg_path):
    """config.xml の重複 from 属性を除去して上書き保存。
    タイムスタンプ付きバックアップを作成する。
    戻り値: (出力パス, バックアップPath, 除去件数, 残り件数)"""
    text = _read_config_text(cfg_path)
    seen = set()
    removed = [0]

    # 行単位で消すと「1行に複数レコード」のファイルで重複していないレコードまで巻き添えになる。
    # <record .../> 単位で判定する。
    def _sub(m):
        key = _record_key(m.group(0))
        if key is None:
            return m.group(0)
        if key in seen:
            removed[0] += 1
            return ""
        seen.add(key)
        return m.group(0)

    out = RECORD_RE.sub(_sub, text)
    if removed[0] == 0:
        return cfg_path, None, 0, len(seen)
    # 既存の .bak を上書きすると、2回目の実行で元の状態が失われる
    bak = _timestamped_backup(cfg_path)
    _atomic_write_text(cfg_path, out)
    return cfg_path, bak, removed[0], len(seen)


def process_one(path, out_path, opts, log):
    """1枚の顔写真を実際に加工する処理本体。
    顔検出 -> 背景除去 -> 顔トリミング -> アップスケール -> 指定サイズへリサイズ -> PNG保存。

    背景除去をトリミングより前に置いているのは、トリミングの水平中心を
    「頭のシルエット中心」で決めるため。目の中点だけを基準にすると、
    振り向いた顔で頭が片側に寄って見切れる。
    顔検出だけは背景除去『前』の元画像に対して行う（透明部が黒く写ると検出精度が落ちるため）。
    背景除去は画像サイズを変えないので、検出したボックス座標はそのまま使える。

    保存できたら True、プレビューでスキップ/キャンセルされたり失敗した場合は False を返す。"""
    from PIL import Image, ImageOps
    # スマホ/一眼のJPEGはEXIFに回転情報を持つ。適用しないと横倒しのまま処理され、
    # 顔検出も失敗して写真全体が90度傾いたまま出力される。
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    debug = opts.get("debug")
    # 判定はクロップ『前』に行う。crop_around_face は画像外にはみ出した分を
    # 透明でパディングするため、クロップ後だと普通の写真も透過済みに見えてしまう。
    already_cutout = is_already_cutout(img)

    # --- 1. 顔検出（元画像に対して。背景除去してからでは精度が落ちる）---
    det = detect_face_box(img, log) if opts.get("face_crop", True) else None
    if det is not None:
        box, eye_mid_y, eye_mid_x, nose_x = det
        if debug:
            log(t(f"  [debug] 顔box={box} eye_y={eye_mid_y} eye_x={eye_mid_x} nose_x={nose_x}",
                  f"  [debug] face box={box} eye_y={eye_mid_y} eye_x={eye_mid_x} nose_x={nose_x}"))
    elif debug and opts.get("face_crop", True):
        log(t("  [debug] 顔を検出できませんでした（全体を使用）",
              "  [debug] face not detected (using full image)"))

    # --- 2. 背景除去（サイズは変わらない）---
    if opts.get("bg_removal", True):
        if already_cutout:
            # 既に背景が抜かれている画像を再処理すると、透明部＝黒として扱われ
            # 黒髪などが背景と誤認されて欠ける。そのまま通す。
            log(t("  [i] 入力が既に背景除去済みのため、背景除去をスキップします",
                  "  [i] Input is already cut out; skipping background removal."))
        else:
            img = remove_background(img, opts.get("rembg_model", "isnet-general-use"),
                                    alpha_matting=opts.get("alpha_matting", False), log=log,
                                    removebg_key=opts.get("removebg_key"))

    # --- 3. 顔トリミング（ここで透過が使えるのでシルエット基準の水平補正が効く）---
    if det is not None:
        img = crop_around_face(img, box, opts.get("face_size", 1.37), opts.get("face_neck", 0.10),
                               log=log, eye_mid_y=eye_mid_y, eye_mid_x=eye_mid_x, nose_x=nose_x)

    # --- 4. アップスケール（切り抜き後なので小さく済む）---
    if opts.get("upscale", True):
        img = upscale(img, float(opts.get("scale", 1.0)),
                      opts.get("esrgan_model", "RealESRGAN_x4plus"),
                      opts.get("ai_upscale", False), log)

    img = fit_to_size(img, int(opts.get("size", 180)), opts.get("fit", "contain"))

    preview = opts.get("_preview")
    if preview is not None:
        decision = preview(img)
        if decision == "cancel":
            cancel = opts.get("_cancel")
            if cancel is not None:
                cancel.set()
            return False
        if decision == "skip":
            return False

    _atomic_save_png(img, out_path)
    return True


def process_folder(opts, log, progress=None):
    in_dir = Path(opts["input"]); out_dir = Path(opts["output"])
    if not in_dir.is_dir():
        log(t(f"[!] 入力フォルダがありません: {in_dir}", f"[!] Input folder not found: {in_dir}")); return
    try:
        if in_dir.resolve() == out_dir.resolve():
            log(t("[!] 入力フォルダと出力フォルダは別にしてください。元画像の上書きを防ぐため処理を中止しました。",
                  "[!] Input and output folders must differ. Processing was stopped to protect source images."))
            return
    except Exception:  # noqa: BLE001
        pass
    out_dir.mkdir(parents=True, exist_ok=True)

    # 出力先が入力フォルダの中にあると、自分が出したPNGを「ペアフォルダ」として
    # 読み込んでしまい、2回目以降の実行で別人の顔が上書きされる
    try:
        out_res = out_dir.resolve()
    except Exception:  # noqa: BLE001
        out_res = out_dir
    subdirs = []
    for d in sorted(in_dir.iterdir()):
        if not d.is_dir():
            continue
        try:
            if d.resolve() == out_res:
                continue
        except Exception:  # noqa: BLE001
            pass
        subdirs.append(d)

    files = sorted(f for f in in_dir.iterdir() if f.suffix.lower() in SUPPORTED_EXT)

    # 対応表（ids.csv）があれば最優先。ペアフォルダもIDスクショもOCRも不要。
    id_map = read_id_map(in_dir)
    if id_map:
        targets = [f for f in files if f.name in id_map]
        missing = [f for f in files if f.name not in id_map]
        log(t(f"対応表モード（{ID_MAP_FILENAME}）: {len(targets)} 件",
              f"ID map mode ({ID_MAP_FILENAME}): {len(targets)} file(s)"))
        if missing:
            log(t(f"  [!] 対応表にIDが無い画像 {len(missing)} 枚はスキップします: ",
                  f"  [!] {len(missing)} image(s) missing from the ID map are skipped: ")
                + ", ".join(f.name for f in missing[:10]) + (" …" if len(missing) > 10 else ""))
        if not targets:
            log(t(f"[!] {ID_MAP_FILENAME} に載っている画像が1枚もありません。",
                  f"[!] None of the images are listed in {ID_MAP_FILENAME}.")); return
        _run_idmap_mode(targets, id_map, out_dir, opts, log, progress)
        return

    if subdirs:
        log(t(f"サブフォルダ（ペア）モード: {len(subdirs)} フォルダ", f"Subfolder (pair) mode: {len(subdirs)} folders"))
        if files:
            # 以前はここが無言だったので、紛れ込んだフォルダ1つで全画像が無視されても気づけなかった
            log(t(f"  [!] 直下の画像 {len(files)} 枚はサブフォルダモードでは無視されます: ",
                  f"  [!] {len(files)} loose image(s) are ignored in subfolder mode: ")
                + ", ".join(f.name for f in files[:10]) + (" …" if len(files) > 10 else ""))
        _run_subfolder_mode(subdirs, out_dir, opts, log, progress)
        return

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
                if kind == "skip":
                    continue
                if kind == "id":
                    # 2枚目以降のIDスクショも顔候補には入れない
                    # （FMのスクショは顔写真より大きいので max(_img_area) に勝ってしまう）
                    if num and id_num is None:
                        id_num = num
                    continue
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
        # 「一番大きい画像＝顔」はFMのスクショ(1920x1080)が顔写真より大きいので危険。
        # ポートレートらしさ（縦長ほど高スコア）を主、面積を従にして選ぶ。
        face = max(faces, key=_portrait_score)
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
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {subname}/{face.name} -> {out_name}")
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


def _run_idmap_mode(files, id_map, out_dir, opts, log, progress):
    """ids.csv の「ファイル名 -> ID」に従って処理する。ペアリングもOCRも行わない。"""
    _run_uid_list(files, [id_map[f.name] for f in files], out_dir, opts, log, progress)


def _run_filename_mode(files, out_dir, opts, log, progress):
    """ファイル名（拡張子を除く）をそのままIDとして使う。"""
    _run_uid_list(files, [f.stem for f in files], out_dir, opts, log, progress,
                  require_digits=True)


def _run_uid_list(files, uid_list, out_dir, opts, log, progress, require_digits=True):
    """「画像 -> UID」が確定しているリストをまとめて処理する共通ルーチン。
    ファイル名モードと対応表モードで中身が同じだったため1本にまとめてある。"""
    cancel = opts.get("_cancel")
    workers = max(1, int(opts.get("workers", 1)))
    items = list(zip(files, uid_list))
    total = len(items)
    uids, uids_lock = [], threading.Lock()
    seen, seen_lock = set(), threading.Lock()
    done, done_lock = [0], threading.Lock()

    def _process(idx_item):
        idx, (f, uid) = idx_item
        if cancel and cancel.is_set():
            return
        uid = str(uid).strip()
        if require_digits and not uid.isdigit():
            # 数字以外は config.xml に書いてもFMが参照しないパスになる
            log(t(f"[skip] IDが数字ではありません（FMのperson IDとして使えません）: {f.name} -> {uid!r}",
                  f"[skip] ID is not numeric (not a valid FM person ID): {f.name} -> {uid!r}"))
            return
        with seen_lock:
            if uid in seen:
                log(t(f"[skip] ID重複: {uid}（{f.name}）", f"[skip] duplicate ID: {uid} ({f.name})"))
                return
            seen.add(uid)
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {f.name} -> {out_name}")
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
            list(exe.map(_process, enumerate(items, 1)))
    else:
        for item in enumerate(items, 1):
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
        if kind == "skip":
            continue
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

    pairs, leftover, unpaired_ids = pair_by_time(faces, ids)
    log(t(f"\nペアリング結果: {len(pairs)} 組", f"\nPairing result: {len(pairs)} pair(s)"))
    for face, idd in pairs:
        log(t(f"  顔: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png", f"  face: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png"))
    if leftover:
        log(t(f"  [!] 相方の見つからない顔 {len(leftover)} 枚: ",
              f"  [!] {len(leftover)} face(s) with no matching ID: ")
            + ", ".join(x["path"].name for x in leftover))
    if unpaired_ids:
        # 以前はここが黙って捨てられていて、出力されない選手に気づけなかった
        log(t(f"  [!] 相方の見つからないID {len(unpaired_ids)} 件（撮影時刻が"
              f"{int(MAX_PAIR_GAP_SEC)}秒以上離れています）: ",
              f"  [!] {len(unpaired_ids)} ID(s) with no matching face "
              f"(more than {int(MAX_PAIR_GAP_SEC)}s apart): ")
            + ", ".join(f"{x['number']}({x['path'].name})" for x in unpaired_ids))
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
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}"))
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {face['path'].name} -> {out_name}")
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
        added, total, backup = write_config(uids, cfg, opts.get("config_append", True),
                                            opts.get("newgen", False))
        tag = t("（newgen: r- 付き）", " (newgen: r- prefix)") if opts.get("newgen", False) else ""
        if opts.get("config_append", True):
            log(t(f"config.xml を更新{tag}: 新規 {added} 件 / 合計 {total} 行 -> {cfg}",
                  f"Updated config.xml{tag}: {added} new / {total} total lines -> {cfg}"))
        else:
            log(t(f"config.xml を生成{tag}: {total} 行 -> {cfg}",
                  f"Generated config.xml{tag}: {total} lines -> {cfg}"))
        if backup is not None:
            log(t(f"既存の config.xml をバックアップしました: {backup.name}",
                  f"Backed up the previous config.xml: {backup.name}"))
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
# 顔の大きさ
# キャンバス高さ = 顔高さ(目〜顎から正規化) × この値。小さいほど顔が大きく写る。
# 既定値は既存FMフェイスパック（180px版24枚）の実測中央値。実測レンジは 1.18〜1.57。
# 髪の量が多くて枠に収まらない場合は crop_around_face が自動で広げる。
DEFAULT_FACE_SIZE = 1.37
FACE_SIZE_MIN, FACE_SIZE_MAX = 0.8, 3.0
FACE_NECK = 0.10               # 参考値（現バージョンでは直接使用しない）
# 旧バージョンの設定ファイル（プリセット番号）からの移行用
LEGACY_ZOOM_TO_SIZE = {0: 1.37, 1: 1.25}


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
        self.last_out = None
        self.log_lines = []          # ログ本文を保持（言語切替時に再描画はしないが保持用）
        self.log_has_content = False # 実処理のログが出たらTrue（案内文だけならFalse）
        # tkdnd は TkinterDnD.Tk() を使わなくても、既存のTkルートに後から載せられる。
        # （クラス構成を変えずに済むので、未導入環境との差分が小さい）
        self.dnd_ok = False
        self.dnd_version = None
        if HAS_DND:
            try:
                self.dnd_version = TkinterDnD._require(self)
                self.dnd_ok = True
            except Exception:  # noqa: BLE001
                self.dnd_ok = False
        self.style = ttk.Style()
        self._init_vars()
        self._preload_settings()     # 言語・テーマ・各値をウィジェット生成前に読み込む
        set_lang(self.lang)
        self.outer = None
        self._build()
        self.apply_theme(self.mode)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(100, self._poll)

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
        self.face_size_var = tk.DoubleVar(value=DEFAULT_FACE_SIZE)

    # ---------- 設定の保存・復元 ----------
    def _get_num(self, var, default):
        """IntVar/DoubleVar は入力欄が空だと TclError を投げる。
        1つ壊れただけで設定ファイル全体が保存されなくなるのを防ぐ。"""
        try:
            return var.get()
        except Exception:  # noqa: BLE001
            return default

    def _collect_settings(self):
        settings = {
            "mode": self.mode, "lang": self.lang,
            "face_size": self._get_num(self.face_size_var, DEFAULT_FACE_SIZE),
            "input": self.in_var.get(), "output": self.out_var.get(),
            "size": self._get_num(self.size_var, 180),
            "scale": self._get_num(self.scale_var, 1.0),
            "fit": self.fit_var.get(), "model": self.model_var.get(),
            "matting": self.matting_var.get(), "ocr": self.ocr_var.get(),
            "facecrop": self.facecrop_var.get(), "upscale": self.upscale_var.get(),
            "ai": self.ai_var.get(), "bg": self.bg_var.get(), "cfg": self.cfg_var.get(),
            "append": self.append_var.get(), "newgen": self.newgen_var.get(),
            "preview": self.preview_var.get(),
            "lowpower": self.lowpower_var.get(),
            "workers": self._get_num(self.workers_var, 1),
            "save_log": self.save_log_var.get(),
            "use_removebg": self.use_removebg_var.get(),
            "overwrite": self.ow_var.get(),
            "debug": self.debug_var.get(),
        }
        # APIキーはこのPC内の設定ファイルだけに保存する。
        # 空欄で保存した場合は、旧版が保存したキーを勝手に削除しない。
        removebg_key = self.removebg_var.get().strip()
        if removebg_key:
            settings["removebg_key"] = removebg_key
        return settings

    def _preload_settings(self):
        try:
            if not self.SETTINGS_PATH.exists():
                return
            d = json.loads(self.SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        # 設定ファイルは手で編集されたり、書き込み中のクラッシュで壊れたりする。
        # 値をそのまま信用すると、ウィンドウが出る前に落ちて起動不能になる。
        if not isinstance(d, dict):
            return
        # 旧版を含むremove.bg認証項目は本人のPC内だけで再利用する。
        # 値は画面上では伏せ字のまま表示し、配布物には含めない。
        for value in _legacy_removebg_secret_items(d).values():
            if isinstance(value, str) and value.strip():
                self.removebg_var.set(value.strip())
                break
        if d.get("mode") in ("dark", "light"):
            self.mode = d["mode"]
        if d.get("lang") in ("ja", "en"):
            self.lang = d["lang"]
        # 旧形式（プリセット番号）からの移行
        if "face_size" not in d and "zoom_idx" in d:
            try:
                d["face_size"] = LEGACY_ZOOM_TO_SIZE.get(int(d["zoom_idx"]), DEFAULT_FACE_SIZE)
            except (TypeError, ValueError):
                pass

        for key, var in (
            ("input",       self.in_var),
            ("output",      self.out_var),
            ("fit",         self.fit_var),
            ("model",       self.model_var),
            ("size",        self.size_var),
            ("face_size",   self.face_size_var),
            ("scale",       self.scale_var),
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
            # APIキーは本人のPC内だけに保存する。旧版が既に保存したキーも、
            # 設定保存の副作用で勝手に削除しない。配布物には設定ファイルを含めない。
            previous = {}
            if self.SETTINGS_PATH.exists():
                try:
                    previous = json.loads(
                        self.SETTINGS_PATH.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    previous = {}
            settings = self._collect_settings()
            for key, value in _legacy_removebg_secret_items(previous).items():
                settings.setdefault(key, value)
            _atomic_write_text(
                self.SETTINGS_PATH,
                json.dumps(settings, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _on_removebg_toggle(self):
        """remove.bg チェックボックスのトグル：キー入力欄を表示/非表示"""
        if self.use_removebg_var.get():
            # before を指定しないと pack 順の末尾（チェックボックス群の下）に移動してしまう
            ref = getattr(self, "_chk_frame", None)
            if ref is not None and ref.winfo_exists():
                self._apikey_frame.pack(fill="x", pady=(0, 4), before=ref)
            else:
                self._apikey_frame.pack(fill="x", pady=(0, 4))
        else:
            self._apikey_frame.pack_forget()

    def _on_close(self):
        # 実行中に閉じると、daemonスレッドが img.save や config.xml の書き換えの
        # 途中で殺され、壊れたPNGや壊れたconfig.xmlが残る。
        th = getattr(self, "_worker", None)
        if th is not None and th.is_alive():
            if not messagebox.askyesno(
                    t("確認", "Confirm"),
                    t("処理中です。中断して終了しますか？（書き込み中のファイルを待ちます）",
                      "A job is running. Cancel it and quit? (will wait for the file being written)")):
                return
            ev = getattr(self, "_cancel_event", None)
            if ev is not None:
                ev.set()
            th.join(timeout=10.0)
        self._save_settings()
        # 予約済みの _poll をキャンセルしてから破棄する
        self._closing = True
        pid = getattr(self, "_poll_id", None)
        if pid is not None:
            try:
                self.after_cancel(pid)
            except Exception:  # noqa: BLE001
                pass
        self.destroy()

    def _build(self):
        if getattr(self, "outer", None) is not None:
            self.outer.destroy()
        self.outer = outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        # ── ヘッダー ────────────────────────────────────────────
        header = ttk.Frame(outer); header.pack(fill="x", pady=(0, 6))
        header.columnconfigure(1, weight=1)
        titlerow = ttk.Frame(header); titlerow.grid(row=0, column=0, sticky="w")
        lbl = ttk.Label(titlerow, text="FM Face Processor", style="Header.TLabel")
        lbl.pack(side="left")
        lbl2 = ttk.Label(titlerow, text=f"  {APP_VERSION}", style="Ver.TLabel")
        lbl2.pack(side="left")
        sub = ttk.Label(header,
                        text=t("顔画像＋IDスクショ → 透過PNG ＋ config.xml",
                               "Face + ID screenshot -> transparent PNG + config.xml"),
                        style="Sub.TLabel")
        sub.grid(row=0, column=1, sticky="w", padx=(10, 6))

        btns = ttk.Frame(header); btns.grid(row=0, column=2, sticky="e")
        self.lang_btn = ttk.Button(btns,
                                   text="EN" if self.lang == "ja" else "日本語",
                                   command=self._toggle_lang,
                                   # Tk の文字幅は英数字基準。日本語3文字は width=4 だと
                                   # 高DPI環境で右端が欠けるため、十分な余白を確保する。
                                   width=7)
        self.lang_btn.pack(side="right", padx=(4, 0))
        self.theme_btn = ttk.Button(btns, text="", command=self._toggle_theme, width=3)
        self.theme_btn.pack(side="right")

        # ── フォルダ選択 ────────────────────────────────────────
        fld = ttk.LabelFrame(outer, text=t("フォルダ", "Folders"), padding=8)
        fld.pack(fill="x", pady=(0, 6))
        fld.columnconfigure(1, weight=1)
        ttk.Label(fld, text=t("入力", "Input")).grid(row=0, column=0, sticky="w", pady=2)
        in_ent = ttk.Entry(fld, textvariable=self.in_var)
        in_ent.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(fld, text="…", command=self._pick_in, width=3).grid(row=0, column=2)
        ttk.Label(fld, text=t("出力", "Output")).grid(row=1, column=0, sticky="w", pady=2)
        out_ent = ttk.Entry(fld, textvariable=self.out_var)
        out_ent.grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(fld, text="…", command=self._pick_out, width=3).grid(row=1, column=2)
        # フォルダや画像をここに放り込めるようにする
        self._register_drop(in_ent, self._on_drop_input)
        self._register_drop(out_ent, self._on_drop_output)
        self._register_drop(fld, self._on_drop_input)

        if self.dnd_ok:
            ttk.Label(fld, style="Sub.TLabel",
                      text=t("フォルダや画像をこの枠にドロップしても設定できます",
                             "You can also drop a folder or images onto this box")
                      ).grid(row=3, column=1, sticky="w", padx=6, pady=(4, 0))

        prow = ttk.Frame(fld); prow.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        # 実行中に押されると処理中のファイルを壊すので、まとめて無効化できるようにする
        self._busy_btns = []
        b = ttk.Button(prow, text=t("一覧でIDを割り当てる", "Assign IDs from a list"),
                       command=self._open_id_editor)
        b.pack(side="left"); self._busy_btns.append(b)
        b = ttk.Button(prow, text=t("ペアフォルダを作る", "Make pair folders"),
                       command=self._make_pair_folders)
        b.pack(side="left", padx=(8, 0)); self._busy_btns.append(b)
        b = ttk.Button(prow, text=t("入力元画像をゴミ箱へ", "Trash source images"),
                       command=self._delete_source_images)
        b.pack(side="left", padx=(8, 0)); self._busy_btns.append(b)
        b = ttk.Button(prow, text=t("config.xml の重複を除去", "Clean duplicate IDs in config.xml"),
                       command=self._dedupe_config_dialog)
        b.pack(side="left", padx=(8, 0)); self._busy_btns.append(b)

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
        ttk.Label(row, text=t("顔サイズ", "Face size")).pack(side="left")
        ttk.Entry(row, textvariable=self.face_size_var, width=5).pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(opt); row2.pack(fill="x", pady=(0, 4))
        ttk.Label(row2, text=t("背景モデル", "BG model")).pack(side="left")
        ttk.Combobox(row2, textvariable=self.model_var, width=20, state="readonly",
                     values=["isnet-general-use", "birefnet-general", "birefnet-general-lite",
                             "birefnet-portrait", "u2net_human_seg", "u2net"]).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(row2, text=t("髪のフチをなめらかにする", "Smooth hair edges (alpha matting)"),
                        variable=self.matting_var).pack(side="left")
        row2b = ttk.Frame(opt); row2b.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(row2b, text=t("remove.bg API を使う（画像を外部送信）",
                                      "Use remove.bg API (uploads the image)"),
                        variable=self.use_removebg_var,
                        command=self._on_removebg_toggle).pack(side="left")

        self._apikey_frame = rowk = ttk.Frame(opt)
        ttk.Label(rowk, text=t("remove.bg APIキー（このPC内だけに保存）",
                               "remove.bg API key (saved only on this PC)")).pack(side="left")
        self._apikey_entry = ttk.Entry(rowk, textvariable=self.removebg_var, width=30, show="●")
        self._apikey_entry.pack(side="left", padx=(6, 0))
        self._apikey_show = tk.BooleanVar(value=False)
        def _toggle_key():
            self._apikey_entry.config(show="" if self._apikey_show.get() else "●")
        ttk.Checkbutton(rowk, text=t("表示", "Show"), variable=self._apikey_show,
                        command=_toggle_key).pack(side="left", padx=(6, 0))
        if self.use_removebg_var.get():
            rowk.pack(fill="x", pady=(0, 4))

        chk = self._chk_frame = ttk.Frame(opt); chk.pack(fill="both", expand=True)
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
        _dl = ttk.Button(act, text=t("ログをすべて削除", "Delete all logs"),
                         command=self._delete_logs)
        _dl.pack(side="left", padx=8)
        self._busy_btns.append(_dl)

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
            # 初期案内は処理ログとして保持しない。保持すると言語切替後も
            # 切替前の言語の案内が復元されてしまう。
            hint = t("顔写真とIDスクショを同じフォルダに入れて「実行」を押してください。",
                     "Put face photos and ID screenshots in one folder, then press Run.")
            self.log.insert("end", hint + "\n")
            self.log_has_content = False

        self._refresh_theme_btn()
        self.apply_theme(self.mode)
        self._apply_busy_state()

    def _apply_busy_state(self):
        """実行中かどうかをボタンの有効/無効に反映する。
        _build() はウィジェットを作り直すので、言語切替の直後にも必ず呼ぶこと。
        （以前は再構築で「実行」が有効に戻り、同じ出力フォルダに2本目のスレッドを
          起動できてしまい、PNGとconfig.xmlが壊れる上に1本目が停止不能になった）"""
        running = bool(getattr(self, "_running", False))
        try:
            if running:
                self.run_btn.config(state="disabled", text=t("処理中…", "Working…"))
                self.cancel_btn.config(state="normal")
                self.open_btn.config(state="disabled")
            else:
                self.run_btn.config(state="normal", text=t("実行", "Run"))
                self.cancel_btn.config(state="disabled")
                if getattr(self, "last_out", None):
                    self.open_btn.config(state="normal")
            for b in getattr(self, "_busy_btns", []):
                b.config(state="disabled" if running else "normal")
        except Exception:  # noqa: BLE001
            pass

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
        # _poll は自分で再スケジュールし続けるので、ここで呼ぶとチェーンが二重になる
        # （切り替えるたびにポーリングループが1本ずつ増え続けていた）


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
        if not Path(base).is_dir():
            # 入力欄は自由入力で、設定ファイルから復元した古いパスのこともある。
            # 存在確認をしないと FileNotFoundError がコールバックから素通りして
            # 「押しても何も起きない」状態になる。
            messagebox.showwarning(t("確認", "Notice"),
                                   t(f"入力フォルダが見つかりません:\n{base}",
                                     f"Input folder not found:\n{base}"))
            return
        n = simpledialog.askinteger(t("ペアフォルダを作る", "Make pair folders"),
                                    t("作るフォルダの数:", "Number of folders to create:"),
                                    minvalue=1, maxvalue=200)
        if not n:
            return
        made = 0
        try:
            for i in range(1, n + 1):
                p = Path(base) / f"pair_{i:03d}"
                p.mkdir(exist_ok=True)
                made += 1
        except OSError as e:
            messagebox.showerror(t("エラー", "Error"),
                                 t(f"フォルダを作成できませんでした（{made} 個目まで作成済み）:\n{e}",
                                   f"Could not create folders (created {made} so far):\n{e}"))
            return
        messagebox.showinfo(t("完了", "Done"),
                            t(f"{made} 個のフォルダを作成しました。\n各フォルダに「顔写真」と「IDスクショ」を1枚ずつ入れてから実行してください。",
                              f"Created {made} folders.\nPut one face photo and one ID screenshot in each, then run."))

    # ---------- ドラッグ＆ドロップ ----------
    def _dnd_paths(self, data):
        """ドロップされた文字列をパスの配列にする。
        Tclのリスト形式（空白を含むパスは {..} で括られる）なので splitlist で分解する。
        「C:/Users/owner/Pictures/face processor」のような空白入りパスがそのまま扱える。"""
        try:
            raw = self.tk.splitlist(data)
        except Exception:  # noqa: BLE001
            raw = [data]
        out = []
        for r in raw:
            r = str(r).strip().strip("{}")
            if r:
                out.append(Path(r))
        return out

    def _register_drop(self, widget, handler):
        """ウィジェットをドロップ先にする。tkinterdnd2 が無ければ何もしない。"""
        if not self.dnd_ok:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)
        except Exception:  # noqa: BLE001
            pass

    def _split_dropped(self, event):
        """ドロップされたものを (フォルダ, 対応画像, その他) に分ける。"""
        dirs, imgs, other = [], [], []
        for p in self._dnd_paths(event.data):
            try:
                if p.is_dir():
                    dirs.append(p)
                elif p.is_file() and p.suffix.lower() in SUPPORTED_EXT:
                    imgs.append(p)
                else:
                    other.append(p)
            except OSError:
                other.append(p)
        return dirs, imgs, other

    def _on_drop_input(self, event):
        """フォルダなら入力に設定。画像ならその親フォルダを入力に設定。"""
        dirs, imgs, other = self._split_dropped(event)
        if dirs:
            self.in_var.set(str(dirs[0]))
            if len(dirs) > 1:
                self._log(t(f"[i] フォルダが複数ドロップされたので最初の1つを使います: {dirs[0].name}",
                            f"[i] Multiple folders dropped; using the first: {dirs[0].name}"))
            return
        if imgs:
            parents = {p.parent for p in imgs}
            if len(parents) > 1:
                messagebox.showwarning(
                    t("確認", "Notice"),
                    t("複数のフォルダにまたがる画像がドロップされました。\n"
                      "1つのフォルダにまとめてからドロップしてください。",
                      "The dropped images live in different folders.\n"
                      "Please put them in one folder first."))
                return
            self.in_var.set(str(next(iter(parents))))
            return
        if other:
            messagebox.showwarning(t("確認", "Notice"),
                                   t("対応していないファイルです。", "Unsupported file."))

    def _on_drop_output(self, event):
        dirs, imgs, _o = self._split_dropped(event)
        target = dirs[0] if dirs else (imgs[0].parent if imgs else None)
        if target is not None:
            self.out_var.set(str(target))

    @staticmethod
    def _incoming_images(in_dir, dirs, imgs):
        """ドロップされたフォルダ/画像から、入力フォルダの外にある対応画像を集める。"""
        found = list(imgs)
        for d in dirs:
            try:
                found += [q for q in sorted(d.iterdir())
                          if q.is_file() and q.suffix.lower() in SUPPORTED_EXT]
            except OSError:
                pass
        try:
            here = Path(in_dir).resolve()
        except OSError:
            here = Path(in_dir)
        out, seen = [], set()
        for q in found:
            try:
                if q.parent.resolve() == here:
                    continue                      # 既に入力フォルダにある
            except OSError:
                pass
            key = str(q)
            if key not in seen:
                seen.add(key); out.append(q)
        return out

    def _copy_into(self, in_dir, paths):
        """入力フォルダへコピーする。同名は上書きせず連番を付ける。"""
        added = 0
        for q in paths:
            dest = Path(in_dir) / q.name
            n = 1
            while dest.exists():
                dest = Path(in_dir) / f"{q.stem}_{n}{q.suffix}"
                n += 1
            try:
                shutil.copy2(q, dest); added += 1
            except Exception as e:  # noqa: BLE001
                self._log(t(f"  [!] コピー失敗: {q.name}: {e}",
                            f"  [!] Copy failed: {q.name}: {e}"))
        self._log(t(f"[i] {added} 枚を入力フォルダにコピーしました",
                    f"[i] Copied {added} image(s) into the input folder"))
        return added

    # ---------- 一覧モード（ID割り当て） ----------
    def _open_id_editor(self, preset=None):
        """入力フォルダの画像を一覧にして、行ごとにIDを打てるウィンドウを開く。
        ペアフォルダもIDスクショもOCRも使わずに「画像 -> ID」を決められる。
        入力内容は入力フォルダの ids.csv に保存され、実行時に自動で使われる。"""
        base = self.in_var.get()
        if not base or not Path(base).is_dir():
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダを先に選んでください。",
                                     "Please select the input folder first."))
            return
        in_dir = Path(base)
        files = sorted(f for f in in_dir.iterdir() if f.suffix.lower() in SUPPORTED_EXT)
        if not files:
            messagebox.showinfo(t("情報", "Info"),
                                t("入力フォルダに画像がありません。",
                                  "No images in the input folder."))
            return
        if len(files) > ID_EDITOR_MAX_ROWS:
            if not messagebox.askyesno(
                    t("確認", "Confirm"),
                    t(f"画像が {len(files)} 枚あります。先頭 {ID_EDITOR_MAX_ROWS} 枚だけ表示しますか？",
                      f"There are {len(files)} images. Show only the first {ID_EDITOR_MAX_ROWS}?")):
                return
            files = files[:ID_EDITOR_MAX_ROWS]

        existing = read_id_map(in_dir)
        if preset:
            existing.update(preset)      # ドロップで作り直したときに入力中の値を引き継ぐ
        win = tk.Toplevel(self)
        win.title(t("一覧でIDを割り当てる", "Assign IDs from a list"))
        win.geometry("640x680")
        set_titlebar_dark(win, self.mode == "dark")
        try:
            win.configure(bg=PALETTES[self.mode]["bg"])
        except Exception:  # noqa: BLE001
            pass

        top = ttk.Frame(win, padding=8); top.pack(fill="x")
        ttk.Button(top, text=t("IDをまとめて貼り付け", "Paste IDs in bulk"),
                   command=lambda: self._bulk_paste_ids(win)).pack(side="left")
        ttk.Button(top, text=t("空欄だけクリア", "Clear all"),
                   command=lambda: [v.set("") for _, v in self._id_rows]).pack(side="left", padx=8)
        count_lbl = ttk.Label(top, text="", style="Sub.TLabel"); count_lbl.pack(side="right")

        ttk.Label(win, padding=(8, 0),
                  text=t("FMの選手一覧からIDを縦に並べてコピーし、"
                         "「IDをまとめて貼り付け」で上から順に流し込めます。",
                         "Copy IDs as one per line from FM, then use "
                         "\"Paste IDs in bulk\" to fill the rows top-down."),
                  style="Sub.TLabel", wraplength=600).pack(fill="x")

        # スクロールできる行リスト
        wrap = ttk.Frame(win, padding=8); wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, highlightthickness=0, borderwidth=0)
        try:
            canvas.configure(bg=PALETTES[self.mode]["bg"])
        except Exception:  # noqa: BLE001
            pass
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        self._id_rows = []          # [(Path, StringVar)]
        self._id_thumbs = []        # PhotoImage の参照を保持しないとGCで消える
        thumb_ok = True
        for i, f in enumerate(files):
            row = ttk.Frame(inner); row.pack(fill="x", pady=1)
            if thumb_ok:
                try:
                    from PIL import Image as _I, ImageTk as _ITk
                    im = _I.open(f); im.thumbnail((44, 44), _I.LANCZOS)
                    ph = _ITk.PhotoImage(im.convert("RGBA"))
                    self._id_thumbs.append(ph)
                    ttk.Label(row, image=ph).pack(side="left", padx=(0, 6))
                except Exception:  # noqa: BLE001
                    thumb_ok = False       # 1枚失敗したら以降は諦める（Pillow/ImageTk無し等）
            ttk.Label(row, text=f.name, width=34, anchor="w").pack(side="left")
            var = tk.StringVar(value=existing.get(f.name, ""))
            ent = ttk.Entry(row, textvariable=var, width=14)
            ent.pack(side="left", padx=6)
            if i == 0:
                ent.focus_set()
            self._id_rows.append((f, var))

        def refresh_count(*_a):
            n = sum(1 for _, v in self._id_rows if v.get().strip())
            count_lbl.config(text=t(f"{len(self._id_rows)} 件中 {n} 件にID入力済み",
                                    f"{n} of {len(self._id_rows)} have an ID"))
        for _, v in self._id_rows:
            v.trace_add("write", refresh_count)
        refresh_count()

        def collect():
            return {f.name: v.get().strip() for f, v in self._id_rows if v.get().strip()}

        def save(and_run=False):
            mapping = collect()
            if not mapping:
                messagebox.showwarning(t("確認", "Notice"),
                                       t("IDが1件も入力されていません。", "No IDs entered."))
                return
            bad = [k for k, v in mapping.items() if not v.isdigit()]
            if bad and not messagebox.askyesno(
                    t("確認", "Confirm"),
                    t(f"数字でないIDが {len(bad)} 件あります（実行時にスキップされます）。続けますか？",
                      f"{len(bad)} ID(s) are not numeric (they will be skipped). Continue?")):
                return
            path = write_id_map(in_dir, mapping)
            self._log(t(f"[i] 対応表を保存しました: {path}", f"[i] Saved ID map: {path}"))
            win.destroy()
            if and_run:
                self._run()
            else:
                messagebox.showinfo(t("完了", "Done"),
                                    t(f"{len(mapping)} 件を {ID_MAP_FILENAME} に保存しました。\n"
                                      "「実行」を押すとこの対応表が使われます。",
                                      f"Saved {len(mapping)} entries to {ID_MAP_FILENAME}.\n"
                                      "Press Run to use it."))

        def on_drop(event):
            """一覧に画像をドロップしたら、入力フォルダにコピーして並べ直す。"""
            dirs, imgs, _o = self._split_dropped(event)
            incoming = self._incoming_images(in_dir, dirs, imgs)
            if not incoming:
                return
            if not messagebox.askyesno(
                    t("確認", "Confirm"),
                    t(f"{len(incoming)} 枚を入力フォルダにコピーして一覧に追加しますか？\n{in_dir}",
                      f"Copy {len(incoming)} image(s) into the input folder and add them?\n{in_dir}"),
                    parent=win):
                return
            self._copy_into(in_dir, incoming)
            keep = collect()
            win.destroy()
            self._open_id_editor(preset=keep)

        self._register_drop(win, on_drop)
        if self.dnd_ok:
            ttk.Label(win, padding=(8, 0), style="Sub.TLabel",
                      text=t("このウィンドウに画像をドロップすると入力フォルダにコピーして追加します",
                             "Drop images here to copy them into the input folder")
                      ).pack(fill="x")

        btm = ttk.Frame(win, padding=8); btm.pack(fill="x")
        ttk.Button(btm, text=t("保存して実行", "Save and run"), style="Accent.TButton",
                   command=lambda: save(True)).pack(side="left")
        ttk.Button(btm, text=t("保存だけ", "Save only"),
                   command=lambda: save(False)).pack(side="left", padx=8)
        ttk.Button(btm, text=t("閉じる", "Close"), command=win.destroy).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", win.destroy)

    def _bulk_paste_ids(self, parent):
        """IDを改行区切りで貼り付けて、一覧の上から順に流し込む。"""
        dlg = tk.Toplevel(parent)
        dlg.title(t("IDをまとめて貼り付け", "Paste IDs in bulk"))
        dlg.geometry("360x420")
        dlg.transient(parent); dlg.grab_set()
        ttk.Label(dlg, padding=8, wraplength=330, style="Sub.TLabel",
                  text=t("1行に1つIDを貼り付けてください。一覧の上から順に入ります。"
                         "数字以外の文字が混ざっていても数字だけ拾います。",
                         "One ID per line. They are filled from the top of the list. "
                         "Non-digit characters are ignored.")).pack(fill="x")
        txt = tk.Text(dlg, height=18, wrap="none")
        txt.pack(fill="both", expand=True, padx=8)
        txt.focus_set()

        def apply_ids():
            raw = txt.get("1.0", "end").splitlines()
            ids = []
            for line in raw:
                digits = re.sub(r"[^0-9]", "", line)
                if digits:
                    ids.append(digits)
            if not ids:
                messagebox.showwarning(t("確認", "Notice"),
                                       t("IDが見つかりませんでした。", "No IDs found."), parent=dlg)
                return
            rows = self._id_rows
            if len(ids) != len(rows) and not messagebox.askyesno(
                    t("確認", "Confirm"),
                    t(f"ID {len(ids)} 件に対して画像は {len(rows)} 枚です。"
                      f"上から {min(len(ids), len(rows))} 件だけ入れますか？",
                      f"{len(ids)} IDs vs {len(rows)} images. "
                      f"Fill only the first {min(len(ids), len(rows))}?"), parent=dlg):
                return
            for (_, var), uid in zip(rows, ids):
                var.set(uid)
            dlg.destroy()

        bar = ttk.Frame(dlg, padding=8); bar.pack(fill="x")
        ttk.Button(bar, text=t("流し込む", "Fill"), style="Accent.TButton",
                   command=apply_ids).pack(side="left")
        ttk.Button(bar, text=t("キャンセル", "Cancel"), command=dlg.destroy).pack(side="right")

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
                # 戻り値 False も失敗（Windows APIのフォールバックが失敗した場合）。
                # 以前は例外だけ数えていたので「N枚送りました」と嘘の報告をしていた。
                if not send_to_trash(f):
                    self._log(t(f"  [!] 削除失敗: {f.name}", f"  [!] Could not delete: {f.name}"))
                    failed += 1
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
            out, backup, removed, kept = dedupe_config(Path(start))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t("エラー", "Error"), str(e))
            return
        if removed == 0:
            messagebox.showinfo(t("重複なし", "No Duplicates"),
                                t(f"config.xml: 重複なし（{kept} 件）。",
                                  f"config.xml: no duplicates ({kept} remain)."))
        else:
            messagebox.showinfo(t("完了", "Done"),
                                t(f"{removed} 件の重複を削除しました（残り {kept} 件）。\n元のファイル: {backup.name}",
                                  f"Removed {removed} duplicate(s) ({kept} remain).\nOriginal: {backup.name}"))

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
        # _build() でウィジェットを作り直すと表示が消えるので、内容を保持しておく
        # （log_lines は復元側だけあって、詰める側が無かった）
        self.log_lines.append(msg)
        if len(self.log_lines) > 5000:
            del self.log_lines[:1000]
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log_has_content = True

    def _show_preview(self, b64, ev, holder):
        """保存前プレビューのダイアログ。選択をholderに入れてevをセットする。
        ここで例外が出ると、ev を待っているワーカースレッドが永久に止まる
        （キャンセルも効かずアプリを殺すしかなくなる）ので、必ず finally でセットする。"""
        try:
            self._show_preview_impl(b64, ev, holder)
        except Exception as e:  # noqa: BLE001
            try:
                self._log(t(f"[!] プレビューを表示できませんでした（保存して続行します）: {e}",
                            f"[!] Could not show preview (saving and continuing): {e}"))
            except Exception:  # noqa: BLE001
                pass
            holder.setdefault("r", "save")
        finally:
            ev.set()

    def _show_preview_impl(self, b64, ev, holder):
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
                    try:
                        self._show_preview(b64, ev, holder)
                    finally:
                        ev.set()
                elif kind == "done":
                    elapsed = time.monotonic() - getattr(self, "_run_start", time.monotonic())
                    self._running = False
                    self._apply_busy_state()
                    self.status_lbl.config(text="")
                    self._current_file = ""
                    self._save_settings()
                    if payload:
                        messagebox.showerror(t("エラー", "Error"), str(payload))
                    else:
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
            if getattr(self, "_running", False) and hasattr(self, "_run_start"):
                elapsed = time.monotonic() - self._run_start
                fname = getattr(self, "_current_file", "")
                text = t(f"処理中… {elapsed:.0f}秒経過", f"Processing… {elapsed:.0f}s")
                if fname:
                    text += f"  |  {fname}"
                self.status_lbl.config(text=text)
        except Exception:  # noqa: BLE001
            pass
        # ウィンドウ破棄後に after が発火すると Tcl が
        # invalid command name "..._poll" を投げる（終了時にstderrへ出る）
        if not getattr(self, "_closing", False):
            try:
                self._poll_id = self.after(100, self._poll)
            except tk.TclError:
                pass

    def _run(self):
        th = getattr(self, "_worker", None)
        if th is not None and th.is_alive():
            # ボタンの無効化だけを頼りにすると、言語切替で再構築された直後に
            # 同じ出力フォルダへ2本目のスレッドを起動できてしまう
            messagebox.showwarning(t("確認", "Notice"),
                                   t("すでに処理中です。", "A job is already running."))
            return
        if not self.in_var.get():
            messagebox.showwarning(t("確認", "Notice"),
                                   t("入力フォルダを選んでください。", "Please select the input folder."))
            return
        try:
            opts = {
                "input":         self.in_var.get(),
                "output":        self.out_var.get() or "processed",
                "size":          int(self.size_var.get()),
                "scale":         float(self.scale_var.get()),
                "ocr_id":        self.ocr_var.get(),
                "face_crop":     self.facecrop_var.get(),
                "face_size":     max(FACE_SIZE_MIN,
                                     min(float(self.face_size_var.get()), FACE_SIZE_MAX)),
                "face_neck":     FACE_NECK,
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
        if not 1 <= opts["size"] <= 4096:
            messagebox.showwarning(t("確認", "Notice"),
                                   t("出力サイズは 1〜4096 px で指定してください。",
                                     "Output size must be between 1 and 4096 px."))
            return
        if not 0 < opts["scale"] <= 8:
            messagebox.showwarning(t("確認", "Notice"),
                                   t("拡大倍率は 0 より大きく 8 以下で指定してください。",
                                     "Upscale factor must be greater than 0 and no more than 8."))
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
                # 万一 ev がセットされないままでも固まらないように上限を置く
                if not ev.wait(timeout=600):
                    return "save"
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

        self._running = True
        self._apply_busy_state()
        self.pb.configure(value=0, maximum=1)
        self.log.delete("1.0", "end")
        self.log_lines.clear()
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

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

if __name__ == "__main__":
    # ── 多重起動防止 ──────────────────────────────────────────
    import ctypes as _ctypes
    _mutex = _ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\FMFaceProcessor_v1")
    if _ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _root = tk.Tk()
        _root.withdraw()
        from tkinter import messagebox as _mb
        _mb.showwarning(
            "FM Face Processor",
            "すでに起動しています。\n既存のウィンドウを確認してください。"
        )
        _root.destroy()
        raise SystemExit(0)
    # ──────────────────────────────────────────────────────────
    App().mainloop()
