[English](README.md)

# touchdesigner-bridge

実行中の TouchDesigner を、外部のシェルから調査、編集するコマンドラインツールである。

- **対象**: Claude Code、Codex CLI、Cursor のようにコマンドを実行できるエージェントと、シェルスクリプト
- **できること**: TD の起動、負荷の高い OP の特定、エラーの一覧表示、パラメータと CHOP の値の確認、TOP の画像の取得、ネットワークの構築
- **出力**: 短いテキスト。既定値と異なるパラメータだけを返し、行数に上限がある
- **接続**: TD とは一時フォルダのファイルでやり取りする。ポートは開かない
- **必要なもの**: Python 3（標準ライブラリのみ）と TouchDesigner
- **対応環境**: Windows、macOS、WSL

## 始め方

### エージェントに任せる場合

このリポジトリの URL をエージェントに渡して、作業を依頼する。エージェントは、リポジトリの取得、TD の起動と接続、コマンドの確認（`td.py help`、`td.py guide`）を自分で行う。

```
https://github.com/monoton-music/touchdesigner-bridge を使って、show.toe で fps が落ちている原因を調べて
```

### 自分で実行する場合

```bash
git clone https://github.com/monoton-music/touchdesigner-bridge
cd touchdesigner-bridge
python3 td.py open scratch.toe      # TD を起動して接続する。ファイルが無ければ空のプロジェクトを作成する
python3 td.py info                  # ビルド、実測の fps、スキップされたフレーム数
python3 td.py err                   # エラーと警告
python3 td.py cook /project1        # cook 時間の長い OP
python3 td.py top /project1/out1    # TOP の画像を PNG で保存する
python3 td.py help                  # 全てのコマンド
```

以降は詳細である。

## 仕組み

TD 側のスクリプトは、一時フォルダの `touchdesigner-bridge/<pid>/in` に置かれたリクエストを毎フレーム確認し、メインスレッドで実行して、結果を `out` に書き出す。外部のコマンド `td.py` は、リクエストのファイルを作成して結果を待つ。1 回の往復は 1〜2 フレームで完了する。

- 通信にファイルを使用するので、ポートを開く必要がなく、ネットワーク上の他の端末からはアクセスできない。
- テキストポートから読み込んだ場合はノードを作成しないので、.toe には何も残らず、TD を終了すれば消える。
- Windows と macOS で動作し、WSL からも `/mnt/c` を経由してそのまま使用できる。

エージェントが消費するトークンを抑えているのは出力の絞り方であり、通信方式ではない。各コマンドは結果を TD 側で整形し、既定値と異なるパラメータだけを返し、行数に上限を設けている。

## コマンド

TD の起動から接続までを 1 つのコマンドで実行できる。

```bash
python3 td.py open scratch.toe               # ファイルが無ければ空のプロジェクトを作成して開き、接続できたら終了する
python3 td.py open show.toe --install        # 既存の .toe に読み込み用の DAT を追加してから開く
```

`open` は、プロジェクトの開始時にブリッジを読み込む Execute DAT（`/local/touchdesigner_bridge`）を .toe に追加する。TD に付属する `toeexpand` と `toecollapse` でファイルを直接書き換えるので、TD を事前に起動しておく必要はない。元のファイルは `.bak` として残す。この DAT を追加した .toe は、以後は通常どおり開くだけで接続できる。

.toe に何も追加したくない場合は、実行中の TD のテキストポート（Alt+T、macOS では Option+T）に次の 1 行を貼り付ける。この 1 行は `python3 td.py setup` で出力できる。`setup` は使用している環境のパスで 1 行を生成し、クリップボードにもコピーする。

```python
exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
```

接続先と TouchDesigner の場所は、次のように指定する。

- TD を複数起動している場合は、`--to` に .toe の名前の一部か pid を指定する。環境変数 `TOUCHDESIGNER_BRIDGE_TO` でも指定できる。
- `python3 td.py status` で、応答している TD の一覧を表示できる。
- TouchDesigner は既定のインストール先から探す（Windows は `C:\Program Files\Derivative`、macOS は `/Applications`）。複数のビルドがある場合は、最も新しいものを選ぶ。
- インストール先が異なる場合や、特定のビルドを使いたい場合は、`TOUCHDESIGNER_BIN` に bin フォルダを指定する。macOS ではアプリの中の `Contents/MacOS` である（例: `/Applications/TouchDesigner.app/Contents/MacOS`）。

必要なものは Python の標準ライブラリだけである。調査には次のコマンドを使用する。

```bash
python3 td.py info                           # ビルド、実測の fps、スキップされたフレーム数、開いているネットワーク
python3 td.py cook /project1 --n 20          # cook 時間の長い順の一覧と、COMP ごとの合計
python3 td.py mem /project1                  # メモリ使用量の多い順の一覧
python3 td.py err                            # エラーと警告。同じ内容は 1 行にまとめて件数を表示する
python3 td.py ls /project1 --depth 2 --type '*TOP'
python3 td.py find / --expr 'absTime'        # 式に absTime を含むパラメータを持つ OP を検索する
python3 td.py op /project1/noise1            # 型、位置、接続、パラメータ、エラー、cook 時間をまとめて表示する
python3 td.py flow /project1                 # COMP 内の配線を a -> b の形式で表示する
python3 td.py par '/project1/noise*' seed    # 既定値と異なるパラメータだけを表示する。名前に * と ? を使用できる
python3 td.py parinfo /project1/noise1 type  # ラベル、型、メニューの値、範囲を表示する
python3 td.py chop /project1/null1
python3 td.py top /project1/out1             # 長辺が 768 以下になるように整数分の 1 に縮小した PNG を保存し、パスを返す
python3 td.py top /project1/out1 --max 0 --crop 800,400,320,180    # 指定した領域を等倍で保存する。数値は左上からの x、y、幅、高さである
python3 td.py top /project1/out1 --when 'absTime.seconds % 38 > 9'  # 式が真になった最初のフレームで画像を取得する
python3 td.py wait --when 'op("/project1/timer1")["done"].eval() == 1'   # プロジェクトが指定の状態になるまでシェルスクリプトを待たせる
python3 td.py compare /project1/out1 /project1/lv1 opacity=0.5           # 同じフレームで、変更の前後を比べる。変更は元に戻す
python3 td.py compare /project1/out1 /project1/shader --swap 'OLD' --with 'NEW'   # DAT のテキストを置き換えて比べる
python3 td.py record /project1/out1 --seconds 3 --file clip.mp4    # 数秒を動画に書き出す
python3 td.py doc noiseTOP --grep seed       # TD に付属するヘルプを検索する
python3 td.py py 'result = [c.name for c in root.children]'
```

ネットワークを変更するコマンドもある。どのコマンドも、1 回のアンドゥで取り消せる。

```bash
python3 td.py set /project1/noise1 seed=3 tx='=absTime.seconds'
python3 td.py new /project1 levelTOP lv1 opacity=0.5
python3 td.py wire /project1/noise1 /project1/lv1
python3 td.py build /project1 - <<'END'      # 複数の OP を一度に作成して接続する
n noiseTOP seed=4
lv levelTOP
out nullTOP
n -> lv
lv -> out
END
python3 td.py layout /project1               # ノードを配線に沿って整列する
python3 td.py note /project1/lv1 '黒レベルを上げる'            # ノードに表示されるコメントを付ける
python3 td.py box /project1 'Source' n lv                    # 指定した OP を整列し、タイトル付きの枠で囲む
python3 td.py look /project1/out                               # 画像を数値で確認する。階調、黒つぶれと白とびの面積、色相を表示する
python3 td.py overlaps /project1                                # ネットワークエディタ上で重なっている OP を一覧表示する
python3 td.py trace /project1/cam ty rz frames=300              # パラメータを時系列で記録する。範囲、速度、急な変化、反転の回数を表示する
python3 td.py motion /project1/out                             # 動きを確認する。1 ステップの変化量、流れの速度と方向、連続フレームの一覧画像を出力する
python3 td.py motion /project1/out --crop 720,360,480,360      # 指定した領域だけを等倍で確認する
python3 td.py motion /project1/out --follow '(op("/project1/pos")["x"].eval(), op("/project1/pos")["y"].eval())'   # 動く対象を一覧画像の中央に保つ
python3 td.py knob /project1 Gain=0.5@lv1.opacity              # COMP にカスタムパラメータを追加し、lv1.opacity に連動させる
python3 td.py rm /project1/lv1 --yes         # --yes を付けない場合は、削除される数を表示するだけで削除しない
python3 td.py rm '/project1/tmp*' --yes      # 一致するものを全て削除する。--yes を付けない場合は一覧を表示する
python3 td.py undo
```

`new` と `build` で作成した OP は、ノードのビューアーが有効になるので、ネットワークを表示すれば各ノードの出力を確認できる。空の COMP に対して `build` を実行した場合は、ノードを配線に沿って整列する。ビューアーは cook 時間を消費するので、負荷が高くなった場合は `python3 td.py flag '/project1/*' viewer=0` で無効にする。

`py` 以外のコマンドには `--when 'EXPR'` を指定できる。

- ブリッジはコマンドを TD の中で保留し、Python の式を毎フレーム評価して、式が真になった最初のフレームでコマンドを実行する。
- `--wait` で指定した秒数（既定値は 60）が経過しても真にならなければ、コマンドを実行せずにエラーを返す。
- `wait` は待つことだけを行うコマンドで、シェルスクリプトを待たせるために使用する。
- `motion` の `--follow` には、中央に保つ対象の画素位置（左上からの x と y）を返す式を指定する。カメラとシーン内の点からこの画素位置を計算する方法は GUIDE.md に記載している。

画像と動画の保存先は、次のとおりである。

- `top`、`motion`、`compare`、`record` は、`--file` でパスを指定するとそこに保存する。
- 指定しない場合は、一時フォルダの `touchdesigner-bridge/<pid>/img` に保存し、パスを返す。
- 一時フォルダの中身は、TD の終了時には消えない。終了してから 10 分以上経った TD のフォルダは、次にいずれかの TD がブリッジを読み込んだときに削除する。残したい画像は `--file` で別の場所に保存する。
- `record` は、録画の間だけ Movie File Out TOP を作成し、終わったら削除する。

全てのコマンドの一覧は `python3 td.py help` で表示できる。作業の進め方、TD の動作、ネットワークの構築例は [GUIDE.md](GUIDE.md) にまとめてあり、`python3 td.py guide` でも表示できる。どのコマンドでも、`--limit N` で行数を、`--timeout S` で待ち時間を変更できる。

## 設計の考え方

### シェルから使う

- Claude Code、Codex CLI、Cursor のようにコマンドを実行できるエージェントと、シェルスクリプトを対象にしている。
- 各コマンドは、テキストを出力して終了コードを返す通常のプロセスである。シェルのパイプ、ヒアドキュメント、スクリプトと組み合わせて使用できる。
  - `build` は構成を標準入力から読み取る。
  - `wait --when` は、プロジェクトが指定の状態になるまでスクリプトを待たせる。
- シェルを持たないクライアント（Claude Desktop のチャットのように、登録されたツールしか呼び出せないクライアント）からは使用できない。

### TD とはファイルでやり取りする

- TD に要求するものは、毎フレーム実行されるスクリプト 1 つだけである。
- ポートを開かないので、ポート番号の設定が不要で、TD を複数起動しても番号が衝突しない。
- リクエストを置くフォルダは、TD のプロセスごとに分かれている。
- テキストポートから読み込めば、.toe に何も追加せずに使用できる。
- フォルダを確認するのは 1 フレームに 1 回なので、リクエストは次のフレームまで待たされる。

### 固定のコマンドと `py`

| | 用途 | 例 |
|---|---|---|
| 固定のコマンド | 繰り返し発生する調査と配線。整形した短い結果を返し、同じ Python を毎回書いて読む作業を省略する。 | `op`、`cook`、`err`、`build` |
| `py` | TD の中で任意の Python を実行する。固定のコマンドにない操作はこれで行う。 | |

固定のコマンドで TD の全ての機能に対応することは目指していない。

### 画像は TD の中でタイミングを指定して撮る

- 静止画に何が映るかは、取得したフレームで決まる。シェルの `sleep` で時刻を合わせると、往復にかかる時間以上のずれが生じる。
- `--when` は TD の中で条件を待ち、条件が真になったフレームでコマンドを実行する。

### 動きは連続フレーム、数値、動画で確かめる

| コマンド | 内容 |
|---|---|
| `motion` | 連続フレームの一覧画像を保存し、フレーム間の流れを測定する。 |
| `motion --follow` | 動く対象を一覧画像の中央に保つ。 |
| `trace` | パラメータを毎フレーム記録する。 |
| `record` | 数秒を動画ファイルに書き出す。 |

- 一覧画像と数値でわかること: 急な変化、物体の反転、対象が画面の外に出たこと。
- 一覧画像と数値ではわからないこと: コマとコマの間に起きたことと、再生したときのテンポ。これは `record` で書き出した動画を再生して確かめる。

### 変更の効果は同じフレームで比べる

- `compare` は、変更を加える前と後の画像を同じフレームで取得し、変更を元に戻す。パラメータの値と、DAT のテキストの置き換えに対応している。
- 時刻が違う 2 枚を比べると、動いている場面では変更の差と場面の差を区別できない。
- 結果は、両方の画像の統計、差がある面積と位置、並べた画像で返す。差がなければ、その旨を返す。

### 制約

- リクエストは TD のメインスレッドで実行する（TD のオブジェクトにはメインスレッドからしかアクセスできない）。時間のかかるリクエストを実行している間は、フレームが停止する。フル HD の TOP の画像を 1 枚取得する処理は、60 fps では 1 フレームの大半を占める。
- リクエストを置くフォルダは、ユーザーアカウントの権限だけで保護されている。同じユーザーの権限で動作するソフトウェアは、リクエストを置いて TD の中でコードを実行できる。

## 出力の方針

- 各コマンドは、既定では必要な情報だけを返す。パラメータは既定値と異なるものだけを返し、一覧には上限を設け、省略した場合は残りの件数と絞り込みの方法を示す。
- 関連する操作は 1 つのコマンドにまとめている。`op` は 1 つの OP について必要な情報を 1 回で返し、`build` は複数の OP の作成と接続を 1 回で実行する。
- 知っておくべき注意は、その場面でコマンドが `note` の行として返す。たとえば `new` と `build` は、設定が必要な型を作成したときにその設定を示し、`top` は、画像が真っ黒、真っ白、または不正な値を含むときにそれを示す。事前に読ませる文書を減らすためである。助言であり、コマンドの動作は変わらない。
- 呼び出し方に誤りがあった場合は、トレースバックではなく修正の方法を返す。存在しないパスに対しては実在する近い名前を、存在しないパラメータに対しては近いパラメータ名を示す。
- 画像は整数分の 1 に縮小して保存し、各画素は元の画素のブロックの平均である。4K の TOP をそのまま読み込むと処理に時間がかかり、画素を間引いて縮小すると点や細い線が消える。倍率を整数にしているのは、1920 を 768 に縮小する 2.5 分の 1 のような端数の倍率では、平均する元の画素の数が位置によって変わり、フィルムグレインや 1〜2 画素幅の模様にむらが出ることがあるためである。アルファチャンネルは破棄し、黒の背景に合成した画像を保存する。

## cook 時間の測定

`cook` は、最初に 20 フレームのフレーム時間を測定し、そのあと `--frames` で指定したフレーム数にわたって、cook された OP の `cpuCookTime` と `gpuCookTime` を合計する。表の値は 1 フレームあたりの平均（ミリ秒）で、cook されなかったフレームは 0 として計算する。COMP ごとの合計は、内部の OP の値を加算して求める。`childrenCPUCookTime` は最後に参照された時点の値のまま更新されないので、使用しない。

ネットワークエディタで表示している階層の OP は、ビューアーの描画のために cook される。そのため、測定時に表示している階層によって結果が変化する。

集計中は全ての OP を毎フレーム走査するので、その間のフレーム時間は長くなる。出力の最初の行に表示するフレーム時間は、走査を始める前に測定した値である。

GPU の時間は目安として扱う。TOP は GPU の処理が完了する前に cook を終了するので、GPU の待ち時間が CPU の時間に含まれて表示されることがある。

## 注意事項

- リクエストは TD のメインスレッドで実行される。時間のかかる処理を送信すると、その間はフレームが停止するので、本番中はブリッジを読み込まない。
- 変更用のコマンドと `py` は、プロジェクトを変更できる。調査では読み取りのコマンドだけを使用し、変更する場合は事前にプロジェクトの所有者に確認する。ブリッジがプロジェクトを保存することはない。
- TD 自身が使用する `/ui` と `/sys` は対象から除外している。
- 一時フォルダに書き込めるソフトウェア、つまり同じユーザーの権限で動作するソフトウェアは、TD の中で任意のコードを実行できる。
- タイムラインを停止していても応答する。遅延実行の基準を `op.TDResources` にしているためである。

## 動作環境

次のビルドの実機で、「コマンド」に挙げたコマンドを実行して動作を確認した。

| TouchDesigner | OS |
|---|---|
| 2025.32460 | Windows 11（WSL2 から操作） |
| 2025.33230 | macOS |
| 2023.12600 | Windows 11（WSL2 から操作） |

- これより古いビルドでは確認していない。新しい属性がないビルドでは古い名前の属性を使用するようにしてあり、この切り替えは TD の代役を使ったテストで確認している。
- POP を扱う操作には、POP のある 2025 以降のビルドが必要である。
- `record` は、H.264 で書き出せないビルドや環境では Motion JPEG で書き出す。
- `open` と `install FILE.toe` は、TouchDesigner の実行ファイルと同じフォルダにある `toeexpand` と `toecollapse` を使用する。

## テスト

```bash
python3 -m unittest discover -s tests
```

`tests/fake_td.py` が TD の代わりとして動作する。API の名前と意味は、TD 2025.32460 に付属する Python のスタブ（`bin/Lib/tdi`）と、付属のオフラインヘルプで確認した。

特定のビルドに依存しないように、以前から存在する API だけを使用し、新しい属性が存在しないビルドでは古い名前を使用する。

## 生成 AI の利用

- 生成 AI が書いたもの: コード、テスト、文書。コーディングエージェントの Claude Code（モデルは Claude Fable 5.2）が生成した。
- 作者が行ったこと: 設計の方針と要件を決め、生成されたものへの修正を指示した。
- 確認の方法: 「動作環境」に挙げたビルドの実機での動作と、`tests/` のテスト。

## ライセンス

MIT ライセンスで公開する。全文は [LICENSE](LICENSE) に記載している。
