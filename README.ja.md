[English](README.md)

# touchdesigner-bridge

実行中の TouchDesigner（以下 TD）を、外部のシェルから調査、編集するコマンドラインツールです。

- **対象**: Claude Code、Codex CLI、Cursor のようにコマンドを実行できるエージェントと、シェルスクリプトです。Claude Desktop のチャットのようにシェルを持たないクライアントからは使用できません。
- **機能**: TD の起動、負荷の高いオペレーター（以下 OP）の特定、エラーの一覧表示、パラメータと CHOP の値の確認、TOP の画像の取得、ネットワークの構築ができます。
- **出力**: 既定値と異なるパラメータだけを返し、行数に上限を設けた短いテキストで出力します。
- **接続**: 一時フォルダ内のファイルを介して通信します。ポートは開きません。
- **要件**: Python 3（標準ライブラリのみ）と TD が必要です。
- **対応環境**: Windows、macOS、WSL で動作します。

## 導入

### エージェントから利用する場合

このリポジトリの URL をエージェントに伝えて、作業を依頼します。エージェントがリポジトリを取得して TD を起動、接続し、コマンドの使用方法を `td.py help` と `td.py guide` で確認します。

```
https://github.com/monoton-music/touchdesigner-bridge を使って、show.toe で fps が落ちている原因を調べて
```

プロジェクトで常に使用させる場合は、そのプロジェクトの `AGENTS.md` または `CLAUDE.md` に次の 1 行を追加します。エージェントがセッションの開始時に読み込むのはこれらのファイルで、このリポジトリのファイルは読み込まないためです。

```
TouchDesigner の作業には touchdesigner-bridge（path/to/touchdesigner-bridge/td.py）を使用する。最初に `python3 td.py guide` を実行し、その内容に従う。
```

### コマンドを直接実行する場合

```bash
git clone https://github.com/monoton-music/touchdesigner-bridge
cd touchdesigner-bridge
python3 td.py open scratch.toe      # TD を起動して接続します。ファイルがなければ空のプロジェクトを作成します
python3 td.py err                   # エラーと警告を表示します
python3 td.py cook /project1        # cook 時間の長い OP を表示します
python3 td.py help                  # 全てのコマンドを表示します
```

## 接続

`td.py open FILE.toe` は TD を起動し、接続できたら終了します。ファイルがない場合は、ブリッジを読み込む Execute DAT（`/local/touchdesigner_bridge`）を入れた空のプロジェクトを作成します。既存の .toe に `--install` を付けると、同じ DAT を追加してからファイルを開きます。この DAT が入った .toe は、以後は開くだけで接続できます。ファイルは TD に付属する `toeexpand` と `toecollapse` で書き換え、元のファイルは `.bak` として残します。

.toe を変更せずに接続する場合は、実行中の TD のテキストポート（Alt+T、macOS では Option+T）に次の 1 行を貼り付けて実行します。`python3 td.py setup` は、現在の環境のパスでこのコードを出力し、クリップボードにもコピーします。

```python
exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
```

接続先の指定と、起動するビルドの選択は次のとおりです。

- TD を複数起動している場合は、`--to`（または環境変数 `TOUCHDESIGNER_BRIDGE_TO`）に .toe のファイル名の一部か PID を指定します。応答中の TD は `td.py status` で一覧表示できます。
- TD の実行ファイルは、Windows では `C:\Program Files\Derivative`、macOS では `/Applications` から検索します。別の場所にある場合は、`TOUCHDESIGNER_BIN` に bin フォルダを指定します（macOS では `TouchDesigner.app/Contents/MacOS`）。
- `open` は .toe を保存したビルドで開き、そのビルドがインストールされていない場合はより新しいビルドを使用します。古いビルドは未対応の要素（2023 のビルドでは POP 全般）を読み飛ばし、そのまま保存するとデータが失われるので、古いビルドでは開きません。

## コマンド

全てのコマンドは `python3 td.py help` で確認できます。主なコマンドを以下に示します。

### 問題の調査

```bash
python3 td.py info                           # ビルド、実測の fps、スキップされたフレーム数を表示します
python3 td.py err                            # エラーと警告を表示します。同じ内容は 1 行にまとめて件数を表示します
python3 td.py cook /project1 --n 20          # OP を cook 時間の長い順に表示し、COMP ごとの合計も表示します
python3 td.py mem /project1                  # OP をメモリ使用量の多い順に表示します
python3 td.py find / --expr 'absTime'        # 式に absTime を含む OP を検索します
```

### ネットワークの確認

```bash
python3 td.py ls /project1 --depth 2 --type '*TOP'   # 子の OP を一覧表示します
python3 td.py op /project1/noise1            # 型、接続、パラメータ、エラー、cook 時間をまとめて表示します
python3 td.py flow /project1                 # COMP 内の配線を a -> b の形式で表示します
python3 td.py par '/project1/noise*' seed    # 既定値と異なるパラメータだけを表示します。名前に * と ? を使用できます
python3 td.py parinfo /project1/noise1 type  # パラメータのラベル、型、メニューの値、範囲を表示します
python3 td.py chop /project1/null1           # チャンネルの現在値を表示します
python3 td.py text /project1/script1         # DAT の内容を表示します
python3 td.py geo /project1/sphere1          # SOP と POP の点数、範囲、アトリビュートを表示します
python3 td.py doc noiseTOP --grep seed       # TD に付属するヘルプを検索します
```

### 画像と動きの確認

```bash
python3 td.py top /project1/out1             # 長辺 768 px 以下の PNG として保存し、パスを返します
python3 td.py top /project1/out1 --max 0 --crop 800,400,320,180    # 指定した領域を等倍で保存します（左上からの x、y、幅、高さ）
python3 td.py look /project1/out1            # 画像の階調、黒つぶれと白とびの面積、色相を数値で表示します
python3 td.py compare /project1/out1 /project1/lv1 opacity=0.5     # 同じフレームで変更の前後を比較し、変更は元に戻します
python3 td.py motion /project1/out1          # 連続フレームの一覧画像を保存し、動きの速度と位置を表示します
python3 td.py trace /project1/cam ty rz frames=300                 # パラメータの時間変化（範囲、速度、急変、反転）を表示します
python3 td.py record /project1/out1 --seconds 3 --file clip.mp4    # 指定した秒数を動画に書き出します。再生して動きのテンポを確認するのに使います
python3 td.py shot /project1                 # /project1 をネットワークエディタに表示して、TD のウィンドウを画像に保存します
```

### ネットワークの編集

以下のコマンドによる変更は、どれも 1 回の `undo` で取り消せます。

```bash
python3 td.py set /project1/noise1 seed=3 tx='=absTime.seconds'
python3 td.py new /project1 levelTOP lv1 opacity=0.5
python3 td.py wire /project1/noise1 /project1/lv1
python3 td.py build /project1 - <<'END'      # 複数の OP を一度に作成して接続します
n noiseTOP seed=4
lv levelTOP
out nullTOP
n -> lv
lv -> out
END
python3 td.py write /project1/shader - < shader.glsl   # DAT のテキストを上書きします。シェーダーはコンパイルし、エラーがあれば出力します
python3 td.py layout /project1               # OP を配線に沿って整列します
python3 td.py note /project1/lv1 '黒レベルを上げる'    # OP にコメントを設定します
python3 td.py knob /project1 Gain=0.5@lv1.opacity      # COMP にカスタムパラメータを追加し、lv1.opacity に連動させます
python3 td.py rm /project1/lv1 --yes         # --yes を指定しない場合は、削除対象の OP を表示するだけです
python3 td.py undo
python3 td.py py 'result = [c.name for c in root.children]'   # 任意の Python コードを実行します。専用のコマンドがない操作に使用します
```

操作の手順、TD の挙動、`build` の例は [GUIDE.md](GUIDE.md) に記載しており、`python3 td.py guide` でも表示できます。

## 変更系コマンドの動作

- `new`、`build`、`set`、`wire` は、操作した OP を cook し、発生したエラーを出力に含めます。TD は cook していない OP のエラーを検出しないので、作成した直後に `err` だけを実行してもエラーは表示されません。
- `set` は、TD が受け付けない値や評価に失敗する式を拒否し、パラメータを元に戻します。
- `build` は 1 行でも失敗すると、途中で作成した OP を全て削除します。
- `new` と `build` で作成した OP はビューアーが有効になります。ビューアーは cook 時間を消費するので、負荷が高い場合は `td.py flag '/project1/*' viewer=0` で無効にします。

## OP の配置

- 空の COMP に `build` を実行すると、OP を配線に沿って左から右へ整列します。
- 式やマテリアルの設定で参照されている OP は、参照元の OP へ接続されているものとみなして隣に配置します。たとえば、ほかの OP の式から読まれる LFO は、その OP のすぐ左に配置します。
- 既に OP がある COMP では、追加した OP を接続先の隣に配置し、既存の OP は移動しません。ただし、既存の配線の途中に挿入するときに間隔が不足する場合は、右側の OP をまとめて右へ移動し、その旨を出力に記載します。
- `layout` は COMP 全体を再配置するので、ブリッジ以外が配置した OP がある COMP では実行を拒否します。所有者から整理を依頼された場合は `--yes` を指定します。
- ブリッジが配置した OP の記録は、TD の起動中だけ保持されます。

## 実行タイミングの指定

静止画の内容は取得したフレームで決まるので、シェルの `sleep` では正確にタイミングを合わせられません。`py` 以外のコマンドには `--when 'EXPR'` を指定でき、Python の式が真になった最初のフレームでコマンドを実行します。`--wait` の秒数（既定値は 60）を経過しても真にならなければ、エラーを返します。

```bash
python3 td.py top /project1/out1 --when 'absTime.seconds % 38 > 9'
python3 td.py wait --when 'op("/project1/timer1")["done"].eval() == 1'   # 式が真になるまでシェルスクリプトを待機させます
```

## 画像の保存先

- `top`、`motion`、`compare`、`record` は、`--file` で指定したパスに保存します。省略した場合は一時フォルダ内の `touchdesigner-bridge/<pid>/img` に保存し、そのパスを返します。
- このフォルダは TD を終了しても残ります。終了してから 10 分以上経ったフォルダは、次にいずれかの TD がブリッジを読み込んだときに削除されます。残したい画像は `--file` で保存先を指定してください。
- macOS で `shot` を使用するには、ターミナルに画面収録の権限が必要です。Windows では TD が自身のウィンドウをキャプチャするので、SSH や WSL からも実行できます。
- `record` は、H.264 に対応していない環境では Motion JPEG で書き出します。

## cook 時間の見方

- `cook` は、最初にフレーム時間を測定したあと、`--frames` で指定したフレーム数にわたって各 OP の CPU と GPU の cook 時間を平均します。単位は 1 フレームあたりのミリ秒です。
- ネットワークエディタで表示中の階層にある OP はビューアーの更新のために cook されるので、表示する階層によって測定結果が変動します。
- GPU 時間は目安です。TOP は GPU の処理が完了する前に cook を終了することがあり、その待ち時間が CPU 時間に含まれる場合があります。

## 仕組み

TD 側のスクリプトは、一時フォルダ内のリクエスト用フォルダを毎フレーム確認し、検出したリクエストをメインスレッドで実行して、結果を同じ場所に書き出します。`td.py` はリクエストを書き込んで結果を待ちます。1 回のリクエストは 1〜2 フレームで完了します。

- ポートを開かないので、ほかの端末からはアクセスできず、TD を複数起動しても競合しません。
- 出力では、一覧を省略したときは残りの件数を示し、パスやパラメータの名前を誤ったときはトレースバックの代わりに近い名前の候補を返します。
- 画像はブロックの平均で整数分の 1 に縮小するので、細い線やフィルムグレインが失われにくくなります。アルファチャンネルは破棄します。

## 注意事項

- リクエストの実行中は TD のフレームが停止します。フル HD の画像を 1 枚取得すると、60 fps では 1 フレーム分の時間の大半を消費します。本番中はブリッジを読み込まないでください。
- 変更系コマンドと `py` はプロジェクトの内容を書き換えます。調査では読み取り専用のコマンドだけを使用し、変更する前に所有者へ確認してください。
- ブリッジは TD のプロジェクトを保存しません。.toe のファイルを書き換えるのは `open --install` と `install FILE.toe` だけです。`py` で保存の処理を書いた場合を除けば、変更は保存されません。
- 同じユーザーの権限で動作するソフトウェアは、リクエスト用フォルダに書き込んで TD 内でコードを実行できます。`TOUCHDESIGNER_BRIDGE_DIR` でフォルダを変更する場合は、ほかのユーザーが書き込める場所を指定しないでください。

## 動作環境

Windows と macOS の TouchDesigner 2023 以降のビルドで、基本的に動作すると考えています。以前から存在する API だけを使用し、新しい属性がないビルドでは古い名前の属性を使用するためです。POP を扱うには 2025 以降のビルドが必要です。

動作を確認したのは次の環境です。

- TouchDesigner 2025.32460、Windows 11（WSL2 から操作）
- TouchDesigner 2025.33230、macOS
- TouchDesigner 2023.12600、Windows 11（WSL2 から操作）。OP の配置、`shot`、`tests/live.py` は確認していません。

## テスト

```bash
python3 -m unittest discover -s tests          # TD は不要です。tests/fake_td.py が TD を模擬します
python3 td.py open scratch.toe && python3 tests/live.py   # 実機の TD が必要です
```

`tests/live.py` は、1 つの COMP 内に 6、25、60 個の OP からなるネットワークをランダムに生成し、OP を追加して配置を崩してから整列します。各段階で、OP の重なり、右から左へ戻る配線、指示なく移動した OP、TD のエラーがないことを確認し、最後に COMP を削除します。

## 生成 AI の利用

このリポジトリのコード、テスト、文書は、コーディングエージェントの Claude Code（モデルは Claude Fable 5.2）が生成しました。作者は設計方針と要件を決定し、生成物を確認して修正を指示しています。動作は、動作環境の節に挙げたビルドの実機と `tests/` のテストで検証しました。

## ライセンス

MIT ライセンスで公開しています。全文は [LICENSE](LICENSE) に記載しています。
