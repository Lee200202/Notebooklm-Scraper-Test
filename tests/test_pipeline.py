"""離線測試：用假的 Gemini 伺服器、假的 YouTube API 回應、記憶體中的試算表驗證整個流程。

不會連到任何外部服務，也不需要任何金鑰。執行方式：
  python -m unittest discover -s tests -v
"""
import base64
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gspread  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
import sheet_store  # noqa: E402
import transcriber  # noqa: E402
import youtube_client  # noqa: E402
from config import TAIPEI, ConfigError  # noqa: E402

MODEL = "gemini-3.8-flash"
TODAY = "todayVideo1"  # YouTube 影片 ID 固定 11 碼
SERVICE_ACCOUNT = {
    "type": "service_account",
    "project_id": "demo-project",
    "private_key_id": "abc123",
    "private_key": "fake-private-key-SECRET-for-tests",
    "client_email": "sheet-writer@demo-project.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


# ---------------------------------------------------------------- 假的 Gemini API
class FakeGemini(BaseHTTPRequestHandler):
    requests: list = []
    polls: dict = {}
    reject_fps = False
    daily_quota = False
    final_status = staticmethod(lambda index: "completed")  # 第 index 個請求的最終狀態

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self)
        if cls.daily_quota:
            return self._send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                                              "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
        video = body["input"][0]["content"][0]
        if cls.reject_fps and "fps" in video.get("processing", {}):
            return self._send(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid fps"}})
        cls.requests.append(body)
        interaction_id = f"int-{len(cls.requests)}"
        cls.polls[interaction_id] = {"count": 0, "index": len(cls.requests) - 1, "processing": video["processing"]}
        self._send(200, {"id": interaction_id, "status": "in_progress"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if "/models/" in path:
            return self._send(200, {"name": f"models/{MODEL}", "displayName": "Gemini 3.8 Flash"})
        interaction_id = path.rstrip("/").split("/")[-1]
        state = type(self).polls[interaction_id]
        state["count"] += 1
        if state["count"] == 1:
            return self._send(200, {"id": interaction_id, "status": "in_progress"})
        status = type(self).final_status(state["index"])
        processing = state["processing"]
        payload = {"id": interaction_id, "status": status,
                   "usage": {"total_input_tokens": 80000, "total_output_tokens": 9000, "total_thought_tokens": 50}}
        if status == "failed":
            payload["errors"] = [{"code": "INTERNAL", "message": "boom"}]
        else:
            text = f"{processing['start_offset']}-{processing['end_offset']} 台積電 2330 漲 3.5%。"
            payload["steps"] = [{"type": "model_output", "content": [{"type": "text", "text": text}]}]
        self._send(200, payload)


# ---------------------------------------------------------------- 記憶體中的試算表
class FakeWorksheet:
    def __init__(self, cols):
        self.rows, self.cols = [], cols

    def row_values(self, row):
        return list(self.rows[row - 1]) if len(self.rows) >= row else []

    def update(self, range_name, values):
        assert range_name == "A1"
        self.rows[0:1] = [list(values[0])]

    def freeze(self, rows=None, cols=None):
        pass

    def col_values(self, col):
        return [str(r[col - 1]) if len(r) >= col else "" for r in self.rows]

    def get_all_values(self):
        return [[str(v) for v in r] for r in self.rows]

    def append_row(self, values, value_input_option, table_range):
        assert value_input_option == "RAW" and table_range == "A1"
        self.rows.append(list(values))


class FakeSpreadsheet:
    def __init__(self):
        self.sheets = {}

    def worksheet(self, title):
        if title not in self.sheets:
            raise gspread.WorksheetNotFound(title)
        return self.sheets[title]

    def add_worksheet(self, title, rows, cols):
        self.sheets[title] = FakeWorksheet(cols)
        return self.sheets[title]


# ---------------------------------------------------------------- 假的 YouTube Data API
def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_video(video_id, day, duration, live="none", ended_minutes_ago=300):
    start = datetime(day.year, day.month, day.day, 10, 5, tzinfo=TAIPEI)
    details = {"actualStartTime": iso(start), "scheduledStartTime": iso(start)}
    if live == "none":
        details["actualEndTime"] = iso(datetime.now(TAIPEI) - timedelta(minutes=ended_minutes_ago))
    weekday = "一二三四五六日"[day.weekday()]
    return {
        "id": video_id,
        "snippet": {"title": f"{day:%Y/%m/%d}({weekday})張震  股市盤中家教班", "publishedAt": iso(start),
                    "liveBroadcastContent": live},
        "contentDetails": {"duration": "P0D" if live != "none" else f"PT{duration // 3600}H{duration % 3600 // 60}M{duration % 60}S"},
        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
        "liveStreamingDetails": details,
    }


def old(i):
    return f"oldVideo{i:03d}"


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGemini)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeGemini.requests, FakeGemini.polls = [], {}
        FakeGemini.reject_fps = FakeGemini.daily_quota = False
        FakeGemini.final_status = staticmethod(lambda index: "completed")
        self.book = FakeSpreadsheet()
        self.videos = {}
        self.today = datetime.now(TAIPEI).date()

        base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        real_client = transcriber.genai.Client
        encoded_account = base64.b64encode(json.dumps(SERVICE_ACCOUNT).encode()).decode()
        patches = [
            mock.patch.dict(os.environ, {
                "GEMINI_API_KEY": "test-gemini", "YOUTUBE_API_KEY": "test-youtube", "SPREADSHEET_ID": "sheet-id",
                "GOOGLE_SERVICE_ACCOUNT_JSON": encoded_account, "GITHUB_ACTIONS": "false",
                # 模擬 GitHub 未設定的 Variables：空字串要改用預設值
                "GEMINI_MODEL": "", "SEGMENT_MINUTES": "", "VIDEO_FPS": "",
            }),
            mock.patch.object(transcriber.genai, "Client",
                              lambda api_key: real_client(api_key=api_key, http_options={"base_url": base_url})),
            mock.patch.object(transcriber, "POLL_SECONDS", 0),
            mock.patch.object(transcriber.time, "sleep", lambda seconds: None),
            mock.patch.object(youtube_client.YouTubeClient, "_get", self._fake_youtube),
            mock.patch.object(sheet_store.gspread, "service_account_from_dict", self._fake_gspread),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- fakes ------------------------------------------------------------
    def _fake_youtube(self, endpoint, **params):  # 已綁定到測試物件，不會收到 YouTubeClient 的 self
        if endpoint == "channels":
            return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}}]}
        if endpoint == "playlistItems":
            return {"items": [{"contentDetails": {"videoId": vid}} for vid in self.videos]}
        return {"items": [self.videos[v] for v in params["id"].split(",") if v in self.videos]}

    def _fake_gspread(self, info, scopes):
        test = self
        assert info["client_email"] == SERVICE_ACCOUNT["client_email"]
        assert scopes == ["https://www.googleapis.com/auth/spreadsheets"]

        class Client:
            def open_by_key(self, key):
                assert key == "sheet-id"
                return test.book

        return Client()

    # -- helpers ----------------------------------------------------------
    def set_channel(self, count=6, **today_kwargs):
        """今天一場 + 往前 count-1 個平日各一場，長度都是 66 分 35 秒（3 段）。"""
        day, days = self.today, []
        while len(days) < count - 1:
            day -= timedelta(days=1)
            if day.weekday() < 5:
                days.append(day)
        self.videos = {TODAY: make_video(TODAY, self.today, 3995, **today_kwargs)}
        for i, d in enumerate(days):
            self.videos[old(i)] = make_video(old(i), d, 3995)

    def rows(self):
        return self.book.sheets["逐字稿"].rows[1:]

    def log_results(self):
        return [r[4] for r in self.book.sheets["執行紀錄"].rows[1:]]

    def run_main(self, *argv):
        with self.assertLogs("transcript", level="INFO") as logs:
            code = main.main(list(argv))
        return code, "\n".join(logs.output)

    # -- tests ------------------------------------------------------------
    def test_parsers(self):
        self.assertEqual(youtube_client.parse_duration("PT1H6M35S"), 3995)
        self.assertEqual(youtube_client.parse_duration("PT57M20S"), 3440)
        self.assertEqual(youtube_client.parse_duration("P0D"), 0)
        self.assertEqual(youtube_client.parse_duration("garbage"), 0)

    def test_service_account_accepts_json_and_wrapped_base64(self):
        raw = json.dumps(SERVICE_ACCOUNT, indent=2)
        self.assertEqual(config.parse_service_account(raw)["client_email"], SERVICE_ACCOUNT["client_email"])
        encoded = base64.b64encode(raw.encode()).decode()
        wrapped = "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60))  # 模擬複製時被斷行
        self.assertEqual(config.parse_service_account(wrapped)["private_key_id"], "abc123")

    def test_service_account_errors_do_not_leak_key(self):
        broken = json.dumps(SERVICE_ACCOUNT)[:-5]
        with self.assertRaises(ConfigError) as ctx:
            config.parse_service_account(broken)
        self.assertNotIn("SECRET", str(ctx.exception))
        with self.assertRaises(ConfigError):
            config.parse_service_account(json.dumps({"type": "authorized_user"}))

    def test_empty_variables_fall_back_to_defaults(self):
        cfg = config.load_settings()
        self.assertEqual((cfg.gemini_model, cfg.segment_minutes, cfg.video_fps), (MODEL, 30, 0.2))
        self.assertNotIn("test-gemini", repr(cfg))
        with mock.patch.dict(os.environ, {"SEGMENT_MINUTES": "abc"}):
            with self.assertRaises(ConfigError):
                config.load_settings()

    def test_first_run_backfills_five_latest_ended_streams(self):
        self.set_channel(count=7, live="live")  # 今天正在直播，不應算進 5 場
        code, _ = self.run_main("--mode", "daily")
        self.assertEqual(code, 0)
        self.assertEqual([r[3] for r in self.rows()], [old(4), old(3), old(2), old(1), old(0)])  # 舊到新
        self.assertEqual(len(FakeGemini.requests), 5 * 3)

        request = FakeGemini.requests[0]
        video = request["input"][0]["content"][0]
        self.assertEqual(request["model"], MODEL)
        self.assertTrue(request["background"])
        self.assertEqual(request["generation_config"], {"thinking_level": "low", "max_output_tokens": 65536})
        self.assertEqual(video["uri"], f"https://www.youtube.com/watch?v={old(4)}")
        self.assertEqual(video["resolution"], "low")
        self.assertEqual(video["processing"], {"type": "static", "start_offset": "0s", "end_offset": "1800s", "fps": 0.2})
        self.assertEqual([r["input"][0]["content"][0]["processing"]["end_offset"] for r in FakeGemini.requests[:3]],
                         ["1800s", "3600s", "3995s"])
        self.assertIn("【1:00:00 – 1:06:35】", self.rows()[0][11])
        self.assertEqual(self.log_results(), ["成功"] * 5)

    def test_daily_waits_until_stream_is_ready_then_runs_once(self):
        self.set_channel(live="live")
        self.run_main("--mode", "init")
        before = len(FakeGemini.requests)

        _, output = self.run_main()
        self.assertIn("直播中", output)
        self.videos[TODAY] = make_video(TODAY, self.today, 3995, ended_minutes_ago=2)
        _, output = self.run_main()
        self.assertIn("等待 5 分鐘緩衝", output)
        self.assertEqual(len(FakeGemini.requests), before)

        self.videos[TODAY] = make_video(TODAY, self.today, 3995, ended_minutes_ago=10)
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(self.rows()[-1][3], TODAY)
        self.assertEqual(len(FakeGemini.requests), before + 3)

        _, output = self.run_main()  # 之後的排程直接略過
        self.assertIn("已經在試算表中", output)
        self.assertEqual(len(FakeGemini.requests), before + 3)

    def test_daily_without_today_stream_exits_quietly(self):
        self.set_channel()
        del self.videos[TODAY]
        self.run_main("--mode", "init")
        code, output = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("還沒有今天", output)

    def test_truncated_segment_is_split_in_half(self):
        self.set_channel(count=1)
        FakeGemini.final_status = staticmethod(lambda index: "incomplete" if index == 0 else "completed")
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        offsets = [(r["input"][0]["content"][0]["processing"]["start_offset"],
                    r["input"][0]["content"][0]["processing"]["end_offset"]) for r in FakeGemini.requests]
        self.assertEqual(offsets, [("0s", "1800s"), ("0s", "900s"), ("900s", "1800s"), ("1800s", "3600s"), ("3600s", "3995s")])

    def test_rejected_fps_falls_back_once(self):
        self.set_channel(count=1)
        FakeGemini.reject_fps = True
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(len(FakeGemini.requests), 3)
        self.assertTrue(all("fps" not in r["input"][0]["content"][0]["processing"] for r in FakeGemini.requests))

    def test_failures_pause_scheduled_runs_but_not_manual_runs(self):
        self.set_channel(count=2, live="live")
        self.run_main("--mode", "init")  # 今天還在直播，init 只會寫入舊的那集
        self.videos[TODAY] = make_video(TODAY, self.today, 3995)
        FakeGemini.final_status = staticmethod(lambda index: "failed")
        codes = [self.run_main()[0] for _ in range(4)]
        self.assertEqual(codes, [1, 1, 1, 0])  # 第 4 次暫停，不再寄失敗通知
        self.assertEqual(self.log_results()[-3:], ["失敗"] * 3)

        FakeGemini.final_status = staticmethod(lambda index: "completed")
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(self.rows()[-1][3], TODAY)

    def test_daily_quota_stops_remaining_videos(self):
        self.set_channel()
        FakeGemini.daily_quota = True
        code, _ = self.run_main("--mode", "init")
        self.assertEqual(code, 1)
        self.assertEqual(self.log_results(), ["配額不足"])
        self.assertEqual(self.rows(), [])

    def test_long_transcript_is_split_across_cells(self):
        store = sheet_store.SheetStore("sheet-id", SERVICE_ACCOUNT)
        self.set_channel(count=1)
        stream = youtube_client.YouTubeClient("k").get_streams([TODAY])[0]
        store.append_transcript(stream, "字" * 65_000, MODEL)
        row = self.rows()[0]
        self.assertEqual([len(c) for c in row[11:]], [30_000, 30_000, 5_000])
        self.assertEqual(row[8], 65_000)

    def test_check_mode_reports_every_component(self):
        self.set_channel()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main.main(["--mode", "check"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().count("✅"), 4)
        self.assertEqual(self.log_results(), ["設定檢查通過"])
        self.assertEqual(FakeGemini.requests, [])  # 檢查不會用掉生成額度

    def test_check_mode_lists_all_missing_secrets(self):
        self.set_channel()
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "", "SPREADSHEET_ID": ""}), redirect_stdout(out):
            code = main.main(["--mode", "check"])
        self.assertEqual(code, 1)
        self.assertIn("GEMINI_API_KEY, SPREADSHEET_ID", out.getvalue())
        self.assertIn("✅ YouTube Data API 正常", out.getvalue())

    def test_scheduled_run_before_setup_is_skipped_quietly(self):
        blank = {name: "" for name in config.REQUIRED_SECRETS}
        with mock.patch.dict(os.environ, {**blank, "GITHUB_EVENT_NAME": "schedule"}):
            code, output = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("尚未設定任何 GitHub Secrets", output)
        with mock.patch.dict(os.environ, {**blank, "GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertEqual(self.run_main("--mode", "init")[0], 1)  # 手動執行仍要明確報錯

    def test_invalid_video_id_is_rejected(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            main.parse_args(["--video-id", "not a valid id; rm -rf /"])


if __name__ == "__main__":
    unittest.main()
