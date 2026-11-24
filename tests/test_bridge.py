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

    def test_err_names_a_perform_window_that_is_not_there(self):
        proj = self.w.globals['project']
        proj.performWindowPath = '/perform'
        self.assertIn('Perform mode shows /perform, which does not exist', self.ok('err', '/project1'))
        # Deeper COMPs are not asked about it, and a project without the setting says nothing.
        self.assertNotIn('Perform', self.ok('err', '/project1/null1'))
        del proj.performWindowPath
        self.assertNotIn('Perform', self.ok('err', '/project1'))

    def test_err_names_a_glsl_top_with_more_than_three_inputs(self):
        p = self.w.globals['op']('/project1')
        g = fake_td.OP('look', 'glslTOP', 'TOP', p)
        g.inputConnectors = [fake_td.Connector(g) for _ in range(4)]
        srcs = [fake_td.OP('src%d' % i, 'noiseTOP', 'TOP', p) for i in range(4)]
        for i, s in enumerate(srcs):
            g.inputConnectors[i].connect(s.outputConnectors[0])
        self.assertIn('look has 4 inputs, but a GLSL TOP reads only three', self.ok('err', '/project1'))

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

    def at(self, comp, *names):
        op = self.w.globals['op']
        return [(op(comp + '/' + n).nodeX, op(comp + '/' + n).nodeY) for n in names]

    def test_layout_keeps_each_chain_on_its_own_row(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='p1 nullTOP\np2 nullTOP\nq1 nullTOP\nq2 nullTOP\nq3 nullTOP\n'
                                    'p1 -> p2\nq1 -> q2\nq2 -> q3\n')
        p1, p2, q1, q2, q3 = self.at('/c', 'p1', 'p2', 'q1', 'q2', 'q3')
        self.assertEqual(p1[1], p2[1])
        self.assertEqual(len(set([q1[1], q2[1], q3[1]])), 1)
        # q3 is alone in its column, and still stays with q1 and q2 instead of sitting beside p2
        self.assertNotEqual(q3[1], p2[1])
        # the longer line takes the top row
        self.assertGreater(q3[1], p2[1])
        self.assertIn('no overlapping', self.ok('overlaps', '/c'))

    def test_layout_brings_side_inputs_in_from_below(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a1 nullTOP\na2 nullTOP\na3 nullTOP\nb1 nullTOP\nmix levelTOP\nout nullTOP\n'
                                    'a1 -> a2\na2 -> a3\na3 -> mix\nb1 -> mix[1]\nmix -> out\n')
        a1, a2, a3, b1, mix, out = self.at('/c', 'a1', 'a2', 'a3', 'b1', 'mix', 'out')
        self.assertEqual(len(set(y for x, y in (a1, a2, a3, mix, out))), 1)      # the main line is straight
        self.assertLess(b1[1], mix[1])                                         # the second input sits below it,
        self.assertEqual(b1[0], a3[0])                                         # in the column just before the mix

    def test_layout_lines_up_a_fan_out_and_shows_a_wire_that_skips_ahead(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='src nullTOP\nl1 nullTOP\nl2 nullTOP\nr1 nullTOP\njoin levelTOP\n'
                                    'src -> l1\nl1 -> l2\nsrc -> r1\nl2 -> join\nr1 -> join[1]\n')
        src, l1, r1 = self.at('/c', 'src', 'l1', 'r1')
        self.assertEqual(l1[0], r1[0])
        self.assertGreater(l1[0], src[0])
        self.ok('new', '/', 'baseCOMP', 'bloom')
        self.ok('build', '/bloom', spec='src nullTOP\nblur nullTOP\nlift levelTOP\nadd levelTOP\n'
                                        'src -> blur\nblur -> lift\nlift -> add\nsrc -> add[1]\n')
        src, blur, lift, add = self.at('/bloom', 'src', 'blur', 'lift', 'add')
        self.assertEqual(len(set([blur[1], lift[1], add[1]])), 1)
        # src -> add would be hidden behind blur and lift on their row, so src sits a row lower
        self.assertLess(src[1], add[1])
        # with the direct wire on the first input, it is the straight line and the chain dips under it
        self.ok('new', '/', 'baseCOMP', 'glow')
        self.ok('build', '/glow', spec='src nullTOP\nblur nullTOP\nadd levelTOP\nsrc -> add\nsrc -> blur\nblur -> add[1]\n')
        src, blur, add = self.at('/glow', 'src', 'blur', 'add')
        self.assertEqual(src[1], add[1])
        self.assertLess(blur[1], add[1])
        self.assertTrue(src[0] < blur[0] < add[0])

    def test_build_into_a_network_sets_the_new_ops_beside_what_they_are_wired_to(self):
        op = self.w.globals['op']
        hand = {'noise1': (-400, 300), 'null1': (-150, 300), 'script1': (-400, 0), 'text1': (500, 500), 'fx': (90, 300)}
        for n, (x, y) in hand.items():
            op('/project1/' + n).nodeX, op('/project1/' + n).nodeY = x, y
        self.ok('build', '/project1', spec='soft levelTOP\nfinal nullTOP\nnoise1 -> soft\nsoft -> final\n')
        # nothing that was there has moved
        self.assertEqual(dict(zip(hand, self.at('/project1', *hand))), hand)
        soft, final = self.at('/project1', 'soft', 'final')
        # to the right of noise1, in wiring order, and moved off its row because null1 is already there
        self.assertEqual(soft[0], -400 + 130 + 40)
        self.assertGreater(final[0], soft[0])
        self.assertEqual(soft[1], final[1])
        self.assertNotEqual(soft[1], 300)
        self.assertLess(abs(soft[1] - 300), 300)
        self.assertIn('no overlapping', self.ok('overlaps', '/project1', deep=False))
        # put between two OPs that sit close together, it cannot fit: it goes below, and build says which wire
        # now runs backwards instead of moving what was there
        out = self.ok('build', '/project1', spec='mid levelTOP\nnoise1 -> mid\nmid -> null1\n')
        self.assertIn('note  mid -> null1 runs right to left: there was no room beside null1', out)
        self.assertEqual(dict(zip(hand, self.at('/project1', *hand))), hand)
        self.ok('rm', '/project1/mid', yes=True)
        self.assertNotIn('right to left', self.ok('build', '/project1', spec='tail nullTOP\nfinal -> tail\n'))
        # an OP that feeds an existing one goes to its left
        self.ok('build', '/project1', spec='pre nullTOP\npre -> noise1\n')
        self.assertEqual(self.at('/project1', 'pre')[0][0], -400 - 40 - 130)
        # one wired to nothing goes to the right of everything; so does a bare new
        self.ok('build', '/project1', spec='spare noiseTOP\n')
        self.ok('new', '/project1', 'nullTOP', 'extra')
        spare, extra = self.at('/project1', 'spare', 'extra')
        self.assertGreater(spare[0], 500 + 130)
        self.assertGreater(extra[0], spare[0])
        self.assertEqual(dict(zip(hand, self.at('/project1', *hand))), hand)
        self.assertIn('no overlapping', self.ok('overlaps', '/project1', deep=False))

    def test_layout_leaves_a_network_someone_arranged(self):
        op = self.w.globals['op']
        out = self.cmd('layout', '/project1')
        self.assertFalse(out['ok'])
        self.assertIn('5 of 5 OPs were not placed by td.py', out['error'])
        self.assertIn('--yes', out['error'])
        self.assertEqual(self.at('/project1', 'noise1', 'null1'), [(0, 0), (0, 0)])
        # what the bridge built by itself it may arrange again without being asked
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb nullTOP\na -> b\n')
        self.ok('layout', '/c')
        # an OP of its own that someone nudged by hand is arranged again, and the reply says which
        op('/c/b').nodeY -= 400
        out = self.ok('layout', '/c')
        self.assertIn('arranged 2 OPs in /c\nnote  1 had been moved by hand since td.py placed them', out)
        self.assertIn(': b. undo puts them back', out)
        self.assertEqual(op('/c/b').nodeY, op('/c/a').nodeY)
        # an OP someone else made is a different matter: nothing moves until asked
        fake_td.OP('theirs', 'nullTOP', 'TOP', op('/c'), nodeX=900, nodeY=900)
        out = self.cmd('layout', '/c')
        self.assertIn('/c: 1 of 3 OPs were not placed by td.py (theirs)', out['error'])
        self.assertEqual((op('/c/theirs').nodeX, op('/c/theirs').nodeY), (900, 900))
        # asked to, it arranges, and the network is then its own to keep tidy
        self.assertEqual(self.ok('layout', '/c', yes=True), 'arranged 3 OPs in /c')
        self.assertEqual(self.ok('layout', '/c'), 'arranged 3 OPs in /c')
        # a COMP below is checked too
        self.assertIn('/project1/fx: 1 of 1', self.cmd('layout', '/', deep=True)['error'])

    def test_layout_when_asked_keeps_the_order_someone_gave(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a1 nullTOP\na2 nullTOP\nb1 nullTOP\nb2 nullTOP\na1 -> a2\nb1 -> b2\n')
        # someone puts the b chain above the a chain, roughly
        for n, (x, y) in {'b1': (13, 900), 'b2': (480, 870), 'a1': (-60, 410), 'a2': (300, 455)}.items():
            op('/c/' + n).nodeX, op('/c/' + n).nodeY = x, y
        self.ok('layout', '/c', yes=True)
        a1, a2, b1, b2 = self.at('/c', 'a1', 'a2', 'b1', 'b2')
        self.assertEqual((b1[1], a1[1]), (b2[1], a2[1]))
        self.assertGreater(b1[1], a1[1])
        # and the network stays where it was: its left edge and its top edge have not moved
        self.assertEqual((min(a1[0], b1[0]), b1[1]), (-60, 900))
        # boxes set side by side stay side by side, in their order; boxes that were stacked stay stacked
        self.ok('box', '/c', 'A', 'a1', 'a2')
        self.ok('box', '/c', 'B', 'b1', 'b2')
        boxes = dict((b.par.Titletext.val, b) for b in op('/c').children if b.OPType == 'annotateCOMP')
        self.ok('layout', '/c')
        self.assertLess(boxes['B'].nodeY + boxes['B'].nodeHeight, boxes['A'].nodeY)
        boxes['B'].nodeX, boxes['B'].nodeY = boxes['A'].nodeX + boxes['A'].nodeWidth + 400, boxes['A'].nodeY
        op('/c/b1').nodeX, op('/c/b1').nodeY = boxes['B'].nodeX + 60, boxes['B'].nodeY + 60
        op('/c/b2').nodeX, op('/c/b2').nodeY = boxes['B'].nodeX + 260, boxes['B'].nodeY + 60
        self.ok('layout', '/c', yes=True)
        self.assertGreater(boxes['B'].nodeX, boxes['A'].nodeX + boxes['A'].nodeWidth)
        self.assertEqual(boxes['B'].nodeY, boxes['A'].nodeY)
        self.assertIn('no overlapping', self.ok('overlaps', '/c'))

    def test_layout_sets_the_rest_to_the_right_of_a_single_box(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\nd nullTOP\na -> b\nb -> c\nc -> d\n')
        self.ok('box', '/c', 'Source', 'a', 'b')
        self.ok('layout', '/c')
        box = [o for o in op('/c').children if o.OPType == 'annotateCOMP'][0]
        a, b, c, d = self.at('/c', 'a', 'b', 'c', 'd')
        self.assertTrue(a[0] < b[0] < box.nodeX + box.nodeWidth < c[0] < d[0])     # every wire still runs rightward
        self.assertIn('no overlapping', self.ok('overlaps', '/c'))

    def test_build_into_a_wire_makes_room_instead_of_turning_a_wire_back(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\nlow nullTOP\na -> b\nb -> c\n')
        hand = {'a': (0, 0), 'b': (170, 0), 'c': (340, 0), 'low': (10, -300)}
        for n, (x, y) in hand.items():
            op('/c/' + n).nodeX, op('/c/' + n).nodeY = x, y
        out = self.ok('build', '/c', spec='soft levelTOP\nmask noiseTOP\nb -> soft\nmask -> soft[1]\nsoft -> c\n')
        a, b, c, low, soft, mask = self.at('/c', 'a', 'b', 'c', 'low', 'soft', 'mask')
        # what is left of the wire stays; c moves right by exactly the room soft needs, on the same row
        self.assertEqual((a, b, low), ((0, 0), (170, 0), (10, -300)))
        self.assertEqual(soft, (340, 0))
        self.assertEqual(c, (510, 0))
        self.assertLess(mask[1], soft[1])
        self.assertIn('note  made room on the wire b -> c: moved c right by 170', out)
        self.assertNotIn('right to left', out)
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs')
        # with room already there, nothing moves and nothing is said
        op('/c/c').nodeX = 900
        out = self.ok('build', '/c', spec='last nullTOP\nsoft -> last\nlast -> c\n')
        self.assertEqual(self.at('/c', 'c', 'last'), [(900, 0), (510, 0)])
        self.assertNotIn('note', out)

    def test_room_made_by_the_bridge_does_not_count_as_moved_by_hand(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\na -> b\nb -> c\n')
        self.assertIn('made room', self.ok('build', '/c', spec='mid levelTOP\nb -> mid\nmid -> c\n'))
        self.assertEqual(self.ok('layout', '/c'), 'arranged 4 OPs in /c')

    def test_err_includes_what_expressions_and_callbacks_raised(self):
        op = self.w.globals['op']
        op('/project1/noise1').scriptErrors = lambda recurse=False: "noise1: Error: name 'lfo9' is not defined (/project1/noise1)"
        out = self.ok('err', '/project1/noise1')
        self.assertTrue(out.startswith('E '), out)
        self.assertIn("name 'lfo9' is not defined  @ /project1/noise1", out)

    def test_build_sets_parameters_after_every_op_exists(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        # seed names an OP that the spec makes two lines further down
        self.ok('build', '/c', spec='n noiseTOP seed==len(op("later").name)\nmid nullTOP\nlater nullTOP\n')
        self.assertEqual(self.w.globals['op']('/c/n').par.seed.expr, 'len(op("later").name)')

    def test_what_is_added_is_not_set_down_on_a_wire(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb nullTOP\nc nullTOP\na -> b\na -> c\n')
        for n, (x, y) in {'a': (0, 0), 'b': (900, 0), 'c': (170, -400)}.items():
            op('/c/' + n).nodeX, op('/c/' + n).nodeY = x, y
        # beside a, on its row, there is room for a node: but the wire a -> b runs there
        self.ok('build', '/c', spec='t nullTOP\na -> t\n')
        t = self.at('/c', 't')[0]
        self.assertEqual(t[0], 170)
        self.assertNotEqual(t[1], 0)

    def test_a_change_reports_what_is_wrong_with_the_op_it_touched(self):
        op = self.w.globals['op']
        op('/project1/noise1')._errors = '  Error: Not enough sources specified (/project1/noise1)'
        op('/project1/noise1')._warnings = '  Warning: Resolution is limited (/project1/noise1)'
        out = self.ok('set', '/project1/noise1', seed=4)
        self.assertEqual(out.splitlines()[1:], ['E noise1: Not enough sources specified', 'W noise1: Resolution is limited'])
        # an OP with nothing wrong adds nothing to the reply
        self.assertEqual(self.ok('set', '/project1/noise1', seed=5).splitlines()[0], 'seed=5')
        op('/project1/noise1')._errors = op('/project1/noise1')._warnings = ''
        self.assertEqual(self.ok('set', '/project1/noise1', seed=6), 'seed=6')

    def test_a_folded_docked_dat_takes_no_room(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb nullTOP\nc nullTOP\nd nullTOP\na -> b\nc -> d\n')
        keys = fake_td.OP('a_keys', 'textDAT', 'DAT', op('/c'), dock=op('/c/a'), showDocked=False)
        self.ok('layout', '/c', yes=True)
        self.assertEqual(op('/c/a').nodeY - op('/c/c').nodeY, 130 + 60)     # one node apart, and the narrow row between two unwired chains
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs')
        keys.showDocked = True                                               # opened: now it needs its room
        self.ok('layout', '/c')
        self.assertEqual(op('/c/a').nodeY - op('/c/c').nodeY, 240 + 60)

    def test_a_build_that_fails_leaves_nothing_behind(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\na -> b\nb -> c\n')
        before = sorted(k.name for k in op('/c').children)
        # the third line names a type that does not exist, after two OPs were made and a wire was to be cut
        out = self.cmd('build', '/c', spec='mid levelTOP\nb -> mid\nmid -> c\nextra noSuchTOP\n')
        self.assertFalse(out['ok'])
        self.assertIn('Nothing was made', out['error'])
        self.assertEqual(sorted(k.name for k in op('/c').children), before)
        # a parameter that does not exist, found after the wires were changed: the wire b -> c is back
        out = self.cmd('build', '/c', spec='mid levelTOP nosuchpar=1\nb -> mid\nmid -> c\n')
        self.assertIn('Nothing was made', out['error'])
        self.assertEqual(sorted(k.name for k in op('/c').children), before)
        self.assertEqual([i.name for i in op('/c/c').inputs], ['b'])

    def test_one_build_can_go_into_two_wires(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb levelTOP\nc nullTOP\nd nullTOP\na -> b\nb -> c\nc -> d\n')
        out = self.ok('build', '/c', spec='p levelTOP\nq levelTOP\na -> p\np -> b\nc -> q\nq -> d\n')
        a, p, b, c, q, d = self.at('/c', 'a', 'p', 'b', 'c', 'q', 'd')
        self.assertEqual(len(set(y for x, y in (a, p, b, c, q, d))), 1)            # all on the one row
        self.assertTrue(a[0] < p[0] < b[0] < c[0] < q[0] < d[0])                    # in the order of the wires
        self.assertEqual(out.count('made room'), 2)
        self.assertNotIn('right to left', out)
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs')

    def test_build_refuses_two_wires_into_one_input(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        out = self.cmd('build', '/c', spec='a noiseTOP\nb noiseTOP\nmix levelTOP\na -> mix\nb -> mix\n')
        self.assertIn('line 5: mix[0] is already wired on line 4', out['error'])
        self.assertIn('mix[1]', out['error'])
        self.assertEqual(self.w.globals['op']('/c').children, [])

    def test_layout_sets_a_late_side_input_near_and_an_unwired_op_under_what_names_it(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='s1 nullTOP\ns2 levelTOP\ns3 nullTOP\ns4 nullTOP\ns5 levelTOP\nend nullTOP\n'
                                    'early nullTOP\ne2 nullTOP\nlate nullTOP\nspare textDAT\nlfo noiseCHOP\n'
                                    'drive noiseTOP seed==op("lfo")["chan1"]\n'
                                    's1 -> s2\ns2 -> s3\ns3 -> s4\ns4 -> s5\ns5 -> end\n'
                                    'early -> e2\ne2 -> s2[1]\nlate -> s5[1]\ndrive -> s1\n')
        s5, end, late, e2, lfo, drive, spare = self.at('/c', 's5', 'end', 'late', 'e2', 'lfo', 'drive', 'spare')
        # late joins far along the row: it sits one row down in the column before s5, not under the rows that
        # early and e2 needed at the other end
        self.assertEqual(late[1], e2[1])
        self.assertLess(late[0], s5[0])
        self.assertGreater(late[0], e2[0] + 130 + 40 + 130)
        # lfo is wired to nothing, but drive reads it in an expression: it sits just left of drive, on its
        # row, as if wired into it
        self.assertEqual(lfo, (drive[0] - 130 - 40, drive[1]))
        # spare belongs to nothing: to the right, past an empty column
        self.assertGreater(spare[0], end[0] + 130 + 40 + 130)
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs')

    def test_layout_keeps_an_op_next_to_the_unwired_op_that_names_it(self):
        self.ok('new', '/', 'baseCOMP', 'c')
        # Nothing here is wired to the Render TOP: it names the camera and the geometry, and the geometry
        # names its material.
        self.ok('build', '/c', spec='mat_gold phongMAT\nzz_spare textDAT\ncam_main cameraCOMP\n'
                                    'geo_ball geometryCOMP material=mat_gold\n'
                                    'render_main renderTOP camera=cam_main geometry=geo_ball\nnull_final nullTOP\n'
                                    'render_main -> null_final\n')
        render, cam, geo, mat, spare, final = self.at('/c', 'render_main', 'cam_main', 'geo_ball', 'mat_gold',
                                                      'zz_spare', 'null_final')
        # what the Render TOP names leads into it from the left, like inputs, and the material is the very
        # next node under its geometry
        self.assertLess(cam[0], render[0])
        self.assertLess(geo[0], render[0])
        self.assertLess(render[0] - geo[0], 2 * (130 + 40) + 1)
        self.assertEqual((geo[0], geo[1] - mat[1]), (mat[0], 90 + 40))
        self.assertNotEqual(cam[1], geo[1])
        self.assertGreater(spare[0], final[0] + 130 + 40 + 130)
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs')
        # A CHOP that two OPs along the wiring read sits before both, and the light that looks at the
        # geometry comes after the geometry
        self.ok('new', '/', 'baseCOMP', 'e')
        self.ok('build', '/e', spec='ctl_speed noiseCHOP\ngeo_ball geometryCOMP\nlight_key lightCOMP lookat=geo_ball\n'
                                    'render_main renderTOP geometry=geo_ball\n'
                                    'noise_a noiseTOP seed==op("ctl_speed")["chan1"]\n'
                                    'noise_b noiseTOP seed==op("ctl_speed")["chan1"]\nnull_final nullTOP\n'
                                    'noise_a -> noise_b\nnoise_b -> render_main\nrender_main -> null_final\n')
        render, geo, ctl, a, b, light = self.at('/e', 'render_main', 'geo_ball', 'ctl_speed', 'noise_a', 'noise_b',
                                                'light_key')
        self.assertLess(ctl[0], a[0])
        self.assertLess(geo[0], render[0])
        self.assertLess(geo[0], light[0])
        self.assertEqual(self.ok('overlaps', '/e'), 'no overlapping OPs in 1 COMPs')
        # Unwired CHOPs that hand a value on through expressions are a flow: it runs left to right, on one
        # row, into the OP that reads its end
        self.ok('new', '/', 'baseCOMP', 'f')
        self.ok('build', '/f', spec='table_song textDAT\nstate_song noiseCHOP seed==len(op("table_song").text)\n'
                                    'values_now noiseCHOP seed==op("state_song")["chan1"]\n'
                                    'noise_wipe noiseTOP seed==op("values_now")["chan1"]\nnull_final nullTOP\n'
                                    'noise_wipe -> null_final\n')
        table, state, values, wipe = self.at('/f', 'table_song', 'state_song', 'values_now', 'noise_wipe')
        self.assertTrue(table[0] < state[0] < values[0] < wipe[0])
        self.assertEqual(len(set([table[1], state[1], values[1], wipe[1]])), 1)
        self.assertEqual(self.ok('overlaps', '/f'), 'no overlapping OPs in 1 COMPs')
        # Two OPs that read each other, and a wire with a reference back along it, do not turn a wire round
        self.ok('new', '/', 'baseCOMP', 'g')
        self.ok('build', '/g', spec='noise_a noiseTOP seed==op("null_out").width\nnull_out nullTOP\n'
                                    'noise_x noiseCHOP seed==op("noise_y")["chan1"]\n'
                                    'noise_y noiseCHOP seed==op("noise_x")["chan1"]\nnoise_a -> null_out\n')
        a, out = self.at('/g', 'noise_a', 'null_out')
        self.assertLess(a[0], out[0])
        self.assertEqual(self.ok('overlaps', '/g'), 'no overlapping OPs in 1 COMPs')
        # A wire out of a child COMP counts as a wire from that COMP
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'h')
        self.ok('build', '/h', spec='base_src baseCOMP\nnull_a nullTOP\nnull_b nullTOP\nnull_a -> null_b\n')
        inner = fake_td.OP('out1', 'nullTOP', 'TOP', op('/h/base_src'))
        op('/h/null_a').inputs.append(inner)
        self.ok('layout', '/h', yes=True)
        src, na = self.at('/h', 'base_src', 'null_a')
        self.assertEqual(src, (na[0] - 130 - 40, na[1]))
        self.assertEqual(self.ok('overlaps', '/h'), 'no overlapping OPs in 2 COMPs')
        # With no wiring at all the two still sit together, whatever else is unwired
        self.ok('new', '/', 'baseCOMP', 'd')
        self.ok('build', '/d', spec='a1 nullCHOP\na2 nullCHOP\ngeo_ball geometryCOMP material=mat_gold\n'
                                    'light_key lightCOMP\nmat_blue phongMAT\nmat_gold phongMAT\nt1 nullTOP\n')
        geo, mat = self.at('/d', 'geo_ball', 'mat_gold')
        self.assertEqual(geo[0], mat[0])
        self.assertEqual(geo[1] - mat[1], 90 + 40)
        self.assertEqual(self.ok('overlaps', '/d'), 'no overlapping OPs in 1 COMPs')
        # A geometry made later for a material that is already there goes under that material
        self.ok('build', '/d', spec='geo_cube geometryCOMP material=mat_blue\n')
        cube, blue = self.at('/d', 'geo_cube', 'mat_blue')
        self.assertLess(abs(cube[0] - blue[0]) + abs(cube[1] - blue[1]), 2 * (130 + 40 + 90 + 40))
        self.assertEqual(self.ok('overlaps', '/d'), 'no overlapping OPs in 1 COMPs')

    def test_overlaps_names_wires_that_turn_back_and_ls_gives_positions(self):
        op = self.w.globals['op']
        self.ok('new', '/', 'baseCOMP', 'c')
        self.ok('build', '/c', spec='a noiseTOP\nb nullTOP\na -> b\n')
        op('/c/b').nodeX = -400
        self.assertEqual(self.ok('overlaps', '/c'), 'no overlapping OPs in 1 COMPs\n/c  1 wires run right to left: a -> b')
        self.assertIn('b nullTOP  at -400 0', self.ok('ls', '/c', pos=True))
        self.assertNotIn(' at ', self.ok('ls', '/c'))

    def test_box_does_not_move_what_someone_placed(self):
        op = self.w.globals['op']
        op('/project1/noise1').nodeX, op('/project1/noise1').nodeY = 700, 650
        op('/project1/null1').nodeX, op('/project1/null1').nodeY = 950, 600
        self.ok('box', '/project1', 'Source', 'noise1', 'null1')
        self.assertEqual(self.at('/project1', 'noise1', 'null1'), [(700, 650), (950, 600)])
        box = [b for b in op('/project1').children if b.OPType == 'annotateCOMP'][0]
        self.assertTrue(box.nodeX < 700 and 950 + 130 < box.nodeX + box.nodeWidth)
        self.assertTrue(box.nodeY < 600 and 650 + 90 < box.nodeY + box.nodeHeight)

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
        self.ok('layout', '/', yes=True)
        self.assertIn('no overlapping', self.ok('overlaps', '/', deep=False))

    def test_layout_deep_and_boxes(self):
        self.ok('new', '/project1', 'baseCOMP', 'inner')
        self.ok('new', '/project1/inner', 'noiseTOP', 'p')
        self.ok('new', '/project1/inner', 'noiseTOP', 'q')
        self.ok('box', '/project1', 'Group', 'noise1', 'null1')
        out = self.ok('layout', '/project1', deep=True, yes=True)
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

    @unittest.skipUnless(os.name == 'posix', 'the stand-in tool is a shell script')
    def test_a_project_opens_with_the_build_that_saved_it(self):
        # Three installs, each with a toeexpand that only prints the build the project was saved with.
        old_env = os.environ.pop('TOUCHDESIGNER_BIN', None)
        try:
            with tempfile.TemporaryDirectory() as base:
                ext = '' if sys.platform == 'darwin' else '.exe'
                saved = os.path.join(base, 'saved')

                def install(build):
                    d = os.path.join(base, 'TouchDesigner.' + build, 'bin')
                    os.makedirs(d)
                    with open(os.path.join(d, 'toeexpand' + ext), 'w') as fp:
                        fp.write('#!/bin/sh\necho "version 099"\necho "build $(cat "%s")"\nexit 1\n' % saved)
                    os.chmod(os.path.join(d, 'toeexpand' + ext), 0o755)
                    return d

                def was_saved_with(build):
                    with open(saved, 'w') as fp:
                        fp.write(build)

                b23, b25a, b25b = install('2023.12600'), install('2025.32280'), install('2025.32460')
                bins = [b23, b25a, b25b]
                toe = os.path.join(base, 'p.toe')
                open(toe, 'w').close()

                was_saved_with('2025.32460')
                self.assertEqual(td.toe_build(b23, toe), (2025, 32460))
                # The usual choice is an older build: the project goes to the one that saved it.
                got, said = td.bin_for(toe, b23, bins)
                self.assertEqual(got, b25b)
                self.assertIn('2025.32460', said)
                self.assertEqual(td.bin_for(toe, b25b, bins), (b25b, None))
                # Its own build is not installed: the nearest newer one, never an older one.
                was_saved_with('2025.32300')
                got, said = td.bin_for(toe, b23, bins)
                self.assertEqual(got, b25b)
                self.assertIn('not installed', said)
                # A newer usual choice is left alone.
                self.assertEqual(td.bin_for(toe, b25b, [b23, b25b]), (b25b, None))
                # Only older builds: refused, with the reason.
                was_saved_with('2026.10000')
                got, said = td.bin_for(toe, b23, bins)
                self.assertIsNone(got)
                self.assertIn('2026.10000', said)
                # A build named by hand is taken as said, and a file that does not exist yet has no build.
                os.environ['TOUCHDESIGNER_BIN'] = b23
                self.assertEqual(td.bin_for(toe, b23, bins), (b23, None))
                del os.environ['TOUCHDESIGNER_BIN']
                self.assertEqual(td.bin_for(os.path.join(base, 'new.toe'), b23, bins), (b23, None))
                # A tool that says nothing readable leaves the usual choice.
                was_saved_with('unknown')
                self.assertEqual(td.bin_for(toe, b23, bins), (b23, None))
        finally:
            if old_env is not None:
                os.environ['TOUCHDESIGNER_BIN'] = old_env

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

    def test_building_before_reading_the_guide_is_pointed_out_once(self):
        import contextlib
        import io
        old = (td.pick, td.send, sys.argv, td.queue_dir)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = os.path.join(tmp.name, 'guide')
        td.pick = lambda base, to=None: ('dir', None)
        td.send = lambda d, ext, body, timeout: {'ok': True, 'result': 'done'}
        td.queue_dir = lambda: base

        def run(*argv):
            sys.argv = ['td.py'] + list(argv)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(td.main(), 0)
            return out.getvalue()
        try:
            self.assertNotIn('note', run('ls', '/a'))                     # looking needs no guide
            self.assertIn('note  read the guide before building', run('new', '/a', 'nullTOP'))
            self.assertEqual(run('new', '/a', 'nullTOP'), 'done\n')        # said once, not every time
            os.remove(os.path.join(base, 'guide_seen'))
            self.assertIn('## Workflow', run('guide'))
            self.assertEqual(run('build', '/a', '--f', os.devnull), 'done\n')   # read: nothing to say
            # a reading counts for a working session, not for ever
            long_ago = time.time() - td.GUIDE_FRESH - 60
            os.utime(os.path.join(base, 'guide_seen'), (long_ago, long_ago))
            self.assertIn('read the guide', run('layout', '/a'))
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
