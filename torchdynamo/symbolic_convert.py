import collections
import dataclasses
import dis
import functools
import inspect
import itertools
import logging
import pprint
import types
from typing import List

import torch
from torch import fx
from torch.fx import GraphModule

from .allowed_functions import is_allowed
from .bytecode_transformation import Instruction
from .bytecode_transformation import create_instruction
from .bytecode_transformation import debug_checks
from .bytecode_transformation import transform_code_object
from .bytecode_transformation import unique_id
from .guards import Guard, GuardedCode
from .guards import GuardRequirement
from .guards import GuardSource
from .variable_tracker import AllowedFunctionOrModuleVariable
from .variable_tracker import ConstDictVariable
from .variable_tracker import ConstantVariable
from .variable_tracker import GetAttrVariable
from .variable_tracker import ListVariable
from .variable_tracker import NNModuleVariable
from .variable_tracker import SliceVariable
from .variable_tracker import TensorVariable
from .variable_tracker import TracingSupported
from .variable_tracker import TupleVariable
from .variable_tracker import VariableTracker

DEBUG = False
counters = collections.defaultdict(collections.Counter)


def unimplemented(name):
    counters["unimplemented"][name] += 1
    raise NotImplementedError(name)


def stack_op(fn):
    nargs = len(inspect.signature(fn).parameters)

    @functools.wraps(fn)
    def impl(self, inst):
        inputs = self.popn(nargs)

        cls = VariableTracker.combine_type(inputs)
        options = VariableTracker.propagate(inputs)
        if issubclass(cls, TensorVariable):
            val = cls(proxy=fn(*[i.as_proxy() for i in inputs]),
                      **options)
        else:
            unimplemented(f"stack_op {cls.__name__}")

        self.push(val)

    return impl


@dataclasses.dataclass
class LocalArg:
    name: str

    def load(self, tracer):
        return tracer.create_load_fast(self.name)


@dataclasses.dataclass
class GlobalArg:
    name: str

    def load(self, tracer):
        return tracer.create_load_global(self.name)


class InstructionTracer(fx.Tracer):
    def __init__(self, instructions: List[Instruction], f_locals, f_globals, f_builtins, code_options):
        super(InstructionTracer, self).__init__()
        self.graph = fx.Graph()
        self.instructions = instructions
        self.stack = []
        self.f_globals = f_globals
        self.f_builtins = f_builtins
        self.indexof = {id(i): n for n, i in enumerate(instructions)}
        self.instruction_pointer = 0
        self.cnt = itertools.count()
        self.graphargs = []
        self.code_options = code_options
        self.nn_modules = {}
        self.guards = set()

        self.symbolic_locals = {k: self.wrap_local(k, f_locals[k])
                                for k in code_options["co_varnames"]
                                if k in f_locals}
        if DEBUG:
            print("names     ", code_options["co_names"])
            print("varnames  ", code_options["co_varnames"])
            print("cellvars  ", code_options["co_cellvars"])
            print("freevars  ", code_options["co_freevars"])
            print("consts    ", code_options["co_consts"])
            print("stacksize ", code_options["co_stacksize"])
            print("argnames  ", self.graphargs)

    def create_load_fast(self, name):
        assert name in self.code_options["co_varnames"]
        return create_instruction("LOAD_FAST",
                                  self.code_options["co_varnames"].index(name),
                                  name)

    def create_load_global(self, name):
        assert name in self.code_options["co_names"]
        return create_instruction("LOAD_GLOBAL",
                                  self.code_options["co_names"].index(name),
                                  name)

    def wrap_local(self, name, value):
        if isinstance(value, torch.Tensor):
            self.graphargs.append(LocalArg(name))
            return TensorVariable(
                proxy=self.create_graph_input(name),
                state=TracingSupported.YES,
                guards={Guard(name, GuardSource.LOCAL, GuardRequirement.TYPE_MATCH)},
            )
        elif isinstance(value, torch.nn.Module):
            key = f"{name}_{next(self.cnt)}"
            self.nn_modules[key] = value
            return NNModuleVariable(
                key=key,
                state=TracingSupported.YES,
                guards={Guard(name, GuardSource.LOCAL, GuardRequirement.VALUE_MATCH)},
            )
        elif value is True or value is False or value is None:
            # For these, just specialize on exact value
            return ConstantVariable(
                value=value,
                guards={Guard(name, GuardSource.LOCAL, GuardRequirement.VALUE_MATCH)},
            )
        else:
            unimplemented(f"wrap_local: {type(value).__name__}")

    def create_graph_input(self, name):
        placeholders = [n for n in self.graph.nodes if n.op == "placeholder"]
        if placeholders:
            ctx = self.graph.inserting_after(placeholders[-1])
        else:
            ctx = self.graph.inserting_before(None)
        with ctx:
            return self.create_proxy('placeholder', f'{name}_{next(self.cnt)}', (), {})

    def call_function(self, fn, args, kwargs):
        if isinstance(fn, AllowedFunctionOrModuleVariable):
            options = VariableTracker.propagate([fn, ] + list(args) + list(kwargs.values()))
            self_should_be_none = getattr(fn.value, "__self__", None)
            if self_should_be_none is not None:
                # weird ones like torch.nn.functional.avg_pool2d have __self__
                assert (isinstance(self_should_be_none, types.ModuleType) and
                        self_should_be_none.__name__ == getattr(fn.value, "__module__", None))
            proxy_args = tuple(arg.as_proxy() for arg in args)
            proxy_kwargs = {key: arg.as_proxy() for key, arg in kwargs.items()}
            self.push(TensorVariable(
                proxy=self.create_proxy('call_function', fn.value, proxy_args, proxy_kwargs),
                **options
            ))
        elif isinstance(fn, GetAttrVariable):
            name = fn.name
            obj = fn.obj
            args = [obj] + list(args)
            options = VariableTracker.propagate([fn, ] + list(args) + list(kwargs.values()))
            proxy_args = tuple(arg.as_proxy() for arg in args)
            proxy_kwargs = {key: arg.as_proxy() for key, arg in kwargs.items()}
            self.push(TensorVariable(
                proxy=self.create_proxy('call_method', name, proxy_args, proxy_kwargs),
                **options
            ))
        elif isinstance(fn, NNModuleVariable):
            mod = self.get_submodule(fn.key)
            if is_allowed(mod.__class__):
                options = VariableTracker.propagate([fn] + args)
                proxy_args = tuple(x.as_proxy() for x in args)
                self.push(TensorVariable(
                    proxy=self.create_proxy('call_module', fn.key, proxy_args, {}),
                    **options
                ))
            else:
                unimplemented("call custom module")
        else:
            unimplemented("call_function")

    def step(self):
        inst = self.instructions[self.instruction_pointer]
        self.instruction_pointer += 1
        if not hasattr(self, inst.opname):
            unimplemented(f"missing: {inst.opname}")
        getattr(self, inst.opname)(inst)
        return inst.opname != "RETURN_VALUE"

    def run(self):
        while self.step():
            pass

    def push(self, val):
        self.stack.append(val)

    def pop(self):
        return self.stack.pop()

    def popn(self, n):
        return list(reversed([self.pop() for _ in range(n)]))

    def LOAD_FAST(self, inst):
        self.push(self.symbolic_locals[inst.argval])

    def STORE_FAST(self, inst):
        self.symbolic_locals[inst.argval] = self.pop()

    def LOAD_CONST(self, inst):
        self.push(ConstantVariable(value=inst.argval,
                                   state=TracingSupported.UNKNOWN))

    def LOAD_GLOBAL(self, inst):
        try:
            value = self.f_globals[inst.argval]
        except KeyError:
            return self.load_builtin(inst)
        if is_allowed(value):
            self.push(AllowedFunctionOrModuleVariable(
                value=value,
                state=TracingSupported.YES,
                guards={Guard(inst.argval, GuardSource.GLOBAL, GuardRequirement.FUNCTION_MATCH)},
            ))
        elif isinstance(value, torch.Tensor):
            assert False, "TODO(jansel): need to debug a crash here"
            # turn a load of a global tensor into an arg for the graph
            self.graphargs.append(GlobalArg(inst.argval))
            self.push(TensorVariable(
                proxy=self.create_graph_input(inst.argval),
                state=TracingSupported.YES,
                guards={Guard(inst.argval, GuardSource.GLOBAL, GuardRequirement.TYPE_MATCH)},
            ))
        else:
            unimplemented("LOAD_GLOBAL")

    def load_builtin(self, inst):
        assert inst.argval in self.f_builtins
        unimplemented(f"load_builtin: {inst.argval}")

    def jump(self, inst):
        self.instruction_pointer = self.indexof[id(inst.target)]

    def POP_JUMP_IF_FALSE(self, inst):
        value = self.pop()
        self.guards.update(value.guards)
        if isinstance(value, ConstantVariable):
            if not value.value:
                self.jump(inst)
        else:
            unimplemented(f"POP_JUMP_IF_FALSE {type(value).__name__}")

    def CALL_FUNCTION(self, inst):
        args = self.popn(inst.argval)
        fn = self.pop()
        self.call_function(fn, args, {})

    def CALL_FUNCTION_EX(self, inst):
        if inst.argval == 0:
            kwargsvars = ConstDictVariable({})
            argsvars = self.pop()
        elif inst.argval == 1:
            kwargsvars = self.pop()
            argsvars = self.pop()
        else:
            unimplemented("CALL_FUNCTION_EX")
        fn = self.pop()
        assert isinstance(argsvars, ListVariable)
        assert isinstance(kwargsvars, ConstDictVariable)
        self.call_function(fn, argsvars.items, kwargsvars.items)

    def CALL_FUNCTION_KW(self, inst):
        argnames = self.pop()
        args = self.popn(inst.argval)
        fn = self.pop()
        assert isinstance(argnames, ConstantVariable)
        argnames = argnames.value
        args, kwargs = args[:-len(argnames)], args[-len(argnames):]
        kwargs = dict(zip(argnames, kwargs))
        assert len(kwargs) == len(argnames)
        self.call_function(fn, args, kwargs)

    def get_submodule(self, keys):
        assert keys
        obj = self.nn_modules
        for k in keys.split("."):
            if isinstance(obj, dict):
                obj = obj[k]
            else:
                obj = getattr(obj, k)
        return obj

    def LOAD_METHOD(self, inst):
        self.LOAD_ATTR(inst)
        self.push(None)

    def CALL_METHOD(self, inst):
        args = self.popn(inst.argval)
        dummy = self.pop()
        assert dummy is None
        fn = self.pop()
        self.call_function(fn, args, {})

    def LOAD_ATTR(self, inst):
        obj = self.pop()
        name = inst.argval
        options = VariableTracker.propagate([obj])
        if isinstance(obj, NNModuleVariable):
            key = f"{obj.key}.{name}"
            subobj = self.get_submodule(key)
            if isinstance(subobj, torch.Tensor):
                self.push(TensorVariable(
                    proxy=self.create_proxy("get_attr", key, tuple(), {}),
                    **options
                ))
            elif isinstance(subobj, torch.nn.Module):
                self.push(NNModuleVariable(
                    key,
                    **options
                ))
            else:
                unimplemented(f"nn.Module attr {type(subobj).__name__}")
        elif isinstance(obj, TensorVariable):
            self.push(GetAttrVariable(obj, name, **options))
        elif isinstance(obj, AllowedFunctionOrModuleVariable):
            self.push(AllowedFunctionOrModuleVariable(
                value=getattr(obj.value, name),
                **options
            ))
        else:
            unimplemented("LOAD_ATTR")

    def BUILD_TUPLE(self, inst):
        items = self.popn(inst.argval)
        options = VariableTracker.propagate(items)
        self.push(TupleVariable(items, **options))

    def BUILD_SLICE(self, inst):
        items = self.popn(inst.argval)
        options = VariableTracker.propagate(items)
        self.push(SliceVariable(items, **options))

    def BUILD_LIST(self, inst):
        items = self.popn(inst.argval)
        options = VariableTracker.propagate(items)
        self.push(ListVariable(items, **options))

    def BUILD_MAP(self, inst):
        items = self.popn(inst.argval * 2)
        options = VariableTracker.propagate(items)
        result = dict()
        for k, v in zip(items[::2], items[1::2]):
            assert isinstance(k, ConstantVariable)
            result[k.value] = v
        assert len(result) == len(items) / 2
        self.push(ConstDictVariable(result, **options))

    def BUILD_CONST_KEY_MAP(self, inst):
        keys = self.pop()
        values = self.popn(inst.argval)
        options = VariableTracker.propagate([keys] + values)
        assert isinstance(keys, ConstantVariable)
        keys = keys.value
        assert isinstance(keys, tuple)
        assert len(keys) == len(values)
        self.push(ConstDictVariable(dict(zip(keys, values)), **options))

    def UNPACK_SEQUENCE(self, inst):
        seq = self.pop()
        if isinstance(seq, ListVariable):
            assert len(seq.items) == inst.argval
            for i in reversed(seq.items):
                self.push(i)
        else:
            unimplemented(f"UNPACK_SEQUENCE {type(seq).__name__}")

    def RETURN_VALUE(self, inst):
        rv = self.pop()
        if rv.state == TracingSupported.YES:
            if isinstance(rv, TensorVariable):
                self.create_node('output', 'output', (self.create_arg(rv.proxy),), {})
            else:
                unimplemented(f"RETURN_VALUE {type(rv).__name__}")
            ncalls = count_calls(self.graph)
            counters["stats"]["calls_captured"] += ncalls
            counters["stats"]["fusions_possible"] += ncalls - 1
            DEBUG and self.graph.print_tabular()
            self.guards.update(rv.guards)
            gm = GraphModule(FakeRootModule(self.nn_modules), self.graph)
            gm.recompile()
            name = unique_id("__translated_fn")
            self.f_globals[name] = gm.forward
            self.code_options["co_names"] = tuple(self.code_options["co_names"]) + (name,)
            self.code_options["co_stacksize"] = len(self.graphargs) + 1
            self.instructions[:] = (
                    [self.create_load_global(name)] +
                    [arg.load(self) for arg in self.graphargs] +
                    [create_instruction("CALL_FUNCTION", len(self.graphargs)),
                     create_instruction("RETURN_VALUE")]
            )
        else:
            unimplemented("not traceable")

    def NOP(self, inst):
        pass

    def POP_TOP(self, inst):
        self.pop()

    def ROT_TWO(self, inst):
        a = self.pop()
        b = self.pop()
        self.push(a)
        self.push(b)

    def ROT_THREE(self, inst):
        a = self.pop()
        b = self.pop()
        c = self.pop()
        self.push(a)
        self.push(c)
        self.push(b)

    def ROT_FOUR(self, inst):
        a = self.pop()
        b = self.pop()
        c = self.pop()
        d = self.pop()
        self.push(a)
        self.push(d)
        self.push(c)
        self.push(b)

    def DUP_TOP(self, inst):
        a = self.pop()
        self.push(a)
        self.push(a)

    def DUP_TOP_TWO(self, inst):
        a = self.pop()
        b = self.pop()
        self.push(b)
        self.push(a)
        self.push(b)
        self.push(a)

    UNARY_POSITIVE = stack_op(lambda tos: +tos)
    UNARY_NEGATIVE = stack_op(lambda tos: -tos)
    UNARY_NOT = stack_op(lambda tos: not tos)
    UNARY_INVERT = stack_op(lambda tos: ~tos)

    # GET_ITER
    # GET_YIELD_FROM_ITER

    BINARY_POWER = stack_op(lambda tos1, tos: tos1 ** tos)
    BINARY_MULTIPLY = stack_op(lambda tos1, tos: tos1 * tos)
    BINARY_MATRIX_MULTIPLY = stack_op(lambda tos1, tos: tos1 @ tos)
    BINARY_FLOOR_DIVIDE = stack_op(lambda tos1, tos: tos1 // tos)
    BINARY_TRUE_DIVIDE = stack_op(lambda tos1, tos: tos1 / tos)
    BINARY_MODULO = stack_op(lambda tos1, tos: tos1 % tos)
    BINARY_ADD = stack_op(lambda tos1, tos: tos1 + tos)
    BINARY_SUBTRACT = stack_op(lambda tos1, tos: tos1 - tos)
    BINARY_SUBSCR = stack_op(lambda tos1, tos: tos1[tos])
    BINARY_LSHIFT = stack_op(lambda tos1, tos: tos1 << tos)
    BINARY_RSHIFT = stack_op(lambda tos1, tos: tos1 >> tos)
    BINARY_AND = stack_op(lambda tos1, tos: tos1 & tos)
    BINARY_XOR = stack_op(lambda tos1, tos: tos1 ^ tos)
    BINARY_OR = stack_op(lambda tos1, tos: tos1 | tos)


class FakeRootModule(torch.nn.Module):
    """ Trick the constructor of fx.GraphModule """

    def __init__(self, nn_modules: dict):
        super(FakeRootModule, self).__init__()
        training = None
        for k, v in nn_modules.items():
            setattr(self, k, v)
            training2 = getattr(v, "training", None)
            assert None in (training, training2) or training == training2
            if training2 is not None:
                training = training2


def count_calls(g: fx.Graph):
    c = 0
    for n in g.nodes:
        if "call" in n.op:
            c += 1
    return c


def convert_frame_assert(frame: types.FrameType):
    code = frame.f_code
    if code.co_filename.startswith("<eval_with_key>"):
        return GuardedCode(code)  # skip FX output
    # TODO(jansel): detect and skip other types of generated code
    debug_checks(code)
    guards = None

    def transform(instructions, code_options):
        nonlocal guards
        tracer = InstructionTracer(instructions,
                                   frame.f_locals,
                                   frame.f_globals,
                                   frame.f_builtins,
                                   code_options)
        tracer.run()
        guards = tracer.guards

    code = transform_code_object(frame.f_code, transform)
    if DEBUG:
        print("ORIGINAL")
        print(dis.Bytecode(code).info())
        print(dis.Bytecode(code).dis())
        print("NEW CODE")
        print(dis.Bytecode(code).info())
        print(dis.Bytecode(code).dis())
        pprint.pprint(guards)
    assert guards is not None
    return GuardedCode(code, guards)


def convert_frame(frame: types.FrameType):
    counters["frames"]["total"] += 1
    try:
        result = convert_frame_assert(frame)
        counters["frames"]["ok"] += 1
        return result
    except NotImplementedError:
        pass
    except Exception as e:
        logging.exception(f"ERROR\n{dis.Bytecode(frame.f_code).dis()}")
        # _, _, exc_tb = sys.exc_info()
        # frame = exc_tb.tb_frame.f_back
        # filename = os.path.split(frame.f_code.co_filename)[-1]
        # counters["errors"][f"{e.__class__.__name__}:{e} [{filename}:{frame.f_lineno}]"] += 1
    return GuardedCode(frame.f_code)