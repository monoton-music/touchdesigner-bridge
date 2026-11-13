"""A stand-in for running the bridge outside TouchDesigner.

Names and meanings follow the Python stubs bundled with TD 2025.32460 (bin/Lib/tdi).
The difference from the real thing is that run() only queues the call, and step() advances one frame.
"""
import fnmatch
import types


class ParMode:
    CONSTANT = 'ParMode.CONSTANT'
    EXPRESSION = 'ParMode.EXPRESSION'
    EXPORT = 'ParMode.EXPORT'
    BIND = 'ParMode.BIND'


class Par:
    def __init__(self, name, val, default=None, expr=None, label=None, style='Float', menuNames=None, page='Common'):
        self.name = name
        self.label = label or name.capitalize()
        self.style = style
        self.menuNames = menuNames
        self.page = types.SimpleNamespace(name=page)
        self.isNumber = style in ('Float', 'Int')
        self.normMin, self.normMax = 0, 1
        self.readOnly = False
        self.pulsed = 0
        self._val = val
        self.default = val if default is None else default
        self.expr = expr or ''
        self.bindExpr = ''
        self.mode = ParMode.EXPRESSION if expr else ParMode.CONSTANT

    @property
    def val(self):
        return self._val

    @val.setter
    def val(self, v):
        self._val = v

    @property
    def isDefault(self):
        return self.mode == ParMode.CONSTANT and self._val == self.default

    def eval(self):
        return self._val

    def pulse(self):
        self.pulsed += 1


class Connector:
    def __init__(self, owner):
        self.owner = owner
        self.source = None

    def connect(self, target):
        src = target.owner
        if self.source is not None:
            self.disconnect()
        self.source = src
        self.owner.inputs.append(src)
        src.outputs.append(self.owner)

    def disconnect(self):
        if self.source is not None:
            self.owner.inputs.remove(self.source)
            self.source.outputs.remove(self.owner)
            self.source = None


class Chan:
    def __init__(self, name, vals):
        self.name = name
        self.vals = list(vals)

    def eval(self):
        return self.vals[0]


class OP:
    def __init__(self, name, optype, family, parent=None, **kw):
        self.name = name
        self.OPType = optype
        self.family = family
        self.parent = parent
        self.children = []
        self.inputs = []
        self.outputs = []
        self.bypass = False
        self.allowCooking = True
        self.cookAbsFrame = -100
        self.cpuCookTime = 0.0
        self.gpuCookTime = 0.0
        self.childrenCPUCookTime = 0.0
        self._pars = {}
        self._errors = ''
        self._warnings = ''
        self.text = ''
        self._chans = []
        self.width = 1280
        self.height = 720
        self.saved = None
        self.cooks_every_frame = False
        self.gpuMemory = 0
        self.cpuMemory = 0
        self.nodeX = 0
        self.nodeY = 0
        self.lock = False
        self.comment = ''
        self.customPages = []
        self.inputConnectors = [Connector(self), Connector(self)]
        self.outputConnectors = [Connector(self)]
        self.pixel = (0.0, 0.0, 0.0, 1.0)
        self.dock = None
        self.nodeWidth = 130
        self.nodeHeight = 90
        self.compile_log = ''
        self.image = None
        self.__dict__.update(kw)
        if parent is not None:
            parent.children.append(self)

    @property
    def path(self):
        if self.parent is None:
            return '/'
        return self.parent.path.rstrip('/') + '/' + self.name

    isCOMP = property(lambda s: s.family == 'COMP')
    isDAT = property(lambda s: s.family == 'DAT')
    isSOP = property(lambda s: s.family == 'SOP')
    isTOP = property(lambda s: s.family == 'TOP')
    totalCooks = 0
    numChans = property(lambda s: len(s._chans))
    numSamples = property(lambda s: len(s._chans[0].vals) if s._chans else 0)

    @property
    def numChildrenRecursive(self):
        return len(self.findChildren())

    @property
    def par(self):
        return types.SimpleNamespace(**self._pars)

    def addPar(self, *a, **k):
        p = Par(*a, **k)
        p.owner = self
        self._pars[p.name] = p
        return p

    def appendCustomPage(self, name):
        owner = self

        class Page:
            def __init__(self):
                self.name = name

            def appendFloat(self, par, label=None):
                return [owner.addPar(par, 0.0, label=label, page=name)]
        page = Page()
        self.customPages.append(page)
        return page

    def pars(self, pattern='*'):
        return [p for n, p in self._pars.items() if fnmatch.fnmatchcase(n, pattern)]

    def chans(self, pattern='*'):
        return [c for c in self._chans if fnmatch.fnmatchcase(c.name, pattern)]

    def findChildren(self, maxDepth=None, _d=1):
        out = []
        for c in self.children:
            out.append(c)
            if maxDepth is None or _d < maxDepth:
                out.extend(c.findChildren(maxDepth=maxDepth, _d=_d + 1))
        return out

    def errors(self, recurse=False):
        parts = [self._errors] + ([c.errors(True) for c in self.children] if recurse else [])
        return '\n'.join(p for p in parts if p)

    def warnings(self, recurse=False):
        parts = [self._warnings] + ([c.warnings(True) for c in self.children] if recurse else [])
        return '\n'.join(p for p in parts if p)

    def save(self, file):
        self.saved = file
        return file

    @property
    def docked(self):
        return [c for c in self.parent.children if c.dock is self] if self.parent is not None else []

    def cook(self, force=False):
        if self.OPType == 'infoDAT':
            target = [c for c in self.parent.children if c.path == self.par.op.val]
            self.text = target[0].compile_log if target else ''

    def numpyArray(self):
        return self.image

    def sample(self, u=None, v=None):
        return self.pixel

    def create(self, optype, name=None):
        if not isinstance(optype, str):
            optype = optype.__name__
        if optype not in KNOWN_TYPES:
            raise TypeError('unknown operator type: %s' % optype)
        base = name or optype[:-len(KNOWN_TYPES[optype])].lower() + '1'
        taken = {c.name for c in self.children}
        while base in taken:
            head = base.rstrip('0123456789')
            base = head + str(int(base[len(head):] or 0) + 1)
        o = OP(base, optype, KNOWN_TYPES[optype], self)
        if optype.startswith('noise'):
            o.addPar('seed', 1, style='Int')
        if optype == 'glslTOP':
            o.addPar('pixeldat', '', style='DAT')
        if optype == 'textDAT':
            o.addPar('language', 'input', style='Menu')
        if optype == 'annotateCOMP':
            o.addPar('Titletext', '', style='Str')
            o.addPar('Bodytext', '', style='Str')
        if optype == 'infoDAT':
            o.addPar('op', '', style='OP')
        if optype == 'moviefileoutTOP':
            o.addPar('videocodec', 'mjpa', style='Menu', menuNames=['mjpa', 'h264', 'hap'])
            o.addPar('file', 'TDMovieOut.0.mov', style='File', expr="'TDMovieOut' + me.fileSuffix")
            o.addPar('record', 0, style='Toggle')
            rec, movie = o.par.record, o

            class Recording(Par):
                """Writes the file when recording starts, as the real TOP does once it has frames."""
                @property
                def val(self):
                    return self._val

                @val.setter
                def val(self, v):
                    self._val = v
                    if v and getattr(movie, 'writes', True):
                        with open(movie.par.file.val, 'wb') as fp:
                            fp.write(b'movie')
            rec.__class__ = Recording
        return o

    def copy(self, src, name=None):
        o = self.create(src.OPType, name or src.name)
        o._pars = {n: Par(n, p.val, default=p.default) for n, p in src._pars.items()}
        return o

    def destroy(self):
        for i in list(self.inputs):
            i.outputs.remove(self)
        for o in list(self.outputs):
            o.inputs.remove(self)
        self.parent.children.remove(self)


KNOWN_TYPES = {'noiseTOP': 'TOP', 'nullTOP': 'TOP', 'levelTOP': 'TOP', 'textDAT': 'DAT', 'baseCOMP': 'COMP',
               'noiseCHOP': 'CHOP', 'boxSOP': 'SOP', 'glslTOP': 'TOP', 'infoDAT': 'DAT', 'annotateCOMP': 'COMP',
               'moviefileoutTOP': 'TOP', 'geometryCOMP': 'COMP', 'renderTOP': 'TOP'}


class AbsTime:
    """Absolute time. seconds follows frame at 60 frames a second."""

    def __init__(self):
        self.frame = 0

    @property
    def seconds(self):
        return self.frame / 60.0


class World:
    """Holds the namespace handed to the bridge script and the means to advance frames."""

    def __init__(self):
        self.root = OP('', 'baseCOMP', 'COMP')
        self.absTime = AbsTime()
        self.queue = []
        self.frame_step = 1
        self.knows_delay_ref = True
        self.undo_blocks = []
        self.undo_stack = []      # newest first, as in TD
        self.undone = 0
        tdres = types.SimpleNamespace(name='TDResources')
        self.run_calls = []

        def run(script, *args, delayFrames=0, **kw):
            if 'delayRef' in kw and not self.knows_delay_ref:
                raise TypeError("run() got an unexpected keyword argument 'delayRef'")
            self.run_calls.append({'delayFrames': delayFrames, 'delayRef': kw.get('delayRef')})
            if callable(script):
                self.queue.append((script, args))
            else:
                # The string form. Only "args[0]()", which the bridge uses, is understood.
                assert script == 'args[0]()', script
                self.queue.append((args[0], ()))

        def op(path):
            if path == '/':
                return self.root
            cur = self.root
            for part in path.strip('/').split('/'):
                cur = next((c for c in cur.children if c.name == part), None)
                if cur is None:
                    return None
            return cur

        op.TDResources = tdres
        self.tdresources = tdres
        self.globals = {
            'run': run, 'op': op, 'root': self.root, 'absTime': self.absTime, 'ParMode': ParMode,
            'project': types.SimpleNamespace(name='fake.toe', cookRate=60.0, realTime=True),
            'app': types.SimpleNamespace(build='2025.32460', samplesFolder=''),
            'families': {},
            'ui': types.SimpleNamespace(undo=types.SimpleNamespace(
                undoStack=self.undo_stack,
                startBlock=lambda name: (self.undo_blocks.append(name), self.undo_stack.insert(0, name)),
                endBlock=lambda: self.undo_blocks.append('end'),
                undo=lambda: (setattr(self, 'undone', self.undone + 1), self.undo_stack and self.undo_stack.pop(0)))),
        }
        for t, fam in KNOWN_TYPES.items():
            cls = type(t, (), {})
            self.globals[t] = cls
            self.globals['families'].setdefault(fam, []).append(cls)

    def step(self):
        """Advance one frame. Updates cookAbsFrame of OPs that cook every frame, then calls the queued runs."""
        self.absTime.frame += self.frame_step
        for o in self.root.findChildren():
            if o.cooks_every_frame:
                o.cookAbsFrame = self.absTime.frame
        q, self.queue = self.queue, []
        for fn, args in q:
            fn(*args)
