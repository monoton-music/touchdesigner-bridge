#!/usr/bin/env python3
"""Inspect and edit a running TouchDesigner. Connect with "td.py open FILE.toe", or paste the line from "td.py setup" into TD.

  td.py guide [part of a heading]      Guide to read before use (workflow, how TD behaves, checking the image and movement)

  td.py setup                          Print the line to paste into the TD textport and copy it to the clipboard
  td.py status | reload | stop         State of the TD side, reload, stop
  td.py open FILE.toe                  Launch TD and wait until connected. Creates an empty project if FILE is missing
  td.py open FILE.toe --install        Add the loader DAT to an existing .toe, then open it (original kept as .bak)
  td.py install [FILE.toe]             Only add the loader DAT. Without FILE, add it to the running TD (save it yourself)
  td.py uninstall                      Remove the loader DAT from the running TD

Inspect
  td.py info                           Build, measured fps, skipped frames, OP count
  td.py look PATH                      A TOP as numbers: tonal spread, black and clipped area, hues, where the light sits
  td.py motion PATH [--frames 8] [--every 2]   How a TOP moves: change per step, flow speed and direction, evenness; saves a contact sheet
                                       --crop X,Y,W,H looks at one region at full detail; --follow 'EXPR' [--size 240] keeps a moving
                                       thing centred (the expression gives its pixel x, y from the top left at every shot)
  td.py compare TOP TARGET par=value ...   The same frame of a TOP with and without a change: both as numbers, how much differs and where,
                                       and a sheet of A, B and the difference. For a DAT: compare TOP DAT --swap 'OLD' --with 'NEW'
  td.py record PATH [--seconds 3] [--file F] [--codec h264]   Record a TOP to a movie file, to watch the motion as it plays
  td.py trace PATH PAR ... [frames=120]    Follow parameters over time: range, speed, jolts, reversals per second, and their shape
  td.py cook [PATH] [--n 20] [--frames 60] [--type '*TOP'] [--sort gpu]   Heaviest cook times first
  td.py mem [PATH] [--n 20] [--sort cpu]                     Largest memory use first
  td.py ls [PATH] [--depth 1] [--type PAT] [--name PAT]      List children
  td.py find [PATH] [--name PAT] [--type PAT] [--text STR] [--expr STR]
  td.py op PATH                        Summary of one OP (type, position, connections, parameters, errors, cook)
  td.py flow COMP                      All wiring inside a COMP as a -> b
  td.py par PATH [PATTERN] [--all]     Parameter values (non-default only)
  td.py parinfo PATH [PATTERN]         Parameter definitions (label, type, menu values, range)
  td.py err [PATH]                     Errors and warnings
  td.py text PATH [--grep STR] [--start N]   DAT contents
  td.py chop PATH [PATTERN]            Current CHOP values
  td.py geo PATH [--attr 'A B']        Point counts, bounds and attribute names of a SOP or POP; --attr prints ranges and first values
  td.py pix PATH [--u 0.5 --v 0.5]     Color of one point of a TOP
  td.py top PATH [--max 768] [--crop X,Y,W,H] [--file F]   Save a TOP as PNG and return the path. Shrinks the long side by a whole factor
                                       (0 keeps every pixel); --crop saves one region, in pixels from the top left
  td.py wait --when 'EXPR'             Return the frame and the time once a Python expression is true; holds a shell script until then
  td.py conn PATH                      Inputs and outputs
  td.py types [PATTERN]                OP types that can be created
  td.py doc NAME [--grep STR] [--start N]    Read the bundled help (noiseTOP, Noise_TOP, ...)

Change (each one is a single undo step)
  td.py set PATH name=value ...        Set parameters (=expr for an expression)
  td.py pulse PATH NAME                Press a pulse
  td.py flag PATH bypass=1 ...         Flags (bypass lock display render viewer allowCooking)
  td.py new PARENT TYPE [NAME] [--x N --y N] [name=value ...]   Create an OP
  td.py build PARENT -  |  build PARENT --f FILE   Create and wire several OPs. One per line:
                                       "name type [par=value ...]", "a -> b", "a -> b[1]"
  td.py copy SRC PARENT [NAME] | rename PATH NAME
  td.py wire SRC DST [--index 0] [--out 0] | unwire DST [--index 0]
  td.py write PATH -  |  write PATH --f FILE     Replace DAT contents
                                       a shader DAT is marked GLSL, docked to its OP and compiled; errors come back
  td.py move PATH X Y
  td.py layout COMP [--deep]           Arrange the OPs along the wiring so none overlap; boxes keep their OPs and are refitted; --deep does every COMP below too
  td.py overlaps [COMP]                OPs lying on top of each other in the network editor, here and in every COMP below
  td.py box COMP 'TITLE' NAME ...       arrange these OPs of the COMP and put a titled box around them (one per stage)
  td.py note PATH 'TEXT'              comment shown on the node. In build, text after # on a line does the same
  td.py knob COMP Name=0.5@child.par  add a custom parameter to the COMP and drive child.par from it
                                       (several at once; without @... it only adds the parameter; --page Controls)
  td.py rm PATH --yes                  Delete (without --yes, only reports what would go); '/project1/tmp*' deletes every match
  td.py undo

  td.py py 'CODE' | py --f FILE | py -   Any Python. Returns result and whatever it prints

The last name in PATH may use * ? (par, set, flag, rm). Example: td.py par '/project1/noise*' seed
With several TDs open, choose one with --to (part of the .toe name, or the pid) or the TOUCHDESIGNER_BRIDGE_TO environment variable.
Every command takes --limit N for the number of lines and --timeout S for the wait.
top, motion, compare and record save under the temp folder unless --file names a path. What a TD leaves there is
removed the next time a TD loads the bridge, once that TD has been gone for ten minutes.
Every command except py takes --when 'EXPR' [--wait 60]: it runs in the first frame in which the Python expression is true,
  and is refused if that does not happen within --wait seconds. The expression sees the same names as py (op, absTime, ...).
  Example: td.py top /project1/out --when 'absTime.seconds % 38 > 9' --max 0 --crop 800,400,320,180
The folder can be changed with the TOUCHDESIGNER_BRIDGE_DIR environment variable.
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))


def _is_wsl():
    try:
        with open('/proc/version') as fp:
            return 'microsoft' in fp.read().lower()
    except OSError:
        return False


IS_WSL = _is_wsl()


def _cache(update=None):
    path = os.path.expanduser('~/.cache/touchdesigner-bridge.json')
    try:
        with open(path) as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        data = {}
    if update:
        data.update(update)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as fp:
            json.dump(data, fp)
    return data


def _windows_dirs():
    """Look up the Windows temp folder and home from WSL. Cached, because starting powershell is slow."""
    c = _cache()
    if 'temp' not in c or 'home' not in c:
        out = subprocess.run(
            ['powershell.exe', '-NoProfile', '-Command', '[IO.Path]::GetTempPath(); $env:USERPROFILE'],
            capture_output=True, text=True, cwd='/mnt/c').stdout.split()
        c = _cache({'temp': to_local(out[0]), 'home': to_local(out[1])})
    return c['temp'], c['home']


def queue_dir():
    d = os.environ.get('TOUCHDESIGNER_BRIDGE_DIR')
    if d:
        return d
    if IS_WSL:
        return os.path.join(_windows_dirs()[0], 'touchdesigner-bridge')
    # On Windows and macOS, the temp folder of the user running TD is visible as is.
    import tempfile
    return os.path.join(tempfile.gettempdir(), 'touchdesigner-bridge')


def to_td(path):
    """Convert a local path to the path TD sees."""
    path = os.path.abspath(path)
    if IS_WSL:
        return subprocess.run(['wslpath', '-w', path], capture_output=True, text=True).stdout.strip()
    return path


def from_td(path):
    """Convert a path returned by TD to a local path. A path with spaces is treated as one path."""
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', path) if IS_WSL else None
    return '/mnt/%s/%s' % (m.group(1).lower(), m.group(2).replace('\\', '/')) if m else path


def _build_of(path):
    """The build numbers in a path as a sort key, so the newest install comes last."""
    return [int(n) for n in re.findall(r'\d+', path)]


def find_bin(base):
    """The TouchDesigner bin folder. Tries the environment variable, a running TD, a TD seen before, then the default location."""
    import glob
    d = os.environ.get('TOUCHDESIGNER_BIN')
    if not d:
        seen = [a.get('bin') for _, a in instances(base) if a.get('bin')]
        d = from_td(seen[0]) if seen else _cache().get('bin')
    if not d or not os.path.isdir(d):
        if sys.platform == 'darwin':
            # The app is named with its build (TouchDesigner 2025.33230.app); the tools sit in Contents/MacOS.
            found = sorted(glob.glob('/Applications/TouchDesigner*.app/Contents/MacOS'), key=_build_of)
        else:
            root = '/mnt/c/Program Files' if IS_WSL else os.environ.get('ProgramFiles', r'C:\Program Files')
            found = sorted(glob.glob(os.path.join(root, 'Derivative', 'TouchDesigner*', 'bin')))
        d = found[-1] if found else None
    if d:
        _cache({'bin': d})
    return d


LOADER = 'local/touchdesigner_bridge'


def loader_text():
    """Contents of the Execute DAT that loads the bridge when the project starts."""
    return ("# touchdesigner-bridge. Loads the bridge when the project starts. Remove with: td.py uninstall\n"
            "def onStart():\n"
            "\tpath = r'%s'\n"
            "\ttry:\n"
            "\t\twith open(path, encoding='utf-8') as f:\n"
            "\t\t\texec(compile(f.read(), path, 'exec'), dict(globals()))\n"
            "\texcept Exception as e:\n"
            "\t\tprint('touchdesigner-bridge:', e)\n"
            "\treturn\n") % to_td(os.path.join(HERE, 'touchdesigner_bridge.py'))


def loader_files():
    """The three files of the loader DAT inside an expanded .toe. The format follows toeexpand output."""
    text = loader_text().encode('utf-8')
    head = b'2\n' + bytes.fromhex('2a000000' + '01000000' * 4 + '02') + len(text).to_bytes(4, 'big')
    return [
        (LOADER + '.n', b'DAT:execute\ntile 0 0 130 90\nflags =  parlanguage 0\nend\n'),
        (LOADER + '.parm', b'?\nstart 0 on\nlanguage 0 python\n?\n'),
        (LOADER + '.text', head + text),
    ]


def toe_with_loader(base, file, bin_dir):
    """Put the loader DAT into file. If file is missing, create an empty project. TD need not be running.

    Uses toeexpand and toecollapse, which ship with TD. The original file is kept as .bak.
    Returns 'created', 'installed' or 'already'.
    """
    import shutil
    work = os.path.join(base, 'work-%d' % os.getpid())
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)

    def tool(name):
        exe = os.path.join(bin_dir, name + ('' if sys.platform == 'darwin' else '.exe'))
        r = subprocess.run([exe, 'p.toe'], cwd=work, capture_output=True, text=True)
        return r.stdout + r.stderr

    try:
        tree, toc = os.path.join(work, 'p.toe.dir'), os.path.join(work, 'p.toe.toc')
        if os.path.exists(file):
            shutil.copyfile(file, os.path.join(work, 'p.toe'))
            said = tool('toeexpand')
            if not os.path.exists(toc):
                raise RuntimeError('toeexpand failed: ' + said.strip())
            with open(toc, newline='') as fp:
                lines = fp.read().split('\n')
            if LOADER + '.n' in lines:
                return 'already'
            # Add at the end of the /local entries. A child listed before its parent cannot be read.
            at = max(i for i, x in enumerate(lines) if x.startswith('local.') or x.startswith('local/'))
            lines[at + 1:at + 1] = [n for n, _ in loader_files()]
            os.remove(os.path.join(work, 'p.toe'))
            result = 'installed'
        else:
            os.makedirs(tree)
            for name, body in (('.start', b'cookrate 60\nrealtime on\n'),
                               ('project1.n', b'COMP:container\ntile 0 0 160 130\nflags =  viewer 1 parlanguage 0\nend\n'),
                               ('local.n', b'COMP:base\ntile -200 -175 160 130\nflags =  viewer 1 parlanguage 0\nend\n')):
                with open(os.path.join(tree, name), 'wb') as fp:
                    fp.write(body)
            lines = ['.start', 'project1.n', 'local.n'] + [n for n, _ in loader_files()] + ['']
            result = 'created'
        os.makedirs(os.path.join(tree, 'local'), exist_ok=True)
        for name, body in loader_files():
            with open(os.path.join(tree, name), 'wb') as fp:
                fp.write(body)
        with open(toc, 'w', newline='') as fp:
            fp.write('\n'.join(lines))
        said = tool('toecollapse')
        if not os.path.exists(os.path.join(work, 'p.toe')):
            raise RuntimeError('toecollapse failed: ' + said.strip())
        if result == 'installed':
            shutil.copyfile(file, file + '.bak')
        shutil.copyfile(os.path.join(work, 'p.toe'), file)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def launch(bin_dir, file):
    """Launch TD. Returns without waiting."""
    quiet = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == 'darwin':
        # bin_dir is NAME.app/Contents/MacOS. Open by the bundle path: the app name carries the build.
        app = os.path.dirname(os.path.dirname(os.path.abspath(bin_dir)))
        subprocess.Popen(['open', '-n', '-a', app if app.endswith('.app') else 'TouchDesigner', file], **quiet)
    else:
        subprocess.Popen([os.path.join(bin_dir, 'TouchDesigner.exe'), to_td(file)], cwd=bin_dir,
                         start_new_session=True, **quiet)


def setup_line():
    """The line to paste into the TD textport, written with the path TD sees."""
    return "exec(open(r'%s', encoding='utf-8').read())" % to_td(os.path.join(HERE, 'touchdesigner_bridge.py'))


def copy_to_clipboard(text):
    for cmd in (['clip.exe'], ['pbcopy'], ['clip']):
        try:
            subprocess.run(cmd, input=text.encode('utf-16-le' if 'clip' in cmd[0] else 'utf-8'), check=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


def instances(base):
    """The TDs that are responding, as (folder, state) pairs. Each TD process has its own folder."""
    found = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return found
    for name in names:
        d = os.path.join(base, name)
        try:
            with open(os.path.join(d, 'out', 'alive.json'), encoding='utf-8') as fp:
                a = json.load(fp)
        except (OSError, ValueError):
            continue
        if time.time() - a['time'] < 10:
            found.append((d, a))
    return found


def pick(base, to=None):
    """Pick one target. to is part of a .toe name or a pid. Returns the reason when none can be picked."""
    found = instances(base)
    if to is not None:
        found = [(d, a) for d, a in found if str(to) == str(a.get('pid')) or str(to).lower() in a['toe'].lower()]
    if len(found) == 1:
        return found[0][0], None
    if not found:
        return None, 'TD is not responding. td.py setup prints the line to paste into the textport.'
    return None, 'multiple TDs. choose with --to: ' + ', '.join('%s (pid %s)' % (a['toe'], a.get('pid')) for d, a in found)


def parse(argv):
    """Split positional arguments from --key value. Values are read as JSON when they parse."""
    def val(s):
        try:
            return json.loads(s)
        except ValueError:
            return s
    args, kw, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith('--') and len(a) > 2:
            if i + 1 < len(argv) and not argv[i + 1].startswith('--'):
                kw[a[2:]] = val(argv[i + 1])
                i += 2
            else:
                kw[a[2:]] = True
                i += 1
        else:
            args.append(a)
            i += 1
    return args, kw


def to_local(text):
    """On WSL, convert returned Windows paths to /mnt/x."""
    if not IS_WSL:
        return text
    # A path inside this WSL distribution comes back as \\wsl.localhost\NAME\... (or \\wsl$\NAME\...).
    distro = os.environ.get('WSL_DISTRO_NAME')
    if distro:
        text = re.sub(r'\\\\wsl(?:\.localhost|\$)\\%s((?:\\[^\s"\\]+)*)' % re.escape(distro),
                      lambda m: m.group(1).replace('\\', '/') or '/', text, flags=re.I)
    return re.sub(r'\b([A-Za-z]):\\([^\s"]*)',
                  lambda m: '/mnt/%s/%s' % (m.group(1).lower(), m.group(2).replace('\\', '/')), text)


def send(d, ext, body, timeout):
    rid = '%d-%s' % (time.time() * 1000, uuid.uuid4().hex[:6])
    req = os.path.join(d, 'in', rid + ext)
    with open(req + '.tmp', 'w', encoding='utf-8') as fp:
        fp.write(body)
    os.replace(req + '.tmp', req)
    res = os.path.join(d, 'out', rid + '.json')
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(res, encoding='utf-8') as fp:
                out = json.load(fp)
        except (OSError, ValueError):
            time.sleep(0.002)
            continue
        os.remove(res)
        return out
    try:
        os.remove(req)
    except OSError:
        pass
    return None


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ('help', '-h', '--help'):
        print(__doc__.strip())
        return 0
    if argv[0] == 'guide':
        # The guide on what to know before use. Given part of a heading, prints only that section.
        with open(os.path.join(HERE, 'GUIDE.md'), encoding='utf-8') as fp:
            parts = re.split(r'(?m)^(?=## )', fp.read())
        if len(argv) > 1:
            parts = [x for x in parts[1:] if argv[1].lower() in x.split('\n', 1)[0].lower()] or \
                ['no such section. sections: ' + ', '.join(x.split('\n', 1)[0][3:] for x in parts[1:])]
        print(''.join(parts).strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'setup':
        line = setup_line()
        print(line)
        print('Paste into the TD textport (Alt+T) and run.' +
              (' Copied to the clipboard.' if copy_to_clipboard(line) else ''), file=sys.stderr)
        return 0
    base = queue_dir()
    if cmd == 'status':
        found = instances(base)
        for d, a in found:
            print('running: %s build %s pid %s frame %d' % (a['toe'], a['build'], a.get('pid'), a['frame']))
        if not found:
            print('not running (%s)' % base)
        return 0 if found else 1

    args, kw = parse(rest)
    timeout = float(kw.pop('timeout', 30))
    if 'when' in kw and cmd not in ('open', 'install', 'py'):
        timeout += float(kw.get('wait', 60))     # the TD side holds the command until the moment comes
    if cmd in ('open', 'install') and args:
        # With the loader DAT in the .toe, just opening it connects. This works without TD running.
        file = args[0]
        bin_dir = find_bin(base)
        if bin_dir is None:
            print('TouchDesigner not found. Set TOUCHDESIGNER_BIN to its bin folder.', file=sys.stderr)
            return 2
        if cmd == 'open' and os.path.exists(file) and not kw.get('install'):
            state = 'kept'
        else:
            try:
                state = toe_with_loader(base, file, bin_dir)
            except (RuntimeError, OSError) as e:
                print(e, file=sys.stderr)
                return 1
        if cmd == 'install':
            print({'created': 'created %s', 'already': 'already installed: %s',
                   'installed': 'installed into %s. the original is kept as .bak'}[state] % file)
            return 0
        before = {a.get('pid') for _, a in instances(base)}
        launch(bin_dir, file)
        end = time.time() + float(kw.get('wait', 90))
        while time.time() < end:
            for _, a in instances(base):
                if a.get('pid') not in before:
                    print('running: %s build %s pid %s' % (a['toe'], a['build'], a.get('pid')))
                    return 0
            time.sleep(0.3)
        print('Launched, but no response. If this .toe has no loader DAT, close TD and add it with '
              'td.py open FILE.toe --install (the original is kept as .bak).', file=sys.stderr)
        return 3
    d, why = pick(base, kw.pop('to', os.environ.get('TOUCHDESIGNER_BRIDGE_TO')))
    if d is None:
        print(why, file=sys.stderr)
        return 2
    if cmd == 'py':
        if 'f' in kw:
            with open(kw['f'], encoding='utf-8') as fp:
                code = fp.read()
        elif args and args[0] == '-':
            code = sys.stdin.read()
        elif args:
            code = args[0]
        else:
            print('no code', file=sys.stderr)
            return 2
        out = send(d, '.py', code, timeout)
    else:
        if cmd == 'install':
            args = [loader_text()]
        if cmd in ('compare', 'record') and isinstance(kw.get('file'), str):
            kw['file'] = to_td(kw['file'])       # TD writes the file, so give it a path it can see
        if cmd == 'record':
            try:
                timeout += float(kw.get('seconds', 3))
            except (TypeError, ValueError):
                pass
        if cmd in ('set', 'flag', 'new', 'knob', 'trace', 'box', 'compare'):
            # Positional arguments of the form name=value are passed as parameters.
            for p in [a for a in args if '=' in a]:
                k, v = p.split('=', 1)
                try:
                    kw[k] = json.loads(v)
                except ValueError:
                    kw[k] = v
            args = [a for a in args if '=' not in a]
        elif cmd in ('write', 'build'):
            key = 'text' if cmd == 'write' else 'spec'
            if 'f' in kw:
                with open(kw.pop('f'), encoding='utf-8') as fp:
                    kw[key] = fp.read()
            elif len(args) > 1 and args[1] == '-':
                kw[key] = sys.stdin.read()
                args = args[:1]
        elif cmd in ('top', 'motion') and isinstance(kw.get('file'), str):
            # TD writes the file, so give it a path it can see.
            kw['file'] = to_td(kw['file'])
        elif cmd == 'reload':
            path = setup_line()
            out = send(d, '.py', path + "\nresult = 'reloaded'", timeout)
            print(out['result'] if out and out['ok'] else (out or {}).get('error', 'timeout'))
            return 0 if out and out['ok'] else 1
        out = send(d, '.json', json.dumps({'cmd': cmd, 'args': args, 'kw': kw}), timeout)

    if out is None:
        print('timeout', file=sys.stderr)
        return 3
    if out.get('stdout'):
        sys.stdout.write(to_local(out['stdout']))
    if not out['ok']:
        sys.stderr.write(out['error'].rstrip('\n') + '\n')
        return 1
    r = out.get('result')
    if r is not None:
        print(to_local(r) if isinstance(r, str) else json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
