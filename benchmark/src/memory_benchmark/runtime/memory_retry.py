"""Activate recorded request retries only in newly started memory workers."""
from pathlib import Path
from .artifacts import file_hash
from .execution_overlay import checked_changes, find_overlay
from .retry import RetryPolicy


def gateway_for_memory(job, output, gateway_class):
    overlay = find_overlay(Path(job['output']))
    policy = overlay[1].get('request_retry_policy') if overlay else None
    if policy is not None:
        root, receipt = overlay
        checked_changes(root, receipt)
        if policy.get('owner') != 'transport' or policy.get('version') != 1:
            raise ValueError('Unknown memory request retry overlay')
        RetryPolicy.from_profile({'AMI_ALLOW_RETRIES': '1'}, policy['max_retries'])
    gateway = gateway_class(job['runtime'], output, expected_identity=job['provider_identity'])
    if policy is None:
        return gateway
    try:
        effective = RetryPolicy.from_profile(gateway.profile, policy['max_retries'])
        if not effective.enabled:
            raise ValueError('Memory retry overlay requires AMI_ALLOW_RETRIES=1')
        # Job/runtime/store hashes stay unchanged. Effective retry policy and
        # the checked activation receipt are recorded separately in usage.json.
        gateway.budget.retry_policy = effective
        gateway.memory_retry_activation = {
            'activation': 'new_memory_workers', 'activated_at': policy['activated_at'],
            'receipt_sha256': file_hash(root / 'execution_overlay.json'),
            'baseline_framework_sha256': receipt['baseline_framework_sha256'],
            'queue': str(root), 'policy': effective.public(),
        }
        gateway.save()
        return gateway
    except BaseException:
        gateway.close()
        raise
