#!/usr/bin/env python3
"""Checks that need a real TouchDesigner. Run with a TD that has the bridge loaded:

  python3 td.py open scratch.toe
  python3 tests/live.py [--to NAME] [--seeds 12] [--keep]

Everything happens inside one COMP made for it at the root (/bridge_live_check), which is removed at the end
(--keep leaves it for looking at). The network editor is pointed at that COMP by the shot check.
test_bridge.py covers the logic against a stand-in; this covers what only the real thing shows: node sizes,
docked DATs, connectors, undo, the window picture.
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import td  # noqa: E402

ROOT = '/bridge_live_check'
failures, checks = [], [0]


def arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith('--') else True
    return default


D, WHY = td.pick(td.queue_dir(), arg('--to', os.environ.get('TOUCHDESIGNER_BRIDGE_TO')))


def cmd(name, *args, **kw):
    out = td.send(D, '.json', json.dumps({'cmd': name, 'args': list(args), 'kw': kw}), 60)
    if out is None:
        raise SystemExit('timeout on %s' % name)
    return out


def ok(name, *args, **kw):
    out = cmd(name, *args, **kw)
    if not out['ok']:
        raise AssertionError('%s %s: %s' % (name, args, out['error']))
    return out['result']


def py(code):
    out = td.send(D, '.py', code, 60)
    if out is None or not out['ok']:
        raise AssertionError('py failed: %s' % (out or {}).get('error', 'timeout'))
    return out['result']


def check(what, cond, detail=''):
    checks[0] += 1
    if not cond:
        failures.append('%s %s' % (what, detail))
        print('FAIL', what, detail)


def nodes(comp):
    """name -> (x, y, w, h, inputs, dock, type) of every child that is drawn as a node (a docked DAT folded
    away under its owner is an icon, and is left out)."""
    rows = py('result = [[c.name, c.nodeX, c.nodeY, c.nodeWidth, c.nodeHeight, [i.name for i in c.inputs if i is not None], '
              'c.dock.name if c.dock else None, c.OPType] for c in op(%r).children if c.dock is None or c.showDocked]' % comp)
    return dict((r[0], tuple(r[1:])) for r in rows)


def geometry(tag, comp, allow_back=()):
    """No node on another, no wire right to left, no wire along a row behind a node."""
    n = nodes(comp)
    real = dict((k, v) for k, v in n.items() if v[6] != 'annotateCOMP')
    names = sorted(real)
    for i, a in enumerate(names):
        ax, ay, aw, ah = real[a][:4]
        for b in names[i + 1:]:
            bx, by, bw, bh = real[b][:4]
            check(tag + ': overlap', not (ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah), '%s/%s' % (a, b))
    for name, (x, y, w, h, ins, dock, kind) in real.items():
        for i in ins:
            ix, iy, iw, ih = real[i][:4]
            if allow_back is not True and (i, name) not in allow_back:
                check(tag + ': wire right to left', ix + iw <= x, '%s -> %s' % (i, name))
            if iy + ih // 2 == y + h // 2:
                for k, (kx, ky, kw, kh, _, kdock, _) in real.items():
                    hit = k not in (i, name) and ix < kx < x and ky < y + h // 2 < ky + kh
                    check(tag + ': wire behind a node', not hit, '%s -> %s behind %s' % (i, name, k))
    said = ok('overlaps', comp, deep=False)
    check(tag + ': overlaps agrees', 'no overlapping' in said and ('right to left' not in said or bool(allow_back)), said)
    return n


SOURCES = ['noiseTOP', 'rampTOP', 'constantTOP']
FILTERS = ['nullTOP', 'levelTOP', 'blurTOP', 'hsvadjustTOP', 'glslTOP', 'compositeTOP', 'compositeTOP']


def random_spec(r, n, prefix='n'):
    """A random network that TD accepts: generators where nothing comes in, filters where something does."""
    feeds, firsts = {}, []
    for k in range(1, n):
        if r.random() < 0.85:
            a = r.randrange(max(0, k - r.choice([1, 1, 1, 2, 4, 8])), k)
            feeds[k] = [a]
            firsts.append((a, k))
    kinds = [r.choice(FILTERS) if k in feeds else r.choice(SOURCES) for k in range(n)]
    for k in feeds:
        if kinds[k] == 'compositeTOP':
            # a Composite with one input is an error in TD: it gets a second, or becomes a Level
            others = [b for b in range(k) if b != feeds[k][0]]
            if others:
                feeds[k].append(r.choice(others))
            else:
                kinds[k] = 'levelTOP'
    lines = ['%s%d %s' % (prefix, k, kinds[k]) for k in range(n)]
    links = ['%s%d -> %s%d%s' % (prefix, a, prefix, k, '[%d]' % i if i else '') for k in sorted(feeds) for i, a in enumerate(feeds[k])]
    return '\n'.join(lines + links) + '\n', firsts


def forget(comp):
    """Make the bridge treat what is in a COMP as someone else's."""
    py("import sys\ns = sys.modules['_touchdesigner_bridge_state']\n"
       "for c in op(%r).children:\n    s.placed.pop(c.id, None)\nresult = 1" % comp)


def random_networks(seeds):
    for size in (6, 25, 60):
        for seed in range(seeds):
            r = random.Random(seed * 1000 + size)
            comp = '%s/r%d_%d' % (ROOT, size, seed)
            ok('new', ROOT, 'baseCOMP', comp.rsplit('/', 1)[1])
            spec, firsts = random_spec(r, size)
            said = ok('build', comp, spec=spec)
            check('build %d/%d: the reply names no trouble' % (size, seed), '\nE ' not in said and '\nW ' not in said, said[-200:])
            geometry('build %d/%d' % (size, seed), comp)
            said = ok('err', comp)
            check('build %d/%d: no errors or warnings' % (size, seed), said == 'no errors, no warnings', said[:200])
            # someone else's from here on: add to it without moving it
            forget(comp)
            before = nodes(comp)
            a, b = r.choice(firsts) if firsts else (0, 0)
            adds = ['t0 nullTOP\nt1 levelTOP\nn%d -> t0\nt0 -> t1\n' % r.randrange(size), 'loose0 noiseTOP\n']
            if firsts:
                adds.append('mid0 levelTOP\nn%d -> mid0\nmid0 -> n%d\n' % (a, b))
            for spec2 in adds:
                said = ok('build', comp, spec=spec2)
                now = nodes(comp)
                back = set()
                for ln in said.splitlines():
                    if 'runs right to left' in ln:
                        x, _, y = ln.split('note  ')[1].split(' runs')[0].partition(' -> ')
                        back.add((x, y))
                for name, v in before.items():
                    same_row = now[name][1] == v[1]
                    check('add %d/%d: only moved right, and said so' % (size, seed),
                          same_row and (now[name][0] == v[0] or (now[name][0] > v[0] and 'made room' in said)),
                          '%s %s -> %s' % (name, v[:2], now[name][:2]))
                old = set((i, k) for k, v in before.items() for i in v[4] if i in before)
                # a wire between two OPs that were both there is the owner's business, unless it was just cut
                geometry('add %d/%d' % (size, seed), comp, allow_back=back | old)
                before = now
            refused = cmd('layout', comp)
            check('layout refuses someone else\'s network', not refused['ok'] and 'not placed by td.py' in (refused['error'] or ''), str(refused)[:120])
            check('nothing moved by the refusal', dict((k, v[:2]) for k, v in nodes(comp).items()) == dict((k, v[:2]) for k, v in before.items()))
            # scattered, then tidied because it was asked for
            py('import random\nr = random.Random(%d)\nfor c in op(%r).children:\n    if c.dock is None:\n'
               '        c.nodeX, c.nodeY = r.randrange(-3000, 3000), r.randrange(-3000, 3000)\nresult = 1' % (seed, comp))
            messy = nodes(comp)
            free = [v for v in messy.values() if v[5] is None]
            corner = (min(v[0] for v in free), max(v[1] + v[3] for v in free))
            ok('layout', comp, yes=True)
            tidy = geometry('tidy %d/%d' % (size, seed), comp)
            free = [v for v in tidy.values() if v[5] is None]
            check('tidy keeps the corner', (min(v[0] for v in free), max(v[1] + v[3] for v in free)) == corner)
            ok('layout', comp)
            check('layout twice gives the same', dict((k, v[:2]) for k, v in nodes(comp).items()) == dict((k, v[:2]) for k, v in tidy.items()))
            ok('undo')
            ok('undo')
            check('undo puts the scattered network back', dict((k, v[:2]) for k, v in nodes(comp).items()) == dict((k, v[:2]) for k, v in messy.items()))
            ok('rm', comp, yes=True)


def boxes():
    comp = ROOT + '/boxes'
    ok('new', ROOT, 'baseCOMP', 'boxes')
    ok('build', comp, spec='a noiseTOP\nb levelTOP\nc blurTOP\nd nullTOP\ne rampTOP\nf lookupTOP\ng nullTOP\n'
                           'a -> b\nb -> c\nc -> d\nd -> f\ne -> f[1]\nf -> g\n')
    ok('box', comp, '1 Source', 'a', 'b')
    geometry('one box', comp, allow_back=True)
    ok('layout', comp)
    n = geometry('one box, layout', comp)
    box = [v for v in n.values() if v[6] == 'annotateCOMP'][0]
    inside = lambda v: box[0] < v[0] and v[0] + v[2] < box[0] + box[2] and box[1] < v[1] and v[1] + v[3] < box[1] + box[3]
    check('box holds its OPs and only those', inside(n['a']) and inside(n['b']) and not any(inside(n[k]) for k in 'cdefg'))
    ok('box', comp, '2 Shape', 'c', 'd')
    ok('box', comp, '3 Colour', 'e', 'f', 'g')
    check('boxes: no errors or warnings', ok('err', comp) == 'no errors, no warnings', ok('err', comp))
    ok('layout', comp)
    n = nodes(comp)
    got = sorted([v for v in n.values() if v[6] == 'annotateCOMP'], key=lambda v: -v[1])
    check('three boxes', len(got) == 3)
    check('stacked boxes stay stacked and apart', all(got[k][1] > got[k + 1][1] + got[k + 1][3] for k in range(len(got) - 1)))
    said = ok('overlaps', comp, deep=False)
    check('boxes: nothing overlaps', 'no overlapping' in said, said)
    # someone else's OPs get a box where they are
    forget(comp)
    before = nodes(comp)
    ok('box', comp, 'Theirs', 'f', 'g')
    now = nodes(comp)
    check('box leaves their OPs in place', all(now[k][:2] == before[k][:2] for k in before))
    ok('rm', comp, yes=True)


def commands():
    comp = ROOT + '/cmds'
    ok('new', ROOT, 'baseCOMP', 'cmds')
    said = ok('build', comp, spec='noise_src noiseTOP\nlevel_pulse levelTOP brightness1==op("lfo_pulse")["chan1"]\nlfo_pulse lfoCHOP\n'
                                  'null_final nullTOP\nnoise_src -> level_pulse\nlevel_pulse -> null_final\n')
    n = geometry('commands', comp)
    check('the LFO sits just left of the OP that reads it, a row down', n['lfo_pulse'][0] + n['lfo_pulse'][2] <= n['level_pulse'][0]
          and n['level_pulse'][0] - n['lfo_pulse'][0] <= 400 and n['lfo_pulse'][1] < n['level_pulse'][1])
    for k in range(3):
        ok('new', comp, 'noiseTOP')
    said = ok('new', comp, 'nullTOP', 'null_alone')
    check('new reports the error of what it made', 'E null_alone: Not enough sources specified' in said, said)
    ok('rm', comp + '/null_alone', yes=True)
    ok('copy', comp + '/noise_src', comp, 'noise_copy')
    geometry('new and copy', comp)
    check('ls --pos', '  at ' in ok('ls', comp, pos=True))
    # an OP nothing looks at has not cooked, so only the command that makes it can say what is wrong with it
    said = ok('build', comp, spec='comp_alone compositeTOP\nnoise_src -> comp_alone\n')
    check('build reports the error of what it made', 'E comp_alone: Not enough sources specified' in said, said)
    said = ok('wire', comp + '/level_pulse', comp + '/comp_alone', index=1)
    check('and wire says nothing once it is put right', '\nE ' not in said, said)
    ok('rm', comp + '/comp_alone', yes=True)
    # an expression may read a channel that exists only once the spec is made and wired
    said = ok('build', comp, spec='xf_spin transformTOP rotate==op("speed_spin")["angle"]\nconst_speed constantCHOP name0=angle value0=6\n'
                                  'speed_spin speedCHOP\nnoise_spin noiseTOP\nnoise_spin -> xf_spin\nconst_speed -> speed_spin\n')
    check('build takes an expression on a channel made by the same spec', 'made 4' in said and '\nE ' not in said, said)
    # a spec that fails part-way leaves nothing: no OPs, and the wire it was cutting is as it was
    had = nodes(comp)
    refused = cmd('build', comp, spec='mid_a levelTOP\nmid_b levelTOP opacity==op("nothing_there")["x"]\n'
                                      'noise_spin -> mid_a\nmid_a -> mid_b\nmid_b -> xf_spin\n')
    check('a build that fails says so in one line', not refused['ok'] and 'Nothing was made' in refused['error']
          and 'Traceback' not in refused['error'], str(refused)[:240])
    now = nodes(comp)
    check('and leaves nothing behind', sorted(now) == sorted(had) and now['xf_spin'][4] == had['xf_spin'][4], '%s %s' % (sorted(set(now) - set(had)), now['xf_spin'][4]))
    # one spec put into two wires of someone else's network: both sit on their wires, no wire turns back
    ok('new', comp, 'baseCOMP', 'two')
    ok('build', comp + '/two', spec='a noiseTOP\nb levelTOP\nc levelTOP\nd nullTOP\na -> b\nb -> c\nc -> d\n')
    forget(comp + '/two')
    said = ok('build', comp + '/two', spec='p blurTOP\nq hsvadjustTOP\na -> p\np -> b\nc -> q\nq -> d\n')
    n = geometry('two insertions', comp + '/two')
    check('both are on the row of their wire', len(set(n[k][1] for k in 'apbcqd')) == 1 and said.count('made room') == 2, said)
    ok('rm', comp + '/two', yes=True)
    refused = cmd('build', comp, spec='x nullTOP\nnoise_src -> x\nlevel_pulse -> x\n')
    check('two wires into one input are refused', not refused['ok'] and 'already wired' in refused['error'])
    check('and nothing was made', 'x' not in nodes(comp))
    refused = cmd('set', comp + '/level_pulse', brightness1='=op("nothing_there")["chan1"]')
    check('an expression that fails is refused in one line', not refused['ok'] and 'was not set' in refused['error']
          and 'Traceback' not in refused['error'], str(refused)[:200])
    check('and the parameter is as it was', 'lfo_pulse' in ok('par', comp + '/level_pulse', 'brightness1'))
    check('no error is left behind', ok('err', comp) == 'no errors, no warnings', ok('err', comp))
    # the same failing expression put there behind the bridge's back is something err has to see
    py("p = op(%r).par.opacity\np.expr = 'op(\"nothing_there\")[0]'\np.mode = ParMode.EXPRESSION\nresult = 1" % (comp + '/level_pulse'))
    py("op(%r).cook(force=True)\nresult = 1" % (comp + '/null_final'))
    said = ok('err', comp)
    check('err sees an expression that fails', said != 'no errors, no warnings', said)
    py("p = op(%r).par.opacity\np.mode = ParMode.CONSTANT\np.val = 1\nresult = 1" % (comp + '/level_pulse'))
    said = ok('compare', comp + '/null_final', comp + '/level_pulse', bypass=1)
    check('compare takes a flag', 'B = with bypass=1' in said, said[:80])
    check('the flag is put back', py('result = op(%r).bypass' % (comp + '/level_pulse')) is False)
    ok('knob', comp, Glow='0.6@level_pulse.opacity')
    check('par shows a knob at its first value', 'Glow=0.6' in ok('par', comp))
    check('doc --grep takes alternatives', 'hue' in ok('doc', 'hsvadjustTOP', grep='hue offset|nothing like this').lower())
    code, said = td.shot(D, comp, None, 60)
    picture = td.to_local(said) if code == 0 else said
    good = code == 0 and os.path.exists(picture) and os.path.getsize(picture) > 20000
    if good:
        with open(picture, 'rb') as fp:
            good = fp.read(8) == b'\x89PNG\r\n\x1a\n'
    check('shot saves a picture of the window', good, '%s %s' % (code, said))
    ok('rm', comp, yes=True)


def main():
    if D is None:
        raise SystemExit(WHY)
    seeds = int(arg('--seeds', 12))
    if not cmd('ls', ROOT)['ok']:
        ok('new', '/', 'baseCOMP', ROOT[1:])
    try:
        for part in (commands, boxes, lambda: random_networks(seeds)):
            try:
                part()
            except AssertionError as e:
                failures.append(str(e))
                print('FAIL', e)
    finally:
        if not arg('--keep'):
            cmd('rm', ROOT, yes=True)
    print('%d checks, %d failures' % (checks[0], len(failures)))
    for f in failures[:40]:
        print('  ' + f)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
