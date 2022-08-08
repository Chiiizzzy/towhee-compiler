from torchdynamo import optimize

from .jit.hook import compile
from towhee.functional import param_scope


class JITCompile:
    def __init__(self, backend="nebullvm", feature=False, **kws) -> None:
        self._params = param_scope(towhee=param_scope(compiler=param_scope(**kws)))
        self._backend = backend
        self._feature = feature

    def __enter__(self):
        self._backend = optimize(self._backend) if self._feature == False else compile(self._backend)

        self._params.__enter__()
        self._backend.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._backend.__exit__(exc_type, exc_val, exc_tb)
        self._params.__exit__(exc_type, exc_val, exc_tb)


def jit_compile(*arg, **kws):
    return JITCompile(*arg, **kws)
