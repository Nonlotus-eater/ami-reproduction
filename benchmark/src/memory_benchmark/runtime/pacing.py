"""Optional cross-process provider pacing; request contents stay unchanged."""
import fcntl
from pathlib import Path
import time

from .artifacts import read_json, write_json


def paced_transport(transport, policy, *, clock=None, sleeper=None):
    import httpx
    clock = clock or time.time
    sleeper = sleeper or time.sleep
    folder = Path(policy["state_directory"])
    folder.mkdir(parents=True, exist_ok=True)

    class PacedTransport(httpx.BaseTransport):
        def handle_request(self, request):
            with (folder / "request.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                path = folder / "state.json"
                state = read_json(path) if path.exists() else {}
                while state.get("next_start_unix", 0) > clock():
                    sleeper(min(5, state["next_start_unix"] - clock()))
                started = clock()
                state.update(last_started_unix=started, requests=state.get("requests", 0) + 1,
                             in_flight=1)
                write_json(path, state)
                try:
                    response = transport.handle_request(request)
                except Exception:
                    state.update(in_flight=0, next_start_unix=clock() + policy["minimum_interval_seconds"])
                    write_json(path, state)
                    raise
                finished = clock()
                state.update(in_flight=0, last_http_status=response.status_code,
                             next_start_unix=max(finished, started + policy["minimum_interval_seconds"]))
                if response.status_code in (429, 500, 502, 503, 504):
                    streak = state.get("consecutive_overload", 0) + 1
                    cooldown = min(policy["maximum_cooldown_seconds"],
                                   policy["overload_cooldown_seconds"] * 2 ** min(streak - 1, 10))
                    try:
                        cooldown = max(cooldown, float(response.headers.get("retry-after", 0)))
                    except ValueError:
                        pass
                    state.update(consecutive_overload=streak,
                        overload_events=state.get("overload_events", 0) + 1,
                        next_start_unix=finished + cooldown)
                elif response.status_code == 200:
                    state["consecutive_overload"] = 0
                write_json(path, state)
                return response

        def close(self):
            transport.close()

    return PacedTransport()
