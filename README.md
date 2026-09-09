<div align="center">
  <img src="assets/fm_face_processor.png" alt="FM Face Processor icon" width="144">
  <h1>FM Face Processor v2.2.1</h1>
  <p><strong>顔写真 + IDスクショ → FMポートレート + config.xml を全自動生成</strong></p>
  <p><a href="#日本語">日本語</a> · <a href="#english">English</a></p>
</div>

[![Python](https://img.shields.io/badge/Python-3.12-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](https://github.com/kumajia/FM-Face-Processor/blob/main/LICENSE)
[![Release](https://img.shields.io/badge/Release-v2.2.1-2ea44f)](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.2.1)

Football Manager 用の選手・スタッフ顔グラフィックを半自動で作るツールです。顔写真とFM内IDのスクショを放り込むだけで、高画質化・背景透過・顔トリミング・`config.xml` 生成までまとめて行います。

<a id="日本語"></a>


## v2.2.1の主な変更

- Windows EXEで、ローカル背景除去モデルの初回ダウンロード時に `'NoneType' object has no attribute 'write'` で失敗する問題を修正

### v2.2.0

- 投稿ガイドに合わせ、頭頂を上端から約5%、顎を約82%へ配置
- 長い首の画像では襟を検出し、襟が見える位置まで保存範囲を自動拡張
- 保存前プレビューに、実際に保存される範囲を示す枠を追加
- 顔・口・顎を変形させず、顎下から襟までを圧縮する「首を短く」を追加
- FM Face Processor専用のアプリアイコンを追加

---

## できること

| 機能 | 説明 |
|------|------|
| 🔍 顔検出 | YuNet（高精度）+ Haar カスケードでフォールバック |
| 🖼️ 高画質化 | Real-ESRGAN x4 で拡大 |
| ✂️ 背景透過 | rembg で透過 PNG 化（モデル選択・髪のフチ調整あり） |
| 📐 水平補正 | YuNetの両目ランドマークで傾きを検出し、両目が水平になるよう自動補正 |
| 🎯 顔トリミング | 頭頂・目・口・顎を基準に、長い首でも襟位置まで自動拡張する正方形クロップ |
| 🔢 ID 自動読取 | RapidOCR でスクショから ID を認識 → `<ID>.png` で保存 |
| 📄 config.xml 生成 | 実行のたびに追記、重複 ID はスキップ |
| 👶 newgen 対応 | ID に `r-` プレフィックスを付けるオプション |
| 🌐 UI | 日本語 / 英語、ダーク / ライト テーマ、設定の保存、専用アプリアイコン |
| 👁️ 保存前プレビュー | 正確な保存枠を見ながら、拡大率・位置・角度・首の長さを1枚ずつ微調整 |
| ☁️ remove.bg API | 任意で使用可能。通信失敗や不完全な切り抜きを検出するとローカルAIへ自動で切り替え |
| 🛡️ データ保護 | 一時ファイル保存、`config.xml` の自動バックアップ、ごみ箱への移動 |

---

## ダウンロードと起動

[Releasesページ](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.2.1)から `FM.Face.Processor_v2.2.1_Windows.zip` をダウンロードして展開してください。

```
FM Face Processor_v2.2.1/
├── EXE/
│   └── FM Face Processor/
│       ├── FM Face Processor.exe
│       └── 必要な実行ファイル・AIモデル一式
├── assets/
│   └── fm_face_processor.png
└── README.md
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

### 保存前プレビュー

- 枠の**内側だけ**が保存されます。枠線そのものはPNGに入りません
- ダークテーマでは白枠、ライトテーマでは黒枠で表示します
- 長い首では襟の開始位置を検出し、顔高さの約18%ぶん襟が見えるまで保存枠を自動拡張します
- 「首を短く」は顔検出で得た顎位置を保護し、顎より下から襟までの首部分だけを滑らかに縦圧縮します
- 短縮量は検出した首の長さの15%以内に自動制限します。襟が写っていない場合も、顎より下の首末端を使って調整できます

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
- remove.bgが成功応答でも背景がほぼ残っている場合は、不完全な結果と判定してローカルAIで再処理します

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

ソース版では `FM Face Processor.py`、`requirements.txt`、`assets` フォルダを同じ構成のまま置いてください。

---

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| IDが読めない | 「IDを自動で読み取る」をOFFにし、ファイル名をIDにする（例: `50053056.jpg`） |
| 顔写真とIDの組み合わせが違う | 撮影時刻を確認するか、1人分ずつサブフォルダに分ける |
| 背景が抜けない | ローカルAIまたはremove.bgを有効にし、処理ログを確認する |
| remove.bgが使えない | APIキーと通信環境を確認する。失敗時はローカルAIへ自動で切り替わる |
| 顔が大きすぎる / 切れる | 「顔の大きさ」の数値を大きくするか、「保存前にプレビュー」で位置と倍率を調整する |
| 首が長すぎる | 「保存前にプレビュー」をONにし、「首を短く」を少しずつ上げる |
| 顔の角度を直したい | 「両目を水平に自動補正」をONにする。必要ならプレビューで角度を微調整する |
| 高画質化できない | 「AI高画質化（Real-ESRGAN）」をOFFにするか、配布ZIPを展開し直す |
| EXEが起動しない | ZIPを完全に展開し、EXE単体ではなくフォルダ一式で起動する |

---

## English

**Face photo + ID screenshot → FM portrait + config.xml, fully automated**

A semi-automatic tool for creating Football Manager player and staff face graphics. Drop in a face photo and an in-game ID screenshot, and the app can upscale the image, remove its background, crop the face, and generate `config.xml`.

### What's new in v2.2.1

- Fixes a Windows EXE failure during the first download of a local background-removal model: `'NoneType' object has no attribute 'write'`

#### v2.2.0

- Places the crown at roughly 5% from the top and the chin near 82% to better match cut-out submission guidelines
- Detects the collar on long-neck sources and automatically expands the saved area to retain it
- Shows the exact saved boundary in the preview
- Adds **Shorten neck**, which compresses only the area below the protected chin without changing the face, mouth, or jaw
- Adds a dedicated FM Face Processor application icon

### Features

| Feature | Description |
|---------|-------------|
| 🔍 Face detection | High-accuracy YuNet with Haar cascade fallback |
| 🖼️ Upscaling | Optional 4× enlargement with Real-ESRGAN |
| ✂️ Background removal | Transparent PNG output using rembg or the optional remove.bg API |
| 📐 Eye levelling | Uses YuNet eye landmarks to correct tilt automatically |
| 🎯 Face crop | Normalized square crop based on crown, eyes, mouth, chin, and detected collar position |
| 🔢 Automatic ID reading | RapidOCR reads the ID screenshot and saves the portrait as `<ID>.png` |
| 📄 `config.xml` | Appends entries automatically and skips duplicate IDs |
| 👶 Newgen support | Optional `r-` prefix for newgen IDs |
| 🌐 UI | English / Japanese, dark / light themes, persistent settings, and a dedicated app icon |
| 👁️ Preview before saving | Exact saved frame plus per-image zoom, position, rotation, and neck-length adjustment |
| ☁️ remove.bg API | Optional; automatically falls back to local AI when unavailable or when the returned cutout is incomplete |
| 🛡️ Data protection | Temporary-file saving, automatic `config.xml` backups, and recoverable Recycle Bin cleanup |

### Download and launch

Download `FM.Face.Processor_v2.2.1_Windows.zip` from the [v2.2.1 release page](https://github.com/kumajia/FM-Face-Processor/releases/tag/v2.2.1), then extract the ZIP.

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

### Preview before saving

- Only the **inside of the frame** is saved; the line itself is never written to the PNG
- The frame is white in dark mode and black in light mode
- For long-neck sources, the crop expands past the detected collar line to keep a visible collar depth of roughly 18% of the face height
- **Shorten neck** protects the detected chin and smoothly compresses only the neck area below it without changing the face scale
- The adjustment is limited to 15% of the detected neck length; when no collar is visible, it safely uses the end of the cut-out below the protected chin

### About remove.bg

When remove.bg is enabled, the face image being processed is uploaded to remove.bg. Review its terms and privacy requirements before use.

- The API key is stored only in the settings file on your own PC
- The key is masked in the app
- Keys saved by older versions are kept and reused instead of being deleted
- Settings and API keys are never included in GitHub, the release ZIP, or the EXE
- If remove.bg is unavailable, processing continues with the local AI
- If remove.bg returns successfully but leaves most of the background intact, the app detects the incomplete mask and retries with the local AI

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

Keep `FM Face Processor.py`, `requirements.txt`, and the `assets` folder in the same directory structure when running from source.

### Troubleshooting

| Issue | Fix |
|-------|-----|
| ID not detected | Turn off **Auto-read ID** and use the ID as the filename (for example, `50053056.jpg`) |
| Face and ID are paired incorrectly | Check capture times or place each person's files in a separate subfolder |
| Background is not removed | Enable local AI or remove.bg and check the processing log |
| remove.bg is unavailable | Check the API key and connection; the app automatically falls back to local AI |
| Face is too large | Increase the **Face size** setting (a larger value makes the face smaller) |
| Chin is cut off | Use v2.2.0 or later; it corrects shallow chin detection and leaves safer space below it |
| Neck is too long | Enable **Preview before saving** and gradually increase **Shorten neck** |
| Face is tilted | Enable **Auto-level eyes**, then fine-tune the angle in the preview if needed |
| Upscaling fails | Turn off **AI upscale (Real-ESRGAN)** or extract the release ZIP again |
| EXE does not start | Fully extract the ZIP and launch it with the complete folder intact |

---

## 支援 / Support

FM Face Processorは今後も無料で提供します。開発の継続を支援したい方は、Ko-fiから任意でサポートできます。

**[FM Face Processorを支援する / Support FM Face Processor](https://ko-fi.com/kumajia)**

支援は完全に任意です。FM Face Processorをご利用いただき、ありがとうございます。

FM Face Processor is and will remain free to use. If it has saved you time and you would like to support continued development, you can do so on Ko-fi.

Support is completely optional. Thank you for using FM Face Processor.

---



## ライセンス / License

ソースコードは [MIT License](LICENSE) で提供しています。

**FM Face Processor** の名称、ロゴ、アイコン、プロモーション画像はMIT Licenseの対象外です。改変版・再配布版では別の名称とブランドを使用し、公式リリースであると誤認させる表示を行わないでください。

The source code is licensed under the [MIT License](LICENSE). The **FM Face Processor** name, logo, icons, and promotional artwork are not covered by the MIT License. Modified or redistributed versions must use a different name and branding and must not imply that they are official releases.

詳しくは [BRANDING.md](BRANDING.md) をご覧ください。 / See [BRANDING.md](BRANDING.md) for details.

---

## 更新履歴 / Changelog

[Releasesページ / Releases](https://github.com/kumajia/FM-Face-Processor/releases)をご覧ください。

バージョン / Version: v2.2.1
