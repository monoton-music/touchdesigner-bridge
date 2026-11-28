[日本語](README.ja.md)

# touchdesigner-bridge

A command-line tool for inspecting and editing a running TouchDesigner from an outside shell.

- **Made for**: agents that run commands, such as Claude Code, Codex CLI and Cursor, and shell scripts. A client without a shell, such as the chat in Claude Desktop, cannot use it
- **What it does**: launches TD, finds heavy OPs, lists errors, reads parameter and CHOP values, grabs the image of a TOP, builds networks
- **Output**: short text. Only non-default parameters are returned, and line counts are capped
- **Connection**: files in the temp folder. No port is opened
- **Requires**: Python 3 (standard library only) and TouchDesigner
- **Runs on**: Windows, macOS, WSL

## Getting started

### Leaving it to an agent

Give the agent the URL of this repository and ask for the work. The agent fetches the repository, launches TD, connects, and learns the commands from `td.py help` and `td.py guide` by itself.

```
Use https://github.com/monoton-music/touchdesigner-bridge to find out why show.toe is dropping frames
```

To have it used in every session of a project, put one line in that project's `AGENTS.md` or `CLAUDE.md`. Agents load those files at the start of a session, not the files of this repository.

```
For TouchDesigner work use touchdesigner-bridge (path/to/touchdesigner-bridge/td.py). Before the first use, run `python3 td.py guide` and follow it.
```

### Running it yourself

```bash
git clone https://github.com/monoton-music/touchdesigner-bridge
cd touchdesigner-bridge
python3 td.py open scratch.toe      # launches TD and connects; creates an empty project if the file is missing
python3 td.py err                   # errors and warnings
python3 td.py cook /project1        # the OPs that take longest to cook
python3 td.py help                  # every command
```

## Connecting

`td.py open FILE.toe` launches TD and returns once it is connected. It adds an Execute DAT that loads the bridge (`/local/touchdesigner_bridge`) to the .toe, so from then on the file connects just by being opened. The file is rewritten with `toeexpand` and `toecollapse`, which ship with TD, and the original is kept as `.bak`. For an existing .toe, add `--install`.

To leave the .toe untouched, paste one line into the textport of the running TD (Alt+T, Option+T on macOS). `python3 td.py setup` prints that line with the paths of your machine and copies it to the clipboard.

```python
exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
```

Choosing the TD:

- With several TDs open, pass part of the .toe name or the pid to `--to`, or set `TOUCHDESIGNER_BRIDGE_TO`. `td.py status` lists the TDs that respond.
- TouchDesigner is looked for in `C:\Program Files\Derivative` on Windows and `/Applications` on macOS. Elsewhere, set `TOUCHDESIGNER_BIN` to its bin folder (on macOS, `TouchDesigner.app/Contents/MacOS`).
- `open` uses the build the .toe was saved with, or a newer one when that is not installed. It never uses an older one: an older build drops what it does not know, such as every POP in a 2023 build, and saving loses it.

## Commands

`python3 td.py help` prints every command. These are the common ones.

Finding problems:

```bash
python3 td.py info                           # build, measured fps, skipped frames
python3 td.py err                            # errors and warnings; identical messages are grouped with a count
python3 td.py cook /project1 --n 20          # heaviest cook times first, and totals per COMP
python3 td.py mem /project1                  # largest memory use first
python3 td.py find / --expr 'absTime'        # OPs with an expression that contains absTime
```

Reading the network:

```bash
python3 td.py ls /project1 --depth 2 --type '*TOP'
python3 td.py op /project1/noise1            # type, connections, parameters, errors and cook in one
python3 td.py flow /project1                 # wiring inside a COMP as a -> b
python3 td.py par '/project1/noise*' seed    # non-default parameters only; names may use * ?
python3 td.py parinfo /project1/noise1 type  # label, type, menu values, range
python3 td.py chop /project1/null1           # current channel values
python3 td.py text /project1/script1         # contents of a DAT
python3 td.py geo /project1/sphere1          # point count, bounds and attributes of a SOP or POP
python3 td.py doc noiseTOP --grep seed       # the help bundled with TD
```

Looking at the picture and the motion:

```bash
python3 td.py top /project1/out1             # saves a PNG, at most 768 on the long side, and returns the path
python3 td.py top /project1/out1 --max 0 --crop 800,400,320,180    # one region at full size (x, y, width, height from the top left)
python3 td.py look /project1/out1            # the picture as numbers: tones, black and clipped area, hues
python3 td.py compare /project1/out1 /project1/lv1 opacity=0.5     # the same frame with and without a change, which is then put back
python3 td.py motion /project1/out1          # a contact sheet of consecutive frames, and how fast and where things move
python3 td.py trace /project1/cam ty rz frames=300                 # parameters over time: range, speed, jolts, reversals
python3 td.py record /project1/out1 --seconds 3 --file clip.mp4    # a few seconds as a movie, to watch the pace
python3 td.py shot /project1                 # a picture of the TD window, with /project1 framed in the network editor
```

Changing the patch (each command is one undo step):

```bash
python3 td.py set /project1/noise1 seed=3 tx='=absTime.seconds'
python3 td.py new /project1 levelTOP lv1 opacity=0.5
python3 td.py wire /project1/noise1 /project1/lv1
python3 td.py build /project1 - <<'END'      # create and wire several at once
n noiseTOP seed=4
lv levelTOP
out nullTOP
n -> lv
lv -> out
END
python3 td.py write /project1/shader - < shader.glsl   # replace the text of a DAT; a shader is compiled and its errors returned
python3 td.py layout /project1               # arrange the nodes along the wiring
python3 td.py note /project1/lv1 'lifts the blacks'    # a comment shown on the node
python3 td.py knob /project1 Gain=0.5@lv1.opacity      # a custom parameter on the COMP that drives lv1.opacity
python3 td.py rm /project1/lv1 --yes         # without --yes, only reports what would be deleted
python3 td.py undo
python3 td.py py 'result = [c.name for c in root.children]'   # any Python, for what no command does
```

What to know before use, such as the workflow, how TD behaves and examples of `build`, is in [GUIDE.md](GUIDE.md), which `python3 td.py guide` also prints.

## What the changing commands do

- `new`, `build`, `set` and `wire` cook what they touched and return its errors. TD finds no error in an OP that has not cooked, so `err` alone would call a freshly built network clean.
- `set` refuses a value TD will not take, or an expression that fails, and puts the parameter back.
- `build` is all or nothing. When one line fails, nothing it made is left behind.
- OPs made by `new` and `build` have their viewer on. Viewers cost cook time; turn them off with `td.py flag '/project1/*' viewer=0`.

## Where new nodes go

- `build` into an empty COMP arranges the nodes along the wiring, from left to right.
- An OP that another reads, through an expression or a setting such as a material, sits beside it as if wired into it, such as an LFO just left of the OP it drives.
- In a COMP that already holds others, new OPs go beside the OP they are wired to. Nothing that was there moves, except when an OP is put into an existing wire without room: then everything to its right moves over, and the reply says so.
- `layout` arranges a whole COMP again, so it refuses a COMP that holds OPs someone else placed. Pass `--yes` when the owner asked for it to be tidied.
- The bridge remembers what it placed only while TD runs.

## Waiting for a moment

Every command except `py` takes `--when 'EXPR'`. It runs in the first frame in which the Python expression is true, and is refused after `--wait` seconds (60 by default). A still depends on the frame it was taken in, and a `sleep` in the shell misses that frame.

```bash
python3 td.py top /project1/out1 --when 'absTime.seconds % 38 > 9'
python3 td.py wait --when 'op("/project1/timer1")["done"].eval() == 1'   # holds a shell script until then
```

## Where pictures are saved

- `top`, `motion`, `compare` and `record` save to the path given with `--file`. Without it, they save under `touchdesigner-bridge/<pid>/img` in the temp folder and return the path.
- That folder is removed after its TD has been gone for ten minutes. Save what you want to keep with `--file`.
- `shot` on macOS needs Screen Recording permission for the terminal. On Windows TD takes the picture itself, so it also works over SSH and from WSL.

## Reading cook

- `cook` measures the frame time first, then averages the CPU and GPU cook time of each OP over `--frames` frames, in milliseconds per frame.
- OPs in the network open in the editor cook for their viewers, so the result depends on what is open.
- GPU times are a rough guide. A TOP can finish cooking before its GPU work is done, and the wait can show up as CPU time.

## How it works

The script on the TD side checks a request folder under the temp folder once a frame, runs what it finds on the main thread and writes the result next to it. `td.py` writes a request and waits for the result, which takes one or two frames.

- No port is opened, so other machines cannot reach it, and several TDs do not collide.
- Loaded from the textport, it leaves nothing in the .toe.
- Output is kept short: only non-default parameters, capped lists with the count of what was cut, and near names instead of a traceback when a path or parameter is wrong.
- Pictures are shrunk by a whole factor, each pixel the mean of a block, so thin lines and grain survive. Alpha is dropped.

## Cautions

- A request stops TD's frames while it runs. Grabbing one full-HD image takes most of a frame at 60 fps. Do not load the bridge during a live show.
- The changing commands and `py` change the patch. When investigating, only read, and ask the owner before changing anything. The bridge never saves the project.
- Software running as your user can write to the request folder and run code inside TD. If you move the folder with `TOUCHDESIGNER_BRIDGE_DIR`, do not choose one other users can write to.

## Tested with

| TouchDesigner | OS |
|---|---|
| 2025.32460 | Windows 11 (from WSL2) |
| 2025.33230 | macOS |
| 2023.12600 | Windows 11 (from WSL2) |

- Node placement, `shot` and `tests/live.py` were run on the two 2025 builds only.
- Older builds were not checked.
- POPs need 2025 or later.
- `record` writes Motion JPEG where H.264 is not available.

## Tests

```bash
python3 -m unittest discover -s tests          # needs no TD; tests/fake_td.py stands in for it
python3 td.py open scratch.toe && python3 tests/live.py   # needs a real TD
```

`tests/live.py` builds random networks of 6, 25 and 60 OPs inside one COMP, adds to them, scatters and tidies them. After each step it checks that no node overlaps another, no wire runs right to left, nothing moved unasked and TD reports no error. It removes the COMP at the end.

## Use of generative AI

The code, tests and documents in this repository were generated by the coding agent Claude Code (model: Claude Fable 5.2). The author decided the design and the requirements, reviewed what was generated and directed its revisions. It was checked on the builds listed under Tested with, and by the tests in `tests/`.

## License

MIT. See [LICENSE](LICENSE).
