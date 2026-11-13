# The side that runs inside TouchDesigner. Load it with exec from the textport.
#
#   exec(open(r'C:\path\to\touchdesigner-bridge\touchdesigner_bridge.py', encoding='utf-8').read())
#
# Every frame it looks for requests placed in touchdesigner-bridge/<pid>/in under the temp folder, runs
# them on TD's main thread and writes the results to out. It opens no port. It creates no nodes
# of its own, so nothing is left in the .toe and it is gone when TD closes. To stop it, send td.py stop.
#
# TD objects can only be touched from the main thread, so instead of another thread or a socket,
# it calls itself again every frame with a delayed run(). delayRef is op.TDResources because with
# the default root the delay does not advance while the timeline is paused, and requests would
# go unanswered.
#
# To avoid depending on a particular build, it uses only long-standing APIs and falls back to the
# old name when a newer attribute is missing (_attr). The syntax also stays within Python 3.5.
import contextlib
import difflib
import fnmatch
import functools
import html
import inspect
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import types

_max = max  # kept because some commands take an argument named max

_HEARTBEAT_FRAMES = 60

# One folder per process, so requests do not mix when several TDs are open.
_dir = os.path.join(
    os.environ.get('TOUCHDESIGNER_BRIDGE_DIR') or os.path.join(tempfile.gettempdir(), 'touchdesigner-bridge'),
    str(os.getpid()))
_in = os.path.join(_dir, 'in')
_out = os.path.join(_dir, 'out')
_img = os.path.join(_dir, 'img')
for _d in (_in, _out, _img):
    os.makedirs(_d, exist_ok=True)

_STALE_SECONDS = 600


def _sweep(base, keep, now=None):
    """Remove the folders left by TD processes that have gone: their requests, replies and saved images.
    A folder belongs to a process that has gone when its heartbeat is missing or has not been written for
    ten minutes. A running TD writes it about once a second, also while the timeline is paused."""
    import shutil
    now = time.time() if now is None else now
    gone = 0
    try:
        names = os.listdir(base)
    except OSError:
        return gone
    for name in names:
        d = os.path.join(base, name)
        if not name.isdigit() or name == keep or not os.path.isdir(d):
            continue
        try:
            beat = os.path.getmtime(os.path.join(d, 'out', 'alive.json'))
        except OSError:
            try:
                beat = os.path.getmtime(d)
            except OSError:
                continue
        if now - beat > _STALE_SECONDS:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    return gone


try:
    _sweep(os.path.dirname(_dir), str(os.getpid()))
except Exception:
    pass

# The generation number is shared across the TD process so that a reload stops the old loop.
_state = sys.modules.setdefault('_touchdesigner_bridge_state', types.SimpleNamespace(gen=0))
_state.gen += 1
_gen = _state.gen
_ticks = [0]
_pending = []  # requests that span several frames (generators)


# ---- Helpers that keep the output short ----

def _attr(o, names, default=0.0):
    """Try the names newest first. Some attributes are missing in some builds."""
    for n in names:
        v = getattr(o, n, None)
        if v is not None:
            return v
    return default


_CPU = ('cpuCookTime', 'cookTime')
_GPU = ('gpuCookTime',)


class Refused(Exception):
    """A wrong call. Returns only a message that shows how to fix it, with no traceback."""


def _o(path):
    if any(ch in str(path) for ch in '*?['):
        # op() would quietly return the first match, and the command would act on that one alone.
        raise Refused('%s is a pattern, and this command takes one OP. * ? work in the last name for: '
                      'par set flag rm' % path)
    o = op(path)
    if o is None:
        # Walk up to where the path exists and add the near names found there.
        parts = str(path).strip('/').split('/')
        base, missing = root, parts[0] if parts else ''
        for i, part in enumerate(parts):
            nxt = op('/' + '/'.join(parts[:i + 1]))
            if nxt is None:
                missing = part
                break
            base = nxt
        names = [c.name for c in base.findChildren(maxDepth=1)] if base.isCOMP else []
        near = difflib.get_close_matches(missing, names, 5, 0.4) or names[:12]
        raise Refused('no such OP: %s. %s has: %s' % (path, base.path, ' '.join(near) or '(no children)'))
    return o


def _need(o, *families):
    """Check that the OP is of a family the command can handle."""
    if o.family not in families:
        raise Refused('%s is a %s, not a %s' % (o.path, o.OPType, ' or '.join(families)))
    return o


def _os(path):
    """If the last name has * ? [, return every matching child of its parent."""
    path = str(path)
    head, _, tail = path.rstrip('/').rpartition('/')
    if not any(ch in tail for ch in '*?['):
        return [_o(path)]
    found = [c for c in _o(head or '/').findChildren(maxDepth=1) if fnmatch.fnmatchcase(c.name, tail)]
    if not found:
        raise Refused('nothing matches %s' % path)
    return found


_SEQ_PAR = re.compile(r'^([a-z]+?)(\d+)[a-z]')


def _par(o, name):
    p = getattr(o.par, name, None)
    if p is None:
        # A parameter of a sequence block that does not exist yet (attr3func when there are 2 blocks):
        # add blocks up to that one.
        m = _SEQ_PAR.match(name)
        seq = getattr(getattr(o, 'seq', None), m.group(1), None) if m else None
        if seq is not None and int(m.group(2)) < 64:
            try:
                if seq.numBlocks <= int(m.group(2)):
                    seq.numBlocks = int(m.group(2)) + 1
                p = getattr(o.par, name, None)
            except Exception:
                p = None
    if p is None:
        near = difflib.get_close_matches(name, [q.name for q in o.pars('*')], 6, 0.4)
        raise Refused('no such par: %s on %s. near: %s (td.py parinfo %s)' % (
            name, o.path, ' '.join(near) or '-', o.path))
    return p


def _clip(lines, limit):
    if limit and len(lines) > limit:
        return lines[:limit] + ['... +%d more. narrow the query or pass --limit N' % (len(lines) - limit)]
    return lines


def _num(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, float):
        return '%g' % round(v, 5)
    return str(v)


def _rel(o, base):
    p = o.path
    b = base.path.rstrip('/')
    return p[len(b) + 1:] if p.startswith(b + '/') else p


def _match(value, pattern):
    return pattern is None or fnmatch.fnmatchcase(value.lower(), str(pattern).lower())


# ---- Commands. They return a string. yield waits one frame. ----

def c_ls(path='/', depth=1, type=None, name=None, limit=80):
    """List children. relative path, type, [child count], [B=bypass X=cooking off]"""
    o = _o(path)
    lines = []
    for k in o.findChildren(maxDepth=depth):
        if not (_match(k.OPType, type) and _match(k.name, name)):
            continue
        s = '%s %s' % (_rel(k, o), k.OPType)
        if k.isCOMP:
            s += ' [%d]' % len(k.children)
            if not getattr(k, 'allowCooking', True):
                s += ' X'
        if getattr(k, 'bypass', False):
            s += ' B'
        lines.append(s)
    return '\n'.join(_clip(lines, limit)) or '(none)'


def _par_lines(o, pattern='*', all=False):
    lines = []
    for p in o.pars(pattern):
        if not all and p.isDefault:
            continue
        mode = str(p.mode).split('.')[-1]
        try:
            val = _num(p.eval())
        except Exception as e:
            val = '<%s>' % e.__class__.__name__
        s = '%s=%s' % (p.name, val)
        if mode == 'EXPRESSION':
            s += '  expr: %s' % p.expr
        elif mode == 'BIND':
            s += '  bind: %s' % getattr(p, 'bindExpr', '')
        elif mode == 'EXPORT':
            s += '  (export)'
        lines.append(s)
    return lines


def c_op(path, limit=40):
    """Summary of one OP. Type, flags, position, connections, non-default parameters, errors, cook."""
    o = _o(path)
    flags = [n for n in ('bypass', 'lock') if getattr(o, n, False)]
    if o.isCOMP and not getattr(o, 'allowCooking', True):
        flags.append('cooking-off')
    head = '%s %s at %s %s' % (o.path, o.OPType, getattr(o, 'nodeX', '?'), getattr(o, 'nodeY', '?'))
    if flags:
        head += ' [%s]' % ' '.join(flags)
    if o.isCOMP:
        head += ' children %d' % len(o.children)
    if getattr(o, 'isTOP', False):
        head += ' %dx%d' % (o.width, o.height)
    lines = [
        head,
        'in: %s' % (' '.join(i.path for i in o.inputs) or '-'),
        'out: %s' % (' '.join(i.path for i in o.outputs) or '-'),
        'cook: cpu %.3f ms gpu %.3f ms, total %s, last at frame %s (now %s)' % (
            _attr(o, _CPU), _attr(o, _GPU), getattr(o, 'totalCooks', '?'), _num(o.cookAbsFrame), _num(absTime.frame)),
    ]
    for label, text in (('E', o.errors()), ('W', o.warnings())):
        for ln in (text or '').splitlines():
            if ln.strip():
                lines.append('%s %s' % (label, ln.strip()))
    return '\n'.join(lines + _clip(_par_lines(o), limit))


def c_flow(path, limit=120):
    """Wiring inside a COMP, all of it as a -> b. Unconnected OPs are listed at the end."""
    o = _o(path)
    kids = o.findChildren(maxDepth=1)
    edges, wired = [], set()
    for k in kids:
        for i, src in enumerate(k.inputs):
            if src is None:
                continue
            edges.append('%s -> %s%s' % (_rel(src, o), k.name, '[%d]' % i if i else ''))
            wired.update((src.path, k.path))
    loose = [k.name for k in kids if k.path not in wired]
    lines = _clip(edges, limit)
    if loose:
        lines.append('unwired: %s' % ' '.join(loose[:60]) + (' ...' if len(loose) > 60 else ''))
    return '\n'.join(lines) or '(empty)'


def c_par(path, pattern='*', all=False, limit=80):
    """Parameters. Only non-default ones unless all is set. Expressions are shown after expr:."""
    return '\n'.join('%s %s\n%s' % (o.path, o.OPType, '\n'.join(_clip(_par_lines(o, pattern, all), limit))
                                    or '(all default)') for o in _os(path))


def c_err(path='/', limit=40):
    """Errors and warnings, including descendants."""
    o = _o(path)
    # At the root, look only at the children findChildren returns, to keep out warnings from TD's own /ui and /sys.
    targets = o.findChildren(maxDepth=1) if o.path == '/' else [o]
    # The same message shows up on many OPs, so group by message and print only the count and the first place.
    groups, order = {}, []
    for label, method in (('E', 'errors'), ('W', 'warnings')):
        for t in targets:
            for ln in (getattr(t, method)(recurse=True) or '').splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                m = re.search(r'\s*\((/[^()]*)\)$', ln)
                where = m.group(1) if m else ''
                msg = re.sub(r'^(/\S*:)?\s*(Warning|Error):\s*', '', ln[:m.start()] if m else ln)
                key = (label, msg)
                if key not in groups:
                    groups[key] = [0, where]
                    order.append(key)
                groups[key][0] += 1
    lines = []
    for label, msg in order:
        n, where = groups[(label, msg)]
        lines.append('%s%s %s%s' % (label, ' x%d' % n if n > 1 else '', msg, '  @ ' + where if where else ''))
    return '\n'.join(_clip(lines, limit)) or 'no errors, no warnings'


def c_find(path='/', name=None, type=None, text=None, expr=None, limit=60):
    """Search by name, type, a string in DAT text, or a string in parameter expressions."""
    o = _o(path)
    lines = []
    for k in o.findChildren():
        if not (_match(k.name, name) and _match(k.OPType, type)):
            continue
        if text is not None:
            if not k.isDAT:
                continue
            try:
                if str(text) not in k.text:
                    continue
            except Exception:
                continue
        if expr is not None:
            hit = [p.name for p in k.pars('*') if str(p.mode).endswith('EXPRESSION') and str(expr) in p.expr]
            if not hit:
                continue
            lines.append('%s %s  %s' % (k.path, k.OPType, ' '.join(hit[:8])))
            continue
        lines.append('%s %s' % (k.path, k.OPType))
    return '\n'.join(_clip(lines, limit)) or '(none)'


def c_text(path, grep=None, start=1, limit=60):
    """DAT contents, with line numbers."""
    o = _need(_o(path), 'DAT')
    lines = []
    for i, ln in enumerate(o.text.splitlines(), 1):
        if i < start or (grep is not None and str(grep) not in ln):
            continue
        lines.append('%d: %s' % (i, ln))
    return '\n'.join(_clip(lines, limit)) or '(empty)'


def c_chop(path, pattern='*', limit=60):
    """Current values of CHOP channels."""
    o = _need(_o(path), 'CHOP')
    lines = []
    for c in o.chans(pattern):
        vals = c.vals
        lo, hi = min(vals), max(vals)
        if lo == hi:
            lines.append('%s=%s' % (c.name, _num(lo)))
        else:
            lines.append('%s %s..%s' % (c.name, _num(lo), _num(hi)))
    return '%s %dch x %d\n%s' % (o.path, o.numChans, o.numSamples, '\n'.join(_clip(lines, limit)))


def c_conn(path):
    """Input and output connections."""
    o = _o(path)
    return 'in: %s\nout: %s' % (
        ' '.join(i.path for i in o.inputs) or '-',
        ' '.join(i.path for i in o.outputs) or '-')


def _region(a, crop):
    """Cut a region out of an image array whose first row is the top. crop is 'x,y,w,h' in pixels from the
    top left corner. What falls outside the image is left black."""
    import numpy
    try:
        x, y, w, h = [int(round(float(v))) for v in (crop if isinstance(crop, (list, tuple)) else str(crop).split(','))]
    except (ValueError, TypeError):
        raise Refused("crop is 'x,y,w,h' in pixels from the top left corner, for example --crop 800,400,320,180")
    if w < 1 or h < 1:
        raise Refused('crop needs a width and a height of at least 1')
    out = numpy.zeros((h, w) + a.shape[2:], dtype=a.dtype)
    x0, y0, x1, y1 = _max(x, 0), _max(y, 0), min(x + w, a.shape[1]), min(y + h, a.shape[0])
    if x1 > x0 and y1 > y0:
        out[y0 - y:y1 - y, x0 - x:x1 - x] = a[y0:y1, x0:x1]
    return out


def _shrink(a, long_side):
    """Shrink an image array so that its long side is at most long_side, by averaging whole blocks of pixels.
    The factor is a whole number: with a fractional one the number of pixels averaged varies from place to
    place, which can leave patterns a pixel or two wide uneven. Returns the array and the factor."""
    k = 1 if not long_side or long_side < 1 else -(-_max(a.shape[0], a.shape[1]) // int(long_side))
    if k <= 1:
        return a, 1
    h, w = a.shape[0] // k * k, a.shape[1] // k * k
    if not h or not w:
        return a, 1
    try:
        # With a whole factor, cv2's area averaging is the same block mean, and several times faster.
        import cv2
        import numpy
        return cv2.resize(numpy.ascontiguousarray(a[:h, :w]), (w // k, h // k), interpolation=cv2.INTER_AREA), k
    except Exception:
        return a[:h, :w].reshape(h // k, k, w // k, k, -1).mean(axis=(1, 3)), k


def _save_png(a, file):
    """Write a float RGB(A) array, top row first, as an 8-bit PNG over black. False without cv2."""
    try:
        import cv2
        import numpy
    except Exception:
        return False
    # Drop alpha. TD colors are premultiplied, so RGB alone is the image over black.
    a = numpy.clip(numpy.nan_to_num(a[:, :, :3]), 0.0, 1.0)
    return bool(cv2.imwrite(file, (a[:, :, [2, 1, 0]] * 255.0 + 0.5).astype(numpy.uint8)))


def _picture_warning(a):
    """A line to add to a reply when the picture is not worth opening as it is: nothing in it, all of it
    clipped, or invalid values in it."""
    try:
        import numpy
        rgb = a[:, :, :3]
        bad = int((~numpy.isfinite(rgb)).sum())
        if bad:
            return ('\nnote  %d values are not numbers (NaN or infinite). A blur or bloom after this spreads them over '
                    'the frame: find the first stage that has them and replace them there' % bad)
        hi = float(rgb.max())
        if hi <= 0.002:
            return '\nnote  the picture is black. If nothing errors, check that something asks this TOP to cook, and what feeds it'
        if float((rgb.min(axis=2) >= 1.0).mean()) > 0.98:
            return '\nnote  the picture is white all over. Look at the stage before the post-processing'
    except Exception:
        pass
    return ''


def c_top(path, file=None, max=768, crop=None):
    """Save a TOP as an image and return the path. The long side is shrunk to at most max by a whole factor;
    max=0 keeps every pixel. crop='x,y,w,h' (pixels from the top left) saves only that region."""
    o = _need(_o(path), 'TOP')
    if isinstance(max, bool) or not isinstance(max, (int, float)) or max < 0:
        raise Refused('max is the longest side in pixels, for example --max 768. 0 keeps every pixel')
    if crop is True:
        raise Refused("crop is 'x,y,w,h' in pixels from the top left corner, for example --crop 800,400,320,180")
    file = file or os.path.join(_img, o.path.strip('/').replace('/', '_') + '.png')
    w, h = o.width, o.height
    if crop or (max and _max(w, h) > max):
        # Work on the array read back, so no TOP has to be created. Without cv2 or numpy, save at full size.
        try:
            a = o.numpyArray()[::-1]
            if crop:
                a = _region(a, crop)
            warn = _picture_warning(a)
            a, k = _shrink(a, max)
            if _save_png(a, file):
                return '%dx%d%s saved at %dx%d%s: %s%s' % (
                    w, h, ' crop %s' % crop if crop else '', a.shape[1], a.shape[0],
                    ' (1 pixel = %dx%d of the TOP)' % (k, k) if k > 1 else ' (every pixel)', file, warn)
        except Refused:
            raise
        except Exception:
            pass
        if crop:
            raise Refused('crop needs numpy and cv2 in this TouchDesigner')
    try:
        o.save(file)
    except Exception as e:
        raise Refused('cannot save %s: %s' % (file, e))
    try:
        warn = _picture_warning(o.numpyArray())
    except Exception:
        warn = ''
    return '%dx%d saved: %s%s' % (w, h, file, warn)


_HUES = ('red', 'orange', 'yellow', 'lime', 'green', 'mint', 'cyan', 'azure', 'blue', 'violet', 'magenta', 'rose')


def _small(o, long_side=256):
    """The TOP as a float array, top row first, thinned to about long_side pixels on the long side."""
    a = o.numpyArray()[::-1]
    k = _max(1, int(_max(a.shape[0], a.shape[1]) // long_side))
    return a[::k, ::k, :3].astype('float32')


def c_look(path):
    """Numbers that describe a TOP as a picture: tonal spread, black and clipped area, colour, where the light sits."""
    import numpy as np
    o = _need(_o(path), 'TOP')
    full = o.numpyArray()
    bad = int(np.isnan(full).sum() + np.isinf(full).sum())
    a = np.nan_to_num(_small(o))
    peak = float(a.max()) if a.size else 0.0
    a = np.clip(a, 0.0, 1.0)
    luma = a[:, :, 0] * .2126 + a[:, :, 1] * .7152 + a[:, :, 2] * .0722
    top = a.max(axis=2)
    low = a.min(axis=2)
    pct = [float(np.percentile(top, p)) for p in (1, 10, 50, 90, 99)]
    sat = np.where(top > 1e-4, (top - low) / np.maximum(top, 1e-4), 0.0)
    coloured = (sat > .25) & (top > .08)
    out = ['%s %dx%d' % (o.path, o.width, o.height) + ('  INVALID VALUES: %d (nan or inf)' % bad if bad else ''),
           'tone   p1 %s  p10 %s  p50 %s  p90 %s  p99 %s   peak %s' % tuple(_num(round(v, 3)) for v in pct + [peak]),
           'area   black(<0.02) %d%%  dark(<0.1) %d%%  mid(0.1-0.6) %d%%  bright(>0.6) %d%%  clipped(>0.98) %s%%' % (
               round(float((top < .02).mean()) * 100), round(float((top < .1).mean()) * 100),
               round(float(((top >= .1) & (top <= .6)).mean()) * 100), round(float((top > .6).mean()) * 100),
               _num(round(float((top > .98).mean()) * 100, 1)))]
    if coloured.any():
        r, g, b = a[:, :, 0][coloured], a[:, :, 1][coloured], a[:, :, 2][coloured]
        hue = (np.degrees(np.arctan2(np.sqrt(3.0) * (g - b), 2 * r - g - b)) + 360.0) % 360.0
        weight = sat[coloured] * top[coloured]
        bins = np.bincount(((hue + 15.0) // 30).astype(int) % 12, weights=weight, minlength=12)
        share = bins / _max(float(bins.sum()), 1e-9)
        named = sorted(((s, n) for s, n in zip(share, _HUES) if s >= .03), reverse=True)
        out.append('colour %d%% of the frame is coloured, mean saturation %s:  %s' % (
            round(float(coloured.mean()) * 100), _num(round(float(sat[coloured].mean()), 2)),
            '  '.join('%s %d%%' % (n, round(s * 100)) for s, n in named)))
    else:
        out.append('colour none (grey or black)')
    h, w = luma.shape
    rows = []
    for j in range(3):
        rows.append(' '.join('%4s' % _num(round(float(luma[j * h // 3:(j + 1) * h // 3, i * w // 3:(i + 1) * w // 3].mean()), 2))
                             for i in range(3)))
    gx = np.abs(np.diff(luma, axis=1)).mean()
    gy = np.abs(np.diff(luma, axis=0)).mean()
    out.append('detail %s (mean brightness step between neighbouring pixels at this size)' % _num(round(float(gx + gy), 3)))
    out.append('light by thirds, top row first:\n  ' + '\n  '.join(rows))
    return '\n'.join(out)


def c_motion(path, frames=8, every=2, file=None, crop=None, follow=None, size=240):
    """How a TOP moves. Takes frames images, every frames apart, and reports how much changes per step, how
    fast and which way the picture flows, and how even the motion is. Saves the images side by side.
    crop='x,y,w,h' looks at one region at full detail instead of the whole picture thinned out.
    follow='EXPR' keeps a moving thing in view: the expression is worked out at every shot and gives the
    pixel (x, y from the top left) to centre a square of size pixels on. A region longer than 480 pixels
    is shrunk by a whole factor."""
    import numpy as np
    o = _need(_o(path), 'TOP')
    frames, every = _max(2, int(frames)), _max(1, int(every))
    if crop is True or follow is True:
        raise Refused("crop is 'x,y,w,h' in pixels from the top left; follow is a Python expression giving the pixel (x, y)")
    if follow:
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        if size < 8:
            raise Refused('size is the side of the followed square in pixels, at least 8')
    shots, centres = [], []
    t0 = time.perf_counter()

    def shot():
        a = o.numpyArray()[::-1]
        if not crop and not follow:
            # Averaged, not thinned: thinning drops points and thin lines, and they flicker from shot to shot.
            return _shrink(a[:, :, :3], 320)[0].astype('float32')
        if follow:
            try:
                x, y = eval(str(follow), dict(_base))
                where = [float(x) - size / 2.0, float(y) - size / 2.0, size, size]
                centres.append((float(x), float(y)))
            except Exception as e:
                raise Refused('follow: %s: %s. It is a Python expression giving the pixel (x, y) to centre on, '
                              'from the top left' % (e.__class__.__name__, e))
        else:
            where = crop
        return _shrink(_region(a, where)[:, :, :3].astype('float32'), 480)[0]

    for i in range(frames):
        shots.append(np.clip(np.nan_to_num(shot()), 0.0, 1.0))
        if i < frames - 1:
            for _ in range(every):
                yield
    span = time.perf_counter() - t0
    grey = [s[:, :, 0] * .2126 + s[:, :, 1] * .7152 + s[:, :, 2] * .0722 for s in shots]
    change = [float(np.abs(b - a).mean()) for a, b in zip(grey, grey[1:])]
    out = ['%s %d frames, %d apart, %s s%s' % (o.path, frames, every, _num(round(span, 2)),
                                         '  (following %s: flow is measured relative to it)' % follow if follow else '  (crop %s)' % crop if crop else ''),
           'change per step  mean %s  min %s  max %s  (0 = still, about 0.1 = most of the picture replaced)' % (
               _num(round(sum(change) / len(change), 3)), _num(round(min(change), 3)), _num(round(_max(change), 3)))]
    if centres:
        # Where the followed point went. Outside the picture the shots are black, which reads as "still".
        lost = sum(1 for x, y in centres if not (0 <= x < o.width and 0 <= y < o.height))
        out.append('centre %d,%d -> %d,%d of %dx%d%s' % (
            round(centres[0][0]), round(centres[0][1]), round(centres[-1][0]), round(centres[-1][1]), o.width, o.height,
            '  OUTSIDE THE PICTURE in %d of %d shots' % (lost, len(centres)) if lost else ''))
    try:
        import cv2
    except Exception:
        cv2 = None
    if cv2 is not None:
        h, w = grey[0].shape
        ys, xs = np.mgrid[0:h, 0:w].astype('float32')
        rx, ry = (xs - w / 2.0) / w, (ys - h / 2.0) / w
        rad = np.sqrt(rx * rx + ry * ry) + 1e-6
        speed, rush, pan, roll = [], [], [], []
        for a, b in zip(grey, grey[1:]):
            f = cv2.calcOpticalFlowFarneback((a * 255).astype('uint8'), (b * 255).astype('uint8'), None, .5, 3, 21, 3, 5, 1.1, 0)
            fx, fy = f[:, :, 0] / w / every, f[:, :, 1] / w / every        # fraction of the width, per frame
            textured = (np.abs(cv2.Laplacian(a, cv2.CV_32F)) > .01)
            if textured.sum() < 50:
                textured = np.ones_like(a, dtype=bool)
            speed.append(float(np.sqrt(fx * fx + fy * fy)[textured].mean()))
            rush.append(float(((fx * rx + fy * ry) / rad)[textured].mean()))    # outward = moving forward
            roll.append(float(((fy * rx - fx * ry) / rad)[textured].mean()))
            pan.append((float(fx[textured].mean()), float(-fy[textured].mean())))
        mean = sum(speed) / len(speed)
        even = 0.0 if mean < 1e-6 else (sum((s - mean) ** 2 for s in speed) / len(speed)) ** .5 / mean
        p = lambda v: _num(round(v * 100, 2))
        out.append('flow   speed %s%% of the width per frame  (%s%% per second)' % (p(mean), _num(round(mean * 100 * project.cookRate, 0))))
        out.append('       outward %s%%  (positive = rushing forward, negative = pulling back)' % p(sum(rush) / len(rush)))
        out.append('       pan x %s%% y %s%%   roll %s%%' % (p(sum(v[0] for v in pan) / len(pan)), p(sum(v[1] for v in pan) / len(pan)), p(sum(roll) / len(roll))))
        out.append('       unevenness %s  (0 = constant speed; above about 0.5 the motion stutters or lurches)' % _num(round(even, 2)))
        file = file or os.path.join(_img, o.path.strip('/').replace('/', '_') + '_motion.png')
        cols = min(4, frames)
        blank = np.zeros_like(shots[0])
        tiles = shots + [blank] * (-len(shots) % cols)
        sheet = np.concatenate([np.concatenate(tiles[i:i + cols], axis=1) for i in range(0, len(tiles), cols)], axis=0)
        if cv2.imwrite(file, (sheet[:, :, [2, 1, 0]] * 255.0 + .5).astype('uint8')):
            out.append('sheet  %s  (in time order, left to right, top to bottom)' % file)
        else:
            out.append('sheet  NOT SAVED: cannot write %s' % file)
    else:
        out.append('flow   not measured: cv2 is not available in this TouchDesigner')
    return '\n'.join(out)


def c_compare(path, target, file=None, max=768, **change):
    """The same frame of a TOP with and without a change: compare /project1/out /project1/lv1 opacity=0.5, or
    for the text of a DAT, compare /project1/out /project1/shader swap='OLD' with='NEW'. The change is made,
    the picture is read again in the same frame, and the change is put back. Reports both pictures as
    numbers, how much differs and where, and saves them side by side with the difference."""
    import numpy as np
    o = _need(_o(path), 'TOP')
    t = _o(target)
    swap, new = change.pop('swap', None), change.pop('with', '')
    if swap is None and not change:
        raise Refused("name the change: par=value ..., or swap='OLD' with='NEW' for the text of a DAT")
    if swap is not None:
        _need(t, 'DAT')
        if isinstance(swap, bool) or isinstance(new, bool):
            raise Refused("swap and with take text: --swap 'OLD' --with 'NEW'")
        swap, new = str(swap), str(new)
        if swap not in t.text:
            raise Refused('%s does not contain %r' % (t.path, swap))
    pars = [_par(t, n) for n in change]
    kept = [(q, q.mode, q.val, q.expr) for q in pars]
    text = t.text if swap is not None else None

    def grab():
        return np.clip(np.nan_to_num(o.numpyArray()[::-1][:, :, :3].astype('float32')), 0.0, 1.0)

    a = grab()
    try:
        if change:
            _set_pars(t, change)
        if swap is not None:
            t.text = text.replace(swap, new)
        b = grab()
    finally:
        if text is not None:
            t.text = text
        for q, mode, val, expr in kept:
            if mode == ParMode.EXPRESSION:
                q.expr = expr
            else:
                q.val = val
            q.mode = mode
    if a.shape != b.shape:
        raise Refused('the change alters the size of %s (%dx%d to %dx%d), so the two cannot be compared' % (
            o.path, a.shape[1], a.shape[0], b.shape[1], b.shape[0]))

    def line(tag, x):
        m = x.max(axis=2)
        q = np.percentile(m, (50, 90, 99))
        return '%s  mean %s  median %s  90%% %s  99%% %s' % (tag, _num(round(float(x.mean()), 4)), _num(round(float(q[0]), 4)),
                                                         _num(round(float(q[1]), 4)), _num(round(float(q[2]), 4)))
    d = np.abs(b - a).max(axis=2)
    moved = d > 1.0 / 255.0
    out = ['%s %dx%d  A = as it is, B = with %s' % (
        o.path, a.shape[1], a.shape[0],
        ' '.join(['%s=%s' % kv for kv in change.items()] + (['%r -> %r in %s' % (swap, new, t.path)] if swap is not None else []))),
        line('A', a), line('B', b)]
    if not moved.any():
        out.append('NO VISIBLE DIFFERENCE: no pixel changes by a step of 8 bits (largest change %s)' % _num(round(float(d.max()), 5)))
    else:
        ys, xs = np.nonzero(moved)
        out.append('differs in %s%% of the picture, by %s on average there and %s at most; B - A mean %s' % (
            _num(round(float(moved.mean()) * 100, 2)), _num(round(float(d[moved].mean()), 4)), _num(round(float(d.max()), 4)),
            _num(round(float((b - a).mean()), 4))))
        out.append('where: x %d..%d  y %d..%d from the top left' % (xs.min(), xs.max(), ys.min(), ys.max()))
    file = file or os.path.join(_img, o.path.strip('/').replace('/', '_') + '_compare.png')
    gain = 1.0 / _max(float(d.max()), 1e-6)
    sheet = np.concatenate([a, b, np.repeat(d[:, :, None], 3, axis=2) * gain], axis=1)
    sheet, k = _shrink(sheet, 3 * max if max else 0)
    if _save_png(sheet, file):
        out.append('sheet  %s  (A, B, and the difference scaled so its largest value is white)' % file)
    return '\n'.join(out)


def c_record(path, seconds=3, file=None, codec=None):
    """Record a TOP to a movie file for a number of seconds, for a person to watch. A Movie File Out TOP is
    made next to the TOP for the length of the recording and removed afterwards. The project keeps running
    at its own rate while it records."""
    o = _need(_o(path), 'TOP')
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 0
    if isinstance(seconds, bool) or not 0 < seconds <= 600:
        raise Refused('seconds is how long to record, more than 0 and at most 600')
    m = _up(o).create('moviefileoutTOP', 'bridge_record')
    try:
        try:
            m.viewer = False
            m.nodeX, m.nodeY = o.nodeX, o.nodeY - 200
        except Exception:
            pass
        codecs = list(getattr(m.par.videocodec, 'menuNames', None) or [])
        if codec is None:
            codec = 'h264' if 'h264' in codecs else 'mjpa'
        elif codecs and codec not in codecs:
            raise Refused('no such codec: %s. codecs: %s' % (codec, ' '.join(codecs)))
        ext = '.mp4' if codec in ('h264', 'h265', 'mpeg4', 'av1') else '.gif' if codec == 'gif' else '.webm' if codec in ('vp8', 'vp9') else '.mov'
        file = file or os.path.join(_img, o.path.strip('/').replace('/', '_') + ext)
        if os.path.exists(file):
            os.remove(file)
        m.inputConnectors[0].connect(o.outputConnectors[0])
        m.par.videocodec.val = codec
        m.par.file.mode = ParMode.CONSTANT
        m.par.file.val = file
        t0 = time.perf_counter()
        f0 = absTime.frame
        m.par.record.val = True
        yield
        problem = m.errors()
        if problem and codec == 'h264' and 'mjpa' in codecs:
            # H.264 goes through the graphics card's encoder; without one, fall back to Motion JPEG.
            m.par.record.val = False
            codec, file = 'mjpa', os.path.splitext(file)[0] + '.mov'
            m.par.videocodec.val = codec
            m.par.file.val = file
            yield
            t0, f0 = time.perf_counter(), absTime.frame
            m.par.record.val = True
            yield
            problem = m.errors()
        if problem:
            raise Refused('cannot record %s: %s' % (o.path, problem.strip().splitlines()[-1]))
        while time.perf_counter() - t0 < seconds:
            yield
        span, frames = time.perf_counter() - t0, absTime.frame - f0
        m.par.record.val = False
        yield
        yield
    finally:
        m.destroy()
    if not os.path.exists(file):
        raise Refused('recorded, but no file was written: %s' % file)
    return '%s %dx%d  %s s, %d frames (%s fps), %s, %s MB: %s' % (
        o.path, o.width, o.height, _num(round(span, 2)), frames, _num(round(frames / _max(span, 1e-6), 1)), codec,
        _num(round(os.path.getsize(file) / 1e6, 1)), file)


_BARS = ' .:-=+*#%@'


def c_trace(path, *names, **kw):
    """Follow parameters over time: trace /project1/cam ty rx rz frames=180.
    For each one: range, fastest change per second, hardest change of that speed (a jolt shows up here),
    how many times a second it reverses direction, and its shape as a strip of characters."""
    o = _o(path)
    frames = _max(8, int(kw.get('frames', 120)))
    pars = [_par(o, n) for n in names]
    if not pars:
        raise Refused('name the parameters to follow')
    rows = [[] for _ in pars]
    t0 = time.perf_counter()
    for _ in range(frames):
        for row, p in zip(rows, pars):
            row.append(float(p.eval()))
        yield
    span = _max(time.perf_counter() - t0, 1e-6)
    dt = span / frames
    out = ['%s %d frames, %s s' % (o.path, frames, _num(round(span, 2)))]
    for p, v in zip(pars, rows):
        d = [(b - a) / dt for a, b in zip(v, v[1:])]
        dd = [(b - a) / dt for a, b in zip(d, d[1:])]
        turns = sum(1 for a, b in zip(d, d[1:]) if a * b < 0)
        lo, hi = min(v), _max(v)
        cols = 60
        strip = ''.join(_BARS[int((v[int(i * (len(v) - 1) / (cols - 1))] - lo) / _max(hi - lo, 1e-9) * (len(_BARS) - 1))]
                        for i in range(cols))
        out.append('%-6s %s .. %s  speed max %s/s  jolt max %s/s2  reverses %s/s\n       [%s]' % (
            p.name, _num(round(lo, 3)), _num(round(hi, 3)), _num(round(_max(abs(x) for x in d), 2)),
            _num(round(_max([abs(x) for x in dd] or [0]), 1)), _num(round(turns / span, 1)), strip))
    return '\n'.join(out)


def _frame_times(frames):
    """Measure the raw frame time. Returns avg, max, median and the number of skipped frames."""
    ts = [time.perf_counter()]
    f0 = absTime.frame
    for _ in range(frames):
        yield
        ts.append(time.perf_counter())
    dts = sorted((b - a) * 1000.0 for a, b in zip(ts, ts[1:]))
    skipped = max(0, int(absTime.frame - f0) - frames)
    return sum(dts) / len(dts), dts[-1], dts[len(dts) // 2], skipped


def _frame_line(avg, mx, med, skipped, frames):
    return 'target %s fps  measured %.1f fps  frame ms avg %.2f med %.2f max %.2f  skipped %d/%d' % (
        _num(project.cookRate), 1000.0 / avg, avg, med, mx, skipped, frames)


def _pane():
    try:
        return ui.panes.current.owner.path
    except Exception:
        return '?'


def c_info(frames=30):
    """Build, project and measured frame time."""
    t = yield from _frame_times(frames)
    return '\n'.join([
        'build %s  toe %s' % (app.build, project.name),
        _frame_line(*t, frames),
        'ops %d  realTime %s  frame %d' % (len(root.findChildren()), project.realTime, absTime.frame),
        'network editor at %s' % _pane(),
    ])


def c_cook(path='/', n=20, frames=60, type=None, sort='cpu'):
    """Heaviest cook times first. Measures the raw frame time, then totals over frames frames."""
    o = _o(path)
    t = yield from _frame_times(20)
    ops = [k for k in o.findChildren() if _match(k.OPType, type)]
    acc = {}
    for _ in range(frames):
        f = absTime.frame
        for k in ops:
            try:
                if k.cookAbsFrame < f - 1:
                    continue
                a = acc.get(k.path)
                if a is None:
                    a = acc[k.path] = [k, 0, 0.0, 0.0]
                a[1] += 1
                a[2] += _attr(k, _CPU)
                a[3] += _attr(k, _GPU)
            except Exception:
                pass
        yield
    fam = {}
    tree = {}
    base = o.path.rstrip('/')
    for k, cooks, cpu, gpu in acc.values():
        fam[k.family] = fam.get(k.family, 0.0) + cpu / frames
        # Add up the per-COMP totals here. childrenCPUCookTime goes stale at whatever it was when last read.
        parts = k.path[len(base) + 1:].split('/')[:-1]
        for i in range(1, len(parts) + 1):
            key = base + '/' + '/'.join(parts[:i])
            node = tree.setdefault(key, [0.0, 0.0, 0])
            node[0] += cpu / frames
            node[1] += gpu / frames
            node[2] += 1
    total = sum(fam.values())
    lines = [
        _frame_line(*t, 20),
        'cooking %d of %d ops under %s, cpu %.2f ms/frame: %s' % (
            len(acc), len(ops), o.path, total,
            ' '.join('%s %.2f' % kv for kv in sorted(fam.items(), key=lambda kv: -kv[1]))),
        'cpu ms/frame | gpu ms/frame | cooks/%d | path type' % frames,
    ]
    col = 3 if sort == 'gpu' else 2
    rows = sorted(acc.values(), key=lambda a: -a[col])[:n]
    if not rows:
        lines[-1] = 'nothing is cooking. an OP cooks only when something asks for its output (a viewer, a window, an output OP)'
    for k, cooks, cpu, gpu in rows:
        lines.append('%.3f %.3f %d %s %s' % (cpu / frames, gpu / frames, cooks, k.path, k.OPType))
    comps = sorted(tree.items(), key=lambda kv: -kv[1][col - 2])[:_max(5, n // 2)]
    if comps:
        lines.append('by COMP, everything inside: cpu ms/frame | gpu ms/frame | cooking ops | path')
        for key, (cpu, gpu, count) in comps:
            lines.append('%.3f %.3f %d %s' % (cpu, gpu, count, key))
    return '\n'.join(lines)


def c_mem(path='/', n=20, sort='gpu'):
    """Largest memory use first, in MB: the OP itself and everything inside it."""
    o = _o(path)
    key = 'cpuMemory' if sort == 'cpu' else 'gpuMemory'
    rows = []
    for k in [o] + (list(o.findChildren()) if o.isCOMP else []):
        g, c = _attr(k, ('gpuMemory',), 0), _attr(k, ('cpuMemory',), 0)
        if g or c:
            rows.append((g, c, k))
    rows.sort(key=lambda r: -(r[1] if key == 'cpuMemory' else r[0]))
    lines = ['total gpu %.0f MB cpu %.0f MB in %d ops' % (
        sum(r[0] for r in rows) / 1e6, sum(r[1] for r in rows) / 1e6, len(rows)), 'gpu MB | cpu MB | path type']
    for g, c, k in rows[:n]:
        lines.append('%.1f %.1f %s %s' % (g / 1e6, c / 1e6, k.path, k.OPType))
    return '\n'.join(lines)


def c_parinfo(path, pattern='*', limit=60):
    """Parameter definitions. name "label" type @page, menu values, range, default."""
    o = _o(path)
    lines = []
    for p in o.pars(pattern):
        s = '%s "%s" %s' % (p.name, p.label, p.style)
        page = getattr(getattr(p, 'page', None), 'name', None)
        if page:
            s += ' @%s' % page
        names = getattr(p, 'menuNames', None)
        if names:
            s += ' menu: %s' % '|'.join(str(m) for m in names[:40])
        elif getattr(p, 'isNumber', False):
            s += ' %s..%s' % (_num(p.normMin), _num(p.normMax))
        s += ' default=%s' % _num(p.default)
        if getattr(p, 'readOnly', False):
            s += ' readonly'
        lines.append(s)
    return '\n'.join(_clip(lines, limit)) or '(none)'


def c_types(pattern='*', limit=80):
    """OP types that can be created. A plain word matches anywhere in the name, in any case (`types pop`)."""
    names = []
    plain = not any(ch in pattern for ch in '*?[')
    for fam in sorted(families):
        for t in families[fam]:
            n = getattr(t, '__name__', None) or str(t)
            if _match(n, pattern) or (plain and pattern.lower() in n.lower()):
                names.append(n)
    return ' '.join(_clip(sorted(names), limit)) or '(none)'


_doc_index = {}


def _doc_file(name):
    folder = os.path.join(app.samplesFolder, 'Learn', 'OfflineHelp', 'https.docs.derivative.ca')
    if not _doc_index:
        for f in os.listdir(folder):
            if f.endswith('.htm'):
                _doc_index[re.sub(r'[\W_]+', '', f[:-4]).lower()] = os.path.join(folder, f)
    key = re.sub(r'[\W_]+', '', str(name)).lower()
    if key in _doc_index:
        return _doc_index[key], []
    near = sorted(os.path.basename(v)[:-4] for k, v in _doc_index.items() if key in k)
    return None, near


def c_doc(name, grep=None, start=1, limit=40):
    """Read the bundled offline help. The name can be noiseTOP, Noise_TOP or noiseTOP_Class."""
    path, near = _doc_file(name)
    if path is None:
        return 'no page "%s". near: %s' % (name, ' '.join(near[:30]) or '-')
    with open(path, 'r', encoding='utf-8', errors='replace') as fp:
        s = fp.read()
    i = s.find('id="mw-content-text"')
    s = s[s.find('>', i) + 1:] if i >= 0 else s
    j = s.find('class="printfooter"')
    s = s[:j] if j >= 0 else s
    s = re.sub(r'(?is)<(script|style).*?</\1>', '', s)
    s = re.sub(r'(?i)<(br|/p|/li|/h\d|/tr|/pre|/div|/dd|/dt)[^>]*>', '\n', s)
    s = html.unescape(re.sub(r'<[^>]+>', '', s))
    out = []
    for n, ln in enumerate((x.strip() for x in s.splitlines() if x.strip()), 1):
        if n < start or (grep is not None and str(grep).lower() not in ln.lower()):
            continue
        out.append('%d: %s' % (n, ln[:400]))
    return '\n'.join(_clip(out, limit)) or '(no match)'


def c_geo(path, n=5, attr=None):
    """Point and primitive counts, bounds and first points of a SOP or POP.
    attr='Height Rot' also prints the range and first values of those POP point attributes."""
    o = _need(_o(path), 'SOP', 'POP')

    def val(name):
        v = getattr(o, name, None)
        return v() if callable(v) else v

    lines = ['%s %s points %s prims %s' % (o.path, o.OPType, val('numPoints'), val('numPrims'))]
    try:
        # computeBounds returned 0 in real TD, so it is not used. Builds without bounds() read min and max.
        b = o.bounds() if callable(getattr(o, 'bounds', None)) else o
        lines.append('min %s max %s' % (
            ' '.join(_num(float(x)) for x in b.min), ' '.join(_num(float(x)) for x in b.max)))
    except Exception as e:
        lines.append('bounds: <%s>' % e.__class__.__name__)
    try:
        # POPs carry named attributes. Their names are what instancing and GLSL refer to.
        attrs = ['%s[%s]' % (a.name, a.size) for a in o.pointAttributes]
        if attrs:
            lines.append('point attributes: ' + ' '.join(attrs))
    except Exception:
        pass
    if getattr(o, 'isSOP', False):
        for i, pt in enumerate(o.points):
            if i >= n:
                break
            lines.append('%d: %s %s %s' % (i, _num(pt.x), _num(pt.y), _num(pt.z)))
    for name in (attr or '').split():
        try:
            import numpy
            vals = o.points(name)
            a = numpy.array([[float(x) for x in v] if hasattr(v, '__len__') else [float(v)]
                             for v in (vals[i] for i in range(min(len(vals), int(val('numPoints')))))], dtype='float64')
            lines.append('%s  min %s  max %s  first: %s' % (
                name, ' '.join(_num(round(float(x), 3)) for x in a.min(axis=0)),
                ' '.join(_num(round(float(x), 3)) for x in a.max(axis=0)),
                ' | '.join(' '.join(_num(round(float(x), 3)) for x in row) for row in a[:int(n)])))
        except Exception as e:
            lines.append('%s: <%s>' % (name, e.__class__.__name__))
    return '\n'.join(lines)


def c_wait():
    """Returns the frame and the time. With --when EXPR it returns once the expression is true: a way to
    hold a shell script until the project reaches a moment."""
    return 'frame %d  absTime %.3f s' % (absTime.frame, absTime.seconds)


def _when(expr, wait, fn, args, kw):
    """Hold a command until a Python expression is true, looking once a frame, then run it in that frame.
    What a still shows depends on the moment it is taken; waiting on the client side misses it by the length
    of a round trip, and a sleep in a shell script by more."""
    ns = dict(_base)
    t0 = time.perf_counter()
    last = None
    while True:
        try:
            last = eval(expr, ns)
        except Exception as e:
            raise Refused('when: %s: %s. It is a Python expression, for example --when "absTime.seconds %% 38 > 9"' % (e.__class__.__name__, e))
        if last:
            break
        if time.perf_counter() - t0 > wait:
            raise Refused('when: still false after %s s (last value %r). Raise --wait, or check the expression' % (_num(wait), last))
        yield
    result = fn(*args, **kw)
    if inspect.isgenerator(result):
        result = yield from result
    return result


def c_pix(path, u=0.5, v=0.5):
    """Color of one point of a TOP. Slow, because it reads back the whole texture."""
    o = _need(_o(path), 'TOP')
    return '%dx%d uv %s %s rgba %s' % (o.width, o.height, _num(u), _num(v),
                                       ' '.join(_num(float(c)) for c in o.sample(u=u, v=v)))


# ---- Commands that change the patch. Each change is grouped into one undo step. ----

def _undoable(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kw):
        try:
            undo = ui.undo
        except (NameError, AttributeError):
            undo = None
        if undo is not None:
            undo.startBlock('touchdesigner-bridge %s' % fn.__name__[2:])
        try:
            return fn(*args, **kw)
        finally:
            if undo is not None:
                try:
                    undo.endBlock()
                except Exception:
                    # Some operations (creating an Annotate COMP) close the undo block themselves.
                    pass
    return wrapped


def _set_pars(o, values):
    out = []
    for name, v in values.items():
        p = _par(o, name)
        if isinstance(v, str) and v.startswith('='):
            p.expr = v[1:]
            p.mode = ParMode.EXPRESSION
        else:
            p.mode = ParMode.CONSTANT
            p.val = v
        out.append('%s=%s' % (name, _num(p.eval())))
    return ' '.join(out)


@_undoable
def c_set(path, **values):
    """Set parameters. A value starting with = is set as an expression."""
    targets = _os(path)
    if len(targets) == 1:
        return _set_pars(targets[0], values)
    return '\n'.join('%s %s' % (t.path, _set_pars(t, values)) for t in targets)


@_undoable
def c_pulse(path, name):
    """Press a pulse parameter."""
    _par(_o(path), name).pulse()
    return 'pulsed %s' % name


_FLAGS = ('bypass', 'lock', 'display', 'render', 'viewer', 'allowCooking', 'activeViewer', 'expose')


@_undoable
def c_flag(path, **flags):
    """Change flags: bypass lock display render viewer allowCooking."""
    for name in flags:
        if name not in _FLAGS:
            raise Refused('unknown flag: %s. flags: %s' % (name, ' '.join(_FLAGS)))
    targets = _os(path)
    for o in targets:
        for name, v in flags.items():
            setattr(o, name, bool(v))
    return '%d op(s): %s' % (len(targets), ' '.join('%s=%s' % (n, _num(bool(v))) for n, v in flags.items()))


@_undoable
def c_new(parent, type, name=None, x=None, y=None, **pars):
    """Create an OP. Write the type like noiseTOP. The remaining name=value are parameters."""
    o = _create(_o(parent), type, name)
    if x is not None and y is not None:
        o.nodeX, o.nodeY = int(x), int(y)
    s = '%s %s' % (o.path, o.OPType)
    return s + (' ' + _set_pars(o, pars) if pars else '') + _hints([o])


# What is worth knowing about a type at the moment one is made. Said by the command that makes it, so that it
# does not have to be read beforehand. Advice, not a rule: nothing is refused or changed because of it.
_ON_CREATE = {
    'geometryCOMP': 'it contains torus1: delete it if it is not needed. Instances that bank need instrord=zxy',
    'renderTOP': 'camera and geometry are looked up by name: set them explicitly',
    'cameraCOMP': 'tz starts at 5. A camera that banks needs rord=zxy (roll, pitch, then yaw)',
    'glslTOP': 'one input connector takes several wires (setInputs([a, b, c])); inputs past the third go in the tops parameter',
    'glslPOP': 'read an input attribute as TDIn_Name(), write Name[id], list what is written in outputattrs, and give each uniform a type (vec0type=vec4)',
    'poptoTOP': 'in its default mode it writes P and ignores Attributes Scope: set rgbamode=custom for another attribute',
    'particlePOP': 'the Null POP at the end of the forces wired after it goes in targetpop; after changing parameters press initializepulse and startpulse',
    'moviefileoutTOP': 'td.py record does this for a few seconds without leaving an OP behind',
}


def _hints(ops):
    seen, out = set(), []
    for o in ops:
        t = o.OPType
        if t in _ON_CREATE and t not in seen:
            seen.add(t)
            out.append('note  %s: %s' % (t, _ON_CREATE[t]))
    return ('\n' + '\n'.join(out)) if out else ''


def _create(p, type, name=None):
    if not p.isCOMP:
        raise Refused('%s is not a COMP' % p.path)
    # Look the type up as a class first, for builds that do not accept the type name as a string.
    try:
        cls = eval(type)
    except Exception:
        cls = type
    try:
        o = p.create(cls, name) if name else p.create(cls)
    except Exception:
        known = [getattr(t, '__name__', str(t)) for fam in families.values() for t in fam]
        near = difflib.get_close_matches(str(type), known, 6, 0.4)
        raise Refused('cannot create %s in %s. near: %s (td.py types PATTERN)' % (type, p.path, ' '.join(near) or '-'))
    # Scripted OPs start with the node viewer off. Turn it on so the network shows what each node makes.
    try:
        o.viewer = True
    except Exception:
        pass
    return o


@_undoable
def c_build(parent, spec):
    """Create and wire several OPs. Each spec line is "name type [par=value ...]", "a -> b" or "a -> b[1]"."""
    p = _o(parent)
    made, links = {}, []
    x = _max([getattr(c, 'nodeX', 0) + getattr(c, 'nodeWidth', 130) for c in p.children] or [0]) + 70
    for n, raw in enumerate(spec.splitlines(), 1):
        ln, _, note = raw.partition('#')
        ln, note = ln.strip(), note.strip()
        if not ln:
            continue
        m = re.match(r'^(\S+)\s*->\s*([^\s\[]+)(?:\[(\d+)\])?$', ln)
        if m:
            links.append((n, m.group(1), m.group(2), int(m.group(3) or 0)))
            continue
        parts = ln.split()
        if len(parts) < 2:
            raise Refused('line %d: expected "name type [par=value ...]" or "a -> b": %s' % (n, raw))
        o = _create(p, parts[1], parts[0])
        o.nodeX, o.nodeY = x, 0
        if note:
            # The text after # stays on the node as its comment, for whoever opens the network later.
            o.comment = note
        x += getattr(o, 'nodeWidth', 130) + 70
        vals = {}
        for kv in parts[2:]:
            k, _, v = kv.partition('=')
            try:
                vals[k] = json.loads(v)
            except ValueError:
                vals[k] = v
        if vals:
            _set_pars(o, vals)
        made[parts[0]] = o
    for n, a, b, index in links:
        src = made.get(a) or _o(p.path.rstrip('/') + '/' + a)
        dst = made.get(b) or _o(p.path.rstrip('/') + '/' + b)
        if index >= len(dst.inputConnectors):
            raise Refused('line %d: %s has %d inputs' % (n, dst.path, len(dst.inputConnectors)))
        dst.inputConnectors[index].connect(src.outputConnectors[0])
    # When the COMP held nothing else, arrange the whole network along the wiring.
    if made and len([c for c in p.children if getattr(c, 'dock', None) is None]) == len(made):
        _layout(made.values())
    return 'made %d: %s; wired %d' % (len(made), ' '.join(o.name for o in made.values()), len(links)) + _hints(made.values())


def _layout(ops, x0=0, y0=0):
    """Place OPs left to right along the wiring, from (x0, y0). Each OP sits in the column just before the
    one it feeds, so a short side chain stays beside what it joins instead of piling up at the far left.
    Unwired OPs go in columns to the right, no taller than the wired part. DATs docked to an OP are tucked
    under it. Returns the width and height used."""
    ops = list(ops)
    inside = set(o.path for o in ops)
    depth = {}

    def walk(o, seen):
        if o.path in depth:
            return depth[o.path]
        if o.path in seen:
            return -1  # a feedback loop: do not follow it
        seen = seen | set([o.path])
        ins = [i for i in o.inputs if i is not None and i.path in inside]
        depth[o.path] = d = 1 + _max([walk(i, seen) for i in ins] or [-1])
        return d

    for o in ops:
        walk(o, set())
    wired = [o for o in ops if [i for i in o.inputs if i is not None and i.path in inside]
             or [x for x in o.outputs if x.path in inside]]
    for o in sorted(wired, key=lambda o: -depth[o.path]):
        outs = [depth[x.path] for x in o.outputs if x.path in inside and depth[x.path] > depth[o.path]]
        if outs:
            depth[o.path] = min(outs) - 1
    loose = [o for o in ops if o not in wired]
    # Cells are as large as the largest node (with its viewer open and its docked DATs), so nothing overlaps.
    docks = _max([len(getattr(o, 'docked', []) or []) for o in ops] or [0])
    cw = _max([220] + [int(getattr(o, 'nodeWidth', 130)) + 90 for o in ops] + [docks * 150 + 60])
    ch = _max([230] + [int(getattr(o, 'nodeHeight', 90)) + 120 + (110 if docks else 0) for o in ops])
    row, bottom = {}, 0
    for d in sorted(set(depth[o.path] for o in wired)):
        col = [o for o in wired if depth[o.path] == d]

        def key(o):
            ys = [row[i.path] for i in o.inputs if i is not None and i.path in row]
            return (sum(ys) / float(len(ys)) if ys else 0, o.name)
        for n, o in enumerate(sorted(col, key=key)):
            row[o.path] = n
            o.nodeX, o.nodeY = x0 + d * cw, y0 - n * ch
            bottom = _max(bottom, n)
    # Unwired OPs go to the right in columns, so the network grows sideways and not downward.
    wide = (_max([depth[o.path] for o in wired] + [0]) + 1) if wired else 0
    tall = _max(bottom + 1 if wired else 0, 3)
    for n, o in enumerate(sorted(loose, key=lambda o: (o.family, o.name))):
        o.nodeX, o.nodeY = x0 + (wide + n // tall) * cw, y0 - (n % tall) * ch
    loose_cols = (len(loose) + tall - 1) // tall
    for o in ops:
        for k, dk in enumerate(getattr(o, 'docked', []) or []):
            dk.nodeX, dk.nodeY = o.nodeX + k * 150, o.nodeY - 110
    rows = _max(bottom + 1 if wired else 0, min(len(loose), tall), 1)
    return _max(wide + loose_cols, 1) * cw, rows * ch


@_undoable
def c_note(path, text=''):
    """Set the comment shown on an OP in the network. Empty text clears it."""
    targets = _os(path)
    for o in targets:
        o.comment = str(text)
    return '%d op(s) noted' % len(targets)


@_undoable
def c_knob(comp, page='Controls', **knobs):
    """Add custom parameters to a COMP, so the values worth changing sit in one place.
    Name=0.5 makes a float. Name=0.5@child.par also drives that parameter from it. Names look like Speed or Haze2."""
    c = _o(comp)
    if not c.isCOMP:
        raise Refused('%s is not a COMP' % c.path)
    if not knobs:
        raise Refused('give at least one Name=value')
    pg = None
    for g in c.customPages:
        if g.name == page:
            pg = g
    out = []
    for name, spec in knobs.items():
        if not re.match(r'^[A-Z][a-z0-9]*$', name):
            raise Refused('bad name %s: one capital letter, then lower case letters or digits (Speed, Haze2)' % name)
        value, _, target = str(spec).partition('@')
        try:
            value = float(value)
        except ValueError:
            raise Refused('%s: the value must be a number, got %s' % (name, value))
        dst = None
        if target:
            tpath, _, tpar = target.rpartition('.')
            t = _o(tpath if tpath.startswith('/') else c.path.rstrip('/') + '/' + tpath)
            dst = _par(t, tpar)
        p = getattr(c.par, name, None)
        if p is None:
            pg = pg or c.appendCustomPage(page)
            p = pg.appendFloat(name, label=name)[0]
            p.normMin, p.normMax = min(0.0, value * 2), _max(1.0, value * 2)
        p.default = value
        p.val = value
        if dst is not None:
            inside = dst.owner.path.rsplit('/', 1)[0] == c.path.rstrip('/')
            dst.expr = ('parent().par.%s' if inside else "op('" + c.path + "').par.%s") % name
            dst.mode = ParMode.EXPRESSION
            out.append('%s=%s -> %s.%s' % (name, _num(value), dst.owner.name, dst.name))
        else:
            out.append('%s=%s' % (name, _num(value)))
    return '%s [%s] %s' % (c.path, page, ' '.join(out))


@_undoable
def _fit_box(box, ops):
    """Size an annotation box around some OPs and whatever is docked to them."""
    ops = list(ops)
    for o in list(ops):
        ops.extend(getattr(o, 'docked', []) or [])
    x0 = min(o.nodeX for o in ops) - 60
    y0 = min(o.nodeY for o in ops) - 60
    x1 = _max(o.nodeX + getattr(o, 'nodeWidth', 130) for o in ops) + 60
    y1 = _max(o.nodeY + getattr(o, 'nodeHeight', 90) for o in ops) + 110
    box.nodeX, box.nodeY, box.nodeWidth, box.nodeHeight = x0, y0, x1 - x0, y1 - y0


def _box_viewer(box):
    """An Annotate COMP shows its own inner panel. Its default path to that panel breaks when the box is
    created or renamed by script, which leaves a warning on it; point it at the panel again."""
    try:
        box.par.opviewer.val = './annotation'
        box.par.reinitextensions.pulse()
    except Exception:
        pass


def _tidy(p):
    kids = [c for c in p.children if getattr(c, 'dock', None) is None and c.OPType != 'annotateCOMP']
    boxes = sorted([b for b in p.children if b.OPType == 'annotateCOMP'], key=lambda b: -(b.nodeY + b.nodeHeight))
    taken, groups = set(), []
    for b in boxes:
        mine = [o for o in kids if o.path not in taken
                and b.nodeX <= o.nodeX + getattr(o, 'nodeWidth', 130) / 2 <= b.nodeX + b.nodeWidth
                and b.nodeY <= o.nodeY + getattr(o, 'nodeHeight', 90) / 2 <= b.nodeY + b.nodeHeight]
        taken.update(o.path for o in mine)
        groups.append((b, mine))
    boxes_in_order = sorted(groups, key=lambda g: (g[0].nodeX, -g[0].nodeY))
    x = 0
    for b, mine in boxes_in_order:
        if not mine:
            continue
        _layout(mine, x, 0)
        _fit_box(b, mine)
        _box_viewer(b)
        x = b.nodeX + b.nodeWidth + 150
    rest = [o for o in kids if o.path not in taken]
    if rest:
        _layout(rest, x, 0)
    return len(kids)


@_undoable
def c_layout(path, deep=False):
    """Arrange the OPs inside a COMP along the wiring, so that none overlap. OPs inside an annotation box
    stay in it: each box is arranged on its own, resized to fit and set to the right of the one before;
    whatever is in no box goes after them. deep also arranges every COMP below this one."""
    p = _o(path)
    if not p.isCOMP:
        raise Refused('%s is not a COMP' % p.path)
    todo = [p]
    if deep:
        todo += [c for c in p.findChildren() if c.isCOMP and c.OPType != 'annotateCOMP'
                 and not any(a.OPType == 'annotateCOMP' for a in _ancestors(c, p)) and c.children]
    n = sum(_tidy(c) for c in todo)
    return 'arranged %d OPs in %s%s' % (n, p.path, ' and %d COMPs below it' % (len(todo) - 1) if deep else '')


def _overlaps(p):
    """Pairs of OPs inside p whose nodes cover each other in the network editor."""
    kids = [c for c in p.children if c.OPType != 'annotateCOMP']
    boxes = [(c, c.nodeX, c.nodeY, c.nodeX + getattr(c, 'nodeWidth', 130), c.nodeY + getattr(c, 'nodeHeight', 90)) for c in kids]
    out = []
    for i, (a, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for b, bx0, by0, bx1, by1 in boxes[i + 1:]:
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                out.append((a.name, b.name))
    return out


def c_overlaps(path='/', deep=True):
    """OPs that lie on top of each other in the network editor, in this COMP and (deep) every COMP below."""
    p = _o(path)
    todo = [p] + ([c for c in p.findChildren() if c.isCOMP and c.OPType != 'annotateCOMP' and c.children] if deep else [])
    lines, total = [], 0
    for c in todo:
        hits = _overlaps(c)
        if hits:
            total += len(hits)
            lines.append('%s  %d: %s%s' % (c.path, len(hits), ', '.join('%s/%s' % h for h in hits[:6]), ' ...' if len(hits) > 6 else ''))
    if not lines:
        return 'no overlapping OPs in %d COMPs' % len(todo)
    return '%d overlapping pairs. `td.py layout COMP` arranges one COMP\n%s' % (total, '\n'.join(lines))


def _up(o):
    p = getattr(o, 'parent', None)
    return p() if callable(p) else p


def _ancestors(o, stop):
    out = []
    o = _up(o)
    while o is not None and o.path != stop.path and o.path != '/':
        out.append(o)
        o = _up(o)
    return out


@_undoable
def c_rename(path, name):
    """Rename an OP. Shader parameters that pointed at a renamed DAT are pointed at the new name."""
    o = _o(path)
    users = _shader_users(o) if getattr(o, 'isDAT', False) else []
    old = o.name
    o.name = name
    for u in users:
        for pn in _SHADER_PARS:
            p = getattr(u.par, pn, None)
            if p is not None and str(p.val) in (old, './' + old):
                p.val = o.name
    return o.path


@_undoable
def c_copy(src, parent, name=None):
    """Copy an OP into another COMP."""
    o = _o(parent).copy(_o(src), name=name) if name else _o(parent).copy(_o(src))
    return '%s %s' % (o.path, o.OPType)


@_undoable
def c_wire(src, dst, index=0, out=0):
    """Wire an output of src to an input of dst."""
    a, b = _o(src), _o(dst)
    if index >= len(b.inputConnectors):
        raise Refused('%s has %d inputs' % (b.path, len(b.inputConnectors)))
    b.inputConnectors[index].connect(a.outputConnectors[out])
    return '%s -> %s[%d]' % (a.path, b.path, index)


@_undoable
def c_unwire(dst, index=0):
    """Disconnect an input of dst."""
    b = _o(dst)
    b.inputConnectors[index].disconnect()
    return 'disconnected %s[%d]' % (b.path, index)


_SHADER_PARS = ('pixeldat', 'vertexdat', 'computedat', 'predat')


def _shader_users(dat):
    """OPs next to a DAT that compile it as a shader."""
    parent = _o(dat.path.rsplit('/', 1)[0] or '/')
    users = []
    for s in parent.children:
        for name in _SHADER_PARS:
            p = getattr(s.par, name, None)
            v = p.eval() if p is not None else None
            if v is not None and (v is dat or getattr(v, 'path', None) == dat.path or v == dat.name):
                users.append(s)
                break
    return users


def _compile_log(o):
    """Compile messages of a GLSL OP, read through a short-lived Info DAT."""
    try:
        parent = _o(o.path.rsplit('/', 1)[0] or '/')
        o.cook(force=True)
        info = parent.create(infoDAT)
        info.par.op.val = o.path
        info.cook(force=True)
        lines = [ln.strip() for ln in info.text.splitlines() if 'ERROR' in ln or 'WARNING' in ln]
        info.destroy()
        return lines
    except Exception:
        return []


@_undoable
def c_write(path, text):
    """Replace the contents of a DAT. A DAT used as a shader is marked as GLSL, docked to the OP that
    compiles it, and compiled at once: compile errors come back in the reply."""
    o = _need(_o(path), 'DAT')
    o.text = text
    out = ['%s %d lines' % (o.path, len(text.splitlines()))]
    users = _shader_users(o)
    if users:
        try:
            o.par.language.val = 'glsl'
            if getattr(o, 'dock', None) is None:
                o.dock = users[0]
        except Exception:
            pass
        for u in users:
            log = _compile_log(u)
            out.append('%s: %s' % (u.name, 'compiled' if not log else 'COMPILE FAILED'))
            out.extend(log[:12])
    return '\n'.join(out)


@_undoable
def c_box(comp, title, *names, **kw):
    """Put a titled box around some OPs of a COMP, after arranging them along their wiring.
    Boxes stack downward in the order they are made, one per stage of the network. body='text' adds a note."""
    c = _o(comp)
    if not c.isCOMP:
        raise Refused('%s is not a COMP' % c.path)
    if not names:
        raise Refused('name the OPs to put in the box')
    ops = []
    for n in names:
        ops.extend(_os(c.path.rstrip('/') + '/' + n))
    boxes = [b for b in c.children if b.OPType == 'annotateCOMP']
    top = min([b.nodeY for b in boxes] or [200]) - 200
    w, hgt = _layout(ops, 0, top - 60)
    box = c.create(annotateCOMP)
    _fit_box(box, ops)
    _box_viewer(box)
    box.par.Titletext.val = str(title)
    try:
        # A freshly made Annotate COMP can point its viewer at a path that does not exist; aim it at its own panel.
        box.par.opviewer.val = './annotation'
        box.par.reinitextensions.pulse()
    except Exception:
        pass
    if kw.get('body'):
        box.par.Bodytext.val = str(kw['body'])
    return 'boxed %d OPs as "%s"' % (len(names), title)


@_undoable
def c_move(path, x, y):
    """Move an OP in the network."""
    o = _o(path)
    o.nodeX, o.nodeY = int(x), int(y)
    return '%s at %d %d' % (o.path, o.nodeX, o.nodeY)


@_undoable
def c_rm(path, yes=False):
    """Delete OPs. The last name may use * ?. Without yes, only reports how many would be deleted."""
    found = [(o, o.path, o.OPType, len(o.findChildren()) if o.isCOMP else 0) for o in _os(path)]
    total = sum(n for _, _, _, n in found)
    if len(found) == 1:
        o, p, kind, n = found[0]
        if not yes:
            return 'would delete %s %s and %d children. add --yes' % (p, kind, n)
        o.destroy()
        return 'deleted %s (+%d children)' % (p, n)
    if not yes:
        return '\n'.join(['would delete %d OPs and %d children. add --yes' % (len(found), total)] +
                         ['%s %s%s' % (p, kind, ' +%d' % n if n else '') for _, p, kind, n in found])
    for o, _, _, _ in found:
        # Deleting an OP takes the OPs docked to it along, so a later match may already be gone.
        if getattr(o, 'valid', True):
            o.destroy()
    return 'deleted %d OPs (+%d children): %s' % (len(found), total, ' '.join(p.rsplit('/', 1)[1] for _, p, _, _ in found))


_LOADER = '/local/touchdesigner_bridge'


@_undoable
def c_install(text):
    """Put the loader Execute DAT in the project. Once saved, just opening the project connects."""
    o = op(_LOADER) or _create(_o('/local'), 'executeDAT', _LOADER.rsplit('/', 1)[1])
    o.text = text
    o.par.start = True
    o.par.active = True
    return 'installed %s. save the project to keep it' % o.path


@_undoable
def c_uninstall():
    """Remove the loader Execute DAT."""
    o = op(_LOADER)
    if o is None:
        return 'not installed'
    o.destroy()
    return 'removed %s. save the project to keep it' % _LOADER


def c_undo():
    """Undo the last change."""
    ui.undo.undo()
    return 'undone'


def c_stop():
    """Stop the bridge."""
    _state.gen += 1
    try:
        os.remove(os.path.join(_out, 'alive.json'))
    except OSError:
        pass
    return 'stopped'


_cmds = {k[2:]: v for k, v in list(globals().items()) if k.startswith('c_') and callable(v)}
_base = dict(globals())


# ---- Receiving and running requests ----

def _write_json(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fp:
        json.dump(data, fp, ensure_ascii=False, default=repr)
    os.replace(tmp, path)


def _reply(rid, t0, buf, result=None, error=None):
    _write_json(os.path.join(_out, rid + '.json'), {
        'ok': error is None, 'result': result, 'error': error,
        'stdout': buf.getvalue(), 'ms': (time.perf_counter() - t0) * 1000.0,
    })


def _advance(job):
    """Advance a generator by one frame. True when it has finished."""
    rid, t0, buf, gen = job
    try:
        with contextlib.redirect_stdout(buf):
            next(gen)
        return False
    except StopIteration as e:
        _reply(rid, t0, buf, result=e.value)
    except Refused as e:
        _reply(rid, t0, buf, error=str(e))
    except Exception:
        _reply(rid, t0, buf, error=traceback.format_exc(limit=4))
    return True


def _start(name):
    path = os.path.join(_in, name)
    with open(path, 'r', encoding='utf-8') as fp:
        src = fp.read()
    os.remove(path)
    rid, ext = os.path.splitext(name)
    buf = io.StringIO()
    t0 = time.perf_counter()
    try:
        with contextlib.redirect_stdout(buf):
            if ext == '.json':
                req = json.loads(src)
                fn = _cmds.get(req['cmd'])
                # --when and --wait belong to the bridge, not to the command. wait is taken only next to when,
                # so a parameter that happens to be called wait can still be set.
                kw = req.get('kw', {})
                when = kw.pop('when', None)
                wait = kw.pop('wait', 60) if when is not None else 60
                if fn is None:
                    raise Refused('unknown command: %s. commands: %s' % (req['cmd'], ' '.join(sorted(_cmds))))
                try:
                    inspect.signature(fn).bind(*req.get('args', []), **kw)
                except TypeError as e:
                    raise Refused('%s: %s. usage: %s%s  %s' % (
                        req['cmd'], e, req['cmd'], inspect.signature(fn), (fn.__doc__ or '').strip()))
                if when is None:
                    result = fn(*req.get('args', []), **kw)
                else:
                    if isinstance(when, bool) or isinstance(wait, bool) or not isinstance(wait, (int, float)):
                        raise Refused('when takes a Python expression and wait a number of seconds: '
                                      '--when "absTime.seconds % 38 > 9" --wait 60')
                    result = _when(str(when), float(wait), fn, req.get('args', []), kw)
            else:
                ns = dict(_base)
                result = _run_script(src, name, ns)
        if inspect.isgenerator(result):
            _pending.append((rid, t0, buf, result))
        else:
            _reply(rid, t0, buf, result=result)
    except Refused as e:
        _reply(rid, t0, buf, error=str(e))
    except Exception:
        _reply(rid, t0, buf, error=traceback.format_exc(limit=4))


def _run_script(src, name, ns):
    """Run a script as one undo step. If it raises part-way, what it had already changed is undone,
    so a failed script leaves the project as it was. Only steps this script added to the undo history are
    undone: some operations close the undo block early, and then a plain undo would take back earlier work."""
    try:
        undo = ui.undo
        before = len(undo.undoStack)
    except (NameError, AttributeError):
        undo = None
    if undo is not None:
        undo.startBlock('touchdesigner-bridge py')
    try:
        exec(compile(src, name, 'exec'), ns)
    except Exception:
        if undo is not None:
            try:
                try:
                    undo.endBlock()
                except Exception:
                    pass
                added = len(undo.undoStack) - before
                for _ in range(max(added, 0)):
                    undo.undo()
                print('script failed: its changes were undone' if added > 0 else
                      'script failed: nothing it did was recorded for undo, so check what it left behind')
            except Exception:
                print('script failed, and its changes could NOT be undone: check the project')
        raise
    if undo is not None:
        try:
            undo.endBlock()
        except Exception:
            pass
    return ns.get('result')


def _tick():
    if _state.gen != _gen:
        return
    try:
        if _pending:
            _pending[:] = [j for j in _pending if not _advance(j)]
        names = sorted(n for n in os.listdir(_in) if n.endswith(('.json', '.py')))
        for name in names:
            _start(name)
        _ticks[0] += 1
        if _ticks[0] % _HEARTBEAT_FRAMES == 1:
            _write_json(os.path.join(_out, 'alive.json'), {
                'time': time.time(), 'frame': absTime.frame, 'toe': project.name, 'build': app.build,
                'pid': os.getpid(), 'bin': getattr(app, 'binFolder', ''),
            })
    except Exception:
        print('touchdesigner-bridge:', traceback.format_exc())
    if _state.gen == _gen:
        _again()


def _again():
    # The string form of run works on old builds too. Builds that do not know delayRef are called without it.
    try:
        run("args[0]()", _tick, delayFrames=1, delayRef=op.TDResources)
    except TypeError:
        run("args[0]()", _tick, delayFrames=1)


print('touchdesigner-bridge: watching', _in)
_tick()
