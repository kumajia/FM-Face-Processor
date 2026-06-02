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
  # AI高画質化を使う場合のみ: py -3.12 -m pip install realesrgan basicsr
  # 見た目をWin11風に: py -3.12 -m pip install sv-ttk （任意）
"""

import io
import os
import re
import json
import queue
import sys
import threading
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
_upsampler = None
_upsampler_tried = False


def _get_realesrgan(model_name, log):
    global _upsampler, _upsampler_tried
    if _upsampler_tried:
        return _upsampler
    _upsampler_tried = True
    try:
        from realesrgan import RealESRGANer
        from basicsr.archs.rrdbnet_arch import RRDBNet
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] Real-ESRGAN 不使用 -> Lanczos にフォールバック ({e})", f"  [i] Real-ESRGAN unavailable -> falling back to Lanczos ({e})"))
        _upsampler = None
        return None
    weights = Path("weights"); weights.mkdir(exist_ok=True)
    model_path = weights / f"{model_name}.pth"
    url = ("https://github.com/xinntao/Real-ESRGAN/releases/download/"
           "v0.1.0/RealESRGAN_x4plus.pth")
    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                    num_block=23, num_grow_ch=32, scale=4)
    if not model_path.exists():
        try:
            from basicsr.utils.download_util import load_file_from_url
            log(t("  [i] Real-ESRGAN の重みをダウンロード中（初回のみ）…", "  [i] Downloading Real-ESRGAN weights (first time only)…"))
            load_file_from_url(url, model_dir=str(weights), file_name=model_path.name)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 重み取得に失敗 -> Lanczos ({e})", f"  [!] Failed to fetch weights -> Lanczos ({e})"))
            _upsampler = None
            return None
    try:
        _upsampler = RealESRGANer(scale=4, model_path=str(model_path),
                                  model=model, half=False)
    except Exception as e:  # noqa: BLE001
        log(t(f"  [!] Real-ESRGAN 初期化失敗 -> Lanczos ({e})", f"  [!] Real-ESRGAN init failed -> Lanczos ({e})"))
        _upsampler = None
    return _upsampler


def upscale(img, factor, model_name, use_ai, log):
    from PIL import Image
    if factor <= 1:
        return img
    if use_ai:
        up = _get_realesrgan(model_name, log)
        if up is not None:
            import numpy as np
            arr = np.array(img.convert("RGB"))[:, :, ::-1]
            try:
                out, _ = up.enhance(arr, outscale=factor)
                return Image.fromarray(out[:, :, ::-1])
            except Exception as e:  # noqa: BLE001
                log(t(f"  [!] アップスケール中にエラー -> Lanczos ({e})", f"  [!] Error during upscaling -> Lanczos ({e})"))
    w, h = img.size
    return img.resize((int(w * factor), int(h * factor)), Image.LANCZOS)


_rembg_sessions = {}


def remove_background(img, model_name, alpha_matting=False):
    from PIL import Image
    from rembg import remove, new_session
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
    out = remove(img.convert("RGBA"), session=sess, **kw)
    if not isinstance(out, Image.Image):
        out = Image.open(io.BytesIO(out))
    return out.convert("RGBA")


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


def _get_face_cascade():
    global _face_cascade
    if _face_cascade is None:
        import cv2
        path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_cascade = cv2.CascadeClassifier(path)
    return _face_cascade


def detect_face_box(img, log):
    """最大の顔の (x, y, w, h) を返す。検出できなければ None。"""
    try:
        import cv2
        import numpy as np
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] OpenCV未導入のため顔トリミングをスキップ ({e})", f"  [i] OpenCV not installed; skipping face crop ({e})"))
        return None
    try:
        cascade = _get_face_cascade()
        arr = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                         minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda b: b[2] * b[3])
        return int(x), int(y), int(w), int(h)
    except Exception as e:  # noqa: BLE001
        log(t(f"  [i] 顔検出に失敗（全体を使用）: {e}", f"  [i] Face detection failed (using whole image): {e}"))
        return None


def crop_around_face(img, box, neck, headroom):
    """頭頂〜首あたりを正方形に切り出す（肩は出さない）。
    neck: 顎より下にどれだけ伸ばすか（顔の高さ基準。小さいほど首だけ）
    headroom: 頭頂の上に取る余白（顔の高さ基準）。
    背景除去後ならアルファから頭頂を正確に取得し、なければ顔枠から推定する。"""
    from PIL import Image
    img = img.convert("RGBA")
    x, y, w, h = box
    cx = x + w / 2.0
    chin = y + h
    a = img.getchannel("A")
    lo, _ = a.getextrema()
    if lo < 255:                         # 透明部分あり＝背景除去済み → 頭頂を正確に
        abox = a.getbbox()
        head_top = abox[1] if abox else (y - 0.45 * h)
    else:                                # 背景ありのときは顔枠から推定
        head_top = y - 0.45 * h
    head_top = min(head_top, y - 0.05 * h)
    top = head_top - headroom * h
    bottom = chin + neck * h             # ← ここで下を「首」で止める
    side = max(1, int(round(bottom - top)))
    left = int(round(cx - side / 2.0))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, (-left, -int(round(top))))
    return canvas


def crop_to_subject(img, neck, headroom, log):
    """顔検出が空振りした時の保険。背景除去後のアルファから人物の輪郭を求め、
    首〜頭を概算して正方形で切り出す。"""
    from PIL import Image
    img = img.convert("RGBA")
    a = img.getchannel("A")
    lo, _ = a.getextrema()
    if lo >= 255:
        return None                      # 背景が抜けていないと推定できない
    bbox = a.getbbox()
    if not bbox:
        return None
    left_b, top_b, right_b, _ = bbox
    sw = right_b - left_b
    cx = left_b + sw / 2.0
    face_h = sw * 0.5 * 1.3              # 肩幅から顔の高さを概算
    head_top = top_b
    chin = head_top + face_h * 1.55
    top = head_top - headroom * face_h
    bottom = chin + neck * face_h
    side = max(1, int(round(bottom - top)))
    left = int(round(cx - side / 2.0))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, (-left, -int(round(top))))
    log(t("  [i] 顔は未検出。輪郭から首〜頭を推定して切り出しました", "  [i] No face detected; cropped from silhouette (head/neck estimate)"))
    return canvas


def process_one(src, out_path, opts, log):
    """高画質化 → 背景除去 → 顔トリミング → リサイズ して保存。"""
    from PIL import Image, ImageOps
    img = Image.open(src); img.load()
    img = ImageOps.exif_transpose(img)
    if opts["upscale"]:
        img = upscale(img, opts["scale"], opts["esrgan_model"], opts["ai_upscale"], log)
    # 顔検出は背景があるうちに行うほうが安定
    box = detect_face_box(img, log) if opts.get("face_crop") else None
    if opts["bg_removal"]:
        try:
            img = remove_background(img, opts["rembg_model"], opts.get("alpha_matting", False))
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 背景除去できませんでした（背景を残して続行）: {e}",
                  f"  [!] Background removal failed (keeping background): {e}"))
            log(t('      → コマンドで  py -3.12 -m pip install "rembg[cpu]"  を実行し、アプリを閉じて開き直してください',
                  '      -> run  py -3.12 -m pip install "rembg[cpu]"  then close and reopen the app'))
            img = img.convert("RGBA")
    else:
        img = img.convert("RGBA")
    if opts.get("face_crop"):
        if box:
            img = crop_around_face(img, box, opts.get("face_neck", 0.30),
                                   opts.get("face_headroom", 0.06))
            log(t("  [i] 顔を検出してトリミングしました", "  [i] Face detected and cropped"))
        else:
            c = crop_to_subject(img, opts.get("face_neck", 0.30),
                                opts.get("face_headroom", 0.06), log)
            if c is not None:
                img = c
    img = fit_to_size(img, opts["size"], opts["fit"])
    img.save(out_path, "PNG")


def write_config(uids, path, append=True, newgen=False):
    """FMフェイスパック形式の config.xml を書き出す。
    append=True なら既存の行を残したまま新しいIDを足し、同じIDは無視（重複除去）。
    newgen=True なら to= のパスを person/r-<ID> にする（生成選手用。画像名は変えない）。
    戻り値: (新規追加数, 合計行数)。"""
    path = Path(path)
    existing = []
    if append and path.exists():
        try:
            # 既存の from は通常ID（数字）と newgen（r-数字）の両方を拾う
            existing = re.findall(r'from="(r-\d+|\d+)"', path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = []
    prefix = "r-" if newgen else ""
    new_ids = [f"{prefix}{u}" for u in (str(x) for x in uids)]
    existing_set = set(existing)
    seen, merged = set(), []
    for u in [str(x) for x in existing] + new_ids:
        if u in seen:
            continue
        seen.add(u)
        merged.append(u)
    lines = ["<record>",
             '\t<boolean id="preload" value="false"/>',
             '\t<boolean id="amap" value="false"/>',
             '\t<list id="maps">']
    for uid in merged:
        to = f"graphics/pictures/person/{uid}/portrait"
        lines.append(f"\t\t<record from={quoteattr(uid)} to={quoteattr(to)}/>")
    lines += ["\t</list>", "</record>", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    new_only = len([u for u in new_ids if u not in existing_set])
    return new_only, len(merged)


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
        if reader is not None:
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
        if id_num is None:
            log(t(f"  [!] {sub.name}: IDを読み取れず、スキップ", f"  [!] {sub.name}: could not read ID, skipped")); continue
        if not faces:
            log(t(f"  [!] {sub.name}: 顔写真が見つからず、スキップ", f"  [!] {sub.name}: no face image found, skipped")); continue
        face = max(faces, key=_img_area)  # 一番大きい画像を顔とみなす
        log(t(f"  [{sub.name}] 顔: {face.name}  =>  {id_num}.png", f"  [{sub.name}] face: {face.name}  =>  {id_num}.png"))
        pairs.append((face, id_num, sub.name))

    if not pairs:
        log(t("[!] 有効なペアがありませんでした。", "[!] No valid pairs found.")); return

    uids, seen, total = [], set(), len(pairs)
    for idx, (face, uid, subname) in enumerate(pairs, 1):
        if progress:
            progress(idx - 1, total)
        if uid in seen:
            log(t(f"[skip] ID重複: {uid}（{subname}）", f"[skip] duplicate ID: {uid} ({subname})")); continue
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            seen.add(uid); uids.append(uid); continue
        log(f"[{idx}/{total}] {subname}/{face.name} -> {uid}.png")
        try:
            process_one(face, out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}")); continue
        seen.add(uid); uids.append(uid)
    if progress:
        progress(total, total)
    _finish(uids, out_dir, opts, log)


def _run_filename_mode(files, out_dir, opts, log, progress):
    uids, seen, total = [], set(), len(files)
    for idx, f in enumerate(files, 1):
        if progress:
            progress(idx - 1, total)
        uid = f.stem
        if uid in seen:
            log(t(f"[skip] UID重複: {uid}", f"[skip] duplicate UID: {uid}")); continue
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            seen.add(uid); uids.append(uid); continue
        log(f"[{idx}/{total}] {f.name} -> {uid}.png")
        try:
            process_one(f, out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}")); continue
        seen.add(uid); uids.append(uid)
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

    uids, seen, total = [], set(), len(pairs)
    for idx, (face, idd) in enumerate(pairs, 1):
        if progress:
            progress(idx - 1, total)
        uid = idd["number"]
        if uid in seen:
            log(t(f"[skip] ID重複: {uid}（{face['path'].name}）", f"[skip] duplicate ID: {uid} ({face['path'].name})")); continue
        out_path = out_dir / f"{uid}.png"
        if out_path.exists() and not opts["overwrite"]:
            log(t(f"[{idx}/{total}] [skip] 既存: {uid}.png", f"[{idx}/{total}] [skip] exists: {uid}.png"))
            seen.add(uid); uids.append(uid); continue
        log(f"[{idx}/{total}] {face['path'].name} -> {uid}.png")
        try:
            process_one(face["path"], out_path, opts, log)
        except Exception as e:  # noqa: BLE001
            log(t(f"  [!] 失敗: {e}", f"  [!] failed: {e}")); continue
        seen.add(uid); uids.append(uid)
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
ZOOM_NECK = [0.45, 0.30, 0.18, 0.08]       # index 0..3
ZOOM_HEAD = [0.10, 0.06, 0.03, 0.00]
ZOOM_LABELS = {
    "ja": ["ゆったり", "標準", "アップ", "超アップ"],
    "en": ["Loose", "Normal", "Close", "Very close"],
}


class App(tk.Tk):
    SETTINGS_PATH = Path.home() / ".fm_face_processor.json"

    def __init__(self):
        super().__init__()
        self.title("FM Face Processor")
        self.geometry("720x700")
        self.minsize(640, 600)
        self.q = queue.Queue()
        self.mode = "dark"
        self.lang = "ja"
        self.zoom_idx = 3            # 既定: 超アップ
        self.last_out = None
        self.log_lines = []          # ログ本文を保持（言語切替時に再描画はしないが保持用）
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
        self.model_var = tk.StringVar(value="birefnet-general")
        self.matting_var = tk.BooleanVar(value=False)
        self.ocr_var = tk.BooleanVar(value=True)
        self.facecrop_var = tk.BooleanVar(value=True)
        self.upscale_var = tk.BooleanVar(value=True)
        self.ai_var = tk.BooleanVar(value=False)
        self.bg_var = tk.BooleanVar(value=True)
        self.cfg_var = tk.BooleanVar(value=True)
        self.append_var = tk.BooleanVar(value=True)
        self.newgen_var = tk.BooleanVar(value=False)
        self.ow_var = tk.BooleanVar(value=False)

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
            "overwrite": self.ow_var.get(),
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
        self.zoom_idx = int(d.get("zoom_idx", self.zoom_idx))
        for key, var in (
            ("input", self.in_var), ("output", self.out_var), ("fit", self.fit_var),
            ("model", self.model_var), ("size", self.size_var), ("scale", self.scale_var),
            ("matting", self.matting_var), ("ocr", self.ocr_var),
            ("facecrop", self.facecrop_var), ("upscale", self.upscale_var), ("ai", self.ai_var),
            ("bg", self.bg_var), ("cfg", self.cfg_var), ("append", self.append_var),
            ("newgen", self.newgen_var), ("overwrite", self.ow_var),
        ):
            if key in d:
                try:
                    var.set(d[key])
                except Exception:  # noqa: BLE001
                    pass

    def _save_settings(self):
        try:
            self.SETTINGS_PATH.write_text(
                json.dumps(self._collect_settings(), ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ---------- 画面の組み立て（再ビルド可能）----------
    def _build(self):
        if self.outer is not None:
            self.outer.destroy()
        outer = ttk.Frame(self, padding=16); outer.pack(fill="both", expand=True)
        self.outer = outer

        header = ttk.Frame(outer); header.pack(fill="x", pady=(0, 12))
        titles = ttk.Frame(header); titles.pack(side="left")
        ttk.Label(titles, text="FM Face Processor", style="Header.TLabel").pack(anchor="w")
        ttk.Label(titles, text=t("顔画像＋IDスクショ → 透過PNG ＋ config.xml",
                                  "Face + ID screenshot -> transparent PNG + config.xml"),
                  style="Sub.TLabel").pack(anchor="w")
        btns = ttk.Frame(header); btns.pack(side="right")
        self.lang_btn = ttk.Button(btns, text=("EN" if self.lang == "ja" else "日本語"),
                                   width=7, command=self._toggle_lang)
        self.lang_btn.pack(side="right", padx=(6, 0))
        self.theme_btn = ttk.Button(btns, width=10, command=self._toggle_theme)
        self.theme_btn.pack(side="right")

        fld = ttk.LabelFrame(outer, text="  " + t("フォルダ", "Folders") + "  ", padding=12)
        fld.pack(fill="x", pady=(0, 10)); fld.columnconfigure(1, weight=1)
        ttk.Label(fld, text=t("入力", "Input")).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(fld, textvariable=self.in_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(fld, text=t("選択", "Browse"), command=self._pick_in).grid(row=0, column=2, padx=(8, 0))
        ttk.Label(fld, text=t("出力", "Output")).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(fld, textvariable=self.out_var).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(fld, text=t("選択", "Browse"), command=self._pick_out).grid(row=1, column=2, padx=(8, 0))
        ttk.Button(fld, text=t("ペア用フォルダを作成", "Create pair folders"),
                   command=self._make_pair_folders).grid(row=2, column=1, sticky="w", pady=(6, 0))

        opt = ttk.LabelFrame(outer, text="  " + t("オプション", "Options") + "  ", padding=12)
        opt.pack(fill="x", pady=(0, 10))
        row = ttk.Frame(opt); row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text=t("出力サイズ(px)", "Size (px)")).pack(side="left")
        ttk.Entry(row, textvariable=self.size_var, width=7).pack(side="left", padx=(6, 16))
        ttk.Label(row, text=t("拡大倍率", "Upscale x")).pack(side="left")
        ttk.Entry(row, textvariable=self.scale_var, width=7).pack(side="left", padx=(6, 16))
        ttk.Label(row, text=t("リサイズ", "Fit")).pack(side="left")
        ttk.Combobox(row, textvariable=self.fit_var, width=10, state="readonly",
                     values=["contain", "cover", "stretch"]).pack(side="left", padx=6)
        ttk.Label(row, text=t("顔の大きさ", "Face size")).pack(side="left", padx=(16, 0))
        self.zoom_cb = ttk.Combobox(row, width=10, state="readonly",
                                    values=ZOOM_LABELS[self.lang])
        self.zoom_cb.current(self.zoom_idx)
        self.zoom_cb.bind("<<ComboboxSelected>>", self._on_zoom)
        self.zoom_cb.pack(side="left", padx=6)

        row2 = ttk.Frame(opt); row2.pack(fill="x", pady=(0, 8))
        ttk.Label(row2, text=t("背景モデル", "BG model")).pack(side="left")
        ttk.Combobox(row2, textvariable=self.model_var, width=20, state="readonly",
                     values=["birefnet-general", "birefnet-general-lite", "birefnet-portrait",
                             "u2net_human_seg", "isnet-general-use", "u2net"]).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(row2, text=t("髪のフチをなめらかにする", "Smooth hair edges (alpha matting)"),
                        variable=self.matting_var).pack(side="left")

        chk = ttk.Frame(opt); chk.pack(fill="x")
        ttk.Checkbutton(chk, text=t("IDを自動で読み取る", "Auto-read ID"),
                        variable=self.ocr_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("顔を中心に切り抜く", "Crop around face"),
                        variable=self.facecrop_var).grid(row=0, column=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("画像を拡大する", "Upscale"),
                        variable=self.upscale_var).grid(row=1, column=0, sticky="w", padx=(0, 16), pady=2)
        ttk.Checkbutton(chk, text=t("AI高画質化(Real-ESRGAN)", "AI upscale (Real-ESRGAN)"),
                        variable=self.ai_var).grid(row=1, column=1, sticky="w", padx=(0, 16), pady=2)
        ttk.Checkbutton(chk, text=t("背景を消して透過にする", "Remove background (transparent)"),
                        variable=self.bg_var).grid(row=1, column=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("config.xmlを作る", "Generate config.xml"),
                        variable=self.cfg_var).grid(row=2, column=0, sticky="w", padx=(0, 16), pady=2)
        ttk.Checkbutton(chk, text=t("config.xmlに書き足す（同じIDは無視）", "Append to config.xml (skip dups)"),
                        variable=self.append_var).grid(row=2, column=1, sticky="w", padx=(0, 16), pady=2)
        ttk.Checkbutton(chk, text=t("作成済みでも作り直す", "Overwrite existing"),
                        variable=self.ow_var).grid(row=2, column=2, sticky="w", pady=2)
        ttk.Checkbutton(chk, text=t("生成選手（newgen）に対応する", "Add r- prefix for newgen players"),
                        variable=self.newgen_var).grid(row=3, column=0, columnspan=3, sticky="w", pady=2)

        act = ttk.Frame(outer); act.pack(fill="x", pady=(0, 8))
        self.run_btn = ttk.Button(act, text=t("実行", "Run"), style="Accent.TButton", command=self._run)
        self.run_btn.pack(side="left")
        self.open_btn = ttk.Button(act, text=t("出力フォルダを開く", "Open output folder"),
                                   command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=8)

        self.pb = ttk.Progressbar(outer, mode="determinate"); self.pb.pack(fill="x", pady=(0, 10))

        logfrm = ttk.Frame(outer); logfrm.pack(fill="both", expand=True)
        self.log = tk.Text(logfrm, height=12, wrap="word", relief="flat",
                           borderwidth=0, font=("Consolas", 10))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logfrm, command=self.log.yview); sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)
        # 既存ログを復元、無ければ案内文
        if self.log_lines:
            self.log.insert("end", "\n".join(self.log_lines) + "\n")
            self.log.see("end")
        else:
            self._log(t("顔写真とIDスクショを同じフォルダに入れて「実行」を押してください。",
                        "Put face photos and ID screenshots in one folder, then press Run."))
        self._refresh_theme_btn()

    # ---------- テーマ ----------
    def apply_theme(self, mode):
        self.mode = mode; pal = PALETTES[mode]
        if HAS_SVTTK:
            try:
                sv_ttk.set_theme(mode)
            except Exception:  # noqa: BLE001
                self._apply_clam(pal)
        else:
            self._apply_clam(pal)
        self.configure(bg=pal["bg"])
        self.log.configure(bg=pal["logbg"], fg=pal["logfg"], insertbackground=pal["logfg"],
                           selectbackground=pal["accent"], highlightthickness=1,
                           highlightbackground=pal["border"])
        set_titlebar_dark(self, mode == "dark")
        self._refresh_theme_btn()

    def _refresh_theme_btn(self):
        if hasattr(self, "theme_btn"):
            if self.mode == "dark":
                self.theme_btn.configure(text="☀  " + t("ライト", "Light"))
            else:
                self.theme_btn.configure(text="🌙  " + t("ダーク", "Dark"))

    def _apply_clam(self, pal):
        s = self.style; s.theme_use("clam")
        s.configure(".", background=pal["bg"], foreground=pal["fg"],
                    fieldbackground=pal["panel"], font=("Segoe UI", 10))
        s.configure("TFrame", background=pal["bg"])
        s.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        s.configure("Header.TLabel", background=pal["bg"], foreground=pal["fg"],
                    font=("Segoe UI Semibold", 17))
        s.configure("Sub.TLabel", background=pal["bg"], foreground=pal["sub"], font=("Segoe UI", 9))
        s.configure("TLabelframe", background=pal["bg"], bordercolor=pal["border"],
                    relief="solid", borderwidth=1)
        s.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["accent"],
                    font=("Segoe UI Semibold", 10))
        s.configure("TButton", background=pal["panel"], foreground=pal["fg"],
                    bordercolor=pal["border"], focuscolor=pal["bg"], padding=(12, 6))
        s.map("TButton", background=[("active", pal["active"]), ("disabled", pal["panel"])])
        s.configure("Accent.TButton", background=pal["accent"], foreground="#ffffff",
                    bordercolor=pal["accent"], focuscolor=pal["accent"], padding=(20, 8))
        s.map("Accent.TButton",
              background=[("active", pal["accent_hi"]), ("disabled", pal["border"])],
              foreground=[("disabled", pal["sub"])])
        s.configure("TCheckbutton", background=pal["bg"], foreground=pal["fg"])
        s.map("TCheckbutton", background=[("active", pal["bg"])], foreground=[("active", pal["fg"])])
        s.configure("TEntry", fieldbackground=pal["panel"], foreground=pal["fg"],
                    insertcolor=pal["fg"], bordercolor=pal["border"], padding=5)
        s.configure("TCombobox", fieldbackground=pal["panel"], background=pal["panel"],
                    foreground=pal["fg"], arrowcolor=pal["fg"], bordercolor=pal["border"], padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", pal["panel"])],
              foreground=[("readonly", pal["fg"])])
        s.configure("Horizontal.TProgressbar", background=pal["accent"],
                    troughcolor=pal["trough"], bordercolor=pal["border"], thickness=8)
        s.configure("TScrollbar", background=pal["panel"], troughcolor=pal["bg"],
                    bordercolor=pal["border"], arrowcolor=pal["fg"])
        self.option_add("*TCombobox*Listbox.background", pal["panel"])
        self.option_add("*TCombobox*Listbox.foreground", pal["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", pal["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

    def _toggle_theme(self):
        self.apply_theme("light" if self.mode == "dark" else "dark")
        self._save_settings()

    def _toggle_lang(self):
        # 現在のログ本文を保持して言語切替→画面を作り直す
        self.log_lines = self.log.get("1.0", "end-1c").split("\n") if hasattr(self, "log") else []
        self.lang = "en" if self.lang == "ja" else "ja"
        set_lang(self.lang)
        self._build()
        self.apply_theme(self.mode)
        self._save_settings()

    def _on_zoom(self, _evt=None):
        self.zoom_idx = self.zoom_cb.current()

    # ---------- イベント ----------
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
                                   t("先に入力フォルダを選んでください。", "Please select the input folder first."))
            return
        n = simpledialog.askinteger(t("ペア用フォルダ作成", "Create pair folders"),
                                    t("いくつ作りますか？", "How many?"),
                                    minvalue=1, maxvalue=1000, parent=self)
        if not n:
            return
        p = Path(base); made = 0
        for i in range(1, n + 1):
            try:
                (p / f"pair_{i:03d}").mkdir(exist_ok=True); made += 1
            except Exception:  # noqa: BLE001
                pass
        messagebox.showinfo(
            t("作成完了", "Done"),
            t(f"{made} 個のフォルダを作成しました。\n各フォルダに「顔写真」と「IDスクショ」を1枚ずつ入れてから実行してください。",
              f"Created {made} folders.\nPut one face photo and one ID screenshot in each, then run."))
        self._log(t(f"ペア用フォルダを {made} 個作成しました: {base}",
                    f"Created {made} pair folders: {base}"))

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

    def _log(self, msg):
        self.log.insert("end", msg + "\n"); self.log.see("end")

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    done, total = payload
                    self.pb.configure(maximum=max(total, 1), value=done)
                elif kind == "done":
                    self.run_btn.config(state="normal", text=t("実行", "Run"))
                    self._save_settings()
                    if payload:
                        messagebox.showerror(t("エラー", "Error"), payload)
                    else:
                        self.open_btn.config(state="normal")
                        messagebox.showinfo(t("完了", "Done"),
                                            t("処理が終わりました。ログでペアの対応を確認してください。",
                                              "Finished. Check the log for the pairings."))
        except queue.Empty:
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
                "input": self.in_var.get(),
                "output": self.out_var.get() or "processed",
                "size": int(self.size_var.get()),
                "scale": float(self.scale_var.get()),
                "ocr_id": self.ocr_var.get(),
                "face_crop": self.facecrop_var.get(),
                "face_neck": ZOOM_NECK[idx],
                "face_headroom": ZOOM_HEAD[idx],
                "upscale": self.upscale_var.get(),
                "ai_upscale": self.ai_var.get(),
                "bg_removal": self.bg_var.get(),
                "make_config": self.cfg_var.get(),
                "config_append": self.append_var.get(),
                "newgen": self.newgen_var.get(),
                "overwrite": self.ow_var.get(),
                "fit": self.fit_var.get(),
                "rembg_model": self.model_var.get(),
                "alpha_matting": self.matting_var.get(),
                "esrgan_model": "RealESRGAN_x4plus",
            }
        except (tk.TclError, ValueError):
            messagebox.showwarning(t("確認", "Notice"),
                                   t("サイズ・倍率は数値で入力してください。",
                                     "Size and upscale must be numbers."))
            return

        self.last_out = str(Path(opts["output"]).resolve())
        self.run_btn.config(state="disabled", text=t("処理中…", "Working…"))
        self.open_btn.config(state="disabled")
        self.pb.configure(value=0)
        self.log.delete("1.0", "end")

        def worker():
            err = ""
            try:
                process_folder(opts, lambda m: self.q.put(("log", m)),
                               lambda d, tot: self.q.put(("progress", (d, tot))))
            except Exception as e:  # noqa: BLE001
                err = str(e)
                self.q.put(("log", f"[!] {e}"))
            self.q.put(("done", err))

        threading.Thread(target=worker, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
