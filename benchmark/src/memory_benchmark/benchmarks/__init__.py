from .locomo_refined.loader import load as load_locomo
from .longmemeval.loader import load as load_longmemeval
from .public_datasets import load_beam, load_clbench, load_v1, load_v2

LOADERS = {"locomo_refined": load_locomo, "longmemeval_s": load_longmemeval,
           "beam": load_beam, "clbench": load_clbench, "personamem_v1": load_v1, "personamem_v2": load_v2}


def load_cases(config, **selection):
    name = config["name"]
    if name not in LOADERS:
        raise ValueError("Unknown benchmark")
    return LOADERS[name](config, **selection)
