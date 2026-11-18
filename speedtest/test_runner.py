import copy
import json
import os
import selectors
import signal
import subprocess
import sys
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
            runner.main([])
        run.assert_not_called()

    @patch.dict("os.environ", {"SPEEDTEST_ACCEPT_LICENSE": "true"})
    @patch("runner.signal.signal")
    @patch("runner.RunTrigger")
    @patch("runner.time.monotonic", side_effect=[100, 120, 120])
    @patch("runner.random.randint", return_value=300)
    @patch("runner.push")
    @patch("runner.Runner")
    def test_random_start_to_start_schedule_accounts_for_test_duration(
        self, runner_class, push, randint, monotonic, trigger_class, register_signal,
    ):
        trigger_class.return_value.wait.side_effect = StopIteration
        instance = MagicMock(values={}, info=None)
        runner_class.return_value = instance
        with self.assertRaises(StopIteration):
            runner.main([])
        randint.assert_called_once_with(300, 3600)
        instance.run_once.assert_called_once()
        push.assert_called_once()
        trigger_class.return_value.wait.assert_called_once_with(280)
        register_signal.assert_called_once_with(
            signal.SIGUSR1, trigger_class.return_value.request,
        )


class TriggerTests(unittest.TestCase):
    @patch("runner.Runner")
    @patch("runner.request_run")
    def test_trigger_command_only_notifies_existing_scheduler(self, request_run, runner_class):
        runner.main(["--trigger"])
        request_run.assert_called_once_with()
        runner_class.assert_not_called()

    @patch("runner.os.kill")
    @patch("runner.Path.read_text", return_value=f"SigCgt:\t{1 << (signal.SIGUSR1 - 1):x}\n")
    @patch("runner.Path.read_bytes", return_value=b"python\0runner.py\0")
    def test_trigger_signals_ready_container_scheduler(self, command, status, kill):
        runner.request_run()
        kill.assert_called_once_with(1, signal.SIGUSR1)

    @patch("runner.os.kill")
    @patch("runner.Path.read_text", return_value="SigCgt:\t0\n")
    @patch("runner.Path.read_bytes", return_value=b"python\0runner.py\0")
    def test_trigger_does_not_signal_before_handler_is_registered(self, command, status, kill):
        with self.assertRaisesRegex(SystemExit, "not ready"):
            runner.request_run()
        kill.assert_not_called()

    @patch("runner.os.kill")
    @patch("runner.Path.read_text", return_value=f"SigCgt:\t{1 << (signal.SIGUSR1 - 1):x}\n")
    @patch("runner.Path.read_bytes", return_value=b"unrelated-process\0")
    def test_trigger_does_not_signal_an_unrelated_process(self, command, status, kill):
        with self.assertRaisesRegex(SystemExit, "not ready"):
            runner.request_run()
        kill.assert_not_called()

    @patch("runner.os.kill")
    @patch("runner.Path.read_bytes", side_effect=FileNotFoundError("No container scheduler"))
    def test_trigger_reports_missing_scheduler(self, command, kill):
        with self.assertRaisesRegex(SystemExit, "Cannot trigger.*No container scheduler"):
            runner.request_run()
        kill.assert_not_called()

    @patch("runner.os.kill", side_effect=PermissionError("Permission denied"))
    @patch("runner.Path.read_text", return_value=f"SigCgt:\t{1 << (signal.SIGUSR1 - 1):x}\n")
    @patch("runner.Path.read_bytes", return_value=b"python\0runner.py\0")
    def test_trigger_reports_signal_delivery_failure(self, command, status, kill):
        with self.assertRaisesRegex(SystemExit, "Cannot trigger.*Permission denied"):
            runner.request_run()

    @patch("runner.time.sleep")
    def test_requests_during_a_test_coalesce_into_one_follow_up(self, sleep):
        trigger = runner.RunTrigger()
        trigger.request(signal.SIGUSR1, None)
        trigger.request(signal.SIGUSR1, None)
        trigger.wait(300)
        sleep.assert_not_called()
        self.assertFalse(trigger.requested)

    @patch("runner.time.monotonic", side_effect=[100, 100, 101])
    @patch("runner.time.sleep")
    def test_wait_wakes_for_manual_request(self, sleep, monotonic):
        trigger = runner.RunTrigger()
        sleep.side_effect = lambda seconds: trigger.request(signal.SIGUSR1, None)
        trigger.wait(300)
        sleep.assert_called_once_with(1)
        self.assertFalse(trigger.requested)

    @patch("runner.time.monotonic", side_effect=[100, 100, 100.5])
    @patch("runner.time.sleep")
    def test_scheduled_deadline_still_runs_without_signal(self, sleep, monotonic):
        trigger = runner.RunTrigger()
        trigger.wait(0.5)
        sleep.assert_called_once_with(0.5)

    def test_real_signal_runs_and_pushes_again_without_resetting_counters(self):
        script = """
import json
import subprocess
from unittest.mock import patch
import runner
from test_runner import RESULT

def publish(url, payload):
    print(json.dumps(payload.decode()), flush=True)
    return True

with patch('runner.subprocess.run', return_value=subprocess.CompletedProcess(
    [], 0, json.dumps(RESULT)
)), patch('runner.push', side_effect=publish):
    runner.main()
"""
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", script],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env={**os.environ, "SPEEDTEST_ACCEPT_LICENSE": "true"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=10), "Initial run did not publish")
                first = json.loads(process.stdout.readline())
                os.kill(process.pid, signal.SIGUSR1)
                self.assertTrue(selector.select(timeout=10), "Manual run did not publish")
                second = json.loads(process.stdout.readline())
            self.assertIn("speedtest_attempts_total 1\n", first)
            self.assertIn("speedtest_attempts_total 2\n", second)
            self.assertIn("speedtest_success 1\n", second)
            self.assertIn("speedtest_download_bits_per_second 100000000\n", second)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()


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
