import copy
import json
import subprocess
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import runner


RESULT = {
    "type": "result",
    "ping": {"latency": 12.5, "jitter": 1.5, "low": 10, "high": 15},
    "download": {
        "bandwidth": 12500000, "bytes": 100000000, "elapsed": 8000,
        "latency": {"iqm": 30, "low": 15, "high": 100, "jitter": 5},
    },
    "upload": {
        "bandwidth": 2500000, "bytes": 20000000, "elapsed": 8000,
        "latency": {"iqm": 40, "low": 20, "high": 200, "jitter": 8},
    },
    "packetLoss": 0.5,
    "isp": 'Example "ISP"\\Network\nLab',
    "server": {"id": 123, "name": "Test server", "location": "City", "country": "UK"},
}


class MeasurementTests(unittest.TestCase):
    def test_all_measurements_and_unit_conversions(self):
        values = runner.measurements(RESULT)
        self.assertEqual(len(values), 19)
        self.assertEqual(values["speedtest_download_bits_per_second"], 100000000)
        self.assertEqual(values["speedtest_upload_bits_per_second"], 20000000)
        self.assertEqual(values["speedtest_ping_latency_seconds"], 0.0125)
        self.assertEqual(values["speedtest_ping_jitter_seconds"], 0.0015)
        self.assertEqual(values["speedtest_download_duration_seconds"], 8)
        self.assertEqual(values["speedtest_upload_latency_high_seconds"], 0.2)
        self.assertEqual(values["speedtest_packet_loss_percent"], 0.5)
        self.assertEqual(values["speedtest_download_bytes"], 100000000)

    def test_optional_missing_or_null_fields_are_not_zero(self):
        result = copy.deepcopy(RESULT)
        result["packetLoss"] = None
        del result["download"]["latency"]
        values = runner.measurements(result)
        self.assertNotIn("speedtest_packet_loss_percent", values)
        self.assertNotIn("speedtest_download_latency_iqm_seconds", values)

    def test_invalid_results_are_rejected(self):
        for invalid in (None, [], {"type": "log"}, {"type": "result"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                runner.measurements(invalid)
        for invalid in (True, "100", float("nan"), float("inf"), -1, None):
            result = copy.deepcopy(RESULT)
            result["download"]["bandwidth"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                runner.measurements(result)

    def test_exposition_escapes_info_labels_and_types_counters(self):
        payload = runner.exposition(
            {"speedtest_attempts_total": 1, **runner.measurements(RESULT)},
            runner.result_info(RESULT),
        ).decode()
        self.assertIn("# TYPE speedtest_attempts_total counter\n", payload)
        self.assertIn('isp="Example \\"ISP\\"\\\\Network\\nLab"', payload)
        self.assertNotIn("externalIp", payload)
        self.assertTrue(payload.endswith("\n"))


class RunnerTests(unittest.TestCase):
    @patch("runner.subprocess.run")
    def test_success_then_failure_retains_last_result(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(RESULT))
        instance = runner.Runner()
        instance.run_once()
        last_success = instance.values["speedtest_last_success_timestamp_seconds"]
        run.side_effect = subprocess.CalledProcessError(1, "speedtest", stderr="offline")
        with self.assertLogs("speedtest", level="ERROR") as logs:
            instance.run_once()
        self.assertIn("offline", logs.output[0])
        self.assertEqual(instance.values["speedtest_success"], 0)
        self.assertEqual(instance.values["speedtest_attempts_total"], 2)
        self.assertEqual(instance.values["speedtest_failures_total"], 1)
        self.assertEqual(instance.values["speedtest_download_bits_per_second"], 100000000)
        self.assertEqual(instance.values["speedtest_last_success_timestamp_seconds"], last_success)
        self.assertEqual(run.call_args.kwargs["timeout"], 180)

    @patch("runner.subprocess.run")
    def test_new_success_removes_old_optional_measurements(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(RESULT))
        instance = runner.Runner()
        instance.run_once()
        result = copy.deepcopy(RESULT)
        del result["packetLoss"]
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(result))
        instance.run_once()
        self.assertNotIn("speedtest_packet_loss_percent", instance.values)
        self.assertEqual(instance.values["speedtest_success"], 1)

    @patch("runner.subprocess.run")
    def test_timeout_and_bad_json_are_reported_without_fabricated_data(self, run):
        for error in (
            subprocess.TimeoutExpired("speedtest", 180),
            subprocess.CompletedProcess([], 0, "not json"),
        ):
            run.side_effect = error if isinstance(error, Exception) else None
            run.return_value = error
            instance = runner.Runner()
            with self.subTest(error=error), self.assertLogs("speedtest", level="ERROR"):
                instance.run_once()
            self.assertEqual(instance.values["speedtest_success"], 0)
            self.assertNotIn("speedtest_download_bits_per_second", instance.values)
            self.assertNotIn("speedtest_last_success_timestamp_seconds", instance.values)

    @patch.dict("os.environ", {"SPEEDTEST_ACCEPT_LICENSE": "false"})
    @patch("runner.subprocess.run")
    def test_license_acceptance_required_before_execution(self, run):
        with self.assertRaises(SystemExit):
            runner.main()
        run.assert_not_called()

    @patch.dict("os.environ", {"SPEEDTEST_ACCEPT_LICENSE": "true"})
    @patch("runner.time.sleep", side_effect=StopIteration)
    @patch("runner.time.monotonic", side_effect=[100, 120, 120])
    @patch("runner.random.randint", return_value=300)
    @patch("runner.push")
    @patch("runner.Runner")
    def test_random_start_to_start_schedule_accounts_for_test_duration(
        self, runner_class, push, randint, monotonic, sleep,
    ):
        instance = MagicMock(values={}, info=None)
        runner_class.return_value = instance
        with self.assertRaises(StopIteration):
            runner.main()
        randint.assert_called_once_with(300, 3600)
        instance.run_once.assert_called_once()
        push.assert_called_once()
        sleep.assert_called_once_with(280)


class PushTests(unittest.TestCase):
    @patch("runner.urllib.request.urlopen")
    def test_uses_put_to_replace_old_metrics(self, urlopen):
        self.assertTrue(runner.push("http://gateway:9091/", b"metrics\n"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(request.full_url, "http://gateway:9091/metrics/job/speedtest")
        self.assertEqual(request.data, b"metrics\n")

    @patch("runner.time.sleep")
    @patch("runner.urllib.request.urlopen", side_effect=urllib.error.URLError("offline"))
    def test_push_retries_and_logs_failure(self, urlopen, sleep):
        with self.assertLogs("speedtest", level="ERROR") as logs:
            self.assertFalse(runner.push("http://gateway:9091", b"metrics\n"))
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len(logs.output), 3)


if __name__ == "__main__":
    unittest.main()
