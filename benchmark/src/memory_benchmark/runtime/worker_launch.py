"""Linux mount/PID isolation for memory workers, with no host-root fallback.

Only vetted code, Python environments, a history/query pair and this history's
memory state are mounted. Prepared targets, other jobs and the host home are
absent. Provider networking remains available; this is a filesystem boundary,
not an isolated remote service for arbitrary hostile code.
"""
from functools import lru_cache
import json
import os
from pathlib import Path
import shutil
import subprocess

from .artifacts import file_hash, write_json
from .native_freeze import snapshot_bindings, verify_method
from .profile import read_profile

POLICY = "bwrap_memory_allowlist_v1"


@lru_cache(maxsize=16)
def python_prefixes(executable):
    data = subprocess.check_output([executable, "-I", "-c",
        "import json,sys;print(json.dumps([sys.prefix,sys.base_prefix]))"], text=True, timeout=15)
    return sorted({Path(p).resolve() for p in json.loads(data)}, key=lambda p: len(p.parts))


def sandbox_command(command, *, readonly, writable, cwd, env):
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("Memory worker requires bubblewrap; install it before running")
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
            "--unshare-ipc", "--unshare-uts", "--cap-drop", "ALL", "--clearenv",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    for key, value in env.items():
        args += ["--setenv", key, str(value)]
    bindings = []
    for name in ("/usr", "/lib", "/lib64", "/bin", "/sbin"):
        if Path(name).exists():
            bindings.append((Path(name), Path(name), False))
    for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs", "/etc/localtime"):
        if Path(name).exists():
            bindings.append((Path(name), Path(name), False))
    bindings += [(Path(a), Path(b), False) for a, b in readonly]
    bindings += [(Path(p), Path(p), True) for p in writable]
    for source, target, _ in bindings:
        for parent_source, parent_target, parent_write in bindings:
            if (not parent_write and parent_source != parent_target and parent_source.is_dir()
                    and target != parent_target and target.is_relative_to(parent_target)):
                # A venv/cache may live under the original native checkout.
                # Its mount point must exist before that code copy becomes RO.
                point = parent_source / target.relative_to(parent_target)
                if source.is_dir():
                    point.mkdir(parents=True, exist_ok=True)
                else:
                    point.parent.mkdir(parents=True, exist_ok=True)
                    point.touch(exist_ok=True)
    # Parent mounts must precede nested venv/code mounts.
    for source, target, write in sorted(bindings, key=lambda item: len(item[1].parts)):
        if target == Path("/"):
            raise ValueError("A worker must never mount the host root")
        args += ["--bind" if write else "--ro-bind", str(source), str(target)]
    args += ["--chdir", str(cwd), "--", *map(str, command)]
    return args


def run_memory_worker(job, **kwargs):
    try:
        return _run_memory_worker(job, **kwargs)
    finally:
        # Also clean up when sandbox preparation or namespace creation fails.
        (Path(job["output"]) / "worker_inputs/provider.env").unlink(missing_ok=True)


def _run_memory_worker(job, *, executable, framework, queue_output, record,
                      timeout, log, script=None, extra_readonly=(), extra_writable=()):
    output = Path(job["output"]).resolve()
    executable = Path(executable).absolute()
    framework, queue_output = Path(framework).resolve(), Path(queue_output).resolve()
    if not job.get("offline"):
        verify_method(record, job["runtime"])
    # Copy exact sanitized inputs. The worker never receives prepared paths.
    inputs = output / "worker_inputs"
    inputs.mkdir(mode=0o700)
    job = {**job, "runtime": dict(job["runtime"])}
    job["method_config"] = dict(job["method_config"])
    for key in ("source_root", "lightmem_root", "memma_root", "adapter_file", "compression_model", "fastembed_cache"):
        if key in job["method_config"]:
            job["method_config"][key] = str(Path(job["method_config"][key]).resolve())
    for field, name in (("history", "history.json"), ("queries", "queries.jsonl")):
        target = inputs / name
        shutil.copyfile(job[field], target)
        if file_hash(target) != job["input_hashes"][field]:
            raise ValueError("Worker input changed while copying")
        job[field] = str(target)
    if not job.get("offline") and job["method"] != "aml_http":
        profile = read_profile(job["runtime"]["env_file"])
        # File permissions and mount scope exclude unrelated workspace secrets.
        profile = {k: v for k, v in profile.items() if k.startswith(("AMI_", "OPENAI_"))}
        private = inputs / "provider.env"
        with open(private, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            handle.write("\n".join(f"{key}={value}" for key, value in profile.items()) + "\n")
        job["runtime"]["env_file"] = "/run/aml-provider.env"
    job["native_code"] = record["native_code"]
    job["worker_isolation"] = POLICY
    job_path = output / "job.json"
    write_json(job_path, job)
    readonly = [(framework / "src", framework / "src")]
    if (inputs / "provider.env").exists():
        readonly.append((inputs / "provider.env", Path("/run/aml-provider.env")))
    readonly += snapshot_bindings(record, queue_output, job["method"])
    readonly += [(prefix, prefix) for prefix in python_prefixes(str(executable))
                 if not prefix.is_relative_to("/usr")]
    readonly += list(extra_readonly)
    # Tokenizer caches contain only public model vocabulary. No home-directory
    # or shared run tree is exposed to satisfy an incidental cache miss.
    cache = Path(os.environ.get("TIKTOKEN_CACHE_DIR", "/tmp/data-gym-cache"))
    if cache.is_dir():
        readonly.append((cache, Path("/tmp/tokenizer-cache")))
    for key in ("compression_model", "fastembed_cache"):
        if not job.get("offline") and key in job["method_config"]:
            path = Path(job["method_config"][key]).resolve()
            readonly.append((path, path))
    writable = [output, *extra_writable]
    if job["runtime"].get("memory_checkpoint_policy"):
        resume = (output.parent if output.name.startswith("memory_attempt_") else output) / "memory_resume"
        resume.mkdir(exist_ok=True, mode=0o700)
        writable.append(resume)
    command = [str(executable), "-u"]
    command += [str(script)] if script else ["-m", "memory_benchmark.runtime.worker"]
    command.append(str(job_path))
    env = {"PATH": f"{Path(executable).parent}:/usr/bin:/bin", "HOME": "/tmp/home",
           "PYTHONPATH": str(framework / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONUNBUFFERED": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false",
           "TIKTOKEN_CACHE_DIR": "/tmp/tokenizer-cache", "HF_HOME": "/tmp/hf",
           "XDG_CACHE_HOME": "/tmp/cache"}
    if job["method"] == "aml_http":
        key = job["method_config"].get("token_env", "AML_MEMORY_API_KEY")
        if key in os.environ:
            env[key] = os.environ[key]
    args = sandbox_command(command, readonly=readonly, writable=writable, cwd=output, env=env)
    write_json(output / "isolation.json", {"policy": POLICY, "read_only_mounts": [str(b) for _, b in readonly],
               "writable_mounts": list(map(str, writable)), "host_root_mounted": False,
               "pid_namespace": True, "network": "shared_for_provider_requests"})
    process = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, timeout=timeout,
                             close_fds=True, env={"PATH": "/usr/bin:/bin"})
    # Do not let a method turn the privileged coordinator into a file reader
    # by returning symlinks to files outside its sandbox.
    for name in ("report.json", "retrievals.jsonl"):
        path = output / name
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
            raise ValueError("Worker artifacts must be independent regular files")
    return process
