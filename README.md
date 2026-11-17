<div align="center">
  <img src="assets/fm_face_processor.png" alt="FM Face Processor icon" width="144">
  <h1>FM Face Processor</h1>
  <p><strong>顔写真 + IDスクショ → FMポートレート + config.xml を自動生成<br>Face photo + ID screenshot → FM portrait + config.xml</strong></p>
  <p><a href="#日本語">日本語</a> · <a href="#english">English</a> · <a href="#türkçe">Türkçe</a></p>
</div>

<div align="center">

[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)](https://www.microsoft.com/windows)
[![Python](https://img.shields.io/badge/Source-Python%203.12-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Release](https://img.shields.io/github/v/release/kumajia/FM-Face-Processor?label=Release)](https://github.com/kumajia/FM-Face-Processor/releases/latest)

</div>

---

## 日本語

Football Manager用の選手・スタッフ顔グラフィックを半自動で作るWindowsアプリです。顔写真とFM内IDのスクリーンショットを入力すると、高画質化・背景透過・顔トリミング・`config.xml`生成までまとめて行います。

### クイックスタート

1. [最新のReleasesページ](https://github.com/kumajia/FM-Face-Processor/releases/latest)から、名前が `_Windows.zip` で終わるファイルをダウンロードする
2. ZIPを完全に展開する
3. `EXE\FM Face Processor\FM Face Processor.exe`をダブルクリックする

**EXE版はPython不要です。** `FM Face Processor.exe`だけを移動せず、フォルダ一式のまま使用してください。

> 初めてローカル背景除去モデルを使用するときは、モデル取得のためインターネット接続と空き容量が必要です。remove.bgを有効にした場合のみ、処理対象の顔画像がremove.bgへ送信されます。

### 対応環境

- Windows版Football Managerで、カスタムグラフィックと`config.xml`を利用できる環境
- EXE版: Python不要。専用GPUは必須ではありませんが、処理速度はPC性能と選択機能により変わります
- ソース版: 64bit版Python 3.12
- 初回モデル取得、remove.bg利用、ソース版のライブラリ準備にはインターネット接続が必要

特定のFootball Manager年式には固定していません。環境固有の問題がある場合は、FMのバージョンを添えて[Issues](https://github.com/kumajia/FM-Face-Processor/issues/new)でお知らせください。

### 処理の流れ

```mermaid
flowchart LR
    A[顔写真] --> C[FM Face Processor]
    B[FM内IDが見えるスクショ] --> C
    G[またはIDを付けたファイル名] --> C
    C --> D[透過ポートレートPNG]
    C --> E[config.xml]
    C --> F[処理ログ]
```

### できること

| 機能 | 説明 |
|------|------|
| 🔍 顔検出 | YuNet（高精度）+ Haarカスケードでフォールバック |
| 🖼️ 高画質化 | Real-ESRGAN x4で拡大 |
| ✂️ 背景透過 | rembgで透過PNG化（モデル選択・髪のフチ調整あり） |
| 📐 水平補正 | YuNetの両目ランドマークで傾きを検出し、両目が水平になるよう自動補正 |
| 🎯 顔トリミング | 頭頂・目・口・顎・襟位置を基準にした正方形クロップ |
| 🔢 ID自動読取 | RapidOCRでスクショからIDを認識し、`<ID>.png`で保存 |
| 📄 config.xml生成 | 実行のたびに追記し、重複IDはスキップ |
| 👶 newgen対応 | IDに`r-`プレフィックスを付けるオプション |
| 🌐 UI | 日本語 / 英語 / トルコ語（既定はWindowsの表示言語に合わせる）、ダーク / ライトテーマ、スクロール対応、設定保存 |
| 👁️ 保存前プレビュー | トリミング前の画像を拡大しながら、襟の移動・手動回転・1:1の保存枠を1つの画面で調整 |
| ☁️ remove.bg API | 任意。失敗または不完全な結果ではローカルAIへ自動切替 |
| 🛡️ データ保護 | 一時ファイル保存、`config.xml`自動バックアップ、ごみ箱への移動 |

### 使い方

#### 「IDスクリーンショット」とは

Football Managerで、選手またはスタッフの固有IDが表示されたプロフィール画面のスクリーンショットです。

1. FMの環境設定を開き、検索欄に `ID` と入力する
2. 「スキン作成を補助するためタイトルバーに画面IDを表示する」を有効にする
   - FMのバージョンや表示言語によって、項目名が多少異なる場合があります
3. 選手またはスタッフのプロフィールを開き、`ID: 2000468148` のようなIDが見える状態でスクリーンショットを撮る

1. 入力フォルダに顔写真とFM内IDが写ったスクリーンショットを入れる
   - 複数人を処理する場合、撮影時刻が近い画像をOCRで組み合わせます
   - 1人分ずつサブフォルダに分けると、より確実です
2. アプリで別々の入力フォルダと出力フォルダを選ぶ
3. 必要なオプションを確認して「実行」を押す
4. 出力された透過PNG、`config.xml`、処理ログを確認する
5. 出力物をFMのグラフィックフォルダへ入れ、ゲーム内でスキンを再読み込みする

#### IDスクリーンショットを使わない場合

「IDを自動で読み取る」をOFFにし、顔画像を `50053056.jpg` のように **IDをファイル名にして**処理できます。この方法ではIDスクリーンショットは必要ありません。

### Football Managerへ導入する

一般的な保存先は次のとおりです。`20XX`は使用中のFMバージョンに置き換えてください。

```text
C:\Users\<ユーザー名>\Documents\Sports Interactive\Football Manager 20XX\graphics
```

OneDriveでドキュメントを同期している場合は、次の場所にあることがあります。

```text
C:\Users\<ユーザー名>\OneDrive\Documents\Sports Interactive\Football Manager 20XX\graphics
```

`graphics`フォルダがなければ作成し、出力したPNGと`config.xml`を同じフェイスパック用フォルダへ入れます。その後、FMの環境設定からキャッシュ使用を無効にし、スキン再読み込みを有効にして再読み込みしてください。項目名はFMのバージョンや表示言語で異なる場合があります。

### 保存前プレビュー

背景除去後・トリミング前の画像を表示し、自動で求めた切り抜き範囲を初期枠にします。襟の移動・保存枠・回転・出力サイズは、モードを切り替えずに1つの画面で操作します。

- **表示の拡大**: 画像の上でマウスホイールを回すと、カーソルの位置を中心に拡大・縮小します。拡大中は中ボタンのドラッグで表示を移動でき、「全体表示」で初期表示に戻ります。表示の拡大や移動は保存枠の位置・大きさや保存されるPNGに影響しません
- **保存枠**: 1:1の枠と下の角の●は常に表示されます。枠の内側をドラッグすると枠が動き、下の角の●をドラッグすると上辺の高さを保ったまま大きさが変わります。襟を選んでいないときは矢印キーでも枠が動きます
- **襟**: 「襟を選択」を押した次のドラッグ、または Shift＋ドラッグで片側の襟を四角く囲みます（枠の外も可）。襟を選んでいる間は画像の外枠と案内文がアクセント色になります。矢印キー（Shift で10倍）か選択の内側のドラッグで動かし、Enterで確定、Escで取り消すと、すぐ枠の操作に戻ります。Ctrl+Zで直前の襟移動を取り消せます。選択範囲は切り取って動かすので移動元に襟が二重に残らず、顔の楕円の内側（顎・口）は形も位置も変わりません
- **回転（微調整）**: −15°〜＋15°（自動水平補正の結果に対する微調整。スライダーを選んでいる間は矢印キーで0.1°ずつ、「0°に戻す」で解除）。保存画像は「襟の移動 → 回転 → 1:1の枠で切り抜き」の順に作ります。回転や拡大をしていても、画面で囲った範囲がそのまま選ばれ、矢印キーの向きどおりに動きます
- **出力サイズ**: 実行の最初のプレビューで 180×180 / 250×250 / 任意（1〜4096 px）から選びます。選ぶまで「保存」は押せません。選んだサイズは同じ実行の次のプレビューに引き継がれ、途中で変えてもすでに保存したPNGは変わりません。サイズを変えても構図は同じです。メイン画面に出力サイズの欄はありません
- **保存・スキップ・キャンセル**: 画像は1枚ずつプレビューを開きます。各画像は、そのプレビューで「保存」を押したときだけ保存されます（「全部保存」はありません）。「スキップ」はその画像を保存せずに次へ、「キャンセル」は保存せずに処理を止めます。操作しないまま10分たつと保存せずに閉じ、処理を止めます
- **次の画像の先読み**: 背景透過の処理先がCPUで、remove.bg を使わないときは、プレビューを開いている間に次の1枚の準備（読み込み・顔検出・背景除去）を進めます。保存・スキップのあと、次のプレビューが早く開きます。保存されるのは、これまでどおり各プレビューで「保存」を押した画像だけです。準備の途中でキャンセルした場合は、その準備が終わるのを待ってから止まります（準備した画像は保存しません）。GPUを選んだとき・remove.bg を使うときは先読みしません
- 枠の内側だけが透過PNGで保存されます。枠線・選択線・枠外の暗い表示はプレビュー専用でPNGに入りません
- 何も編集せずに保存すると、従来の自動出力と同じ画像になります
- 長い首では襟の開始位置を検出し、顔高さの約18%ぶん襟が見えるまで保存枠を自動拡張します

### 表示言語とフォント

- 右上の言語メニューの「Windowsに合わせる」が既定です。Windowsの表示言語が日本語なら日本語、トルコ語ならトルコ語、それ以外（英語・未対応の言語・取得できない場合）は英語で表示し、起動のたびに判定します
- メニューで日本語・English・Türkçeを選ぶと、その言語が優先されます（以前のバージョンで選んだ言語もそのまま使います）
- 日本語表示では、設定項目・ボタン・プレビューの案内・ログに Noto Sans JP（Regular）を使います。インストールされていない場合はメイリオ（標準）を使います。英語・トルコ語の表示は従来のフォントのままです

### remove.bgとプライバシー

remove.bgを有効にすると、処理対象の顔画像がremove.bgへ送信されます。利用規約とプライバシー要件を確認し、送信してよい画像だけに使用してください。

- APIキーは本人のPC内にある設定ファイルだけへ保存し、画面上では伏せ字で表示します
- 設定ファイルとAPIキーはGitHub、配布ZIP、EXEには含めません
- 旧バージョンが保存したキーは削除せず再利用します
- remove.bgが利用できない、または切り抜きが不完全な場合はローカルAIで再処理します

### データ保護

- PNGと`config.xml`は一時ファイルへ完成させてから置換します
- 既存`config.xml`の上書き・再生成前には、日時付き`.bak`を作成します
- 入力フォルダと出力フォルダが同じ場合は処理を開始しません
- 「入力元画像をゴミ箱へ」は確認後、完全削除ではなくWindowsのごみ箱へ移動します
- 大切な素材は、このアプリとは別の場所にも保管してください

### ソース版を使う場合

ソース版のみ64bit版Python 3.12が必要です。

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 "FM Face Processor.py"
```

`FM Face Processor.py`、`requirements.txt`、`assets`フォルダは同じ構成のまま置いてください。初回のライブラリ準備と、ローカル背景除去モデルを初めて使用するときはインターネット接続が必要です。

### 主な更新内容

#### v2.3.1

- トルコ語に対応し、右上の一覧から日本語・英語・トルコ語を選択可能に変更
- 選択した言語を保存し、起動時とガイド表示に反映
- 代替ダークテーマを、通常テーマと同系統の濃いグレーと水色に調整

#### v2.3.0

- 初回起動時に、IDスクリーンショットとファイル名による2種類のID指定方法を案内
- ヘッダーの「使い方」ボタンから、ID表示例付きガイドをいつでも表示
- 画面全体を縦スクロール対応にし、最小ウィンドウサイズを縮小
- 使用頻度の低い項目を「詳細設定」へまとめ、折りたたみ可能に変更
- 前回終了時のウィンドウサイズを保存

#### v2.2.1

- Windows EXEで、ローカル背景除去モデルの初回ダウンロード時に`'NoneType' object has no attribute 'write'`で失敗する問題を修正

#### v2.2.0

- 投稿ガイドに合わせ、頭頂を上端から約5%、顎を約82%へ配置
- 長い首の画像では襟を検出し、襟が見える位置まで保存範囲を自動拡張
- 保存前プレビューに、実際に保存される範囲を示す枠を追加
- 顔・口・顎を変形させず、顎下から襟までを圧縮する「首を短く」を追加
- 専用アプリアイコンを追加

### トラブルシューティング

| 症状 | 対処 |
|------|------|
| IDが読めない | 「IDを自動で読み取る」をOFFにし、顔画像のファイル名をIDにする（例: `50053056.jpg`） |
| 顔写真とIDの組み合わせが違う | 撮影時刻を確認するか、1人分ずつサブフォルダに分ける |
| 背景が抜けない | ローカルAIまたはremove.bgを有効にし、処理ログを確認する |
| remove.bgが使えない | APIキーと通信環境を確認する。失敗時はローカルAIへ自動で切り替わる |
| 顔が大きすぎる / 切れる | 「顔の大きさ」の数値を大きくするか、プレビューで枠を動かす・広げる |
| 首が長すぎる / 襟が枠の外にある | プレビューの「襟を選択」（または Shift＋ドラッグ）で左右の襟を上へ動かす |
| 顔が傾いている | 「両目を水平に自動補正」をONにし、必要ならプレビューで回転を微調整する |
| 高画質化できない | 「AI高画質化（Real-ESRGAN）」をOFFにするか、配布ZIPを展開し直す |
| EXEが起動しない | ZIPを完全に展開し、EXE単体ではなくフォルダ一式で起動する |

解決しない場合は[新しいIssueを作成](https://github.com/kumajia/FM-Face-Processor/issues/new)し、アプリのバージョン、FMのバージョン、症状、再現手順、処理ログを記載してください。公開前に、ログや画像から本名・メールアドレス・APIキーなどを削除してください。

### 支援

FM Face Processorは今後も無料で提供します。開発の継続を支援したい方は、Ko-fiから任意でサポートできます。

[FM Face Processorを支援する](https://ko-fi.com/kumajia)

支援は完全に任意です。FM Face Processorをご利用いただき、ありがとうございます。

### ライセンス

このリポジトリ内で作者が作成したソースコードは[MIT License](LICENSE)で提供しています。

第三者が提供するライブラリ、AIモデル、その他の依存物には、それぞれの権利者が定めるライセンスが適用されます。これらは本プロジェクトのMIT Licenseによって再許諾されるものではありません。

`FM Face Processor`の名称、ロゴ、`assets/fm_face_processor.png`を含む専用アイコン、プロモーション画像はMIT Licenseの対象外です。改変版・再配布版では別の名称とブランドを使用し、公式リリースまたは作者公認であると誤認させる表示を行わないでください。

本プロジェクトは独立した非公式プロジェクトであり、Sports InteractiveまたはSEGAとの提携・公認関係はありません。Football Managerおよび関連する名称・商標は、それぞれの権利者に帰属します。

詳しくは[BRANDING.md](BRANDING.md)をご覧ください。

### 更新履歴

すべての更新履歴は[Releasesページ](https://github.com/kumajia/FM-Face-Processor/releases)をご覧ください。

---

## English

FM Face Processor is a semi-automatic Windows app for creating Football Manager player and staff face graphics. Give it a face photo and an in-game ID screenshot, and it can upscale the image, remove its background, crop the face, and generate `config.xml`.

### Quick start

1. Open the [latest Releases page](https://github.com/kumajia/FM-Face-Processor/releases/latest) and download the file whose name ends in `_Windows.zip`
2. Fully extract the ZIP
3. Double-click `EXE\FM Face Processor\FM Face Processor.exe`

**Python is not required for the EXE version.** Keep the entire `FM Face Processor` folder together; do not move only the EXE.

> An internet connection and free disk space are required the first time the local background-removal model is downloaded. A face image is uploaded to remove.bg only when you enable the remove.bg option.

### Requirements

- A Windows version of Football Manager that supports custom graphics and `config.xml`
- EXE version: Python is not required. A dedicated GPU is not required, but processing speed depends on your PC and the selected features
- Source version: 64-bit Python 3.12
- Internet access for the first model download, remove.bg, and source dependency installation

The app is not tied to one specific Football Manager yearly release. If you encounter a version-specific problem, include your FM version in a new [Issue](https://github.com/kumajia/FM-Face-Processor/issues/new).

### Workflow

```mermaid
flowchart LR
    A[Face photo] --> C[FM Face Processor]
    B[In-game screenshot showing the ID] --> C
    G[Or an ID-based filename] --> C
    C --> D[Transparent portrait PNG]
    C --> E[config.xml]
    C --> F[Processing log]
```

### Features

| Feature | Description |
|---------|-------------|
| 🔍 Face detection | High-accuracy YuNet with Haar cascade fallback |
| 🖼️ Upscaling | Optional 4× enlargement with Real-ESRGAN |
| ✂️ Background removal | Transparent PNG output using rembg or the optional remove.bg API |
| 📐 Eye levelling | Uses YuNet eye landmarks to correct tilt automatically |
| 🎯 Face crop | Square crop based on crown, eyes, mouth, chin, and detected collar position |
| 🔢 Automatic ID reading | RapidOCR reads the ID screenshot and saves the portrait as `<ID>.png` |
| 📄 config.xml | Appends entries automatically and skips duplicate IDs |
| 👶 Newgen support | Optional `r-` prefix for newgen IDs |
| 🌐 UI | English / Japanese / Turkish (follows the Windows display language by default), dark / light themes, scrolling, and persistent settings |
| 👁️ Preview before saving | Zoom into the uncropped image and adjust collar moves, rotation, and the 1:1 saved frame on one screen |
| ☁️ remove.bg API | Optional; falls back to local AI when unavailable or incomplete |
| 🛡️ Data protection | Temporary-file saving, automatic `config.xml` backups, and Recycle Bin cleanup |

### How to use

#### What is an ID screenshot?

It is a screenshot of a player or staff profile in Football Manager with that person's unique ID visible.

1. Open **Preferences** in FM and search for `ID`.
2. Enable **Show screen IDs in the title bar to assist skinning**.
   - The exact option name may vary slightly depending on the FM version and display language.
3. Open the player or staff profile and take a screenshot with an ID such as `ID: 2000468148` visible.

1. Put face photos and screenshots containing FM IDs in the input folder
   - OCR pairs screenshots with face photos using nearby capture times
   - For the most reliable pairing, place each person's files in a separate subfolder
2. Select separate input and output folders in the app
3. Review the options and click **Run**
4. Check the transparent PNG files, `config.xml`, and processing log in the output folder
5. Copy the output to your FM graphics folder and reload the skin in-game

#### Using the app without an ID screenshot

Turn off **Auto-read ID** and name the face image with the ID, for example `50053056.jpg`. No ID screenshot is required when using this method.

### Install the output in Football Manager

The usual Windows location is shown below. Replace `20XX` with your FM version.

```text
C:\Users\<username>\Documents\Sports Interactive\Football Manager 20XX\graphics
```

If OneDrive syncs your Documents folder, it may instead be located at:

```text
C:\Users\<username>\OneDrive\Documents\Sports Interactive\Football Manager 20XX\graphics
```

Create the `graphics` folder if it does not exist, then put the generated PNG files and `config.xml` together in a face-pack folder. In FM Preferences, disable skin caching, enable skin reloading, and reload the skin. Setting names can vary by FM version and display language.

### Preview before saving

The preview shows the background-removed image before cropping, with the automatically calculated crop as the starting frame. Collar moves, the saved frame, rotation, and output size all work on one screen, with no mode switching.

- **Zoom**: turn the mouse wheel over the image to zoom around the cursor. While zoomed, drag with the middle button to pan; **Fit to view** returns to the initial view. Zooming and panning never change the frame or the saved PNG
- **Frame**: the 1:1 frame and its bottom corner handles are always shown. Drag inside the frame to move it, or drag a bottom corner to resize it while the top edge stays put. With no collar selected, the arrow keys also move the frame
- **Collar**: click **Select collar** and drag, or Shift+drag, to draw a rectangle around one side of the collar (outside the frame is fine). While a collar is selected, the image border and the hint turn the accent color. Move it with the arrow keys (Shift = 10x) or by dragging inside the selection; Enter commits and Esc cancels, both returning straight to the frame. Ctrl+Z undoes the last collar move. The selection is cut, not copied, so no second collar is left behind, and the face ellipse (chin and mouth) keeps its shape and position
- **Rotation (fine-tune)**: -15° to +15° on top of auto-leveling (0.1° per arrow key while the slider has focus; **Reset to 0°** clears it). The saved image is built as collar moves -> rotation -> 1:1 crop. Even when rotated or zoomed, the area you enclose on screen is the one selected, and it moves in the arrow key's direction
- **Output size**: in the first preview of a run, choose 180×180, 250×250 or Custom (1-4096 px); Save stays disabled until a size is chosen. The size carries over to the next previews of the run, and changing it later never alters PNGs already saved. Switching keeps the same composition. The main window has no output-size field
- **Save, Skip, Cancel**: every image opens its own preview and is saved only when you press Save in that preview (there is no Save all). Skip moves on without saving the image; Cancel stops without saving. A preview left idle for 10 minutes closes without saving and stops the run
- **Next image prepared in advance**: when background removal runs on CPU and remove.bg is not used, the next image is prepared (loading, face detection, background removal) while a preview is open, so the next preview opens sooner after Save or Skip. As before, only images you Save in their own preview are written. If you cancel while an image is being prepared, the run stops once that preparation has finished (the prepared image is not saved). Nothing is prepared in advance with GPU selected or with remove.bg
- Only the inside of the frame is saved as a transparent PNG. The frame line, selection outline, and dimming are preview-only
- Saving without any edit produces the same image as the automatic output
- For long-neck sources, the crop expands past the detected collar line to retain roughly 18% of the face height below it

### Display language and fonts

- **Match Windows** is the default in the language menu: Japanese Windows shows Japanese, Turkish Windows shows Turkish, and anything else (English, unsupported languages, or if the language cannot be read) shows English. It is checked at every launch
- Choosing 日本語, English, or Türkçe in the menu takes priority (a language chosen in an older version is kept)
- In Japanese, settings, buttons, preview hints, and the log use Noto Sans JP (Regular), or Meiryo (regular weight) if Noto Sans JP is not installed. English and Turkish keep the existing fonts

### remove.bg and privacy

When remove.bg is enabled, the face image being processed is uploaded to remove.bg. Review its terms and privacy requirements and use it only for images you are allowed to upload.

- The API key is stored only in the settings file on your PC and is masked in the app
- Settings and API keys are never included in GitHub, the release ZIP, or the EXE
- Keys saved by older versions are retained and reused
- If remove.bg is unavailable or returns an incomplete cutout, the app retries with the local AI

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

Keep `FM Face Processor.py`, `requirements.txt`, and the `assets` folder in the same directory structure. Internet access is required while installing dependencies and the first time the local background-removal model is used.

### What's new

#### v2.3.1

- Adds Turkish alongside Japanese and English in the language selector
- Saves the selected language and applies it to startup and the built-in guide
- Matches the fallback dark palette to the standard charcoal and cyan theme

#### v2.3.0

- Explains both ID-screenshot and filename-based workflows on first launch
- Adds an always-available **Guide** button with a visible ID example
- Makes the whole interface vertically scrollable and reduces the minimum window size
- Moves less frequently used options into a collapsible **Advanced settings** section
- Remembers the window size from the previous session

#### v2.2.1

- Fixes a Windows EXE failure during the first download of a local background-removal model: `'NoneType' object has no attribute 'write'`

#### v2.2.0

- Places the crown at roughly 5% from the top and the chin near 82% to better match cut-out submission guidelines
- Detects the collar on long-neck sources and automatically expands the saved area to retain it
- Shows the exact saved boundary in the preview
- Adds **Shorten neck**, which compresses only the area below the protected chin
- Adds a dedicated FM Face Processor application icon

### Troubleshooting

| Issue | Fix |
|-------|-----|
| ID not detected | Turn off **Auto-read ID** and use the ID as the face-image filename, for example `50053056.jpg` |
| Face and ID are paired incorrectly | Check capture times or place each person's files in a separate subfolder |
| Background is not removed | Enable local AI or remove.bg and check the processing log |
| remove.bg is unavailable | Check the API key and connection; the app automatically falls back to local AI |
| Face is too large or cropped | Increase **Face size** or move/enlarge the frame in the preview |
| Neck is too long / collar is outside the frame | In the preview, raise each side of the collar with **Select collar** (or Shift+drag) |
| Face is tilted | Enable **Auto-level eyes**, then fine-tune the rotation in the preview if needed |
| Upscaling fails | Turn off **AI upscale (Real-ESRGAN)** or extract the release ZIP again |
| EXE does not start | Fully extract the ZIP and launch it with the complete folder intact |

If the problem remains, [open a new Issue](https://github.com/kumajia/FM-Face-Processor/issues/new) and include the app version, FM version, symptoms, reproduction steps, and processing log. Remove names, email addresses, API keys, and other personal information from logs and images before posting them publicly.

### Support

FM Face Processor is and will remain free to use. If it has saved you time and you would like to support continued development, you can do so on Ko-fi.

[Support FM Face Processor](https://ko-fi.com/kumajia)

Support is completely optional. Thank you for using FM Face Processor.

### License

Source code authored for this repository is licensed under the [MIT License](LICENSE).

Third-party libraries, AI models, and other dependencies remain subject to their respective owners' licenses. They are not relicensed under this project's MIT License.

The `FM Face Processor` name, logo, dedicated icons including `assets/fm_face_processor.png`, and promotional artwork are not covered by the MIT License. Modified or redistributed versions must use a different name and branding and must not imply that they are official releases or endorsed by the author.

This is an independent, unofficial project and is not affiliated with or endorsed by Sports Interactive or SEGA. Football Manager and related names and marks belong to their respective owners.

See [BRANDING.md](BRANDING.md) for details.

### Changelog

See the [Releases page](https://github.com/kumajia/FM-Face-Processor/releases) for the complete changelog.


---

## Türkçe

FM Face Processor, Football Manager için oyuncu ve personel portreleri hazırlayan ücretsiz bir Windows uygulamasıdır. Yüzleri algılar ve kırpar; isteğe bağlı olarak görüntüleri büyütür, arka planı kaldırır, kişi ID’siyle şeffaf PNG dosyaları kaydeder ve `config.xml` oluşturur.

### Dil seçimi

v2.3.1 sürümünde sağ üstteki dil listesinden **Türkçe**’yi seçin. Dil tercihiniz kaydedilir. Japonca ve İngilizce de kullanılabilir.

### Hızlı başlangıç

1. Uygulamada girdi ve çıktı için **ayrı klasörler** seçin.
2. Kişinin ID’sini şu iki yöntemden biriyle belirtin:
   - **Dosya adı:** “ID’yi otomatik oku” seçeneğini kapatın ve yüz fotoğrafını `2000468148.jpg` gibi kişinin ID’siyle adlandırın. Ekran görüntüsü gerekmez.
   - **Ekran görüntüsü:** FM Tercihleri’nde `ID`yi arayın ve arayüz tasarımı için ekran ID’lerini gösteren seçeneği etkinleştirin. Kişinin ID’sinin göründüğü profil ekran görüntüsünü ve yüz fotoğrafını aynı girdi klasörüne koyun. Her kişi için ayrı bir alt klasör kullanmak eşleştirmeyi daha güvenilir kılar.
3. **Başlat**’a tıklayın. Çıktı klasöründeki PNG dosyalarını, `config.xml` dosyasını ve işlem günlüğünü kontrol edin.
4. PNG dosyalarını ve `config.xml` dosyasını Football Manager’ın aynı yüz paketi klasörüne koyun, ardından oyunda arayüzü yeniden yükleyin.

### Kaynak koddan çalıştırma

Windows’ta 64 bit Python 3.12 ile, arşivi çıkardığınız klasörde şu komutları çalıştırın:

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 "FM Face Processor.py"
```

Yapay zekâ ile büyütme için isteğe bağlı Real-ESRGAN ONNX modeli ayrıca gerekir. İlk yerel arka plan kaldırma işleminde model indirmek için internet bağlantısı gerekir. **remove.bg API** seçeneği etkinleştirilirse işlenen yüz görüntüleri remove.bg hizmetine gönderilir. API anahtarı yalnızca bu bilgisayarda saklanır.

PNG oluşmazsa işlem günlüğünü kontrol edin. Yüz fotoğrafı ile ID ekran görüntüsünün kayıt zamanları birbirinden uzaksa, ikisini kişiye özel aynı alt klasöre koyun veya dosya adı yöntemini kullanın.
