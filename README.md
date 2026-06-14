# FM Face Processor

**顔写真 + IDスクショ → FMポートレート + config.xml を全自動生成**

![Python](https://img.shields.io/badge/Python-3.12-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

Football Manager 用の選手・スタッフ顔グラフィックを半自動で作るツールです。顔写真とFM内IDのスクショを放り込むだけで、高画質化・背景透過・顔トリミング・config.xml 生成まで全部やります。


---

## できること

| 機能 | 説明 |
|------|------|
| 🔍 顔検出 | YuNet（高精度）+ Haar カスケードでフォールバック |
| 🖼️ 高画質化 | Real-ESRGAN x4 で拡大 |
| ✂️ 背景透過 | rembg で透過 PNG 化（モデル選択・髪のフチ調整あり） |
| 🎯 顔トリミング | 目〜顎の距離で正規化、肩が写らない正方形クロップ |
| 🔢 ID 自動読取 | RapidOCR でスクショから ID を認識 → `<ID>.png` で保存 |
| 📄 config.xml 生成 | 実行のたびに追記、重複 ID はスキップ |
| 👶 newgen 対応 | ID に `r-` プレフィックスを付けるオプション |
| 🌐 UI | 日本語 / 英語、ダーク / ライト テーマ、設定の保存 |

---

## 必要なファイル

リリースページからダウンロードして、**すべて同じフォルダ**に置いてください。

```
FM Face Processor_v1.x.x/
├── FM Face Processor.py
├── face_detection_yunet_2023mar.onnx   ← 顔検出モデル（リリースに同梱）
├── Real-ESRGAN-x4plus.onnx             ← 高画質化モデル
└── real_esrgan_x4plus.data
```

> `Real-ESRGAN-x4plus.onnx` と `real_esrgan_x4plus.data` はサイズが大きいため含まれていません。別途入手してください。

---

## セットアップ（初回のみ）

### 1. Python 3.12 を入れる

Microsoft Store で **Python Install Manager** を検索して「入手」し、インストール後にコマンドプロンプトを開いて実行：

```
py install 3.12
```

途中の質問は以下の通りに答えてください：

- `Add commands directory to your PATH now?` → **y**
- `Install CPython now?` → **n**（最新版を避けるため）
- `View online help?` → **n**

確認：`py list` で `3.12` が表示されれば OK

### 2. ライブラリを入れる

```
py -3.12 -m pip install pillow "rembg[cpu]" rapidocr-onnxruntime opencv-python
```

### 3. 起動

`FM Face Processor.py` をダブルクリック

> 初回実行時は AI モデルの自動ダウンロードがあるため時間がかかります（2 回目以降は速い）

---

## 使い方

1. フォルダに「顔写真」と「ID スクショ」を入れる
   - ペアが複数あるときは **サブフォルダに分ける**と確実
2. アプリで入力フォルダを選んで「実行」
3. 出力フォルダに透過 PNG 一式と `config.xml` が生成される
4. それを FM のグラフィックフォルダに入れて、ゲーム内でスキンを再読み込み

---

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| 背景が抜けない | `py -3.12 -m pip install "rembg[cpu]"` を実行してアプリを再起動 |
| ID が読めない | 「ID を自動で読み取る」を外し、ファイル名を ID にする（例: `50053056.jpg`） |
| 顔が大きすぎ / 切れる | 「顔の大きさ」を一段戻す |

---

## English

**Face photo + ID screenshot → FM portrait + config.xml, fully automated**

A semi-automatic tool for Football Manager face graphics. Drop in a face photo and an ID screenshot — it upscales, removes the background, crops the face, and generates `config.xml` automatically.

### Features
- Face detection (YuNet + Haar fallback)
- 4× upscaling via Real-ESRGAN
- Background removal to transparent PNG (rembg, with hair-edge smoothing)
- Normalized square crop based on eye-to-chin distance (no shoulders)
- OCR-based ID reading → saves as `<ID>.png`
- Auto-generates `config.xml` (appends each run, deduplicates IDs)
- Newgen support (`r-` prefix option)
- Japanese / English UI, dark / light theme, persistent settings

### Setup (once)

1. Install **Python Install Manager** from the Microsoft Store, then:
   ```
   py install 3.12
   ```
2. Install libraries:
   ```
   py -3.12 -m pip install pillow "rembg[cpu]" rapidocr-onnxruntime opencv-python
   ```
3. Double-click `FM Face Processor.py` to launch.

### Files needed

Download from the Releases page and keep all files in the same folder:
- `FM Face Processor.py`
- `face_detection_yunet_2023mar.onnx`
- `Real-ESRGAN-x4plus.onnx` + `real_esrgan_x4plus.data`

### Troubleshooting

| Issue | Fix |
|-------|-----|
| Background not removed | Run `py -3.12 -m pip install "rembg[cpu]"` and reopen the app |
| ID not detected | Uncheck "Auto-read ID" and name the file as the ID (e.g. `50053056.jpg`) |
| Face too big / cut off | Lower the "Face size" setting by one step |

---

## 更新履歴 / Changelog

[Releases ページ](https://github.com/kumajia/FM-Face-Processor/releases)をご覧ください。
