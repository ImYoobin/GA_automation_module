from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from google_ads_exporter.google_adapter import _run_worker_process, _worker_login_and_crawl
from google_ads_exporter.models import AdsAccount


class _FakeQueue:
    def __init__(self) -> None:
        self._items: list[object] = []

    def put(self, item: object) -> None:
        self._items.append(item)

    def get_nowait(self) -> object:
        if not self._items:
            raise __import__("queue").Empty
        return self._items.pop(0)


class _FakeProcess:
    def __init__(self, result_queue: _FakeQueue) -> None:
        self._alive = False
        self.result_queue = result_queue
        self.terminated = False

    def start(self) -> None:
        self._alive = True
        self.result_queue.put(
            {
                "ok": True,
                "accounts": [],
                "browser_used": "msedge",
                "log_file": "C:/Temp/worker.log",
            }
        )

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        _ = timeout


class _FakeSpawnContext:
    def __init__(self) -> None:
        self.event_queue = _FakeQueue()
        self.result_queue = _FakeQueue()
        self._queue_count = 0
        self.last_process: _FakeProcess | None = None

    def Queue(self) -> _FakeQueue:
        self._queue_count += 1
        return self.event_queue if self._queue_count == 1 else self.result_queue

    def Process(self, target, kwargs, daemon) -> _FakeProcess:
        _ = target, kwargs, daemon
        self.last_process = _FakeProcess(self.result_queue)
        return self.last_process


class LoginWorkerFlowTests(unittest.TestCase):
    def test_run_worker_process_returns_early_when_result_is_ready(self) -> None:
        fake_context = _FakeSpawnContext()

        with (
            patch("google_ads_exporter.google_adapter.mp.get_context", return_value=fake_context),
            patch("google_ads_exporter.google_adapter.time.sleep"),
        ):
            result = _run_worker_process(
                worker_target=Mock(),
                worker_kwargs={},
                progress_cb=None,
                timeout_sec=30.0,
                max_restarts=0,
                return_on_result=True,
                post_result_grace_sec=0.0,
            )

        self.assertTrue(result["ok"])
        self.assertIsNotNone(fake_context.last_process)
        self.assertTrue(fake_context.last_process.terminated)

    def test_worker_login_and_crawl_keeps_success_payload_when_cleanup_fails(self) -> None:
        stop_event = Mock()
        heartbeat_thread = Mock()
        logger = Mock()
        result_queue = Mock()
        account = AdsAccount(
            name="Innisfree Main",
            cid="123-456-7890",
            cid_digits="1234567890",
        )

        def _login_side_effect(**kwargs):
            kwargs["on_accounts_crawled"]([account], "msedge")
            raise RuntimeError("cleanup failed")

        with (
            patch("google_ads_exporter.google_adapter.setup_logger", return_value=(logger, "C:/Temp/login.log")),
            patch("google_ads_exporter.google_adapter._ensure_playwright_event_loop_policy"),
            patch(
                "google_ads_exporter.google_adapter._start_worker_heartbeat",
                return_value=(stop_event, heartbeat_thread),
            ),
            patch("google_ads_exporter.google_adapter.login_and_crawl_accounts", side_effect=_login_side_effect),
        ):
            _worker_login_and_crawl(
                event_queue=Mock(),
                result_queue=result_queue,
                browser="msedge",
                headless=False,
                target_map_path="",
            )

        self.assertEqual(result_queue.put.call_count, 1)
        payload = result_queue.put.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["browser_used"], "msedge")
        self.assertEqual(payload["accounts"][0]["cid"], "123-456-7890")
        logger.warning.assert_called_once()
        stop_event.set.assert_called_once()
        heartbeat_thread.join.assert_called_once_with(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
