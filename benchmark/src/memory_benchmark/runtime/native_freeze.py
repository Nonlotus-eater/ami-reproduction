"""Fingerprint and copy runtime code AND non-secret native configuration."""
import os
from pathlib import Path
import re
import shutil
import json
import subprocess

from .artifacts import contract_hash, file_hash
from .profile import read_profile

ROOT_KEYS = ("source_root", "lightmem_root", "memma_root")
SUFFIXES = {".py", ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".j2", ".jinja"}
EXCLUDED = {"__pycache__", "node_modules", "dataset", "datasets", "models", "runs", "artifacts", "tests", "test"}
SECRET = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:$|_)", re.I)


def native_files(root):
    root = Path(root).resolve(strict=True)
    hashes = {}
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in EXCLUDED)
        for name in sorted(files):
            if name != ".env.example" and (name.startswith(".") or Path(name).suffix not in SUFFIXES):
                continue
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError("Native runtime files must not be symlinks")
            hashes[str(path.relative_to(root))] = file_hash(path)
    if not any(name.endswith(".py") for name in hashes):
        raise ValueError("Native source directory has no Python code")
    return hashes


def native_identity(config):
    result = {}
    for key in ROOT_KEYS:
        if key not in config:
            continue
        root = Path(config[key]).resolve()
        hashes = native_files(root)
        result[key] = {"path": str(root), "python_files": sum(p.endswith(".py") for p in hashes),
                       "sha256": contract_hash(hashes), "files": hashes}
    return result


def profile_identity(runtime):
    # Credential rotation does not change algorithm identity; every other
    # consumed profile setting (e.g. extraction tokens) does.
    profile = read_profile(runtime["env_file"])
    return contract_hash({k: v for k, v in profile.items() if not SECRET.search(k)})


def environment_identity(executable):
    script = ("import importlib.metadata as m,json,sys;"
              "print(json.dumps({'python':sys.version,'packages':"
              "sorted((d.metadata['Name'],d.version) for d in m.distributions())}))")
    value = json.loads(subprocess.check_output([executable, "-I", "-c", script], text=True, timeout=30))
    return {"executable_sha256": file_hash(Path(executable).resolve()),
            "packages_sha256": contract_hash(value), "python": value["python"]}


def freeze_methods(methods, output, runtime, *, offline=False):
    result = {}
    for name, config in methods.items():
        identity = {} if offline else native_identity(config)
        for key, source in identity.items():
            target = Path(output) / "frozen_native" / name / key
            for relative, digest in source["files"].items():
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(source["path"]) / relative, destination)
                if file_hash(destination) != digest:
                    raise ValueError("Native runtime changed while freezing")
        result[name] = {"config": config, "native_code": identity,
                        "profile_sha256": None if offline or name == "aml_http" else profile_identity(runtime),
                        "environment": None if offline else environment_identity(config["python"])}
    return result


def verify_method(record, runtime):
    if native_identity(record["config"]) != record["native_code"]:
        raise ValueError("Native method code or configuration changed")
    expected = record.get("profile_sha256")
    if expected is not None and profile_identity(runtime) != expected:
        raise ValueError("Native effective profile changed")
    if record.get("environment") is not None and environment_identity(record["config"]["python"]) != record["environment"]:
        raise ValueError("Native Python environment changed")


def snapshot_bindings(record, output, name):
    bindings = []
    for key, source in record["native_code"].items():
        frozen = Path(output) / "frozen_native" / name / key
        if native_files(frozen) != source["files"]:
            raise ValueError("Frozen native runtime changed")
        bindings.append((frozen, Path(source["path"])))
    return bindings
