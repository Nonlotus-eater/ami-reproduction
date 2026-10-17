"""Read the process's cgroup quota, not the host's advertised capacity."""
from pathlib import Path
import os


def limits(base=Path('/sys/fs/cgroup'), *, affinity_count=None):
    base = Path(base)
    cpu, memory = None, None
    if (base / 'cpu.max').exists():
        quota, period = (base / 'cpu.max').read_text().split()
        if quota != 'max':
            cpu = int(quota) / int(period)
    elif (base / 'cpu/cpu.cfs_quota_us').exists():
        quota = int((base / 'cpu/cpu.cfs_quota_us').read_text())
        period = int((base / 'cpu/cpu.cfs_period_us').read_text())
        if quota > 0:
            cpu = quota / period
    memory_path = base / 'memory.max'
    if not memory_path.exists():
        memory_path = base / 'memory/memory.limit_in_bytes'
    if memory_path.exists():
        raw = memory_path.read_text().strip()
        if raw != 'max' and int(raw) < 2 ** 60:
            memory = int(raw)
    eligible = affinity_count if affinity_count is not None else len(os.sched_getaffinity(0))
    return {'cpu_quota_cores': cpu, 'eligible_logical_cpus': eligible,
            'effective_cpu_cores': min(eligible, cpu) if cpu is not None else eligible,
            'memory_limit_bytes': memory}


def safe_parallelism(resource_limits, tasks):
    cpu_slots = max(1, int(resource_limits['effective_cpu_cores'] // 2))
    memory = resource_limits['memory_limit_bytes']
    memory_slots = max(1, int((memory - 128 * 1024 ** 2) // (1536 * 1024 ** 2))) if memory else tasks
    return min(tasks, cpu_slots, memory_slots)
