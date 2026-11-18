# Speedtest + Prometheus + Grafana

Runs the **official Ookla speedtest binary** immediately at startup, then chooses
a fresh, uniformly random start-to-start interval between **5 and 60 minutes**.
Tests never overlap and time out after 3 minutes.

```text
speedtest -> Pushgateway <- Prometheus <- Grafana
```

Prometheus scrapes metrics rather than accepting ordinary HTTP pushes, so the
runner pushes to a persistent Pushgateway, which Prometheus scrapes every 15s.

## Run

1. Install Docker with Docker Compose (Docker Desktop works on macOS).
2. Copy `.env.example` to `.env`.
3. Set a strong `GRAFANA_ADMIN_PASSWORD` in `.env`. Review the
   [Ookla EULA](https://www.speedtest.net/about/eula) and
   [privacy policy](https://www.speedtest.net/about/privacy), and set
   `SPEEDTEST_ACCEPT_LICENSE=true` only if you accept them. The CLI's license is
   for personal, non-commercial use.
4. Start the stack:

   ```sh
   docker compose up -d --build
   ```

Open **http://localhost:3000/d/speedtest** and log in as `admin` with your chosen
password. The **Network / Internet Speedtest** dashboard and Prometheus data
source are provisioned automatically. Only Grafana publishes a host port, bound
to localhost. Prometheus and Pushgateway are internal to the Compose network.

## Run a test manually

```sh
docker compose exec speedtest python runner.py --trigger
docker compose logs -f speedtest
```

This requests a test from the existing scheduler without stopping or restarting
the container. The command confirms the request, not test completion; follow the
logs for the result. The existing runner starts a test within about a second
and pushes its results normally, preserving counters and the last successful
measurements on failure. Prometheus picks up results on its next 15s scrape.
The next random interval starts from the manual test's start time.

If a test or its result push is in progress, the manual request queues one
follow-up test instead of interrupting it or starting an overlapping test.
Multiple requests received before the queued test starts are combined into one.
Manual tests bypass the usual 5-minute minimum interval.

## Alerting

A provisioned Grafana alert, **Speedtest latest test is not OK**, checks the
dashboard's `speedtest_success` metric every 30s. It fires on the first evaluation
where the value is not `1`, and resolves when a successful test reports `1`.
Missing data and query errors also trigger the alert, including during initial
startup before the first result is available. The rule links to **Latest test
status** on the dashboard.

The rule appears under **Alerting > Alert rules**. To receive notifications,
configure a contact point and notification policy in Grafana; no external
notification destination is configured by this stack.

The status metric retains the last result if the runner stops; this rule does
not detect a stopped runner whose last result was successful. The dashboard's
data-age and next-test panels still indicate stale results.

## Metrics and behavior

The dashboard displays download/upload rates, idle and loaded latency and jitter,
packet loss, transferred bytes, phase and overall durations, server/ISP details,
success/failure counters, data age, and the next scheduled test. All numeric
measurement fields from CLI JSON are exported; optional fields not supplied by
the test server (for example packet loss or loaded latency) show **No data**.
Bandwidth is converted from bytes/s to bits/s and durations from ms to seconds.
IP addresses, MAC addresses, and per-test result URLs are not exported.

Metrics use the `speedtest_` prefix. Query `{__name__=~"speedtest_.*"}` in
Prometheus to see them. The original CLI JSON is not stored.

A failed test logs its error, sets `speedtest_success` to `0`, increments the
failure counter, and retains the last successful measurements. Use the status
and data-age panels to distinguish old results from a healthy test. Push failures
are logged and retried three times; the next scheduled test still runs.
If the runner stops, the gateway keeps the last snapshot: data age continues to
increase, and an overdue next-test time indicates the runner needs attention.

Prometheus retains 90 days of history. All three monitoring services use named
volumes; Pushgateway checkpoints its latest snapshot every 30s. Runner counters
reset on runner restart (use Prometheus `increase()` for counter queries).
Changing the Grafana admin password in `.env` only affects initial setup; use
Grafana's password settings for an existing Grafana volume.

```sh
docker compose logs -f speedtest
docker compose down       # Stop; retain saved data
```

**Bandwidth warning:** Ookla tests can transfer substantial amounts of data.
This schedule averages roughly 44 tests/day. Docker Desktop measures the
container's internet path through its VM/NAT, not Wi-Fi signal strength or a
specific physical interface. The runner image supports amd64 and arm64.

## Validate

```sh
python3 -m unittest discover -s speedtest -v
docker compose config --quiet
docker compose exec prometheus promtool check config /etc/prometheus/prometheus.yml
```

Unit tests use synthetic JSON and do not run a real speed test.

## License

This project's original code, configuration, and documentation are licensed under
the **GNU General Public License, version 3 only** (`GPL-3.0-only`). See [LICENSE](LICENSE)
for the full license text.

**Ookla Speedtest CLI is proprietary third-party software and is not covered by
this project's GPLv3 license.** The Dockerfile downloads it directly from Ookla
during a local build. Its use remains subject to the
[Ookla EULA](https://www.speedtest.net/about/eula) and
[privacy policy](https://www.speedtest.net/about/privacy). The EULA restricts the
CLI to personal, non-commercial use and prohibits redistribution. Do not publish
built Docker images containing the CLI without permission from Ookla. These
restrictions apply to the CLI, not to this project's original GPL-licensed files.

Other third-party software, including Prometheus, Pushgateway, Grafana, and the
container base images, remains subject to its respective licenses.
