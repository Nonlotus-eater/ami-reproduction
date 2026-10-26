"""Transport-owned retries; the OpenAI SDK remains configured with zero retries."""
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import random
import time


RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
FATAL_CODES = frozenset({'model_not_found', 'ModelNotFound', 'invalid_api_key',
    'InvalidApiKey', 'insufficient_quota', 'WorkSpaceNotFound', 'model_not_supported',
    'NotFound', 'RequestPath.NotFound', 'invalid_parameter', 'InvalidParameter'})


@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool
    max_retries: int
    maximum_retry_after: float = 300.0

    @classmethod
    def from_profile(cls, profile, max_retries=0):
        if type(max_retries) is not int or not 0 <= max_retries <= 100:
            raise ValueError('max_retries must be an integer from 0 to 100')
        flag = profile.get('AMI_ALLOW_RETRIES', '0')
        if flag not in {'0', '1'}:
            raise ValueError('AMI_ALLOW_RETRIES must be 0 or 1')
        return cls(flag == '1', max_retries)

    @property
    def effective_max_retries(self):
        return self.max_retries if self.enabled else 0

    def public(self):
        return {'owner': 'transport', 'enabled': self.enabled,
                'classification_version': 2,
                'max_retries': self.max_retries,
                'effective_max_retries': self.effective_max_retries,
                'max_attempts': self.effective_max_retries + 1, 'sdk_max_retries': 0,
                'maximum_retry_after_seconds': self.maximum_retry_after}

    def delay(self, retry_index, headers):
        """Honor a reasonable Retry-After; stop rather than retry before a long one."""
        value = headers.get('retry-after')
        seconds = None
        if value is not None:
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                try:
                    deadline = parsedate_to_datetime(value)
                    if deadline.tzinfo is None:
                        deadline = deadline.replace(tzinfo=timezone.utc)
                    seconds = max(0.0, deadline.timestamp() - time.time())
                except (TypeError, ValueError, OverflowError):
                    pass
        if seconds is not None and math.isfinite(seconds) and seconds >= 0:
            if seconds > self.maximum_retry_after:
                return None, 'retry_after_exceeds_limit'
            return seconds, 'retry_after'
        return min(60.0, 2.0 ** retry_index * (0.75 + random.random() * 0.5)), 'backoff'


def retryable_transport_error(error):
    import httpx
    return isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


def retryable_http_error(status, code, message, headers):
    if status not in RETRYABLE_STATUS or headers.get('x-should-retry', '').lower() == 'false':
        return False
    # This provider uses model_not_found for temporary distributor capacity.
    # A genuine 404/unknown-model response, quota or authentication error stays fatal.
    if (status == 503 and code == 'model_not_found' and isinstance(message, str)
            and message.casefold().startswith('no available channel for model ')):
        return True
    return code not in FATAL_CODES


def sleep(seconds):
    time.sleep(seconds)
