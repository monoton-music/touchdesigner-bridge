# Guide to read before use

What to know before inspecting or building in TouchDesigner with `td.py`. `python3 td.py help` lists the commands. `python3 td.py guide WORD` prints only the section whose heading contains WORD.

## Workflow

1. Connect. `td.py status` lists the TDs that respond. With none, launch one with `td.py open FILE.toe`. With several, set `TOUCHDESIGNER_BRIDGE_TO` to a pid or part of the .toe name.
2. Look. `ls`, `flow`, `op` and `err` show the current shape. `par` returns only non-default parameters. `find / --expr WORD` and `--text WORD` find the OPs whose expressions or DAT text contain a word.
3. Look up an unfamiliar OP before building with it: `types '*particle*'` finds the type, `parinfo PATH` gives parameter names and menu values, `doc TYPE` explains it. Do not guess parameter names.
4. Build. `build` creates and wires several OPs at once: one line per OP (`name type par=value ...`), then one line per wire (`a -> b`, `a -> b[1]`). `set PATH name=value` sets values; `name==expr` sets an expression.
5. Verify. `err` for errors, `cook` for load, `top` for the image, `geo` for point counts, `chop` for values. Always look at the image: it can be all white or all black with no errors.

What no command does, do with `py`.

- `td.py py 'result = ...'` runs Python inside TD and prints `result`. A longer script goes in through standard input: `td.py py - < script.py`.
- The script sees what the textport sees (`op`, `root`, `absTime`, `project`, `tdu`).
- Paths on the command line are converted for TD. Paths written inside a script are not: under WSL, write them as TD sees them (`D:/work/file.glsl`).
- `py` does not take `--when`. Put `td.py wait --when 'EXPR'` before it.
- A wrong path or parameter name is answered with the near names that exist. Read the reply before retrying.
- Commands add a `note` line when what they just did has a known catch: a type made by `new` or `build` that needs a setting, a picture from `top` that is black, white all over or holds invalid values. The note is advice at the moment it applies, so it is not repeated in this guide.
- Each changing command is one undo step, and `undo` takes it back.

In a project someone has open, only inspect. Change things only when asked, or in a project you opened yourself. Do not save.

## Leave a network a person can work with

Someone will open the network after you and change it by hand.

- Name each OP for what it does (`haze`, `only_hex`), not `null3`.
- Say why it is there. In `build`, text after `#` on a line becomes the node's comment; `note PATH 'text'` sets it later.
- Put the values worth changing on the parent COMP: `knob /project1 Speed=1.6@sim/inst.vec0valuey` adds a custom parameter on a Controls page and drives the target from it.
- Give a stage its own COMP when it has more than a handful of nodes.
- Arrange along the wiring with `layout COMP` (`--deep` for every COMP below). Networks grow left to right; unwired OPs go in columns to the right. `build` into an empty COMP arranges by itself.
- Box each stage of a COMP: `box /project1 '2 Lighting' light smooth compose --body 'what this stage does'`. Boxes stack in the order made, so number the titles. `layout` keeps OPs in their boxes.
- Run `overlaps` after building by script. It lists OPs lying on top of each other.
- In shaders, comment each uniform with what its components mean, and each step with what it does to the picture.
- Remove what is not used. A GLSL TOP or POP creates default DATs next to itself; delete them once it points at your own.

## How TD behaves

Cooking

- An OP cooks only when something asks for its output. With no viewer and no output, `cook` returns 0 and particles stay at 0. Open the viewer of the output TOP to run it: `td.py py 'op("/project1/out").openViewer()'`.
- OPs made by `new` and `build` have their node viewer on, and each viewer costs cook time. Turn them off when the network gets heavy: `flag '/project1/*' viewer=0`.
- OPs in the location open in the network editor cook for their viewers, so the result of `cook` changes with that location.
- Delayed execution (`run(..., delayFrames=1)`) stops while the timeline is paused unless it is given `delayRef=op.TDResources`.

Building

- The minimum that renders in 3D is a Geometry COMP (render and display flags on the last OP inside), a Camera COMP and a Render TOP. A light is needed only for materials that use lighting.
- Shaders go in through `write`. The DAT is marked as GLSL, docked to the OP that compiles it and compiled at once; the reply says `compiled` or lists the compiler's errors. `rename` keeps the OP pointing at a renamed shader DAT.
- A `py` script is one undo step. If it raises part-way, what it changed is undone, so a failed script does not leave the network half-built.
- `set` adds sequence blocks as needed (`attr3func=lt` on an OP that has two blocks).
- A material with a normal map needs tangents on the geometry: an Attribute Create SOP with Compute Tangents, or a Normal POP.

POPs and instancing

- Instancing draws every point of the instance POP. Hiding with the Active attribute or scaling to zero still pays for the point; remove it with a Delete POP.
- Read POP bounds with `bounds()`. `computeBounds()` returned 0.

## Load and memory

- `cook PATH` is the first thing to run when the frame rate drops.
- A chain of POPs costs CPU time per operator per cook, almost regardless of the point count. The cost is the number of operators that cook every frame.
- Anything that reads a value that changes every frame (a camera position, `absTime.seconds`) recooks everything after it. Build once what can be built once. Write a changing value into a parameter from an Execute DAT only when its rounded value changes.
- `mem PATH` before making a point set larger. A POP holds about its point capacity times its attribute count, and an operator after a Delete POP keeps the capacity of what went in. Drop attributes first: a Math Mix POP that copies the few attributes still needed onto themselves, with Delete Input Attributes on, passes only those.
- TD's main thread uses a full CPU core even with a light patch, because it spins while waiting for the next frame. That is not load.
- When fps is low but `cook` is light, look outside the patch: a CPU or GPU clock that has dropped, or the refresh rate of the monitor that vsync follows.
- GPU times are a rough guide. Time spent waiting for the GPU can show up as CPU time.
- Every request runs on TD's main thread. One `top` of a full-HD image, or one shot of `motion`, takes most of a frame at 60 fps. A `py` script that takes a second stops the picture for a second.

## Checking the image

- `top PATH` saves a PNG and returns the path. It shrinks by a whole factor and averages each block, so points and thin lines survive. Alpha is dropped, giving the image over black.
- For detail, cut a region instead of saving the frame larger: `top PATH --max 0 --crop 800,400,320,180` (x, y, width, height from the top left) saves every pixel of it. `pix` gives the colour of one point.
- `look PATH` describes the picture in numbers: tonal percentiles, the share that is black, mid, bright or clipped, hues, brightness by thirds, invalid values. Use it to check a claim such as "the mid-tones are lifted" before spending an image on it.
- `compare TOP TARGET par=value` (or `--swap 'OLD' --with 'NEW'` for the text of a DAT) answers "did this change do anything, and where". It reads the TOP, makes the change, reads again in the same frame and puts the change back. In a moving scene, two pictures taken at different times cannot answer that. `NO VISIBLE DIFFERENCE` means the change does not reach that TOP.
- A still shows the frame in which it was taken. `--when 'EXPR'` holds a command inside TD and runs it in the first frame in which the Python expression is true: `top /project1/out --when 'absTime.seconds % 38 > 9'`. A `sleep` in the shell misses the moment by a round trip or more. If the expression is still false after `--wait` seconds (60 by default) the command is refused. Every command except `py` takes it.
- `wait --when 'EXPR'` only waits. Use it to hold a shell script until the project reaches a state.
- Several commands started in the background with the same `--when` all run in the same frame. That captures every stage of a chain at one moment.
- With the working colour space at pass-through (the default), TOP values are stored and shown as they are. To work in linear light, keep the chain in a float format (`rgba16float`), do blurs, glow and lens effects there, and convert to display values once, in the last shader.
- `top`, `motion`, `compare` and `record` take `--file PATH`. Without it they save under the temp folder, which is emptied only after that TD has gone.

## Checking movement

- `motion PATH` takes several frames and reports how much of the picture changes per step, how fast it flows, whether it rushes outward, pans or rolls, and how even the speed is. It saves the frames as one sheet. Keep `--every` at 1 to 3 for speed and direction; a larger step is only for the sheet.
- On the whole frame a small object is a few pixels. `--crop X,Y,W,H` takes the shots from one region at full size.
- `--follow 'EXPR'` keeps a moving object in the middle of every shot. The expression returns its pixel (x, y from the top left) at each shot; `--size` sets the side of the square (240 by default). The report says where the pixel went and when it left the picture. This finds an object that flips over or passes through another.
- The pixel of a scene point comes from the camera. With horizontal field of view `fov`, picture `W`×`H` and `k = (W / 2) / tan(fov / 2)`, a point `q` in camera space lands at `(W / 2 - k * q.x / q.z, H / 2 + k * q.y / q.z)`:

```bash
Q="op('/project1/cam').worldTransform.getInverse() * tdu.Position(*op('/project1/geo/out1').points('P')[9])"
python3 td.py motion /project1/out --every 4 --size 240 \
  --follow "(lambda q: (960 - 1144.1 * q.x / q.z, 540 + 1144.1 * q.y / q.z))($Q)" \
  --when "(lambda q: q.z < 0 and abs(q.x / q.z) < 0.5 and abs(q.y / q.z) < 0.25)($Q)"
```

  Here `k` is 1144.1 (1920 wide, `fov` 80). The `--when` starts the shots once the point is in front of the camera and inside the frame.
- `trace PATH PAR ... frames=300` follows parameters frame by frame: range, top speed, the hardest jolt, reversals per second, and the shape as a strip of characters. A few reversals a second is natural for a hand-held or vehicle camera; tens is jitter, often a stepped lookup table.
- `record PATH --seconds 3 --file clip.mp4` writes a movie. A sheet does not show what happens between two shots or the pace of the motion; a movie does. Extract its frames and compare neighbours to find the one frame that jumps.
- Motion that repeats has a seam. Check the frames on both sides of the moment a loop starts over, not only the middle of the loop.

## Troubleshooting

- "TD is not responding": TD is not running, or the bridge is not loaded. Use `td.py open FILE.toe` or `td.py setup`.
- Nothing cooks: nothing is looking at the output. Open a viewer.
- Built it but no image: check `err`, the `camera` and `geometry` of the Render TOP, the render flag and the camera position (`tz` defaults to 5), in that order.
- Set a value but nothing changes: check the mode with `par PATH NAME --all`. A constant has no effect while an expression or export is in place.
