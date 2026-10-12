"""One bounded, cancellable request path for Kite candle history in this process."""
from collections import OrderedDict
from copy import deepcopy
import logging
import threading
import time


class HistoryCancelled(RuntimeError):
    pass


class HistoricalRequests:
    def __init__(self, spacing=0.5, attempts=3, cache_size=64, cache_ttl=60,
                 clock=time.monotonic, sleep=time.sleep, log=None):
        self.spacing = spacing
        self.attempts = attempts
        self.cache_size = cache_size
        self.cache_ttl = cache_ttl
        self.clock, self.sleep = clock, sleep
        self.log = log or logging.getLogger('kite.history')
        self.lock = threading.Lock()
        self.next_request_at = 0.
        self.cache = OrderedDict()
        self.requests = self.cache_hits = self.retries = self.rate_limits = 0

    def stats(self):
        # Do not block health requests behind an HTTP call or a retry delay.
        return {'requests': self.requests, 'cache_hits': self.cache_hits,
                'retries': self.retries, 'rate_limits': self.rate_limits,
                'max_requests_per_second': 1 / self.spacing}

    @staticmethod
    def _limited(exc):
        return getattr(exc, 'code', None) == 429 or 'too many requests' in str(exc).lower()

    def _wait(self, delay, stop):
        if stop is not None:
            if stop.is_set() or (delay > 0 and stop.wait(delay)):
                raise HistoryCancelled('History loading cancelled during shutdown')
        elif delay > 0:
            self.sleep(delay)

    def fetch(self, broker, token, start, end, interval, stop=None):
        # Group overlapping startup/activation requests made within the same
        # minute. The first response is only reused for up to 60 seconds.
        key = (broker, token, interval, start.replace(second=0, microsecond=0),
               end.replace(second=0, microsecond=0))
        while not self.lock.acquire(timeout=.1):
            self._wait(0, stop)
        try:
            self._wait(0, stop)
            now = self.clock()
            for cached_key, (created, _) in list(self.cache.items()):
                if now - created >= self.cache_ttl:
                    del self.cache[cached_key]
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                self.cache_hits += 1
                return deepcopy(cached[1])
            for attempt in range(self.attempts):
                self._wait(max(0, self.next_request_at - self.clock()), stop)
                self.next_request_at = self.clock() + self.spacing
                self.requests += 1
                try:
                    records = broker.historical_data(token, start, end, interval)
                except Exception as exc:
                    if not self._limited(exc):
                        raise
                    self.rate_limits += 1
                    delay = 2 ** (attempt + 1)
                    self.next_request_at = max(self.next_request_at, self.clock() + delay)
                    if attempt + 1 == self.attempts:
                        self.log.warning('History rate limit persists for token=%s interval=%s after %d attempts; next requests also respect backoff', token, interval, self.attempts)
                        raise
                    self.retries += 1
                    self.log.warning('History rate-limited for token=%s interval=%s; retry %d/%d after %ds',
                                     token, interval, attempt + 1, self.attempts - 1, delay)
                    continue
                # Do not cache failures or empty responses; real missing data can
                # recover on a later explicit load. Keep responses private.
                if records and self.cache_size:
                    self.cache[key] = (self.clock(), deepcopy(records))
                    while len(self.cache) > self.cache_size:
                        self.cache.popitem(last=False)
                return records
        finally:
            self.lock.release()
