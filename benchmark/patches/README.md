# Native compatibility patches

Pinned commits, SHA-256 and adapter sources: [baseline lock](../sources/baselines.lock.json).

* AMI: accept system messages without changing their roles.
* InvMem: preserve unknown recording dates.
* ReFind: accept system messages.

Apply each patch only to its pinned upstream checkout. Keep upstream attribution and licenses.
InvMem/ReFind adapters and bridge are in `integrations/`; native implementations are fetched separately.
