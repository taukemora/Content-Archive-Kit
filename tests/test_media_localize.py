#!/usr/bin/env python3
"""图片本地化预算：数量、单图、总量、时间与失败位置说明。"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _support  # noqa: E402

ROOT = _support.ROOT
sys.path.insert(0, str(ROOT / "scripts"))

import archive_url  # noqa: E402
from archive_url import localize_images  # noqa: E402
from media_policy import MediaBudget  # noqa: E402

MIB = 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n"


def budget(**overrides) -> MediaBudget:
    values = {
        "max_images": 100,
        "max_single_image_bytes": 10 * MIB,
        "max_total_image_bytes": 50 * MIB,
        "max_download_seconds": 180.0,
    }
    values.update(overrides)
    return MediaBudget(**values)


class FakeResponse:
    def __init__(self, chunks, *, content_length=None, clock=None, advance=0.0):
        self._chunks = list(chunks)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False
        self.close_calls = 0
        self._clock = clock
        self._advance = advance

    @property
    def content(self):
        return b"".join(self._chunks)

    def iter_content(self, chunk_size=1, decode_unicode=False):
        for chunk in self._chunks:
            if self._clock is not None and self._advance:
                self._clock[0] += self._advance
            yield chunk

    def close(self):
        self.closed = True
        self.close_calls += 1


class LocalizeImagesBudgetTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(dir=str(_support.SCRATCH), prefix="media_localize_"))
        self.images = self.dir / "images"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_localize(self, text, responses, *, base="https://example.com/a", budget_value=None,
                     side_effect=None, clock=None):
        calls = []

        def fake_fetch(url, **kwargs):
            calls.append(url)
            if side_effect is not None:
                return side_effect(url, **kwargs)
            if url in responses:
                item = responses[url]
                return item() if callable(item) else item
            raise AssertionError(f"unexpected fetch: {url}")

        with patch.object(archive_url, "assert_public_url", lambda url: None), patch.object(
            archive_url, "safe_fetch", side_effect=fake_fetch
        ):
            if clock is not None:
                with patch.object(archive_url.time, "monotonic", side_effect=lambda: clock[0]):
                    result = localize_images(text, base, self.images, budget=budget_value or budget())
            else:
                result = localize_images(text, base, self.images, budget=budget_value or budget())
        return result, calls

    def test_max_images_keeps_remaining_image_as_external_link_with_reason(self):
        urls = [f"https://example.com/{i}.png" for i in range(1, 4)]
        text = "\n\n".join(f"![p{i}]({u})" for i, u in enumerate(urls, 1))
        responses = {u: FakeResponse([PNG + b"x"]) for u in urls}
        (new, ok, failed, notes), calls = self.run_localize(
            text, responses, budget_value=budget(max_images=2)
        )
        self.assertEqual((ok, failed), (2, 1))
        self.assertEqual(len(calls), 2)
        self.assertIn("images/001", new)
        self.assertIn("images/002", new)
        self.assertIn(urls[2], new)
        self.assertIn("未本地化", new)
        self.assertIn("最多 2 张", " ".join(notes))
        self.assertEqual(len(list(self.images.iterdir())), 2)

    def test_single_image_limit_allows_larger_budget_and_blocks_oversize(self):
        big = "https://example.com/big.png"
        small = "https://example.com/small.png"
        responses = {
            big: FakeResponse([PNG + b"0" * int(1.5 * MIB)]),
            small: FakeResponse([PNG + b"0" * 1024]),
        }
        text = f"![big]({big})\n\n![small]({small})"
        (new, ok, failed, notes), _ = self.run_localize(
            text, responses, budget_value=budget(max_single_image_bytes=1 * MIB)
        )
        self.assertEqual((ok, failed), (1, 1))
        self.assertIn(big, new)
        self.assertIn("单图大小超限", " ".join(notes))
        self.assertIn("未本地化", new)

    def test_three_mib_image_is_allowed_by_default_ten_mib_budget(self):
        url = "https://example.com/medium.png"
        (new, ok, failed, notes), _ = self.run_localize(
            f"![medium]({url})",
            {url: FakeResponse([PNG + b"0" * (3 * MIB)])},
            budget_value=budget(),
        )
        self.assertEqual((ok, failed), (1, 0), notes)
        self.assertIn("images/001.png", new)

    def test_content_length_over_limit_is_rejected_without_reading(self):
        url = "https://example.com/huge.png"
        response = FakeResponse([PNG + b"z"], content_length=2 * MIB)
        (new, ok, failed, notes), _ = self.run_localize(
            f"![huge]({url})", {url: response}, budget_value=budget(max_single_image_bytes=MIB)
        )
        self.assertEqual((ok, failed), (0, 1))
        self.assertIn("单图大小超限", " ".join(notes))
        self.assertTrue(response.closed)
        self.assertEqual(list(self.images.iterdir()) if self.images.exists() else [], [])

    def test_total_limit_stops_further_network_attempts(self):
        urls = [f"https://example.com/t{i}.png" for i in range(1, 4)]
        one_mib = (PNG + b"0" * MIB)[:MIB]
        responses = {u: FakeResponse([one_mib]) for u in urls}
        text = "\n\n".join(f"![t{i}]({u})" for i, u in enumerate(urls, 1))
        (new, ok, failed, notes), calls = self.run_localize(
            text,
            responses,
            budget_value=budget(max_single_image_bytes=2 * MIB, max_total_image_bytes=2 * MIB),
        )
        self.assertEqual((ok, failed), (2, 1))
        self.assertEqual(len(calls), 2, "total budget must stop before the third request")
        self.assertIn("总大小", " ".join(notes))
        self.assertIn(urls[2], new)

    def test_time_budget_includes_streaming_and_stops_later_images(self):
        first = "https://example.com/slow.png"
        second = "https://example.com/next.png"
        clock = [0.0]
        responses = {
            # 每读一个 chunk 就把假时钟推进 1 秒，首图因此超时。
            first: FakeResponse([PNG, b"x"], clock=clock, advance=1.0),
            second: FakeResponse([PNG]),
        }
        (new, ok, failed, notes), calls = self.run_localize(
            f"![slow]({first})\n\n![next]({second})",
            responses,
            budget_value=budget(max_download_seconds=0.5),
            clock=clock,
        )
        self.assertEqual(ok, 0)
        self.assertEqual(failed, 2)
        self.assertEqual(len(calls), 1, "time budget must stop subsequent network attempts")
        self.assertIn("时间预算", " ".join(notes))
        self.assertIn(first, new)
        self.assertIn(second, new)

    def test_failed_attempts_consume_the_time_budget(self):
        urls = [f"https://example.com/f{i}.png" for i in range(1, 4)]
        clock = [0.0]

        def failing(url, **kwargs):
            clock[0] += 0.6
            raise OSError("boom")

        (new, ok, failed, notes), calls = self.run_localize(
            "\n\n".join(f"![f{i}]({u})" for i, u in enumerate(urls, 1)),
            {},
            side_effect=failing,
            budget_value=budget(max_download_seconds=1.0),
            clock=clock,
        )
        self.assertEqual(ok, 0)
        self.assertEqual(failed, 3)
        self.assertEqual(len(calls), 2, "second failed attempt exhausts the budget")
        self.assertIn("时间预算", " ".join(notes))
        self.assertIn("未本地化", new)

    def test_duplicate_url_is_fetched_once_and_reused(self):
        url = "https://example.com/same.png"
        (new, ok, failed, notes), calls = self.run_localize(
            f"![a]({url})\n\n![b]({url})",
            {url: FakeResponse([PNG + b"x"])},
        )
        self.assertEqual((ok, failed), (1, 0), notes)
        self.assertEqual(len(calls), 1)
        self.assertEqual(new.count("images/001.png"), 2)

    def test_ssrf_blocked_image_keeps_position_and_reason(self):
        url = "http://127.0.0.1:9090/secret.png"
        with patch.object(archive_url, "assert_public_url", side_effect=ValueError("private")):
            new, ok, failed, notes = localize_images(
                f"![secret]({url})", "https://example.com/a", self.images, budget=budget()
            )
        self.assertEqual((ok, failed), (0, 1))
        self.assertIn(url, new)
        self.assertIn("SSRF", " ".join(notes))
        self.assertIn("未本地化", new)

    def test_stream_cap_still_stops_before_full_payload(self):
        url = "https://example.com/stream.png"
        payload = PNG + b"0" * (3 * MIB)
        chunks = [payload[i:i + 64 * 1024] for i in range(0, len(payload), 64 * 1024)]
        response = FakeResponse(chunks)
        (new, ok, failed, notes), _ = self.run_localize(
            f"![stream]({url})",
            {url: response},
            budget_value=budget(max_single_image_bytes=MIB),
        )
        self.assertEqual((ok, failed), (0, 1))
        self.assertIn("单图大小超限", " ".join(notes))
        self.assertTrue(response.closed)
        self.assertIn("![stream", new)


if __name__ == "__main__":
    unittest.main()
