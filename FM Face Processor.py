#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FM Face Processor (JA/EN/TR, 全自動OCR / ダークモード)
===================================================
日本語・英語・トルコ語を右上で選べます (select JA/EN/TR at the top right).
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

import io
import math
import os
import re
import shutil
import json
import queue
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from xml.sax.saxutils import quoteattr

# PyInstaller の画面版では標準出力が接続されず None になる。
# rembg の初回モデル取得で tqdm が stderr へ進捗を書こうとするため、
# 書き込み先を破棄用ストリームで補う。
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

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
APP_VERSION = "v2.4.0"
APP_ICON_FILENAME = "fm_face_processor.ico"
APP_USER_MODEL_ID = "kumajia.FMFaceProcessor"
# 「ID」に続く数字（FMのperson ID）を拾う
# FMのperson IDは5桁以上。2桁以上を拾うと "Real Madrid 2024 Unique ID 1928374651"
# のような文字列で年号や背番号を先に拾ってしまうため下限を上げてある。
ID_RE = re.compile(r"id[^0-9]{0,6}(\d{5,})", re.IGNORECASE)

# ----- 言語切替（日本語/英語/トルコ語）-----
LANGUAGES = {"ja": "日本語", "en": "English", "tr": "Türkçe"}
# 言語メニューの「Windowsに合わせる」。設定ファイルには "lang": "system" で保存する。
LANG_SYSTEM = "system"
_LANG = "ja"


def set_lang(lang):
    global _LANG
    _LANG = lang if lang in LANGUAGES else "ja"


def _windows_ui_langid():
    """現在のWindowsユーザーの表示言語（LANGID）。取得できなければ None。"""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:  # noqa: BLE001
        return None


def detect_windows_language():
    """Windowsの表示言語を ja / tr / en に対応付ける。

    日本語 → ja、トルコ語 → tr。英語・未対応の言語・取得失敗は en。"""
    langid = _windows_ui_langid()
    if not langid:
        return "en"
    primary = int(langid) & 0x3FF   # 下位10ビットが主言語（0x11=日本語, 0x1F=トルコ語）
    return {0x11: "ja", 0x1F: "tr"}.get(primary, "en")


def resolve_language(pref):
    """設定値（ja / en / tr / system）から実際に表示する言語を決める。"""
    return pref if pref in LANGUAGES else detect_windows_language()


def _display_width(text):
    """全角文字を2、半角文字を1として数えた文字幅（ttk の width 指定用）。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def language_menu_label(pref):
    """言語メニューに表示する名前。「Windowsに合わせる」は表示中の言語で書く。"""
    if pref in LANGUAGES:
        return LANGUAGES[pref]
    return t("Windowsに合わせる", "Match Windows", "Windows ile aynı")


# 日本語表示で優先するUIフォント。Noto Sans JP が無ければメイリオ（標準ウェイト）。
# メイリオは日本語版Windowsでは「メイリオ」、他言語版では「Meiryo」という名前で見える。
JP_UI_FONT_CANDIDATES = ("Noto Sans JP", "Meiryo", "メイリオ")
# Tk と sv_ttk が文字に使う名前付きフォント。これを差し替えると、
# ttk の各スタイル・入力欄・コンボボックスの一覧まで同じフォントになる。
UI_NAMED_FONTS = (
    "TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
    "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont",
    "SunValleyCaptionFont", "SunValleyBodyFont", "SunValleyBodyStrongFont",
    "SunValleyBodyLargeFont", "SunValleySubtitleFont", "SunValleyTitleFont",
    "SunValleyTitleLargeFont", "SunValleyDisplayFont",
)
LOG_FONT_DEFAULT = ("Consolas", 10)


def japanese_ui_font_family(root):
    """インストール済みの日本語UIフォント名。どちらも無ければ None（既存の指定のまま）。"""
    from tkinter import font as tkfont
    try:
        available = set(tkfont.families(root))
    except Exception:  # noqa: BLE001
        return None
    for family in JP_UI_FONT_CANDIDATES:
        if family in available:
            return family
    return None


def t(ja, en, tr=None):
    """選択中の言語を返す。未翻訳のトルコ語は英語にフォールバックする。"""
    if _LANG == "tr":
        return tr if tr is not None else en
    return en if _LANG == "en" else ja


def resource_path(filename):
    """開発時とPyInstaller版の両方で同梱ファイルを見つける。"""
    roots = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        roots.append(Path(bundle_root))
    try:
        source_root = Path(__file__).resolve().parent
        roots.extend((source_root / "assets", source_root))
    except Exception:  # noqa: BLE001
        pass
    roots.append(Path.cwd())
    for root in roots:
        candidate = root / filename
        if candidate.exists():
            return candidate
    return None


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
                  f"  [i] AI upscale unavailable (no onnxruntime). Using normal resize: {e}", f'  [i] Yapay zekâ ile büyütme kullanılamıyor (onnxruntime yok). Normal büyütme kullanılıyor: {e}'))
            return None
        path = _find_esrgan_model()
        if not path:
            log(t("  [i] AI高画質化モデル(.onnx)が見つかりません。通常拡大で続行します。",
                  "  [i] AI upscale model (.onnx) not found. Using normal resize.", '  [i] Yapay zekâ büyütme modeli (.onnx) bulunamadı. Normal büyütme kullanılıyor.'))
            return None
        try:
            so = _ort_options()
            _esrgan_sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"]) if so \
                else ort.InferenceSession(path, providers=["CPUExecutionProvider"])
            log(t("  [i] AI高画質化（Real-ESRGAN）を使用します。",
                  "  [i] Using AI upscale (Real-ESRGAN).", '  [i] Yapay zekâ ile büyütme (Real-ESRGAN) kullanılıyor.'))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] AI高画質化モデルを読み込めませんでした（通常拡大で続行）: {e}",
                  f"  [!] Could not load AI upscale model (using normal resize): {e}", f'  [!] Yapay zekâ büyütme modeli yüklenemedi (normal büyütme kullanılıyor): {e}'))
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
                      f"  [!] Error during AI upscale -> normal resize ({e})", f'  [!] Yapay zekâ ile büyütme sırasında hata -> normal büyütme ({e})'))
    w, h = img.size
    return img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)


_rembg_sessions = {}
_rembg_lock = threading.Lock()


class BackgroundRemovalError(RuntimeError):
    """背景除去を安全に完了できず、出力を保存してはいけない状態。"""

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
        hist = alpha.histogram()
        total = img.width * img.height
        transparent = sum(hist[:8])
        opaque = sum(hist[248:])
        if transparent <= total * float(min_ratio) or opaque <= total * 0.05:
            return False

        # 透明な外枠や角丸だけの通常写真を「切り抜き済み」と誤認しない。
        # 本物の人物切り抜きなら、不透明部分の外接矩形の内側にも、髪・首・肩の
        # シルエットに沿った透明部分が存在する。
        solid = alpha.point(lambda value: 255 if value >= 128 else 0)
        bbox = solid.getbbox()
        if bbox is None:
            return False
        enclosed = alpha.crop(bbox)
        enclosed_total = enclosed.width * enclosed.height
        enclosed_transparent = sum(enclosed.histogram()[:8])
        return enclosed_transparent > enclosed_total * 0.005
    except Exception:  # noqa: BLE001
        return False


def _removebg_mask_is_incomplete(img, face_box=None):
    """remove.bgが背景をほぼ残した場合を検出し、理由も返す。"""
    try:
        import numpy as np
        alpha = np.asarray(img.convert("RGBA").getchannel("A"), dtype=np.uint8)
        height, width = alpha.shape
        transparent_ratio = float((alpha <= 8).mean())
        opaque_ratio = float((alpha >= 248).mean())

        face_area_ratio = None
        face_opaque_ratio = None
        far_background_ratio = None
        if face_box is not None:
            x, y, w, h = (int(round(v)) for v in face_box)
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(width, x + max(1, w)), min(height, y + max(1, h))
            if x1 > x0 and y1 > y0:
                face_area_ratio = ((x1 - x0) * (y1 - y0)) / float(width * height)
                face_opaque_ratio = float((alpha[y0:y1, x0:x1] >= 128).mean())

                # 顔から十分離れた場所は、人物写真ならほぼ背景のはず。
                # 回転でできた上端の透明三角だけではここを通過できないよう、
                # 顔・首・上半身が入り得る広めの長方形の「外側」を確認する。
                near_x0 = max(0, int(round(x - 2.5 * w)))
                near_x1 = min(width, int(round(x + 3.5 * w)))
                near_y0 = max(0, int(round(y - 1.25 * h)))
                near_y1 = min(height, int(round(y + 5.0 * h)))
                far_mask = np.ones((height, width), dtype=bool)
                far_mask[near_y0:near_y1, near_x0:near_x1] = False
                far_count = int(far_mask.sum())
                if far_count >= width * height * 0.08:
                    far_background_ratio = float((alpha[far_mask] <= 32).mean())

        # 顔そのものが大きく消えていれば、背景量に関係なく不完全。
        if face_opaque_ratio is not None and face_opaque_ratio < 0.70:
            return True, t(
                f"顔領域の保持率が低い（{face_opaque_ratio:.0%}）",
                f"low face retention ({face_opaque_ratio:.0%})", f'yüz yeterince korunmadı ({face_opaque_ratio:.0%})')

        # 顔が画像の一部しか占めない素材なのに、ほぼ全体が不透明なら
        # ポスター背景などを人物とまとめて残した可能性が高い。
        small_face = face_area_ratio is None or face_area_ratio < 0.20
        if small_face and far_background_ratio is not None and far_background_ratio < 0.35:
            return True, t(
                f"顔から離れた背景が残っている（透明 {far_background_ratio:.1%}）",
                f"distant background remains ({far_background_ratio:.1%} transparent)", f'uzaktaki arka plan kalıntıları mevcut ({far_background_ratio:.1%} şeffaf)')

        if small_face and transparent_ratio < 0.05 and opaque_ratio > 0.85:
            return True, t(
                f"透明部分が少なすぎる（{transparent_ratio:.1%}）",
                f"too little transparency ({transparent_ratio:.1%})", f'şeffaflık çok az ({transparent_ratio:.1%})')

        # 回転で生じた細い透明三角だけを「背景除去成功」と誤認しない。
        if small_face and transparent_ratio < 0.025:
            return True, t(
                f"背景がほぼ残っている（透明 {transparent_ratio:.1%}）",
                f"background mostly remains ({transparent_ratio:.1%} transparent)", f'arka planın büyük kısmı duruyor ({transparent_ratio:.1%} şeffaf)')

        return False, ""
    except Exception:  # noqa: BLE001
        return False, ""


def remove_background(img, model_name, alpha_matting=False, log=None, removebg_key=None,
                      face_box=None):
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
            incomplete, reason = _removebg_mask_is_incomplete(out, face_box)
            if incomplete:
                if log:
                    log(t(f"  [i] remove.bgの切り抜きが不完全なため、ローカルAIで再処理します: {reason}",
                          f"  [i] remove.bg cutout was incomplete; retrying with local AI: {reason}", f'  [i] remove.bg arka planı tam kaldıramadı; yerel yapay zekâ ile yeniden deneniyor: {reason}'))
                _count_result("removebg_incomplete")
                try:
                    return _remove_background_once(img, model_name, alpha_matting, log=log)
                except Exception as local_error:  # noqa: BLE001
                    message = t(
                        f"remove.bgとローカルAIの両方で背景除去に失敗しました: {local_error}",
                        f"Both remove.bg and local AI background removal failed: {local_error}", f'Hem remove.bg hem de yerel yapay zekâ ile arka plan kaldırma başarısız oldu: {local_error}')
                    if log:
                        log(t(f"  [!] {message}（この画像は保存しません）",
                              f"  [!] {message} (this image will not be saved)", f'  [!] {message} (bu görüntü kaydedilmeyecek)'))
                    raise BackgroundRemovalError(message) from local_error
            if log:
                log(t("  [i] remove.bg で背景を除去しました", "  [i] Background removed via remove.bg", '  [i] Arka plan remove.bg ile kaldırıldı'))
            _count_result("removebg_ok")
            return out
        except BackgroundRemovalError:
            raise
        except Exception as e:  # noqa: BLE001
            _count_result("removebg_error")
            if log:
                log(t(f"  [i] remove.bg が使えませんでした（ローカルAIで続行）: {e}",
                      f"  [i] remove.bg unavailable (using local AI): {e}", f'  [i] remove.bg kullanılamıyor (yerel yapay zekâ kullanılıyor): {e}'))
    try:
        return _remove_background_once(img, model_name, alpha_matting, log=log)
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
                      "  [i] Out of memory; retrying with the lighter u2net model…", '  [i] Bellek yetersiz; daha hafif u2net modeliyle yeniden deneniyor…'))
            return _remove_background_once(img, "u2net", alpha_matting, max_side=1280, log=log)
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


# CPU推論時に onnxruntime のメモリアリーナを切るモデル。
# birefnet-portrait は既定設定だと推論のたびに確保した数GBをアリーナが抱え続け、
# 並列1で約13GB・並列2で約23GBに達する。切ると推論後に解放され、
# 出力は同一のままピークがほぼ半分になる（処理は1〜2割遅くなる）。
_LOW_MEM_ARENA_MODELS = frozenset({"birefnet-portrait"})


def _rembg_runs_on_cpu(ort):
    """rembg がプロバイダ未指定時にCPUを選ぶかどうか（rembg の BaseSession と同じ判定）。"""
    device = ort.get_device()
    providers = ort.get_available_providers()
    if device == "GPU" and "CUDAExecutionProvider" in providers:
        return False
    if device[0:3] == "GPU" and "ROCMExecutionProvider" in providers:
        return False
    return True


def _new_rembg_session(model_name):
    """rembg のセッションを作る。対象モデルのCPU推論だけ、メモリアリーナを切る。
    rembg の new_session() は SessionOptions を受け取れないため、
    対象モデルはセッションクラスを直接作る（それ以外は new_session() のまま）。"""
    from rembg import new_session
    if model_name not in _LOW_MEM_ARENA_MODELS:
        return new_session(model_name)
    try:
        import onnxruntime as ort
        from rembg.sessions import sessions_class
    except Exception:  # noqa: BLE001
        return new_session(model_name)
    session_class = next((sc for sc in sessions_class if sc.name() == model_name), None)
    if session_class is None or not _rembg_runs_on_cpu(ort):
        return new_session(model_name)
    sess_opts = ort.SessionOptions()
    if "OMP_NUM_THREADS" in os.environ:   # new_session() と同じ扱い
        threads = int(os.environ["OMP_NUM_THREADS"])
        sess_opts.inter_op_num_threads = threads
        sess_opts.intra_op_num_threads = threads
    sess_opts.enable_cpu_mem_arena = False
    sess_opts.enable_mem_pattern = False
    return session_class(model_name, sess_opts, providers=["CPUExecutionProvider"])


# ----- 背景透過の処理先（CPU / GPU：ONNX Runtime DirectML）-----
REMBG_DEVICE_CPU = "cpu"
REMBG_DEVICE_DML = "dml"
REMBG_DEVICES = (REMBG_DEVICE_CPU, REMBG_DEVICE_DML)
# GPU（DirectML）に対応させるモデル（実機で確認済みの2つ）。それ以外のモデルは従来どおりCPUで処理する。
DML_MODELS = ("birefnet-portrait", "birefnet-general-lite")
# 内蔵GPUと外部GPUがあるPCでは、高性能側（外部GPU）を選ばせる
_DML_PROVIDER_OPTIONS = {"device_id": 0, "performance_preference": "high_performance",
                         "device_filter": "gpu"}
# DirectML のグラフ融合を切る。既定（融合あり）だと BiRefNet の融合グラフがGPUメモリに収まらず、
# 共有GPUメモリ（＝PCのRAM）へ11〜14GBあふれて1枚2〜4秒かかる（RTX 4080 SUPERで実測）。
# 切ると出力はほぼ同じ（マスク差は最大1/255）のまま、推論は約10倍速く、RAMも大きく減る。
_DML_DISABLE_GRAPH_FUSION = True
# 1回の「実行」（バッチ）ごとの状態。GPUが失敗したら、そのバッチではGPUに戻さない。
# GPU省メモリ（FP16）: birefnet-general-lite を GPU（DirectML）で使うときだけ選べる（初期値はオフ）。
# 1024×1024のまま重みと計算をFP16にした検証済みモデルをEXEに同梱する（専用VRAM 9.9GB → 5.1GB、RTX 4080 SUPERで実測）。
# CPUに切り替えるときは、従来どおり同じモデルのFP32版（~/.u2net）を使う。
DML_FP16_MODELS = {
    "birefnet-general-lite": ("birefnet-general-lite-fp16.onnx",
                              "311cfd8088ee71224ba0687b00dfad1ed28fc05aae0ce64e87965cc3d4b29d6a"),
}
_dml_fp16_checked = {}
# FP16モデルを使えない理由の区分（"missing" 見つからない / "corrupt" 中身が違う / "load" 読み込めない）。
# 診断結果（共有用）には、この区分だけを出す（パスや例外の生テキストは出さない）。
_dml_fp16_kind = {}
_rembg_batch = {"device": REMBG_DEVICE_CPU, "dml_failed": False, "counts": {}, "models": {},
                "announced": set(), "dml_fail": None, "dml_name": None, "fp16": False,
                "fp16_requested": False, "fp16_fallback": None}
_rembg_batch_lock = threading.Lock()


class _DmlRunError(RuntimeError):
    """DirectML での推論に失敗した（同じモデルのCPU推論に切り替える合図）。"""


class _SerializedSession:
    """onnxruntime セッションの run() を1本ずつに制限する薄いラッパー。
    DirectML は同じセッションへの同時の推論呼び出しに対応していないため。"""

    def __init__(self, inner):
        self._inner = inner
        self._run_lock = threading.Lock()

    def run(self, *args, **kwargs):
        with self._run_lock:
            try:
                return self._inner.run(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                raise _DmlRunError(_ort_error_text(e)) from e

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _ort_error_text(e):
    """onnxruntime の例外を読める文字列にする。日本語版Windowsでは、エラー文（CP932）を
    UTF-8として読もうとして UnicodeDecodeError になることがあるため、元のバイト列を読み直す。"""
    if isinstance(e, UnicodeDecodeError) and isinstance(e.object, (bytes, bytearray)):
        try:
            text = bytes(e.object).decode("mbcs", errors="replace")
            return " ".join(text.split())[:400]
        except Exception:  # noqa: BLE001
            pass
    text = " ".join(str(e).split()) or type(e).__name__
    return f"{type(e).__name__}: {text}"[:400]


_MODEL_LOAD_ERROR_TYPES = ("InvalidProtobuf", "InvalidGraph", "NoModel", "NoSuchFile")
_MODEL_LOAD_ERROR_MARKS = ("INVALID_PROTOBUF", "INVALID_GRAPH", "NO_MODEL", "NO_SUCHFILE", "FP16 model unavailable")


def _is_model_load_error(e, text):
    """onnxruntime がモデルファイル自体を読めなかった失敗か（GPU・ドライバ側の失敗と区別する）。"""
    if type(e).__name__ in _MODEL_LOAD_ERROR_TYPES:
        return True
    text = text or ""
    if any(m in text for m in _MODEL_LOAD_ERROR_MARKS):
        return True
    return "Load model" in text and "failed" in text


def dml_unavailable_reason():
    """DirectML が使えない理由。使えるなら空文字。"""
    if sys.platform != "win32":
        return "Windows only"
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
    except Exception as e:  # noqa: BLE001
        return f"onnxruntime: {_ort_error_text(e)}"
    if "DmlExecutionProvider" not in providers:
        return f"DmlExecutionProvider not in {providers}"
    return ""


def _query_dxgi_adapter():
    """高性能優先で並べた最初のハードウェアGPU（ログ表示用）。失敗しても処理には影響しない。"""
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        def guid(text):
            u = uuid.UUID(text)
            g = GUID(u.fields[0], u.fields[1], u.fields[2])
            g.Data4[:] = list(u.bytes[8:])
            return g

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class DESC1(ctypes.Structure):
            _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint),
                        ("DeviceId", ctypes.c_uint), ("SubSysId", ctypes.c_uint),
                        ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                        ("DedicatedSystemMemory", ctypes.c_size_t),
                        ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID),
                        ("Flags", ctypes.c_uint)]

        def method(obj, index, *argtypes):
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])

        dxgi = ctypes.WinDLL("dxgi")
        factory = ctypes.c_void_p()
        iid_adapter1 = guid("29038f61-3839-4626-91fd-086879011a05")
        use6 = dxgi.CreateDXGIFactory1(ctypes.byref(guid("c1b6694f-ff09-44a9-b03c-77900a0a1d17")),
                                       ctypes.byref(factory)) == 0
        if not use6 and dxgi.CreateDXGIFactory1(
                ctypes.byref(guid("770aae78-f26f-4dba-a829-253c83d1b387")), ctypes.byref(factory)) != 0:
            return {}
        try:
            for i in range(16):
                adapter = ctypes.c_void_p()
                if use6:   # IDXGIFactory6::EnumAdapterByGpuPreference(HIGH_PERFORMANCE)
                    hr = method(factory, 29, ctypes.c_uint, ctypes.c_int, ctypes.POINTER(GUID),
                                ctypes.POINTER(ctypes.c_void_p))(
                        factory, i, 2, ctypes.byref(iid_adapter1), ctypes.byref(adapter))
                else:      # IDXGIFactory1::EnumAdapters1
                    hr = method(factory, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                        factory, i, ctypes.byref(adapter))
                if hr != 0 or not adapter.value:
                    break
                d = DESC1()
                try:
                    method(adapter, 10, ctypes.POINTER(DESC1))(adapter, ctypes.byref(d))
                finally:
                    method(adapter, 2)(adapter)
                if d.Flags & 2:   # DXGI_ADAPTER_FLAG_SOFTWARE
                    continue
                return {"name": d.Description.strip(), "vendor": f"0x{d.VendorId:04x}",
                        "luid": f"0x{d.AdapterLuid.HighPart & 0xffffffff:08x}_0x{d.AdapterLuid.LowPart:08x}",
                        "vram_mb": int(d.DedicatedVideoMemory // (1024 * 1024))}
        finally:
            method(factory, 2)(factory)
    except Exception:  # noqa: BLE001
        pass
    return {}


_DXGI_DETAILS = {}


def _dxgi_adapter_names():
    """このPCのGPU（DXGIアダプター）の名前を LUID ごとに返す。失敗したら空。"""
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                        ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

        class DESC1(ctypes.Structure):
            _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint),
                        ("DeviceId", ctypes.c_uint), ("SubSysId", ctypes.c_uint),
                        ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                        ("DedicatedSystemMemory", ctypes.c_size_t),
                        ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID),
                        ("Flags", ctypes.c_uint)]

        def method(obj, index, *argtypes):
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])

        u = uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387")   # IID_IDXGIFactory1
        iid = GUID(u.fields[0], u.fields[1], u.fields[2])
        iid.Data4[:] = list(u.bytes[8:])
        factory = ctypes.c_void_p()
        if ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return {}
        names = {}
        try:
            for i in range(16):
                adapter = ctypes.c_void_p()
                if method(factory, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))(
                        factory, i, ctypes.byref(adapter)) != 0 or not adapter.value:
                    break                                       # IDXGIFactory1::EnumAdapters1
                d = DESC1()
                try:
                    method(adapter, 10, ctypes.POINTER(DESC1))(adapter, ctypes.byref(d))
                finally:
                    method(adapter, 2)(adapter)
                luid = f"0x{d.AdapterLuid.HighPart & 0xffffffff:08x}_0x{d.AdapterLuid.LowPart:08x}"
                names[luid] = d.Description.strip()
                _DXGI_DETAILS[luid] = {"name": d.Description.strip(), "vram": int(d.DedicatedVideoMemory),
                                       "software": bool(d.Flags & 2)}
        finally:
            method(factory, 2)(factory)
        return names
    except Exception:  # noqa: BLE001
        return {}


def _gpu_mem_by_luid(split=False):
    """このプロセスが各GPUで使っているメモリ（専用＋共有、バイト）を LUID ごとに返す。
    タスクマネージャーと同じ Windows の性能カウンタ「GPU Process Memory」を読む。読めなければ None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class FMT(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class ITEM(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", FMT)]

        pdh = ctypes.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                     ctypes.POINTER(wintypes.DWORD),
                                                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        for fn in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW, pdh.PdhCollectQueryData,
                   pdh.PdhGetFormattedCounterArrayW, pdh.PdhCloseQuery):
            fn.restype = wintypes.DWORD
        query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            return None
        try:
            counters, kinds = [], []
            for kind in ("Dedicated Usage", "Shared Usage"):
                h = ctypes.c_void_p()
                path = "\\GPU Process Memory(pid_%d_*)\\%s" % (os.getpid(), kind)
                if pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(h)) == 0:
                    counters.append(h)
                    kinds.append(kind)
            if not counters or pdh.PdhCollectQueryData(query) != 0:
                return None
            fmt = 0x00000200 | 0x00001000 | 0x00008000   # DOUBLE | NOSCALE | NOCAP100
            out = {}
            for h, kind in zip(counters, kinds):
                size, count = wintypes.DWORD(0), wintypes.DWORD(0)
                rc = pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), None)
                if rc != 0x800007D2 or not size.value:    # PDH_MORE_DATA
                    continue
                buf = ctypes.create_string_buffer(size.value)
                if pdh.PdhGetFormattedCounterArrayW(h, fmt, ctypes.byref(size), ctypes.byref(count), buf) != 0:
                    continue
                items = ctypes.cast(buf, ctypes.POINTER(ITEM))
                for i in range(count.value):
                    if items[i].FmtValue.CStatus not in (0, 1):
                        continue
                    m = re.search(r"luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", items[i].szName or "")
                    if m:
                        key = m.group(1).lower()
                        value = items[i].FmtValue.doubleValue
                        if split:      # [専用, 共有]
                            pair = out.setdefault(key, [0.0, 0.0])
                            pair[0 if kind.startswith("Dedicated") else 1] += value
                        else:
                            out[key] = out.get(key, 0.0) + value
            return out
        finally:
            pdh.PdhCloseQuery(query)
    except Exception:  # noqa: BLE001
        return None


def _identify_dml_adapter(before, after):
    """DirectMLセッションの作成前と最初の推論後で、このプロセスのGPUメモリが増えたGPUを探す。
    1つに絞れたらその LUID、絞れなければ None。"""
    if not isinstance(after, dict) or not after:
        return None
    before = before or {}
    grown = sorted(((after[k] - before.get(k, 0.0), k) for k in after), reverse=True)
    if grown[0][0] < 32 * 1024 * 1024:                         # 32MB未満の増加では判断しない
        return None
    if len(grown) > 1 and grown[1][0] > grown[0][0] * 0.25:     # 2つのGPUが同じくらい増えた
        return None
    return grown[0][1]


def _gpu_label(name):
    """ログに出すGPUの表記。使ったGPUを特定できないときは「GPU（DirectML）」だけにする。"""
    if name:
        return t(f"GPU（DirectML: {name}）", f"GPU (DirectML: {name})", f"GPU (DirectML: {name})")
    return t("GPU（DirectML）", "GPU (DirectML)", "GPU (DirectML)")


def _dml_session_adapter(sess):
    """このDirectMLセッションが実際に使ったGPUの名前（特定できなければ None）。初回だけ調べる。"""
    if not getattr(sess, "_fmfp_gpu_checked", False):
        sess._fmfp_gpu_checked = True
        luid = _identify_dml_adapter(getattr(sess, "_fmfp_gpu_before", None), _gpu_mem_by_luid())
        sess._fmfp_gpu_luid = luid
        sess._fmfp_gpu_name = _dxgi_adapter_names().get(luid) if luid else None
    return sess._fmfp_gpu_name


def _dml_fp16_model_path(model_name):
    """同梱のFP16モデルのパスと、使えない場合の理由。中身はSHA-256で確かめ、結果は記録する。"""
    if model_name in _dml_fp16_checked:
        return _dml_fp16_checked[model_name]
    fname, sha = DML_FP16_MODELS[model_name]
    path = resource_path(fname)
    result = (None, f"{fname} not found")
    kind = "missing"
    if path is not None and Path(path).is_file():
        try:
            import hashlib
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() == sha:
                result, kind = (str(path), ""), ""
            else:
                result, kind = (None, f"{fname}: SHA-256 mismatch"), "corrupt"
        except Exception as e:  # noqa: BLE001
            result, kind = (None, f"{fname}: {type(e).__name__}"), "load"
    _dml_fp16_checked[model_name] = result
    _dml_fp16_kind[model_name] = kind
    return result


def fp16_fallback_reason(kind):
    """FP16モデルを使えなかった理由（画面・診断結果用の短い文）。"""
    return {"missing": t("GPU省メモリ（FP16）のモデルが見つからない", "the GPU memory saver (FP16) model was not found",
                         "GPU bellek tasarrufu (FP16) modeli bulunamadı"),
            "corrupt": t("GPU省メモリ（FP16）のモデルが壊れている（SHA-256が一致しない）",
                         "the GPU memory saver (FP16) model is damaged (SHA-256 mismatch)",
                         "GPU bellek tasarrufu (FP16) modeli bozuk (SHA-256 uyuşmuyor)"),
            "load": t("GPU省メモリ（FP16）のモデルを読み込めない", "the GPU memory saver (FP16) model could not be loaded",
                      "GPU bellek tasarrufu (FP16) modeli yüklenemedi")}.get(kind, "?")


def _precision_label(precision):
    return "FP16" if precision == "fp16" else "FP32"


def _device_label(device, name=None, precision="fp32"):
    """ログに出す処理先＋精度。例: GPU（DirectML: NVIDIA …）・FP16 / CPU・FP32"""
    p = _precision_label(precision)
    base = _gpu_label(name) if device == REMBG_DEVICE_DML else "CPU"
    return t(f"{base}・{p}", f"{base}, {p}", f"{base}, {p}")


def _new_rembg_session_dml(model_name, precision="fp32"):
    """DirectML で動く rembg セッションを作る。失敗したら例外（呼び出し側がCPUに切り替える）。
    onnxruntime は既定だとGPUの初期化や推論に失敗したとき黙ってCPUで作り直すため、
    その仕組みを止めて、失敗をアプリ側で確実に検出・記録できるようにしている。"""
    import onnxruntime as ort
    from rembg.sessions import sessions_class
    from rembg.sessions.base import BaseSession
    session_class = next((sc for sc in sessions_class if sc.name() == model_name), None)
    if session_class is None:
        raise RuntimeError(f"unknown model: {model_name}")
    sess_opts = ort.SessionOptions()
    if "OMP_NUM_THREADS" in os.environ:   # new_session() と同じ扱い
        threads = int(os.environ["OMP_NUM_THREADS"])
        sess_opts.inter_op_num_threads = threads
        sess_opts.intra_op_num_threads = threads
    # DirectML の要件：メモリパターン無効・逐次実行
    sess_opts.enable_mem_pattern = False
    sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    if _DML_DISABLE_GRAPH_FUSION:
        sess_opts.add_session_config_entry("ep.dml.disable_graph_fusion", "1")
    providers =[("DmlExecutionProvider", dict(_DML_PROVIDER_OPTIONS)), "CPUExecutionProvider"]
    # 実際に使われたGPUを後で特定するため、作成前のGPUメモリを控えておく
    gpu_before = _gpu_mem_by_luid() or {}
    if precision == "fp16":
        model_path, why = _dml_fp16_model_path(model_name)
        if not model_path:
            raise RuntimeError(f"FP16 model unavailable: {why}")
        if session_class.__init__ is not BaseSession.__init__:
            raise RuntimeError(f"FP16 is not supported for {model_name}")
    if session_class.__init__ is BaseSession.__init__:
        # BaseSession.__init__ と同じ手順。enable_fallback=0 で黙ったCPU切り替えを止める
        sess = session_class.__new__(session_class)
        sess.model_name = model_name
        if precision != "fp16":
            model_path = str(session_class.download_models())
        sess.inner_session = ort.InferenceSession(
            model_path, sess_options=sess_opts, providers=providers, enable_fallback=0)
    else:
        sess = session_class(model_name, sess_opts, providers=providers)
    got = sess.inner_session.get_providers()
    if "DmlExecutionProvider" not in got:
        raise RuntimeError(f"DirectML was not enabled (providers={got})")
    sess.inner_session.disable_fallback()
    sess.inner_session = _SerializedSession(sess.inner_session)
    sess._fmfp_gpu_before = gpu_before
    sess._fmfp_precision = precision
    return sess


# ----- テスト結果の共有（診断用）-----
# 外部テスターが結果を貼り付けて共有するためのテキスト。専用の項目だけから組み立て、
# 画像名・フォルダパス・Windowsユーザー名・APIキー・例外の生テキストは含めない。
APP_BUILD_NOTE = ("DirectML GPU option, settings/diagnostics fixes, lasso collar selection, "
                  "FP16 fallback / lasso / run result / shared log fixes (audit fix 4), "
                  "preview stop / device record / all-failed fixes (fix 5), nothing-saved end state (fix 6), "
                  "skip totals / nothing-saved wording (fix 7), status wrap / config.xml result (fix 8), config.xml outcome / cancel boundary (fix 9), "
                  "output size chosen in the preview (size choice), "
                  "every image saved from its own preview (each-save), "
                  "parallel workers field removed (no-workers), "
                  "next image prepared during the preview on CPU (prefetch)")
# 1回の「実行」の実績。設定値ではなく、実際に起きたことだけを数える。
#   saved / failed / skipped … 画像ごとの結果（skipped には既存ファイル・ID重複などを含む）
#   preview_skipped / preview_cancelled … プレビューで「スキップ」「キャンセル」した枚数（skipped の内数ではない）
#   removebg_ok … remove.bg の切り抜きをそのまま使った枚数
#   removebg_incomplete / removebg_error … remove.bg を試したがローカル背景透過で処理し直した枚数
#   bg_already_cutout … 入力が透過済みのため背景透過を行わなかった枚数
#   skipped_existing … skipped のうち、出力先に同じ名前のPNGがあったため処理しなかった枚数
#   preview_aborted … プレビューを確認できないまま中止した画像（表示失敗・無操作・応答なしなど。利用者のキャンセルではない）
_BATCH_STAT_KEYS = ("saved", "failed", "skipped", "preview_skipped", "preview_cancelled",
                    "removebg_ok", "removebg_incomplete", "removebg_error", "bg_already_cutout",
                    "skipped_existing", "preview_aborted")
_batch_stats = {k: 0 for k in _BATCH_STAT_KEYS}
# 処理を開始できなかった理由（区分）。開始できたときは None。
#   no_input 入力フォルダが無い / same_dir 入力と出力が同じ / no_images 対応画像が無い
#   no_pairs 有効なペアが無い（サブフォルダモード）/ no_ids IDスクショが無い / no_faces 顔写真が無い（OCRモード）
_batch_not_started = [None]
# プレビューを確認できずに中止した理由（区分）。中止していなければ None。
#   display_error 表示できなかった / idle_timeout 一定時間操作されなかった / no_response 応答がなかった
#   unconfirmed 保存の確認が得られないまま閉じた
_batch_preview_abort = [None]
# config.xml の結果（_finish が write_config の結果をそのまま記録）。None＝config.xml はオフ。
#   {"result": "created" | "regenerated" | "overwritten" | "updated" | "unchanged" | "failed",
#    "added": 新規件数, "total": 合計件数, "backup": 以前のファイルを退避したか（書き込みに失敗しても退避は残す）,
#    "reason": unchanged の理由（no_ids / all_registered）, "error": 失敗の区分（permission / disk_full / io / other）,
#    "stage": 失敗した段階（read / backup / write）}
#   実行の記録（collect_test_record）では、config.xml がオンなのに処理前に中断したとき {"result": "not_run"} になる。
_batch_config = [None]
# 失敗した画像の理由（処理の段階の区分）-> 枚数。理由の文は failure_kind_reason（定型文）
_batch_fail_kinds = {}
_proc_stage = threading.local()      # process_one がいまどの段階か（失敗の理由の区分に使う）
# 次の画像の先読み（_Prefetcher）のスレッドだけが持つ文脈。ops があるスレッドでは、ログ・集計・背景透過の実績を
# その場で反映せず ops にためる（その画像に進んだ時点で、処理スレッドが順番どおりに反映する）。
_prefetch_ctx = threading.local()
_batch_sizes = {}            # 実際に保存したPNGの寸法 (幅, 高さ) -> 枚数（保存後のファイルから読む）
# 保存前プレビューで選んだ出力サイズ（選んだ順・続けて同じ値は1回）。診断に出す。設定値は入れない
_batch_preview_sizes = []
_batch_stats_lock = threading.Lock()
_SYSINFO = {}


def _count_result(kind, n=1):
    ops = getattr(_prefetch_ctx, "ops", None)
    if ops is not None:
        # 先読み中: この実行の集計にはまだ入れない（破棄されたら数えない）
        ops.append(("count", kind, int(n)))
        return
    with _batch_stats_lock:
        _batch_stats[kind] = _batch_stats.get(kind, 0) + int(n)


OUTPUT_SIZE_MIN, OUTPUT_SIZE_MAX = 1, 4096


def parse_output_size(value):
    """出力サイズの入力（数値・文字列）を 1〜4096 の整数にする。使えない値は None。"""
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            value = value.strip()
            if not value.isdigit():
                return None
        n = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != n:
        return None
    return n if OUTPUT_SIZE_MIN <= n <= OUTPUT_SIZE_MAX else None


def _note_preview_size(size):
    """保存前プレビューで出力サイズが決まった・変わったことを記録する（診断用）。"""
    with _batch_stats_lock:
        if not _batch_preview_sizes or _batch_preview_sizes[-1] != int(size):
            _batch_preview_sizes.append(int(size))


def _count_output_size(size):
    with _batch_stats_lock:
        key = (int(size[0]), int(size[1]))
        _batch_sizes[key] = _batch_sizes.get(key, 0) + 1


def _reset_batch_stats():
    with _batch_stats_lock:
        _batch_stats.clear()
        _batch_stats.update({k: 0 for k in _BATCH_STAT_KEYS})
        _batch_sizes.clear()
        del _batch_preview_sizes[:]
        _batch_not_started[0] = None
        _batch_preview_abort[0] = None
        _batch_config[0] = None
        _batch_fail_kinds.clear()


def _mark_not_started(reason):
    """処理対象が無いまま終わった（保存処理を1枚も始めていない）ことを記録する。"""
    with _batch_stats_lock:
        _batch_not_started[0] = reason


class CancelGate:
    """キャンセルの受付。threading.Event と同じ is_set / wait / set を持つ。
    画像処理から終了処理へ移る境界で close() し、その時点でキャンセルを受け付けていたかを一度だけ確定する。
    境界より後の request()（キャンセルボタン・終了操作）は受け付けず False を返し、確定した結果も変えない。"""

    def __init__(self):
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def request(self):
        """キャンセルを要求する。受け付けたら True、終了処理に入ったあとなら False。"""
        with self._lock:
            if self._closed:
                return False
            self._ev.set()
            return True

    def set(self):
        return self.request()

    def is_set(self):
        return self._ev.is_set()

    def wait(self, timeout=None):
        return self._ev.wait(timeout)

    @property
    def closed(self):
        return self._closed

    def close(self):
        """境界：以後の要求を受け付けず、キャンセルを受け付けていたか（確定した結果）を返す。何度呼んでも同じ結果。"""
        with self._lock:
            self._closed = True
            return self._ev.is_set()


def close_cancel(cancel):
    """キャンセルの受付を締め切り、確定した結果を返す（CancelGate 以外の Event は今の状態を返す）。"""
    if cancel is None:
        return False
    close = getattr(cancel, "close", None)
    if close is not None:
        return bool(close())
    return bool(cancel.is_set())


def _mark_preview_abort(kind):
    """プレビューを確認できずに中止したことを記録する（最初の理由を残す）。"""
    with _batch_stats_lock:
        if _batch_preview_abort[0] is None:
            _batch_preview_abort[0] = kind


def preview_abort_reason(kind):
    """プレビューで中止した理由（定型文。画面の状態表示・処理ログの最後の行・診断結果で同じ文を使う）。"""
    return {"display_error": t("プレビューを表示できなかったため", "the preview could not be shown",
                               "önizleme gösterilemediği için"),
            "idle_timeout": t("プレビューが一定時間操作されなかったため", "the preview was left idle too long",
                              "önizleme uzun süre kullanılmadığı için"),
            "no_response": t("プレビューの応答がなかったため", "the preview did not respond", "önizleme yanıt vermediği için"),
            "unconfirmed": t("プレビューで保存を確認できなかったため", "saving was not confirmed in the preview",
                             "kaydetme önizlemede onaylanmadığı için")}.get(kind, "?")


class _PrefetchDiscarded(Exception):
    """破棄された先読みを、段階の境目で打ち切るための例外（先読みスレッドの中だけで使う）。"""


def _set_stage(name):
    _proc_stage.v = name
    job = getattr(_prefetch_ctx, "job", None)
    if job is not None and job["discard"]:
        raise _PrefetchDiscarded()


def _is_download_error(exc):
    """モデルのダウンロード・通信の失敗らしい例外か（例外の連鎖もたどる）。"""
    seen = 0
    x = exc
    while x is not None and seen < 6:
        seen += 1
        mod = (type(x).__module__ or "").split(".")[0]
        if mod in ("requests", "urllib3", "urllib", "pooch", "http", "socket", "ssl"):
            return True
        if isinstance(x, (ConnectionError, TimeoutError)):
            return True
        msg = str(x).lower()
        if any(k in msg for k in ("download", "network", "connection", "urlopen", "name resolution",
                                  "timed out", "http error", "ssl")):
            return True
        x = x.__cause__ or x.__context__
    return False


def _count_failure(exc):
    """失敗した画像の理由を、失敗した段階の区分で数える（例外の文は数えない）。区分を返す。"""
    stage = getattr(_proc_stage, "v", None) or "other"
    _proc_stage.v = None
    if isinstance(exc, MemoryError):
        kind = "memory"
    elif stage == "bg" and _is_download_error(exc):
        kind = "bg_download"
    else:
        kind = stage
    with _batch_stats_lock:
        _batch_fail_kinds[kind] = _batch_fail_kinds.get(kind, 0) + 1
    return kind


def failure_kind_reason(kind):
    """失敗の理由（定型文。パスや例外の文は含めない）。"""
    return {"read": t("画像を読み込めない", "the image could not be read", "görüntü okunamadı"),
            "detect": t("顔検出・水平補正でエラー", "error in face detection / leveling", "yüz algılama / düzeltmede hata"),
            "bg_download": t("背景透過のモデルを用意できない（ダウンロードに失敗。インターネット接続が必要）",
                             "the background removal model could not be prepared (download failed; internet connection required)",
                             "arka plan kaldırma modeli hazırlanamadı (indirme başarısız; internet bağlantısı gerekir)"),
            "bg": t("背景透過でエラー", "error in background removal", "arka plan kaldırmada hata"),
            "edit": t("トリミング・プレビューの編集でエラー", "error in cropping / preview edits",
                      "kırpma / önizleme düzenlemelerinde hata"),
            "upscale": t("拡大（高画質化）でエラー", "error in upscaling", "büyütmede hata"),
            "save": t("PNGの作成・保存でエラー", "error while making / saving the PNG", "PNG oluşturma / kaydetmede hata"),
            "memory": t("メモリ不足", "out of memory", "bellek yetersiz"),
            "other": t("その他のエラー", "other error", "diğer hata")}.get(kind, t("その他のエラー", "other error", "diğer hata"))


def format_fail_kinds(kinds):
    """{区分: 枚数} -> 「背景透過でエラー 2 枚 / 画像を読み込めない 1 枚」（多い順）"""
    items = sorted((kinds or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    if not items:
        return t("不明", "unknown", "bilinmiyor")
    return " / ".join(t(f"{failure_kind_reason(k)} {n} 枚", f"{failure_kind_reason(k)}: {n}",
                        f"{failure_kind_reason(k)}: {n}") for k, n in items)


def not_started_reason(reason):
    """開始できなかった理由（画面・診断結果用。パスは含めない）。"""
    exts = " ".join(sorted(SUPPORTED_EXT))
    return {"no_input": t("入力フォルダが見つかりません", "the input folder was not found", "girdi klasörü bulunamadı"),
            "same_dir": t("入力フォルダと出力フォルダが同じです（元画像の上書きを防ぐため）",
                          "the input and output folders are the same (to protect the source images)",
                          "girdi ve çıktı klasörleri aynı (kaynak görüntüleri korumak için)"),
            "no_images": t(f"入力フォルダに対応画像がありません（{exts}）", f"no supported images in the input folder ({exts})",
                           f"girdi klasöründe desteklenen görüntü yok ({exts})"),
            "no_pairs": t("有効なペア（IDと顔写真）がありません", "no valid pairs (ID and face photo)",
                          "geçerli çift (ID ve yüz fotoğrafı) yok"),
            "no_numeric": t("ファイル名がID（数字）の画像がありません", "no image is named with an ID (digits)",
                            "adı ID (rakam) olan görüntü yok"),
            "no_ids": t("IDスクリーンショットが見つかりません", "no ID screenshots were found", "ID ekran görüntüsü bulunamadı"),
            "no_faces": t("顔写真が見つかりません", "no face photos were found", "yüz fotoğrafı bulunamadı")}.get(reason, "?")


def _batch_size_counts():
    """保存したPNGの寸法ごとの枚数を、多い順（同数なら大きい順）に返す。"""
    with _batch_stats_lock:
        items = list(_batch_sizes.items())
    return sorted(items, key=lambda kv: (-kv[1], -kv[0][0], -kv[0][1]))


def format_size_counts(items):
    """[((w, h), n), ...] -> 「250×250 px 3 枚 / 180×180 px 2 枚」"""
    if not items:
        return t("なし", "none", "yok")
    return " / ".join(t(f"{w}×{h} px {n} 枚", f"{w}×{h} px: {n}", f"{w}×{h} px: {n}")
                      for (w, h), n in items)


def output_size_line(r):
    """診断の「出力サイズ」の行。プレビューの実行ではプレビューで選んだサイズ（非表示の設定値は出さない）。"""
    if r.get("size_source") == "preview" or (r.get("size_source") is None and r.get("preview")):
        chosen = [int(v) for v in (r.get("preview_sizes") or [])]
        if chosen:
            txt = " → ".join(f"{v}×{v} px" for v in chosen)
        else:
            txt = t("未選択", "not chosen", "seçilmedi")
        return t("出力サイズ", "Output size", "Çıktı boyutu") + ": " + t(
            f"保存前プレビューで選択（{txt}）", f"chosen in the preview ({txt})", f"önizlemede seçildi ({txt})")
    size = r.get("size")
    return (t("出力サイズ（設定）", "Output size (setting)", "Çıktı boyutu (ayar)")
            + (f": {size}×{size} px" if size else ": ?"))


def _outcome_kind(result):
    """process_one の戻り値を、数える区分に変える。"""
    if result is True:
        return "saved"
    if result == "skip":
        return "preview_skipped"
    if result == "cancel":
        return "preview_cancelled"
    if result == "abort":
        return "preview_aborted"
    return "failed"


class _GpuPeakSampler:
    """実行中、このアプリのGPUメモリ（専用・共有）をアダプターごとに0.5秒おきに読み、最大値を残す。"""

    def __init__(self, interval=0.5):
        self.interval = interval
        self.peak = {}
        self.measured = False
        self._stop = threading.Event()
        self._th = None

    def start(self):
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()
        return self

    def _sample(self):
        snap = _gpu_mem_by_luid(split=True)
        if isinstance(snap, dict):
            self.measured = True
            for luid, (ded, shr) in snap.items():
                p = self.peak.setdefault(luid, [0.0, 0.0])
                p[0] = max(p[0], ded)
                p[1] = max(p[1], shr)

    def _loop(self):
        while True:
            self._sample()
            if self._stop.wait(self.interval):
                break

    def stop(self):
        self._stop.set()
        if self._th is not None:
            self._th.join(3)
        self._sample()


def _system_info():
    """OS・CPU・RAM・GPU（型番と専用VRAM）・onnxruntime。1回だけ調べて記録する。"""
    if _SYSINFO:
        return _SYSINFO
    info = {"os": "", "cpu": "", "threads": os.cpu_count() or 0, "ram_gb": None, "gpus": [], "ort": ""}
    try:
        # 共有用なので「Windows 11」まで（ビルド番号は出さない）。Windows 11 もメジャー番号は 10 のため、ビルドで判定する
        build = int(sys.getwindowsversion().build)
        info["os"] = "Windows 11" if build >= 22000 else "Windows 10" if build >= 10240 else "Windows"
    except Exception:  # noqa: BLE001
        pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            info["cpu"] = " ".join(str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).split())
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        class MSX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MSX(); m.dwLength = ctypes.sizeof(m)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            info["ram_gb"] = m.ullTotalPhys / 1024 ** 3
    except Exception:  # noqa: BLE001
        pass
    try:
        _dxgi_adapter_names()
        info["gpus"] = [dict(luid=k, **v) for k, v in _DXGI_DETAILS.items() if not v.get("software")]
    except Exception:  # noqa: BLE001
        pass
    try:
        import onnxruntime as ort
        info["ort"] = ort.__version__ + (" DirectML" if "DmlExecutionProvider" in ort.get_available_providers() else "")
    except Exception:  # noqa: BLE001
        pass
    _SYSINFO.update(info)
    return _SYSINFO


def _hresult_code(text):
    """エラー文からHRESULT（例: 887A0002）だけを取り出す。無ければ空。"""
    m = re.search(r"\b(8[0-9A-Fa-f]{7})\b", text or "")
    return f"0x{m.group(1).upper()}" if m else ""


def collect_test_record(kind, opts, sampler):
    """1回の「実行」の結果を、共有用の項目だけで記録する（画像名やパスは持たない）。"""
    with _rembg_batch_lock:
        b = {k: _rembg_batch.get(k) for k in ("counts", "dml_fail", "dml_name", "dml_luid", "fp16", "models",
                                              "fp16_requested", "fp16_fallback", "cpu_reason")}
        counts = dict(b["counts"] or {})
        models = dict(b["models"] or {})
    with _batch_stats_lock:
        st = dict(_batch_stats)
        not_started = _batch_not_started[0]
        preview_abort = _batch_preview_abort[0]
        fail_kinds = dict(_batch_fail_kinds)
        config = _batch_config[0]
        preview_sizes = list(_batch_preview_sizes)
    sizes = _batch_size_counts()
    # 保存前プレビューの実行では、出力サイズはプレビューで選ぶ。メイン画面の（非表示の）設定値は記録しない
    preview_run = bool(opts.get("_preview"))
    if config is None and opts.get("make_config") and not not_started:
        # config.xml はオンだが、config.xml の処理の前に中断した（予期しない例外）。設定が「オフ」のときとは区別する
        config = {"result": "not_run", "reason": "aborted"}
    fail = None
    if b["dml_fail"]:
        stage, err = b["dml_fail"]
        fail = {"stage": stage, "code": _hresult_code(err)}
    gpu_mem = "na"
    if kind == "gpu":
        gpu_mem = None
        if sampler is not None and sampler.measured:
            peak = sampler.peak
            luid = b["dml_luid"] if b["dml_luid"] in peak else (
                max(peak, key=lambda k: sum(peak[k])) if peak else None)
            ded, shr = peak.get(luid, [0.0, 0.0]) if luid else (0.0, 0.0)
            gpu_mem = {"dedicated_gb": ded / 1024 ** 3, "shared_gb": shr / 1024 ** 3}
    vram = None
    if b["dml_luid"] and b["dml_luid"] in _DXGI_DETAILS:
        vram = _DXGI_DETAILS[b["dml_luid"]]["vram"] / 1024 ** 3
    return {
        "kind": kind,
        "bg_removal": bool(opts.get("bg_removal")), "model": str(opts.get("rembg_model", "")),
        "fp16": bool(b["fp16"]),
        # GPU省メモリ（FP16）を選んだが使えず、CPU・FP32にした理由の区分（None＝切り替えなし）
        "fp16_requested": bool(b.get("fp16_requested")), "fp16_fallback": b.get("fp16_fallback"),
        "not_started": not_started,                      # 開始できなかった理由の区分（None＝開始した）
        "skipped_existing": st.get("skipped_existing", 0),
        # 選んだ処理先（画面の選択）と GPU省メモリ（FP16）の選択。実際の処理先は gpu_images / cpu_images
        "device_selected": str(opts.get("_device_selected") or (REMBG_DEVICE_DML if kind == "gpu" else REMBG_DEVICE_CPU)),
        "fp16_selected": bool(opts.get("_fp16_selected")),
        # GPUを選んだのに開始時からCPUにした理由の区分（dml_unavailable / model_unsupported / fp16）。None＝なし
        "cpu_reason": b.get("cpu_reason"),
        "preview_aborted": st.get("preview_aborted", 0), "preview_abort": preview_abort,
        "fail_kinds": fail_kinds,
        # 実際に同時に処理した数（画像は必ず1枚ずつプレビューで確認するので1）。設定値ではない
        "workers": int(opts.get("_workers_actual", 1) or 1),
        "matting": bool(opts.get("alpha_matting")),
        # 出力サイズの設定値（プレビューなしの実行だけ）。プレビューの実行では None
        "size": None if preview_run else int(opts.get("size", 0) or 0),
        "size_source": "preview" if preview_run else "setting",
        "preview_sizes": preview_sizes,                  # プレビューで選んだサイズ（選んだ順）
        "sizes": [[w, h, n] for (w, h), n in sizes],     # 実際に保存したPNGの寸法と枚数
        "preview": bool(opts.get("_preview")),
        "preview_skipped": st.get("preview_skipped", 0), "preview_cancelled": st.get("preview_cancelled", 0),
        "removebg": bool(opts.get("removebg_key")) and bool(opts.get("bg_removal")),
        "removebg_ok": st.get("removebg_ok", 0), "removebg_incomplete": st.get("removebg_incomplete", 0),
        "removebg_error": st.get("removebg_error", 0), "bg_already_cutout": st.get("bg_already_cutout", 0),
        "models": models,
        "saved": st.get("saved", 0), "failed": st.get("failed", 0), "skipped": st.get("skipped", 0),
        "gpu_images": counts.get(REMBG_DEVICE_DML, 0), "cpu_images": counts.get(REMBG_DEVICE_CPU, 0),
        "gpu_name": b["dml_name"], "gpu_vram_gb": vram, "fallback": fail, "gpu_mem": gpu_mem,
        "config": config,                                # config.xml の結果（None＝オフ）
        "elapsed": None, "status": None,
    }


def run_status_text(r):
    """記録の状態を短い文にする（診断結果・メイン画面の状態表示・処理ログの最後の結果行で同じ文を使う）。
    config.xml を書き込めなかったときは、その旨を状態の文に足す（保存した枚数はそのまま）。"""
    text = _run_status_core(r)
    if (r.get("config") or {}).get("result") == "failed" and r.get("status") != "not_started":
        text += t("。config.xml を書き込めませんでした", ". config.xml could not be written", ". config.xml yazılamadı")
    return text


def config_error_kind(e):
    """config.xml の書き込みに失敗した理由の区分（例外の生テキストは共有しない）。"""
    if isinstance(e, PermissionError):
        return "permission"
    if getattr(e, "errno", None) == 28 or getattr(e, "winerror", None) in (39, 112):
        return "disk_full"
    if isinstance(e, OSError):
        return "io"
    return "other"


def config_error_reason(kind):
    return {"permission": t("書き込みが許可されていない・ほかのアプリが使用中", "no permission to write, or the file is in use",
                            "yazma izni yok veya dosya kullanımda"),
            "disk_full": t("ディスクの空きが足りない", "not enough disk space", "yeterli disk alanı yok"),
            "io": t("ファイルの書き込みエラー", "file write error", "dosya yazma hatası"),
            }.get(kind, t("不明なエラー", "unknown error", "bilinmeyen hata"))


def config_result_text(r):
    """config.xml の結果（画面の状態表示・処理ログの最後の結果行・共有診断で同じ文）。write_config が実際に行った操作から作る。
    パスや例外の文は含めない（共有診断に出るため）。"""
    c = r.get("config")
    if c is None:
        return t("オフ", "off", "kapalı")
    res = c.get("result")
    if res == "not_run":
        return t("未実行（処理が中断されたため）", "not run (processing was interrupted)",
                 "çalıştırılmadı (işlem yarıda kesildiği için)")
    if res == "failed":
        why = config_error_reason(c.get("error"))
        if c.get("stage") == "backup":
            text = t(f"書き込めませんでした（バックアップを作成できませんでした・{why}）",
                     f"could not be written (a backup could not be created; {why})",
                     f"yazılamadı (yedek oluşturulamadı; {why})")
        else:
            text = t(f"書き込めませんでした（{why}）", f"could not be written ({why})", f"yazılamadı ({why})")
        if c.get("backup"):
            text += t("・バックアップあり", "; backup kept", "; yedek mevcut")
        return text
    if res == "unchanged":
        if c.get("reason") == "all_registered":
            return t("変更なし（すべて登録済み）", "not changed (all already registered)", "değiştirilmedi (tümü zaten kayıtlı)")
        return t("変更なし（書き込むIDなし）", "not changed (no IDs to write)", "değiştirilmedi (yazılacak ID yok)")
    a, n = c.get("added", 0), c.get("total", 0)
    if res == "updated":
        text = t(f"更新（新規 {a} 件・合計 {n} 件）", f"updated ({a} new, {n} total)", f"güncellendi ({a} yeni, toplam {n})")
    elif res == "regenerated":
        text = t(f"再生成（{n} 件）", f"regenerated ({n})", f"yeniden oluşturuldu ({n})")
    elif res == "overwritten":
        text = t(f"上書き（{n} 件）", f"overwritten ({n})", f"üzerine yazıldı ({n})")
    else:
        text = t(f"生成（{n} 件）", f"generated ({n})", f"oluşturuldu ({n})")
    if c.get("backup"):
        text += t("・以前のファイルをバックアップ", ", previous file backed up", ", önceki dosya yedeklendi")
    return text


def run_result_line(r):
    """最後の処理結果の1行：状態・保存/失敗/スキップの枚数・config.xml の結果（オンのとき）。
    処理ログの最後の結果行と、画面の状態表示の先頭で同じ文。共有診断は同じ関数で「状態」「枚数」「config.xml」を出す。"""
    text = run_status_text(r) + "  |  " + run_counts_text(r)
    if r.get("config") is not None:
        text += "  |  config.xml: " + config_result_text(r)
    return text


def _run_status_core(r):
    st = r.get("status")
    if st == "not_started":
        why = not_started_reason(r.get("not_started"))
        if r.get("not_started") in NOT_STARTED_PRECHECK:
            return t(f"開始できず（{why}）", f"Not started ({why})", f"Başlatılamadı ({why})")
        return t(f"処理対象なし（{why}）", f"Nothing to process ({why})", f"İşlenecek görüntü yok ({why})")
    if st == "stopped":
        why = preview_abort_reason(r.get("preview_abort"))
        return t(f"中止（{why}。キャンセル操作ではありません）", f"Stopped ({why}; not cancelled by the user)",
                 f"Durduruldu ({why}; kullanıcı iptal etmedi)")
    # 保存0の状態は、画面の状態表示・処理ログの最後の行・共有診断でこの同じ文を使う（既存のPNGだけのスキップも同じ）
    if st == "all_failed":
        n = r.get("failed", 0)
        return t(f"保存できませんでした。保存 0 枚、{n} 枚すべて失敗", f"Could not save images. 0 saved, all {n} failed",
                 f"Görüntüler kaydedilemedi. 0 kaydedildi, {n} görüntünün tümü başarısız")
    if st == "failed_skipped":
        f, ss = r.get("failed", 0), skip_summary_text(r)
        return t(f"保存できませんでした。保存 0 枚、失敗 {f} 枚、{ss}", f"Could not save images. 0 saved, {f} failed, {ss}",
                 f"Görüntüler kaydedilemedi. 0 kaydedildi, {f} başarısız, {ss}")
    if st == "all_skipped":
        ss = skip_summary_text(r)
        return t(f"すべてスキップしました。保存 0 枚、{ss}", f"All images skipped. 0 saved, {ss}",
                 f"Tüm görüntüler atlandı. 0 kaydedildi, {ss}")
    return {"done": t("完了", "Finished", "Tamamlandı"), "cancelled": t("キャンセル", "Cancelled", "İptal edildi"),
            "error": t("エラーで中断", "Stopped by an error", "Bir hata nedeniyle durdu")}.get(st, "?")


NOT_STARTED_PRECHECK = ("no_input", "same_dir", "no_images")


def skip_parts(r):
    """スキップの区分ごとの枚数 (既存のPNG, ID重複など, プレビューでスキップ)。
    処理側は skipped（既存のPNG・ID重複など。skipped_existing はその内数）と preview_skipped（プレビューで
    スキップ。skipped の内数ではない）を別々に数えるので、3つの区分は重ならない（二重に数えない）。"""
    ex = int(r.get("skipped_existing", 0) or 0)
    other = max(0, int(r.get("skipped", 0) or 0) - ex)
    ps = int(r.get("preview_skipped", 0) or 0)
    return ex, other, ps


def skip_total(r):
    """スキップの合計（既存のPNG ＋ ID重複など ＋ プレビューでスキップ）。枚数欄・状態の文・ダイアログで同じ値。"""
    return sum(skip_parts(r))


def _skip_part_labels(r, unit):
    """スキップの内訳（0 の区分は出さない）。unit=" 枚" なら「既存PNG 1 枚・ID重複など 1 枚」、"" なら枚数欄用。"""
    ex, other, ps = skip_parts(r)
    parts = []
    if ex:
        parts.append(t(f"既存PNG {ex}{unit}", f"existing PNG {ex}", f"mevcut PNG {ex}"))
    if other:
        parts.append(t(f"ID重複など {other}{unit}", f"duplicate IDs etc. {other}", f"yinelenen ID vb. {other}"))
    if ps:
        parts.append(t(f"プレビュー {ps}{unit}", f"preview {ps}", f"önizleme {ps}"))
    return t("・", ", ", ", ").join(parts)


def skip_summary_text(r):
    """スキップの合計と内訳。例「スキップ 2 枚（既存PNG 1 枚・ID重複など 1 枚）」"""
    sk = skip_total(r)
    bd = _skip_part_labels(r, " 枚")
    if not bd:
        return t(f"スキップ {sk} 枚", f"{sk} skipped", f"{sk} atlandı")
    return t(f"スキップ {sk} 枚（{bd}）", f"{sk} skipped ({bd})", f"{sk} atlandı ({bd})")


def existing_only_skip(r):
    """スキップの全件が既存のPNGによる（ID重複など・プレビューでのスキップが1枚も無い）ときだけ True。"""
    ex, other, ps = skip_parts(r)
    return ex > 0 and other == 0 and ps == 0


def zero_saved_status(r):
    """保存0で終わったときの状態（画面・処理ログの最後の行・共有診断で共通）。該当しなければ None。
      all_failed     失敗だけ（スキップなし）→ 警告「保存できませんでした」
      failed_skipped 失敗とスキップが混在   → 警告「保存できませんでした」
      all_skipped    スキップだけ（失敗なし）→ 情報「すべてスキップしました」
    保存が1枚でもある・プレビューでキャンセルした・何も処理していない場合は None。"""
    if r.get("saved", 0) or r.get("preview_cancelled"):
        return None
    f, sk = int(r.get("failed", 0) or 0), skip_total(r)
    if f and sk:
        return "failed_skipped"
    if f:
        return "all_failed"
    if sk:
        return "all_skipped"
    return None


def run_counts_text(r):
    """枚数欄：保存・失敗・スキップの枚数。スキップは合計（既存のPNG ＋ ID重複など ＋ プレビューでスキップ）で、
    内訳を「うち」で書く。プレビューでのキャンセルと中止（表示失敗・無操作・応答なしなど）は、それぞれ別の項目。
    画面の状態表示・ダイアログ・共有診断で同じ文を使う。"""
    sk = skip_total(r)
    bd = _skip_part_labels(r, "")
    bd_ja = f"（うち{bd}）" if bd else ""
    bd_en = f" ({bd})" if bd else ""
    text = t(f"保存 {r['saved']} / 失敗 {r['failed']} / スキップ {sk}{bd_ja}",
             f"saved {r['saved']} / failed {r['failed']} / skipped {sk}{bd_en}",
             f"kaydedilen {r['saved']} / başarısız {r['failed']} / atlanan {sk}{bd_en}")
    pc, pa = r.get("preview_cancelled", 0), r.get("preview_aborted", 0)
    if pc:
        text += t(f" / プレビューでキャンセル {pc}", f" / cancelled in the preview {pc}", f" / önizlemede iptal {pc}")
    if pa:
        text += t(f" / プレビューで中止 {pa}（保存せず）", f" / stopped in the preview {pa} (not saved)",
                  f" / önizlemede durdurulan {pa} (kaydedilmedi)")
    return text


def rembg_actual_text(r):
    """背景透過の実績：実際に処理した処理先・精度・枚数だけ（設定値は出さない）。"""
    g, c = r.get("gpu_images", 0), r.get("cpu_images", 0)
    parts = []
    if g:
        p = "FP16" if r.get("fp16") else "FP32"
        parts.append(t(f"GPU・{p} {g} 枚", f"GPU {p}: {g}", f"GPU {p}: {g}"))
    if c:
        parts.append(t(f"CPU・FP32 {c} 枚", f"CPU FP32: {c}", f"CPU FP32: {c}"))
    if not parts:
        return t("なし（今回は背景透過で処理した画像はありません）", "none (no image was processed by background removal this time)",
                 "yok (bu sefer arka plan kaldırmayla işlenen görüntü yok)")
    return " / ".join(parts)


def cpu_reason_text(r):
    """GPUを選んだ実行で、CPUで処理した理由（定型文。エラーの文やパスは出さない）。
    CPUで処理していなければ「なし」。背景透過を実行していなければその旨を添える。"""
    g, c = r.get("gpu_images", 0), r.get("cpu_images", 0)
    f = r.get("fallback") or {}
    code = f.get("code") or ""
    code_txt = t(f"、エラーコード {code}", f", error code {code}", f", hata kodu {code}") if code else ""
    why = None
    if r.get("fp16_fallback"):
        fb = fp16_fallback_reason(r["fp16_fallback"])
        if r["fp16_fallback"] == "load":
            why = t(f"{fb}ため、CPU・FP32に切り替え（GPU・FP32は不使用{code_txt}）",
                    f"{fb}, so it switched to CPU FP32 (GPU FP32 not used{code_txt})",
                    f"{fb}, bu yüzden CPU FP32'ye geçildi (GPU FP32 kullanılmadı{code_txt})")
        else:
            why = t(f"{fb}ため、開始時からCPU・FP32（GPU・FP32は不使用）",
                    f"{fb}, so CPU FP32 was used from the start (GPU FP32 not used)",
                    f"{fb}, bu yüzden başlangıçtan itibaren CPU FP32 kullanıldı (GPU FP32 kullanılmadı)")
    elif r.get("cpu_reason") == "dml_unavailable":
        why = t("GPU（DirectML）を利用できないため、開始時からCPU・FP32",
                "GPU (DirectML) is not available, so CPU FP32 was used from the start",
                "GPU (DirectML) kullanılamadığı için başlangıçtan itibaren CPU FP32 kullanıldı")
    elif r.get("cpu_reason") == "model_unsupported":
        m = r.get("model", "")
        why = t(f"{m} はGPU（DirectML）に対応していないため、CPU・FP32",
                f"{m} is not supported on GPU (DirectML), so CPU FP32 was used",
                f"{m} GPU'da (DirectML) desteklenmediği için CPU FP32 kullanıldı")
    elif f.get("stage") == "init":
        why = t(f"GPU（DirectML）の初期化に失敗したため、CPU・FP32に切り替え{code_txt}",
                f"GPU (DirectML) initialization failed, so it switched to CPU FP32{code_txt}",
                f"GPU (DirectML) başlatma başarısız olduğu için CPU FP32'ye geçildi{code_txt}")
    elif f.get("stage") == "run":
        why = t(f"GPU（DirectML）の推論に失敗したため、途中からCPU・FP32{code_txt}",
                f"GPU (DirectML) inference failed, so it switched to CPU FP32 partway{code_txt}",
                f"GPU (DirectML) çıkarımı başarısız olduğu için işlemin ortasında CPU FP32'ye geçildi{code_txt}")
    if why is None:
        if g + c == 0:
            return t("なし（今回は背景透過で処理した画像はありません）", "none (no image was processed by background removal this time)",
                     "yok (bu sefer arka plan kaldırmayla işlenen görüntü yok)")
        return t("なし（CPUでは処理していません）", "none (nothing was processed on CPU)", "yok (CPU'da işlenen görüntü yok)")
    if g + c == 0:
        why += t("（今回は背景透過で処理した画像が無いため、CPUで処理した画像は 0 枚）",
                 " (no image was processed by background removal this time, so 0 images on CPU)",
                 " (bu sefer arka plan kaldırmayla işlenen görüntü olmadığı için CPU'da 0 görüntü)")
    return why


def format_test_results(records, include_pc=False):
    """記録（GPUテスト・CPUテスト）から共有用テキストを作る。表示言語に合わせる。
    共有用なので、全GPUの一覧・実行日時・OSのビルド番号は出さない。CPUの型番・論理スレッド数・搭載RAMは
    include_pc=True（「PCの詳細も含める」）のときだけ出す。GPUは実際に使ったものだけを各テストに出す。"""
    si = _system_info()
    L = []
    L.append(t("=== FM Face Processor テスト結果 ===", "=== FM Face Processor: test results ===",
               "=== FM Face Processor: test sonuçları ==="))
    L.append(t("ビルド", "Build", "Sürüm") + f": FM Face Processor {APP_VERSION} ({APP_BUILD_NOTE})")
    if si.get("os"):
        L.append(f"OS: {si['os']}")
    if include_pc:
        cpu = si.get("cpu") or t("不明", "unknown", "bilinmiyor")
        L.append(f"CPU: {cpu}" + (t(f"（論理 {si['threads']} スレッド）", f" ({si['threads']} logical threads)",
                                     f" ({si['threads']} mantıksal iş parçacığı)") if si.get("threads") else ""))
        ram = f"{si['ram_gb']:.1f} GB" if si.get("ram_gb") else t("不明", "unknown", "bilinmiyor")
        L.append(t("搭載RAM", "Installed RAM", "Kurulu RAM") + f": {ram}")
    if si.get("ort"):
        L.append(f"onnxruntime: {si['ort']}")
    for r in records:
        L.append("")
        head = t("GPUテスト", "GPU test", "GPU testi") if r["kind"] == "gpu" else t("CPUテスト", "CPU test", "CPU testi")
        if r.get("_prev"):
            head += t("（直前に開始できた実行）", " (previous run that started)", " (başlayan önceki çalıştırma)")
        L.append(f"--- {head} ---")
        L.append(t("状態", "Status", "Durum") + f": {run_status_text(r)}")
        if r.get("status") == "not_started":
            L.append(t("処理した画像: なし（画像の処理を始める前に止まりました）",
                       "Images processed: none (stopped before processing any image)",
                       "İşlenen görüntü: yok (herhangi bir görüntü işlenmeden durdu)"))
            continue
        if r.get("removebg") and r["bg_removal"]:
            dev_head = t("ローカル背景透過の処理先（選択）", "Local background removal device (selected)",
                         "Yerel arka plan kaldırma işlem birimi (seçilen)")
        else:
            dev_head = t("処理先の設定（選択）", "Device setting (selected)", "İşlem birimi ayarı (seçilen)")
        sel = "GPU (DirectML)" if r["kind"] == "gpu" else "CPU"
        if r["kind"] == "gpu" and r.get("fp16_selected"):
            sel += t("・GPU省メモリ（FP16）: オン", ", GPU memory saver (FP16): on", ", GPU bellek tasarrufu (FP16): açık")
        L.append(dev_head + ": " + sel)
        if r["kind"] == "gpu":
            used = r["gpu_name"] or t("特定できず", "not identified", "belirlenemedi")
            if r["gpu_name"] and r["gpu_vram_gb"]:
                used += t(f"（専用VRAM {r['gpu_vram_gb']:.1f} GB）", f" (dedicated VRAM {r['gpu_vram_gb']:.1f} GB)",
                          f" (ayrılmış VRAM {r['gpu_vram_gb']:.1f} GB)")
            if not r["gpu_images"]:
                used = t("なし（GPUで処理した画像なし）", "none (no image processed on GPU)", "yok (GPU'da işlenen görüntü yok)")
            L.append(t("使用したGPU", "GPU used", "Kullanılan GPU") + f": {used}")
        if r["bg_removal"]:
            if r.get("removebg"):
                rb_local = r["removebg_incomplete"] + r["removebg_error"]
                L.append(t(f"remove.bg: 優先（そのまま使用 {r['removebg_ok']} 枚 / ローカルで再処理 {rb_local} 枚"
                           f"：切り抜きが不完全 {r['removebg_incomplete']}・エラー {r['removebg_error']}）",
                           f"remove.bg: preferred (used as is {r['removebg_ok']} / re-processed locally {rb_local}:"
                           f" incomplete cutout {r['removebg_incomplete']}, error {r['removebg_error']})",
                           f"remove.bg: öncelikli (olduğu gibi kullanılan {r['removebg_ok']} / yerelde yeniden işlenen {rb_local}:"
                           f" eksik kesim {r['removebg_incomplete']}, hata {r['removebg_error']})"))
            else:
                L.append(t("remove.bg: 使用しない", "remove.bg: not used", "remove.bg: kullanılmadı"))
            model_txt = r["model"]
            used = r.get("models") or {}
            if used and set(used) != {r["model"]}:
                actual = " / ".join(f"{m} {n}" for m, n in sorted(used.items(), key=lambda kv: -kv[1]))
                model_txt += t(f"（実際: {actual} 枚）", f" (actual: {actual})", f" (gerçek: {actual})")
            L.append(t("背景モデル（ローカル）", "Background model (local)", "Arka plan modeli (yerel)") + f": {model_txt}")
            # 実績：実際に処理した処理先・精度・枚数だけ（GPUを選んでいても全件CPUなら「CPU・FP32」）
            if r.get("removebg"):
                act_head = t("ローカル背景透過の実績（処理先・精度・枚数）", "Local background removal, actual (device, precision, images)",
                             "Yerel arka plan kaldırma, gerçek (birim, hassasiyet, görüntü)")
            else:
                act_head = t("背景透過の実績（処理先・精度・枚数）", "Background removal, actual (device, precision, images)",
                             "Arka plan kaldırma, gerçek (birim, hassasiyet, görüntü)")
            L.append(act_head + ": " + rembg_actual_text(r))
            if r["kind"] == "gpu":
                L.append(t("CPUで処理した理由", "Reason for CPU processing", "CPU'da işleme nedeni") + ": " + cpu_reason_text(r))
        else:
            L.append(t("背景透過: オフ", "Background removal: off", "Arka plan kaldırma: kapalı"))
        L.append(t("並列処理数（実際）", "Parallel workers (actual)", "Paralel işlem sayısı (gerçek)") + f": {r['workers']}")
        local_n = r["gpu_images"] + r["cpu_images"]
        if r["matting"] and r["bg_removal"] and local_n == 0:
            matting = t("オン（今回は未使用：ローカル背景透過 0 枚）", "on (not used: 0 images by local background removal)",
                        "açık (kullanılmadı: yerel arka plan kaldırma 0 görüntü)")
        elif r["matting"] and r["bg_removal"]:
            matting = t(f"オン（ローカル背景透過 {local_n} 枚に適用）", f"on (applied to {local_n} image(s) by local background removal)",
                        f"açık (yerel arka plan kaldırmadaki {local_n} görüntüye uygulandı)")
        elif r["matting"]:
            matting = t("オン（背景透過オフのため未使用）", "on (not used: background removal is off)",
                        "açık (kullanılmadı: arka plan kaldırma kapalı)")
        else:
            matting = t("オフ", "off", "kapalı")
        L.append(t("髪のフチ補正", "Hair edge smoothing", "Saç kenarı yumuşatma") + ": " + matting)
        L.append(output_size_line(r))
        sizes = [((w, h), n) for w, h, n in (r.get("sizes") or [])]
        L.append(t("保存したPNGの実寸（サイズ別の枚数）", "Saved PNG actual size (count per size)",
                   "Kaydedilen PNG gerçek boyutu (boyut başına sayı)") + ": " + format_size_counts(sizes))
        el = f"{r['elapsed']:.1f}" if isinstance(r["elapsed"], (int, float)) else "?"
        L.append(t(f"処理時間（実行〜完了）: {el} 秒", f"Time (Run to finish): {el} s", f"Süre (Başlat'tan bitişe): {el} sn"))
        L.append(t("枚数", "Images", "Görüntüler") + ": " + run_counts_text(r))
        L.append("config.xml: " + config_result_text(r))
        if r.get("failed"):
            L.append(t("失敗の理由", "Failure reasons", "Başarısızlık nedenleri") + ": " + format_fail_kinds(r.get("fail_kinds")))
        if r.get("preview"):
            pa = r.get("preview_aborted", 0)
            pa_ja = f" / 中止 {pa} 枚" if pa else ""
            pa_en = f" / stopped {pa}" if pa else ""
            pa_tr = f" / durdurulan {pa}" if pa else ""
            L.append(t(f"保存前プレビュー: オン（スキップ {r['preview_skipped']} 枚 / キャンセル {r['preview_cancelled']} 枚{pa_ja}）",
                       f"Preview before saving: on (skipped {r['preview_skipped']} / cancelled {r['preview_cancelled']}{pa_en})",
                       f"Kaydetmeden önce önizleme: açık (atlanan {r['preview_skipped']} / iptal edilen {r['preview_cancelled']}{pa_tr})"))
        else:
            L.append(t("保存前プレビュー: オフ", "Preview before saving: off", "Kaydetmeden önce önizleme: kapalı"))
        if r.get("bg_already_cutout"):
            L.append(t(f"背景透過を行わなかった枚数（入力が透過済み）: {r['bg_already_cutout']}",
                       f"Background removal skipped (input already transparent): {r['bg_already_cutout']}",
                       f"Arka plan kaldırma atlandı (girdi zaten şeffaf): {r['bg_already_cutout']}"))
        label = t("GPUメモリのピーク（アプリ単体）", "GPU memory peak (this app)", "GPU bellek zirvesi (bu uygulama)")
        gm = r["gpu_mem"]
        if gm == "na":
            val = t("対象外（CPU処理）", "not applicable (CPU)", "uygulanamaz (CPU)")
        elif not gm and r["kind"] == "gpu" and not r["gpu_images"]:
            val = t("対象外（GPUで処理した画像なし）", "not applicable (no image processed on GPU)",
                    "uygulanamaz (GPU'da işlenen görüntü yok)")
        elif not gm:
            val = t("未計測", "not measured", "ölçülmedi")
        else:
            val = t(f"専用 {gm['dedicated_gb']:.2f} GB / 共有 {gm['shared_gb']:.2f} GB",
                    f"dedicated {gm['dedicated_gb']:.2f} GB / shared {gm['shared_gb']:.2f} GB",
                    f"ayrılmış {gm['dedicated_gb']:.2f} GB / paylaşılan {gm['shared_gb']:.2f} GB")
        L.append(f"{label}: {val}")
    return "\n".join(L) + "\n"


def scrub_share_text(text, secrets=()):
    """共有本文の念のための後処理：ユーザー名・ホームフォルダ・指定した文字列（APIキー・入出力パス）を伏せる。
    本文は専用の項目だけから作るので通常は何も変わらない（GPU名などに混ざった場合の備え）。"""
    items = [s for s in secrets if s and len(str(s)) >= 4]          # 空・短い値は対象外（本文を壊さない）
    try:
        items.append(str(Path.home()))
    except Exception:  # noqa: BLE001
        pass
    user = os.environ.get("USERNAME") or ""
    for s in sorted({str(s) for s in items}, key=len, reverse=True):
        text = re.sub(re.escape(s), "<hidden>", text, flags=re.IGNORECASE)
    if len(user) >= 4:
        text = re.sub(r"(?<![0-9A-Za-z])" + re.escape(user) + r"(?![0-9A-Za-z])", "<hidden>", text,
                      flags=re.IGNORECASE)
    return text


def release_dml_sessions():
    """DirectML（GPU）のセッションと、その参照をすべて解放してGPUメモリを返す。解放した数を返す。
    セッションを次の実行まで持ち越すと、処理が終わってもGPUの専用・共有メモリを抱えたままになり、
    VRAMの少ないGPUではPC全体が重くなるため、バッチの終わりに毎回呼ぶ。"""
    import gc
    with _rembg_lock:
        keys = [k for k in _rembg_sessions if len(k) > 1 and k[1] == REMBG_DEVICE_DML]
        dead = [_rembg_sessions.pop(k) for k in keys]
    for sess in dead:
        try:
            inner = sess.inner_session
            if isinstance(inner, _SerializedSession):
                inner._inner = None          # onnxruntime の InferenceSession への参照を切る
            sess.inner_session = None
        except Exception:  # noqa: BLE001
            pass
    count = len(dead)
    del dead
    gc.collect()
    return count


def begin_rembg_batch(device, model_name, log=None, fp16=False, removebg=False):
    """「実行」1回の最初に呼ぶ。実際に使う背景透過の処理先を決めてログに出す。
    画像は必ず1枚ずつ処理する（各画像をプレビューで確認する）ので、並列数は扱わない。
    removebg=True（remove.bg を優先）のときは、CPU／GPU はローカル背景透過の処理先として
    remove.bg が使えなかった画像と、切り抜きが不完全だった画像の再処理にだけ使う。"""
    device = device if device in REMBG_DEVICES else REMBG_DEVICE_CPU
    if removebg:
        dev_ja, dev_en, dev_tr = ("ローカル背景透過の処理先", "Local background removal device",
                                  "Yerel arka plan kaldırma işlem birimi")
    else:
        dev_ja, dev_en, dev_tr = ("背景透過の処理先", "Background removal device", "Arka plan kaldırma işlem birimi")
    if removebg and log:
        log(t("[i] remove.bg を優先します。下の処理先（CPU／GPU）はローカル背景透過の処理先で、"
              "remove.bg が使えなかった画像と、切り抜きが不完全だった画像の再処理に使います",
              "[i] remove.bg comes first. The device below (CPU/GPU) is for local background removal, used only for "
              "images remove.bg could not process and for re-processing incomplete remove.bg cutouts",
              "[i] Önce remove.bg kullanılır. Aşağıdaki işlem birimi (CPU/GPU) yerel arka plan kaldırma içindir; yalnızca "
              "remove.bg'nin işleyemediği görüntülerde ve eksik kesimlerin yeniden işlenmesinde kullanılır"))
    use_dml = False
    cpu_reason = None
    if device == REMBG_DEVICE_DML:
        if model_name not in DML_MODELS:
            cpu_reason = "model_unsupported"
            if log:
                names = " / ".join(DML_MODELS)
                log(t(f"[i] {dev_ja}: GPU（DirectML）は {names} のみ対応のため、{model_name} はCPUで処理します",
                      f"[i] {dev_en}: GPU (DirectML) supports only {names}; {model_name} runs on CPU",
                      f"[i] {dev_tr}: GPU (DirectML) yalnızca {names} modellerini destekler; {model_name} CPU'da çalışacak"))
        else:
            reason = dml_unavailable_reason()
            if reason:
                cpu_reason = "dml_unavailable"
                if log:
                    log(t(f"[!] GPU（DirectML）を使えません（{reason}）。CPUで処理します",
                          f"[!] GPU (DirectML) is unavailable ({reason}). Using CPU",
                          f"[!] GPU (DirectML) kullanılamıyor ({reason}). CPU kullanılıyor"))
            else:
                use_dml = True
    use_fp16 = False
    fp16_requested = bool(fp16 and use_dml and model_name in DML_FP16_MODELS)
    fp16_fallback = None
    if fp16:
        if use_dml and model_name in DML_FP16_MODELS:
            path, why = _dml_fp16_model_path(model_name)
            if path:
                use_fp16 = True
            else:
                # FP16を選んだのにモデルを使えない: GPU・FP32（専用VRAMをFP16の約2倍使う）には切り替えず、
                # 同じモデルのCPU・FP32で処理する
                use_dml = False
                fp16_fallback = _dml_fp16_kind.get(model_name) or "missing"
                cpu_reason = "fp16"
                if log:
                    log(t(f"[!] GPU省メモリ（FP16）のモデルを使えません（{why}）。GPU・FP32は使わず、"
                          f"{model_name} をCPU・FP32で処理します",
                          f"[!] GPU memory saver (FP16) model unavailable ({why}). GPU FP32 is not used; "
                          f"{model_name} runs on CPU (FP32)",
                          f"[!] GPU bellek tasarrufu (FP16) modeli kullanılamıyor ({why}). GPU FP32 kullanılmıyor; "
                          f"{model_name} CPU'da (FP32) çalışacak"))
        elif log and device == REMBG_DEVICE_DML and model_name in DML_FP16_MODELS:
            log(t("[i] GPU（DirectML）を使えないため、GPU省メモリ（FP16）は使わず、CPU・FP32で処理します",
                  "[i] GPU (DirectML) is unavailable, so the GPU memory saver (FP16) is not used; processing on CPU (FP32)",
                  "[i] GPU (DirectML) kullanılamadığı için GPU bellek tasarrufu (FP16) kullanılmıyor; CPU'da (FP32) işleniyor"))
        elif log:
            log(t("[i] GPU省メモリ（FP16）は、GPU（DirectML）で birefnet-general-lite を使うときだけ有効です。今回はFP32で処理します",
                  "[i] GPU memory saver (FP16) only applies to birefnet-general-lite on GPU (DirectML). Using FP32 this time",
                  "[i] GPU bellek tasarrufu (FP16) yalnızca GPU (DirectML) üzerinde birefnet-general-lite için geçerlidir. Bu sefer FP32 kullanılıyor"))
    with _rembg_batch_lock:
        _rembg_batch.update(device=REMBG_DEVICE_DML if use_dml else REMBG_DEVICE_CPU,
                            dml_failed=False, counts={}, models={}, announced=set(), dml_fail=None,
                            dml_name=None, dml_luid=None, fp16=use_fp16, removebg=bool(removebg),
                            fp16_requested=fp16_requested, fp16_fallback=fp16_fallback,
                            cpu_reason=cpu_reason)
    if use_dml:
        if log:
            log(t(f"[i] {dev_ja}: GPU（DirectML） — {model_name}",
                  f"[i] {dev_en}: GPU (DirectML) — {model_name}",
                  f"[i] {dev_tr}: GPU (DirectML) — {model_name}"))
            if use_fp16:
                log(t("[i] GPU省メモリ（FP16）: 有効 — 1024×1024のFP16モデルでGPU処理します（CPUに切り替える場合はFP32）",
                      "[i] GPU memory saver (FP16): on — GPU uses the 1024×1024 FP16 model (FP32 if it switches to CPU)",
                      "[i] GPU bellek tasarrufu (FP16): açık — GPU 1024×1024 FP16 modeli kullanır (CPU'ya geçilirse FP32)"))
        return
    if fp16_fallback:
        if log:
            why_txt = fp16_fallback_reason(fp16_fallback)
            log(t(f"[i] {dev_ja}: CPU・FP32 — {model_name}（{why_txt}ため、GPUから切り替え）",
                  f"[i] {dev_en}: CPU (FP32) — {model_name} (switched from GPU: {why_txt})",
                  f"[i] {dev_tr}: CPU (FP32) — {model_name} (GPU'dan geçildi: {why_txt})"))
            try:
                u2 = Path(os.getenv("U2NET_HOME", os.path.join(os.path.expanduser("~"), ".u2net")))
                if not (u2 / f"{model_name}.onnx").is_file():
                    log(t(f"[!] CPU用の {model_name}（FP32）のモデルがまだありません。最初の画像でダウンロードします（インターネット接続が必要です）",
                          f"[!] The CPU (FP32) model for {model_name} is not downloaded yet. It will be downloaded for the first image (internet connection required)",
                          f"[!] {model_name} için CPU (FP32) modeli henüz indirilmedi. İlk görüntüde indirilecek (internet bağlantısı gerekir)"))
            except Exception:  # noqa: BLE001
                pass
        return
    if log and device == REMBG_DEVICE_CPU:
        log(t(f"[i] {dev_ja}: CPU", f"[i] {dev_en}: CPU", f"[i] {dev_tr}: CPU"))


def log_rembg_batch_summary(log):
    """このバッチで背景透過を実際にどこで実行したかをまとめてログに出す。"""
    with _rembg_batch_lock:
        counts = dict(_rembg_batch["counts"])
        fail = _rembg_batch["dml_fail"]
        name = _rembg_batch["dml_name"]
        precision = "fp16" if _rembg_batch["fp16"] else "fp32"
        removebg = bool(_rembg_batch.get("removebg"))
        fp16_fb = _rembg_batch.get("fp16_fallback")
    with _batch_stats_lock:
        rb_ok = _batch_stats.get("removebg_ok", 0)
        rb_inc = _batch_stats.get("removebg_incomplete", 0)
        rb_err = _batch_stats.get("removebg_error", 0)
    if log and (removebg or rb_ok or rb_inc or rb_err):
        log(t(f"[i] remove.bg（このバッチ）: そのまま使用 {rb_ok} 枚 / ローカルで再処理 {rb_inc + rb_err} 枚"
              f"（切り抜きが不完全 {rb_inc}・remove.bgのエラー {rb_err}）",
              f"[i] remove.bg (this batch): used as is {rb_ok} / re-processed locally {rb_inc + rb_err}"
              f" (incomplete cutout {rb_inc}, remove.bg error {rb_err})",
              f"[i] remove.bg (bu toplu işlem): olduğu gibi kullanılan {rb_ok} / yerelde yeniden işlenen {rb_inc + rb_err}"
              f" (eksik kesim {rb_inc}, remove.bg hatası {rb_err})"))
    if not counts or not log:
        return
    if removebg:
        head_ja = "ローカル背景透過の実行先（このバッチ）"
        head_en = "Local background removal ran on (this batch)"
        head_tr = "Yerel arka plan kaldırmanın çalıştığı birim (bu toplu işlem)"
    else:
        head_ja = "背景透過の実行先（このバッチ）"
        head_en = "Background removal ran on (this batch)"
        head_tr = "Arka plan kaldırmanın çalıştığı birim (bu toplu işlem)"
    gpu = counts.get(REMBG_DEVICE_DML, 0)
    cpu = counts.get(REMBG_DEVICE_CPU, 0)
    label = _device_label(REMBG_DEVICE_DML, name, precision)
    cpu_label = _device_label(REMBG_DEVICE_CPU)
    if fail:
        stage, err = fail
        err = err if len(err) <= 200 else err[:200] + "…"
        stage_ja, stage_en, stage_tr = {"init": ("初期化", "initialization", "başlatma"),
                                        "run": ("推論", "inference", "çıkarım")}[stage]
        log(t(f"[i] {head_ja}: {label} {gpu} 枚 / {cpu_label} {cpu} 枚"
              f"（途中でCPUに切り替え。理由: GPUの{stage_ja}に失敗 — {err}）",
              f"[i] {head_en}: {label} {gpu} / {cpu_label} {cpu} images"
              f" (switched to CPU; reason: GPU {stage_en} failed — {err})",
              f"[i] {head_tr}: {label} {gpu} / {cpu_label} {cpu} görüntü"
              f" (CPU'ya geçildi; neden: GPU {stage_tr} başarısız — {err})"))
    elif gpu == 0 and fp16_fb:
        why_txt = fp16_fallback_reason(fp16_fb)
        log(t(f"[i] {head_ja}: {cpu_label} {cpu} 枚（{why_txt}ため、GPU・FP32は使わずCPUに切り替え）",
              f"[i] {head_en}: {cpu_label} {cpu} images (switched to CPU without using GPU FP32: {why_txt})",
              f"[i] {head_tr}: {cpu_label} {cpu} görüntü (GPU FP32 kullanılmadan CPU'ya geçildi: {why_txt})"))
    elif gpu == 0:
        # CPUだけで処理したバッチでは、使っていないGPUの行を出さない
        log(t(f"[i] {head_ja}: {cpu_label} {cpu} 枚",
              f"[i] {head_en}: {cpu_label} {cpu} images",
              f"[i] {head_tr}: {cpu_label} {cpu} görüntü"))
    else:
        log(t(f"[i] {head_ja}: {label} {gpu} 枚 / {cpu_label} {cpu} 枚",
              f"[i] {head_en}: {label} {gpu} / {cpu_label} {cpu} images",
              f"[i] {head_tr}: {label} {gpu} / {cpu_label} {cpu} görüntü"))


def _rembg_effective_device(model_name):
    if getattr(_prefetch_ctx, "ops", None) is not None:
        # 先読みはローカルCPU処理の実行でだけ動く。実行の状態（_rembg_batch）は読まないので、
        # 遅れて動いていても、あとから始まった実行のGPU設定を拾うことはない
        return REMBG_DEVICE_CPU
    with _rembg_batch_lock:
        if (_rembg_batch["device"] == REMBG_DEVICE_DML and model_name in DML_MODELS
                and not _rembg_batch["dml_failed"]):
            return REMBG_DEVICE_DML
    return REMBG_DEVICE_CPU


def _get_rembg_session(model_name, device, precision="fp32"):
    key = (model_name, device) if precision == "fp32" else (model_name, device, precision)
    with _rembg_lock:
        sess = _rembg_sessions.get(key)
        if sess is None:
            if device == REMBG_DEVICE_DML:
                sess = _new_rembg_session_dml(model_name, precision)
            else:
                sess = _new_rembg_session(model_name)
            _rembg_sessions[key] = sess
    return sess


def _on_dml_failure(model_name, stage, err, log, fp16_load=False):
    """GPUの初期化／推論に失敗：理由を記録し、このバッチの残りは同じモデルのCPU推論にする。
    fp16_load=True は、GPU省メモリ（FP16）のモデルでセッションを作れなかった場合（GPU・FP32は試さない）。"""
    import gc
    with _rembg_batch_lock:
        _rembg_batch["dml_failed"] = True
        _rembg_batch["dml_fail"] = (stage, err)
        if fp16_load:
            _rembg_batch["fp16_fallback"] = "load"
        done_on_gpu = _rembg_batch["counts"].get(REMBG_DEVICE_DML, 0)
        name = _rembg_batch["dml_name"]
        precision = "fp16" if _rembg_batch["fp16"] else "fp32"
    with _rembg_lock:
        dead = [_rembg_sessions.pop(k) for k in list(_rembg_sessions)
                if k[0] == model_name and k[1] == REMBG_DEVICE_DML]
    del dead
    gc.collect()   # GPUメモリを手放す
    if log:
        label = _device_label(REMBG_DEVICE_DML, name, precision)
        stage_ja, stage_en, stage_tr = {"init": ("初期化", "initialization", "başlatma"),
                                        "run": ("推論", "inference", "çıkarım")}[stage]
        log(t(f"  [!] {label} の{stage_ja}に失敗しました: {err}",
              f"  [!] {label} {stage_en} failed: {err}",
              f"  [!] {label} {stage_tr} başarısız oldu: {err}"))
        if fp16_load:
            log(t("  [i] GPU省メモリ（FP16）のモデルを読み込めないため、GPU・FP32は使わずCPU・FP32に切り替えます",
                  "  [i] The GPU memory saver (FP16) model could not be loaded, so GPU FP32 is not used; switching to CPU (FP32)",
                  "  [i] GPU bellek tasarrufu (FP16) modeli yüklenemediği için GPU FP32 kullanılmıyor; CPU'ya (FP32) geçiliyor"))
        log(t(f"  [i] この画像から {model_name} のCPU推論（FP32）で続行します（ここまでGPUで {done_on_gpu} 枚。このバッチではGPUに戻しません）",
              f"  [i] Continuing with {model_name} on CPU (FP32) from this image ({done_on_gpu} image(s) done on GPU so far; GPU will not be retried in this batch)",
              f"  [i] Bu görüntüden itibaren {model_name} CPU'da (FP32) devam ediyor (şimdiye kadar GPU'da {done_on_gpu} görüntü; bu toplu işlemde GPU yeniden denenmeyecek)"))


def _note_rembg_use(model_name, device, sess, log):
    ops = getattr(_prefetch_ctx, "ops", None)
    if ops is not None:
        # 先読み中: 実行先の実績（枚数・最初の1回のログ）は、その画像に進んだ時点で記録する
        ops.append(("rembg", model_name, device, sess))
        return
    name = None
    precision = getattr(sess, "_fmfp_precision", "fp32") if device == REMBG_DEVICE_DML else "fp32"
    if device == REMBG_DEVICE_DML:
        # 最初の推論が終わった時点で、実際にGPUメモリが増えたアダプターを調べる（以後は記録を使う）
        name = _dml_session_adapter(sess)
    with _rembg_batch_lock:
        counts = _rembg_batch["counts"]
        counts[device] = counts.get(device, 0) + 1
        models = _rembg_batch.setdefault("models", {})
        models[model_name] = models.get(model_name, 0) + 1
        first = (model_name, device, precision) not in _rembg_batch["announced"]
        _rembg_batch["announced"].add((model_name, device, precision))
        if device == REMBG_DEVICE_DML:
            _rembg_batch["dml_name"] = name
            _rembg_batch["dml_luid"] = getattr(sess, "_fmfp_gpu_luid", None)
    if first and log:
        try:
            providers = ", ".join(sess.inner_session.get_providers())
        except Exception:  # noqa: BLE001
            providers = "?"
        label = _device_label(device, name, precision)
        log(t(f"  [i] 背景透過: {model_name} を {label} で実行中（providers: {providers}）",
              f"  [i] Background removal: running {model_name} on {label} (providers: {providers})",
              f"  [i] Arka plan kaldırma: {model_name}, {label} üzerinde çalışıyor (sağlayıcılar: {providers})"))


def _remove_background_once(img, model_name, alpha_matting=False, max_side=1600, log=None):
    from PIL import Image
    from rembg import remove
    device = _rembg_effective_device(model_name)
    sess = None
    if device == REMBG_DEVICE_DML:
        with _rembg_batch_lock:
            precision = "fp16" if _rembg_batch["fp16"] and model_name in DML_FP16_MODELS else "fp32"
        try:
            sess = _get_rembg_session(model_name, REMBG_DEVICE_DML, precision)
        except Exception as e:  # noqa: BLE001
            err_text = _ort_error_text(e)
            # FP16のモデルを読み込めないときだけ「FP16モデルを読み込めない」とする。GPU側の初期化失敗は従来どおり
            # 「GPUの初期化に失敗」（どちらも GPU・FP32 は試さず CPU・FP32 にする）
            _on_dml_failure(model_name, "init", err_text, log,
                            fp16_load=(precision == "fp16" and _is_model_load_error(e, err_text)))
            device = REMBG_DEVICE_CPU
    if sess is None:
        sess = _get_rembg_session(model_name, REMBG_DEVICE_CPU)
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
    try:
        out = remove(small, session=sess, **kw)
    except _DmlRunError as e:
        _on_dml_failure(model_name, "run", str(e), log)
        device = REMBG_DEVICE_CPU
        sess = _get_rembg_session(model_name, REMBG_DEVICE_CPU)
        out = remove(small, session=sess, **kw)
    _note_rembg_use(model_name, device, sess, log)
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
                      "  [i] High-accuracy face detection unavailable (old OpenCV). Using basic detection.", '  [i] Yüksek doğruluklu yüz algılama kullanılamıyor (eski OpenCV). Temel algılama kullanılıyor.'))
                return None
            path = _find_yunet_model()
            if not path:
                log(t("  [i] 顔検出モデル(.onnx)が見つかりません。簡易検出で続行します。",
                      "  [i] Face model (.onnx) not found. Using basic detection.", '  [i] Yüz modeli (.onnx) bulunamadı. Temel algılama kullanılıyor.'))
                return None
            _yunet = cv2.FaceDetectorYN.create(path, "", (320, 320), 0.6, 0.3, 5000)
            log(t("  [i] 高精度の顔検出（YuNet）を使用します。",
                  "  [i] Using high-accuracy face detection (YuNet).", '  [i] Yüksek doğruluklu yüz algılama (YuNet) kullanılıyor.'))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [i] 顔検出モデルを読み込めませんでした（簡易検出で続行）: {e}",
                  f"  [i] Could not load face model (using basic detection): {e}", f'  [i] Yüz modeli yüklenemedi (temel algılama kullanılıyor): {e}'))
            _yunet = None
        return _yunet


def detect_face_box_yunet(img, log):
    """YuNetで顔を検出し、顔box・目/鼻/口座標・両目の傾きを返す。

    戻り値は ``(box, eye_mid_y, eye_mid_x, nose_x, eye_angle, mouth_mid_y)``。
    eye_angle は画像の水平線に対する両目の角度（度）で、ランドマークを
    取得できない場合は None。mouth_mid_y は顔boxが顎を浅く検出したときの
    顎位置補正に使う。
    """
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
        eye_angle = None  # 両目を結ぶ線の角度。傾き補正に使用
        mouth_mid_y = None  # 口角のy中点。顎位置の過小検出を補正する
        if len(best) >= 10:
            try:
                right_eye_x, right_eye_y = float(best[4]), float(best[5])
                left_eye_x, left_eye_y = float(best[6]), float(best[7])
                eye_mid_x = (right_eye_x + left_eye_x) / 2.0
                eye_mid_y = (right_eye_y + left_eye_y) / 2.0
                nose_x    =  float(best[8])                           # 鼻x
                dx = left_eye_x - right_eye_x
                dy = left_eye_y - right_eye_y
                if abs(dx) >= 2.0:
                    candidate = math.degrees(math.atan2(dy, dx))
                    if abs(candidate) <= 45.0:
                        eye_angle = candidate
            except Exception:
                pass
            if len(best) >= 14:
                try:
                    mouth_mid_y = (float(best[11]) + float(best[13])) / 2.0
                except Exception:
                    pass
        elif len(best) >= 8:
            try:
                eye_mid_x = (float(best[4]) + float(best[6])) / 2.0
                eye_mid_y = (float(best[5]) + float(best[7])) / 2.0
            except Exception:
                pass
        return ((int(round(bx)), int(round(by)), int(round(bw)), int(round(bh))),
                eye_mid_y, eye_mid_x, nose_x, eye_angle, mouth_mid_y)
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
    """顔を検出し ``(box, eye_y, eye_x, nose_x, eye_angle, mouth_y)`` を返す。
    YuNet（同梱 .onnx）を優先し、使えない場合は Haar にフォールバックする。"""
    try:
        import cv2  # noqa: F401
        import numpy as np
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] OpenCV未導入のため顔トリミングをスキップ ({e})", f"  [i] OpenCV not installed; skipping face crop ({e})", f'  [i] OpenCV yüklü değil; yüz kırpma atlanıyor ({e})'))
        return None
    yb = detect_face_box_yunet(img, log)
    if yb is not None:
        return yb
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
        return ((int(round(x)), int(round(y)), int(round(w)), int(round(h))),
                None, None, None, None, None)
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] 顔検出に失敗（全体を使用）: {e}", f"  [i] Face detection failed (using whole image): {e}", f'  [i] Yüz algılanamadı (görüntünün tamamı kullanılıyor): {e}'))
        return None


# 自動補正する最小/最大角度。小さすぎる補正は再サンプリングによる劣化の方が
# 目立ち、大きすぎる角度は横顔やランドマーク誤検出の可能性が高い。
AUTO_LEVEL_MIN_DEG = 0.8
AUTO_LEVEL_MAX_DEG = 20.0


def _flatten_for_detection(img):
    """透過画像を顔検出しやすい白背景RGBへ合成する。"""
    from PIL import Image
    rgba = img.convert("RGBA")
    flat = Image.new("RGB", rgba.size, (255, 255, 255))
    flat.paste(rgba.convert("RGB"), mask=rgba.getchannel("A"))
    return flat


def straighten_face(img, det, already_cutout, log=None):
    """YuNetの両目ランドマークを使って顔を水平にし、再検出結果も返す。

    回転後にもう一度顔を検出し、目の傾きが改善した場合だけ採用する。
    これにより、横顔やランドマーク誤検出で画像を悪化させるのを防ぐ。
    """
    if det is None or len(det) < 5:
        return img, det
    eye_angle = det[4]
    if eye_angle is None or abs(eye_angle) < AUTO_LEVEL_MIN_DEG:
        return img, det
    if abs(eye_angle) > AUTO_LEVEL_MAX_DEG:
        if log is not None:
            log(t(f"  [i] 顔の傾き {eye_angle:+.1f}° は大きすぎるため自動補正をスキップ",
                  f"  [i] Skipping auto-level: face angle {eye_angle:+.1f}° is too large", f'  [i] Otomatik hizalama atlandı: {eye_angle:+.1f}° yüz açısı çok büyük'))
        return img, det

    from PIL import Image
    resampling = getattr(Image, "Resampling", Image)
    fill = (0, 0, 0, 0) if already_cutout else (255, 255, 255, 255)
    rotated = img.convert("RGBA").rotate(
        eye_angle, resample=resampling.BICUBIC, expand=True, fillcolor=fill)
    redetected = detect_face_box(_flatten_for_detection(rotated), log or (lambda _m: None))
    if redetected is None:
        return img, det
    residual = redetected[4] if len(redetected) >= 5 else None
    # YuNetが再び角度を返した場合、明確に改善していなければ元画像へ戻す。
    if residual is not None and abs(residual) >= abs(eye_angle) - 0.25:
        return img, det
    if log is not None:
        suffix = "" if residual is None else f" -> {residual:+.1f}°"
        log(t(f"  [i] 両目を基準に顔を水平補正: {eye_angle:+.1f}°{suffix}",
              f"  [i] Auto-levelled face from the eyes: {eye_angle:+.1f}°{suffix}", f'  [i] Yüz, gözlere göre hizalandı: {eye_angle:+.1f}°{suffix}'))
    return rotated, redetected


# ==========================================================================
# 保存前プレビュー: 襟の移動（Photopea風）と 1:1 トリミング枠（ペイント3D風）
# ==========================================================================
# プレビューは「背景除去後・トリミング前」の画像を表示し、編集内容は
#   moves: [(x0, y0, x1, y1, dx, dy), ...]  … 矩形選択した画素の移動
#          {"poly", "dx", "dy", "fill": "center", "exclude"} … 投げ縄（自由選択）した画素の移動
#   rect:  (left, top, side)                 … 保存する正方形の枠
# という座標データだけで表す。保存時はワーカー側で同じ関数を元画像へ適用するので、
# プレビューで見た構図と保存される PNG の構図が必ず一致する。

def crop_square(img, rect):
    """(left, top, side) の正方形を切り抜く。画像の外側は透明で埋める。

    crop_around_face と同じ貼り付け方なので、同じ枠なら画素まで同じになる。"""
    from PIL import Image
    left, top, side = (int(round(float(v))) for v in rect)
    side = max(1, side)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img.convert("RGBA"), (-left, -top))
    return canvas


def default_crop_rect(img, fit="contain"):
    """顔を検出できなかった画像の初期枠。従来の fit_to_size と同じ範囲を正方形で表す。"""
    w, h = img.size
    side = min(w, h) if fit == "cover" else max(w, h)
    return (int(round((w - side) / 2.0)), int(round((h - side) / 2.0)), int(side))


def collar_protect_box(face_box, chin_y, img_size):
    """顎・口を含む顔を囲む楕円の外接矩形 (x0, y0, x1, y1) を返す（画像の外にはみ出してよい）。

    襟を移動するとき、この楕円の内側の元画素は持ち上げず、常に最前面へ残す。
    楕円は顔の中心を中心に、下端を顎より少し下（顔高さの2%）に置く。
    矩形で守ると顎の両脇の首まで最前面に残り、上げた襟の上に首の肌が四角く重なるので、
    顎のラインに沿って細くなる楕円にしている。"""
    if face_box is None or chin_y is None:
        return None
    x, y, w, h = (float(v) for v in face_box)
    if w < 2 or h < 2:
        return None
    cx, cy = x + w / 2.0, y + h / 2.0
    bottom = float(chin_y) + max(2.0, h * 0.02)
    ry = max(bottom - cy, h * 0.3)
    rx = w * 0.62
    return (int(math.floor(cx - rx)), int(math.floor(bottom - 2 * ry)),
            int(math.ceil(cx + rx)), int(math.ceil(bottom)))


def collar_protect_mask(protect_box, img_size):
    """collar_protect_box の楕円を、画像内に収めた矩形と L モードのマスクにする。"""
    from PIL import Image, ImageDraw
    if protect_box is None:
        return None, None
    ex0, ey0, ex1, ey1 = (int(v) for v in protect_box)
    box = normalize_selection(ex0, ey0, ex1, ey1, img_size)
    if box is None:
        return None, None
    mask = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
    ImageDraw.Draw(mask).ellipse((ex0 - box[0], ey0 - box[1], ex1 - box[0] - 1, ey1 - box[1] - 1),
                                 fill=255)
    return box, mask


def normalize_selection(x0, y0, x1, y1, img_size):
    """ドラッグした2点を、画像内に収めた整数の矩形 (x0, y0, x1, y1) にする。"""
    width, height = int(img_size[0]), int(img_size[1])
    left = max(0, min(width, int(math.floor(min(x0, x1)))))
    right = max(0, min(width, int(math.ceil(max(x0, x1)))))
    top = max(0, min(height, int(math.floor(min(y0, y1)))))
    bottom = max(0, min(height, int(math.ceil(max(y0, y1)))))
    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)


def _composite_clipped(dst, src, x, y):
    """src を dst の (x, y) へアルファ合成する。dst からはみ出す部分は捨てる。"""
    x, y = int(x), int(y)
    sx0, sy0 = max(0, -x), max(0, -y)
    sx1, sy1 = min(src.width, dst.width - x), min(src.height, dst.height - y)
    if sx1 <= sx0 or sy1 <= sy0:
        return
    dst.alpha_composite(src, (x + sx0, y + sy0), (sx0, sy0, sx1, sy1))


def normalize_move(move):
    """襟の移動1件を、矩形 (x0, y0, x1, y1, dx, dy) か {"poly", "dx", "dy"} に整える。

    回転した画面で四角く囲んだ範囲は、元画像の上では傾いた四角形になるので
    {"poly": [[x, y] × 4], "dx": int, "dy": int} で表す。
    「襟を自由選択（投げ縄）」の範囲は {"poly": [...], "dx", "dy", "fill": "center"} で、画素の中心が
    内側にある画素だけを選ぶ（polygon_center_mask）。"exclude": [多角形, ...] があれば、その内側の
    画素は選ばない（反対側の襟で先に選んだ範囲と重ねて描いても、重なった画素を二重に動かさない）。"""
    if isinstance(move, dict):
        poly = [(float(x), float(y)) for x, y in move["poly"]]
        out = {"poly": poly, "dx": int(round(float(move["dx"]))),
               "dy": int(round(float(move["dy"])))}
        if move.get("fill") == "center":
            out["fill"] = "center"
            excl = [[(float(x), float(y)) for x, y in ex] for ex in (move.get("exclude") or [])
                    if len(ex) >= 3]
            if excl:
                out["exclude"] = excl
        return out
    return tuple(int(round(float(v))) for v in move)


def polygon_center_mask(poly, box):
    """多角形の内側に画素の中心 (x+0.5, y+0.5) がある画素を 255 にした L マスク（box の範囲）。

    辺を共有する2つの多角形（左右の襟を別々に選んだときの中央の線など）で、境界の画素が
    両方に入って二重に動き、線が出るのを防ぐ。左端は含み右端は含まない半開区間で塗るので、
    共有した辺の上の画素はどちらか一方にだけ入る。
    投げ縄は点が数百になるので、行ごとの交点を numpy でまとめて求める（偶奇規則）。
    行ごとの交点を並べて2つずつ組にする塗り方と、画素単位で同じ結果になる。"""
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = box
    w, h = int(x1 - x0), int(y1 - y0)
    if w <= 0 or h <= 0 or len(poly) < 3:
        return Image.new("L", (max(0, w), max(0, h)), 0)
    pts = np.asarray(poly, dtype=np.float64) - np.array([x0, y0], dtype=np.float64)
    a = pts
    b = np.roll(pts, -1, axis=0)
    keep = a[:, 1] != b[:, 1]
    a, b = a[keep], b[keep]
    if len(a) == 0:
        return Image.new("L", (w, h), 0)
    lo = np.minimum(a[:, 1], b[:, 1])
    hi = np.maximum(a[:, 1], b[:, 1])
    # 行 row（中心 yc = row + 0.5）を横切る辺: lo <= yc < hi
    r0 = np.clip(np.ceil(lo - 0.5), 0, h).astype(np.int64)
    r1 = np.clip(np.ceil(hi - 0.5), 0, h).astype(np.int64)
    counts = np.maximum(r1 - r0, 0)
    total = int(counts.sum())
    acc = np.zeros((h, w + 1), dtype=np.int32)
    if total:
        edge = np.repeat(np.arange(len(a)), counts)
        start = np.repeat(np.cumsum(counts) - counts, counts)
        rows = r0[edge] + (np.arange(total) - start)
        yc = rows + 0.5
        ax, ay, bx, by = a[edge, 0], a[edge, 1], b[edge, 0], b[edge, 1]
        xs = ax + (yc - ay) * (bx - ax) / (by - ay)
        # 画素の中心 c = col + 0.5 が交点 x 以上になる最初の列から、内外が入れ替わる
        cols = np.clip(np.ceil(xs - 0.5), 0, w).astype(np.int64)
        np.add.at(acc, (rows, cols), 1)
    inside = (np.cumsum(acc, axis=1)[:, :w] & 1).astype(bool)
    return Image.fromarray(inside.astype(np.uint8) * 255, "L")


def lasso_selection_mask(move, box):
    """投げ縄の移動1件の選択マスク（box の範囲）。"exclude" の多角形の内側は除く。"""
    import numpy as np
    from PIL import Image
    mask = polygon_center_mask(move["poly"], box)
    excl = move.get("exclude") or []
    if not excl:
        return mask
    m = np.asarray(mask) > 0
    for ex in excl:
        m &= ~(np.asarray(polygon_center_mask(ex, box)) > 0)
    return Image.fromarray(m.astype(np.uint8) * 255, "L")


def polygon_centroid_x(poly):
    """多角形の面積の重心のx（面積がほぼ0なら頂点の平均）。投げ縄は点の密度が場所で違うので、
    頂点の平均ではなく面積で左右を判定する。"""
    n = len(poly)
    area2 = 0.0
    cx = 0.0
    for i in range(n):
        (xa, ya), (xb, yb) = poly[i], poly[(i + 1) % n]
        cr = xa * yb - xb * ya
        area2 += cr
        cx += (xa + xb) * cr
    if abs(area2) < 1e-9:
        return sum(x for x, _ in poly) / max(1, n)
    return cx / (3.0 * area2)


def move_center_x(move):
    """左右どちらの襟かを判定するための、移動元範囲の中心x（面積の重心）。"""
    move = normalize_move(move)
    if isinstance(move, dict):
        return polygon_centroid_x(move["poly"])
    return (move[0] + move[2]) / 2.0


def _center_move_piece(rest, orig, box, mask, move, center_done):
    """多角形の移動1件の「切り取る画素」と「透明にする範囲」を返す（apply_collar_moves 用）。

    左右の襟を隣り合う多角形で別々に選び、同じ量だけ動かしたとき、回転中などで移動の向きが
    継ぎ目と平行でないと、先に動かした側が1px未満ずつ反対側の範囲へはみ出す。次の選択がその
    はみ出しをもう一度動かすと、継ぎ目に沿って点々と穴が開く。そこで、範囲が重ならない
    （隣り合う）先の多角形が置いた画素は、この選択では動かさずに残し、代わりに移動前の画素を
    切り取る。範囲が重なる選択（動かした襟を選び直して、さらに動かす場合）は従来どおり、
    今見えている画素を切り取る。"""
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = box
    m = np.asarray(mask) > 0
    full = np.zeros((rest.height, rest.width), dtype=bool)
    full[y0:y1, x0:x1] = m
    dep = np.zeros_like(m)
    layer = None             # 先の多角形が置いた画素だけ（下地と重ねる前の値）
    for src_j, dst_j, piece_j, at_j in center_done:
        if not (src_j & full).any():
            here = dst_j[y0:y1, x0:x1] & m
            if here.any():
                dep |= here
                if layer is None:
                    layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
                _composite_clipped(layer, piece_j, at_j[0] - x0, at_j[1] - y0)
    piece = rest.crop(box)
    if dep.any():
        dep_mask = Image.fromarray(dep.astype(np.uint8) * 255, "L")
        piece.paste(orig.crop(box), (0, 0), dep_mask)
        # 置かれた画素は、この選択の元の画素と重ねずに、置かれた値そのものにする
        # （1つの選択でまとめて動かした結果と同じになる）
        rest.paste(layer, (x0, y0), dep_mask)
    clear_mask = Image.fromarray((m & ~dep).astype(np.uint8) * 255, "L")
    # この移動の移動先（画像内に収まる分）を記録する
    dst = np.zeros_like(full)
    dx, dy = int(move["dx"]), int(move["dy"])
    h, w = full.shape
    sy0, sy1 = max(0, -dy), min(h, h - dy)
    sx0, sx1 = max(0, -dx), min(w, w - dx)
    if sy1 > sy0 and sx1 > sx0:
        dst[sy0 + dy:sy1 + dy, sx0 + dx:sx1 + dx] = full[sy0:sy1, sx0:sx1]
    # piece は呼び出し側でマスクを掛けたものがそのまま置かれる（同じオブジェクトを記録する）
    center_done.append((full, dst, piece, (x0 + dx, y0 + dy)))
    return piece, clear_mask


def apply_collar_moves(img, moves, protect_box=None):
    """選択した画素を切り取って移動する（Photopea の「選択範囲の移動」と同じ考え方）。

    moves は矩形 (x0, y0, x1, y1, dx, dy) または傾いた四角形 {"poly", "dx", "dy"}
    （normalize_move を参照）を順に適用する。選択範囲は切り取りなので
    移動元は透明になり、襟が二重に残らない。protect_box の楕円の内側の元画素（顎・口）は
    持ち上げも上書きもせず最前面へ戻すため、顔の形と位置は変わらない。"""
    from PIL import Image, ImageChops, ImageDraw
    src = img.convert("RGBA")
    if not moves:
        return src.copy()
    rest = src.copy()
    clear_color = (0, 0, 0, 0)
    pbox, pmask = collar_protect_mask(protect_box, src.size)
    if pbox is not None:
        rest.paste(clear_color, pbox, pmask)
    orig = rest.copy()       # 移動前（多角形どうしの継ぎ目の処理で使う）
    center_done = []         # 適用済みの多角形の移動: (移動元のマスク, 移動先のマスク, 置いた画素, 置いた位置)
    for move in moves:
        move = normalize_move(move)
        if isinstance(move, dict):
            xs = [x for x, _ in move["poly"]]
            ys = [y for _, y in move["poly"]]
            # ImageDraw.polygon は右端・下端の画素も塗るので、その1px分まで含める
            box = normalize_selection(min(xs), min(ys), max(xs) + 1, max(ys) + 1, src.size)
            if box is None:
                continue
            # 範囲の内側だけを切り取る。境界をぼかさないので、
            # どの画素も「全部動く」か「動かない」かのどちらかになり、二重に残らない。
            if move.get("fill") == "center":
                mask = lasso_selection_mask(move, box)
                piece, clear_mask = _center_move_piece(rest, orig, box, mask, move, center_done)
                piece.putalpha(ImageChops.multiply(piece.getchannel("A"), mask))
                rest.paste(clear_color, box, clear_mask)
                _composite_clipped(rest, piece, box[0] + move["dx"], box[1] + move["dy"])
                continue
            mask = Image.new("L", (box[2] - box[0], box[3] - box[1]), 0)
            ImageDraw.Draw(mask).polygon([(x - box[0], y - box[1]) for x, y in move["poly"]],
                                         fill=255)
            piece = rest.crop(box)
            piece.putalpha(ImageChops.multiply(piece.getchannel("A"), mask))
            rest.paste(clear_color, box, mask)
            _composite_clipped(rest, piece, box[0] + move["dx"], box[1] + move["dy"])
            continue
        x0, y0, x1, y1, dx, dy = move
        box = normalize_selection(x0, y0, x1, y1, src.size)
        if box is None:
            continue
        piece = rest.crop(box)
        rest.paste(clear_color, box)
        _composite_clipped(rest, piece, box[0] + dx, box[1] + dy)
    if pbox is not None:
        # 楕円の内側だけ、元の顔を襟の上に重ねる
        face = src.crop(pbox)
        region = rest.crop(pbox)
        under_alpha = region.getchannel("A")
        region.alpha_composite(face)
        rest.paste(region, pbox[:2], pmask)
        # 下に襟が来ていない画素は元の顔をそのまま戻す（合成の丸め誤差も残さない）。
        untouched = ImageChops.multiply(under_alpha.point(lambda a: 255 if a == 0 else 0), pmask)
        rest.paste(face, pbox[:2], untouched)
    return rest


# 保存前プレビューの手動回転の範囲（旧版のスライダーと同じ）。
PREVIEW_ANGLE_LIMIT = 15.0
# 保存前プレビューを操作しないまま放置できる時間（秒）。過ぎたら保存せずにバッチを止める。
# 以前は10分待つと確認なしで「保存」して次へ進んでいた。
PREVIEW_IDLE_TIMEOUT_SEC = 600
# 上の時間に加えて、プレビューが開かない・応答しないときにワーカー側で待つ猶予（秒）。
PREVIEW_WAIT_GRACE_SEC = 60


def clamp_preview_angle(angle):
    """手動回転角を −15°〜＋15°・0.1°刻みにそろえる。"""
    try:
        angle = float(angle)
    except (TypeError, ValueError):
        return 0.0
    return round(max(-PREVIEW_ANGLE_LIMIT, min(PREVIEW_ANGLE_LIMIT, angle)), 1)


def rotate_point(x, y, angle, center, inverse=False):
    """回転前の座標 (x, y) を、center 中心に angle 度（正で反時計回り）回した座標にする。

    inverse=True なら回転後の座標から回転前の座標へ戻す。
    プレビューの表示・襟の選択・保存はすべてこの対応を使う。"""
    rad = math.radians(float(angle))
    c, s = math.cos(rad), math.sin(rad)
    if inverse:
        s = -s
    rx, ry = x - center[0], y - center[1]
    # 画面座標は y が下向きなので、反時計回りは (c, s; -s, c)
    return center[0] + c * rx + s * ry, center[1] - s * rx + c * ry


def _rotation_affine(angle, center, left, top):
    """出力画素 (u, v) が回転後座標 (left+u, top+v) に対応するときの、
    Image.transform(AFFINE) 用の係数（出力 → 回転前の元画像）。"""
    rad = math.radians(float(angle))
    c, s = math.cos(rad), math.sin(rad)
    cx, cy = float(center[0]), float(center[1])
    qx, qy = float(left) - cx, float(top) - cy
    return (c, -s, cx + c * qx - s * qy,
            s, c, cy + s * qx + c * qy)


def rotate_crop_square(img, rect, angle=0.0, center=None):
    """img を center 中心に angle 度回転し、回転後の座標で rect=(left, top, side) を切り抜く。

    回転と切り抜きを1回のアフィン変換で行うので、大きな画像でも枠の大きさ分しか計算しない。
    0°なら crop_square と同じ（画素まで一致）。"""
    from PIL import Image
    angle = clamp_preview_angle(angle)
    if abs(angle) < 0.05 or center is None:
        return crop_square(img, rect)
    left, top, side = (int(round(float(v))) for v in rect)
    side = max(1, side)
    # 透明部分の黒が輪郭へにじまないよう、乗算済みアルファで補間する
    src = img.convert("RGBA").convert("RGBa")
    out = src.transform((side, side), Image.AFFINE, _rotation_affine(angle, center, left, top),
                        resample=Image.BICUBIC)
    return out.convert("RGBA")


def preview_edit_is_empty(edit, auto_rect):
    """襟の移動なし・枠が初期位置・回転0°なら True（従来の自動出力をそのまま使う）。"""
    if not edit:
        return True
    rect = tuple(int(v) for v in (edit.get("rect") or auto_rect))
    return (not edit.get("moves") and rect == tuple(int(v) for v in auto_rect)
            and abs(clamp_preview_angle(edit.get("angle", 0.0))) < 0.05)


def preview_edit_without_unprotected_moves(edit, protect_box):
    """顎の保護範囲（protect_box）が無いときは、襟の移動を取り除いた編集内容を返す。

    顔検出OFF・検出失敗では顎の位置が分からず、襟を動かすと顔の下まで切り取られて
    しまうため、プレビューで選べないだけでなく保存時にも適用しない。"""
    if not edit or protect_box is not None or not edit.get("moves"):
        return edit
    edit = dict(edit)
    edit["moves"] = []
    return edit


def render_preview_edit(img, edit, auto_rect, protect_box=None, rotate_center=None):
    """プレビューの編集内容を元画像へ適用し、保存枠で切り抜いた正方形を返す。

    順番は「襟の移動 → 手動回転（rotate_center 中心）→ 1:1 の枠で切り抜き」。
    編集が無い（襟の移動なし・枠が初期位置のまま・回転0°）なら None を返し、
    呼び出し側は従来の自動トリミング結果をそのまま使う。
    protect_box が None（顎の位置が不明）のときは襟の移動を適用しない。"""
    edit = preview_edit_without_unprotected_moves(edit, protect_box)
    if preview_edit_is_empty(edit, auto_rect):
        return None
    moves = [normalize_move(m) for m in (edit.get("moves") or [])]
    rect = tuple(int(v) for v in (edit.get("rect") or auto_rect))
    edited = apply_collar_moves(img, moves, protect_box)
    return rotate_crop_square(edited, rect, edit.get("angle", 0.0), rotate_center)


def make_checkerboard(size, mode="dark", tile=10):
    """透過部分を示す市松模様（プレビュー表示専用）。"""
    from PIL import Image, ImageDraw
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    dark = mode == "dark"
    check_a = (62, 62, 66, 255) if dark else (210, 210, 214, 255)
    check_b = (82, 82, 87, 255) if dark else (235, 235, 238, 255)
    board = Image.new("RGBA", (w, h), check_a)
    draw = ImageDraw.Draw(board)
    for y in range(0, h, tile):
        for x in range((y // tile) % 2 * tile, w, tile * 2):
            draw.rectangle((x, y, min(x + tile - 1, w - 1), min(y + tile - 1, h - 1)),
                           fill=check_b)
    return board


# 目をキャンバス上端から何割の位置に置くか。
# 既存FMフェイスパック（180px版24枚）の実測中央値が 49.4% だったため 0.49。
EYE_RATIO = 0.49

# 目→口の距離に対する口→顎の推定比率。顔boxが顎を浅く取った時だけ使う。
# 大きすぎる補正は crop_around_face 側で元box高さの25%までに制限する。
MOUTH_TO_CHIN_RATIO = 0.85

# 髪の上端に最低限確保する余白（キャンバス高さに対する割合）。
# 投稿ガイドの完成例に合わせ、頭頂を上端から約5%に配置する。
CROWN_MARGIN = 0.05


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


def _silhouette_collar_y(img, chin_y, face_cx, face_w, face_h):
    """背景除去済みの元画像から、襟・肩が首より広がり始めるy座標を探す。"""
    try:
        import numpy as np
        alpha = np.asarray(img.convert("RGBA"))[..., 3]
        if alpha.size == 0 or int(alpha.min()) > 250:
            return None
        h, w = alpha.shape
        lo = max(0, int(round(face_cx - face_w * 1.8)))
        hi = min(w, int(round(face_cx + face_w * 1.8)) + 1)
        start = max(0, int(round(chin_y + max(2.0, face_h * 0.02))))
        end = min(h, int(round(chin_y + face_h * 1.25)))
        if hi - lo < 8 or end - start < 8:
            return None
        widths = (alpha[start:end, lo:hi] > 40).sum(axis=1).astype(float)
        smooth = np.convolve(np.pad(widths, (2, 2), mode="edge"),
                             np.ones(5, dtype=float) / 5.0, mode="valid")
        minimum = max(3.0, face_w * 0.06)
        neck_search_end = min(len(smooth), max(5, int(round(face_h * 0.55))))
        valid = np.nonzero(smooth[:neck_search_end] > minimum)[0]
        if valid.size < 5:
            return None
        narrow_index = int(valid[np.argmin(smooth[valid])])
        neck_width = float(smooth[narrow_index])
        widening = max(neck_width * 1.38, face_w * 0.48)
        for row in range(narrow_index + 1, len(smooth) - 2):
            if all(smooth[row:row + 3] >= widening):
                return float(start + row)
        return None
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
                     eye_mid_y=None, eye_mid_x=None, nose_x=None,
                     mouth_mid_y=None, return_meta=False):
    """顔の高さを基準に正方形クロップ。
    ・eye_mid_y がボックス内20〜46%にある場合: 目を上から40%に配置（FM標準スタイル）
    ・それ以外フォールバック: 顔ボックス上端を上から25%に配置（ひげ誤検出に強い）
    ・ヘアガード: 顔ボックス上端が最低22%の余白を持つよう保証（安全網）
    size_factor: キャンバス高さ = 顔高さ × size_factor（目〜顎距離で正規化）
    neck: 顎の下に残す余白（キャンバス高さに対する割合）"""
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
    # YuNetの顔boxは、AI拡大画像や細長い顔で顎を浅く取ることがある。
    # 口角はbox下端より安定しているので、目→口の距離から顎を推定し、
    # box下端より下になる場合だけ安全側へ補正する。
    if eye_mid_y is not None and mouth_mid_y is not None:
        eye_to_mouth = mouth_mid_y - eye_mid_y
        valid_mouth = (h * 0.15 <= eye_to_mouth <= h * 0.60
                       and y + h * 0.45 <= mouth_mid_y <= y + h * 1.05)
        if valid_mouth:
            estimated_chin = mouth_mid_y + eye_to_mouth * MOUTH_TO_CHIN_RATIO
            estimated_chin = min(estimated_chin, y + h * 1.25)
            if estimated_chin > chin:
                if log is not None and estimated_chin - chin > h * 0.02:
                    log(t(f"  [i] 口の位置から顎を補正: {chin:.0f} -> {estimated_chin:.0f}px",
                          f"  [i] Corrected chin from mouth landmarks: {chin:.0f} -> {estimated_chin:.0f}px", f'  [i] Çene konumu ağız noktalarına göre düzeltildi: {chin:.0f} -> {estimated_chin:.0f} piksel'))
                chin = estimated_chin
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
                          f"  [i] Enlarging canvas to avoid cutting the crown: {canvas_h:.0f} -> {need:.0f}px", f'  [i] Başın üstünün kesilmemesi için tuval büyütülüyor: {canvas_h:.0f} -> {need:.0f} piksel'))
                canvas_h = need
            top = anchor_y - canvas_h * anchor_ratio
        else:
            # 透過が無い（背景除去前）場合の安全網: 顔ボックス上端に最低 hair_guard の余白
            if y - top < hair_guard:
                top = y - hair_guard

        bottom = top + canvas_h

        # 長い首の素材でも襟に届く前に切らない。背景除去済みの元画像で
        # 首が横へ広がる位置を探し、必要な場合だけキャンバスを下方向へ拡張する。
        collar_required_bottom = None
        collar_y = _silhouette_collar_y(img, chin, x + w / 2.0, w, h)
        if collar_y is not None:
            # 襟の開始線だけでは完成画像で数pxしか見えず「首だけ」に見える。
            # 顔高さの18%ぶん襟を見せ、肩を入れすぎない深さまで確保する。
            collar_required_bottom = collar_y + max(2.0, canvas_h * 0.025,
                                                     h * 0.18)
            # 長い首でも襟の最初の数pxは必ず見せる。拡大上限は55%。
            max_bottom = bottom + canvas_h * 0.55
            collar_required_bottom = min(collar_required_bottom, max_bottom)
            if collar_required_bottom > bottom:
                old_h = canvas_h
                bottom = collar_required_bottom
                canvas_h = bottom - top
                if log is not None:
                    log(t(f"  [i] 襟を残すためキャンバスを拡大: {old_h:.0f} -> {canvas_h:.0f}px",
                          f"  [i] Enlarging canvas to retain the collar: {old_h:.0f} -> {canvas_h:.0f}px", f'  [i] Yakayı korumak için tuval büyütülüyor: {old_h:.0f} -> {canvas_h:.0f} piksel'))

        # 投稿ガイドに合わせ、顎下の余白を必要以上に残さない。
        # 正方形の大きさは変えず、切り抜き枠全体を上へ移動するので、
        # 顔サイズの一貫性と頭頂ガードを保ったまま肩の写り込みを減らせる。
        neck = max(0.01, min(float(neck), 0.20))
        desired_bottom = chin + canvas_h * neck
        if collar_required_bottom is not None:
            desired_bottom = max(desired_bottom, collar_required_bottom)
        if bottom > desired_bottom:
            shift = bottom - desired_bottom
            top -= shift
            bottom -= shift
            if log is not None and shift > canvas_h * 0.01:
                log(t(f"  [i] 顎下の余白を詰めました: {shift:.0f}px",
                      f"  [i] Tightened the space below the chin by {shift:.0f}px", f'  [i] Çene altındaki boşluk {shift:.0f} piksel azaltıldı'))

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
                  f"  [i] Horizontal center corrected to silhouette: {cx:.0f} -> {sil_cx:.0f}", f'  [i] Yatay merkez silüete göre düzeltildi: {cx:.0f} -> {sil_cx:.0f}'))
        cx = sil_cx
    left = int(round(cx - side / 2.0))
    canvas = crop_square(img, (left, top, side))
    if return_meta:
        chin_ratio = max(0.0, min(1.0, (float(chin) - top) / side))
        # rect / chin_y / face_box は保存前プレビュー（トリミング前の画像上の座標）で使う
        return canvas, {"chin_ratio": chin_ratio, "rect": (left, top, side),
                        "chin_y": float(chin), "face_box": tuple(box)}
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
        log(t("  [i] RapidOCR を初期化中…", "  [i] Initializing RapidOCR…", '  [i] RapidOCR başlatılıyor…'))
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
        log(t("  [i] EasyOCR を初期化中…（初回はモデルDLで少し時間がかかります）", "  [i] Initializing EasyOCR… (first run downloads models)", '  [i] EasyOCR başlatılıyor… (ilk çalıştırmada modeller indirilir)'))
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
        '(Or uncheck "Auto-read ID" to use filename-as-ID instead.)', 'OCR motoru bulunamadı. Lütfen şu komutu çalıştırın:\n    py -3.12 -m pip install rapidocr-onnxruntime\n(Hafiftir, torch gerektirmez.)\n(Veya dosya adını ID olarak kullanmak için "ID’yi otomatik oku" seçeneğini kapatın.)'))


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
              f"  [!] Cannot open image (skipped): {Path(path).name}: {e}", f'  [!] Görüntü açılamadı (atlandı): {Path(path).name}: {e}'))
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
                         "Could not move to Recycle Bin (send2trash not installed)", 'Geri Dönüşüm Kutusu’na taşınamadı (send2trash yüklü değil)'))


def _source_image_delete_targets(input_dir, output_dir=None):
    """ペアフォルダ内の元画像だけを列挙し、出力フォルダは必ず除外する。"""
    base = Path(input_dir)
    try:
        output_resolved = Path(output_dir).resolve() if output_dir else None
    except OSError:
        output_resolved = Path(output_dir) if output_dir else None
    targets = []
    for sub in sorted(d for d in base.iterdir() if d.is_dir()):
        try:
            if output_resolved is not None and sub.resolve() == output_resolved:
                continue
        except OSError:
            if output_resolved is not None and sub == output_resolved:
                continue
        targets.extend(
            f for f in sub.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXT)
    return targets



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
                       "Cannot determine the encoding of config.xml (please re-save it as UTF-8)", 'config.xml dosyasının kodlaması belirlenemedi (lütfen UTF-8 olarak yeniden kaydedin)'))


def _timestamped_backup(path):
    """既存ファイルを上書き前に退避し、作成したバックアップのPathを返す。"""
    path = Path(path)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = path.with_name(f"{path.name}.{stamp}.bak")
    serial = 2
    while bak.exists():
        bak = path.with_name(f"{path.name}.{stamp}_{serial}.bak")
        serial += 1
    try:
        shutil.copy2(path, bak)
    except BaseException:
        # 途中まで書いたバックアップは残さない（不完全なファイルをバックアップと誤認させない）
        try:
            if bak.exists():
                os.chmod(bak, 0o666)
                bak.unlink()
        except Exception:  # noqa: BLE001
            pass
        raise
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
    戻り値: 実際に行った操作の結果（dict）。画面の状態表示・処理ログ・共有診断はこの結果を共通に使う。
      "result":
        "created"      ファイルが無かったので新しく作った
        "regenerated"  空のファイル（追記モードでは壊れたファイルも）を作り直した
        "overwritten"  追記オフで、中身のある既存ファイルを置き換えた（元はバックアップ）
        "updated"      追記した（新規のIDがあった）
        "unchanged"    追記するIDがすべて登録済みで、書き込まなかった（"reason": "all_registered"。内容も更新時刻も変えない）
        "failed"       書き込めなかった（"error": 区分、"stage": "read" / "backup" / "write"）
      "added": 新規件数, "total": 合計件数, "backup": 以前のファイルをバックアップしたか（その後の書き込みに失敗しても残す）
      "_backup_path" / "_exc": 処理ログ用（共有診断には出さない。_finish が取り除く）
    書き込みは一時ファイルに書いてから置き換えるので、失敗しても元の config.xml はバイト単位で変わらない。
    バックアップを作れなかったときは書き込まない（元を退避できないまま置き換えない）。"""
    prefix = "r-" if newgen else ""
    res = {"result": None, "added": 0, "total": 0, "backup": False, "_backup_path": None}

    def failed(stage, e, backup=None):
        return dict(res, result="failed", error=config_error_kind(e), stage=stage,
                    backup=backup is not None, _backup_path=backup, _exc=e)

    try:
        exists = cfg_path.exists()
        size = cfg_path.stat().st_size if exists else 0
        existing = []
        text = ""
        if append and exists:
            text = _read_config_text(cfg_path)
            # from= の値は "12345" とは限らない（既製パックは "face_12345"、newgenは "r-12345"）。
            # 数字だけを要求すると重複を検出できず、同じIDが二重に入る。
            for rec in RECORD_RE.findall(text):
                k = _record_key(rec)
                if k is not None:
                    existing.append(k)
    except Exception as e:  # noqa: BLE001
        return failed("read", e)

    def record_lines(ids):
        return ["\t\t<record from=%s to=%s/>\n" % (
            quoteattr(f"{prefix}{u}"),
            quoteattr(f"graphics/pictures/person/{u}/portrait"))
            for u in ids]

    # 追記できるのは <list id="maps"> を持つ正しい形の既存ファイルだけ。
    # 空ファイルや壊れたファイルに追記すると、ルート要素の無い不正なXMLが出来上がり、
    # FMがパック全体を読まなくなる（しかも以降ずっとそこに追記し続ける）。
    close = -1
    if append and exists and text.strip():
        close = text.rfind("</list>")
        if close == -1:
            close = text.rfind("</graphics>")  # 旧形式の config.xml からの移行用フォールバック
    if close != -1:
        existing_set = set(existing)
        new_uids = [u for u in uids if str(u) not in existing_set]
        total = len(existing) + len(new_uids)
        new_text = text[:close] + "".join(record_lines(new_uids)) + text[close:]
        if new_text == text:
            # 追記するIDがすべて登録済み：書き込まない（内容も更新時刻も変えない）
            return dict(res, result="unchanged", reason="all_registered", total=total)
        try:
            _atomic_write_text(cfg_path, new_text)
        except Exception as e:  # noqa: BLE001
            return failed("write", e)
        return dict(res, result="updated", added=len(new_uids), total=total)

    # 新規作成・作り直し・上書き。今回のIDをすべて書く（重複は1回だけ）
    ids = list(dict.fromkeys(str(u) for u in uids))
    if not exists:
        op = "created"
    elif append or size == 0:
        op = "regenerated"          # 空のファイル、または追記先として使えない壊れたファイル
    else:
        op = "overwritten"          # 追記オフ（明示的な上書き）
    backup = None
    if exists and size > 0:
        # 明示的な上書き、または壊れた既存ファイルの再生成では、元を必ず退避する。
        try:
            backup = _timestamped_backup(cfg_path)
        except Exception as e:  # noqa: BLE001
            return failed("backup", e)
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
    try:
        _atomic_write_text(cfg_path, header + "".join(record_lines(ids)) + footer)
    except Exception as e:  # noqa: BLE001
        return failed("write", e, backup)      # バックアップは消さない（名前は処理ログに出す）
    return dict(res, result=op, added=len(ids), total=len(ids), backup=backup is not None, _backup_path=backup)


def _log_config_result(res, cfg, newgen, log):
    """write_config の結果を処理ログに書く。パスと例外の文は処理ログだけに出す（共有診断には出さない）。"""
    tag = t("（newgen: r- 付き）", " (newgen: r- prefix)", ' (newgen: r- öneki)') if newgen else ""
    r = res.get("result")
    bpath = res.get("_backup_path")
    added, total = res.get("added"), res.get("total")
    if r == "failed":
        e = res.get("_exc")
        why = config_error_reason(res.get("error"))
        detail = f"{type(e).__name__}: {e}" if e is not None else ""
        where = "" if cfg.name in detail else f" -> {cfg}"      # 例外の文にパスがあれば重ねて出さない
        if res.get("stage") == "backup":
            log(t(f"[!] config.xml のバックアップを作成できなかったため、config.xml を書き換えていません（{why}）: {detail}{where}",
                  f"[!] A backup of config.xml could not be created, so config.xml was not rewritten ({why}): {detail}{where}",
                  f"[!] config.xml yedeği oluşturulamadığı için config.xml yeniden yazılmadı ({why}): {detail}{where}"))
        else:
            log(t(f"[!] config.xml を書き込めませんでした（{why}）: {detail}{where}",
                  f"[!] Could not write config.xml ({why}): {detail}{where}",
                  f"[!] config.xml yazılamadı ({why}): {detail}{where}"))
        if bpath is not None:
            log(t(f"既存の config.xml のバックアップを残しました: {bpath.name}（元の config.xml は変更していません）",
                  f"Kept the backup of the previous config.xml: {bpath.name} (the original config.xml was not changed)",
                  f"Önceki config.xml yedeği korundu: {bpath.name} (asıl config.xml değiştirilmedi)"))
        return
    if r == "unchanged":
        log(t(f"config.xml: 追記するIDはすべて登録済みのため、書き込んでいません（合計 {total} 件） -> {cfg}",
              f"config.xml: not changed; every ID to add is already registered ({total} total) -> {cfg}",
              f"config.xml: değiştirilmedi; eklenecek tüm ID'ler zaten kayıtlı (toplam {total}) -> {cfg}"))
        return
    if r == "updated":
        log(t(f"config.xml を更新{tag}: 新規 {added} 件 / 合計 {total} 行 -> {cfg}",
              f"Updated config.xml{tag}: {added} new / {total} total lines -> {cfg}",
              f'config.xml güncellendi{tag}: {added} yeni / toplam {total} satır -> {cfg}'))
    elif r == "regenerated":
        log(t(f"config.xml を再生成{tag}（空または壊れたファイルを作り直し）: {total} 行 -> {cfg}",
              f"Regenerated config.xml{tag} (the empty or broken file was rebuilt): {total} lines -> {cfg}",
              f'config.xml yeniden oluşturuldu{tag} (boş veya bozuk dosya yeniden kuruldu): {total} satır -> {cfg}'))
    elif r == "overwritten":
        log(t(f"config.xml を上書き{tag}: {total} 行 -> {cfg}",
              f"Overwrote config.xml{tag}: {total} lines -> {cfg}",
              f'config.xml üzerine yazıldı{tag}: {total} satır -> {cfg}'))
    else:
        log(t(f"config.xml を生成{tag}: {total} 行 -> {cfg}",
              f"Generated config.xml{tag}: {total} lines -> {cfg}",
              f'config.xml oluşturuldu{tag}: {total} satır -> {cfg}'))
    if bpath is not None:
        log(t(f"既存の config.xml をバックアップしました: {bpath.name}",
              f"Backed up the previous config.xml: {bpath.name}", f'Önceki config.xml yedeklendi: {bpath.name}'))


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


class _Prefetcher:
    """次にプレビューを開く画像1枚の「プレビュー前の処理」（_prepare_one）を、別スレッドで先に進める。
    「実行」1回につき1つ作る。枠は1つだけ（処理中と準備済みを合わせて最大1枚）。

    ・start(): プレビューを依頼する直前に、次にプレビューを開く見込みの画像で呼ぶ。
    ・take(): 処理のループがその画像に進んだときに呼ぶ。まだ処理中なら同じ処理の完了を待ち（二重に処理しない）、
      ためておいたログ・集計・背景透過の実績を、呼び出したスレッドで順番どおりに反映して結果を返す。
      先読みが失敗していたら、同じ例外をここで送出する（失敗の反映もその画像に進んだ時点）。
    ・discard() / shutdown(): 画像のループを抜けたとき（完了・キャンセル・放置終了・異常中止）に呼ぶ。
      先読みは使わずに捨て、shutdown() はスレッドの終了まで待つ。「実行」が終わったと知らせる前に必ず待つので、
      遅れて終わった先読みが次の実行に混ざることはない。
    先読みスレッドはファイルを書かない。PNGの保存と config.xml の更新は処理スレッドだけが行う。"""

    def __init__(self, opts):
        self._opts = opts
        self._lock = threading.Lock()
        self._job = None
        self._closed = False

    def start(self, key, path):
        cancel = self._opts.get("_cancel")
        if cancel is not None and cancel.is_set():
            return False
        with self._lock:
            if self._closed or self._job is not None:
                return False
            job = {"key": key, "path": path, "ops": [], "done": threading.Event(), "discard": False,
                   "result": None, "error": None, "stage": None}
            job["thread"] = threading.Thread(target=self._work, args=(job,), daemon=True, name="fm-prefetch")
            self._job = job
        try:
            job["thread"].start()
        except Exception:  # noqa: BLE001
            # スレッドを作れなかった（メモリ・スレッドの枯渇）。枠を空け、この画像は処理スレッドが自分で処理する
            with self._lock:
                if self._job is job:
                    self._job = None
            return False
        return True

    def _work(self, job):
        ops = job["ops"]
        _prefetch_ctx.ops = ops
        _prefetch_ctx.job = job
        try:
            res = _prepare_one(job["path"], self._opts, lambda msg: ops.append(("log", msg)))
            if not job["discard"]:
                job["result"] = res
        except BaseException as e:  # noqa: BLE001
            # Exception 以外で止まったときも「この画像の失敗」として持つ（結果なしで終わると、処理スレッドが
            # 同じ画像をもう一度処理し、ログと集計が二重になる）
            job["error"] = e if isinstance(e, Exception) else RuntimeError(f"{type(e).__name__}: {e}")
            job["stage"] = getattr(_proc_stage, "v", None)
        finally:
            _prefetch_ctx.ops = None
            _prefetch_ctx.job = None
            _proc_stage.v = None
            job["done"].set()

    def take(self, key, log):
        """key の画像の先読みがあれば、完了を待って結果を返す（無ければ None。呼び出し側が自分で処理する）。"""
        with self._lock:
            if self._closed:
                return None
            job, self._job = self._job, None      # 枠を空ける（次の start は、この画像のプレビューを開くとき）
        if job is None:
            return None
        if job["key"] != key:
            # 見込みと違う画像に進んだ（先読みのあとで出力フォルダの状態が変わったなど）。使わずに捨て、
            # 同時に2枚を処理しないよう、終わるのを待ってから呼び出し側が自分で処理する
            job["discard"] = True
            job["thread"].join()
            job["ops"], job["result"], job["error"] = [], None, None
            return None
        while not job["done"].wait(0.2):
            pass
        ops, job["ops"] = job["ops"], []
        result, error, stage = job["result"], job["error"], job["stage"]
        job["result"] = job["error"] = None
        for op in ops:
            if op[0] == "log":
                log(op[1])
            elif op[0] == "count":
                _count_result(op[1], op[2])
            elif op[0] == "rembg":
                _note_rembg_use(op[1], op[2], op[3], log)
        if error is not None:
            _proc_stage.v = stage           # 失敗の理由の区分は、先読みが止まった段階で数える
            raise error
        return result

    def discard(self, log=None):
        """受付を閉じ、先読み（処理中・準備済み）を使わないことを確定する。待たない。捨てるものがあれば True。"""
        with self._lock:
            self._closed = True
            job = self._job
            if job is None or job["discard"]:
                return False
            job["discard"] = True
        if log:
            name = Path(job["path"]).name
            log(t(f"[i] 先読みしていた次の画像（{name}）は使わずに破棄しました（保存・集計には含めません）",
                  f"[i] The next image prepared in advance ({name}) was discarded (not saved, not counted)",
                  f"[i] Önceden hazırlanan sonraki görüntü ({name}) atıldı (kaydedilmedi, sayılmadı)"))
        return True

    def shutdown(self, log=None):
        """先読みを捨て、スレッドが終わるまで待つ。何度呼んでもよい。"""
        self.discard(log)
        with self._lock:
            job, self._job = self._job, None
        if job is None:
            return
        th = job["thread"]
        if th.is_alive():
            if log:
                log(t("[i] 先読み中だった処理が終わるのを待っています…（結果は使いません）",
                      "[i] Waiting for the image being prepared in advance to finish… (its result is not used)",
                      "[i] Önceden hazırlanmakta olan görüntünün bitmesi bekleniyor… (sonucu kullanılmayacak)"))
            th.join()
        job["ops"], job["result"], job["error"] = [], None, None


def _next_preview_item(entries, idx, seen, seen_lock, out_dir, opts, require_digits=False, strip=False):
    """idx 番目（1始まり）の次に保存前プレビューを開く見込みの画像を (キー, パス) で返す。無ければ None。
    entries は処理順の (パス, ID)。処理のループと同じ順・同じ条件（数字でないID・ID重複・既存PNG）で
    先を見るだけで、何も変更しない。実際にどう扱うかは、ループがその画像に進んだときに決まる。"""
    with seen_lock:
        taken = set(seen)
    for j in range(idx, len(entries)):
        path, uid = entries[j]
        if strip:
            uid = str(uid).strip()
        if require_digits and not str(uid).isdigit():
            continue
        if uid in taken:
            continue
        taken.add(uid)
        if (out_dir / out_filename(uid, opts)).exists() and not opts["overwrite"]:
            continue
        return (j + 1, str(path)), path
    return None


def process_one(path, out_path, opts, log, item_key=None, next_item=None):
    """1枚の顔写真を実際に加工する処理本体。
    顔検出 -> 背景除去 -> 顔トリミング -> （保存前プレビュー）-> アップスケール
    -> 指定サイズへリサイズ -> PNG保存。

    背景除去をトリミングより前に置いているのは、トリミングの水平中心を
    「頭のシルエット中心」で決めるため。目の中点だけを基準にすると、
    振り向いた顔で頭が片側に寄って見切れる。
    顔検出だけは背景除去『前』の元画像に対して行う（透明部が黒く写ると検出精度が落ちるため）。
    背景除去は画像サイズを変えないので、検出したボックス座標はそのまま使える。

    保存できたら True、プレビューでスキップしたら "skip"、プレビューでキャンセルしたら "cancel"、
    プレビューを確認できずに中止したら（表示失敗・無操作・応答なしなど）"abort"（どちらもバッチを止める）、
    それ以外で保存できなかったら False を返す。失敗の理由の区分は _set_stage の段階で数える。

    プレビューより前（読み込み〜顔トリミング）は _prepare_one、プレビュー以降は _finish_one。
    先読み（opts["_prefetch"]）がある実行では、item_key の画像が先読み済み・先読み中ならその結果を使い
    （同じ画像を2回処理しない）、この画像のプレビューを依頼する直前に next_item() の画像の先読みを始める。"""
    pf = opts.get("_prefetch")
    prep = None
    if pf is not None and item_key is not None:
        prep = pf.take(item_key, log)
    if prep is None:
        prep = _prepare_one(path, opts, log)
    return _finish_one(prep, out_path, opts, log, pf, next_item)


def _prepare_one(path, opts, log):
    """process_one の前半（プレビュー前）: 読み込み → 顔検出 → 背景除去 → 顔トリミング。
    ファイルは書かない。先読みスレッドから呼ばれたときは、ログ・集計もその場では反映しない（_prefetch_ctx）。"""
    from PIL import Image, ImageOps
    _set_stage("read")
    # スマホ/一眼のJPEGはEXIFに回転情報を持つ。適用しないと横倒しのまま処理され、
    # 顔検出も失敗して写真全体が90度傾いたまま出力される。
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    debug = opts.get("debug")
    # 判定はクロップ『前』に行う。crop_around_face は画像外にはみ出した分を
    # 透明でパディングするため、クロップ後だと普通の写真も透過済みに見えてしまう。
    already_cutout = is_already_cutout(img)

    # --- 1. 顔検出（元画像に対して。背景除去してからでは精度が落ちる）---
    _set_stage("detect")
    det = detect_face_box(img, log) if opts.get("face_crop", True) else None
    if det is not None and opts.get("auto_level", True):
        img, det = straighten_face(img, det, already_cutout, log)
    if det is not None:
        box, eye_mid_y, eye_mid_x, nose_x, eye_angle, mouth_mid_y = det
        if debug:
            log(t(f"  [debug] 顔box={box} eye_y={eye_mid_y} eye_x={eye_mid_x} nose_x={nose_x} mouth_y={mouth_mid_y} angle={eye_angle}",
                  f"  [debug] face box={box} eye_y={eye_mid_y} eye_x={eye_mid_x} nose_x={nose_x} mouth_y={mouth_mid_y} angle={eye_angle}", f'  [debug] yüz kutusu={box} eye_y={eye_mid_y} eye_x={eye_mid_x} nose_x={nose_x} mouth_y={mouth_mid_y} açı={eye_angle}'))
    elif debug and opts.get("face_crop", True):
        log(t("  [debug] 顔を検出できませんでした（全体を使用）",
              "  [debug] face not detected (using full image)", '  [debug] yüz algılanamadı (görüntünün tamamı kullanılıyor)'))

    # --- 2. 背景除去（サイズは変わらない）---
    if opts.get("bg_removal", True):
        if already_cutout:
            # 既に背景が抜かれている画像を再処理すると、透明部＝黒として扱われ
            # 黒髪などが背景と誤認されて欠ける。そのまま通す。
            log(t("  [i] 入力が既に背景除去済みのため、背景除去をスキップします",
                  "  [i] Input is already cut out; skipping background removal.", '  [i] Girdinin arka planı zaten kaldırılmış; arka plan kaldırma atlanıyor.'))
            _count_result("bg_already_cutout")
        else:
            _set_stage("bg")
            img = remove_background(img, opts.get("rembg_model", "isnet-general-use"),
                                    alpha_matting=opts.get("alpha_matting", False), log=log,
                                    removebg_key=opts.get("removebg_key"),
                                    face_box=det[0] if det is not None else None)

    # --- 3. 顔トリミング（ここで透過が使えるのでシルエット基準の水平補正が効く）---
    _set_stage("edit")
    cutout = img  # 背景除去後・トリミング前。保存前プレビューはこの画像を編集する。
    crop_meta = {}
    if det is not None:
        img, crop_meta = crop_around_face(
            img, box, opts.get("face_size", 1.37), opts.get("face_neck", FACE_NECK),
            log=log, eye_mid_y=eye_mid_y, eye_mid_x=eye_mid_x,
            nose_x=nose_x, mouth_mid_y=mouth_mid_y, return_meta=True)
    return {"img": img, "cutout": cutout, "crop_meta": crop_meta}


def _finish_one(prep, out_path, opts, log, pf=None, next_item=None):
    """process_one の後半: 保存前プレビュー → アップスケール → リサイズ → PNG保存。処理スレッドだけが呼ぶ。"""
    from PIL import Image
    _set_stage("edit")
    img, cutout, crop_meta = prep["img"], prep["cutout"], prep["crop_meta"]
    # 出力サイズ: プレビューなしは設定値。保存前プレビューでは None で、プレビューで選んだサイズを使う
    size = parse_output_size(opts.get("size"))

    # --- 4. 保存前プレビュー（トリミング前の画像で襟の移動と枠の調整）---
    # アップスケールより前に行うので、スキップした画像でAI高画質化を無駄に回さない。
    preview = opts.get("_preview")
    if preview is not None:
        auto_rect = crop_meta.get("rect") or default_crop_rect(cutout, opts.get("fit", "contain"))
        protect_box = collar_protect_box(crop_meta.get("face_box"), crop_meta.get("chin_y"),
                                         cutout.size)
        face_box = crop_meta.get("face_box")
        # 手動回転は顔の中心（顔が無ければ自動枠の中心）を軸にする。保存時も同じ軸を使う。
        if face_box is not None:
            rotate_center = (face_box[0] + face_box[2] / 2.0, face_box[1] + face_box[3] / 2.0)
        else:
            rotate_center = (auto_rect[0] + auto_rect[2] / 2.0, auto_rect[1] + auto_rect[2] / 2.0)
        request = {
            "image": cutout.copy(),
            "rect": tuple(auto_rect),
            "protect_box": protect_box,
            "face_cx": None if face_box is None else face_box[0] + face_box[2] / 2.0,
            "rotate_center": rotate_center,
            "size": size,
            # 顔トリミングしていない画像を stretch で出力する場合、枠と回転が初期状態なら
            # 画像全体を正方形に引き伸ばして保存する（従来どおり）。プレビューもその見た目にする。
            "stretch_full": (not crop_meta.get("rect")
                             and opts.get("fit", "contain") == "stretch"),
        }
        if pf is not None and next_item is not None:
            # このプレビューを開いている間に、次にプレビューを開く画像1枚の準備を進める
            try:
                nxt = next_item()
                if nxt is not None:
                    pf.start(nxt[0], nxt[1])
            except Exception:  # noqa: BLE001
                pass
        preview_result = preview(request)
        decision, edit = preview_result, None
        if isinstance(preview_result, tuple):
            decision, edit = preview_result
        if decision not in ("save", "skip", "cancel"):
            # 想定外の値で保存へ進まない（保存はその画像のプレビューで「保存」を押したときだけ）
            decision, edit = "cancel", None
        if decision == "cancel":
            abort = (edit or {}).get("abort") if isinstance(edit, dict) else None
            if abort:
                log("  [!] " + abort)
            else:
                log(t("  [i] プレビューでキャンセルされたため、保存しませんでした",
                      "  [i] Preview was cancelled; the image was not saved", '  [i] Önizleme iptal edildi; görüntü kaydedilmedi'))
            cancel = opts.get("_cancel")
            if cancel is not None:
                cancel.set()
            # 確認できずに中止した画像は、利用者のキャンセルとは別に数える
            return "abort" if abort else "cancel"
        if decision == "skip":
            log(t("  [i] プレビューでスキップされたため、保存しませんでした",
                  "  [i] Skipped in preview; the image was not saved", '  [i] Önizlemede atlandı; görüntü kaydedilmedi'))
            return "skip"
        if edit:
            size = parse_output_size(edit.get("size")) or size
            if protect_box is None and edit.get("moves"):
                log(t("  [!] 顎の位置を確認できないため、襟の移動は反映しませんでした",
                      "  [!] The chin position could not be confirmed, so the collar moves were not applied",
                      "  [!] Çene konumu doğrulanamadığı için yaka taşıma uygulanmadı"))
                edit = preview_edit_without_unprotected_moves(edit, protect_box)
            edited = render_preview_edit(cutout, edit, auto_rect, protect_box, rotate_center)
            if edited is not None:
                img = edited
                n_moves = len(edit.get("moves") or [])
                angle = clamp_preview_angle(edit.get("angle", 0.0))
                log(t(f"  [i] プレビューの編集を反映しました（襟の移動 {n_moves} 回・回転 {angle:+.1f}°・枠 {edited.width}px）",
                      f"  [i] Applied preview edits ({n_moves} collar move(s), {angle:+.1f}° rotation, {edited.width}px frame)",
                      f"  [i] Önizleme düzenlemeleri uygulandı ({n_moves} yaka taşıma, {angle:+.1f}° döndürme, {edited.width} piksel çerçeve)"))

    if size is None:
        # 出力サイズが決まっていない（プレビューでサイズを選ばずに保存へ進んだ）。既定値を推測して保存しない
        raise ValueError("output size was not chosen")

    # --- 5. アップスケール（切り抜き後なので小さく済む）---
    _set_stage("upscale")
    if opts.get("upscale", True):
        img = upscale(img, float(opts.get("scale", 1.0)),
                      opts.get("esrgan_model", "RealESRGAN_x4plus"),
                      opts.get("ai_upscale", False), log)

    _set_stage("save")
    img = fit_to_size(img, size, opts.get("fit", "contain"))
    _atomic_save_png(img, out_path)
    # 診断用の寸法は設定値ではなく、保存したファイルから読む
    try:
        with Image.open(out_path) as saved:
            saved_size = saved.size
    except Exception:  # noqa: BLE001
        saved_size = img.size
    _count_output_size(saved_size)
    log(t(f"  [i] 保存: {saved_size[0]}×{saved_size[1]} px",
          f"  [i] Saved: {saved_size[0]}×{saved_size[1]} px",
          f"  [i] Kaydedildi: {saved_size[0]}×{saved_size[1]} px"))
    return True


def folder_precheck(input_dir, output_dir):
    """処理を始める前に分かる「開始できない理由」の区分（入力先なし・入出力が同じ・対応画像なし）。
    開始できるなら None。判定は process_folder と同じ（サブフォルダがあればペアモードとして中身は後で確かめる）。"""
    in_dir = Path(input_dir)
    out_dir = Path(output_dir)
    if not in_dir.is_dir():
        return "no_input"
    try:
        if in_dir.resolve() == out_dir.resolve():
            return "same_dir"
    except Exception:  # noqa: BLE001
        pass
    try:
        out_res = out_dir.resolve()
    except Exception:  # noqa: BLE001
        out_res = out_dir
    try:
        for d in in_dir.iterdir():
            if d.is_dir():
                try:
                    if d.resolve() == out_res:
                        continue
                except Exception:  # noqa: BLE001
                    pass
                return None
            if d.suffix.lower() in SUPPORTED_EXT:
                return None
    except Exception:  # noqa: BLE001
        return None          # 読めない場合は従来どおり処理側で扱う
    return "no_images"


def process_folder(opts, log, progress=None):
    in_dir = Path(opts["input"]); out_dir = Path(opts["output"])
    if not in_dir.is_dir():
        _mark_not_started("no_input")
        log(t(f"[!] 入力フォルダがありません: {in_dir}", f"[!] Input folder not found: {in_dir}", f'[!] Girdi klasörü bulunamadı: {in_dir}')); return
    try:
        if in_dir.resolve() == out_dir.resolve():
            _mark_not_started("same_dir")
            log(t("[!] 入力フォルダと出力フォルダは別にしてください。元画像の上書きを防ぐため処理を中止しました。",
                  "[!] Input and output folders must differ. Processing was stopped to protect source images.", '[!] Girdi ve çıktı klasörleri farklı olmalıdır. Kaynak görüntüleri korumak için işlem durduruldu.'))
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

    if subdirs:
        log(t(f"サブフォルダ（ペア）モード: {len(subdirs)} フォルダ", f"Subfolder (pair) mode: {len(subdirs)} folders", f'Alt klasör (çift) modu: {len(subdirs)} klasör'))
        if files:
            # 以前はここが無言だったので、紛れ込んだフォルダ1つで全画像が無視されても気づけなかった
            log(t(f"  [!] 直下の画像 {len(files)} 枚はサブフォルダモードでは無視されます: ",
                  f"  [!] {len(files)} loose image(s) are ignored in subfolder mode: ", f'  [!] Alt klasör modunda doğrudan girdi klasöründeki {len(files)} görüntü yok sayılır: ')
                + ", ".join(f.name for f in files[:10]) + (" …" if len(files) > 10 else ""))
        _run_subfolder_mode(subdirs, out_dir, opts, log, progress)
        return

    if not files:
        _mark_not_started("no_images")
        log(t(f"[!] 対応画像が見つかりません: {in_dir}", f"[!] No supported images found: {in_dir}", f'[!] Desteklenen görüntü bulunamadı: {in_dir}')); return
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
            log(t(f"  [i] OCR使用不可（フォルダ名をIDとして使用）: {e}", f"  [i] OCR unavailable (using folder name as ID): {e}", f'  [i] OCR kullanılamıyor (klasör adı ID olarak kullanılıyor): {e}'))
            reader = None

    pairs = []  # (face_path, id_number, subname)
    for sub in subdirs:
        imgs = sorted(f for f in sub.iterdir() if f.suffix.lower() in SUPPORTED_EXT)
        if not imgs:
            log(t(f"  [!] {sub.name}: 画像なし、スキップ", f"  [!] {sub.name}: no images, skipped", f'  [!] {sub.name}: görüntü yok, atlandı')); continue
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
            log(t(f"  [!] {sub.name}: IDを読み取れず、スキップ", f"  [!] {sub.name}: could not read ID, skipped", f'  [!] {sub.name}: ID okunamadı, atlandı')); _count_result("skipped"); continue
        if not faces:
            log(t(f"  [!] {sub.name}: 顔写真が見つからず、スキップ", f"  [!] {sub.name}: no face image found, skipped", f'  [!] {sub.name}: yüz fotoğrafı bulunamadı, atlandı')); _count_result("skipped"); continue
        # 「一番大きい画像＝顔」はFMのスクショ(1920x1080)が顔写真より大きいので危険。
        # ポートレートらしさ（縦長ほど高スコア）を主、面積を従にして選ぶ。
        face = max(faces, key=_portrait_score)
        log(t(f"  [{sub.name}] 顔: {face.name}  =>  {id_num}.png", f"  [{sub.name}] face: {face.name}  =>  {id_num}.png", f'  [{sub.name}] yüz: {face.name}  =>  {id_num}.png'))
        pairs.append((face, id_num, sub.name))

    if not pairs:
        _mark_not_started("no_pairs")
        log(t("[!] 有効なペアがありませんでした。", "[!] No valid pairs found.", '[!] Geçerli bir çift bulunamadı.')); return

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
                log(t(f"[skip] ID重複: {uid}（{subname}）", f"[skip] duplicate ID: {uid} ({subname})", f'[atlandı] yinelenen ID: {uid} ({subname})')); _count_result("skipped")
                return
            seen.add(uid)
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}", f'[{idx}/{total}] [atlandı] zaten mevcut: {out_name}')); _count_result("skipped"); _count_result("skipped_existing")
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {subname}/{face.name} -> {out_name}")
        if opts.get("_status"): opts["_status"](face.name)
        t0 = time.monotonic()
        try:
            res = process_one(face, out_path, opts, log, item_key=(idx, str(face)),
                              next_item=lambda: _next_preview_item(
                                  [(p[0], p[1]) for p in pairs], idx, seen, seen_lock, out_dir, opts))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}", f'  [!] başarısız: {e}'))
            _count_failure(e)
            res = False
        ok = res is True
        _count_result(_outcome_kind(res))
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)", f'  [i] tamamlandı ({elapsed:.1f} sn)'))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(pairs, 1)))
    else:
        for item in enumerate(pairs, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    cancelled = _enter_finish(opts)      # 画像処理から終了処理へ移る境界（キャンセルの受付をここで確定）
    if progress:
        progress(done[0], total) if cancelled else progress(total, total)
    _finish(uids, out_dir, opts, log)


def _run_filename_mode(files, out_dir, opts, log, progress):
    """ファイル名（拡張子を除く）をそのままIDとして使う。"""
    if not any(f.stem.strip().isdigit() for f in files):
        _mark_not_started("no_numeric")
        log(t("[!] ファイル名がID（数字）になっている画像がありません。「IDを自動で読み取る」がオフのときは、"
              "顔写真の名前を 2000468148.jpg のようにしてください。",
              "[!] No image is named with an ID (digits). With 'Auto-read ID' off, name the face photo like 2000468148.jpg.",
              "[!] Adı ID (rakam) olan görüntü yok. 'ID'yi otomatik oku' kapalıyken yüz fotoğrafını 2000468148.jpg gibi adlandırın."))
        return
    _run_uid_list(files, [f.stem for f in files], out_dir, opts, log, progress,
                  require_digits=True)


def _run_uid_list(files, uid_list, out_dir, opts, log, progress, require_digits=True):
    """「画像 -> UID」が確定しているリストをまとめて処理する共通ルーチン。"""
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
                  f"[skip] ID is not numeric (not a valid FM person ID): {f.name} -> {uid!r}", f'[atlandı] ID sayısal değil (geçerli bir FM kişi ID’si değil): {f.name} -> {uid!r}')); _count_result("skipped")
            return
        with seen_lock:
            if uid in seen:
                log(t(f"[skip] ID重複: {uid}（{f.name}）", f"[skip] duplicate ID: {uid} ({f.name})", f'[atlandı] yinelenen ID: {uid} ({f.name})')); _count_result("skipped")
                return
            seen.add(uid)
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}", f'[{idx}/{total}] [atlandı] zaten mevcut: {out_name}')); _count_result("skipped"); _count_result("skipped_existing")
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {f.name} -> {out_name}")
        if opts.get("_status"): opts["_status"](f.name)
        t0 = time.monotonic()
        try:
            res = process_one(f, out_path, opts, log, item_key=(idx, str(f)),
                              next_item=lambda: _next_preview_item(
                                  items, idx, seen, seen_lock, out_dir, opts,
                                  require_digits=require_digits, strip=True))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}", f'  [!] başarısız: {e}'))
            _count_failure(e)
            res = False
        ok = res is True
        _count_result(_outcome_kind(res))
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)", f'  [i] tamamlandı ({elapsed:.1f} sn)'))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(items, 1)))
    else:
        for item in enumerate(items, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    cancelled = _enter_finish(opts)      # 画像処理から終了処理へ移る境界（キャンセルの受付をここで確定）
    if progress:
        progress(done[0], total) if cancelled else progress(total, total)
    _finish(uids, out_dir, opts, log)


def _run_ocr_mode(files, out_dir, opts, log, progress):
    reader = get_ocr_reader(log)  # easyocr 未導入なら例外
    log(t(f"画像を仕分け中…（{len(files)}枚／横長のスクショのみOCR）", f"Sorting images… ({len(files)} files; OCR only on wide screenshots)", f'Görüntüler sınıflandırılıyor… ({len(files)} dosya; OCR yalnızca geniş ekran görüntülerine uygulanır)'))
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
            log(t(f"  [顔 ] {f.name}", f"  [face] {f.name}", f'  [yüz] {f.name}'))
            faces.append({"path": f, "mtime": mtime})

    if not ids:
        _mark_not_started("no_ids")
        log(t("[!] IDスクショが1枚も見つかりませんでした。ID入りのスクショを入れてください。", "[!] No ID screenshots found. Please include screenshots that show the ID.", '[!] ID içeren ekran görüntüsü bulunamadı. Lütfen ID’nin göründüğü ekran görüntülerini ekleyin.'))
        return
    if not faces:
        _mark_not_started("no_faces")
        log(t("[!] 顔写真が1枚も見つかりませんでした。", "[!] No face images found.", '[!] Yüz fotoğrafı bulunamadı.'))
        return

    pairs, leftover, unpaired_ids = pair_by_time(faces, ids)
    log(t(f"\nペアリング結果: {len(pairs)} 組", f"\nPairing result: {len(pairs)} pair(s)", f'\nEşleştirme sonucu: {len(pairs)} çift'))
    for face, idd in pairs:
        log(t(f"  顔: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png", f"  face: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png", f"  yüz: {face['path'].name}  <->  ID: {idd['path'].name}  =>  {idd['number']}.png"))
    if leftover:
        log(t(f"  [!] 相方の見つからない顔 {len(leftover)} 枚: ",
              f"  [!] {len(leftover)} face(s) with no matching ID: ", f'  [!] Eşleşen ID’si olmayan {len(leftover)} yüz: ')
            + ", ".join(x["path"].name for x in leftover))
        _count_result("skipped", len(leftover))
    if unpaired_ids:
        # 以前はここが黙って捨てられていて、出力されない選手に気づけなかった
        log(t(f"  [!] 相方の見つからないID {len(unpaired_ids)} 件（撮影時刻が"
              f"{int(MAX_PAIR_GAP_SEC)}秒以上離れています）: ",
              f"  [!] {len(unpaired_ids)} ID(s) with no matching face "
              f"(more than {int(MAX_PAIR_GAP_SEC)}s apart): ", f'  [!] Eşleşen yüzü olmayan {len(unpaired_ids)} ID (zaman farkı {int(MAX_PAIR_GAP_SEC)} saniyeden fazla): ')
            + ", ".join(f"{x['number']}({x['path'].name})" for x in unpaired_ids))
    log(t("  ※ 取り違えが無いか、上の対応を一度確認してください。\n", "  * Please double-check the pairings above for mismatches.\n", '  * Lütfen yukarıdaki eşleştirmelerde hata olmadığını kontrol edin.\n'))
    if not pairs:
        _mark_not_started("no_pairs")
        return

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
                log(t(f"[skip] ID重複: {uid}（{face['path'].name}）", f"[skip] duplicate ID: {uid} ({face['path'].name})", f"[atlandı] yinelenen ID: {uid} ({face['path'].name})")); _count_result("skipped")
                return
            seen.add(uid)
        out_name = out_filename(uid, opts)
        out_path = out_dir / out_name
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {out_name}", f"[{idx}/{total}] [skip] exists: {out_name}", f'[{idx}/{total}] [atlandı] zaten mevcut: {out_name}')); _count_result("skipped"); _count_result("skipped_existing")
            with uids_lock: uids.append(uid)
            with done_lock:
                done[0] += 1
                if progress: progress(done[0], total)
            return
        log(f"[{idx}/{total}] {face['path'].name} -> {out_name}")
        if opts.get("_status"): opts["_status"](face["path"].name)
        t0 = time.monotonic()
        try:
            res = process_one(face["path"], out_path, opts, log, item_key=(idx, str(face["path"])),
                              next_item=lambda: _next_preview_item(
                                  [(p[0]["path"], p[1]["number"]) for p in pairs], idx, seen, seen_lock,
                                  out_dir, opts))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}", f'  [!] başarısız: {e}'))
            _count_failure(e)
            res = False
        ok = res is True
        _count_result(_outcome_kind(res))
        with done_lock:
            done[0] += 1
            if progress: progress(done[0], total)
        if ok:
            elapsed = time.monotonic() - t0
            log(t(f"  [i] 完了 ({elapsed:.1f}秒)", f"  [i] done ({elapsed:.1f}s)", f'  [i] tamamlandı ({elapsed:.1f} sn)'))
            with uids_lock: uids.append(uid)

    use_parallel = workers > 1 and not opts.get("_preview")
    if use_parallel:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            list(exe.map(_process, enumerate(pairs, 1)))
    else:
        for item in enumerate(pairs, 1):
            if cancel and cancel.is_set(): break
            _process(item)
    cancelled = _enter_finish(opts)      # 画像処理から終了処理へ移る境界（キャンセルの受付をここで確定）
    if progress:
        progress(done[0], total) if cancelled else progress(total, total)
    _finish(uids, out_dir, opts, log)


def _enter_finish(opts):
    """画像処理から終了処理へ移る境界。キャンセルの受付を締め切り、受け付けていたかを一度だけ確定して返す。
    何度呼んでも同じ結果（画像のループの直後と _finish の先頭の両方で呼ぶ）。画面には一度だけ知らせる。"""
    cancelled = close_cancel(opts.get("_cancel"))
    cb = opts.get("_finishing")
    if cb is not None and not opts.get("_finishing_sent"):
        opts["_finishing_sent"] = True
        try:
            cb(cancelled)
        except Exception:  # noqa: BLE001
            pass
    return cancelled


def _finish(uids, out_dir, opts, log):
    # 境界：ここより後のキャンセル要求は受け付けない。ログ・画面・共有診断はこの確定結果を使う
    cancelled = _enter_finish(opts)
    # 画像のループを抜けた（完了・キャンセル・放置終了など）。残っている先読みは使わない。ここでは待たず、
    # PNG以外の書き込み（config.xml・処理ログ）を先に済ませてから、最後に先読みスレッドの終了を待つ
    prefetch = opts.get("_prefetch")
    if prefetch is not None:
        prefetch.discard(log)
    log_rembg_batch_summary(log)
    with _batch_stats_lock:
        saved = _batch_stats.get("saved", 0)
        failed = _batch_stats.get("failed", 0)
        zero_rec = {k: _batch_stats.get(k, 0) for k in ("saved", "failed", "skipped", "preview_skipped",
                                                        "skipped_existing", "preview_cancelled", "preview_aborted")}
        abort_kind = _batch_preview_abort[0]
        fail_kinds = dict(_batch_fail_kinds)
    if failed:
        log(t(f"[!] 失敗 {failed} 枚（理由: {format_fail_kinds(fail_kinds)}）",
              f"[!] {failed} image(s) failed (reasons: {format_fail_kinds(fail_kinds)})",
              f"[!] {failed} görüntü başarısız (nedenler: {format_fail_kinds(fail_kinds)})"))
    if saved:
        log(t("保存したPNGの実寸: ", "Saved PNG actual size: ", "Kaydedilen PNG gerçek boyutu: ")
            + format_size_counts(_batch_size_counts()))
    # config.xml：write_config が実際に行った操作（生成・再生成・上書き・更新・変更なし・失敗、バックアップ）を先に記録し、
    # 最後の結果行に含める。uids には「既存のためスキップ」した分も入る（config.xml に載せるため）
    cfg_res = None
    if opts["make_config"]:
        cfg = out_dir / "config.xml"
        if not uids:
            cfg_res = {"result": "unchanged", "reason": "no_ids", "added": 0, "total": None, "backup": False}
            log(t("config.xml: 書き込むIDがないため変更していません", "config.xml: not changed (no IDs to write)",
                  "config.xml: değiştirilmedi (yazılacak ID yok)"))
        else:
            res = write_config(uids, cfg, opts.get("config_append", True), opts.get("newgen", False))
            _log_config_result(res, cfg, opts.get("newgen", False), log)
            cfg_res = {k: v for k, v in res.items() if not k.startswith("_")}   # パス・例外は記録に残さない
    with _batch_stats_lock:
        _batch_config[0] = cfg_res
    # 最後の処理結果：状態・枚数・config.xml の結果。画面の状態表示・共有診断と同じ判定・同じ文（run_result_line）
    if abort_kind:
        status = "stopped"
    elif cancelled:
        status = "cancelled"
    else:
        status = zero_saved_status(zero_rec) or "done"
    end_rec = dict(zero_rec, status=status, preview_abort=abort_kind, config=cfg_res)
    warn = status in ("stopped", "all_failed", "failed_skipped") or (cfg_res or {}).get("result") == "failed"
    log(("\n[!] " if warn else "\n") + run_result_line(end_rec) + f" -> {out_dir}")
    # ログファイルを保存
    lines = opts.get("_log_lines")
    if lines is not None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        log_path = out_dir / f"processing_log_{ts}.txt"
        try:
            log_path.write_text("\n".join(lines), encoding="utf-8")
            log(t(f"[i] ログを保存しました: {log_path.name}", f"[i] Log saved: {log_path.name}", f'[i] Günlük kaydedildi: {log_path.name}'))
        except Exception as e:  # noqa: BLE001
            log(t(f"[!] ログ保存失敗: {e}", f"[!] Log save failed: {e}", f'[!] Günlük kaydedilemedi: {e}'))
    if prefetch is not None:
        prefetch.shutdown(log)


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


def set_app_icon(window):
    """ルートと子ウィンドウで共通のアプリアイコンを使う。"""
    icon_path = resource_path(APP_ICON_FILENAME)
    if not icon_path:
        return
    try:
        window.iconbitmap(default=str(icon_path))
    except Exception:  # noqa: BLE001
        pass


def set_windows_app_id():
    """タスクバーとピン留めで常に同じアプリとして扱わせる。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
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
FACE_NECK = 0.18               # 顎を上端約82%に置き、下側に首・襟・肩を残す
# 旧バージョンの設定ファイル（プリセット番号）からの移行用
LEGACY_ZOOM_TO_SIZE = {0: 1.37, 1: 1.25}


class App(tk.Tk):
    SETTINGS_PATH = Path.home() / ".fm_face_processor.json"

    def __init__(self):
        set_windows_app_id()
        super().__init__()
        set_app_icon(self)
        self.title(f"FM Face Processor {APP_VERSION}")
        self.geometry("720x800")
        self.minsize(560, 520)
        self.q = queue.Queue()
        self.mode = "dark"
        # lang_pref は設定値（ja / en / tr / system）、lang は実際に表示する言語。
        # 設定ファイルに言語が無いときは「Windowsに合わせる」。
        self.lang_pref = LANG_SYSTEM
        self.lang = "en"
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
        self.lang = resolve_language(self.lang_pref)
        set_lang(self.lang)
        self.outer = None
        self._build()
        self.apply_theme(self.mode)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(100, self._poll)
        self._first_help_id = None
        if not self.help_seen:
            self._first_help_id = self.after(350, self._show_first_run_help)

    # ---------- tk変数（1度だけ作る。再ビルドでも値が残る）----------
    def _init_vars(self):
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar(value="processed")
        # 出力サイズはプレビューで画像ごとに選ぶ（メイン画面の欄は無い）。並列処理数の欄も無い（画像は必ず
        # 1枚ずつ処理する）。旧版の設定ファイルの "size"・"preview"・"workers" は使わず、設定を保存するときに
        # そのまま書き戻すだけにする（_legacy_settings）
        self._legacy_settings = {}
        self.scale_var = tk.DoubleVar(value=4.0)
        self.fit_var = tk.StringVar(value="contain")
        self.model_var = tk.StringVar(value="isnet-general-use")
        self.matting_var = tk.BooleanVar(value=False)
        self.rembg_device_var = tk.StringVar(value=REMBG_DEVICE_CPU)   # 初回はCPU
        self.dml_fp16_var = tk.BooleanVar(value=False)                 # GPU省メモリ（FP16）。初期値オフ
        self.model_var.trace_add("write", lambda *_: self._sync_device_ui())
        self.rembg_device_var.trace_add("write", lambda *_: self._sync_device_ui())
        self.ocr_var = tk.BooleanVar(value=True)
        self.facecrop_var = tk.BooleanVar(value=True)
        self.auto_level_var = tk.BooleanVar(value=True)
        self.upscale_var = tk.BooleanVar(value=True)
        self.ai_var = tk.BooleanVar(value=False)
        self.bg_var = tk.BooleanVar(value=True)
        self.bg_var.trace_add("write", lambda *_: self._sync_device_ui())
        self.cfg_var = tk.BooleanVar(value=True)
        # 前提の設定がオフで処理に使われないチェックボックスは、この「オフ表示用」の変数に差し替えて
        # 無効化する。本来の変数（ai_var など）は書き換えないので、前提を戻すと以前の選択が戻り、
        # 設定ファイルにも以前の選択が保存される。
        self._dep_off_vars = {}
        self.append_var = tk.BooleanVar(value=True)
        self.newgen_var = tk.BooleanVar(value=False)
        self.lowpower_var = tk.BooleanVar(value=False)
        self.save_log_var = tk.BooleanVar(value=True)
        self.removebg_var = tk.StringVar(value="")
        self.use_removebg_var = tk.BooleanVar(value=False)
        for var in (self.facecrop_var, self.upscale_var, self.scale_var, self.cfg_var, self.bg_var,
                    self.use_removebg_var, self.removebg_var):
            var.trace_add("write", lambda *_: self._sync_dependencies())
        self.ow_var = tk.BooleanVar(value=False)
        self.debug_var = tk.BooleanVar(value=False)
        self.face_size_var = tk.DoubleVar(value=DEFAULT_FACE_SIZE)
        self.advanced_var = tk.BooleanVar(value=False)
        self.help_seen = False
        self.saved_window_size = ""
        self._test_results = {}      # 直近の GPUテスト / CPUテスト（共有用の項目だけ）
        self._pending_test = None

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
            "mode": self.mode, "lang": self.lang_pref,
            "face_size": self._get_num(self.face_size_var, DEFAULT_FACE_SIZE),
            "input": self.in_var.get(), "output": self.out_var.get(),
            "scale": self._get_num(self.scale_var, 1.0),
            "fit": self.fit_var.get(), "model": self.model_var.get(),
            "matting": self.matting_var.get(), "ocr": self.ocr_var.get(),
            "rembg_device": self.rembg_device_var.get(),
            "dml_fp16": bool(self.dml_fp16_var.get()),
            "facecrop": self.facecrop_var.get(), "upscale": self.upscale_var.get(),
            "auto_level": self.auto_level_var.get(),
            "ai": self.ai_var.get(), "bg": self.bg_var.get(), "cfg": self.cfg_var.get(),
            "append": self.append_var.get(), "newgen": self.newgen_var.get(),
            "lowpower": self.lowpower_var.get(),
            "save_log": self.save_log_var.get(),
            "use_removebg": self.use_removebg_var.get(),
            "overwrite": self.ow_var.get(),
            "debug": self.debug_var.get(),
            "show_advanced": self.advanced_var.get(),
            "help_seen": self.help_seen,
        }
        try:
            if self.state() == "normal":
                settings["window_size"] = f"{self.winfo_width()}x{self.winfo_height()}"
        except Exception:  # noqa: BLE001
            pass
        # APIキーはこのPC内の設定ファイルだけに保存する。
        # 空欄で保存した場合は、旧版が保存したキーを勝手に削除しない。
        removebg_key = self.removebg_var.get().strip()
        if removebg_key:
            settings["removebg_key"] = removebg_key
        # 旧版の "size"・"preview"・"workers" は使わないが、消さずに読んだときの値のまま残す（旧版で開いても値が変わらない）
        for key, value in self._legacy_settings.items():
            settings.setdefault(key, value)
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
        # 手動で選んだ言語（既存の設定ファイルを含む）を優先する。
        # "system" または未設定なら、起動のたびにWindowsの表示言語から決める。
        if d.get("lang") in LANGUAGES or d.get("lang") == LANG_SYSTEM:
            self.lang_pref = d["lang"]
        self.help_seen = bool(d.get("help_seen", False))
        window_size = d.get("window_size", "")
        if isinstance(window_size, str) and re.fullmatch(r"\d{3,4}x\d{3,4}", window_size):
            self.saved_window_size = window_size
            try:
                self.geometry(window_size)
            except Exception:  # noqa: BLE001
                pass
        # 旧形式（プリセット番号）からの移行
        if "face_size" not in d and "zoom_idx" in d:
            try:
                d["face_size"] = LEGACY_ZOOM_TO_SIZE.get(int(d["zoom_idx"]), DEFAULT_FACE_SIZE)
            except (TypeError, ValueError):
                pass

        # 旧版の出力サイズ・「保存前にプレビュー」・並列処理数は画面にも処理にも使わない（"preview": false や
        # "workers": 8 でも、画像は1枚ずつプレビューで確認して保存する）。保存時に同じ値を書き戻すためだけに覚えておく
        for key in ("size", "preview", "workers"):
            if key in d:
                self._legacy_settings[key] = d[key]
        if d.get("rembg_device") in REMBG_DEVICES:
            self.rembg_device_var.set(d["rembg_device"])
        if isinstance(d.get("dml_fp16"), bool):
            self.dml_fp16_var.set(d["dml_fp16"])
        for key, var in (
            ("input",       self.in_var),
            ("output",      self.out_var),
            ("fit",         self.fit_var),
            ("model",       self.model_var),
            ("face_size",   self.face_size_var),
            ("scale",       self.scale_var),
            ("use_removebg", self.use_removebg_var),
            ("matting",     self.matting_var),
            ("ocr",         self.ocr_var),
            ("facecrop",    self.facecrop_var),
            ("auto_level",  self.auto_level_var),
            ("upscale",     self.upscale_var),
            ("ai",          self.ai_var),
            ("bg",          self.bg_var),
            ("cfg",         self.cfg_var),
            ("append",      self.append_var),
            ("newgen",      self.newgen_var),
            ("overwrite",   self.ow_var),
            ("lowpower",    self.lowpower_var),
            ("save_log",    self.save_log_var),
            ("debug",       self.debug_var),
            ("show_advanced", self.advanced_var),
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

    def _flow(self, frame, gap=10, pady=2):
        """frame の子を作成順に横へ並べ、frame の幅に入りきらない分は次の行へ折り返す。"""
        # grid だと折り返した行どうしで列幅がそろって隙間ができるので、place で行ごとに詰めて置く
        state = {"layout": None}

        def relayout(width=None):
            if width is None:
                width = frame.winfo_width()
            if width <= 1:
                width = 10 ** 6
            rows = [[]]
            x = 0
            for w in frame.winfo_children():
                if getattr(w, "_fm_hidden", False):
                    w.place_forget()          # 隠した項目は並べない
                    continue
                need = w.winfo_reqwidth()
                if rows[-1] and x + gap + need > width:
                    rows.append([]); x = 0
                x += (gap if rows[-1] else 0) + need
                rows[-1].append(w)
            layout = []
            y = 0
            for row in rows:
                h = max((w.winfo_reqheight() for w in row), default=0)
                x = 0
                for w in row:
                    layout.append((w, x, y + pady + (h - w.winfo_reqheight()) // 2))
                    x += w.winfo_reqwidth() + gap
                y += h + 2 * pady
            key = (tuple((str(w), x, yy) for w, x, yy in layout), y)
            if key == state["layout"]:
                return
            state["layout"] = key
            for w, x, yy in layout:
                w.place(x=x, y=yy)
            frame.configure(height=max(1, y))
        relayout(10 ** 6)
        frame._fm_relayout = relayout
        frame.bind("<Configure>", lambda e: relayout(e.width), add="+")
        for child in frame.winfo_children():
            # フォントやテーマが変わって子の大きさが変わったときも並べ直す
            child.bind("<Configure>", lambda e: relayout(), add="+")

    def _pairs(self, frame, n_pairs, gap=16, pady=2):
        """先頭 n_pairs 個は2列の表に、残りは1項目1行に並べる。幅が足りないときは全部1列にする。"""
        state = {"cols": None}

        def relayout(width):
            kids = frame.winfo_children()
            pairs, rest = kids[:n_pairs], kids[n_pairs:]
            col0 = max((w.winfo_reqwidth() for w in pairs[0::2]), default=0)
            col1 = max((w.winfo_reqwidth() for w in pairs[1::2]), default=0)
            cols = 2 if col0 + gap + col1 <= width else 1
            if cols == state["cols"]:
                return
            state["cols"] = cols
            for i in range(2):
                frame.columnconfigure(i, weight=1 if cols == 2 else 0)
            for i, w in enumerate(pairs):
                if cols == 2:
                    w.grid(row=i // 2, column=i % 2, columnspan=1, sticky="w", pady=pady,
                           padx=((gap if i % 2 else 0), 0))
                else:
                    w.grid(row=i, column=0, columnspan=2, sticky="w", pady=pady, padx=0)
            base = (len(pairs) + 1) // 2 if cols == 2 else len(pairs)
            for j, w in enumerate(rest):
                w.grid(row=base + j, column=0, columnspan=2, sticky="w", pady=pady)
        relayout(10 ** 6)
        frame.bind("<Configure>", lambda e: relayout(e.width), add="+")

    def _see_status(self):
        """状態欄の全文が見えるように、必要ならメイン画面を縦にスクロールする（内容がウィンドウより高いときだけ）。
        状態欄がウィンドウより高いときは先頭を見せる。"""
        try:
            canvas, outer, lbl = self.ui_canvas, self.outer, self.status_lbl
            self.update_idletasks()
            self.update_idletasks()          # 折り返し（wraplength の追従）で変わった高さを反映する
            total, view_h = outer.winfo_height(), canvas.winfo_height()
            if total <= view_h or view_h <= 1:
                return
            top = lbl.winfo_rooty() - outer.winfo_rooty()
            bottom = top + lbl.winfo_height()
            y0 = canvas.canvasy(0)
            if top >= y0 and bottom <= y0 + view_h:
                return
            target = min(bottom + 8 - view_h, top - 8)
            target = max(0, min(target, total - view_h))
            canvas.yview_moveto(target / total)
        except Exception:  # noqa: BLE001
            pass

    def _wrap_label(self, parent):
        """親の幅に合わせて折り返す小さな案内ラベル（狭いウィンドウでも文字が切れない）。"""
        lbl = ttk.Label(parent, style="Sub.TLabel", justify="left")

        def _fit(event):
            width = max(120, event.width - 4)
            if str(lbl.cget("wraplength")) != str(width):
                lbl.configure(wraplength=width)
        lbl.bind("<Configure>", _fit)
        return lbl

    def _sync_device_ui(self):
        """背景透過の処理先・モデルの選択に合わせて、GPUの案内・FP16の表示を切り替える。
        - GPUを選んだとき：短い案内（対応モデルならGPU向けの案内、非対応モデルならCPUで処理する旨）
        - FP16：GPU ＋ birefnet-general-lite のときだけ表示"""
        try:
            hint = getattr(self, "_gpu_hint", None)
            if hint is None or not hint.winfo_exists():
                return
            # 背景透過がオフのときは処理先自体を使わないので、GPUの案内・FP16も出さない
            gpu = self.rembg_device_var.get() == REMBG_DEVICE_DML and bool(self.bg_var.get())
            model = self.model_var.get()
            anchor = self._device_anchor
            if gpu:
                if model in DML_MODELS:
                    text = t("高性能GPU向け。RTX 4080 SUPER（16GB）で動作確認済み",
                             "For high-performance GPUs. Tested on RTX 4080 SUPER (16 GB).",
                             "Yüksek performanslı GPU'lar için. RTX 4080 SUPER (16 GB) ile test edildi.")
                else:
                    names = " / ".join(DML_MODELS)
                    text = t(f"このモデルはCPUで処理します（GPU対応: {names}）",
                             f"This model runs on CPU (GPU supports: {names})",
                             f"Bu model CPU'da çalışır (GPU desteği: {names})")
                hint.configure(text=text)
                hint.pack(fill="x", pady=(0, 4), before=anchor)
            else:
                hint.pack_forget()
            if gpu and model in DML_FP16_MODELS:
                self._fp16_row.pack(fill="x", pady=(0, 4), before=anchor)
            else:
                self._fp16_row.pack_forget()
            self.after_idle(self._on_content_configure)
        except Exception:  # noqa: BLE001
            pass

    def _on_removebg_toggle(self):
        """remove.bg チェックボックスのトグル：キー入力欄・案内の表示を切り替える"""
        self._sync_dependencies()

    def _dep_check(self, chk, real_var, active):
        """前提の設定がオフで処理に使われないチェックボックスを、オフ表示で無効にする。
        本来の変数は書き換えないので、前提を戻すと以前の選択がそのまま表示される。"""
        if chk is None or not chk.winfo_exists():
            return
        if active:
            if str(chk.cget("variable")) != str(real_var):
                chk.configure(variable=real_var)
            chk.state(["!disabled"])
        else:
            off = self._dep_off_vars.get(str(chk))
            if off is None:
                off = self._dep_off_vars[str(chk)] = tk.BooleanVar(value=False)
            if str(chk.cget("variable")) != str(off):
                chk.configure(variable=off)
            chk.state(["disabled"])

    def _dep_widget(self, widget, active, enabled_state=None):
        """入力欄・コンボボックス・ラジオボタンを、前提の設定に合わせて有効／無効にする。
        sv_ttk では無効にした入力欄・コンボボックスの文字が白いままで区別できないため、
        テーマの「無効」の文字色を入力欄自体に設定する（有効に戻すと消す）。"""
        if widget is None or not widget.winfo_exists():
            return
        if enabled_state is not None:
            widget.configure(state=enabled_state if active else "disabled")
        else:
            widget.state(["!disabled"] if active else ["disabled"])
        cls = widget.winfo_class()
        if cls in ("TEntry", "TCombobox"):
            color = ""
            if not active:
                try:
                    color = self.style.lookup(cls, "foreground", ["disabled"]) or ""
                except Exception:  # noqa: BLE001
                    color = ""
            try:
                widget.configure(foreground=color)
            except Exception:  # noqa: BLE001
                pass

    def _sync_dependencies(self):
        """処理の依存関係を画面に反映する。
        - 顔トリミングがオフ → 顔を検出しないので、自動水平補正と顔サイズは使われない
        - 拡大がオフ（または倍率1以下）→ 拡大しないので、AI高画質化は使われない（倍率欄は拡大オフで無効）
        - config.xml を作らない → 「config.xml に書き足す」は使われない
        - 背景透過がオフ → 背景モデル・髪のフチ・処理先・FP16・remove.bg は使われない
        - remove.bg を優先 → CPU／GPU は「ローカル背景透過の処理先」。いつ使うかを案内に出す"""
        try:
            ai_chk = getattr(self, "_ai_chk", None)
            if ai_chk is None or not ai_chk.winfo_exists():
                return
            face = bool(self.facecrop_var.get())
            up = bool(self.upscale_var.get())
            try:
                scale_ok = float(self.scale_var.get()) > 1.0
            except Exception:  # noqa: BLE001
                scale_ok = True   # 入力途中（空欄など）は判断しない
            bg = bool(self.bg_var.get())
            cfg = bool(self.cfg_var.get())
            use_rb = bool(self.use_removebg_var.get())
            has_key = bool(self.removebg_var.get().strip())

            self._dep_check(self._auto_level_chk, self.auto_level_var, face)
            self._dep_widget(self._face_size_entry, face)
            self._dep_widget(self._scale_entry, up)
            self._dep_check(ai_chk, self.ai_var, up and scale_ok)
            self._dep_check(self._append_chk, self.append_var, cfg)
            self._dep_check(self._matting_chk, self.matting_var, bg)
            self._dep_check(self._removebg_chk, self.use_removebg_var, bg)
            self._dep_check(self._fp16_chk, self.dml_fp16_var, bg)
            for rb in self._device_radios:
                self._dep_widget(rb, bg)
            self._dep_widget(self._model_combo, bg, enabled_state="readonly")

            # remove.bg のキー入力欄（before を指定しないと pack 順の末尾に移動してしまう）
            keyrow = self._apikey_frame
            if bg and use_rb:
                if not keyrow.winfo_manager():
                    ref = getattr(self, "_advanced_checks_frame", None)
                    if ref is not None and ref.winfo_exists():
                        keyrow.pack(fill="x", pady=(0, 4), before=ref)
                    else:
                        keyrow.pack(fill="x", pady=(0, 4))
            else:
                keyrow.pack_forget()

            rb_first = bg and use_rb and has_key
            self._device_lbl.configure(text=(
                t("ローカル背景透過の処理先", "Local background removal device",
                  "Yerel arka plan kaldırma işlem birimi") if rb_first else
                t("背景透過の処理先", "Background removal device", "Arka plan kaldırma işlem birimi")))
            hint = self._removebg_hint
            if bg and use_rb:
                if has_key:
                    text = t("remove.bg を優先します。上のCPU／GPUはローカル背景透過の処理先で、"
                             "remove.bg が使えなかった画像（通信やキーのエラー）と、切り抜きが不完全だった画像の再処理に使います。"
                             "背景モデルと髪のフチの設定も、このローカル処理にだけ使います。",
                             "remove.bg comes first. The CPU/GPU above is for local background removal, used only for images "
                             "remove.bg could not process (network or key errors) and to re-process incomplete cutouts. "
                             "The background model and hair-edge settings also apply only to this local processing.",
                             "Önce remove.bg kullanılır. Yukarıdaki CPU/GPU yerel arka plan kaldırma içindir; yalnızca remove.bg'nin "
                             "işleyemediği görüntülerde (ağ veya anahtar hataları) ve eksik kesimlerin yeniden işlenmesinde kullanılır. "
                             "Arka plan modeli ve saç kenarı ayarları da yalnızca bu yerel işlemde kullanılır.")
                else:
                    text = t("APIキーが未入力のため remove.bg は使われません。すべて上のCPU／GPUで処理します。",
                             "No API key is entered, so remove.bg is not used. All images are processed on the CPU/GPU above.",
                             "API anahtarı girilmediği için remove.bg kullanılmaz. Tüm görüntüler yukarıdaki CPU/GPU'da işlenir.")
                hint.configure(text=text)
                if not hint.winfo_manager():
                    hint.pack(fill="x", pady=(0, 4), after=self._device_anchor)
            else:
                hint.pack_forget()
            self.after_idle(self._on_content_configure)
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self):
        # 実行中に閉じると、daemonスレッドが img.save や config.xml の書き換えの
        # 途中で殺され、壊れたPNGや壊れたconfig.xmlが残る。
        th = getattr(self, "_worker", None)
        if th is not None and th.is_alive():
            if not messagebox.askyesno(
                    t("確認", "Confirm", 'Onay'),
                    t("処理中です。中断して終了しますか？（書き込み中のファイルを待ちます）",
                      "A job is running. Cancel it and quit? (will wait for the file being written)", 'Bir işlem çalışıyor. İptal edip çıkılsın mı? (yazılmakta olan dosya beklenecek)')):
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
        first_help_id = getattr(self, "_first_help_id", None)
        if first_help_id is not None:
            try:
                self.after_cancel(first_help_id)
            except Exception:  # noqa: BLE001
                pass
        self.destroy()

    def _on_content_configure(self, _event=None):
        """スクロール領域を、現在のUI全体の大きさへ合わせる。"""
        canvas = getattr(self, "ui_canvas", None)
        if canvas is not None and canvas.winfo_exists():
            canvas.configure(scrollregion=canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        """埋め込みフレームを表示領域と同じ幅にする。"""
        canvas = getattr(self, "ui_canvas", None)
        item = getattr(self, "_canvas_window", None)
        if canvas is not None and item is not None:
            canvas.itemconfigure(item, width=max(1, event.width))

    def _on_main_mousewheel(self, event):
        """小さいウィンドウでは画面全体をホイールで縦スクロールする。"""
        canvas = getattr(self, "ui_canvas", None)
        outer = getattr(self, "outer", None)
        if canvas is None or outer is None:
            return None

        # ログ欄の上では従来どおりログだけをスクロールする。
        widget = event.widget
        while widget is not None:
            if widget is getattr(self, "log", None):
                return None
            widget = getattr(widget, "master", None)

        if outer.winfo_reqheight() <= canvas.winfo_height():
            return None
        delta = int(-event.delta / 120) if event.delta else 0
        if delta == 0:
            delta = -1 if event.delta > 0 else 1
        canvas.yview_scroll(delta, "units")
        return "break"

    def _sync_advanced_visibility(self):
        frame = getattr(self, "advanced_frame", None)
        button = getattr(self, "advanced_btn", None)
        if frame is None or button is None:
            return
        if self.advanced_var.get():
            frame.pack(fill="x", pady=(6, 0))
            marker = "▲"
        else:
            frame.pack_forget()
            marker = "▼"
        button.config(text=t(f"詳細設定 {marker}", f"Advanced settings {marker}", f'Gelişmiş ayarlar {marker}'))
        self.after_idle(self._on_content_configure)

    def _toggle_advanced(self):
        self.advanced_var.set(not self.advanced_var.get())
        self._sync_advanced_visibility()

    def _show_first_run_help(self):
        self._first_help_id = None
        self.help_seen = True
        self._save_settings()
        self._show_id_help()

    def _open_online_guide(self):
        anchor = {"ja": "%E4%BD%BF%E3%81%84%E6%96%B9", "en": "how-to-use", "tr": "türkçe"}[self.lang]
        try:
            webbrowser.open(f"https://github.com/kumajia/FM-Face-Processor#{anchor}")
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning(
                t("確認", "Notice", 'Uyarı'),
                t(f"ブラウザを開けませんでした: {e}", f"Could not open the browser: {e}", f'Tarayıcı açılamadı: {e}'))

    def _show_id_help(self):
        existing = getattr(self, "_help_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self)
        self._help_window = win
        set_app_icon(win)
        win.title(t("使い方：IDの指定", "Quick guide: providing an ID", 'Hızlı rehber: ID belirtme'))
        win.geometry("560x520")
        win.minsize(440, 400)
        win.transient(self)

        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=t("IDの指定方法は2つあります", "There are two ways to provide an ID", 'ID belirtmenin iki yolu vardır'),
            style="Header.TLabel").pack(anchor="w", pady=(0, 10))

        auto = ttk.LabelFrame(
            body,
            text=t("方法1：FMの画面からIDを自動で読む", "Method 1: Read the ID from FM automatically", 'Yöntem 1: ID’yi FM’den otomatik oku'),
            padding=10)
        auto.pack(fill="x", pady=(0, 10))
        ttk.Label(
            auto,
            text=t(
                "FMの環境設定で「ID」を検索し、スキン作成を補助するための画面ID表示を有効にします。"
                "選手またはスタッフのプロフィールを開き、IDが見えるスクリーンショットと顔写真を同じ入力フォルダへ入れてください。",
                "In FM Preferences, search for 'ID' and enable the option that shows screen IDs for skinning. "
                "Open the player or staff profile, then put a screenshot showing the ID and the face photo in the same input folder.", 'FM Tercihleri’nde “ID”yi arayın ve arayüz tasarımı için ekran ID’lerini gösteren seçeneği etkinleştirin. Oyuncu veya personel profilini açın, ardından ID’nin göründüğü bir ekran görüntüsünü ve yüz fotoğrafını aynı girdi klasörüne koyun.'),
            wraplength=500,
            justify="left").pack(fill="x", anchor="w")

        sample = ttk.LabelFrame(
            auto, text=t("IDスクリーンショットの例", "Example ID screenshot", 'ID içeren ekran görüntüsü örneği'), padding=10)
        sample.pack(fill="x", pady=(10, 0))
        sample_row = ttk.Frame(sample)
        sample_row.pack(fill="x")
        ttk.Label(sample_row, text="Finn Sundstrom 25").pack(side="left")
        ttk.Label(sample_row, text="ID: 2000468148",
                  font=("", 11, "bold")).pack(side="right")
        ttk.Label(
            sample,
            text=t("このように人物のIDが読める状態で画面を撮ります。",
                   "Take the screenshot while the person's ID is readable like this.", 'Ekran görüntüsünü kişinin ID’si bu şekilde okunabilirken alın.'),
            style="Sub.TLabel").pack(anchor="w", pady=(6, 0))

        manual = ttk.LabelFrame(
            body,
            text=t("方法2：スクリーンショットを使わない", "Method 2: Do not use a screenshot", 'Yöntem 2: Ekran görüntüsü kullanmadan'),
            padding=10)
        manual.pack(fill="x", pady=(0, 10))
        ttk.Label(
            manual,
            text=t(
                "「IDを自動で読み取る」をOFFにし、顔写真のファイル名を人物IDにします。\n例：2000468148.jpg",
                "Turn off 'Auto-read ID' and rename the face photo to the person's ID.\nExample: 2000468148.jpg", '“ID’yi otomatik oku” seçeneğini kapatın ve yüz fotoğrafının adını kişinin ID’siyle değiştirin.\nÖrnek: 2000468148.jpg'),
            wraplength=500,
            justify="left").pack(fill="x", anchor="w")

        ttk.Label(
            body,
            text=t("入力と出力には別々のフォルダを選び、準備できたら「実行」を押します。",
                   "Choose separate input and output folders, then click Run.", 'Girdi ve çıktı için ayrı klasörler seçin, ardından Başlat’a tıklayın.'),
            wraplength=500,
            justify="left").pack(fill="x", anchor="w", pady=(0, 10))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", side="bottom")
        ttk.Button(
            buttons,
            text=t("詳しい説明をGitHubで開く", "Open the full guide on GitHub", 'Tam rehberi GitHub’da aç'),
            command=self._open_online_guide).pack(side="left")
        ttk.Button(buttons, text=t("閉じる", "Close", 'Kapat'),
                   command=win.destroy).pack(side="right")

        try:
            bg = self.style.lookup("TFrame", "background")
            if bg:
                win.configure(background=bg)
        except Exception:  # noqa: BLE001
            pass
        win.focus_force()

    def _build(self):
        if getattr(self, "ui_shell", None) is not None:
            self.ui_shell.destroy()

        self.ui_shell = shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        self.ui_canvas = canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0)
        scroll = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.outer = outer = ttk.Frame(canvas, padding=12)
        self._canvas_window = canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", self._on_content_configure)
        canvas.bind("<Configure>", self._on_canvas_configure)
        self.bind("<MouseWheel>", self._on_main_mousewheel)

        # ── ヘッダー ────────────────────────────────────────────
        header = ttk.Frame(outer); header.pack(fill="x", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        titlerow = ttk.Frame(header); titlerow.grid(row=0, column=0, sticky="w")
        lbl = ttk.Label(titlerow, text="FM Face Processor", style="Header.TLabel")
        lbl.pack(side="left")
        lbl2 = ttk.Label(titlerow, text=f"  {APP_VERSION}", style="Ver.TLabel")
        lbl2.pack(side="left")
        # 以前はここに「顔画像＋IDスクショ → 透過PNG ＋ config.xml」の説明行があった。
        # IDの使い方は「使い方」ボタンのガイドと、ログ欄の初期案内で説明する。

        btns = ttk.Frame(header); btns.grid(row=0, column=1, sticky="e")
        lang_choices = [language_menu_label(LANG_SYSTEM)] + list(LANGUAGES.values())
        self.language_var = tk.StringVar(value=language_menu_label(self.lang_pref))
        self.lang_combo = ttk.Combobox(
            btns, textvariable=self.language_var,
            values=tuple(lang_choices), state="readonly",
            width=max(9, max(_display_width(v) for v in lang_choices) + 2))
        self.lang_combo.bind("<<ComboboxSelected>>", self._select_lang)
        self.lang_combo.pack(side="right", padx=(4, 0))
        self.theme_btn = ttk.Button(btns, text="", command=self._toggle_theme, width=3)
        self.theme_btn.pack(side="right")
        self.help_btn = ttk.Button(btns, text=t("使い方", "Guide", 'Rehber'),
                                   command=self._show_id_help)
        self.help_btn.pack(side="right", padx=(0, 4))

        # ── フォルダ選択 ────────────────────────────────────────
        fld = ttk.LabelFrame(outer, text=t("フォルダ", "Folders", 'Klasörler'), padding=8)
        fld.pack(fill="x", pady=(0, 6))
        fld.columnconfigure(1, weight=1)
        ttk.Label(fld, text=t("入力", "Input", 'Girdi')).grid(row=0, column=0, sticky="w", pady=2)
        in_ent = ttk.Entry(fld, textvariable=self.in_var)
        in_ent.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(fld, text="…", command=self._pick_in, width=3).grid(row=0, column=2)
        ttk.Label(fld, text=t("出力", "Output", 'Çıktı')).grid(row=1, column=0, sticky="w", pady=2)
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
                             "You can also drop a folder or images onto this box", 'Bu alana klasör veya görüntü de sürükleyebilirsiniz')
                      ).grid(row=3, column=1, sticky="w", padx=6, pady=(4, 0))

        prow = ttk.Frame(fld); prow.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        # 実行中に押されると処理中のファイルを壊すので、まとめて無効化できるようにする
        self._busy_btns = []
        b = ttk.Button(prow, text=t("ペアフォルダを作る", "Make pair folders", 'Eşleştirme klasörleri oluştur'),
                       command=self._make_pair_folders)
        self._busy_btns.append(b)
        b = ttk.Button(prow, text=t("入力元画像をゴミ箱へ", "Trash source images", 'Kaynak görüntüleri çöpe taşı'),
                       command=self._delete_source_images)
        self._busy_btns.append(b)
        b = ttk.Button(prow, text=t("config.xml の重複を除去", "Clean duplicate IDs in config.xml", 'config.xml’deki yinelenen ID’leri temizle'),
                       command=self._dedupe_config_dialog)
        self._busy_btns.append(b)
        self._flow(prow, gap=8)

        # ── オプション ──────────────────────────────────────────
        opt = ttk.LabelFrame(outer, text=t("オプション", "Options", 'Seçenekler'), padding=8)
        opt.pack(fill="x", pady=(0, 6))

        basic = ttk.Frame(opt)
        basic.pack(fill="x")
        basic.columnconfigure(0, weight=1)
        basic.columnconfigure(1, weight=1)
        ttk.Checkbutton(basic, text=t("IDを自動で読み取る", "Auto-read ID", 'ID’yi otomatik oku'),
                        variable=self.ocr_var).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Checkbutton(basic, text=t("顔を中心に切り抜く", "Crop around face", 'Yüzün etrafını kırp'),
                        variable=self.facecrop_var).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Checkbutton(basic, text=t("画像を拡大する", "Upscale", 'Görüntüyü büyüt'),
                        variable=self.upscale_var).grid(row=1, column=0, sticky="w", pady=2)
        # 顔トリミングがオフのときは顔を検出しないため、自動水平補正も行われない（_sync_dependencies で無効表示）
        self._auto_level_chk = ttk.Checkbutton(basic, text=t("両目を水平に自動補正", "Auto-level eyes", 'Gözleri otomatik hizala'),
                                               variable=self.auto_level_var)
        self._auto_level_chk.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Checkbutton(basic, text=t("背景を消して透過にする", "Remove background (transparent)", 'Arka planı kaldır (şeffaf)'),
                        variable=self.bg_var).grid(row=2, column=0, sticky="w", pady=2)
        # 「保存前にプレビュー」は無い（画像は必ず1枚ずつプレビューで確認して保存する）
        ttk.Checkbutton(basic, text=t("config.xml を作る", "Generate config.xml", 'config.xml oluştur'),
                        variable=self.cfg_var).grid(row=2, column=1, sticky="w", pady=2)

        self.advanced_btn = ttk.Button(opt, command=self._toggle_advanced)
        self.advanced_btn.pack(fill="x", pady=(6, 0))
        self.advanced_frame = advanced = ttk.Frame(opt)

        # 項目のまとまり単位で横に並べ、入りきらないときは次の行へ折り返す（狭いウィンドウ・長い訳語対策）
        row = ttk.Frame(advanced); row.pack(fill="x", pady=(0, 4))
        # 出力サイズの欄は無い（プレビューで 180×180 / 250×250 / 任意 から選ぶ）
        g = ttk.Frame(row)
        ttk.Label(g, text=t("拡大倍率", "Upscale factor", 'Büyütme katsayısı')).pack(side="left")
        self._scale_entry = ttk.Entry(g, textvariable=self.scale_var, width=5)
        self._scale_entry.pack(side="left", padx=(6, 0))
        ttk.Label(g, text="x", style="Sub.TLabel").pack(side="left", padx=(2, 0))
        g = ttk.Frame(row)
        ttk.Label(g, text=t("フィット", "Fit", 'Sığdırma')).pack(side="left")
        ttk.Combobox(g, textvariable=self.fit_var, width=8, state="readonly",
                     values=["contain", "cover", "stretch"]).pack(side="left", padx=(6, 0))
        g = ttk.Frame(row)
        ttk.Label(g, text=t("顔サイズ", "Face size", 'Yüz boyutu')).pack(side="left")
        self._face_size_entry = ttk.Entry(g, textvariable=self.face_size_var, width=5)
        self._face_size_entry.pack(side="left", padx=(6, 0))
        self._flow(row)

        row2 = ttk.Frame(advanced); row2.pack(fill="x", pady=(0, 4))
        g = ttk.Frame(row2)
        ttk.Label(g, text=t("背景モデル", "BG model", 'Arka plan modeli')).pack(side="left")
        self._model_combo = ttk.Combobox(
            g, textvariable=self.model_var, width=18, state="readonly",
            values=["isnet-general-use", "birefnet-general", "birefnet-general-lite",
                    "birefnet-portrait", "u2net_human_seg", "u2net"])
        self._model_combo.pack(side="left", padx=(6, 0))
        self._matting_chk = ttk.Checkbutton(
            row2, text=t("髪のフチをなめらかにする", "Smooth hair edges (alpha matting)", 'Saç kenarlarını yumuşat (alpha matting)'),
            variable=self.matting_var)
        self._flow(row2, gap=16)
        row2d = ttk.Frame(advanced); row2d.pack(fill="x", pady=(0, 4))
        # remove.bg を優先するときは「ローカル背景透過の処理先」と表示する（_sync_dependencies）
        self._device_lbl = ttk.Label(row2d, text=t("背景透過の処理先", "Background removal device",
                                                   "Arka plan kaldırma işlem birimi"))
        self._device_lbl.pack(side="left")
        self._device_radios = []
        rb = ttk.Radiobutton(row2d, text="CPU", value=REMBG_DEVICE_CPU, variable=self.rembg_device_var)
        rb.pack(side="left", padx=(8, 0))
        self._device_radios.append(rb)
        rb = ttk.Radiobutton(row2d, text=t("GPU（DirectML）", "GPU (DirectML)", "GPU (DirectML)"),
                             value=REMBG_DEVICE_DML, variable=self.rembg_device_var)
        rb.pack(side="left", padx=(8, 0))
        self._device_radios.append(rb)
        # GPUを選んだときだけ出す短い案内（選択に応じて文面を切り替える）
        self._gpu_hint = self._wrap_label(advanced)
        # GPU省メモリ（FP16）：GPU ＋ birefnet-general-lite を選んだときだけ表示する
        self._fp16_row = row2e = ttk.Frame(advanced)
        self._fp16_chk = ttk.Checkbutton(row2e, text=t("GPU省メモリ（FP16）", "GPU memory saver (FP16)",
                                                       "GPU bellek tasarrufu (FP16)"),
                                         variable=self.dml_fp16_var)
        self._fp16_hint = ttk.Label(row2e, style="Sub.TLabel",
                                    text=t("VRAMの使用量を減らします", "Uses less VRAM", "Daha az VRAM kullanır"))
        self._flow(row2e, gap=8)
        self._device_anchor = row2b = ttk.Frame(advanced); row2b.pack(fill="x", pady=(0, 4))
        self._removebg_chk = ttk.Checkbutton(
            row2b, text=t("remove.bg API を使う（画像を外部送信）",
                          "Use remove.bg API (uploads the image)", 'remove.bg API kullan (görüntüyü dışarı yükler)'),
            variable=self.use_removebg_var, command=self._on_removebg_toggle)
        self._removebg_chk.pack(side="left")
        # remove.bg を優先するとき、CPU／GPU がいつ使われるかを示す案内（_sync_dependencies で文面と表示を切り替え）
        self._removebg_hint = self._wrap_label(advanced)

        self._apikey_frame = rowk = ttk.Frame(advanced)
        ttk.Label(rowk, text=t("remove.bg APIキー（このPC内だけに保存）",
                               "remove.bg API key (saved only on this PC)", 'remove.bg API anahtarı (yalnızca bu bilgisayarda saklanır)'))
        g = ttk.Frame(rowk)
        self._apikey_entry = ttk.Entry(g, textvariable=self.removebg_var, width=24, show="●")
        self._apikey_entry.pack(side="left")
        self._apikey_show = tk.BooleanVar(value=False)
        def _toggle_key():
            self._apikey_entry.config(show="" if self._apikey_show.get() else "●")
        ttk.Checkbutton(g, text=t("表示", "Show", 'Göster'), variable=self._apikey_show,
                        command=_toggle_key).pack(side="left", padx=(6, 0))
        self._flow(rowk, gap=6)

        chk = self._advanced_checks_frame = ttk.Frame(advanced)
        chk.pack(fill="both", expand=True)
        # 拡大がオフ（または倍率1以下）のときは拡大処理自体を行わないため、AI高画質化も使われない
        self._ai_chk = ttk.Checkbutton(chk, text=t("AI高画質化 (Real-ESRGAN)", "AI upscale (Real-ESRGAN)", 'Yapay zekâ ile büyüt (Real-ESRGAN)'),
                                       variable=self.ai_var)
        self._append_chk = ttk.Checkbutton(chk, text=t("config.xml に書き足す（重複無視）", "Append to config.xml (skip dups)", 'config.xml’ye ekle (yinelenenleri atla)'),
                                           variable=self.append_var)
        ttk.Checkbutton(chk, text=t("作成済みでも作り直す", "Overwrite existing", 'Mevcut dosyaların üzerine yaz'),
                        variable=self.ow_var)
        ttk.Checkbutton(chk, text=t("生成選手 (newgen) に対応する — ID に r- を付ける",
                                    "Add r- prefix for newgen players", 'Yeni üretilen oyuncular (newgen) için r- öneki ekle'),
                        variable=self.newgen_var)
        ttk.Checkbutton(chk, text=t("低負荷モード（他のアプリにCPUを譲る・処理は遅くなる）",
                                    "Low-load mode (yields CPU to other apps; slower)", 'Düşük yük modu (CPU’yu diğer uygulamalara bırakır; daha yavaş)'),
                        variable=self.lowpower_var)
        # 並列処理数の欄は無い（画像は必ず1枚ずつプレビューで確認して保存するため、常に1枚ずつ処理する）
        ttk.Checkbutton(chk, text=t("ログをファイルに保存する（出力フォルダに processing_log_*.txt）",
                                    "Save log to file (processing_log_*.txt in output folder)", 'Günlüğü dosyaya kaydet (çıktı klasörüne processing_log_*.txt)'),
                        variable=self.save_log_var)
        ttk.Checkbutton(chk, text=t("デバッグモード（診断ログを表示）", "Debug mode (show diagnostic logs)", 'Hata ayıklama modu (tanılama günlüklerini göster)'),
                        variable=self.debug_var)
        # 先頭の4つは2列（入りきらないときは1列）、残りは1項目1行
        self._pairs(chk, n_pairs=4)

        self._sync_device_ui()
        self._sync_dependencies()
        self._sync_advanced_visibility()

        act = ttk.Frame(outer); act.pack(fill="x", pady=(0, 8))
        for column in range(4):
            act.columnconfigure(column, weight=1)
        self.run_btn = ttk.Button(act, text=t("実行", "Run", 'Başlat'), style="Accent.TButton", command=self._run)
        self.run_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ttk.Button(act, text=t("キャンセル", "Cancel", 'İptal'),
                                     style="Danger.TButton", command=self._cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.open_btn = ttk.Button(act, text=t("出力フォルダを開く", "Open output folder", 'Çıktı klasörünü aç'),
                                   command=self._open_out, state="disabled")
        self.open_btn.grid(row=0, column=2, sticky="ew", padx=(8, 0))
        _dl = ttk.Button(act, text=t("ログをすべて削除", "Delete all logs", 'Tüm günlükleri sil'),
                         command=self._delete_logs)
        _dl.grid(row=0, column=3, sticky="ew", padx=(8, 0))
        self._busy_btns.append(_dl)

        self.pb = ttk.Progressbar(outer, mode="determinate"); self.pb.pack(fill="x", pady=(0, 4))
        status_text = ""
        fn = getattr(self, "_status_fn", None)
        if fn is not None and not getattr(self, "_running", False):
            try:
                status_text = fn()          # 直前の実行結果を、今の表示言語で出し直す
            except Exception:  # noqa: BLE001
                status_text = ""
        # 状態欄：ウィンドウの幅に合わせて折り返し、枚数・失敗の理由まで全文を読めるようにする（幅を変えても追従）
        self.status_lbl = self._wrap_label(outer)
        self.status_lbl.configure(text=status_text)
        self.status_lbl.pack(fill="x", pady=(0, 4))

        logfrm = ttk.Frame(outer); logfrm.pack(fill="both", expand=True)
        self.log = tk.Text(logfrm, height=12, wrap="word", relief="flat",
                           borderwidth=0, font=LOG_FONT_DEFAULT)
        sb = ttk.Scrollbar(logfrm, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        # 診断用：直近のGPU／CPUテストの結果を共有用テキストでコピーする（通常操作の列からは外す）
        diag = ttk.Frame(outer); diag.pack(fill="x", pady=(6, 0))
        self.copy_btn = ttk.Button(diag, text=t("テスト結果をコピー…", "Copy test results…", "Test sonuçlarını kopyala…"),
                                   command=self._copy_test_results, state="disabled")
        self.copy_btn.pack(side="right")
        ttk.Label(diag, style="Sub.TLabel", text=t("診断用", "For diagnostics", "Tanılama için")
                  ).pack(side="right", padx=(0, 8))
        if self.log_lines:
            self.log.insert("end", "\n".join(self.log_lines))
            self.log.see("end")
            self.log_has_content = True
        else:
            # 初期案内は処理ログとして保持しない。保持すると言語切替後も
            # 切替前の言語の案内が復元されてしまう。
            hint = t(
                "IDの指定方法:\n"
                "1) 顔写真＋「ID: 2000468148」のような表示が見えるFM画面を同じフォルダへ\n"
                "2) または「IDを自動で読み取る」をOFFにして、顔写真を 2000468148.jpg に変更\n"
                "入力と出力に別々のフォルダを選び、「実行」を押してください。",
                "Two ways to provide an ID:\n"
                "1) Put the face photo and an FM screenshot showing an ID such as 'ID: 2000468148' in one folder\n"
                "2) Or turn off 'Auto-read ID' and rename the face photo to 2000468148.jpg\n"
                "Choose separate input and output folders, then press Run.", 'ID belirtmenin iki yolu vardır:\n1) Yüz fotoğrafını ve “ID: 2000468148” gibi bir ID’nin göründüğü FM ekran görüntüsünü aynı klasöre koyun\n2) Veya “ID’yi otomatik oku” seçeneğini kapatıp yüz fotoğrafının adını 2000468148.jpg olarak değiştirin\nGirdi ve çıktı için ayrı klasörler seçin, ardından Başlat’a tıklayın.')
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
                self.run_btn.config(state="disabled", text=t("処理中…", "Working…", 'Çalışıyor…'))
                ev = getattr(self, "_cancel_event", None)
                can_cancel = ev is None or not (ev.is_set() or getattr(ev, "closed", False))
                self.cancel_btn.config(state="normal" if can_cancel else "disabled")
                self.open_btn.config(state="disabled")
            else:
                self.run_btn.config(state="normal", text=t("実行", "Run", 'Başlat'))
                self.cancel_btn.config(state="disabled")
                if getattr(self, "last_out", None):
                    self.open_btn.config(state="normal")
            for b in getattr(self, "_busy_btns", []):
                b.config(state="disabled" if running else "normal")
            if getattr(self, "copy_btn", None) is not None:
                self.copy_btn.config(state="normal" if (not running and self._test_results) else "disabled")
        except Exception:  # noqa: BLE001
            pass

    def _share_text(self, recs, include_pc):
        """共有本文。APIキーと入出力フォルダのパスは、万一混ざっていても伏せる。"""
        secrets = [self.removebg_var.get().strip(), self.in_var.get().strip(), self.out_var.get().strip()]
        for p in (self.in_var.get().strip(), self.out_var.get().strip()):
            if p:
                try:
                    secrets.append(str(Path(p).resolve()))
                except Exception:  # noqa: BLE001
                    pass
        return scrub_share_text(format_test_results(recs, include_pc=include_pc), secrets)

    def _copy_test_results(self):
        """直近のGPUテストとCPUテスト（片方だけでも可）の共有本文を確認画面に出し、
        「コピー」を押したときだけ、表示している本文をそのままクリップボードへ入れる。
        「キャンセル」・Esc・閉じるボタンでは、クリップボードを変えない。"""
        recs = []
        for k in ("gpu", "cpu"):
            r = self._test_results.get(k)
            if r is None:
                continue
            recs.append(r)
            if r.get("status") == "not_started" and r.get("previous"):
                recs.append(dict(r["previous"], _prev=True))
        if not recs:
            return
        old = getattr(self, "_share_dialog", None)
        if old is not None and old.winfo_exists():
            old.lift()
            old.focus_force()
            return
        win = tk.Toplevel(self)
        self._share_dialog = win
        set_app_icon(win)
        win.title(t("テスト結果の確認", "Review test results", "Test sonuçlarını incele"))
        win.transient(self)
        set_titlebar_dark(win, self.mode == "dark")
        body = ttk.Frame(win, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=t("次の本文をコピーします。画像名・フォルダのパス・ユーザー名・APIキー・実行日時は含みません。",
                               "The text below will be copied. It does not include image names, folder paths, "
                               "your user name, API keys or the date and time of the run.",
                               "Aşağıdaki metin kopyalanacak. Görüntü adları, klasör yolları, kullanıcı adınız, "
                               "API anahtarları ve çalıştırma tarihi/saati dahil değildir."),
                  wraplength=560, justify="left").pack(fill="x", anchor="w", pady=(0, 8))
        pc_var = tk.BooleanVar(value=False)       # 開くたびにオフ
        txt_frame = ttk.Frame(body)
        txt_frame.pack(fill="both", expand=True)
        txt = tk.Text(txt_frame, width=78, height=22, wrap="word", relief="flat", borderwidth=0,
                      font=LOG_FONT_DEFAULT)
        try:
            txt.configure(background=self.log.cget("background"), foreground=self.log.cget("foreground"))
        except Exception:  # noqa: BLE001
            pass
        tsb = ttk.Scrollbar(txt_frame, command=txt.yview)
        txt.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        def refresh():
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            txt.insert("1.0", self._share_text(recs, bool(pc_var.get())))
            txt.configure(state="disabled")       # 読み取り専用（表示どおりの本文をコピーする）

        def widget_text():
            return txt.get("1.0", "end-1c")

        def close():
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
            self._share_dialog = None

        def do_copy():
            text = widget_text()
            try:
                self.clipboard_clear()
                self.clipboard_append(text)
                self.update()
            except Exception:  # noqa: BLE001
                close()
                return
            close()
            which = " + ".join(dict.fromkeys("GPU" if r["kind"] == "gpu" else "CPU" for r in recs))
            self.status_lbl.config(text=t(f"テスト結果をコピーしました（{which}）", f"Test results copied ({which})",
                                          f"Test sonuçları kopyalandı ({which})"))

        ttk.Checkbutton(body, text=t("PCの詳細も含める（CPUの型番・論理スレッド数・搭載RAM）",
                                     "Include PC details (CPU model, logical threads, installed RAM)",
                                     "PC ayrıntılarını ekle (CPU modeli, mantıksal iş parçacıkları, kurulu RAM)"),
                        variable=pc_var, command=refresh).pack(anchor="w", pady=(8, 0))
        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text=t("キャンセル", "Cancel", "İptal"), command=close).pack(side="right")
        copy_b = ttk.Button(btns, text=t("コピー", "Copy", "Kopyala"), command=do_copy,
                            style="Accent.TButton" if HAS_SVTTK else "TButton")
        copy_b.pack(side="right", padx=(0, 8))
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda _e: close())
        refresh()
        # 自動検証用に操作を参照できるようにする（画面の動作には影響しない）
        win._fm_share = {"pc_var": pc_var, "refresh": refresh, "text": widget_text, "widget_text": widget_text,
                         "copy": do_copy, "cancel": close, "text_widget": txt}
        win.update_idletasks()
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - win.winfo_reqwidth()) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - win.winfo_reqheight()) // 3)
            win.geometry(f"+{x}+{y}")
        except tk.TclError:
            pass
        win.grab_set()
        copy_b.focus_set()

    def _run_result(self, rec, payload, cancelled, notice, elapsed):
        """実行の終わり：ダイアログとメイン画面の状態表示を、診断結果と同じ状態・同じ文で出す。
        優先順: エラー → 中止（notice）→ キャンセル → 開始できず／処理対象なし → 全件スキップ → 完了。
        背景透過の処理先の切り替え（FP16を使えない／GPUの失敗）は、どの状態でも書き添える。
        状態表示は関数で保持し、言語を切り替えたときも今の言語で出し直す（_build）。"""
        rec = rec or {}

        def notes():
            out = []
            cf = rec.get("config") or {}
            if cf.get("result") == "failed":
                why = config_error_reason(cf.get("error"))
                n_saved = rec.get("saved", 0)
                if n_saved:
                    kept = t(f"保存した画像（{n_saved} 枚）はそのまま残っています。", f"The saved images ({n_saved}) are kept.",
                             f"Kaydedilen görüntüler ({n_saved}) korunur.")
                else:
                    kept = t("出力先のPNGには影響ありません（今回の保存 0 枚）。",
                             "The PNGs in the output folder are not affected (0 saved this run).",
                             "Çıktı klasöründeki PNG'ler etkilenmedi (bu çalıştırmada 0 kaydedildi).")
                if cf.get("stage") == "backup":
                    head = t(f"config.xml のバックアップを作成できなかったため、config.xml を書き換えませんでした（{why}）。"
                             f"元の config.xml はそのままです。",
                             f"A backup of config.xml could not be created, so config.xml was not rewritten ({why}). "
                             f"The original config.xml is unchanged. ",
                             f"config.xml yedeği oluşturulamadığı için config.xml yeniden yazılmadı ({why}). "
                             f"Asıl config.xml değişmedi. ")
                else:
                    head = t(f"config.xml を書き込めませんでした（{why}）。", f"config.xml could not be written ({why}). ",
                             f"config.xml yazılamadı ({why}). ")
                    if cf.get("backup"):
                        head += t("以前の config.xml のバックアップを出力先に残しました（ファイル名は処理ログにあります）。"
                                  "元の config.xml は変更していません。",
                                  "A backup of the previous config.xml was kept in the output folder (the file name is in "
                                  "the processing log). The original config.xml was not changed. ",
                                  "Önceki config.xml'in yedeği çıktı klasöründe bırakıldı (dosya adı işlem günlüğünde). "
                                  "Asıl config.xml değiştirilmedi. ")
                out.append(head + kept)
            elif cf.get("result") == "not_run":
                out.append(t("config.xml は作成・変更していません（処理が中断されたため）。",
                             "config.xml was not created or changed (processing was interrupted).",
                             "config.xml oluşturulmadı veya değiştirilmedi (işlem yarıda kesildi)."))
            fb = rec.get("fp16_fallback")
            g, c = rec.get("gpu_images", 0), rec.get("cpu_images", 0)
            if fb:
                why = fp16_fallback_reason(fb)
                if g + c == 0:
                    if rec.get("removebg"):
                        head = t("ローカル背景透過の処理先", "The local background removal device", "Yerel arka plan kaldırma birimi")
                    else:
                        head = t("背景透過の処理先", "The background removal device", "Arka plan kaldırma birimi")
                    out.append(head + t(f"を CPU・FP32 に切り替えました（{why}ため。GPU・FP32は使っていません。CPUで処理できた画像 0 枚）",
                                        f" was switched to CPU (FP32) ({why}; GPU FP32 was not used; 0 images were processed on CPU)",
                                        f" CPU'ya (FP32) geçirildi ({why}; GPU FP32 kullanılmadı; CPU'da 0 görüntü işlendi)"))
                else:
                    out.append(t(f"背景透過は CPU・FP32 で処理しました（{why}ため。GPU・FP32は使っていません）",
                                 f"Background removal ran on CPU (FP32) ({why}; GPU FP32 was not used)",
                                 f"Arka plan kaldırma CPU'da (FP32) yapıldı ({why}; GPU FP32 kullanılmadı)"))
            elif rec.get("kind") == "gpu" and rec.get("cpu_reason") == "dml_unavailable":
                if g + c == 0:
                    out.append(t("GPU（DirectML）を利用できないため、背景透過の処理先を CPU・FP32 にしました（CPUで処理した画像 0 枚）",
                                 "GPU (DirectML) is not available, so background removal was set to CPU (FP32) (0 images processed on CPU)",
                                 "GPU (DirectML) kullanılamadığı için arka plan kaldırma CPU'ya (FP32) ayarlandı (CPU'da 0 görüntü işlendi)"))
                else:
                    out.append(t(f"GPU（DirectML）を利用できないため、背景透過は CPU・FP32 で処理しました（CPU {c} 枚）",
                                 f"GPU (DirectML) is not available, so background removal ran on CPU (FP32) (CPU {c} images)",
                                 f"GPU (DirectML) kullanılamadığı için arka plan kaldırma CPU'da (FP32) yapıldı (CPU {c} görüntü)"))
            elif rec.get("kind") == "gpu" and rec.get("fallback"):
                if (rec["fallback"] or {}).get("stage") == "init":
                    what = t("初期化", "initialization", "başlatma")
                else:
                    what = t("推論", "inference", "çıkarım")
                out.append(t(f"GPU（DirectML）の{what}に失敗したため、背景透過を CPU・FP32 に切り替えました（GPU {g} 枚 / CPU {c} 枚）",
                             f"GPU (DirectML) {what} failed, so background removal switched to CPU (FP32) (GPU {g} / CPU {c} images)",
                             f"GPU (DirectML) {what} başarısız olduğu için arka plan kaldırma CPU'ya (FP32) geçti (GPU {g} / CPU {c} görüntü)"))
            return out

        def short_note():
            if rec.get("fp16_fallback"):
                return t("  |  背景透過: CPU・FP32（FP16を使えないため）", "  |  Background removal: CPU (FP32; FP16 unavailable)",
                         "  |  Arka plan kaldırma: CPU (FP32; FP16 kullanılamıyor)")
            if rec.get("kind") == "gpu" and rec.get("cpu_reason") == "dml_unavailable":
                return t("  |  背景透過: CPU・FP32（GPUを利用できないため）", "  |  Background removal: CPU (FP32; GPU unavailable)",
                         "  |  Arka plan kaldırma: CPU (FP32; GPU kullanılamıyor)")
            if rec.get("kind") == "gpu" and (rec.get("fallback") or {}).get("stage") == "init":
                return t("  |  背景透過: CPU・FP32（GPUの初期化に失敗）", "  |  Background removal: CPU (FP32; GPU initialization failed)",
                         "  |  Arka plan kaldırma: CPU (FP32; GPU başlatma başarısız)")
            if rec.get("kind") == "gpu" and rec.get("fallback"):
                return t("  |  背景透過: 途中からCPU・FP32", "  |  Background removal: switched to CPU (FP32)",
                         "  |  Arka plan kaldırma: CPU'ya (FP32) geçti")
            return ""

        def counts():
            return ("  |  " + run_counts_text(rec)) if rec else ""

        def show(fn):
            self._status_fn = fn
            try:
                self.status_lbl.config(text=fn())
            except Exception:  # noqa: BLE001
                pass
            self._see_status()               # 結果の全文が見える位置まで、必要ならメイン画面をスクロールする

        extra = notes()
        tail = ("\n\n" + "\n".join(extra)) if extra else ""
        cfg_failed = (rec.get("config") or {}).get("result") == "failed"

        def info(title, body):
            # config.xml を書き込めなかったときは、情報ではなく警告にする（保存した枚数は本文のとおり）
            if cfg_failed:
                messagebox.showwarning(t("config.xml を書き込めませんでした", "Could not write config.xml", "config.xml yazılamadı"), body)
            else:
                messagebox.showinfo(title, body)

        def final_line():
            return run_result_line(rec)
        status = rec.get("status")
        if payload:
            show(lambda: (final_line() if rec else t("エラーで中断しました", "Stopped by an error", "Bir hata nedeniyle durdu")
                          + counts()) + short_note())
            cres = (rec.get("config") or {}).get("result")
            if cres not in (None, "failed", "not_run"):
                # config.xml の処理を終えてから中断した：その結果を書き添える（失敗・未実行は notes() が書く）
                tail += ("\n\n" if not tail else "\n") + "config.xml: " + config_result_text(rec)
            messagebox.showerror(t("エラー", "Error", 'Hata'), str(payload) + tail)
            return
        if status == "stopped" or (notice and not rec):
            kind = rec.get("preview_abort") or "unconfirmed"
            saved_n = rec.get("saved", 0)
            if rec:
                show(lambda: final_line() + short_note())
            else:
                show(lambda: t("中止しました", "Stopped", "Durduruldu") + t(f"（{preview_abort_reason(kind)}）",
                                                                        f" ({preview_abort_reason(kind)})",
                                                                        f" ({preview_abort_reason(kind)})")
                     + short_note())
            messagebox.showwarning(
                t("処理を中止しました", "Processing stopped", 'İşlem durduruldu'),
                t("キャンセル操作ではなく、次の理由で処理を中止しました。", "Processing was stopped for the reason below (not by Cancel).",
                  "İşlem aşağıdaki nedenle durduruldu (İptal ile değil).") + "\n"
                + t("理由", "Reason", "Neden") + ": " + preview_abort_reason(kind)
                + ("\n\n" + notice if (notice and kind == "display_error") else "") + "\n\n"
                + t(f"確認できなかった画像は保存していません。それまでに保存した画像（{saved_n} 枚）はそのまま残っています。",
                    f"The image that could not be confirmed was not saved. Images saved before this point ({saved_n}) are kept.",
                    f"Onaylanamayan görüntü kaydedilmedi. Bu noktadan önce kaydedilen görüntüler ({saved_n}) korunur.")
                + ("\n" + t("枚数", "Images", "Görüntüler") + ": " + run_counts_text(rec) if rec else "")
                + tail)
            return
        if cancelled:
            show(lambda: (final_line() if rec else t("キャンセルしました", "Cancelled", "İptal edildi")) + short_note())
            info(
                t("キャンセル", "Cancelled", 'İptal edildi'),
                t("処理をキャンセルしました。完了済みの画像は保存されています。",
                  "Processing was cancelled. Completed images have been saved.", 'İşlem iptal edildi. Tamamlanan görüntüler kaydedildi.') + tail)
            return
        if status == "not_started":
            kind = rec.get("not_started")

            def title():
                if kind in NOT_STARTED_PRECHECK:
                    return t("処理を開始できませんでした", "Processing could not start", "İşlem başlatılamadı")
                return t("処理できる画像がありませんでした", "There was nothing to process", "İşlenecek görüntü yoktu")

            show(lambda: title() + ": " + not_started_reason(kind))
            in_dir = getattr(self, "_run_paths", ("", ""))[0]
            messagebox.showwarning(
                title(),
                title() + t("。", ".", ".") + "\n" + t("理由", "Reason", "Neden") + ": " + not_started_reason(kind)
                + (f"\n\n{in_dir}" if in_dir else "")
                + "\n\n" + t("画像は保存していません。", "No image was saved.", "Hiçbir görüntü kaydedilmedi.") + tail)
            return
        if status == "all_skipped":
            # 全件スキップ（失敗なし）：情報。状態の文は処理ログの最後の行・共有診断と同じ（run_status_text）
            ex = rec.get("skipped_existing", 0)
            cres = rec.get("config") or {}
            cfg_txt = ""
            if ex and cres.get("result") == "updated":
                cfg_txt = t("\nconfig.xml は、既存のPNGの分を含めて更新しました。",
                            "\nconfig.xml was updated, including the existing PNGs.",
                            "\nconfig.xml, mevcut PNG'ler dahil güncellendi.")
            elif ex and cres.get("result") in ("created", "regenerated", "overwritten"):
                verb = {"created": t("生成", "generated", "oluşturuldu"),
                        "regenerated": t("再生成", "regenerated", "yeniden oluşturuldu"),
                        "overwritten": t("上書き", "overwritten", "üzerine yazıldı")}[cres["result"]]
                cfg_txt = t(f"\nconfig.xml は、既存のPNGの分を含めて{verb}しました。",
                            f"\nconfig.xml was {verb}, including the existing PNGs.",
                            f"\nconfig.xml, mevcut PNG'ler dahil {verb}.")
            elif ex and cres.get("result") == "unchanged" and cres.get("reason") == "all_registered":
                cfg_txt = t("\nconfig.xml は変更していません（既存のPNGの分はすべて登録済み）。",
                            "\nconfig.xml was not changed (the existing PNGs are all registered already).",
                            "\nconfig.xml değiştirilmedi (mevcut PNG'lerin tümü zaten kayıtlı).")
            redo = t("作り直す場合は「作成済みでも作り直す」をオンにしてください。",
                     "To redo them, turn on 'Overwrite existing'.",
                     "Yeniden oluşturmak için 'Mevcut dosyaların üzerine yaz'ı açın.")
            if existing_only_skip(rec):
                body = t(f"保存した画像はありません（保存 0 件）。\n出力先に同じ名前のPNGがあるため、{ex} 枚すべてをスキップしました"
                         f"（「作成済みでも作り直す」がオフ）。",
                         f"No image was saved (0 saved).\nAll {ex} were skipped because a PNG with the same name already "
                         f"exists in the output folder ('Overwrite existing' is off).",
                         f"Hiçbir görüntü kaydedilmedi (0 kaydedildi).\nÇıktı klasöründe aynı adlı PNG bulunduğu için {ex} "
                         f"görüntünün tümü atlandı ('Mevcut dosyaların üzerine yaz' kapalı).") + cfg_txt + "\n" + redo
            else:
                body = t("保存した画像はありません（保存 0 件）。", "No image was saved (0 saved).",
                         "Hiçbir görüntü kaydedilmedi (0 kaydedildi).") + "\n" + skip_summary_text(rec) + t("。", ".", ".")
                if ex:
                    body += "\n" + t(f"既存PNG（{ex} 枚）は、出力先に同じ名前のPNGがあるためスキップしました（「作成済みでも作り直す」がオフ）。",
                                     f"The existing PNGs ({ex}) were skipped because a PNG with the same name already exists "
                                     f"in the output folder ('Overwrite existing' is off).",
                                     f"Mevcut PNG'ler ({ex}), çıktı klasöründe aynı adlı PNG bulunduğu için atlandı "
                                     f"('Mevcut dosyaların üzerine yaz' kapalı).") + cfg_txt + "\n" + redo
                if skip_parts(rec)[1]:
                    body += "\n" + t("ID重複などの詳細はログを確認してください。", "See the log for the duplicate IDs etc.",
                                     "Yinelenen ID vb. için günlüğe bakın.")
            show(lambda: final_line() + short_note())
            info(
                t("すべてスキップしました", "All images skipped", "Tüm görüntüler atlandı"),
                body + "\n\n" + t("枚数", "Images", "Görüntüler") + ": " + run_counts_text(rec) + tail)
            return
        if status in ("all_failed", "failed_skipped"):
            # 保存0で失敗を含む：警告。状態の文は処理ログの最後の行・共有診断と同じ（run_status_text）
            f = rec.get("failed", 0)
            show(lambda: final_line() + "  |  " + t("失敗の理由", "Failure reasons", "Başarısızlık nedenleri")
                 + ": " + format_fail_kinds(rec.get("fail_kinds")) + short_note())
            if status == "all_failed":
                what = t(f"処理した {f} 枚すべてで失敗しました。", f"All {f} processed image(s) failed.",
                         f"İşlenen {f} görüntünün tümü başarısız oldu.")
            else:
                what = t(f"失敗 {f} 枚、{skip_summary_text(rec)}でした。", f"{f} failed, {skip_summary_text(rec)}.",
                         f"{f} başarısız, {skip_summary_text(rec)}.")
            messagebox.showwarning(
                t("保存できませんでした", "Could not save images", "Görüntüler kaydedilemedi"),
                t("保存した画像はありません（保存 0 件）。", "No image was saved (0 saved).",
                  "Hiçbir görüntü kaydedilmedi (0 kaydedildi).") + "\n" + what + "\n"
                + t("失敗の理由", "Failure reasons", "Başarısızlık nedenleri") + ": " + format_fail_kinds(rec.get("fail_kinds"))
                + "\n\n" + t("枚数", "Images", "Görüntüler") + ": " + run_counts_text(rec) + "\n"
                + t("詳しくはログを確認してください。", "See the log for details.", "Ayrıntılar için günlüğe bakın.") + tail)
            return

        def fail_note():
            if rec.get("failed"):
                return "  |  " + t("失敗の理由", "Failure reasons", "Başarısızlık nedenleri") + ": " \
                    + format_fail_kinds(rec.get("fail_kinds"))
            return ""

        show(lambda: (final_line() if rec else t("完了", "Done", "Tamamlandı")) + fail_note() + short_note())
        elapsed_msg = t(f"処理が終わりました（{elapsed:.0f}秒）。\nログでペアの対応を確認してください。",
                        f"Finished in {elapsed:.0f}s.\nCheck the log for the pairings.", f'{elapsed:.0f} saniyede tamamlandı.\nEşleştirmeleri günlükten kontrol edin.')
        if rec:
            elapsed_msg += "\n\n" + t("枚数", "Images", "Görüntüler") + ": " + run_counts_text(rec)
            if rec.get("failed"):
                elapsed_msg += "\n" + t("失敗の理由", "Failure reasons", "Başarısızlık nedenleri") + ": " \
                    + format_fail_kinds(rec.get("fail_kinds"))
        info(t("完了", "Done", 'Tamamlandı'), elapsed_msg + tail)

    def apply_theme(self, mode):
        self.mode = mode
        if HAS_SVTTK:
            sv_ttk.set_theme(mode)
        else:
            self._apply_clam("dark" if mode == "dark" else "light")
        # sv_ttk は初回の set_theme で自分のフォントを作るので、テーマ適用の後に差し替える
        self._apply_ui_fonts()
        try:
            canvas = getattr(self, "ui_canvas", None)
            bg = self.style.lookup("TFrame", "background")
            if canvas is not None and bg:
                canvas.configure(background=bg)
        except Exception:  # noqa: BLE001
            pass
        self._refresh_theme_btn()
        self._sync_dependencies()     # 無効表示の文字色はテーマごとに違う

    def _apply_ui_fonts(self):
        """日本語表示なら Noto Sans JP（無ければメイリオ）の標準ウェイトへ切り替える。

        Tk の名前付きフォント（TkDefaultFont など）と sv_ttk のフォント（SunValleyBodyFont など）を
        直接差し替えるので、ttk の各スタイル・入力欄・コンボボックスの一覧・プレビューの案内まで
        同じフォントになる。sv_ttk はテーマ切替でフォントを作り直さないため、ライト／ダークを
        切り替えても指定は残る。英語・トルコ語では最初に記録した既存の指定へ戻す。"""
        from tkinter import font as tkfont
        backup = self.__dict__.setdefault("_font_backup", {})
        family = japanese_ui_font_family(self) if self.lang == "ja" else None
        self.ui_font_family = family
        for name in UI_NAMED_FONTS:
            try:
                font = tkfont.nametofont(name, root=self)
                options = font.configure()
            except (tk.TclError, RuntimeError):
                continue  # sv_ttk 未導入など
            if name not in backup:
                backup[name] = options
            if family:
                font.configure(family=family, weight="normal", slant="roman")
            else:
                font.configure(**backup[name])
        log = getattr(self, "log", None)
        if log is not None:
            try:
                # Consolas には日本語の字形が無く、別フォントで代用されて読みにくい
                log.configure(font=(family, 10) if family else LOG_FONT_DEFAULT)
            except tk.TclError:
                pass
        if not HAS_SVTTK:
            try:
                ttk.Style(self).configure("Header.TLabel", font=(family or "", 14, "bold"))
            except tk.TclError:
                pass

    def _refresh_theme_btn(self):
        if hasattr(self, "theme_btn"):
            self.theme_btn.config(text="☀" if self.mode == "dark" else "🌙")

    def _apply_clam(self, mode):
        s = ttk.Style(self)
        s.theme_use("clam")
        if mode == "dark":
            # Match the Sun Valley dark theme used by the v2.2.0 Windows build.
            pal = {"bg": "#1c1c1c", "fg": "#fafafa", "sel": "#3d3d3d",
                   "acc": "#57c8ff", "sub": "#fafafa", "entry": "#292929",
                   "btn": "#2b2b2b", "btnfg": "#fafafa", "danger": "#c42b1c",
                   "hover": "#323232"}
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
        accent_fg = "#000000" if mode == "dark" else "#ffffff"
        s.configure("Accent.TButton", background=pal["acc"], foreground=accent_fg,
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
              foreground=[("active", accent_fg)])
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
            self.log.configure(background=pal["bg"] if mode == "dark" else pal["entry"], foreground=pal["fg"],
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

    def _select_lang(self, _event=None):
        selected = self.language_var.get()
        if selected == language_menu_label(LANG_SYSTEM):
            pref = LANG_SYSTEM
        else:
            pref = next((code for code, name in LANGUAGES.items() if name == selected),
                        self.lang_pref)
        if pref == self.lang_pref:
            return
        self.lang_pref = pref
        lang = resolve_language(pref)
        if lang == self.lang:
            # 表示言語は変わらない（例: 日本語版Windowsで「日本語」→「Windowsに合わせる」）
            self._save_settings()
            return
        # Reopen an existing guide in the newly selected language.
        guide = getattr(self, "_help_window", None)
        guide_open = guide is not None and guide.winfo_exists()
        if guide_open:
            guide.destroy()
        self.lang = lang
        set_lang(lang)
        self._build()
        self._save_settings()
        if guide_open:
            self._show_id_help()
        # _poll schedules itself; rebuilding must not start a second chain.


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
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("入力フォルダを先に選んでください。",
                                     "Please select the input folder first.", 'Lütfen önce girdi klasörünü seçin.'))
            return
        if not Path(base).is_dir():
            # 入力欄は自由入力で、設定ファイルから復元した古いパスのこともある。
            # 存在確認をしないと FileNotFoundError がコールバックから素通りして
            # 「押しても何も起きない」状態になる。
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t(f"入力フォルダが見つかりません:\n{base}",
                                     f"Input folder not found:\n{base}", f'Girdi klasörü bulunamadı:\n{base}'))
            return
        n = simpledialog.askinteger(t("ペアフォルダを作る", "Make pair folders", 'Eşleştirme klasörleri oluştur'),
                                    t("作るフォルダの数:", "Number of folders to create:", 'Oluşturulacak klasör sayısı:'),
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
            messagebox.showerror(t("エラー", "Error", 'Hata'),
                                 t(f"フォルダを作成できませんでした（{made} 個目まで作成済み）:\n{e}",
                                   f"Could not create folders (created {made} so far):\n{e}", f'Klasörler oluşturulamadı (şu ana kadar {made} klasör oluşturuldu):\n{e}'))
            return
        messagebox.showinfo(t("完了", "Done", 'Tamamlandı'),
                            t(f"{made} 個のフォルダを作成しました。\n各フォルダに「顔写真」と「IDスクショ」を1枚ずつ入れてから実行してください。",
                              f"Created {made} folders.\nPut one face photo and one ID screenshot in each, then run.", f'{made} klasör oluşturuldu.\nHer klasöre bir yüz fotoğrafı ve ID içeren bir ekran görüntüsü koyun, ardından işlemi başlatın.'))

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
                            f"[i] Multiple folders dropped; using the first: {dirs[0].name}", f'[i] Birden fazla klasör bırakıldı; ilki kullanılıyor: {dirs[0].name}'))
            return
        if imgs:
            parents = {p.parent for p in imgs}
            if len(parents) > 1:
                messagebox.showwarning(
                    t("確認", "Notice", 'Uyarı'),
                    t("複数のフォルダにまたがる画像がドロップされました。\n"
                      "1つのフォルダにまとめてからドロップしてください。",
                      "The dropped images live in different folders.\n"
                      "Please put them in one folder first.", 'Bırakılan görüntüler farklı klasörlerde bulunuyor.\nLütfen önce hepsini aynı klasöre koyun.'))
                return
            self.in_var.set(str(next(iter(parents))))
            return
        if other:
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("対応していないファイルです。", "Unsupported file.", 'Desteklenmeyen dosya.'))

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
                            f"  [!] Copy failed: {q.name}: {e}", f'  [!] Kopyalanamadı: {q.name}: {e}'))
        self._log(t(f"[i] {added} 枚を入力フォルダにコピーしました",
                    f"[i] Copied {added} image(s) into the input folder", f'[i] Girdi klasörüne {added} görüntü kopyalandı'))
        return added

    def _delete_source_images(self):
        base = self.in_var.get()
        if not base:
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("入力フォルダを先に選んでください。",
                                     "Please select the input folder first.", 'Lütfen önce girdi klasörünü seçin.'))
            return
        p = Path(base)
        if not p.exists():
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("入力フォルダが見つかりません。", "Input folder not found.", 'Girdi klasörü bulunamadı.'))
            return
        output = self.out_var.get().strip()
        if output:
            try:
                if p.resolve() == Path(output).resolve():
                    messagebox.showwarning(
                        t("確認", "Notice", 'Uyarı'),
                        t("入力と出力が同じフォルダのため、元画像を安全に判別できません。\n"
                          "出力フォルダを分けてから実行してください。",
                          "Input and output are the same folder, so source images cannot be identified safely.\n"
                          "Choose a separate output folder first.", 'Girdi ve çıktı aynı klasör olduğundan kaynak görüntüler güvenli biçimde ayırt edilemiyor.\nLütfen önce ayrı bir çıktı klasörü seçin.'))
                    return
            except OSError:
                pass
        targets = _source_image_delete_targets(p, output or None)
        if not targets:
            messagebox.showinfo(t("情報", "Info", 'Bilgi'),
                                t("削除する画像がありません。", "No images found to delete.", 'Silinecek görüntü bulunamadı.'))
            return
        if not messagebox.askyesno(
                t("元画像をゴミ箱へ", "Trash source images", 'Kaynak görüntüleri çöpe taşı'),
                t(f"{len(targets)} 枚の元画像を入力フォルダ内の各サブフォルダから"
                  " をごみ箱に送ります。\nフォルダ自体は残します。出力フォルダの完成画像は消えません。\n（ごみ箱から元に戻せます）\n\nよろしいですか？",
                  f"Send {len(targets)} image(s)"
                  " inside the input folder to the Recycle Bin.\nFolders are kept. Finished images in the output folder are safe.\n(Can be restored from Recycle Bin)\n\nProceed?", f'Girdi klasöründeki {len(targets)} görüntü Geri Dönüşüm Kutusu’na taşınacak.\nKlasörler korunur. Çıktı klasöründeki tamamlanmış görüntüler etkilenmez.\n(Geri Dönüşüm Kutusu’ndan geri yüklenebilir)\n\nDevam edilsin mi?')):
            return
        failed = 0
        for f in targets:
            try:
                # 戻り値 False も失敗（Windows APIのフォールバックが失敗した場合）。
                # 以前は例外だけ数えていたので「N枚送りました」と嘘の報告をしていた。
                if not send_to_trash(f):
                    self._log(t(f"  [!] 削除失敗: {f.name}", f"  [!] Could not delete: {f.name}", f'  [!] Silinemedi: {f.name}'))
                    failed += 1
            except Exception as e:  # noqa: BLE001
                self._log(t(f"  [!] 削除失敗: {f.name}: {e}", f"  [!] Could not delete: {f.name}: {e}", f'  [!] Silinemedi: {f.name}: {e}'))
                failed += 1
        done_n = len(targets) - failed
        msg = t(f"{done_n} 枚をごみ箱に送りました。", f"Moved {done_n} image(s) to the Recycle Bin.", f'{done_n} görüntü Geri Dönüşüm Kutusu’na taşındı.')
        if failed:
            msg += t(f"\n{failed} 枚は削除できませんでした。", f"\n{failed} image(s) could not be deleted.", f'\n{failed} görüntü silinemedi.')
        messagebox.showinfo(t("完了", "Done", 'Tamamlandı'), msg)

    def _dedupe_config_dialog(self):
        start = filedialog.askopenfilename(
            title=t("掃除する config.xml を選んでください", "Choose a config.xml to clean", 'Temizlenecek config.xml dosyasını seçin'),
            filetypes=[("XML", "*.xml"), ("All", "*.*")])
        if not start:
            return
        try:
            out, backup, removed, kept = dedupe_config(Path(start))
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t("エラー", "Error", 'Hata'), str(e))
            return
        if removed == 0:
            messagebox.showinfo(t("重複なし", "No Duplicates", 'Yinelenen ID yok'),
                                t(f"config.xml: 重複なし（{kept} 件）。",
                                  f"config.xml: no duplicates ({kept} remain).", f'config.xml: yinelenen ID yok ({kept} kayıt).'))
        else:
            messagebox.showinfo(t("完了", "Done", 'Tamamlandı'),
                                t(f"{removed} 件の重複を削除しました（残り {kept} 件）。\n元のファイル: {backup.name}",
                                  f"Removed {removed} duplicate(s) ({kept} remain).\nOriginal: {backup.name}", f'{removed} yinelenen kayıt silindi ({kept} kayıt kaldı).\nOrijinal dosya: {backup.name}'))

    def _cancel(self):
        """キャンセルの要求。画像処理から終了処理へ移る境界より前なら受け付ける（CancelGate）。
        境界を越えたあと（終了処理中）のクリックは受け付けず、結果（完了など）も変えない。"""
        ev = getattr(self, "_cancel_event", None)
        accepted = False
        if ev is not None:
            accepted = ev.request() if hasattr(ev, "request") else (ev.set() or True)
        self.cancel_btn.config(state="disabled")
        if accepted:
            self._log(t("[i] キャンセルを要求しました…現在の画像が終わったら停止します。",
                        "[i] Cancel requested… will stop after the current image finishes.", '[i] İptal istendi… mevcut görüntü tamamlandıktan sonra durdurulacak.'))
        else:
            self._log(t("[i] 画像の処理が終わり、終了処理に入っていたため、キャンセルは受け付けませんでした。",
                        "[i] Cancel was not accepted: image processing had already finished and the run is finishing.",
                        "[i] İptal kabul edilmedi: görüntü işleme zaten bitmişti ve çalıştırma sonlandırılıyor."))

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
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t(f"フォルダを開けませんでした: {e}", f"Could not open folder: {e}", f'Klasör açılamadı: {e}'))

    def _delete_logs(self):
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning(t("ログ削除", "Delete Logs", 'Günlükleri sil'),
                                   t("出力フォルダが設定されていません。", "Output folder is not set.", 'Çıktı klasörü ayarlanmamış.'))
            return
        from pathlib import Path as _Path
        logs = list(_Path(out).glob("processing_log_*.txt"))
        if not logs:
            messagebox.showinfo(t("ログ削除", "Delete Logs", 'Günlükleri sil'),
                                t("削除するログファイルがありません。", "No log files to delete.", 'Silinecek günlük dosyası yok.'))
            return
        if messagebox.askyesno(
                t("ログをすべて削除", "Delete All Logs", 'Tüm günlükleri sil'),
                t(f"出力フォルダ内のログファイル {len(logs)} 件を削除しますか？\n（この操作は元に戻せません）",
                  f"Delete {len(logs)} log file(s) from the output folder?\n(This cannot be undone)", f'Çıktı klasöründeki {len(logs)} günlük dosyası silinsin mi?\n(Bu işlem geri alınamaz)')):
            for lf in logs:
                try:
                    lf.unlink()
                except Exception:
                    pass
            messagebox.showinfo(t("ログ削除", "Delete Logs", 'Günlükleri sil'),
                                t(f"{len(logs)} 件のログを削除しました。",
                                  f"Deleted {len(logs)} log file(s).", f'{len(logs)} günlük dosyası silindi.'))

    def _log(self, msg):
        # _build() でウィジェットを作り直すと表示が消えるので、内容を保持しておく
        # （log_lines は復元側だけあって、詰める側が無かった）
        self.log_lines.append(msg)
        if len(self.log_lines) > 5000:
            del self.log_lines[:1000]
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log_has_content = True

    def _show_preview(self, request, ev, holder):
        """保存前プレビューのダイアログ。選択をholderに入れてevをセットする。
        ここで例外が出ると、ev を待っているワーカースレッドが永久に止まる
        （キャンセルも効かずアプリを殺すしかなくなる）ので、必ず finally でセットする。
        表示に失敗したときは保存しない（holder["r"] = "error"）。以前は「保存して続行」にしていたため、
        確認できなかった画像がそのまま書き出されていた。理由はワーカー側で処理ログに出す。"""
        if holder.get("stale"):
            # ワーカーがすでに待つのをやめた依頼（応答なしで中止済み）。今さら開いても結果は使われない
            ev.set()
            return
        try:
            self._show_preview_impl(request, ev, holder)
        except Exception as e:  # noqa: BLE001
            holder["r"] = "error"
            holder["error"] = f"{type(e).__name__}: {e}"
            holder.pop("edit", None)
            win = holder.get("_win")
            try:
                if win is not None and win.winfo_exists():
                    win.grab_release()
                    win.destroy()
            except Exception:  # noqa: BLE001
                pass
        finally:
            holder.pop("_win", None)
            if "r" not in holder:
                holder["r"] = "noanswer"    # 選択されないまま閉じた。保存しない
            ev.set()

    def _show_preview_impl(self, request, ev, holder):
        """保存前プレビュー。トリミング前の画像に、自動で求めた保存枠を初期表示する。

        1つの画面で次の操作をまとめて行う（モードの切り替えは無い）。
        ・保存枠: 枠の内側をドラッグで移動、下の角の●で大きさを変更（上辺の高さは固定・1:1）
        ・襟: 「襟を四角で選択」の次のドラッグ、または Shift＋ドラッグで四角く囲む。
          「襟を自由選択（投げ縄）」では、左ボタンを押したまま首と襟の境目に沿って線を描き、
          離すと始点と終点を結んで囲んだ範囲を選ぶ（反対側の襟で確定済みの範囲は除く）。
          どちらも矢印キーか選択の内側のドラッグで動かす（Photopea風の切り取り移動）。
          Enterで確定、Escで取り消し、Ctrl+Zで戻す（投げ縄を描いている途中は線を捨てる）
        ・手動回転（−15°〜＋15°）と出力サイズ（180×180 / 250×250 / 任意 1〜4096 px）。
          実行の最初のプレビューではサイズは未選択で、選ぶまで「保存」は押せない。
          選んだサイズだけをこの実行の次のプレビューに引き継ぐ（次の画像もプレビューを開き、その画像の
          「保存」を押したときだけ保存する。「全部保存」は無い）
        ・操作は「保存」「スキップ」「キャンセル」の3つ
        ・操作しないまま PREVIEW_IDLE_TIMEOUT_SEC 秒たつと、保存せずに閉じる（"timeout"）
        ・表示: ホイールでカーソル位置を中心に拡大・縮小、拡大中は中ボタンのドラッグで移動、
          「全体表示」で初期表示へ。ズームと表示位置は画面だけのもので、保存には影響しない
        保存される画像は「襟の移動 → 回転 → 1:1の枠で切り抜き」の順に作る。
        枠線・選択線・暗い表示は Canvas 上の表示だけで、保存画像には入らない。

        座標は3種類ある。
          元画像の座標 … 襟の移動（moves）はこの座標で記録する
          回転後の座標 … 保存枠（frame）の座標。0°なら元画像の座標と同じ
          画面の座標   … Canvas 上の位置。ズームと表示位置（view）で回転後の座標と対応する"""
        from PIL import Image as _I, ImageDraw as _D, ImageTk
        resampling = getattr(_I, "Resampling", _I)
        base = request["image"].convert("RGBA")
        img_w, img_h = base.size
        auto_rect = tuple(int(v) for v in request["rect"])
        protect_box = request.get("protect_box")
        # 顎の保護範囲が無い（顔検出OFF・検出失敗）ときは襟を動かせない。枠・回転・ズームは使える。
        collar_enabled = protect_box is not None
        # 顔トリミング無し＋stretch: 枠と回転が初期状態なら画像全体を正方形に引き伸ばして保存される
        stretch_full = bool(request.get("stretch_full"))
        face_cx = request.get("face_cx")
        if face_cx is None:
            face_cx = auto_rect[0] + auto_rect[2] / 2.0
        rotate_center = request.get("rotate_center") or (auto_rect[0] + auto_rect[2] / 2.0,
                                                         auto_rect[1] + auto_rect[2] / 2.0)
        # この実行で前の画像までに選んだ出力サイズ。実行の最初の画像では None（未選択）
        init_size = parse_output_size(request.get("size"))
        dark = self.mode == "dark"
        accent = "#57c8ff" if dark else "#005fb8"

        win = tk.Toplevel(self)
        holder["_win"] = win          # 表示に失敗したとき、呼び出し側で確実に閉じるため
        holder["active"] = time.monotonic()
        win.title(t("プレビュー・微調整", "Preview & adjust", 'Önizleme ve ayarlama'))
        win.grab_set()
        win.resizable(False, False)
        set_titlebar_dark(win, dark)

        # 表示範囲: 画像と初期枠の両方を含め、枠を外側へ動かせる余白を足す。
        margin = max(img_w, img_h) * 0.08
        vx0 = int(math.floor(min(0, auto_rect[0]) - margin))
        vy0 = int(math.floor(min(0, auto_rect[1]) - margin))
        vx1 = int(math.ceil(max(img_w, auto_rect[0] + auto_rect[2]) + margin))
        vy1 = int(math.ceil(max(img_h, auto_rect[1] + auto_rect[2]) + margin))
        try:
            screen_w = int(win.winfo_screenwidth())
            screen_h = int(win.winfo_screenheight())
        except tk.TclError:
            screen_w, screen_h = 1280, 900
        # 画像以外（ボタン・回転・案内文）に約500pxを残す。表示倍率125%の
        # 1920×1080 では画面が 1536×864 相当になるので、そこでもボタンまで収める。
        max_w = max(360, min(760, screen_w - 160))
        max_h = max(300, min(680, screen_h - 500))
        base_scale = min(max_w / float(vx1 - vx0), max_h / float(vy1 - vy0), 2.0)
        cw = max(1, int(round((vx1 - vx0) * base_scale)))
        ch = max(1, int(round((vy1 - vy0) * base_scale)))
        # 文字の長さでダイアログの幅が変わらないよう、説明文はこの幅で折り返す
        content_w = max(cw, 720)
        min_side = max(16, int(round(min(img_w, img_h) * 0.05)))
        # 画面1pxが元画像の約1/4pxになるところまで拡大できる
        zoom_max = max(1.0, min(16.0, 4.0 / base_scale))

        # ---------------- 表示（ズーム・表示位置）----------------
        view = {"zoom": 1.0, "ox": float(vx0), "oy": float(vy0)}  # ox, oy = 画面左上の回転後座標

        def cur_scale():
            return base_scale * view["zoom"]

        def to_canvas(x, y):
            """回転後の座標 → 画面上の点。"""
            s = cur_scale()
            return (x - view["ox"]) * s, (y - view["oy"]) * s

        def to_image(px, py):
            """画面上の点 → 回転後の座標。"""
            s = cur_scale()
            return px / s + view["ox"], py / s + view["oy"]

        def step_px():
            """矢印キー1回 = 画面上の約1px（拡大するほど細かく動く）。"""
            return max(1, int(round(1.0 / cur_scale())))

        def clamp_view():
            s = cur_scale()
            view["ox"] = max(float(vx0), min(view["ox"], vx1 - cw / s))
            view["oy"] = max(float(vy0), min(view["oy"], vy1 - ch / s))

        def zoom_at(px, py, factor):
            """画面上の点 (px, py) の下にある位置を動かさずに拡大・縮小する。"""
            zoom = max(1.0, min(zoom_max, view["zoom"] * factor))
            if abs(zoom - view["zoom"]) < 1e-9:
                return
            qx, qy = to_image(px, py)
            view["zoom"] = zoom
            s = cur_scale()
            view["ox"], view["oy"] = qx - px / s, qy - py / s
            clamp_view()
            schedule(1)

        def pan_by(dpx, dpy):
            s = cur_scale()
            view["ox"] -= dpx / s
            view["oy"] -= dpy / s
            clamp_view()
            schedule(1)

        def fit_view():
            view.update(zoom=1.0, ox=float(vx0), oy=float(vy0))
            schedule(1)

        st = {
            "moves": [],          # 確定済みの移動（normalize_move の形式、元画像の座標）
            "float": None,        # 移動中の選択 {"poly", "rect", "fx", "fy", "hist"}
            "armed": False,       # 「襟を四角で選択」を押した直後（次のドラッグで囲む）
            "lasso_armed": False,  # 「襟を自由選択（投げ縄）」を押した直後（次の左ドラッグで線を描く）
            "lasso": None,        # 投げ縄を描いている途中 {"pts": [元画像の座標]}（左ボタンを押している間）
            "frame": list(auto_rect),
            "angle": 0.0,         # 手動回転（度、正で反時計回り）
            "drag": None,
            "last_nudge": 0.0,
            "collar_blocked": 0.0,  # 襟を選べない状態で Shift＋ドラッグした時刻（案内を出す）
        }

        def stretch_view_active():
            """保存結果が「画像全体を正方形に引き伸ばし」になる状態か（枠も回転も初期状態）。"""
            return (stretch_full and tuple(int(v) for v in st["frame"]) == auto_rect
                    and abs(st["angle"]) < 0.05)

        def to_source(x, y):
            """回転後の座標 → 元画像の座標。"""
            return rotate_point(x, y, st["angle"], rotate_center, inverse=True)

        def to_view(x, y):
            """元画像の座標 → 回転後の座標。"""
            return rotate_point(x, y, st["angle"], rotate_center)

        def screen_vector_to_source(dx, dy):
            """画面上の移動量を、元画像の座標での移動量にする（回転していれば向きが変わる）。"""
            return rotate_point(dx, dy, st["angle"], (0.0, 0.0), inverse=True)

        cache = {"edited": None, "reduced": {}, "comp": None, "photo": None}
        checker = make_checkerboard((cw, ch), self.mode)

        def float_offset(f):
            return int(round(f["fx"])), int(round(f["fy"]))

        def float_move(f):
            dx, dy = float_offset(f)
            if f["rect"] is not None:
                return tuple(f["rect"]) + (dx, dy)
            move = {"poly": [list(p) for p in f["poly"]], "dx": dx, "dy": dy}
            if f.get("fill") == "center":
                move["fill"] = "center"
                if f.get("exclude"):
                    move["exclude"] = [[list(p) for p in ex] for ex in f["exclude"]]
            return move

        def all_moves(include_float=True):
            moves = list(st["moves"])
            f = st["float"]
            if include_float and f is not None and any(float_offset(f)):
                moves.append(float_move(f))
            return moves

        # ---------------- 画面の組み立て ----------------
        style = ttk.Style(win)
        style.configure("FMCollarOn.TLabel", foreground=accent)
        # 出力サイズの選択: "180" / "250" / "custom"（任意の値・入力欄）/ "none"（未選択）
        if init_size in (180, 250):
            size_choice = tk.StringVar(value=str(init_size))
            size_custom = tk.StringVar(value="")
        elif init_size:
            size_choice = tk.StringVar(value="custom")
            size_custom = tk.StringVar(value=str(init_size))
        else:
            size_choice = tk.StringVar(value="none")
            size_custom = tk.StringVar(value="")
        save_btns = []      # 「保存」（出力サイズが決まるまで無効）

        def chosen_size():
            c = size_choice.get()
            if c == "custom":
                return parse_output_size(size_custom.get())
            if c in ("180", "250"):
                return int(c)
            return None

        def update_save_btns():
            ok = chosen_size() is not None
            for b in save_btns:
                try:
                    b.state(["!disabled"] if ok else ["disabled"])
                except tk.TclError:
                    pass

        def on_size_change():
            update_save_btns()
            refresh_info()

        def on_custom_typed(*_):
            # 入力欄に数字を入れたら「任意」を選んだことにする
            if size_custom.get().strip() and size_choice.get() != "custom":
                size_choice.set("custom")
            on_size_change()

        def set_size(value):
            """自動検証用: 180 / 250 はその選択、ほかの値は「任意」に入れる。None は未選択に戻す。"""
            if value is None:
                size_choice.set("none")
            elif str(value) in ("180", "250"):
                size_choice.set(str(value))
            else:
                size_custom.set(str(value))
                size_choice.set("custom")
            on_size_change()
        # sv_ttk の Toolbutton には「選択中」の見た目が無いので、sv_ttk では通常のラジオボタン
        choice_style = "TRadiobutton" if HAS_SVTTK else "Toolbutton"

        top_bar = ttk.Frame(win)
        top_bar.pack(fill="x", padx=12, pady=(12, 6))
        # 襟の選択は「四角」（ドラッグで囲む）と「投げ縄」（左ボタンを押したまま首と襟の境目に沿って描く）の2通り。
        # 入りきらないボタンは次の行へ折り返す（長い訳語・狭い画面）。
        hidden_bar = ttk.Frame(win)     # 襟を動かせない画像では、確定・取り消しをここに置いて表示しない
        select_btn = ttk.Button(top_bar, text=t("襟を四角で選択 (Shift＋ドラッグ)", "Collar: rectangle (Shift+drag)",
                                                "Yaka: dikdörtgen (Shift+sürükle)"),
                                command=lambda: arm_select())
        lasso_btn = ttk.Button(top_bar, text=t("襟を自由選択（投げ縄）", "Collar: lasso", "Yaka: kement"),
                               command=lambda: arm_lasso())
        bar = top_bar if collar_enabled else hidden_bar
        commit_btn = ttk.Button(bar, text=t("襟の移動を確定 (Enter)", "Commit move (Enter)",
                                            "Taşımayı onayla (Enter)"),
                                command=lambda: on_enter())
        # 確定した襟の移動・移動中の選択を、新しい順に1つずつ戻す
        undo_btn = ttk.Button(bar, text=t("1つ戻す (Ctrl+Z)", "Undo (Ctrl+Z)", "Geri al (Ctrl+Z)"),
                              command=lambda: undo())
        collar_off_text = t("顎の位置を確認できないため、襟を移動できません",
                            "The chin position could not be confirmed, so the collar cannot be moved",
                            "Çene konumu doğrulanamadığı için yaka taşınamaz")
        if collar_enabled:
            collar_note = None
        else:
            select_btn.state(["disabled"])
            lasso_btn.state(["disabled"])
            collar_note = ttk.Label(top_bar, text=collar_off_text, style="FMCollarOn.TLabel")
        self._flow(top_bar, gap=6)

        bg = "#151517" if dark else "#eeeef0"
        # 襟の選択・移動中は外枠をアクセント色にして、枠の操作中と見分けられるようにする
        canvas = tk.Canvas(win, width=cw, height=ch, highlightthickness=3, bd=0,
                           background=bg, highlightbackground=bg, highlightcolor=bg, takefocus=1)
        canvas.pack(padx=9, pady=(0, 6))
        img_item = canvas.create_image(0, 0, anchor="nw")
        frame_shadow = canvas.create_rectangle(0, 0, 0, 0, outline="#000000", width=4)
        frame_line = canvas.create_rectangle(0, 0, 0, 0, outline="#ffffff", width=2)
        # 回転中は選択範囲が画面上で傾くことがあるので、選択線は多角形で描く
        sel_shadow = canvas.create_polygon(0, 0, 0, 0, 0, 0, outline="#000000", fill="", width=3,
                                           state="hidden")
        sel_line = canvas.create_polygon(0, 0, 0, 0, 0, 0, outline="#ffffff", fill="", width=1,
                                         dash=(4, 4), state="hidden")
        handles = {
            corner: canvas.create_oval(0, 0, 0, 0, fill="#ffffff", outline="#202020", width=2)
            for corner in ("bl", "br")
        }
        # 投げ縄の途中経過: なぞった線（実線）、ボタンを離すと結ばれる終点→始点（点線）、始点の目印
        lasso_shadow = canvas.create_line(0, 0, 0, 0, fill="#000000", width=4, state="hidden")
        lasso_line = canvas.create_line(0, 0, 0, 0, fill=accent, width=2, state="hidden")
        lasso_close = canvas.create_line(0, 0, 0, 0, fill="#ffffff", width=1, dash=(3, 3), state="hidden")
        lasso_first = canvas.create_oval(0, 0, 0, 0, fill=accent, outline="#ffffff", width=1, state="hidden")

        view_row = ttk.Frame(win)
        view_row.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Button(view_row, text=t("全体表示", "Fit to view", "Tümünü göster"),
                   command=lambda: fit_view()).pack(side="left")
        ttk.Button(view_row, text=t("枠を自動の位置に戻す", "Reset frame to auto", 'Çerçeveyi otomatiğe döndür'),
                   command=lambda: reset_frame()).pack(side="left", padx=(6, 0))
        # 出力サイズは専用の1行に置く（表示ボタンと同じ行だと、長い訳語で詰まる）。
        # 1行に収めるのは、125%表示（1536×864相当）でも下のボタンが画面内に残るようにするため。
        size_box = ttk.Frame(win)
        size_box.pack(fill="x", padx=12, pady=(0, 4))
        ttk.Label(size_box, text=t("出力サイズ（この実行の次の画像にも引き継ぐ）:", "Output size (kept for the next images of this run):",
                                   'Çıktı boyutu (bu çalıştırmanın sonraki görüntüleri için korunur):')).pack(side="left")
        for value in ("180", "250"):
            ttk.Radiobutton(size_box, text=f"{value}×{value}", value=value, variable=size_choice,
                            style=choice_style,
                            command=lambda: on_size_change()).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(size_box, text=t("任意", "Custom", "Özel"), value="custom", variable=size_choice,
                        style=choice_style, command=lambda: on_size_change()).pack(side="left", padx=(6, 0))
        size_entry = ttk.Entry(size_box, textvariable=size_custom, width=5)
        size_entry.pack(side="left", padx=(4, 0))
        ttk.Label(size_box, text="px", style="Sub.TLabel").pack(side="left", padx=(2, 0))
        size_custom.trace_add("write", on_custom_typed)

        angle_row = ttk.Frame(win)
        angle_row.pack(fill="x", padx=12, pady=(0, 4))
        angle_var = tk.DoubleVar(value=0.0)
        angle_text = tk.StringVar(value="+0.0°")
        ttk.Label(angle_row, text=t("回転（微調整）", "Rotation (fine-tune)", 'Döndürme (ince ayar)')).pack(side="left")
        angle_scale = ttk.Scale(angle_row, variable=angle_var, from_=-PREVIEW_ANGLE_LIMIT,
                                to=PREVIEW_ANGLE_LIMIT, length=280,
                                command=lambda v: set_angle(v))
        angle_scale.pack(side="left", padx=8)
        angle_label = ttk.Label(angle_row, textvariable=angle_text, width=7)
        angle_label.pack(side="left")
        ttk.Button(angle_row, text=t("0°に戻す", "Reset to 0°", "0°'ye döndür"),
                   command=lambda: set_angle(0.0)).pack(side="left", padx=(6, 0))

        banner = ttk.Label(win, wraplength=content_w, justify="left")
        banner.pack(fill="x", padx=12, pady=(2, 2))
        ttk.Label(win, wraplength=content_w, justify="left", text=t(
            "ホイールでカーソル位置を中心に拡大・縮小、拡大中は中ボタンのドラッグで表示を移動します"
            "（表示の拡大は保存に影響しません）。回転は−15°〜＋15°（スライダーをクリック後、矢印キーで0.1°・"
            "Shift＋矢印で1°）。枠線や暗い部分は表示用で、保存されるのは枠の内側だけです。",
            "Use the mouse wheel to zoom around the cursor and drag with the middle button to pan while "
            "zoomed (zoom never affects the saved image). Rotation fine-tunes by -15° to +15° (after "
            "clicking the slider: arrow keys 0.1°, Shift+arrow 1°). Lines and dimming are display only; "
            "just the inside of the frame is saved.",
            "Fare tekerleğiyle imleç çevresinde yakınlaştırın; yakınlaştırılmışken orta tuşla sürükleyerek "
            "kaydırın (yakınlaştırma kaydedilen görüntüyü etkilemez). Döndürme -15° ile +15° arasında ince "
            "ayardır (kaydırıcıya tıkladıktan sonra ok tuşu 0,1°, Shift+ok 1°). Çizgiler ve karartma "
            "yalnızca görüntü içindir; yalnızca çerçevenin içi kaydedilir.")).pack(fill="x", padx=12, pady=(0, 4))
        status_label = ttk.Label(win, wraplength=content_w, justify="left")
        status_label.pack(fill="x", padx=12, pady=(0, 4))

        # ---------------- 描画 ----------------
        def rebuild_edited():
            """襟の移動を反映した元画像の大きさの画像を作り直す。"""
            moves = all_moves()
            cache["edited"] = apply_collar_moves(base, moves, protect_box) if moves else base
            cache["reduced"] = {}

        def rebuild_comp():
            """ズーム・表示位置・回転を1回のアフィン変換で反映し、市松模様に重ねる。

            stretch で枠と回転が初期状態のときは、保存結果と同じく画像全体を枠いっぱいに
            （縦横別の倍率で）引き伸ばして表示する。"""
            s = cur_scale()
            stretched = stretch_view_active()
            cache["stretch_shown"] = stretched
            if stretched:
                left, top, side = st["frame"]
                sx_scale, sy_scale = img_w / float(side), img_h / float(side)
                a, b, c = sx_scale, 0.0, (view["ox"] - left) * sx_scale
                d, e, f = 0.0, sy_scale, (view["oy"] - top) * sy_scale
                # 画面1pxあたりの元画素数（縦横の大きい方）で平均縮小の段数を決める
                src_per_px = max(sx_scale, sy_scale) / s
            else:
                a, b, c, d, e, f = _rotation_affine(st["angle"], rotate_center, view["ox"], view["oy"])
                src_per_px = 1.0 / s
            # 縮小表示では、先に整数分の1へ平均縮小してから補間する（細部のちらつき防止）
            k = max(1, int(src_per_px))
            src = cache["reduced"].get(k)
            if src is None:
                src = cache["edited"].convert("RGBa")
                if k > 1:
                    src = src.reduce(k)
                cache["reduced"][k] = src
            coeffs = (a / s / k, b / s / k, c / k, d / s / k, e / s / k, f / k)
            # 大きく拡大したときは画素の境目が分かるよう補間しない
            method = resampling.NEAREST if 1.0 / src_per_px >= 2.0 else resampling.BILINEAR
            shown = src.transform((cw, ch), _I.AFFINE, coeffs, resample=method).convert("RGBA")
            comp = checker.copy()
            comp.alpha_composite(shown)
            cache["comp"] = comp

        def float_canvas_poly(f):
            dx, dy = float_offset(f)
            pts = []
            for x, y in f["poly"]:
                pts.extend(to_canvas(*to_view(x + dx, y + dy)))
            return pts

        def float_mask(f):
            """投げ縄の選択で実際に動く画素（反対側の襟と重なった部分を除いた有効範囲）。
            (box, bool配列) を返し、選択ごとに1回だけ作る。輪郭表示・ドラッグ判定・画素移動
            （apply_collar_moves の lasso_selection_mask）はすべてこの範囲を使う。"""
            cached = f.get("_mask")
            if cached is None:
                import numpy as np
                xs = [x for x, _ in f["poly"]]
                ys = [y for _, y in f["poly"]]
                box = normalize_selection(min(xs), min(ys), max(xs) + 1, max(ys) + 1, base.size)
                if box is None:
                    cached = (None, None)
                else:
                    cached = (box, np.asarray(lasso_selection_mask(f, box)) > 0)
                f["_mask"] = cached
            return cached

        def float_outline_src(f):
            """選択の輪郭（元画像の座標・移動前）の一覧。反対側の襟で選んだ範囲を除いた投げ縄では、
            なぞった線ではなく実際に動く範囲（float_mask）の輪郭を描く（境目の画素の中心を結んだ線）。
            動く画素はすべて輪郭で囲む（小さな切れ端も描く）。"""
            if not f.get("exclude"):
                return [f["poly"]]
            cached = f.get("_outline")
            if cached is not None:
                return cached
            outline = [f["poly"]]
            try:
                import cv2
                import numpy as np
                box, sel = float_mask(f)
                if box is not None:
                    mask = sel.astype(np.uint8) * 255
                    # 1px の余白を付けて、画像の端に接する輪郭も閉じた線にする
                    padded = np.pad(mask, 1)
                    found = cv2.findContours(padded, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
                    contours = found[0] if len(found) == 2 else found[1]
                    parts = []
                    for c in contours:
                        c = c.reshape(-1, 2)
                        # 画素 (col, row) の中心 = 元画像の (box_x0 + col + 0.5, box_y0 + row + 0.5)
                        pts = [(box[0] + float(cx_) - 0.5, box[1] + float(cy_) - 0.5) for cx_, cy_ in c]
                        if len(pts) < 3:
                            # 1〜2画素の切れ端：画素の四角で囲む（動く画素に輪郭が無い状態を作らない）
                            px0 = min(p[0] for p in pts) - 0.5
                            py0 = min(p[1] for p in pts) - 0.5
                            px1 = max(p[0] for p in pts) + 0.5
                            py1 = max(p[1] for p in pts) + 0.5
                            pts = [(px0, py0), (px1, py0), (px1, py1), (px0, py1)]
                        parts.append(pts)
                    if parts:
                        outline = parts
            except Exception:  # noqa: BLE001
                pass   # 輪郭を作れなければ、なぞった線を描く（表示だけ）
            f["_outline"] = outline
            return outline

        def float_canvas_parts(f):
            dx, dy = float_offset(f)
            return [[c for x, y in part for c in to_canvas(*to_view(x + dx, y + dy))]
                    for part in float_outline_src(f)]

        def redraw():
            if stretch_full and stretch_view_active() != cache.get("stretch_shown"):
                # 枠や回転が初期状態から外れた／戻った: 引き伸ばし表示と切り抜き表示を切り替える
                rebuild_comp()
            left, top, side = st["frame"]
            fx0, fy0 = to_canvas(left, top)
            fx1, fy1 = to_canvas(left + side, top + side)
            # 枠外を薄暗くする（襟を選ぶときも見える濃さ）。表示専用で保存されない。
            dim = _I.new("RGBA", (cw, ch), (0, 0, 0, 125))
            _D.Draw(dim).rectangle((int(round(fx0)), int(round(fy0)),
                                    int(round(fx1)) - 1, int(round(fy1)) - 1), fill=(0, 0, 0, 0))
            shown = cache["comp"].copy()
            shown.alpha_composite(dim)
            photo = ImageTk.PhotoImage(shown)
            canvas.itemconfigure(img_item, image=photo)
            cache["photo"] = photo
            canvas.coords(frame_shadow, fx0, fy0, fx1, fy1)
            canvas.coords(frame_line, fx0, fy0, fx1, fy1)
            r = 6
            for corner, hx in (("bl", fx0), ("br", fx1)):
                canvas.coords(handles[corner], hx - r, fy1 - r, hx + r, fy1 + r)
            drag = st["drag"]
            parts = None
            if drag is not None and drag["kind"] == "select":
                (sx, sy), (ex, ey) = drag["start"], drag["cur"]
                pts = []
                for x, y in ((sx, sy), (ex, sy), (ex, ey), (sx, ey)):
                    pts.extend(to_canvas(x, y))
                parts = [pts]
            elif st["float"] is not None:
                parts = float_canvas_parts(st["float"])
            canvas.delete("selx")
            if not parts:
                canvas.itemconfigure(sel_shadow, state="hidden")
                canvas.itemconfigure(sel_line, state="hidden")
            else:
                for item in (sel_shadow, sel_line):
                    canvas.coords(item, *parts[0])
                    canvas.itemconfigure(item, state="normal")
                # 範囲が複数に分かれたときの残りの輪郭
                for extra in parts[1:]:
                    canvas.create_polygon(*extra, outline="#000000", fill="", width=3, tags="selx")
                    canvas.create_polygon(*extra, outline="#ffffff", fill="", width=1, dash=(4, 4),
                                          tags="selx")
            draw_lasso_items()
            refresh_info()

        def draw_lasso_items():
            """投げ縄の線だけを描き直す（画像は作り直さない）。ドラッグ中はこれだけを呼ぶ。
            描画に時間がかかると、Tk は溜まったマウス移動を1つにまとめてしまい、途中の点が抜けて
            曲線が直線に置き換わるため、線の更新は軽く保つ。"""
            ld = st["lasso"]
            if ld is None or not ld["pts"]:
                for item in (lasso_shadow, lasso_line, lasso_close, lasso_first):
                    canvas.itemconfigure(item, state="hidden")
            else:
                # 点は元画像の座標で持つので、ズーム・表示位置・回転を変えても画像上の同じ場所に出る
                cpts = [to_canvas(*to_view(x, y)) for x, y in ld["pts"]]
                line = [c for pt in cpts for c in pt]
                if len(line) < 4:
                    line = line + line
                for item in (lasso_shadow, lasso_line):
                    canvas.coords(item, *line)
                    canvas.itemconfigure(item, state="normal")
                # ボタンを離すと終点と始点を結ぶ（その線を点線で先に見せる）
                canvas.coords(lasso_close, *cpts[-1], *cpts[0])
                canvas.itemconfigure(lasso_close, state="normal")
                fx0_, fy0_ = cpts[0]
                canvas.coords(lasso_first, fx0_ - 4, fy0_ - 4, fx0_ + 4, fy0_ + 4)
                canvas.itemconfigure(lasso_first, state="normal")

        pending = {"id": None, "level": 0}

        def schedule(level=0):
            """キー押しっぱなしやドラッグ中の連続イベントを、アイドル時の1回の描画にまとめる。

            level 2 = 襟の移動から作り直す、1 = 回転・ズーム・表示位置だけ作り直す、0 = 線だけ。"""
            pending["level"] = max(pending["level"], level)
            if pending["id"] is None:
                pending["id"] = win.after_idle(flush)

        def flush():
            pending["id"] = None
            level, pending["level"] = pending["level"], 0
            try:
                if level >= 2:
                    rebuild_edited()
                if level >= 1:
                    rebuild_comp()
                redraw()
            except tk.TclError:
                pass  # ダイアログが閉じられた後

        def side_name(move):
            return "left" if move_center_x(move) < face_cx else "right"

        def refresh_info():
            names = {"left": t("左", "Left", 'Sol'), "right": t("右", "Right", 'Sağ')}
            f = st["float"]
            ld = st["lasso"]
            lasso_on = st["lasso_armed"] or ld is not None
            collar_active = (st["armed"] or f is not None or lasso_on
                             or (st["drag"] or {}).get("kind") == "select")
            if ld is not None:
                banner.configure(text=t(
                    "● 投げ縄で描いています: 首と襟の境目に沿ってなぞり、ボタンを離すと始点と終点を結んで"
                    "囲んだ範囲を選択します（Escで中止）",
                    "● Drawing the lasso: trace along the neck/collar boundary; releasing the button joins the end to "
                    "the start and selects the enclosed area (Esc cancels)",
                    "● Kement çiziliyor: boyun/yaka sınırı boyunca çizin; tuşu bırakınca bitiş başlangıca bağlanır ve "
                    "çevrili alan seçilir (Esc iptal eder)"), style="FMCollarOn.TLabel")
            elif st["lasso_armed"]:
                banner.configure(text=t(
                    "● 襟を自由選択（投げ縄）: 左ボタンを押したまま、首と襟の境目に沿って片側の襟を囲むように"
                    "描いてください（画像の外も可）。反対側の襟と中央で重ねて描いても、重なった所は二重に動きません。Escで中止",
                    "● Lasso collar selection: hold the left button and draw around one side of the collar, following "
                    "the neck/collar boundary (outside the image is fine). Overlapping the other side in the middle "
                    "never moves those pixels twice. Esc cancels",
                    "● Kement yaka seçimi: sol tuşu basılı tutup boyun/yaka sınırını izleyerek yakanın bir tarafının "
                    "çevresini çizin (görüntünün dışı da olur). Ortada diğer tarafla çakışması pikselleri iki kez "
                    "taşımaz. Esc iptal eder"), style="FMCollarOn.TLabel")
            elif st["armed"] or (st["drag"] or {}).get("kind") == "select":
                banner.configure(text=t(
                    "● 襟を選択中: 片側の襟をドラッグで四角く囲んでください（枠の外も可）。Escで中止",
                    "● Selecting collar: drag a rectangle around one side of the collar (outside the frame "
                    "is fine). Esc cancels",
                    "● Yaka seçiliyor: yakanın bir tarafını dikdörtgenle sürükleyerek seçin (çerçeve dışı da "
                    "olur). Esc iptal eder"), style="FMCollarOn.TLabel")
            elif f is not None:
                where = names[side_name(float_move(f))]
                dx, dy = float_offset(f)
                banner.configure(text=t(
                    f"● 襟を移動中（{where}  {dx:+d}, {dy:+d}px）: 矢印キー（Shiftで10倍）か選択の内側の"
                    "ドラッグで動かし、Enterで確定・Escで取り消し",
                    f"● Moving collar ({where}  {dx:+d}, {dy:+d}px): use the arrow keys (Shift = 10x) or drag "
                    "inside the selection; Enter commits, Esc cancels",
                    f"● Yaka taşınıyor ({where}  {dx:+d}, {dy:+d} px): ok tuşları (Shift = 10 kat) veya seçimin "
                    "içinden sürükleme; Enter onaylar, Esc iptal eder"), style="FMCollarOn.TLabel")
            elif time.monotonic() - st["collar_blocked"] < 3.0:
                banner.configure(text="● " + collar_off_text, style="FMCollarOn.TLabel")
            else:
                banner.configure(text=t(
                    "枠の操作: 枠の内側をドラッグで移動、下の角の●で大きさを変更（上辺の高さは固定）。"
                    "矢印キーでも枠が動きます",
                    "Frame: drag inside it to move, drag a bottom corner to resize (the top edge stays). The "
                    "arrow keys also move the frame",
                    "Çerçeve: taşımak için içinden sürükleyin, boyutlandırmak için alt köşeyi sürükleyin (üst "
                    "kenar sabit). Ok tuşları da çerçeveyi taşır"), style="TLabel")
            ring = accent if collar_active else bg
            canvas.configure(highlightbackground=ring, highlightcolor=ring)
            select_btn.configure(style="Accent.TButton" if st["armed"] else "TButton")
            lasso_btn.configure(style="Accent.TButton" if lasso_on else "TButton")
            if collar_enabled:
                done = {side_name(m) for m in st["moves"]}
                marks = []
                for key in ("left", "right"):
                    mark = t("確定済み", "committed", 'onaylandı') if key in done else t("未", "not yet", 'henüz değil')
                    marks.append(f"{names[key]}: {mark}")
                collar_txt = t(f"襟 {' / '.join(marks)}", f"Collar {' / '.join(marks)}",
                               f"Yaka {' / '.join(marks)}")
            else:
                collar_txt = None   # 襟を動かせないことは上部の案内に出ている
            side = st["frame"][2]
            out = chosen_size()
            if out is not None:
                dest = t(f"保存 {out}×{out}px", f"saved as {out}×{out}px", f"{out}×{out} px")
            elif size_choice.get() == "custom":
                dest = t("任意のサイズを 1〜4096 px で入力してください",
                         "enter a custom size of 1-4096 px", "1-4096 px arası özel boyut girin")
            else:
                dest = t("出力サイズを選ぶと保存できます", "choose an output size to save",
                         "kaydetmek için çıktı boyutu seçin")
            if stretch_view_active():
                frame_txt = t(f"画像全体 {img_w}×{img_h}px を引き伸ばし → {dest}",
                              f"Whole image {img_w}×{img_h}px stretched → {dest}",
                              f"Görüntünün tamamı {img_w}×{img_h} px esnetilir → {dest}")
            else:
                frame_txt = t(f"枠 {side}×{side}px → {dest}",
                              f"Frame {side}×{side}px → {dest}",
                              f"Çerçeve {side}×{side} px → {dest}")
            parts = [p for p in (collar_txt, frame_txt) if p]
            status_label.configure(text=t(
                "  ・  ".join(parts) + f"  ・  回転 {st['angle']:+.1f}°"
                f"  ・  表示 {view['zoom'] * 100:.0f}%",
                "  ·  ".join(parts) + f"  ·  "
                f"Rotation {st['angle']:+.1f}°  ·  View {view['zoom'] * 100:.0f}%",
                "  ·  ".join(parts) + f"  ·  "
                f"Döndürme {st['angle']:+.1f}°  ·  Görünüm %{view['zoom'] * 100:.0f}"))
            commit_btn.state(["!disabled"] if f is not None else ["disabled"])
            undo_btn.state(["!disabled"] if (f is not None or st["moves"] or ld is not None)
                           else ["disabled"])

        # ---------------- 回転 ----------------
        def set_angle(value):
            angle = clamp_preview_angle(value)
            if abs(float(angle_var.get()) - angle) > 1e-6:
                angle_var.set(angle)
            angle_text.set(f"{angle:+.1f}°")
            if angle != st["angle"]:
                st["angle"] = angle
                schedule(1)

        def nudge_angle(delta):
            set_angle(st["angle"] + delta)
            return "break"

        for key, delta in (("Left", -0.1), ("Down", -0.1), ("Right", 0.1), ("Up", 0.1)):
            angle_scale.bind(f"<{key}>", lambda e, d=delta: nudge_angle(d))
            angle_scale.bind(f"<Shift-{key}>", lambda e, d=delta: nudge_angle(d * 10))

        def focus_angle_scale(_e=None):
            """つまみ・目盛りのクリックでキーボードフォーカスをスライダーへ移す。
            ttk.Scale はクリックではフォーカスを取らないため、そのままだと矢印キーが
            画像側（保存枠の移動）へ届いてしまう。ドラッグ処理は既定のまま続けさせる。"""
            angle_scale.focus_set()

        angle_scale.bind("<ButtonPress-1>", focus_angle_scale, add="+")
        # スライダーにフォーカスがある間は角度の表示を強調し、矢印キーの行き先を分かるようにする
        angle_scale.bind("<FocusIn>", lambda e: angle_label.configure(style="FMCollarOn.TLabel"), add="+")
        angle_scale.bind("<FocusOut>", lambda e: angle_label.configure(style="TLabel"), add="+")

        # ---------------- 襟の移動 ----------------
        def show_collar_blocked():
            """襟を選べない画像で襟を選ぼうとしたときの案内（3秒表示）。"""
            st["collar_blocked"] = time.monotonic()
            refresh_info()
            # ダイアログ側の after は閉じた時点でコマンドが消え、Tcl のエラーが出るので本体に登録する
            self.after(3100, lambda: win.winfo_exists() and refresh_info())

        def arm_select():
            """「襟を四角で選択」: 次のドラッグで襟を囲む。動かした選択は先に確定する。"""
            if not collar_enabled:
                show_collar_blocked()
                return
            commit_float()
            st["lasso_armed"] = False
            st["lasso"] = None
            st["armed"] = True
            canvas.focus_set()
            canvas.configure(cursor="crosshair")
            redraw()

        def commit_float():
            f = st["float"]
            if f is not None and any(float_offset(f)):
                st["moves"].append(float_move(f))
            st["float"] = None
            redraw()

        def cancel_collar():
            """Esc: 選択を始めたところなら中止し、編集中の選択は元の位置へ戻して解除する。"""
            if st["lasso"] is not None or st["lasso_armed"]:
                # 描いている途中でも、押した直後でも、投げ縄をやめる（Esc は左ボタンを押したままでも効く）
                st["lasso"] = None
                st["lasso_armed"] = False
                if (st["drag"] or {}).get("kind") == "lasso":
                    st["drag"] = None
            elif st["armed"]:
                st["armed"] = False
            else:
                f = st["float"]
                st["float"] = None
                if f is not None and any(float_offset(f)):
                    schedule(2)
            canvas.configure(cursor="")
            redraw()

        LASSO_STEP_PX = 1.5   # 投げ縄: 前の点から画面上でこの距離以上動いたら点を足す

        def arm_lasso():
            """「襟を自由選択（投げ縄）」: 次の左ドラッグで線を描き、離すと囲んだ範囲を選ぶ。
            動かした選択は先に確定する。"""
            if not collar_enabled:
                show_collar_blocked()
                return
            commit_float()
            st["armed"] = False
            st["lasso_armed"] = True
            st["lasso"] = None
            canvas.focus_set()
            canvas.configure(cursor="crosshair")
            redraw()

        def lasso_start(px, py):
            """左ボタンを押した: 投げ縄の始点（画面の座標）。点は元画像の座標で記録する
            （ズーム・回転を変えても、なぞった場所は画像上で変わらない）。"""
            st["lasso"] = {"pts": [to_source(*to_image(px, py))]}
            st["drag"] = {"kind": "lasso"}
            redraw()

        def lasso_add(px, py, force=False):
            """ドラッグ中: 前の点から画面上で LASSO_STEP_PX 以上離れたら点を足す。"""
            ld = st["lasso"]
            if ld is None:
                return
            lx, ly = to_canvas(*to_view(*ld["pts"][-1]))
            if force or math.hypot(px - lx, py - ly) >= LASSO_STEP_PX:
                if math.hypot(px - lx, py - ly) > 0.01:
                    ld["pts"].append(to_source(*to_image(px, py)))
                draw_lasso_items()

        def poly_area(poly):
            return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))) / 2.0

        def lasso_finish(px, py):
            """左ボタンを離した: 終点と始点を結び、囲んだ範囲を移動できる選択にする。
            反対側の襟で確定済みの投げ縄と重なった部分は選ばない（"exclude"）。中央で少し重ねて
            描けば、左右の継ぎ目に隙間も二重の移動もできない。小さすぎる線（クリックだけなど）は
            選択にせず、投げ縄を押した状態のまま次のドラッグを待つ。"""
            ld = st["lasso"]
            if ld is None:
                return
            lasso_add(px, py, force=True)
            st["lasso"] = None
            pts = [(float(x), float(y)) for x, y in ld["pts"]]
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            s = cur_scale()
            if len(pts) < 3 or poly_area(pts) * s * s < 25.0 \
                    or normalize_selection(min(xs), min(ys), max(xs) + 1, max(ys) + 1, base.size) is None:
                redraw()
                return    # 線だけ・画像の外だけ
            side = "left" if polygon_centroid_x(pts) < face_cx else "right"
            excl = [[(float(x), float(y)) for x, y in m["poly"]] for m in st["moves"]
                    if isinstance(m, dict) and m.get("fill") == "center" and side_name(m) != side]
            f = {"rect": None, "poly": pts, "fx": 0.0, "fy": 0.0, "hist": [], "fill": "center"}
            if excl:
                f["exclude"] = excl
            box, sel = float_mask(f)
            if box is None or not sel.any():
                redraw()
                return    # 囲んだ範囲がすべて反対側の選択の中
            st["lasso_armed"] = False
            st["float"] = f
            canvas.configure(cursor="")
            redraw()

        def on_enter():
            """Enter: 移動中の選択を確定する（投げ縄を描いている途中は何もしない）。"""
            if st["lasso"] is not None:
                return
            commit_float()

        def undo():
            if st["lasso"] is not None:
                # 描いている途中の線を捨てる（投げ縄を押した状態に戻る）
                st["lasso"] = None
                if (st["drag"] or {}).get("kind") == "lasso":
                    st["drag"] = None
                redraw()
                return
            f = st["float"]
            if f is not None and f["hist"]:
                f["fx"], f["fy"] = f["hist"].pop()
                st["last_nudge"] = 0.0
            elif f is not None:
                st["float"] = None
            elif st["moves"]:
                st["moves"].pop()
            else:
                return
            schedule(2)

        def nudge(ddx, ddy, big=False):
            """矢印キー: 襟を選択していれば襟を、していなければ保存枠を画面上の向きへ動かす。"""
            if st["lasso"] is not None or st["lasso_armed"]:
                return    # 投げ縄で選ぶ途中は、枠も選択も動かさない
            step = step_px() * (10 if big else 1)
            f = st["float"]
            if f is not None:
                now = time.monotonic()
                # キーを押しっぱなし・連打した一続きの移動は、取り消し1回でまとめて戻す
                if not f["hist"] or now - st["last_nudge"] > 0.6:
                    f["hist"].append((f["fx"], f["fy"]))
                st["last_nudge"] = now
                # 画面上の上下左右へ動かす（回転中は元画像の座標では斜めの移動になる）
                vx, vy = screen_vector_to_source(ddx * step, ddy * step)
                f["fx"] += vx
                f["fy"] += vy
                schedule(2)
            else:
                left, top, side = st["frame"]
                st["frame"] = clamp_frame(left + ddx * step, top + ddy * step, side)
                schedule()

        # ---------------- 保存枠 ----------------
        def clamp_frame(left, top, side):
            side = int(max(min_side, min(side, vx1 - vx0, vy1 - vy0)))
            left = int(min(max(left, vx0), vx1 - side))
            top = int(min(max(top, vy0), vy1 - side))
            return [left, top, side]

        def reset_frame():
            st["frame"] = list(auto_rect)
            redraw()

        def hit_handle(px, py):
            left, top, side = st["frame"]
            for corner, hx in (("bl", left), ("br", left + side)):
                cx, cy = to_canvas(hx, top + side)
                if abs(px - cx) <= 10 and abs(py - cy) <= 10:
                    return corner
            return None

        def snap_selection(box):
            """新しい選択の辺が、確定済みの移動の辺から画面上4px以内ならそろえる。

            左右を中央で分けて選ぶと、手作業では1px重なったり隙間ができたりして、
            重なった列だけ二重に動いて線が出る。辺をそろえてそれを防ぐ（0°のときだけ）。"""
            tol = max(1, int(round(4.0 / cur_scale())))
            xs, ys = set(), set()
            for move in st["moves"]:
                if isinstance(move, dict):
                    continue
                x0, y0, x1, y1, dx, dy = move
                xs.update((x0, x1, x0 + dx, x1 + dx))
                ys.update((y0, y1, y0 + dy, y1 + dy))

            def snap(v, candidates):
                best = min(candidates, key=lambda c: abs(c - v), default=None)
                return best if best is not None and abs(best - v) <= tol else v

            x0, y0, x1, y1 = box
            snapped = normalize_selection(snap(x0, xs), snap(y0, ys), snap(x1, xs), snap(y1, ys),
                                          base.size)
            return snapped or box

        def make_selection(start, end):
            """画面で囲んだ四角形（回転後の座標）を、元画像の座標の選択範囲にする。"""
            (sx, sy), (ex, ey) = start, end
            if abs(st["angle"]) < 0.05:
                box = normalize_selection(sx, sy, ex, ey, base.size)
                if box is None:
                    return None
                box = snap_selection(box)
                x0, y0, x1, y1 = box
                return {"rect": box, "poly": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                        "fx": 0.0, "fy": 0.0, "hist": []}
            if abs(ex - sx) < 2 or abs(ey - sy) < 2:
                return None
            corners = ((sx, sy), (ex, sy), (ex, ey), (sx, ey))
            poly = [to_source(x, y) for x, y in corners]
            xs = [x for x, _ in poly]
            ys = [y for _, y in poly]
            if normalize_selection(min(xs), min(ys), max(xs), max(ys), base.size) is None:
                return None  # 画像の外だけを囲んだ
            return {"rect": None, "poly": poly, "fx": 0.0, "fy": 0.0, "hist": []}

        def in_frame(x, y):
            left, top, side = st["frame"]
            return left <= x <= left + side and top <= y <= top + side

        def in_float(x, y):
            """回転後の座標 (x, y) が、移動中の選択範囲の内側か。
            投げ縄は実際に動く画素（float_mask：反対側の襟と重なった部分を除く）で判定するので、
            除外した部分を押しても選択はドラッグされない（枠の中なら枠の操作になる）。"""
            f = st["float"]
            if f is None:
                return False
            dx, dy = float_offset(f)
            px, py = to_source(x, y)
            px, py = px - dx, py - dy
            if f.get("fill") == "center":
                box, sel = float_mask(f)
                if box is None:
                    return False
                ix, iy = int(math.floor(px)), int(math.floor(py))
                return (box[0] <= ix < box[2] and box[1] <= iy < box[3]
                        and bool(sel[iy - box[1], ix - box[0]]))
            inside = False
            pts = f["poly"]
            for i in range(len(pts)):
                (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
                if (y1 > py) != (y2 > py) and px < (x2 - x1) * (py - y1) / (y2 - y1) + x1:
                    inside = not inside
            return inside

        # ---------------- マウス ----------------
        def on_press(e):
            canvas.focus_set()
            if st["lasso_armed"] and collar_enabled:
                lasso_start(e.x, e.y)   # 「襟を自由選択（投げ縄）」の直後: 押したまま線を描く
                return
            x, y = to_image(e.x, e.y)
            shift = bool(int(e.state) & 0x0001)
            if collar_enabled and (st["armed"] or shift):   # 「襟を選択」の直後、または Shift＋ドラッグ
                commit_float()   # 動かした選択は確定してから（Photopeaの選択解除と同じ）
                st["drag"] = {"kind": "select", "start": (x, y), "cur": (x, y)}
                return
            if shift:
                # 顎の位置が分からない画像では襟を選ばず、通常の枠の操作として扱う
                show_collar_blocked()
            corner = hit_handle(e.x, e.y)
            if corner is not None:
                left, top, side = st["frame"]
                st["drag"] = {"kind": "resize", "corner": corner, "left": left,
                              "right": left + side, "top": top}
            elif in_float(x, y):
                f = st["float"]
                f["hist"].append((f["fx"], f["fy"]))
                st["drag"] = {"kind": "float", "start": (x, y), "orig": (f["fx"], f["fy"])}
            elif in_frame(x, y):
                st["drag"] = {"kind": "frame", "start": (x, y), "orig": list(st["frame"])}

        def on_motion(e):
            drag = st["drag"]
            if drag is None:
                return
            if drag["kind"] == "lasso":
                lasso_add(e.x, e.y)
                return
            x, y = to_image(e.x, e.y)
            if drag["kind"] == "frame":
                left, top, side = drag["orig"]
                st["frame"] = clamp_frame(left + round(x - drag["start"][0]),
                                          top + round(y - drag["start"][1]), side)
                schedule()
            elif drag["kind"] == "resize":
                # 上辺の高さは固定。右下は左辺、左下は右辺を固定して1:1で広げる。
                top = drag["top"]
                if drag["corner"] == "br":
                    left = drag["left"]
                    side = min(max(x - left, y - top), vx1 - left, vy1 - top)
                    side = max(min_side, int(round(side)))
                else:
                    right = drag["right"]
                    side = min(max(right - x, y - top), right - vx0, vy1 - top)
                    side = max(min_side, int(round(side)))
                    left = right - side
                st["frame"] = [int(left), int(top), side]
                schedule()
            elif drag["kind"] == "float":
                f = st["float"]
                vx, vy = screen_vector_to_source(x - drag["start"][0], y - drag["start"][1])
                f["fx"] = drag["orig"][0] + vx
                f["fy"] = drag["orig"][1] + vy
                schedule(2)
            elif drag["kind"] == "select":
                drag["cur"] = (x, y)
                schedule()

        def on_release(e):
            drag = st["drag"]
            st["drag"] = None
            if drag is None:
                return
            if drag["kind"] == "lasso":
                lasso_finish(e.x, e.y)
            elif drag["kind"] == "select":
                st["armed"] = False
                sel = make_selection(drag["start"], to_image(e.x, e.y))
                if sel is not None:
                    st["float"] = sel
            elif drag["kind"] == "float":
                f = st["float"]
                if f["hist"] and float_offset({"fx": f["hist"][-1][0], "fy": f["hist"][-1][1]}) \
                        == float_offset(f):
                    f["hist"].pop()  # クリックしただけ（動いていない）
            on_hover(e)
            redraw()

        def on_double(e):
            """ダブルクリックの2回目の押下も、通常の押下として扱う（投げ縄を描き始められる）。"""
            return on_press(e)

        def on_hover(e):
            if st["drag"] is not None:
                return
            x, y = to_image(e.x, e.y)
            if st["armed"] or st["lasso_armed"]:
                cursor = "crosshair"
            else:
                corner = hit_handle(e.x, e.y)
                if corner == "br":
                    cursor = "size_nw_se"
                elif corner == "bl":
                    cursor = "size_ne_sw"
                else:
                    cursor = "fleur" if (in_float(x, y) or in_frame(x, y)) else ""
            try:
                canvas.configure(cursor=cursor)
            except tk.TclError:
                canvas.configure(cursor="")

        def on_wheel(e):
            """ホイール: カーソル直下を中心に拡大・縮小（表示だけ）。"""
            if e.widget is canvas:
                px, py = e.x, e.y
            else:
                px, py = e.x_root - canvas.winfo_rootx(), e.y_root - canvas.winfo_rooty()
            if not (0 <= px < cw and 0 <= py < ch) or not e.delta:
                return None
            # 通常のホイール1目盛り（120）で1.25倍。タッチパッドの細かい量はその分だけ拡大する
            zoom_at(px, py, 1.25 ** (max(-480, min(480, int(e.delta))) / 120.0))
            return "break"

        def on_pan_press(e):
            st["pan"] = (e.x, e.y)
            canvas.configure(cursor="fleur")

        def on_pan_motion(e):
            last = st.get("pan")
            if last is None:
                return
            st["pan"] = (e.x, e.y)
            pan_by(e.x - last[0], e.y - last[1])

        def on_pan_release(e):
            st["pan"] = None
            on_hover(e)

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<Double-Button-1>", on_double)
        canvas.bind("<B1-Motion>", on_motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Motion>", on_hover)
        canvas.bind("<ButtonPress-2>", on_pan_press)
        canvas.bind("<B2-Motion>", on_pan_motion)
        canvas.bind("<ButtonRelease-2>", on_pan_release)
        win.bind("<MouseWheel>", on_wheel)
        def in_size_entry(e):
            # 出力サイズの入力欄でのキー操作は、襟・枠の移動や取り消しにしない
            return str(getattr(e, "widget", "")) == str(size_entry)

        for key, (ddx, ddy) in (("Left", (-1, 0)), ("Right", (1, 0)),
                                ("Up", (0, -1)), ("Down", (0, 1))):
            win.bind(f"<{key}>", lambda e, a=ddx, b=ddy: None if in_size_entry(e) else (nudge(a, b), "break")[1])
            win.bind(f"<Shift-{key}>",
                     lambda e, a=ddx, b=ddy: None if in_size_entry(e) else (nudge(a, b, True), "break")[1])
        win.bind("<Return>", lambda e: canvas.focus_set() if in_size_entry(e) else on_enter())
        win.bind("<Escape>", lambda e: None if in_size_entry(e) else cancel_collar())
        win.bind("<Control-z>", lambda e: None if in_size_entry(e) else undo())
        win.bind("<Control-Z>", lambda e: None if in_size_entry(e) else undo())

        btns = ttk.Frame(win)
        btns.pack(pady=(4, 12))

        def answer(r):
            out = chosen_size()
            if r == "save" and out is None:
                # 出力サイズが決まるまで保存しない（ボタンも無効。キー操作や自動操作でも受け付けない）。
                # 入力の確認はタイマーの取り消しより先に行う。断ったときは放置の検出と描画の予約を
                # そのまま残すので、このまま放置すれば通常どおり "timeout" で閉じる（保存しない）。
                on_size_change()
                return
            if pending["id"] is not None:
                win.after_cancel(pending["id"])
                pending["id"] = None
            if idle["id"] is not None:
                try:
                    win.after_cancel(idle["id"])
                except Exception:  # noqa: BLE001
                    pass
                idle["id"] = None
            holder["r"] = r
            if r in ("save", "skip") and out is not None:
                # 選んだ出力サイズはスキップしたときも次の画像へ引き継ぐ（保存はしない）
                holder["size"] = out
            if r == "save":
                # 未確定の選択も、動かしてあれば確定して保存する（Photopeaの選択解除と同じ）。
                # ズームと表示位置は保存に関係しないので含めない。
                holder["edit"] = {"moves": all_moves(include_float=True),
                                  "rect": tuple(int(v) for v in st["frame"]),
                                  "angle": st["angle"],
                                  "size": out}
            win.destroy()
            ev.set()

        save_btn = ttk.Button(btns, text=t("保存", "Save", 'Kaydet'), style="Accent.TButton",
                              command=lambda: answer("save"))
        save_btn.pack(side="left", padx=4)
        ttk.Button(btns, text=t("スキップ", "Skip", 'Atla'),
                   command=lambda: answer("skip")).pack(side="left", padx=4)
        # 「全部保存」は無い。どの画像も、その画像のプレビューで「保存」を押したときだけ保存する
        save_btns.append(save_btn)
        update_save_btns()
        ttk.Button(btns, text=t("キャンセル", "Cancel", 'İptal'),
                   command=lambda: answer("cancel")).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", lambda: answer("cancel"))

        # 放置の検出: 操作（マウス・キー・ホイール）のたびに時刻を更新し、一定時間操作が無ければ
        # 保存せずに閉じる（"timeout"）。以前はワーカー側が10分で「保存」として先へ進んでいた。
        idle = {"id": None}

        def touch(_e=None):
            holder["active"] = time.monotonic()

        def check_idle():
            idle["id"] = None
            if not win.winfo_exists():
                return
            if time.monotonic() - holder.get("active", 0.0) >= PREVIEW_IDLE_TIMEOUT_SEC:
                answer("timeout")
                return
            idle["id"] = win.after(1000, check_idle)

        # 矢印キーなどは個別のバインドが優先されるので、KeyRelease / ButtonRelease でも拾う
        for seq in ("<Motion>", "<ButtonPress>", "<ButtonRelease>", "<KeyPress>", "<KeyRelease>", "<MouseWheel>"):
            win.bind(seq, touch, add="+")
        idle["id"] = win.after(1000, check_idle)
        # 自動検証用に状態と操作を参照できるようにする（画面の動作には影響しない）
        win._fm_preview = {"state": st, "view": view, "nudge": nudge, "commit": commit_float,
                           "cancel": cancel_collar, "undo": undo, "answer": answer,
                           "arm_select": arm_select, "zoom_at": zoom_at, "pan_by": pan_by,
                           "fit_view": fit_view, "set_angle": set_angle, "angle_scale": angle_scale,
                           "size_choice": size_choice, "size_custom": size_custom, "size_entry": size_entry,
                           "chosen_size": chosen_size, "set_size": set_size,
                           "save_btn": save_btn, "idle": idle, "pending": pending,
                           "to_canvas": to_canvas, "to_image": to_image,
                           "canvas": canvas, "cache": cache, "rotate_center": rotate_center,
                           "base_scale": base_scale, "cur_scale": cur_scale, "step_px": step_px,
                           "select_btn": select_btn, "banner": banner,
                           "lasso_btn": lasso_btn, "arm_lasso": arm_lasso, "lasso_start": lasso_start,
                           "lasso_add": lasso_add, "lasso_finish": lasso_finish, "on_enter": on_enter,
                           "float_outline_src": float_outline_src, "in_float": in_float, "float_mask": float_mask,
                           "commit_btn": commit_btn, "undo_btn": undo_btn, "all_moves": all_moves,
                           "collar_enabled": collar_enabled, "collar_note": collar_note,
                           "stretch_full": stretch_full, "stretch_view_active": stretch_view_active,
                           "status_label": status_label, "reset_frame": reset_frame}
        rebuild_edited()
        rebuild_comp()
        redraw()
        # 大きさを測り、画面からはみ出さない位置に置く
        win.update_idletasks()
        req_w, req_h = win.winfo_reqwidth(), win.winfo_reqheight()
        pos_x = max(0, (screen_w - req_w) // 2)
        pos_y = max(0, min((screen_h - req_h) // 2, screen_h - req_h - 60))
        win.geometry(f"+{pos_x}+{pos_y}")
        canvas.focus_set()
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
                    request, ev, holder = payload
                    try:
                        self._show_preview(request, ev, holder)
                    finally:
                        ev.set()
                elif kind == "testrec":
                    self._pending_test = payload
                elif kind == "notice":
                    self._run_notice = payload
                elif kind == "finishing":
                    # 終了処理に入った（キャンセルの受付は確定済み）。以後キャンセルボタンは押せない
                    try:
                        self.cancel_btn.config(state="disabled")
                    except Exception:  # noqa: BLE001
                        pass
                elif kind == "done":
                    cancelled = False
                    if isinstance(payload, tuple):
                        payload, cancelled = payload
                    elapsed = time.monotonic() - getattr(self, "_run_start", time.monotonic())
                    rec = getattr(self, "_pending_test", None)
                    notice = getattr(self, "_run_notice", None)
                    self._run_notice = None
                    if rec is not None:
                        rec["elapsed"] = elapsed
                        if payload:
                            rec["status"] = "error"
                        elif rec.get("preview_abort") or notice:
                            # 表示失敗・無操作・応答なしでの中止は、利用者のキャンセルとは別の状態にする
                            rec["status"] = "stopped"
                            rec["preview_abort"] = rec.get("preview_abort") or "unconfirmed"
                        elif cancelled:
                            rec["status"] = "cancelled"
                        elif rec.get("not_started"):
                            rec["status"] = "not_started"
                        elif zero_saved_status(rec):
                            # 保存0：all_skipped（スキップだけ）／all_failed（失敗だけ）／failed_skipped（混在）
                            rec["status"] = zero_saved_status(rec)
                        else:
                            rec["status"] = "done"
                        prev = self._test_results.get(rec["kind"])
                        if rec["status"] == "not_started" and prev is not None:
                            # 開始できなかった実行で、直前に開始できた実行の結果を失わないようにする（共有本文に併記）
                            rec["previous"] = prev if prev.get("status") != "not_started" else prev.get("previous")
                        self._test_results[rec["kind"]] = rec
                        self._pending_test = None
                    self._running = False
                    self._apply_busy_state()
                    self.status_lbl.config(text="")
                    self._current_file = ""
                    self._save_settings()
                    self._run_result(rec, payload, cancelled, notice, elapsed)
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
                text = t(f"処理中… {elapsed:.0f}秒経過", f"Processing… {elapsed:.0f}s", f'İşleniyor… {elapsed:.0f} sn')
                with _rembg_batch_lock:
                    fb_now = _rembg_batch.get("fp16_fallback")
                    unavail_now = _rembg_batch.get("cpu_reason") == "dml_unavailable"
                if unavail_now:
                    text += t("  |  背景透過: CPU・FP32（GPUを利用できないため）", "  |  Background removal: CPU (FP32; GPU unavailable)",
                              "  |  Arka plan kaldırma: CPU (FP32; GPU kullanılamıyor)")
                if fb_now:
                    text += t("  |  背景透過: CPU・FP32（FP16を使えないため）", "  |  Background removal: CPU (FP32; FP16 unavailable)",
                              "  |  Arka plan kaldırma: CPU (FP32; FP16 kullanılamıyor)")
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

    def _start_ui_wake(self):
        """実行中、メイン画面へ空のメッセージ（WM_NULL）を0.25秒ごとに送り、画面のタイマー（_poll）が止まったままに
        ならないようにする。Windows の Tk は、メッセージが1通も来ないと、予約したタイマーの時刻を過ぎても待ち続ける
        ことがある（処理は終わっているのに、完了のダイアログや次のプレビューが出ない）。実行が終わって2秒たったら止まる。
        Tk のオブジェクトには触れない（ウィンドウのハンドルは呼び出し時にメインスレッドで取る）。"""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winfo_id())
            post = ctypes.windll.user32.PostMessageW
            post.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        except Exception:  # noqa: BLE001
            return

        def loop():
            idle = 0
            while idle < 8 and not getattr(self, "_closing", False):
                time.sleep(0.25)
                idle = 0 if getattr(self, "_running", False) else idle + 1
                try:
                    post(hwnd, 0, 0, 0)          # WM_NULL: 何もしないメッセージ
                except Exception:  # noqa: BLE001
                    return

        threading.Thread(target=loop, daemon=True, name="fm-ui-wake").start()

    def _run(self):
        th = getattr(self, "_worker", None)
        if th is not None and th.is_alive():
            # ボタンの無効化だけを頼りにすると、言語切替で再構築された直後に
            # 同じ出力フォルダへ2本目のスレッドを起動できてしまう
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("すでに処理中です。", "A job is already running.", 'Bir işlem zaten çalışıyor.'))
            return
        if not self.in_var.get():
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("入力フォルダを選んでください。", "Please select the input folder.", 'Lütfen girdi klasörünü seçin.'))
            return
        # 画像は必ず1枚ずつプレビューで確認して保存する。出力サイズはプレビューで選ぶ（メイン画面の欄は無い）
        try:
            opts = {
                "input":         self.in_var.get(),
                "output":        self.out_var.get() or "processed",
                "size":          None,
                "scale":         float(self.scale_var.get()),
                "ocr_id":        self.ocr_var.get(),
                "face_crop":     self.facecrop_var.get(),
                # 顔トリミングがオフだと顔を検出しないため、自動水平補正は行われない（画面も無効表示）
                "auto_level":    self.auto_level_var.get() and self.facecrop_var.get(),
                "face_size":     max(FACE_SIZE_MIN,
                                     min(float(self.face_size_var.get()), FACE_SIZE_MAX)),
                "face_neck":     FACE_NECK,
                "upscale":       self.upscale_var.get(),
                # 拡大がオフだとAI高画質化も行われない（画面も無効表示）
                "ai_upscale":    self.ai_var.get() and self.upscale_var.get(),
                "bg_removal":    self.bg_var.get(),
                "make_config":   self.cfg_var.get(),
                "config_append": self.append_var.get(),
                "newgen":        self.newgen_var.get(),
                "overwrite":     self.ow_var.get(),
                "fit":           self.fit_var.get(),
                "rembg_model":   self.model_var.get(),
                "alpha_matting": self.matting_var.get(),
                "esrgan_model":  "RealESRGAN_x4plus",
                "removebg_key":  (self.removebg_var.get().strip()
                                  if self.use_removebg_var.get() and self.bg_var.get() else ""),
                "debug":         self.debug_var.get(),
            }
        except (tk.TclError, ValueError):
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("拡大倍率・顔サイズは数値で入力してください。",
                                     "Upscale factor and face size must be numbers.",
                                     'Büyütme katsayısı ve yüz boyutu sayı olmalıdır.'))
            return
        if not 0 < opts["scale"] <= 8:
            messagebox.showwarning(t("確認", "Notice", 'Uyarı'),
                                   t("拡大倍率は 0 より大きく 8 以下で指定してください。",
                                     "Upscale factor must be greater than 0 and no more than 8.", 'Büyütme katsayısı 0’dan büyük ve en fazla 8 olmalıdır.'))
            return

        self._run_notice = None
        # size: プレビューで選んだ出力サイズ。実行の最初は未選択（None）で、選んだ値だけをこの実行の
        # 次のプレビューに引き継ぐ（設定ファイルには保存しない）。どの画像もプレビューを開き、
        # その画像で「保存」が押されたときだけ保存する（「全部保存」は無い）
        state = {"size": None}

        def abort(kind, msg):
            # 確認できないまま保存しない。バッチを止め、終了時のダイアログで理由を出す。
            # 区分（kind）は診断結果・状態表示・処理ログの最後の行で同じ定型文にする（msg は画面とログの詳しい文）
            _mark_preview_abort(kind)
            self.q.put(("notice", msg))
            return "cancel", {"abort": msg}

        def preview_cb(request):
            request = dict(request, size=state["size"])
            ev = threading.Event()
            holder = {}
            self.q.put(("preview", (request, ev, holder)))
            # プレビュー側は操作のたびに holder["active"] を更新し、放置が PREVIEW_IDLE_TIMEOUT_SEC を
            # 超えると "timeout" で閉じる。それでも応答が無いとき（開けない・固まった）は、ここで打ち切る。
            start = time.monotonic()
            limit = PREVIEW_IDLE_TIMEOUT_SEC + PREVIEW_WAIT_GRACE_SEC
            while not ev.wait(timeout=0.5):
                if opts["_cancel"].is_set():
                    # 終了操作などでキャンセルされた。保存しない
                    holder["stale"] = True
                    return "cancel", None
                if time.monotonic() - holder.get("active", start) > limit:
                    holder["stale"] = True      # 後から開こうとしても表示しない
                    return abort("no_response", t(
                        "プレビューの応答がないため、この画像を保存せずに処理を止めました",
                        "The preview did not respond, so this image was not saved and processing was stopped",
                        "Önizleme yanıt vermediği için bu görüntü kaydedilmedi ve işlem durduruldu"))
            r = holder.get("r")
            chosen = parse_output_size(holder.get("size"))
            if chosen and r in ("save", "skip"):
                if chosen != state["size"]:
                    _note_preview_size(chosen)
                state["size"] = chosen
            if r == "timeout":
                minutes = max(1, int(round(PREVIEW_IDLE_TIMEOUT_SEC / 60.0)))
                return abort("idle_timeout", t(
                    f"プレビューが{minutes}分間操作されなかったため、この画像を保存せずに処理を止めました",
                    f"The preview was left idle for {minutes} min, so this image was not saved and processing was stopped",
                    f"Önizleme {minutes} dakika boyunca kullanılmadığı için bu görüntü kaydedilmedi ve işlem durduruldu"))
            if r == "error":
                err = holder.get("error", "")
                return abort("display_error", t(
                    f"プレビューを表示できなかったため、この画像を保存せずに処理を止めました: {err}",
                    f"The preview could not be shown, so this image was not saved and processing was stopped: {err}",
                    f"Önizleme gösterilemediği için bu görüntü kaydedilmedi ve işlem durduruldu: {err}"))
            if r not in ("save", "skip", "cancel"):
                return abort("unconfirmed", t(
                    "プレビューで保存の確認ができなかったため、この画像を保存せずに処理を止めました",
                    "Saving was not confirmed in the preview, so this image was not saved and processing was stopped",
                    "Kaydetme önizlemede onaylanmadığı için bu görüntü kaydedilmedi ve işlem durduruldu"))
            edit = holder.get("edit")
            if r == "save" and not chosen:
                # 出力サイズが決まらないまま保存しない（プレビュー側でも押せない）
                return abort("unconfirmed", t(
                    "プレビューで出力サイズが選ばれなかったため、この画像を保存せずに処理を止めました",
                    "No output size was chosen in the preview, so this image was not saved and processing was stopped",
                    "Önizlemede çıktı boyutu seçilmediği için bu görüntü kaydedilmedi ve işlem durduruldu"))
            return r, edit

        opts["_preview"] = preview_cb

        low_power = self.lowpower_var.get()
        use_removebg = bool(self.use_removebg_var.get())
        # 画像は必ず1枚ずつプレビューで確認するので、処理も常に1枚ずつ（旧版の並列処理数の設定は使わない）
        opts["workers"] = 1
        rembg_device = self.rembg_device_var.get()
        # FP16は画面に表示されている（GPU ＋ 対応モデル）ときだけ有効。非表示のときのチェック状態は使わない
        dml_fp16 = bool(self.dml_fp16_var.get() and rembg_device == REMBG_DEVICE_DML
                        and opts.get("rembg_model") in DML_FP16_MODELS)
        opts["_device_selected"] = rembg_device
        opts["_fp16_selected"] = dml_fp16
        # 次の画像の先読み: ローカルCPU処理のときだけ。GPUを選んだとき・remove.bg を有効にしたときは従来の流れ
        if rembg_device != REMBG_DEVICE_DML and not (use_removebg and opts.get("bg_removal")):
            opts["_prefetch"] = _Prefetcher(opts)
        self._cancel_event = CancelGate()
        opts["_cancel"] = self._cancel_event
        opts["_finishing"] = lambda cancelled: self.q.put(("finishing", cancelled))
        opts["_status"] = lambda name: self.q.put(("status", name))
        _log_lines = [] if self.save_log_var.get() else None
        _log_lock = threading.Lock()
        opts["_log_lines"] = _log_lines
        self.last_out = str(Path(opts["output"]).resolve())
        self._run_paths = (str(opts["input"]), str(opts["output"]))   # 開始できなかったときの案内用（画面だけ）
        self._run_cfg = bool(opts.get("make_config"))
        self._status_fn = None
        self._pending_test = None
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

        test_kind = "gpu" if rembg_device == REMBG_DEVICE_DML else "cpu"

        def worker():
            err = ""
            _reset_batch_stats()
            # 入力先なし・入出力が同じ・対応画像なしは、GPUの準備やGPUメモリの計測より前に確かめる
            not_started = folder_precheck(opts["input"], opts["output"])
            sampler = _GpuPeakSampler().start() if (test_kind == "gpu" and not not_started) else None
            if low_power:
                set_low_power(True)
                set_process_priority(True)
                _log_fn(t("[i] 低負荷モードで実行します（処理は遅めです）",
                           "[i] Running in low-load mode (slower)", '[i] Düşük yük modunda çalışıyor (daha yavaş)'))
            try:
                if not_started:
                    _mark_not_started(not_started)
                    begin_rembg_batch(REMBG_DEVICE_CPU, opts["rembg_model"])   # 前回の状態を消すだけ
                    where = f"（{opts['input']}）" if not_started != "same_dir" else ""
                    _log_fn(t(f"[!] 処理を開始できませんでした: {not_started_reason(not_started)}{where}",
                              f"[!] Processing could not start: {not_started_reason(not_started)}{where}",
                              f"[!] İşlem başlatılamadı: {not_started_reason(not_started)}{where}"))
                elif opts.get("bg_removal"):
                    if use_removebg and not opts.get("removebg_key"):
                        _log_fn(t("[i] remove.bg: APIキーが未入力のため使いません（ローカル背景透過で処理します）",
                                  "[i] remove.bg: not used because no API key is entered (local background removal is used)",
                                  "[i] remove.bg: API anahtarı girilmediği için kullanılmıyor (yerel arka plan kaldırma kullanılıyor)"))
                    begin_rembg_batch(rembg_device, opts["rembg_model"], _log_fn, fp16=dml_fp16,
                                      removebg=bool(opts.get("removebg_key")))
                else:
                    begin_rembg_batch(REMBG_DEVICE_CPU, opts["rembg_model"])
                # 実際に同時に処理する数（常に1枚ずつ）。診断結果にはこちらを出す
                opts["_workers_actual"] = 1
                if opts.get("_prefetch") is not None and not not_started:
                    _log_fn(t("[i] 次の画像の先読み: 有効（プレビューを開いている間に、次の1枚の準備を進めます）",
                              "[i] Next-image prefetch: on (the next image is prepared while a preview is open)",
                              "[i] Sonraki görüntüyü önceden hazırlama: açık (önizleme açıkken sonraki görüntü hazırlanır)"))
                if not not_started:
                    process_folder(opts, _log_fn,
                                   progress=lambda d, tot: self.q.put(("progress", (d, tot))))
            except Exception as e:  # noqa: BLE001
                err = e
            finally:
                try:
                    # 例外で止まったときも含め、先読みを捨ててスレッドの終了を待つ（「完了」を知らせる前）
                    if opts.get("_prefetch") is not None:
                        opts["_prefetch"].shutdown(_log_fn)
                except Exception:  # noqa: BLE001
                    pass
                if low_power:
                    set_process_priority(False)
                    set_low_power(False)
                # 境界で確定した結果（終了処理に入ったあとの要求は含まない）。開始できなかった・例外で止まったときはここで締め切る
                cancelled = close_cancel(self._cancel_event)
                try:
                    if sampler is not None:
                        sampler.stop()
                    self.q.put(("testrec", collect_test_record(test_kind, opts, sampler)))
                except Exception:  # noqa: BLE001
                    pass
                try:
                    # バッチが終わったら（正常終了・キャンセル・例外のどれでも）GPUのセッションを手放す
                    if release_dml_sessions():
                        _log_fn(t("[i] GPU（DirectML）のセッションを解放しました（GPUメモリを返却）",
                                  "[i] Released the GPU (DirectML) session (GPU memory returned)",
                                  "[i] GPU (DirectML) oturumu serbest bırakıldı (GPU belleği iade edildi)"))
                except Exception:  # noqa: BLE001
                    pass
                self.q.put(("done", (err, cancelled)))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()
        self._start_ui_wake()

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
