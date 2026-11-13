[日本語](README.ja.md)

# touchdesigner-bridge

A command-line tool for inspecting and editing a running TouchDesigner from an outside shell.

- **Made for**: agents that run commands, such as Claude Code, Codex CLI and Cursor, and shell scripts
- **What it does**: launches TD, finds heavy OPs, lists errors, reads parameter and CHOP values, grabs the image of a TOP, builds networks
- **Output**: short text. Only non-default parameters are returned, and line counts are capped
- **Connection**: it talks to TD through files in the temp folder. No port is opened
- **Requires**: Python 3 (standard library only) and TouchDesigner
- **Runs on**: Windows, macOS, WSL

## Getting started

### Leaving it to an agent

Give the agent the URL of this repository and ask for the work. The agent fetches the repository, launches TD and connects, and reads the commands (`td.py help`, `td.py guide`) by itself.

```
Use https://github.com/monoton-music/touchdesigner-bridge to find out why show.toe is dropping frames
```

### Running it yourself

```bash
git clone https://github.com/monoton-music/touchdesigner-bridge
cd touchdesigner-bridge
python3 td.py open scratch.toe      # launches TD and connects; creates an empty project if the file is missing
python3 td.py info                  # build, measured fps, skipped frames
python3 td.py err                   # errors and warnings
python3 td.py cook /project1        # the OPs that take longest to cook
python3 td.py top /project1/out1    # saves the image of a TOP as a PNG
python3 td.py help                  # every command
```

The rest of this document is detail.

## How it works

Every frame, the script on the TD side looks for requests placed in `touchdesigner-bridge/<pid>/in` under the temp folder, runs them on the main thread and writes the results to `out`. The outside command `td.py` places a request file and waits for the result. A round trip takes one or two frames.

- It opens no port. Other machines on the network cannot reach it.
- Loaded from the textport, it creates no nodes. Nothing is left in the .toe, and it is gone when TD closes.
- It runs on Windows and macOS, and also works as is from WSL through `/mnt/c`.

The transport has nothing to do with the amount of tokens. What reduces tokens is how the output is narrowed: common inspections are made into commands and formatted on the TD side, only non-default parameters are returned, line counts are capped, and so on.

## Commands

One command takes you from launching TD to being connected.

```bash
python3 td.py open scratch.toe               # creates an empty project if missing and opens it; returns once connected
python3 td.py open show.toe --install        # adds the loader DAT to an existing .toe, then opens it
```

`open` puts an Execute DAT that loads the bridge when the project starts (`/local/touchdesigner_bridge`) into the .toe. It rewrites the file directly with `toeexpand` and `toecollapse`, which ship with TD, so TD does not have to be running first. The original file is kept as `.bak`. From then on, a .toe that contains this DAT connects just by being opened normally.

If you do not want to put anything in the .toe, paste one line into the textport (Alt+T, Option+T on macOS) of the running TD. `python3 td.py setup` prints that line (written with the paths of your environment, and also copied to the clipboard).

```python
exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
```

The target and the location of TouchDesigner are chosen as follows.

- With several TDs open, pass part of the .toe name or the pid to `--to`. The `TOUCHDESIGNER_BRIDGE_TO` environment variable also works.
- `python3 td.py status` lists the TDs that are responding.
- TouchDesigner is looked for in the default location (`C:\Program Files\Derivative` on Windows, `/Applications` on macOS). When several builds are installed, the newest is chosen.
- If it is somewhere else, or you want a particular build, set `TOUCHDESIGNER_BIN` to its bin folder. On macOS that is `Contents/MacOS` inside the app (for example `/Applications/TouchDesigner.app/Contents/MacOS`).

Only the Python standard library is required.

```bash
python3 td.py info                           # build, measured fps, skipped frames, open networks
python3 td.py cook /project1 --n 20          # heaviest cook times first, and totals per COMP
python3 td.py mem /project1                  # largest memory use first
python3 td.py err                            # errors and warnings; identical messages are grouped with a count
python3 td.py ls /project1 --depth 2 --type '*TOP'
python3 td.py find / --expr 'absTime'        # OPs with a parameter whose expression contains absTime
python3 td.py op /project1/noise1            # type, position, connections, parameters, errors and cook in one
python3 td.py flow /project1                 # wiring inside a COMP as a -> b
python3 td.py par '/project1/noise*' seed    # non-default parameters only; names may use * ?
python3 td.py parinfo /project1/noise1 type  # label, type, menu values, range
python3 td.py chop /project1/null1
python3 td.py top /project1/out1             # saves a PNG shrunk by a whole factor to at most 768 on the long side and returns the path
python3 td.py top /project1/out1 --max 0 --crop 800,400,320,180    # one region with every pixel (x, y, width, height from the top left)
python3 td.py top /project1/out1 --when 'absTime.seconds % 38 > 9'  # taken in the first frame in which the expression is true
python3 td.py wait --when 'op("/project1/timer1")["done"].eval() == 1'   # holds a shell script until the project reaches a moment
python3 td.py compare /project1/out1 /project1/lv1 opacity=0.5           # the same frame with and without a change; the change is put back
python3 td.py compare /project1/out1 /project1/shader --swap 'OLD' --with 'NEW'   # the same, replacing text in a DAT
python3 td.py record /project1/out1 --seconds 3 --file clip.mp4    # writes a few seconds to a movie file
python3 td.py doc noiseTOP --grep seed       # looks up the help bundled with TD
python3 td.py py 'result = [c.name for c in root.children]'
```

There are also commands that change the patch. Each one is a single undo step.

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
python3 td.py layout /project1               # arrange the nodes along the wiring
python3 td.py look /project1/out                               # the picture as numbers: tones, black and clipped area, hues
python3 td.py overlaps /project1                                # OPs lying on top of each other in the network editor
python3 td.py trace /project1/cam ty rz frames=300              # follow parameters over time: range, speed, jolts, reversals
python3 td.py motion /project1/out                             # how it moves: change per step, flow speed and direction, a contact sheet
python3 td.py motion /project1/out --crop 720,360,480,360      # the same for one region at full size
python3 td.py motion /project1/out --follow '(op("/project1/pos")["x"].eval(), op("/project1/pos")["y"].eval())'   # keeps a moving object centred
python3 td.py note /project1/lv1 'lifts the blacks'            # comment shown on the node
python3 td.py box /project1 'Source' n lv                    # arrange these and draw a titled box around them
python3 td.py knob /project1 Gain=0.5@lv1.opacity              # custom parameter on the COMP that drives lv1.opacity
python3 td.py rm /project1/lv1 --yes         # without --yes, only reports how many would be deleted
python3 td.py rm '/project1/tmp*' --yes      # every match; without --yes, lists them
python3 td.py undo
```

OPs made by `new` and `build` have their node viewer on, so the network shows what each node makes. `build` into an empty COMP also arranges the nodes along the wiring. Viewers cost cook time; when a network gets heavy, turn them off with `python3 td.py flag '/project1/*' viewer=0`.

Every command except `py` takes `--when 'EXPR'`.

- The bridge holds the command inside TD, evaluates the Python expression once a frame, and runs the command in the first frame in which it is true.
- If that does not happen within `--wait` seconds (60 by default), the command is refused.
- `wait` is a command that does nothing else, for holding a shell script.
- `--follow` of `motion` takes an expression that returns the pixel (x, y from the top left) of the object to keep centred. GUIDE.md shows how to get that pixel from a camera and a point in the scene.

Pictures and movies are saved as follows.

- `top`, `motion`, `compare` and `record` save to the path given with `--file`.
- Without it they save under `touchdesigner-bridge/<pid>/img` in the temp folder and return the path.
- The temp folder is not emptied when TD closes. The folder of a TD that has been gone for ten minutes is removed the next time any TD loads the bridge. Save what you want to keep somewhere else with `--file`.
- `record` makes a Movie File Out TOP for the length of the recording and removes it afterwards.


`python3 td.py help` prints the full list. What to know before use (workflow, how TD behaves, build examples) is collected in [GUIDE.md](GUIDE.md), which `python3 td.py guide` also prints. Every command takes `--limit N` for the number of lines and `--timeout S` for the wait.

## Design

### Made for clients that have a shell

- The bridge is for agents that run commands, such as Claude Code, Codex CLI and Cursor, and for shell scripts.
- Each command is an ordinary process that prints text and returns an exit status, so it combines with pipes, here-documents and shell scripts.
  - `build` reads its spec from standard input.
  - `wait --when` holds a script until the project reaches a given state.
- A client without a shell, such as the chat in Claude Desktop, which can only call the tools registered with it, cannot use the bridge.

### Requests and replies are files

- All it asks of TD is one script that runs every frame.
- No port is opened, so there is no port number to configure and none to collide when several TDs run.
- Each TD process has its own request folder.
- Loaded from the textport, it is used without adding anything to the .toe.
- The folder is read once a frame, so a request waits for the next frame.

### Fixed commands and `py`

| | Used for | Examples |
|---|---|---|
| Fixed commands | Inspections and wiring that recur. A short formatted answer saves writing and reading the same Python every time. | `op`, `cook`, `err`, `build` |
| `py` | Runs any Python inside TD. It is the intended way to do what no command does. | |

The fixed commands are not meant to cover the whole of TD.

### Pictures are taken inside TD, at a chosen moment

- What a still shows depends on the frame in which it was taken. A `sleep` in the shell misses that frame by a round trip or more.
- `--when` waits for a condition inside TD and runs the command in the frame in which the condition becomes true.

### Movement is checked with consecutive frames, numbers and a movie

| Command | What it does |
|---|---|
| `motion` | Saves a contact sheet of consecutive frames and measures the flow between them. |
| `motion --follow` | Keeps a moving object in the middle of the sheet. |
| `trace` | Records parameters every frame. |
| `record` | Writes a few seconds to a movie file. |

- What the sheet and the numbers show: a jolt, an object that flips over or leaves the frame.
- What they do not show: what happens between two shots, and the pace of the motion as it plays. Play back a movie written by `record` for that.

### The effect of a change is compared in one frame

- `compare` takes the picture before and after a change in the same frame, and puts the change back. It takes parameter values, and a replacement in the text of a DAT.
- Two pictures taken at different times cannot tell the change from the scene moving on.
- It returns both pictures as numbers, how much of the picture differs and where, and a sheet of the two side by side. When nothing differs, it says so.

### Limits

- Requests run on TD's main thread, because TD objects can only be touched there. A request that takes long stops frames for that long. Grabbing one full-HD image of a TOP takes most of a frame at 60 fps.
- The request folder is protected only by the permissions of the user account. Software running as the same user can place a request and have code executed inside TD.

## Output design

- Output is short by default. Only non-default parameters are returned, lists are capped, and when a list is cut, the remaining count and a way to narrow it are added.
- One call is enough. `op` returns what is needed about one OP in a single call, and `build` creates and wires several OPs in a single call.
- What is worth knowing is said at the moment it applies, as a `note` line. `new` and `build` name the setting a type needs when they make one, and `top` says when the picture is black, white all over or holds invalid values. That keeps the reading beforehand short. It is advice: the command does the same either way.
- A wrong call gets a fix, not a traceback. A path that does not exist comes with near names that do exist, and a parameter that does not exist comes with near parameter names.
- Images are saved shrunk by a whole factor, each pixel the mean of a block. Reading a 4K TOP as is is heavy, plain decimation drops points and thin lines, and with a fractional factor (1920 to 768 is 2.5) the number of pixels averaged varies from place to place, which can leave film grain and patterns one or two pixels wide uneven. Alpha is dropped, giving the image over black.

## Reading cook

`cook` first measures the raw frame time over 20 frames, then adds up `cpuCookTime` and `gpuCookTime` of the OPs that are cooking for the number of frames given by `--frames`. The values in the table are averages per frame (milliseconds), and a frame in which an OP did not cook counts as 0. The totals per COMP are added up from the values of the OPs inside. `childrenCPUCookTime` is not used, because it goes stale at whatever value it had when something last read it.

OPs in the location open in the network editor cook for their viewers. The result changes with the location that is open while you inspect.

During the totaling every OP is scanned each frame, so frame time grows in that period. The frame time on the first line is the value measured before the scan.

Read GPU times as a rough guide. A TOP finishes cooking before its GPU work is done, so time spent waiting for the GPU can show up in the CPU time.

## Cautions

- Requests run on TD's main thread. A heavy request stops frames while it runs, so do not load the bridge during a live show.
- The changing commands and `py` can change the patch. When investigating, only read, and check with the owner before changing anything. Do not save the project.
- TD's own `/ui` and `/sys` are excluded.
- Anything that can write to the temp folder, that is, software running as the same user, can have code executed inside TD.
- It responds even while the timeline is paused (the delay reference is `op.TDResources`).

## Tested with

The commands under Commands were run against the real TD of these builds.

| TouchDesigner | OS |
|---|---|
| 2025.32460 | Windows 11 (driven from WSL2) |
| 2025.33230 | macOS |
| 2023.12600 | Windows 11 (driven from WSL2) |

- Older builds were not checked. On a build without a newer attribute the old name is used, and that fallback is covered by the tests that run against the stand-in for TD.
- Working with POPs needs a build that has them, 2025 or later.
- `record` writes Motion JPEG where the build or the machine cannot write H.264.
- `open` and `install FILE.toe` rely on `toeexpand` and `toecollapse` next to the TouchDesigner executable.

## Tests

```bash
python3 -m unittest discover -s tests
```

`tests/fake_td.py` stands in for TD. Names and meanings were checked against the Python stubs bundled with TD 2025.32460 (`bin/Lib/tdi`) and the bundled offline help.

To avoid depending on a particular build, only long-standing APIs are used, and builds without a newer attribute fall back to the old name.

## License

MIT. See [LICENSE](LICENSE).
