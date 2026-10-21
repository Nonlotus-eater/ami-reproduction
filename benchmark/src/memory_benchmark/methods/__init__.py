from importlib import import_module

MODULES = {'mem0': 'mem0', 'activememoryindex': 'activememoryindex', 'invmem': 'invmem', 'refind': 'refind'}


def create(name, config, history, output, scope, gateway, *, offline=False):
    if offline:
        from .fixture import Adapter
    else:
        Adapter = import_module(f"memory_benchmark.methods.{MODULES[name]}").Adapter
    return Adapter(config, history, output, scope, gateway)
