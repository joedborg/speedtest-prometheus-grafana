import json
import logging
import math
import os
import random
import subprocess
import time
import urllib.error
import urllib.request


LOG = logging.getLogger("speedtest")
MIN_INTERVAL = 300
MAX_INTERVAL = 3600
TEST_TIMEOUT = 180

# Ookla reports bandwidth in bytes/s, and elapsed/latency values in milliseconds.
FIELDS = {
    "ping.latency": ("ping_latency_seconds", 0.001),
    "ping.jitter": ("ping_jitter_seconds", 0.001),
    "ping.low": ("ping_low_seconds", 0.001),
    "ping.high": ("ping_high_seconds", 0.001),
    "packetLoss": ("packet_loss_percent", 1),
}
for direction in ("download", "upload"):
    FIELDS.update({
        f"{direction}.bandwidth": (f"{direction}_bits_per_second", 8),
        f"{direction}.bytes": (f"{direction}_bytes", 1),
        f"{direction}.elapsed": (f"{direction}_duration_seconds", 0.001),
        **{
            f"{direction}.latency.{stat}": (
                f"{direction}_latency_{stat}_seconds", 0.001
            )
            for stat in ("iqm", "low", "high", "jitter")
        },
    })

REQUIRED_FIELDS = {
    "ping.latency", "ping.jitter",
    "download.bandwidth", "download.bytes", "download.elapsed",
    "upload.bandwidth", "upload.bytes", "upload.elapsed",
}


def measurements(result):
    if not isinstance(result, dict) or result.get("type") != "result":
        raise ValueError("Expected a speedtest result object")
    values = {}
    for path, (name, scale) in FIELDS.items():
        value = result
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value is None and path not in REQUIRED_FIELDS:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"Missing or invalid measurement: {path}")
        values[f"speedtest_{name}"] = value * scale
    return values


def label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def result_info(result):
    server = result.get("server", {})
    labels = {
        "isp": result.get("isp", ""),
        "server_id": server.get("id", ""),
        "server_name": server.get("name", ""),
        "server_location": server.get("location", ""),
        "server_country": server.get("country", ""),
    }
    return ",".join(f'{key}="{label(value)}"' for key, value in labels.items())


def exposition(values, info):
    lines = []
    for name, value in sorted(values.items()):
        kind = "counter" if name.endswith("_total") else "gauge"
        lines.extend((f"# TYPE {name} {kind}", f"{name} {value}"))
    if info is not None:
        lines.extend((
            "# TYPE speedtest_info gauge",
            f"speedtest_info{{{info}}} 1",
        ))
    return ("\n".join(lines) + "\n").encode()


def push(url, payload):
    request = urllib.request.Request(
        url.rstrip("/") + "/metrics/job/speedtest",
        data=payload,
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
            return True
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            LOG.error("Pushgateway push failed (%s/3): %s", attempt + 1, error)
            if attempt < 2:
                time.sleep(5)
    return False


class Runner:
    def __init__(self):
        self.values = {
            "speedtest_attempts_total": 0,
            "speedtest_failures_total": 0,
        }
        self.info = None

    def run_once(self):
        started = time.monotonic()
        self.values["speedtest_last_attempt_timestamp_seconds"] = time.time()
        self.values["speedtest_attempts_total"] += 1
        try:
            completed = subprocess.run(
                ["speedtest", "--accept-license", "--accept-gdpr",
                 "--format=json", "--progress=no"],
                check=True, capture_output=True, text=True, timeout=TEST_TIMEOUT,
            )
            result = json.loads(completed.stdout)
            values = measurements(result)
            info = result_info(result)
        except (subprocess.SubprocessError, OSError, ValueError, TypeError,
                AttributeError) as error:
            detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else error
            LOG.error("Speedtest failed: %s", detail)
            self.values["speedtest_success"] = 0
            self.values["speedtest_failures_total"] += 1
        else:
            # Replace the entire snapshot so absent optional fields do not linger.
            self.values = {
                key: value for key, value in self.values.items()
                if key not in {f"speedtest_{name}" for name, _ in FIELDS.values()}
            }
            self.values.update(values)
            self.info = info
            self.values["speedtest_success"] = 1
            self.values["speedtest_last_success_timestamp_seconds"] = time.time()
            LOG.info("Speedtest succeeded: download %.2f Mbps, upload %.2f Mbps",
                     values["speedtest_download_bits_per_second"] / 1e6,
                     values["speedtest_upload_bits_per_second"] / 1e6)
        self.values["speedtest_run_duration_seconds"] = time.monotonic() - started


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("SPEEDTEST_ACCEPT_LICENSE", "").lower() != "true":
        raise SystemExit(
            "Set SPEEDTEST_ACCEPT_LICENSE=true after reviewing Ookla's EULA "
            "(https://www.speedtest.net/about/eula) and privacy policy "
            "(https://www.speedtest.net/about/privacy)."
        )
    runner = Runner()
    url = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")
    while True:
        started = time.monotonic()
        interval = random.randint(MIN_INTERVAL, MAX_INTERVAL)
        next_run = time.time() + interval
        runner.run_once()
        runner.values["speedtest_interval_seconds"] = interval
        runner.values["speedtest_next_run_timestamp_seconds"] = next_run
        push(url, exposition(runner.values, runner.info))
        LOG.info("Next test scheduled in %.0f seconds",
                 max(0, interval - (time.monotonic() - started)))
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
