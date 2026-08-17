# FM Face Processor v2.1.0

**顔写真 + IDスクショ → FMポートレート + config.xml を全自動生成**

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](https://github.com/kumajia/FM-Face-Processor/blob/main/LICENSE)
[![Release](https://img.shields.io/badge/Release-v2.1.0-2ea44f)](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.1.0)

Football Manager 用の選手・スタッフ顔グラフィックを半自動で作るツールです。顔写真とFM内IDのスクショを放り込むだけで、高画質化・背景透過・顔トリミング・`config.xml` 生成までまとめて行います。

v2.1.0では、投稿ガイドに合わせて**両目の自動水平補正**と**顎下を詰めたクロップ**を追加しました。保存前プレビューでは、拡大率・位置・角度を1枚ずつ微調整できます。

---

## できること

| 機能 | 説明 |
|------|------|
| 🔍 顔検出 | YuNet（高精度）+ Haar カスケードでフォールバック |
| 🖼️ 高画質化 | Real-ESRGAN x4 で拡大 |
| ✂️ 背景透過 | rembg で透過 PNG 化（モデル選択・髪のフチ調整あり） |
| 📐 水平補正 | YuNetの両目ランドマークで傾きを検出し、両目が水平になるよう自動補正 |
| 🎯 顔トリミング | 目〜顎の距離で正規化し、顎下を詰めて肩を抑えた正方形クロップ |
| 🔢 ID 自動読取 | RapidOCR でスクショから ID を認識 → `<ID>.png` で保存 |
| 📄 config.xml 生成 | 実行のたびに追記、重複 ID はスキップ |
| 👶 newgen 対応 | ID に `r-` プレフィックスを付けるオプション |
| 🌐 UI | 日本語 / 英語、ダーク / ライト テーマ、設定の保存 |
| 👁️ 保存前プレビュー | 1枚ずつ拡大率・左右・上下・角度を微調整して保存可能 |
| ☁️ remove.bg API | 任意で使用可能。失敗時はローカルAIへ自動で切り替え |
| 🛡️ データ保護 | 一時ファイル保存、`config.xml` の自動バックアップ、ごみ箱への移動 |

---

## ダウンロードと起動

[Releasesページ](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.1.0)から `FM.Face.Processor_v2.1.0_Windows.zip` をダウンロードして展開してください。

```
FM Face Processor_v2.1.0/
├── EXE/
│   └── FM Face Processor/
│       ├── FM Face Processor.exe
│       └── 必要な実行ファイル・AIモデル一式
├── README.md
└── FM_Face_Processor_仕様書_v2.1.0_Dark.docx
```

`EXE\FM Face Processor\FM Face Processor.exe` をダブルクリックすれば起動します。**Pythonのインストールは不要**です。

> `FM Face Processor.exe` だけを別の場所へ移動せず、`FM Face Processor` フォルダごと使用してください。

---

## 使い方

1. 入力フォルダに「顔写真」と「FM内IDが写ったスクリーンショット」を入れる
   - 複数人を処理する場合、画像の撮影時刻が近い順にOCRで組み合わせます
   - 1人分ずつサブフォルダに分けると、より確実です
2. アプリで入力フォルダと出力フォルダを選ぶ
   - 元画像保護のため、入力と出力には別のフォルダを指定してください
3. 必要なオプションを確認して「実行」を押す
4. 出力フォルダに透過PNG、`config.xml`、処理ログが生成される
5. 出力物をFMのグラフィックフォルダに入れ、ゲーム内でスキンを再読み込みする

### IDを読み取れない場合

「IDを自動で読み取る」をOFFにし、顔画像を `50053056.jpg` のように **IDをファイル名にして**処理できます。

OCRの組み合わせ結果は処理ログで確認してください。重要なフェイスパックでは「保存前にプレビュー」もおすすめします。

---

## remove.bgについて

remove.bgを有効にすると、処理対象の顔画像がremove.bgへ送信されます。利用規約とプライバシー要件を確認して使用してください。

- APIキーは**本人のPC内にある設定ファイルだけ**へ保存します
- 画面上では伏せ字で表示します
- 旧バージョンが保存したキーも勝手に削除せず再利用します
- 設定ファイルとAPIキーはGitHub、配布ZIP、EXEには含めません
- remove.bgが利用できない場合は、ローカルAIで処理を続行します

---

## データ保護

- PNGと `config.xml` は、一時ファイルへ完成させてから置換します
- 既存 `config.xml` の上書き・再生成前には、日時付き `.bak` を作成します
- 入力フォルダと出力フォルダが同じ場合は処理を開始しません
- 「入力元画像をゴミ箱へ」は確認画面を表示し、完全削除ではなくWindowsのごみ箱へ移動します
- 大切な素材は、このアプリとは別の場所にも保管してください

---

## ソース版を使う場合

ソース版のみPython 3.12（64bit）が必要です。

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 "FM Face Processor.py"
```

初回のライブラリ準備と、ローカル背景除去モデルを初めて使うときはインターネット接続が必要です。

---

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| IDが読めない | 「IDを自動で読み取る」をOFFにし、ファイル名をIDにする（例: `50053056.jpg`） |
| 顔写真とIDの組み合わせが違う | 撮影時刻を確認するか、1人分ずつサブフォルダに分ける |
| 背景が抜けない | ローカルAIまたはremove.bgを有効にし、処理ログを確認する |
| remove.bgが使えない | APIキーと通信環境を確認する。失敗時はローカルAIへ自動で切り替わる |
| 顔が大きすぎる / 切れる | 「顔の大きさ」を小さくするか、「保存前にプレビュー」で位置と倍率を調整する |
| 顔の角度を直したい | 「両目を水平に自動補正」をONにする。必要ならプレビューで角度を微調整する |
| 高画質化できない | 「AI高画質化（Real-ESRGAN）」をOFFにするか、配布ZIPを展開し直す |
| EXEが起動しない | ZIPを完全に展開し、EXE単体ではなくフォルダ一式で起動する |

---

## English

**Face photo + ID screenshot → FM portrait + config.xml, fully automated**

A semi-automatic tool for creating Football Manager player and staff face graphics. Drop in a face photo and an in-game ID screenshot, and the app can upscale the image, remove its background, crop the face, and generate `config.xml`.

Version 2.1.0 adds **automatic eye levelling**, a **tighter chin crop**, and per-image zoom, position, and rotation controls in the save preview.

### Features

- Face detection with YuNet and Haar fallback
- Automatic eye levelling using YuNet landmarks
- Optional 4× upscaling via Real-ESRGAN
- Transparent PNG background removal using local AI (rembg) or the remove.bg API
- Normalized square crop based on eye-to-chin distance with tighter space below the chin
- OCR-based ID reading with RapidOCR → saves as `<ID>.png`
- Automatic `config.xml` generation with append and duplicate-skip support
- Newgen support with the `r-` prefix option
- Preview with per-image zoom, position, and rotation adjustment before saving
- English / Japanese UI, dark / light themes, and persistent settings
- Safer saving, `config.xml` backups, and recoverable Recycle Bin cleanup

### Download and launch

Download `FM.Face.Processor_v2.1.0_Windows.zip` from the [v2.1.0 release page](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.1.0), then extract the ZIP.

Run:

```text
EXE\FM Face Processor\FM Face Processor.exe
```

Python is **not required** for the EXE version. Keep the entire `FM Face Processor` folder together; do not move only the EXE.

### How to use

1. Put face photos and screenshots containing FM IDs in the input folder.
   - OCR pairs screenshots with face photos using nearby capture times.
   - For the most reliable pairing, place each person's files in a separate subfolder.
2. Select separate input and output folders in the app.
3. Review the options and click **Run**.
4. The output folder will contain transparent PNG files, `config.xml`, and a processing log.
5. Copy the output to your FM graphics folder and reload the skin in-game.

If OCR cannot read an ID, turn off **Auto-read ID** and name the face image with the ID, for example `50053056.jpg`.

Check the processing log to confirm OCR pairing. **Preview before saving** is recommended for important face packs.

### About remove.bg

When remove.bg is enabled, the face image being processed is uploaded to remove.bg. Review its terms and privacy requirements before use.

- The API key is stored only in the settings file on your own PC
- The key is masked in the app
- Keys saved by older versions are kept and reused instead of being deleted
- Settings and API keys are never included in GitHub, the release ZIP, or the EXE
- If remove.bg is unavailable, processing continues with the local AI

### Data protection

- PNG files and `config.xml` are completed in temporary files before replacement
- A timestamped `.bak` is created before replacing or rebuilding an existing `config.xml`
- Processing is blocked when the input and output folders are the same
- Source cleanup moves files to the Windows Recycle Bin after confirmation instead of permanently deleting them
- Keep a separate backup of important source images

### Running from source

The source version requires 64-bit Python 3.12.

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 "FM Face Processor.py"
```

An internet connection is required when preparing the libraries and the first time the local background-removal model is used.

### Troubleshooting

| Issue | Fix |
|-------|-----|
| ID not detected | Turn off **Auto-read ID** and use the ID as the filename (for example, `50053056.jpg`) |
| Face and ID are paired incorrectly | Check capture times or place each person's files in a separate subfolder |
| Background is not removed | Enable local AI or remove.bg and check the processing log |
| remove.bg is unavailable | Check the API key and connection; the app automatically falls back to local AI |
| Face is too large / cut off | Lower the **Face size** setting |
| Face is tilted | Enable **Auto-level eyes**, then fine-tune the angle in the preview if needed |
| Upscaling fails | Turn off **AI upscale (Real-ESRGAN)** or extract the release ZIP again |
| EXE does not start | Fully extract the ZIP and launch it with the complete folder intact |

---

## 更新履歴 / Changelog

[Releasesページ / Releases](https://github.com/kumajia/FM-Face-Processor/releases)をご覧ください。

バージョン / Version: v2.1.0
