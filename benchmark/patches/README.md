# Native compatibility patches

Pinned commits, SHA-256 and adapter sources: [baseline lock](../sources/baselines.lock.json).

* AMI: accept system messages without changing their roles.
* InvMem: preserve unknown recording dates.
* ReFind: accept system messages.

Apply each patch only to its pinned upstream checkout. Keep upstream attribution and licenses.
InvMem/ReFind adapters are in `src/memory_benchmark/methods/`; the frozen-worker bridge is in `src/memory_benchmark/runtime/native_bridge/`. Native implementations are fetched separately.
