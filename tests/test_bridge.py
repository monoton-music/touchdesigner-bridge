import json
import os
import sys
import subprocess
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import fake_td  # noqa: E402
import td  # noqa: E402


def build_world():
    w = fake_td.World()
    p = fake_td.OP('project1', 'containerCOMP', 'COMP', w.root)
    noise = fake_td.OP('noise1', 'noiseTOP', 'TOP', p, cooks_every_frame=True, cpuCookTime=0.5, gpuCookTime=2.0)
    noise.addPar('resolutionw', 1920, default=256)
    noise.addPar('seed', 1, style='Int')
    noise.addPar('type', 'sparse', label='Type', style='Menu', menuNames=['sparse', 'hermite', 'random'], page='Noise')
    noise.addPar('reset', 0, label='Reset', style='Pulse')
    noise.gpuMemory = 8e6
    noise.pixel = (0.25, 0.5, 1.0, 1.0)
    noise.addPar('tx', 0.25, default=0, expr='absTime.seconds')
    slow = fake_td.OP('script1', 'scriptCHOP', 'CHOP', p, cooks_every_frame=True, cpuCookTime=8.0)
    slow._chans = [fake_td.Chan('a', [0.5]), fake_td.Chan('b', [2])]
    slow._warnings = 'script1: slow thing'
    idle = fake_td.OP('null1', 'nullTOP', 'TOP', p, cpuCookTime=99.0)
    idle.inputs = [noise]
    noise.outputs = [idle]
    code = fake_td.OP('text1', 'textDAT', 'DAT', p)
    code.text = 'import os\nprint("hello")\nvalue = 3\n'
    inner = fake_td.OP('fx', 'baseCOMP', 'COMP', p, allowCooking=False, cooks_every_frame=True, childrenCPUCookTime=3.0)
    bad = fake_td.OP('glsl1', 'glslTOP', 'TOP', inner, bypass=True)
    bad._errors = 'glsl1: compile failed'
    return w


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'q')
        os.environ['TOUCHDESIGNER_BRIDGE_DIR'] = self.dir
        self.w = build_world()
        self.load()
        self.stop = False
        self.thread = threading.Thread(target=self.pump, daemon=True)
        self.thread.start()

    def load(self):
        with open(os.path.join(ROOT, 'touchdesigner_bridge.py'), encoding='utf-8') as fp:
            self.ns = dict(self.w.globals)      # the bridge's own names, for tests that replace one of them
            exec(compile(fp.read(), 'touchdesigner_bridge.py', 'exec'), self.ns)

    def pump(self):
        while not self.stop:
            self.w.step()
            time.sleep(0.001)

    def tearDown(self):
        self.stop = True
        self.thread.join()
        self.tmp.cleanup()

    def qdir(self):
        d, why = td.pick(self.dir)
        self.assertIsNotNone(d, why)
        return d

    def cmd(self, name, *args, **kw):
        out = td.send(self.qdir(), '.json', json.dumps({'cmd': name, 'args': list(args), 'kw': kw}), 5)
        self.assertIsNotNone(out, 'timeout')
        return out

    def ok(self, name, *args, **kw):
        out = self.cmd(name, *args, **kw)
        self.assertTrue(out['ok'], out['error'])
        return out['result']

    def test_loop_survives_paused_timeline(self):
        # With the default delayRef (root), delays do not advance while the timeline is paused.
        self.assertTrue(self.w.run_calls)
        for c in self.w.run_calls:
            self.assertIs(c['delayRef'], self.w.tdresources)
            self.assertEqual(c['delayFrames'], 1)

    def test_ls(self):
        r = self.ok('ls', '/project1')
        self.assertIn('noise1 noiseTOP', r)
        self.assertIn('fx baseCOMP [1] X', r)
        self.assertNotIn('glsl1', r)
        self.assertIn('fx/glsl1 glslTOP B', self.ok('ls', '/project1', depth=2))
        self.assertEqual(self.ok('ls', '/project1', type='*CHOP'), 'script1 scriptCHOP')

    def test_ls_limit(self):
        r = self.ok('ls', '/project1', limit=2)
        self.assertEqual(len(r.splitlines()), 3)
        self.assertIn('+3 more', r)

    def test_par_shows_only_non_default(self):
        r = self.ok('par', '/project1/noise1')
        self.assertIn('resolutionw=1920', r)
        self.assertIn('tx=0.25  expr: absTime.seconds', r)
        self.assertNotIn('seed', r)
        self.assertIn('seed=1', self.ok('par', '/project1/noise1', all=True))

    def test_set(self):
        self.assertEqual(self.ok('set', '/project1/noise1', seed=7), 'seed=7')
        self.assertIn('seed=7', self.ok('par', '/project1/noise1'))
        self.ok('set', '/project1/noise1', seed='=me.digits')
        self.assertIn('expr: me.digits', self.ok('par', '/project1/noise1'))
        self.assertFalse(self.cmd('set', '/project1/noise1', nope=1)['ok'])

    def test_err(self):
        r = self.ok('err', '/')
        self.assertIn('E glsl1: compile failed', r)
        self.assertIn('W script1: slow thing', r)
        self.assertEqual(self.ok('err', '/project1/null1'), 'no errors, no warnings')

    def test_err_groups_repeated_messages(self):
        p = self.w.globals['op']('/project1')
        for i in range(3):
            k = fake_td.OP('knob%d' % i, 'glslTOP', 'TOP', p)
            k._warnings = "/project1/knob%d:Warning: Uniform 'rot' is not assigned. (/project1/knob%d)" % (i, i)
        self.assertIn("W x3 Uniform 'rot' is not assigned.  @ /project1/knob0", self.ok('err', '/'))

    def test_mem(self):
        r = self.ok('mem', '/').splitlines()
        self.assertEqual(r[0], 'total gpu 8 MB cpu 0 MB in 1 ops')
        self.assertEqual(r[2], '8.0 0.0 /project1/noise1 noiseTOP')

    def test_parinfo(self):
        r = self.ok('parinfo', '/project1/noise1')
        self.assertIn('type "Type" Menu @Noise menu: sparse|hermite|random default=sparse', r)
        self.assertIn('seed "Seed" Int @Common 0..1 default=1', r)

    def test_types(self):
        self.assertEqual(self.ok('types', 'noise*'), 'noiseCHOP noiseTOP')

    def test_doc(self):
        folder = os.path.join(self.tmp.name, 'Learn', 'OfflineHelp', 'https.docs.derivative.ca')
        os.makedirs(folder)
        with open(os.path.join(folder, 'Noise_TOP.htm'), 'w', encoding='utf-8') as fp:
            fp.write('<html><div id="mw-content-text"><p>Makes noise.</p><p>Seed <b>seed</b> &amp; more</p></div>'
                     '<div class="printfooter">x</div></html>')
        self.w.globals['app'].samplesFolder = self.tmp.name
        self.load()
        self.assertEqual(self.ok('doc', 'noiseTOP', grep='seed'), '2: Seed seed & more')
        self.assertIn('near: Noise_TOP', self.ok('doc', 'noise'))

    def test_pix_and_geo(self):
        self.assertEqual(self.ok('pix', '/project1/noise1'), '1280x720 uv 0.5 0.5 rgba 0.25 0.5 1 1')
        self.assertEqual(self.cmd('geo', '/project1/noise1')['error'], '/project1/noise1 is a noiseTOP, not a SOP or POP')

    def test_new_wire_write_rm_are_undo_steps(self):
        self.assertEqual(self.ok('new', '/project1', 'levelTOP', 'lv', x=100, y=-50), '/project1/lv levelTOP')
        self.assertEqual(self.ok('new', '/project1', 'noiseTOP'), '/project1/noise2 noiseTOP')
        self.assertEqual(self.ok('wire', '/project1/noise1', '/project1/lv'), '/project1/noise1 -> /project1/lv[0]')
        self.assertEqual(self.ok('conn', '/project1/lv'), 'in: /project1/noise1\nout: -')
        self.ok('wire', '/project1/noise2', '/project1/lv')
        self.assertEqual(self.ok('conn', '/project1/lv'), 'in: /project1/noise2\nout: -')
        self.ok('unwire', '/project1/lv')
        self.assertEqual(self.ok('conn', '/project1/lv'), 'in: -\nout: -')
        self.assertEqual(self.ok('write', '/project1/text1', text='a\nb'), '/project1/text1 2 lines')
        self.assertEqual(self.cmd('write', '/project1/lv', text='x')['error'], '/project1/lv is a levelTOP, not a DAT')
        self.assertEqual(self.cmd('chop', '/project1/lv')['error'], '/project1/lv is a levelTOP, not a CHOP')
        self.assertEqual(self.ok('move', '/project1/lv', 10, 20), '/project1/lv at 10 20')
        self.assertEqual(self.ok('copy', '/project1/noise1', '/project1/fx', 'n'), '/project1/fx/n noiseTOP')
        self.assertIn('would delete /project1/fx baseCOMP and 2 children', self.ok('rm', '/project1/fx'))
        self.assertIn('fx baseCOMP', self.ok('ls', '/project1'))
        self.assertEqual(self.ok('rm', '/project1/fx', yes=True), 'deleted /project1/fx (+2 children)')
        self.assertNotIn('fx baseCOMP', self.ok('ls', '/project1'))
        # A pattern deletes every match, and a command that takes one OP refuses a pattern.
        self.ok('new', '/project1', 'nullTOP', 'tmp1')
        self.ok('new', '/project1', 'baseCOMP', 'tmp2')
        self.ok('copy', '/project1/noise1', '/project1/tmp2', 'n')
        said = self.ok('rm', '/project1/tmp*')
        self.assertTrue(said.startswith('would delete 2 OPs and '), said)
        self.assertIn('/project1/tmp1 nullTOP', said)
        self.assertIn('tmp1 nullTOP', self.ok('ls', '/project1'))
        self.assertTrue(self.ok('rm', '/project1/tmp*', yes=True).startswith('deleted 2 OPs (+'))
        self.assertNotIn('tmp', self.ok('ls', '/project1'))
        self.assertEqual(self.cmd('rm', '/project1/tmp*', yes=True)['error'], 'nothing matches /project1/tmp*')
        self.assertIn('is a pattern, and this command takes one OP', self.cmd('op', '/project1/noise*')['error'])
        blocks = [b for b in self.w.undo_blocks if b != 'end']
        self.assertEqual(blocks[0], 'touchdesigner-bridge new')
        self.assertEqual(self.w.undo_blocks.count('end'), len(blocks))
        self.assertFalse(self.cmd('new', '/project1', 'nopeTOP')['ok'])
        self.assertEqual(self.w.undo_blocks.count('end'), len(blocks) + 1)  # the block is closed even on failure

    def test_flag_pulse_undo(self):
        self.assertEqual(self.ok('flag', '/project1/noise1', bypass=1), '1 op(s): bypass=1')
        self.assertIn('noise1 noiseTOP B', self.ok('ls', '/project1'))
        self.assertFalse(self.cmd('flag', '/project1/noise1', nope=1)['ok'])
        self.assertEqual(self.ok('pulse', '/project1/noise1', 'reset'), 'pulsed reset')
        self.assertEqual(self.ok('undo'), 'undone')
        self.assertEqual(self.w.undone, 1)

    def test_find_text_chop_conn(self):
        self.assertEqual(self.ok('find', '/', type='glsl*'), '/project1/fx/glsl1 glslTOP')
        self.assertEqual(self.ok('find', '/', text='hello'), '/project1/text1 textDAT')
        self.assertEqual(self.ok('text', '/project1/text1', grep='value'), '3: value = 3')
        self.assertIn('a=0.5\nb=2', self.ok('chop', '/project1/script1'))
        self.assertEqual(self.ok('conn', '/project1/noise1'), 'in: -\nout: /project1/null1')

    def test_top_saves_image(self):
        r = self.ok('top', '/project1/noise1')
        self.assertTrue(r.startswith('1280x720 saved: '))
        self.assertTrue(r.endswith('project1_noise1.png'))

    def test_errors_say_how_to_fix_the_call(self):
        self.assertEqual(self.cmd('par', '/project1/noise')['error'],
                         'no such OP: /project1/noise. /project1 has: noise1')
        self.assertEqual(self.cmd('set', '/project1/noise1', sed=1)['error'],
                         'no such par: sed on /project1/noise1. near: seed reset (td.py parinfo /project1/noise1)')
        self.assertIn('unknown command: nope. commands: box build chop', self.cmd('nope')['error'])
        self.assertIn('usage: par(path', self.cmd('par')['error'])
        self.assertIn('near: noiseTOP noiseCHOP', self.cmd('new', '/project1', 'noisTOP')['error'])
        self.assertNotIn('Traceback', self.cmd('par', '/nope')['error'])

    def test_glob_paths(self):
        p = self.w.globals['op']('/project1')
        fake_td.OP('noise2', 'noiseTOP', 'TOP', p).addPar('seed', 1, style='Int')
        self.assertEqual(self.ok('set', '/project1/noise*', seed=9),
                         '/project1/noise1 seed=9\n/project1/noise2 seed=9')
        self.assertEqual(self.ok('par', '/project1/noise?', 'seed').count('seed=9'), 2)
        self.assertEqual(self.ok('flag', '/project1/noise*', bypass=1), '2 op(s): bypass=1')
        self.assertEqual(self.cmd('par', '/project1/zzz*')['error'], 'nothing matches /project1/zzz*')

    def test_op_summary_and_flow(self):
        r = self.ok('op', '/project1/noise1').splitlines()
        self.assertEqual(r[0], '/project1/noise1 noiseTOP at 0 0 1280x720')
        self.assertEqual(r[1:3], ['in: -', 'out: /project1/null1'])
        self.assertIn('resolutionw=1920', r)
        f = self.ok('flow', '/project1').splitlines()
        self.assertEqual(f[0], 'noise1 -> null1')
        self.assertEqual(f[-1], 'unwired: script1 text1 fx')

    def test_find_by_expression(self):
        self.assertEqual(self.ok('find', '/', expr='absTime'), '/project1/noise1 noiseTOP  tx')

    def test_build_makes_and_wires_in_one_step(self):
        spec = 'n noiseTOP seed=4\nlv levelTOP  # comment\nout nullTOP\nn -> lv\nlv -> out\nnoise1 -> out[1]\n'
        self.assertEqual(self.ok('build', '/project1', spec=spec), 'made 3: n lv out; wired 3')
        self.assertEqual(self.ok('conn', '/project1/out'), 'in: /project1/lv /project1/noise1\nout: -')
        self.assertIn('seed=4', self.ok('par', '/project1/n'))
        self.assertEqual([b for b in self.w.undo_blocks if b != 'end'], ['touchdesigner-bridge build'])
        self.assertIn('line 1: expected', self.cmd('build', '/project1', spec='oops')['error'])
        self.assertEqual(self.ok('rename', '/project1/out', 'final'), '/project1/final')

    def test_created_ops_show_their_viewer(self):
        self.ok('new', '/project1', 'nullTOP', 'v1')
        self.assertTrue(self.w.globals['op']('/project1/v1').viewer)

    def test_build_comment_note_and_knob(self):
        self.ok('new', '/', 'baseCOMP', 'k')
        self.ok('build', '/k', spec='n noiseTOP seed=2  # the source pattern\n')
        op = self.w.globals['op']
        self.assertEqual(op('/k/n').comment, 'the source pattern')
        self.ok('note', '/k/n', 'changed')
        self.assertEqual(op('/k/n').comment, 'changed')
        self.assertEqual(self.ok('knob', '/k', Seed='5@n.seed', Gain=0.25),
                         '/k [Controls] Seed=5 -> n.seed Gain=0.25')
        self.assertEqual(op('/k').par.Seed.val, 5.0)
        self.assertEqual(op('/k/n').par.seed.expr, 'parent().par.Seed')
        self.assertEqual([g.name for g in op('/k').customPages], ['Controls'])
        self.assertIn('bad name', self.cmd('knob', '/k', speed=1)['error'])
        self.assertIn('error', self.cmd('knob', '/k', Zed='1@n.nope'))

    def test_layout_follows_the_wiring(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        # A feedback loop (fb reads back from out) must not stop the layout.
        spec = 'a noiseTOP\nfb nullTOP\nmix levelTOP\nout nullTOP\nnote textDAT\na -> mix\nfb -> mix[1]\nmix -> out\nout -> fb\n'
        self.ok('build', '/c', spec=spec)
        at = dict((n, (self.w.globals['op']('/c/' + n).nodeX, self.w.globals['op']('/c/' + n).nodeY))
                  for n in ('a', 'fb', 'mix', 'out', 'note'))
        self.assertLess(at['a'][0], at['mix'][0])
        self.assertLess(at['mix'][0], at['out'][0])
        self.assertEqual(len(set(at.values())), 5)
        # the unwired DAT goes to the right of the wired part, not underneath it
        self.assertGreater(at['note'][0], max(at[n][0] for n in ('a', 'fb', 'mix', 'out')))
        self.assertEqual(self.ok('layout', '/c'), 'arranged 5 OPs in /c')

    def test_write_treats_a_shader_as_a_shader(self):
        self.ok('new', '/', 'glslTOP', 'look')
        self.ok('new', '/', 'textDAT', 'look_px')
        self.ok('new', '/', 'textDAT', 'notes')
        op = self.w.globals['op']
        self.ok('set', '/look', pixeldat='look_px')
        self.assertEqual(self.ok('write', '/look_px', 'void main(){}'), '/look_px 1 lines\nlook: compiled')
        self.assertEqual(op('/look_px').par.language.val, 'glsl')
        self.assertIs(op('/look_px').dock, op('/look'))
        op('/look').compile_log = "Pixel Shader Compile Results:\nERROR: /look_px:1: 'oops' : undeclared identifier\n"
        out = self.ok('write', '/look_px', 'void main(){ oops }')
        self.assertIn('look: COMPILE FAILED', out)
        self.assertIn("'oops' : undeclared identifier", out)
        # The temporary Info DAT is gone, and a plain DAT is left alone.
        self.assertEqual(sorted(c.name for c in self.w.root.children), ['look', 'look_px', 'notes', 'project1'])
        self.assertEqual(self.ok('write', '/notes', 'hello'), '/notes 1 lines')
        self.assertEqual(op('/notes').par.language.val, 'input')

    def test_rename_keeps_the_shader_reference(self):
        self.ok('new', '/', 'glslTOP', 'look')
        self.ok('new', '/', 'textDAT', 'old_px')
        self.ok('set', '/look', pixeldat='old_px')
        self.ok('rename', '/old_px', 'look_px')
        self.assertEqual(self.w.globals['op']('/look').par.pixeldat.val, 'look_px')

    def test_frame_boxes_a_stage(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\nd nullTOP\na -> b\nc -> d\n')
        op = self.w.globals['op']
        self.assertEqual(self.ok('box', '/c', 'Source', 'a', 'b', body='where it starts'), 'boxed 2 OPs as "Source"')
        self.ok('box', '/c', 'Output', 'c', 'd')
        boxes = [o for o in op('/c').children if o.OPType == 'annotateCOMP']
        self.assertEqual([b.par.Titletext.val for b in boxes], ['Source', 'Output'])
        self.assertEqual(boxes[0].par.Bodytext.val, 'where it starts')
        for box, names in zip(boxes, (('a', 'b'), ('c', 'd'))):
            for n in names:
                o = op('/c/' + n)
                self.assertTrue(box.nodeX < o.nodeX and o.nodeX + o.nodeWidth < box.nodeX + box.nodeWidth)
                self.assertTrue(box.nodeY < o.nodeY and o.nodeY + o.nodeHeight < box.nodeY + box.nodeHeight)
        # The second box sits below the first.
        self.assertLess(boxes[1].nodeY + boxes[1].nodeHeight, boxes[0].nodeY)
        self.assertIn('name the OPs', self.cmd('box', '/c', 'Empty')['error'])

    def test_look_describes_the_picture(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        self.ok('new', '/', 'noiseTOP', 'pic')
        img = np.zeros((90, 160, 4), dtype='float32')
        img[:, :80, 2] = 1.0                       # left half pure blue, right half black
        img[0, 0, 0] = float('nan')
        self.w.globals['op']('/pic').image = img
        out = self.ok('look', '/pic')
        self.assertIn('INVALID VALUES: 1', out)
        self.assertIn('black(<0.02) 50%', out)
        self.assertIn('blue 100%', out)

    def test_motion_measures_change(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        self.ok('new', '/', 'noiseTOP', 'pic')
        self.w.globals['op']('/pic').image = np.full((90, 160, 4), 0.5, dtype='float32')
        out = self.ok('motion', '/pic', frames=3, every=1)
        self.assertIn('/pic 3 frames, 1 apart', out)
        self.assertIn('change per step  mean 0 ', out)

    def picture(self, name, w=1920, h=1080):
        """A TOP whose red channel holds the column and whose green channel holds the row, counted from the
        top left, both divided by 1000. Returns what the bridge hands to the PNG writer."""
        import numpy as np
        self.ok('new', '/', 'noiseTOP', name)
        img = np.zeros((h, w, 4), dtype='float32')
        img[:, :, 0] = np.arange(w)[None, :] / 1000.0
        img[:, :, 1] = np.arange(h)[::-1, None] / 1000.0      # numpyArray has the bottom row first
        img[:, :, 3] = 1.0
        o = self.w.globals['op']('/' + name)
        o.image, o.width, o.height = img, w, h
        saved = []
        self.ns['_save_png'] = lambda a, file: saved.append(a) or True
        return saved

    def test_top_shrinks_by_a_whole_factor(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        saved = self.picture('pic')
        # 1920 / 768 = 2.5 is rounded up to 3, so every saved pixel is the mean of a 3x3 block.
        r = self.ok('top', '/pic')
        self.assertTrue(r.startswith('1920x1080 saved at 640x360 (1 pixel = 3x3 of the TOP): '), r)
        self.assertEqual(saved[-1].shape[:2], (360, 640))
        self.assertAlmostEqual(float(saved[-1][0, 0, 0]), 0.001, places=6)       # mean of columns 0, 1, 2
        self.assertAlmostEqual(float(saved[-1][0, 0, 1]), 0.001, places=6)       # mean of rows 0, 1, 2
        self.assertAlmostEqual(float(saved[-1][10, 100, 0]), 0.301, places=6)
        self.assertTrue(self.ok('top', '/pic', max=1920).startswith('1920x1080 saved: '))
        self.assertTrue(self.ok('top', '/pic', max=0).startswith('1920x1080 saved: '))
        self.assertTrue(self.ok('top', '/pic', max=960).startswith('1920x1080 saved at 960x540 (1 pixel = 2x2'))
        self.assertIn('max is the longest side', self.cmd('top', '/pic', max='big')['error'])
        # A width that the factor does not divide loses the last columns instead of being stretched.
        saved = self.picture('odd', 1000, 10)
        self.assertIn('saved at 333x3 (1 pixel = 3x3', self.ok('top', '/odd', max=400))

    def test_top_crop_keeps_every_pixel(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        saved = self.picture('pic')
        r = self.ok('top', '/pic', max=0, crop='800,400,320,180')
        self.assertTrue(r.startswith('1920x1080 crop 800,400,320,180 saved at 320x180 (every pixel): '), r)
        a = saved[-1]
        self.assertEqual(a.shape[:2], (180, 320))
        self.assertAlmostEqual(float(a[0, 0, 0]), 0.8, places=6)        # column 800
        self.assertAlmostEqual(float(a[0, 0, 1]), 0.4, places=6)        # row 400 from the top
        self.assertAlmostEqual(float(a[179, 319, 0]), 1.119, places=6)
        self.assertAlmostEqual(float(a[179, 319, 1]), 0.579, places=6)
        # The default max leaves a small region alone and shrinks a large one by a whole factor.
        self.assertIn('saved at 320x180 (every pixel)', self.ok('top', '/pic', crop='800,400,320,180'))
        self.assertIn('saved at 768x384 (1 pixel = 2x2', self.ok('top', '/pic', crop='0,0,1536,768'))
        # What lies outside the picture is black.
        self.ok('top', '/pic', max=0, crop='-10,-10,20,20')
        a = saved[-1]
        self.assertEqual(float(np.abs(a[:10]).sum() + np.abs(a[:, :10]).sum()), 0.0)
        self.assertAlmostEqual(float(a[10, 10, 0]), 0.0, places=6)
        self.assertAlmostEqual(float(a[19, 19, 0]), 0.009, places=6)
        self.assertEqual(self.ok('top', '/pic', max=0, crop=[800, 400, 32, 18]).count('saved at 32x18'), 1)
        for bad in ('800,400', 'a,b,c,d', '0,0,0,10', True):
            self.assertIn('crop', self.cmd('top', '/pic', crop=bad)['error'])
            self.assertNotIn('Traceback', self.cmd('top', '/pic', crop=bad)['error'])

    def test_top_without_cv2(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest('numpy is not installed')
        self.picture('pic')
        self.ns['_save_png'] = lambda a, file: False
        o = self.w.globals['op']('/pic')
        self.assertTrue(self.ok('top', '/pic').startswith('1920x1080 saved: '))      # TD saves it at full size
        self.assertIsNotNone(o.saved)
        o.saved = None
        self.assertEqual(self.cmd('top', '/pic', crop='0,0,10,10')['error'], 'crop needs numpy and cv2 in this TouchDesigner')
        self.assertIsNone(o.saved)

    def moving_picture(self):
        """A 400x300 TOP with a white 20x20 square that moves 5 pixels to the right every frame."""
        import numpy as np
        self.ok('new', '/', 'noiseTOP', 'mv')
        o = self.w.globals['op']('/mv')
        o.width, o.height = 400, 300

        def image():
            img = np.zeros((300, 400, 4), dtype='float32')
            x = 100 + self.w.absTime.frame * 5 % 200
            img[300 - 110:300 - 90, x - 10:x + 10, :3] = 1.0       # rows 90 to 110 from the top
            return img
        o.numpyArray = image

    def test_motion_crop_and_follow(self):
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest('numpy is not installed')
        self.moving_picture()
        whole = self.ok('motion', '/mv', frames=4, every=1)
        self.assertNotIn('change per step  mean 0 ', whole)
        # A region the square never enters stays still; the region it crosses does not.
        still = self.ok('motion', '/mv', frames=4, every=1, crop='0,200,400,100')
        self.assertIn('(crop 0,200,400,100)', still.splitlines()[0])
        self.assertIn('change per step  mean 0 ', still)
        self.assertNotIn('change per step  mean 0 ', self.ok('motion', '/mv', frames=4, every=1, crop='0,60,400,80'))
        # Centred on the square, every shot is the same picture.
        expr = '(100 + absTime.frame * 5 % 200, 100)'
        held = self.ok('motion', '/mv', frames=4, every=1, follow=expr, size=64)
        self.assertIn('(following %s: flow is measured relative to it)' % expr, held.splitlines()[0])
        self.assertIn('change per step  mean 0 ', held)
        self.assertRegex(held, r'(?m)^centre \d+,100 -> \d+,100 of 400x300$')
        self.assertIn('OUTSIDE THE PICTURE in 3 of 3 shots', self.ok('motion', '/mv', frames=3, every=1, follow='(-50, 500)'))
        # Following near the edge is allowed: the part outside the picture is black.
        self.assertIn('change per step  mean 0 ', self.ok('motion', '/mv', frames=3, every=1, follow='(0, 0)'))
        for bad in ('nope', '1 +', '5', '("a", 2)'):
            err = self.cmd('motion', '/mv', frames=3, every=1, follow=bad)['error']
            self.assertTrue(err.startswith('follow: '), err)
            self.assertNotIn('Traceback', err)
        self.assertIn('size is the side', self.cmd('motion', '/mv', follow='(0, 0)', size=0)['error'])
        self.assertIn('crop is', self.cmd('motion', '/mv', frames=3, crop='1,2')['error'])

    def test_wait_reports_the_time(self):
        r = self.ok('wait')
        self.assertRegex(r, r'^frame \d+  absTime [\d.e-]+ s$')

    def test_when_runs_in_the_first_true_frame(self):
        target = self.w.absTime.frame + 40
        self.assertTrue(self.ok('wait', when='absTime.frame >= %d' % target).startswith('frame %d ' % target))
        # An expression that is already true does not hold the command back.
        self.assertTrue(self.ok('wait', when='True').startswith('frame '))
        self.assertEqual(self.ok('conn', '/project1/noise1', when='absTime.seconds > 0'), 'in: -\nout: /project1/null1')
        r = self.ok('top', '/project1/noise1', when='absTime.frame >= %d' % (self.w.absTime.frame + 10))
        self.assertTrue(r.startswith('1280x720 saved: '))

    def test_when_holds_commands_that_span_frames(self):
        target = self.w.absTime.frame + 30
        r = self.ok('info', frames=10, when='absTime.frame >= %d' % target)
        self.assertIn('skipped 0/10', r)
        self.assertGreaterEqual(int(r.split('frame ')[-1].split()[0]), target + 10)
        self.ok('new', '/', 'noiseTOP', 'n')
        self.assertIn('/n 10 frames', self.ok('trace', '/n', 'seed', frames=10, when='absTime.frame >= %d' % (self.w.absTime.frame + 5)))
        self.assertIn('cooks/5', self.ok('cook', '/', frames=5, when='True'))

    def test_when_gives_up_and_explains(self):
        t0 = time.time()
        err = self.cmd('wait', when='absTime.frame < 0', wait=0.2)['error']
        self.assertEqual(err, 'when: still false after 0.2 s (last value False). Raise --wait, or check the expression')
        self.assertLess(time.time() - t0, 3)
        for bad, kind in (('nope > 1', 'NameError'), ('1 +', 'SyntaxError'), ('1 / 0', 'ZeroDivisionError')):
            err = self.cmd('wait', when=bad)['error']
            self.assertTrue(err.startswith('when: %s: ' % kind), err)
            self.assertNotIn('Traceback', err)
        self.assertIn('when takes a Python expression', self.cmd('wait', when=True)['error'])
        self.assertIn('when takes a Python expression', self.cmd('wait', when='True', wait='long')['error'])
        # A wrong call is refused at once, not after the wait.
        self.assertIn('usage: par(path', self.cmd('par', when='absTime.frame < 0')['error'])
        self.assertIn('unknown command: nope', self.cmd('nope', when='absTime.frame < 0')['error'])
        # The command's own refusal comes back when its moment arrives.
        self.assertEqual(self.cmd('par', '/project1/noise', when='True')['error'],
                         'no such OP: /project1/noise. /project1 has: noise1')

    def test_when_with_a_changing_command(self):
        target = self.w.absTime.frame + 20
        self.assertEqual(self.ok('set', '/project1/noise1', seed=7, when='absTime.frame >= %d' % target), 'seed=7')
        self.assertEqual(self.w.globals['op']('/project1/noise1').par.seed.val, 7)
        self.assertEqual(self.w.undo_blocks, ['touchdesigner-bridge set', 'end'])
        # Timed out, the command never ran, so nothing was changed and no undo step was opened.
        self.assertIn('still false', self.cmd('set', '/project1/noise1', seed=9, when='False', wait=0.05)['error'])
        self.assertEqual(self.w.globals['op']('/project1/noise1').par.seed.val, 7)
        self.assertEqual(self.w.undo_blocks, ['touchdesigner-bridge set', 'end'])
        # when and wait are not passed on as parameters; without when, wait is an ordinary parameter name.
        self.assertIn('no such par: wait', self.cmd('set', '/project1/noise1', wait=3)['error'])

    def test_info_spans_frames(self):
        f0 = self.w.absTime.frame
        r = self.ok('info', frames=10)
        self.assertGreaterEqual(self.w.absTime.frame - f0, 10)
        self.assertIn('toe fake.toe', r)
        self.assertIn('ops 7', r)
        self.assertIn('skipped 0/10', r)

    def test_info_counts_skipped_frames(self):
        self.w.frame_step = 2
        self.assertIn('skipped 10/10', self.ok('info', frames=10))

    def test_cook_ranks_only_cooking_ops(self):
        r = self.ok('cook', '/', frames=10)
        rows = r.splitlines()
        head = rows.index('cpu ms/frame | gpu ms/frame | cooks/10 | path type')
        self.assertTrue(rows[head + 1].startswith('8.000 0.000 10 /project1/script1 scriptCHOP'))
        self.assertTrue(rows[head + 2].startswith('0.500 2.000 10 /project1/noise1 noiseTOP'))
        self.assertNotIn('null1', r)  # an OP that is not cooking is not counted, even if its last cook was heavy
        self.assertIn('cooking 3 of 7 ops under /', r)
        self.assertIn('CHOP 8.00 TOP 0.50', r)
        comps = rows[rows.index('by COMP, everything inside: cpu ms/frame | gpu ms/frame | cooking ops | path') + 1:]
        self.assertEqual(comps[0], '8.500 2.000 3 /project1')  # fx itself counts as an OP; it has no contents, so it gets no COMP line
        gpu = self.ok('cook', '/', frames=5, sort='gpu').splitlines()
        self.assertIn('/project1/noise1', gpu[gpu.index('cpu ms/frame | gpu ms/frame | cooks/5 | path type') + 1])

    def test_commands_say_what_is_worth_knowing_at_that_moment(self):
        out = self.ok('new', '/', 'geometryCOMP', 'geo')
        self.assertIn('note  geometryCOMP: it contains torus1', out)
        self.assertNotIn('note', self.ok('new', '/', 'noiseTOP', 'plain'))
        out = self.ok('build', '/', 'r1 renderTOP\nr2 renderTOP\nn noiseTOP')
        self.assertEqual(out.count('note  renderTOP'), 1)
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        o = self.w.globals['op']('/plain')
        self.ns['_save_png'] = lambda a, file: True
        o.width, o.height = 1920, 1080
        o.image = np.zeros((1080, 1920, 4), dtype='float32')
        self.assertIn('note  the picture is black', self.ok('top', '/plain'))
        o.image = np.ones((1080, 1920, 4), dtype='float32')
        self.assertIn('note  the picture is white all over', self.ok('top', '/plain'))
        o.image = np.full((1080, 1920, 4), 0.3, dtype='float32'); o.image[5, 5, 0] = np.nan
        self.assertIn('note  1 values are not numbers', self.ok('top', '/plain'))
        o.image = np.full((1080, 1920, 4), 0.3, dtype='float32')
        self.assertNotIn('note', self.ok('top', '/plain'))

    def test_ab_compares_the_same_frame_and_puts_the_change_back(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        self.ok('new', '/', 'noiseTOP', 'pic')
        o = self.w.globals['op']('/pic')
        base = np.zeros((90, 160, 4), dtype='float32')
        base[10:30, 40:80, :3] = 0.5
        o.numpyArray = lambda: base * (2.0 if o.par.seed.val == 7 else 1.0)
        saved = []
        self.ns['_save_png'] = lambda a, file: saved.append(a) or True
        out = self.ok('compare', '/pic', '/pic', seed=7)
        self.assertIn('B = with seed=7', out)
        self.assertIn('differs in 5.56% of the picture', out)
        self.assertIn('where: x 40..79  y 60..79 from the top left', out)
        self.assertEqual(o.par.seed.val, 1)
        self.assertEqual(saved[0].shape[:2], (90, 480))
        out = self.ok('compare', '/pic', '/pic', seed=3)
        self.assertIn('NO VISIBLE DIFFERENCE', out)

    def test_ab_swaps_text_in_a_dat(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest('numpy is not installed')
        self.ok('new', '/', 'noiseTOP', 'pic')
        o, d = self.w.globals['op']('/pic'), self.w.globals['op']('/project1/text1')
        o.numpyArray = lambda: np.full((9, 16, 4), 0.25 if 'value = 3' in d.text else 0.75, dtype='float32')
        self.ns['_save_png'] = lambda a, file: True
        before = d.text
        out = self.ok('compare', '/pic', '/project1/text1', swap='value = 3', **{'with': 'value = 4'})
        self.assertIn('A  mean 0.25', out)
        self.assertIn('B  mean 0.75', out)
        self.assertEqual(d.text, before)
        out = self.cmd('compare', '/pic', '/project1/text1', swap='not there')
        self.assertIn('does not contain', out['error'])
        out = self.cmd('compare', '/pic', '/pic')
        self.assertIn('name the change', out['error'])

    def test_record_writes_a_movie_and_removes_its_operator(self):
        self.ok('new', '/', 'noiseTOP', 'pic')
        with tempfile.TemporaryDirectory() as tmp:
            file = os.path.join(tmp, 'clip.mp4')
            out = self.ok('record', '/pic', seconds=0.05, file=file)
            self.assertIn('h264', out)
            self.assertIn(file, out)
            self.assertTrue(os.path.exists(file))
        self.assertIsNone(self.w.globals['op']('/bridge_record'))
        out = self.cmd('record', '/pic', seconds=0)
        self.assertIn('seconds is how long to record', out['error'])
        out = self.cmd('record', '/pic', seconds=0.05, codec='nope')
        self.assertIn('no such codec', out['error'])
        self.assertIsNone(self.w.globals['op']('/bridge_record'))

    def test_sweep_removes_what_gone_processes_left(self):
        with tempfile.TemporaryDirectory() as base:
            for name in ('111', '222', '333', 'img'):
                os.makedirs(os.path.join(base, name, 'out'))
                with open(os.path.join(base, name, 'out', 'alive.json'), 'w') as fp:
                    fp.write('{}')
            now = time.time()
            self.assertEqual(self.ns['_sweep'](base, '333', now=now + 30), 0)
            os.utime(os.path.join(base, '222', 'out', 'alive.json'), (now + 3600, now + 3600))
            self.assertEqual(self.ns['_sweep'](base, '333', now=now + 3600), 1)
            self.assertEqual(sorted(os.listdir(base)), ['222', '333', 'img'])

    def test_trace_follows_a_parameter(self):
        self.ok('new', '/', 'noiseTOP', 'n')
        out = self.ok('trace', '/n', 'seed', frames=10)
        self.assertIn('/n 10 frames', out)
        self.assertIn('seed   1 .. 1  speed max 0/s', out)
        self.assertIn('name the parameters', self.cmd('trace', '/n')['error'])

    def test_overlaps_are_found_and_layout_clears_them(self):
        self.ok('new', '/', 'noiseTOP', 'a')
        self.ok('new', '/', 'noiseTOP', 'b')
        td.send(self.qdir(), '.py', 'for n in ("a", "b"):\n    op("/" + n).nodeX = 0; op("/" + n).nodeY = 0', 5)
        self.assertIn('a/b', self.ok('overlaps', '/', deep=False))
        self.ok('layout', '/')
        self.assertIn('no overlapping', self.ok('overlaps', '/', deep=False))

    def test_layout_deep_and_boxes(self):
        self.ok('new', '/project1', 'baseCOMP', 'inner')
        self.ok('new', '/project1/inner', 'noiseTOP', 'p')
        self.ok('new', '/project1/inner', 'noiseTOP', 'q')
        self.ok('box', '/project1', 'Group', 'noise1', 'null1')
        out = self.ok('layout', '/project1', deep=True)
        self.assertIn('COMPs below it', out)
        self.assertIn('no overlapping', self.ok('overlaps', '/project1'))

    def test_mem_of_one_op_and_types_by_plain_word(self):
        self.assertIn('total gpu', self.ok('mem', '/project1/noise1'))
        self.assertIn('noiseTOP', self.ok('types', 'noiset'))

    def test_failed_script_with_nothing_recorded_undoes_nothing(self):
        self.w.undo_stack.insert(0, 'earlier work')
        real = self.w.globals['ui'].undo.startBlock
        self.w.globals['ui'].undo.startBlock = lambda name: None       # as when TD records nothing for the script
        try:
            before = self.w.undone
            out = td.send(self.qdir(), '.py', 'result = 1/0', 5)
            self.assertFalse(out['ok'])
            self.assertEqual(self.w.undone, before)
            self.assertEqual(self.w.undo_stack[0], 'earlier work')
        finally:
            self.w.globals['ui'].undo.startBlock = real

    def test_failed_script_is_undone(self):
        before = self.w.undone
        out = td.send(self.qdir(), '.py', 'x = 1\nresult = 1/0', 5)
        self.assertFalse(out['ok'])
        self.assertEqual(self.w.undone, before + 1)
        self.assertIn('its changes were undone', out.get('stdout', '') + out.get('error', ''))
        out = td.send(self.qdir(), '.py', 'result = 2', 5)
        self.assertEqual(self.w.undone, before + 1)

    def test_py(self):
        out = td.send(self.qdir(), '.py', 'print("hi")\nresult = [c.name for c in root.children]', 5)
        self.assertEqual(out['stdout'], 'hi\n')
        self.assertEqual(out['result'], ['project1'])
        out = td.send(self.qdir(), '.py', 'result = 1/0', 5)
        self.assertFalse(out['ok'])
        self.assertIn('ZeroDivisionError', out['error'])

    def test_reload_replaces_old_loop(self):
        self.load()
        time.sleep(0.05)
        self.assertEqual(len(self.w.queue) <= 1, True)
        self.assertIn('ops 7', self.ok('info', frames=2))

    def test_stop(self):
        self.assertEqual(self.ok('stop'), 'stopped')
        time.sleep(0.05)
        self.assertEqual(self.w.queue, [])
        self.assertEqual(td.instances(self.dir), [])
        self.assertIn('not responding', td.pick(self.dir)[1])


class OldBuildTest(BridgeTest):
    """Works on builds that lack the newer attributes and arguments."""

    def load(self):
        self.w.knows_delay_ref = False
        for o in self.w.root.findChildren():
            # Leave only the old names, without cpuCookTime, gpuCookTime and childrenCPUCookTime.
            if 'cpuCookTime' in o.__dict__:
                o.cookTime = o.__dict__.pop('cpuCookTime')
                o.childrenCookTime = o.__dict__.pop('childrenCPUCookTime')
                del o.__dict__['gpuCookTime']
        super().load()

    def test_loop_survives_paused_timeline(self):
        self.assertTrue(all(c['delayRef'] is None for c in self.w.run_calls))

    def test_cook_ranks_only_cooking_ops(self):
        r = self.ok('cook', '/', frames=10)
        self.assertIn('8.000 0.000 10 /project1/script1 scriptCHOP', r)


class ClientTest(unittest.TestCase):
    def test_pick_between_several_instances(self):
        with tempfile.TemporaryDirectory() as base:
            for pid, toe in ((11, 'Show.toe'), (22, 'Scratch.toe')):
                os.makedirs(os.path.join(base, str(pid), 'out'))
                with open(os.path.join(base, str(pid), 'out', 'alive.json'), 'w') as fp:
                    json.dump({'time': time.time(), 'frame': 1, 'toe': toe, 'build': 'x', 'pid': pid}, fp)
            os.makedirs(os.path.join(base, '33', 'out'))
            with open(os.path.join(base, '33', 'out', 'alive.json'), 'w') as fp:
                json.dump({'time': time.time() - 60, 'frame': 1, 'toe': 'Dead.toe', 'build': 'x', 'pid': 33}, fp)
            d, why = td.pick(base)
            self.assertIsNone(d)
            self.assertIn('Show.toe (pid 11), Scratch.toe (pid 22)', why)
            self.assertNotIn('Dead', why)
            self.assertTrue(td.pick(base, 'scratch')[0].endswith('22'))
            self.assertTrue(td.pick(base, 11)[0].endswith('11'))

    def test_loader_text_file_matches_the_expanded_format(self):
        files = dict(td.loader_files())
        body = files['local/touchdesigner_bridge.text']
        text = td.loader_text().encode('utf-8')
        self.assertTrue(body.startswith(b'2\n*\x00\x00\x00'))
        self.assertEqual(body[-len(text) - 4:-len(text)], len(text).to_bytes(4, 'big'))
        self.assertTrue(body.endswith(text))
        compile(td.loader_text(), 'loader', 'exec')

    @unittest.skipUnless(os.name == 'posix', 'the stand-in tools are shell scripts')
    def test_toe_with_loader(self):
        # In place of toeexpand and toecollapse, put tools that only bundle with tar.
        with tempfile.TemporaryDirectory() as base:
            bin_dir = os.path.join(base, 'bin')
            os.makedirs(bin_dir)
            ext = '' if sys.platform == 'darwin' else '.exe'
            for name, body in (('toecollapse' + ext, 'tar cf "$1" "$1.dir" "$1.toc"'),
                               ('toeexpand' + ext, 'tar xf "$1"')):
                with open(os.path.join(bin_dir, name), 'w') as fp:
                    fp.write('#!/bin/sh\n' + body + '\n')
                os.chmod(os.path.join(bin_dir, name), 0o755)
            names = [n for n, _ in td.loader_files()]

            def toc(file):
                out = subprocess.run(['tar', 'xOf', file, 'p.toe.toc'], capture_output=True, text=True).stdout
                return out.split('\n')

            new = os.path.join(base, 'new.toe')
            self.assertEqual(td.toe_with_loader(base, new, bin_dir), 'created')
            self.assertEqual(toc(new)[:6], ['.start', 'project1.n', 'local.n'] + names)
            self.assertEqual(td.toe_with_loader(base, new, bin_dir), 'already')
            self.assertFalse(os.path.exists(new + '.bak'))

            # For an existing project, it is added at the end of the /local entries.
            work = os.path.join(base, 'w')
            os.makedirs(os.path.join(work, 'p.toe.dir', 'local'))
            lines = ['.start', 'project1.n', 'project1/a.n', 'local.n', 'local.parm', 'local/time.n', 'index.n', '']
            with open(os.path.join(work, 'p.toe.toc'), 'w') as fp:
                fp.write('\n'.join(lines))
            subprocess.run(['tar', 'cf', 'p.toe', 'p.toe.dir', 'p.toe.toc'], cwd=work, check=True)
            old = os.path.join(base, 'old.toe')
            os.rename(os.path.join(work, 'p.toe'), old)
            self.assertEqual(td.toe_with_loader(base, old, bin_dir), 'installed')
            self.assertEqual(toc(old), lines[:6] + names + lines[6:])
            self.assertEqual(toc(old + '.bak'), lines)
            self.assertEqual([n for n in os.listdir(base) if n.startswith('work-')], [])

    def test_when_lengthens_the_client_wait(self):
        # The TD side holds the command for up to --wait seconds, so the client has to wait at least that long.
        seen = []
        old = (td.pick, td.send, sys.argv, td.queue_dir)
        td.pick = lambda base, to=None: ('dir', None)
        td.send = lambda d, ext, body, timeout: seen.append((json.loads(body), timeout)) or {'ok': True, 'result': None}
        td.queue_dir = lambda: 'base'
        try:
            for argv, want in ((['wait', '--when', 'absTime.frame > 9'], 90.0),
                               (['top', '/a', '--when', 'x', '--wait', '5', '--timeout', '2'], 7.0),
                               (['ls', '/a'], 30.0)):
                sys.argv = ['td.py'] + argv
                self.assertEqual(td.main(), 0)
                self.assertEqual(seen[-1][1], want)
            self.assertEqual(seen[0][0], {'cmd': 'wait', 'args': [], 'kw': {'when': 'absTime.frame > 9'}})
            self.assertEqual(seen[1][0]['kw'], {'when': 'x', 'wait': 5})
        finally:
            td.pick, td.send, sys.argv, td.queue_dir = old

    def test_parse(self):
        self.assertEqual(td.parse(['/a', '--depth', '2', '--all', '--type', '*TOP']),
                         (['/a'], {'depth': 2, 'all': True, 'type': '*TOP'}))

    def test_setup_line_points_at_the_bridge(self):
        line = td.setup_line()
        self.assertTrue(line.startswith("exec(open(r'"))
        self.assertIn('touchdesigner_bridge.py', line)

    def test_windows_paths_become_wsl_paths(self):
        old = td.IS_WSL
        td.IS_WSL = True
        try:
            self.assertEqual(td.to_local(r'1x1 saved: C:\Users\a\Temp\x.png'), '1x1 saved: /mnt/c/Users/a/Temp/x.png')
            distro = os.environ.get('WSL_DISTRO_NAME')
            os.environ['WSL_DISTRO_NAME'] = 'Ubuntu-22.04'
            try:
                self.assertEqual(td.to_local(r'2 s, mjpa: \\wsl.localhost\Ubuntu-22.04\home\a\clip.mp4'), '2 s, mjpa: /home/a/clip.mp4')
                self.assertEqual(td.to_local(r'saved: \\wsl$\ubuntu-22.04\tmp\x.png  (every pixel)'), 'saved: /tmp/x.png  (every pixel)')
                self.assertEqual(td.to_local(r'\\wsl.localhost\Other\home\a'), r'\\wsl.localhost\Other\home\a')
            finally:
                if distro is None:
                    del os.environ['WSL_DISTRO_NAME']
                else:
                    os.environ['WSL_DISTRO_NAME'] = distro
        finally:
            td.IS_WSL = old


if __name__ == '__main__':
    unittest.main()
