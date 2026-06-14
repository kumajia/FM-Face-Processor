"""
FM Face Processor - クロップ結果プレビュースクリプト
pair_005 / pair_006 の入力画像でYuNet検出値とクロップ結果を確認する
引数なし: pair_005, pair_006 をテスト
引数あり: python test_crop_preview.py 005 006 007 ...
"""
import sys, os, cv2, numpy as np
from pathlib import Path
from PIL import Image, ImageDraw

BASE = Path(__file__).parent
MODEL = str(BASE / "face_detection_yunet_2023mar.onnx")

FACE_PROC_DIR = Path.home() / "Pictures" / "face processor"
pairs = sys.argv[1:] if len(sys.argv) > 1 else ["005", "006"]

OUT = BASE / "crop_preview_out"
OUT.mkdir(exist_ok=True)

ZOOM_SIZE = [2.50, 2.20, 2.00, 1.85, 1.65]
LABELS    = ["ゆったり", "標準", "アップ", "FM標準", "超アップ"]

def detect_yunet(img_pil):
    img_cv = cv2.cvtColor(np.array(img_pil.convert("RGB")), cv2.COLOR_RGB2BGR)
    h, w = img_cv.shape[:2]
    net = cv2.FaceDetectorYN.create(MODEL, "", (w, h), 0.5, 0.3, 5000)
    net.setInputSize((w, h))
    _, faces = net.detect(img_cv)
    if faces is None or len(faces) == 0:
        return None, None, None, None

    upper = [f for f in faces if (float(f[1]) + float(f[3]) / 2) < h * 0.55]
    pool = upper if upper else list(faces)
    best = max(pool, key=lambda f: float(f[2]) * float(f[3]) * float(f[4]))

    score = float(best[4])
    box = (int(round(float(best[0]))), int(round(float(best[1]))),
           int(round(float(best[2]))), int(round(float(best[3]))))
    eye_mid_y = None
    eye_mid_x = None
    if len(best) >= 9:
        try:
            eye_mid_y = (float(best[6]) + float(best[8])) / 2.0
            eye_mid_x = (float(best[5]) + float(best[7])) / 2.0
        except Exception:
            pass

    print(f"  検出数(upper)={len(pool)}, 選択スコア={score:.3f}")
    for i, f in enumerate(pool):
        bx,by,bw,bh = int(f[0]),int(f[1]),int(f[2]),int(f[3])
        print(f"    [{i}] box=({bx},{by},{bw},{bh}) score={float(f[4]):.3f} area={bw*bh}")

    return box, eye_mid_y, eye_mid_x, score

def crop_around_face(img, box, size_factor, eye_mid_y=None, eye_mid_x=None):
    img = img.convert("RGBA")
    x, y, w, h = box
    cx = x + w / 2.0
    # eye_mid_x が有効なら水平中心を補正（本番と同じロジック）
    if (eye_mid_x is not None
            and x + w * 0.15 <= eye_mid_x <= x + w * 0.85
            and eye_mid_y is not None
            and y + h * 0.20 <= eye_mid_y <= y + h * 0.46):
        cx = eye_mid_x
    canvas_h = size_factor * h

    valid_eye = (eye_mid_y is not None
                 and y + h * 0.20 <= eye_mid_y <= y + h * 0.46)
    if valid_eye:
        top = eye_mid_y - canvas_h * 0.40
    else:
        top = y - canvas_h * 0.25

    hair_guard = canvas_h * 0.22
    if y - top < hair_guard:
        top = y - hair_guard

    bottom = top + canvas_h
    top_i = int(round(top))
    side = max(1, int(round(bottom - top_i)))
    left = int(round(cx - side / 2.0))
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, (-left, -top_i))
    return canvas, top_i, left, side, valid_eye

def process_pair(pair_id):
    pair_dir = FACE_PROC_DIR / f"pair_{pair_id}"
    if not pair_dir.exists():
        print(f"[スキップ] フォルダなし: {pair_dir}")
        return

    imgs = sorted(pair_dir.glob("ChatGPT Image*.png"))
    if not imgs:
        imgs = sorted(pair_dir.glob("*.png")) + sorted(pair_dir.glob("*.jpg"))
    if not imgs:
        print(f"[スキップ] 画像なし: {pair_dir}")
        return

    # 本番と同じ: 面積最大の画像を顔として選択
    def _img_area(p):
        try:
            from PIL import Image as _I
            w, h = _I.open(p).size
            return w * h
        except Exception:
            return 0
    src = max(imgs, key=_img_area)
    print(f"\n=== pair_{pair_id}: {src.name} ===")
    img_pil = Image.open(src).convert("RGB")
    print(f"  画像サイズ: {img_pil.size}")

    box, eye_mid_y, eye_mid_x, score = detect_yunet(img_pil)
    if box is None:
        print("  [!] 顔未検出")
        return

    x, y, w, h = box
    img_w, img_h = img_pil.size
    cx_box = x + w / 2.0
    e_in_box = ((eye_mid_y - y) / h * 100) if eye_mid_y is not None else None
    eye_str = f"{eye_mid_y:.1f}" if eye_mid_y is not None else "None"
    ebox_str = f"{e_in_box:.1f}" if e_in_box is not None else "?"
    valid_str = str(20 <= e_in_box <= 46) if e_in_box is not None else "N/A"
    ex_str = f"{eye_mid_x:.1f}" if eye_mid_x is not None else "None"
    print(f"  box=({x},{y},{w},{h})  cx_box={cx_box:.1f}  eye_cx={ex_str}/{img_w}  h={h}")
    print(f"  eye_mid_y={eye_str}  eye_in_box={ebox_str}%  (valid=20-46%: {valid_str})")

    panels = []
    for sf, lbl in zip(ZOOM_SIZE, LABELS):
        cropped, top_i, left_i, side, used_eye = crop_around_face(img_pil, box, sf, eye_mid_y, eye_mid_x)
        canvas_h = sf * h
        eye_pct = ((eye_mid_y - top_i) / canvas_h * 100) if eye_mid_y else None
        chin_pct = ((y + h - top_i) / canvas_h * 100)
        face_top_pct = ((y - top_i) / canvas_h * 100)
        mode = "eye@40%" if used_eye else "boxTop@25%"
        eye_str = f"{eye_pct:.0f}%" if eye_pct else "N/A"
        print(f"  {lbl}(sf={sf}): 顔上={face_top_pct:.0f}% 目={eye_str} "
              f"顎下={(100-chin_pct):.0f}%  [{mode}]  side={side} left={left_i}")

        vis = cropped.convert("RGB")
        dv = ImageDraw.Draw(vis)
        sz = vis.size[0]
        if eye_mid_y:
            ey = int(eye_mid_y - top_i)
            if 0 <= ey < sz:
                dv.line([(0,ey),(sz-1,ey)], fill=(255,60,60), width=2)
        cy2 = int((y+h) - top_i)
        if 0 <= cy2 < sz:
            dv.line([(0,cy2),(sz-1,cy2)], fill=(255,230,0), width=2)
        for xx in range(0, sz, 8):
            t40 = int(sz * 0.40)
            dv.line([(xx,t40),(min(xx+4,sz-1),t40)], fill=(0,230,0), width=1)

        thumb = vis.resize((180, 180), Image.LANCZOS)
        panels.append((thumb, lbl, sf, eye_pct, chin_pct, mode))

    W = 190 * (len(panels) + 1)
    result = Image.new("RGB", (W, 280), (30, 30, 30))
    dr = ImageDraw.Draw(result)
    # 元画像は縦横比を維持してリサイズ（強制リサイズすると比較が狂う）
    orig_thumb = Image.new("RGB", (180, 180), (50, 50, 50))
    tmp = img_pil.copy()
    tmp.thumbnail((180, 180), Image.LANCZOS)
    ox = (180 - tmp.size[0]) // 2
    oy = (180 - tmp.size[1]) // 2
    orig_thumb.paste(tmp, (ox, oy))
    result.paste(orig_thumb, (5, 5))
    dr.text((5, 190), f"元画像 {img_pil.size[0]}x{img_pil.size[1]}", fill=(200,200,200))
    dr.text((5, 204), src.name[:24], fill=(150,150,150))
    dr.text((5, 218), f"box=({x},{y},{w},{h})", fill=(180,180,100))
    dr.text((5, 232), f"eye_in_box={e_in_box:.0f}%" if e_in_box is not None else "eye=N/A", fill=(180,100,100))
    dr.text((5, 246), f"eye_cx={ex_str}", fill=(100,180,180))

    for i, (thumb, lbl, sf, epct, cpct, mode) in enumerate(panels):
        xo = 190 * (i+1) + 5
        result.paste(thumb, (xo, 5))
        dr.text((xo, 190), f"{lbl} sf={sf}", fill=(220,220,100))
        dr.text((xo, 204), f"目:{epct:.0f}% 顎下:{(100-cpct):.0f}%" if epct else f"顎下:{(100-cpct):.0f}%", fill=(220,220,100))
        dr.text((xo, 218), f"[{mode}]", fill=(150,200,150))

    out_path = OUT / f"pair_{pair_id}_preview.png"
    result.save(out_path)
    print(f"  → {out_path.name}")

def main():
    print(f"テスト: {FACE_PROC_DIR}")
    if not FACE_PROC_DIR.exists():
        print("[!] フォルダが見つかりません")
        sys.exit(1)
    for p in pairs:
        process_pair(p.zfill(3))
    print(f"\n完了! → {OUT}")
    if sys.platform == "win32":
        os.startfile(str(OUT))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n[エラー] {e}")
        traceback.print_exc()
    input("\nEnterキーで閉じる...")
