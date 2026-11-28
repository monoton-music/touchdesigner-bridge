[English](README.md)

# touchdesigner-bridge

実行中の TouchDesigner を、外部のシェルから調査、編集するコマンドラインツールです。

- **対象**: Claude Code、Codex CLI、Cursor のようにコマンドを実行できるエージェントと、シェルスクリプトです。Claude Desktop のチャットのようにシェルを持たないクライアントからは使用できません。
- **できること**: TD の起動、負荷の高い OP の特定、エラーの一覧表示、パラメータと CHOP の値の確認、TOP の画像の取得、ネットワークの構築ができます。
- **出力**: 短いテキストを返します。パラメータは既定値と異なるものだけを返し、行数には上限があります。
- **接続**: 一時フォルダのファイルでやり取りします。ポートは開きません。
- **必要なもの**: Python 3（標準ライブラリのみ）と TouchDesigner が必要です。
- **対応環境**: Windows、macOS、WSL で動作します。

## 始め方

### エージェントに任せる場合

このリポジトリの URL をエージェントに渡して、作業を依頼します。エージェントは、リポジトリの取得、TD の起動と接続を自分で行い、コマンドの使い方を `td.py help` と `td.py guide` で確認します。

```
https://github.com/monoton-music/touchdesigner-bridge を使って、show.toe で fps が落ちている原因を調べて
```

特定のプロジェクトで常に使用させる場合は、そのプロジェクトの `AGENTS.md` または `CLAUDE.md` に次の 1 行を追加します。エージェントがセッションの開始時に読み込むのはこれらのファイルで、このリポジトリのファイルは読み込みません。

```
TouchDesigner の作業には touchdesigner-bridge（path/to/touchdesigner-bridge/td.py）を使用する。最初に `python3 td.py guide` を実行し、その内容に従う。
```

### 自分で実行する場合

```bash
git clone https://github.com/monoton-music/touchdesigner-bridge
cd touchdesigner-bridge
python3 td.py open scratch.toe      # TD を起動して接続します。ファイルが無ければ空のプロジェクトを作成します
python3 td.py err                   # エラーと警告を表示します
python3 td.py cook /project1        # cook 時間の長い OP を表示します
python3 td.py help                  # 全てのコマンドを表示します
```

## 接続

`td.py open FILE.toe` は TD を起動し、接続できたら終了します。このとき、ブリッジを読み込む Execute DAT（`/local/touchdesigner_bridge`）を .toe に追加するので、以後はそのファイルを開くだけで接続できます。ファイルは TD に付属する `toeexpand` と `toecollapse` で書き換え、元のファイルは `.bak` として残します。既存の .toe には `--install` を付けます。

.toe に何も追加したくない場合は、実行中の TD のテキストポート（Alt+T、macOS では Option+T）に次の 1 行を貼り付けます。`python3 td.py setup` は、使用している環境のパスでこの 1 行を出力し、クリップボードにもコピーします。

```python
exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
```

接続する TD は次のように選びます。

- TD を複数起動している場合は、`--to` に .toe の名前の一部か pid を指定します。環境変数 `TOUCHDESIGNER_BRIDGE_TO` でも指定できます。応答している TD は `td.py status` で一覧表示できます。
- TouchDesigner は、Windows では `C:\Program Files\Derivative`、macOS では `/Applications` から探します。別の場所にある場合は、`TOUCHDESIGNER_BIN` に bin フォルダを指定します（macOS では `TouchDesigner.app/Contents/MacOS`）。
- `open` は、.toe を保存したビルドで開き、そのビルドがなければより新しいビルドで開きます。古いビルドでは開きません。古いビルドは知らない要素を読み飛ばし（2023 のビルドでは POP の全て）、そのまま保存すると失われるためです。

## コマンド

全てのコマンドは `python3 td.py help` で表示できます。ここでは、よく使うものを挙げます。

問題を探すとき:

```bash
python3 td.py info                           # ビルド、実測の fps、スキップされたフレーム数
python3 td.py err                            # エラーと警告。同じ内容は 1 行にまとめて件数を表示します
python3 td.py cook /project1 --n 20          # cook 時間の長い順の一覧と、COMP ごとの合計
python3 td.py mem /project1                  # メモリ使用量の多い順の一覧
python3 td.py find / --expr 'absTime'        # 式に absTime を含む OP を検索します
```

ネットワークを読むとき:

```bash
python3 td.py ls /project1 --depth 2 --type '*TOP'
python3 td.py op /project1/noise1            # 型、接続、パラメータ、エラー、cook 時間をまとめて表示します
python3 td.py flow /project1                 # COMP 内の配線を a -> b の形式で表示します
python3 td.py par '/project1/noise*' seed    # 既定値と異なるパラメータだけを表示します。名前に * と ? を使用できます
python3 td.py parinfo /project1/noise1 type  # ラベル、型、メニューの値、範囲
python3 td.py chop /project1/null1           # チャンネルの現在の値
python3 td.py text /project1/script1         # DAT の内容
python3 td.py geo /project1/sphere1          # SOP と POP の点の数、範囲、アトリビュート
python3 td.py doc noiseTOP --grep seed       # TD に付属するヘルプを検索します
```

画像と動きを確認するとき:

```bash
python3 td.py top /project1/out1             # 長辺 768 以下の PNG を保存し、パスを返します
python3 td.py top /project1/out1 --max 0 --crop 800,400,320,180    # 指定した領域を等倍で保存します（左上からの x、y、幅、高さ）
python3 td.py look /project1/out1            # 画像を数値で示します。階調、黒つぶれと白とびの面積、色相
python3 td.py compare /project1/out1 /project1/lv1 opacity=0.5     # 同じフレームで変更の前後を比較し、変更は元に戻します
python3 td.py motion /project1/out1          # 連続フレームの一覧画像と、動きの速さと位置
python3 td.py trace /project1/cam ty rz frames=300                 # パラメータの時間変化。範囲、速度、急な変化、反転
python3 td.py record /project1/out1 --seconds 3 --file clip.mp4    # 数秒を動画に書き出し、再生したときのテンポを確認します
python3 td.py shot /project1                 # /project1 をネットワークエディタに表示して、TD のウィンドウを画像に保存します
```

ネットワークを変更するとき（どのコマンドも 1 回のアンドゥで取り消せます）:

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
python3 td.py write /project1/shader - < shader.glsl   # DAT のテキストを置き換えます。シェーダーはコンパイルしてエラーを返します
python3 td.py layout /project1               # ノードを配線に沿って整列します
python3 td.py note /project1/lv1 '黒レベルを上げる'    # ノードに表示されるコメントを付けます
python3 td.py knob /project1 Gain=0.5@lv1.opacity      # COMP にカスタムパラメータを追加し、lv1.opacity に連動させます
python3 td.py rm /project1/lv1 --yes         # --yes を付けない場合は、削除される OP を表示するだけです
python3 td.py undo
python3 td.py py 'result = [c.name for c in root.children]'   # 任意の Python を実行します。コマンドにない操作に使います
```

作業の進め方、TD の動作、`build` の例は [GUIDE.md](GUIDE.md) にまとめてあり、`python3 td.py guide` でも表示できます。

## 変更用のコマンドの動作

- `new`、`build`、`set`、`wire` は、操作した OP を cook し、そのエラーを返答に含めます。TD は cook していない OP のエラーを検出しないので、作成した直後に `err` だけを実行してもエラーは表示されません。
- `set` は、TD が受け付けない値や評価に失敗する式を拒否し、パラメータを元に戻します。
- `build` は全てを実行するか、何も実行しないかのどちらかです。1 行でも失敗すると、作成した OP を残しません。
- `new` と `build` で作成した OP はビューアーが有効になります。ビューアーは cook 時間を消費するので、負荷が高い場合は `td.py flag '/project1/*' viewer=0` で無効にします。

## ノードの配置

- 空の COMP に `build` を実行すると、ノードを配線に沿って左から右へ整列します。
- 式やマテリアルの設定で参照される OP は、参照する OP へ配線されているものとして隣に配置します。たとえば LFO は、それで動かす OP のすぐ左に置きます。
- 既に OP がある COMP では、追加した OP を配線先の OP の隣に配置し、既存の OP は移動しません。例外は既存の配線の途中に挿入して間隔が足りない場合で、右側の OP をまとめて右へ移動し、そのことを返答に記載します。
- `layout` は COMP 全体を配置し直すので、ほかの人が配置した OP がある COMP では実行を拒否します。持ち主から整理を依頼された場合は `--yes` を付けます。
- ブリッジが自分で配置した OP を記録しているのは、TD が起動している間だけです。

## タイミングを指定する

`py` 以外のコマンドには `--when 'EXPR'` を指定できます。Python の式が真になった最初のフレームでコマンドを実行し、`--wait` の秒数（既定値は 60）が過ぎても真にならなければ実行を拒否します。静止画に何が映るかは取得したフレームで決まり、シェルの `sleep` ではそのフレームに合わせられないためです。

```bash
python3 td.py top /project1/out1 --when 'absTime.seconds % 38 > 9'
python3 td.py wait --when 'op("/project1/timer1")["done"].eval() == 1'   # その状態になるまでシェルスクリプトを待たせます
```

## 画像の保存先

- `top`、`motion`、`compare`、`record` は、`--file` で指定したパスに保存します。指定しない場合は一時フォルダの `touchdesigner-bridge/<pid>/img` に保存し、パスを返します。
- このフォルダは、その TD が終了してから 10 分後以降に削除されます。残したい画像は `--file` で保存してください。
- macOS で `shot` を使うには、ターミナルに画面収録の権限が必要です。Windows では TD が自身のウィンドウを撮影するので、SSH や WSL からも実行できます。

## cook 時間の読み方

- `cook` は、最初にフレーム時間を測定し、そのあと `--frames` のフレーム数で各 OP の CPU と GPU の cook 時間を平均します。単位は 1 フレームあたりのミリ秒です。
- ネットワークエディタで表示している階層の OP はビューアーのために cook されるので、表示している階層によって結果が変わります。
- GPU の時間は目安です。TOP は GPU の処理が終わる前に cook を終えることがあり、その待ち時間が CPU の時間に含まれることがあります。

## 仕組み

TD 側のスクリプトは、一時フォルダのリクエスト用のフォルダを毎フレーム確認し、見つけたリクエストをメインスレッドで実行して、結果を同じ場所に書き出します。`td.py` はリクエストを書き込んで結果を待ちます。1 回の往復は 1〜2 フレームで完了します。

- ポートを開かないので、ほかの端末からはアクセスできず、TD を複数起動しても衝突しません。
- テキストポートから読み込んだ場合は、.toe に何も残りません。
- 出力は短く保ちます。既定値と異なるパラメータだけを返し、一覧を省略したときは残りの件数を示し、パスやパラメータの名前を誤ったときはトレースバックではなく近い名前を返します。
- 画像は整数分の 1 に縮小し、各画素は元の画素のブロックの平均にするので、細い線やフィルムグレインが消えません。アルファチャンネルは破棄します。

## 注意事項

- リクエストを実行している間は TD のフレームが止まります。フル HD の画像を 1 枚取得すると、60 fps では 1 フレームの大半を使います。本番中はブリッジを読み込まないでください。
- 変更用のコマンドと `py` はプロジェクトを変更します。調査では読み取りのコマンドだけを使い、変更する前にプロジェクトの所有者に確認してください。ブリッジがプロジェクトを保存することはありません。
- 同じユーザーの権限で動くソフトウェアは、リクエスト用のフォルダに書き込んで TD の中でコードを実行できます。`TOUCHDESIGNER_BRIDGE_DIR` でフォルダを変更する場合は、他のユーザーが書き込める場所を指定しないでください。

## 動作環境

| TouchDesigner | OS |
|---|---|
| 2025.32460 | Windows 11（WSL2 から操作） |
| 2025.33230 | macOS |
| 2023.12600 | Windows 11（WSL2 から操作） |

- ノードの配置、`shot`、`tests/live.py` は、2025 の 2 つのビルドでだけ実行しました。
- これより古いビルドでは確認していません。
- POP を扱うには 2025 以降のビルドが必要です。
- `record` は、H.264 で書き出せない環境では Motion JPEG で書き出します。

## テスト

```bash
python3 -m unittest discover -s tests          # TD は不要です。tests/fake_td.py が TD の代わりをします
python3 td.py open scratch.toe && python3 tests/live.py   # 実機の TD が必要です
```

`tests/live.py` は、1 つの COMP の中に OP の数が 6、25、60 のネットワークを乱数で作成し、OP を追加し、位置を散らしてから整列します。各段階で、ノードが重なっていないこと、右から左へ戻る配線がないこと、指示なしに移動した OP がないこと、TD がエラーを報告していないことを確認し、最後に COMP を削除します。

## 生成 AI の利用

このリポジトリのコード、テスト、文書は、コーディングエージェントの Claude Code（モデルは Claude Fable 5.2）が生成しました。作者は設計の方針と要件を決め、生成されたものを確認して修正を指示しています。動作は、動作環境の節に挙げたビルドの実機と、`tests/` のテストで確認しました。

## ライセンス

MIT ライセンスで公開しています。全文は [LICENSE](LICENSE) に記載しています。
