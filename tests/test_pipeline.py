"""離線測試：用假的 Gemini 伺服器、假的 YouTube API 回應、記憶體中的試算表驗證整個流程。

不會連到任何外部服務，也不需要任何金鑰。執行方式：
  python -m unittest discover -s tests -v
"""
import base64
import io
import json
import os
import smtplib
import sys
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, time, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gspread  # noqa: E402

import config  # noqa: E402
import main  # noqa: E402
import notifier  # noqa: E402
import sheet_store  # noqa: E402
import transcriber  # noqa: E402
import youtube_client  # noqa: E402
from config import TAIPEI, ConfigError  # noqa: E402

REAL_YOUTUBE_GET = youtube_client.YouTubeClient._get

MODEL = "gemini-3.5-flash-lite"
FALLBACK = "gemini-1.5-pro"
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
    requests: list = []   # 被接受的請求
    post_calls = 0        # 所有 POST（包含被拒絕的）
    polls: dict = {}
    reject_fps = False
    daily_quota = False
    final_status = staticmethod(lambda index: "completed")  # 第 index 個請求的最終狀態
    post_error = staticmethod(lambda call_no, body: None)    # 回傳 (HTTP 狀態碼, JSON) 模擬送出失敗
    no_background_models: set = set()                        # 不支援背景模式的模型（只能用串流）
    poll_error = staticmethod(lambda index, count: None)     # 回傳 (HTTP 狀態碼, JSON) 模擬輪詢失敗

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
        body["_api_key"] = self.headers.get("x-goog-api-key")
        cls = type(self)
        cls.post_calls += 1
        error = cls.post_error(cls.post_calls, body)
        if error:
            return self._send(*error)
        if cls.daily_quota:
            return self._send(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                                              "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}})
        video = body["input"][0]["content"][0]
        if cls.reject_fps and "fps" in video.get("processing", {}):
            return self._send(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid fps"}})
        if body.get("background") and body["model"] in cls.no_background_models:
            # 實際在 GitHub 上收到的錯誤
            return self._send(400, {"error": {
                "message": f"Model '{body['model']}' does not support background interactions.", "code": "invalid_request"}})
        if body.get("stream"):
            cls.requests.append(body)
            return self._send_stream(video["processing"])
        cls.requests.append(body)
        interaction_id = f"int-{len(cls.requests)}"
        cls.polls[interaction_id] = {"count": 0, "index": len(cls.requests) - 1, "processing": video["processing"]}
        self._send(200, {"id": interaction_id, "status": "in_progress"})

    def _send_stream(self, processing):
        text = f"{processing['start_offset']}-{processing['end_offset']} 台積電 2330 漲 3.5%。"
        events = [
            {"event_type": "interaction.created", "interaction": {"id": "stream-1", "status": "in_progress"}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": text[:10]}},
            {"event_type": "step.delta", "index": 0, "delta": {"type": "text", "text": text[10:]}},
            {"event_type": "interaction.completed", "interaction": {
                "id": "stream-1", "status": "completed",
                "usage": {"total_input_tokens": 80000, "total_output_tokens": 9000, "total_thought_tokens": 0}}},
        ]
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events) + "data: [DONE]\n\n"
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if "/models/" in path:
            m_name = path.rstrip("/").split("/")[-1]
            return self._send(200, {"name": f"models/{m_name}", "displayName": m_name})
        interaction_id = path.rstrip("/").split("/")[-1]
        state = type(self).polls[interaction_id]
        state["count"] += 1
        error = type(self).poll_error(state["index"], state["count"])
        if error:
            return self._send(*error)
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


# ---------------------------------------------------------------- 假的 Gmail SMTP
class FakeSMTP:
    sent: list = []
    logins: list = []
    fail_login = False

    def __init__(self, host, port, timeout=None):
        assert (host, port) == ("smtp.gmail.com", 465)

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted")
        FakeSMTP.logins.append((user, password))

    def send_message(self, message):
        FakeSMTP.sent.append(message)

    def quit(self):
        pass


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


def make_video(video_id, day, duration, live="none", ended_minutes_ago=300, ended_at=None):
    start = datetime(day.year, day.month, day.day, 10, 5, tzinfo=TAIPEI)
    details = {"actualStartTime": iso(start), "scheduledStartTime": iso(start)}
    if live == "none":
        details["actualEndTime"] = iso(ended_at or datetime.now(TAIPEI) - timedelta(minutes=ended_minutes_ago))
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


INVALID_ARGUMENT = (400, {"error": {"message": "Request contains an invalid argument.", "code": "invalid_request"}})
# 實際在 GitHub 上收到的 429 訊息格式
QUOTA_EXCEEDED = (429, {"error": {"code": "resource_exhausted", "message": (
    "You exceeded your current quota, please check your plan and billing details. For more information on this error, "
    "head to: https://ai.google.dev/gemini-api/docs/rate-limits. To monitor your current usage, head to: "
    "https://ai.dev/rate-limit. \n* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\nPlease retry in 41.3s.")}})


def processing_of(request):
    return request["input"][0]["content"][0]["processing"]


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGemini)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeGemini.requests, FakeGemini.polls, FakeGemini.post_calls = [], {}, 0
        FakeGemini.reject_fps = FakeGemini.daily_quota = False
        FakeGemini.final_status = staticmethod(lambda index: "completed")
        FakeGemini.post_error = staticmethod(lambda call_no, body: None)
        FakeGemini.poll_error = staticmethod(lambda index, count: None)
        FakeSMTP.sent, FakeSMTP.logins, FakeSMTP.fail_login = [], [], False
        FakeGemini.no_background_models = set()
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
                "MAIL_USERNAME": "", "MAIL_APP_PASSWORD": "", "MAIL_TO": "",  # 預設不寄信
                "GITHUB_REPOSITORY": "owner/repo", "GITHUB_EVENT_NAME": "",
            }),
            mock.patch.object(notifier.smtplib, "SMTP_SSL", FakeSMTP),
            mock.patch.object(
                transcriber.genai,
                "Client",
                lambda api_key, **kw: (
                    (lambda c: (setattr(c.interactions.sdk_configuration, "retry_config", None), c)[1])(
                        real_client(api_key=api_key, http_options={"base_url": base_url, "retry_options": {"attempts": 1}})
                    )
                ),
            ),
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
        self.assertEqual((cfg.gemini_model, cfg.segment_minutes, cfg.video_fps), (MODEL, 30, 0))
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
        self.assertEqual(video["processing"], {"type": "static", "start_offset": "0s", "end_offset": "1800s"})  # 預設不帶 fps
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
        self.assertIn("沒有今天", output)

    def test_truncated_segment_is_split_in_half(self):
        self.set_channel(count=1)
        FakeGemini.final_status = staticmethod(lambda index: "incomplete" if index == 0 else "completed")
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        offsets = [(r["input"][0]["content"][0]["processing"]["start_offset"],
                    r["input"][0]["content"][0]["processing"]["end_offset"]) for r in FakeGemini.requests]
        self.assertEqual(offsets, [("0s", "1800s"), ("0s", "900s"), ("900s", "1800s"), ("1800s", "3600s"), ("3600s", "3995s")])

    def test_fps_rejected_by_name_is_disabled_for_all_segments(self):
        self.set_channel(count=1)
        FakeGemini.reject_fps = True  # 錯誤訊息明確寫 "Invalid fps"
        with mock.patch.dict(os.environ, {"VIDEO_FPS": "0.2"}):
            self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(FakeGemini.post_calls, 4)  # 只被拒絕 1 次，之後的片段都不再帶 fps
        self.assertTrue(all("fps" not in processing_of(r) for r in FakeGemini.requests))

    def test_generic_400_with_fps_retries_that_segment_without_fps(self):
        # 重現 GitHub 上的實際情況：帶 fps=0.2 的片段在輪詢時一直回傳 400，不帶 fps 則成功
        self.set_channel(count=1)

        def fail_first_segment_with_fps(index, count):
            p = processing_of(FakeGemini.requests[index])
            return INVALID_ARGUMENT if "fps" in p and p["start_offset"] == "0s" and count == 2 else None

        FakeGemini.poll_error = staticmethod(fail_first_segment_with_fps)
        with mock.patch.dict(os.environ, {"VIDEO_FPS": "0.2"}):
            self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual([(processing_of(r)["start_offset"], "fps" in processing_of(r)) for r in FakeGemini.requests],
                         [("0s", True), ("0s", False), ("1800s", True), ("3600s", True)])
        self.assertEqual(self.rows()[0][3], TODAY)

    def test_failures_pause_scheduled_runs_but_not_manual_runs(self):
        self.set_channel(count=2, live="live")
        self.run_main("--mode", "init")  # 今天還在直播，init 只會寫入舊的那集
        self.videos[TODAY] = make_video(TODAY, self.today, 3995)
        FakeGemini.final_status = staticmethod(lambda index: "failed")
        codes, requests_per_run = [], []
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}), redirect_stdout(io.StringIO()) as out:
            for _ in range(4):
                before = len(FakeGemini.requests)
                codes.append(self.run_main()[0])
                requests_per_run.append(len(FakeGemini.requests) - before)
        # 排程的轉錄失敗不讓 job 失敗（避免 GitHub 立即寄信），改在 Actions 加錯誤標註、17:05 寄報告
        self.assertEqual(codes, [0, 0, 0, 0])
        self.assertIn("::error title=轉錄未完成", out.getvalue())
        self.assertEqual(requests_per_run, [3, 3, 3, 0])  # 第 4 次暫停，不再送出請求
        self.assertEqual(self.log_results()[-3:], ["失敗"] * 3)

        # 在 Actions 頁面手動執行 init：不受每日失敗次數限制
        FakeGemini.final_status = staticmethod(lambda index: "completed")
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertEqual(self.run_main("--mode", "init")[0], 0)
        self.assertEqual(self.rows()[-1][3], TODAY)

    def test_transient_400_without_fps_is_retried(self):
        # 不帶 fps 偶爾也會在輪詢時回傳一次 400，重送同一段即可
        self.set_channel(count=1)
        FakeGemini.poll_error = staticmethod(lambda index, count: INVALID_ARGUMENT if index == 1 and count == 2 else None)
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual([processing_of(r)["end_offset"] for r in FakeGemini.requests], ["1800s", "3600s", "3600s", "3995s"])
        self.assertEqual(self.rows()[0][3], TODAY)

    def test_failed_status_is_retried_within_the_run(self):
        self.set_channel(count=1)
        FakeGemini.final_status = staticmethod(lambda index: "failed" if index == 0 else "completed")
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(len(FakeGemini.requests), 4)

    def test_segment_gives_up_after_max_attempts(self):
        self.set_channel(count=1)
        FakeGemini.final_status = staticmethod(lambda index: "failed")
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 1)
        self.assertEqual(len(FakeGemini.requests), transcriber.MAX_SEGMENT_ATTEMPTS)  # 第一段失敗就不再送後面的段落
        self.assertIn("重試 3 次仍失敗", self.book.sheets["執行紀錄"].rows[-1][5])

    def test_not_found_model_is_not_retried(self):
        self.set_channel(count=1)
        FakeGemini.post_error = staticmethod(lambda call_no, body: (404, {"error": {"message": "model not found", "code": "not_found"}}))
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 1)
        self.assertEqual(FakeGemini.post_calls, 1)

    def test_quota_summary_extracts_metric_limit_and_model(self):
        message = str(QUOTA_EXCEEDED[1])
        self.assertEqual(transcriber._quota_summary(message),
                         "generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash")
        self.assertFalse(transcriber._is_daily_quota(message))  # 訊息裡沒有 per day 字樣，只能靠持續 429 判斷

    def test_primary_quota_exhausted_switches_to_fallback_model(self):
        # 重現 GitHub 上的情況：第一段成功後主要模型額度用完 → 改用備援模型完成剩下的片段與影片
        self.set_channel(count=2)
        FakeGemini.post_error = staticmethod(
            lambda call_no, body: QUOTA_EXCEEDED if body["model"] == MODEL and call_no > 1 else None)
        with mock.patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": FALLBACK}), \
                self.assertLogs("transcriber", level="WARNING") as transcriber_logs:
            code, _ = self.run_main("--mode", "init")
        self.assertEqual(code, 0)
        self.assertEqual([r["model"] for r in FakeGemini.requests], [MODEL] + [FALLBACK] * 5)
        self.assertEqual([r[9] for r in self.rows()], [f"{MODEL} + {FALLBACK}", FALLBACK])  # 「模型」欄
        warnings = " | ".join(transcriber_logs.output)
        self.assertIn(f"改用備援模型 {FALLBACK}", warnings)
        self.assertIn("limit: 20, model: gemini-3.8-flash", warnings)  # 完整印出是哪一種額度

    def test_all_models_exhausted_stops_the_run(self):
        self.set_channel()
        FakeGemini.post_error = staticmethod(lambda call_no, body: QUOTA_EXCEEDED)
        with mock.patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": FALLBACK}):
            self.assertEqual(self.run_main("--mode", "init")[0], 1)
        self.assertEqual(self.log_results(), ["配額不足"])  # 只記一次，後面的影片不再嘗試
        self.assertIn(f"{MODEL} / {FALLBACK}", self.book.sheets["執行紀錄"].rows[-1][5])
        self.assertEqual(self.rows(), [])

    def test_fallback_model_can_be_disabled(self):
        self.set_channel(count=1)
        FakeGemini.post_error = staticmethod(lambda call_no, body: QUOTA_EXCEEDED)
        with mock.patch.dict(os.environ, {"GEMINI_FALLBACK_MODEL": "none"}):
            self.assertEqual(self.run_main("--video-id", TODAY)[0], 1)
        self.assertEqual(self.log_results(), ["配額不足"])
        self.assertNotIn(FALLBACK, self.book.sheets["執行紀錄"].rows[-1][5])

    def test_daily_quota_stops_remaining_videos(self):
        self.set_channel()
        FakeGemini.daily_quota = True
        code, _ = self.run_main("--mode", "init")
        self.assertEqual(code, 1)
        self.assertEqual(self.log_results(), ["配額不足"])
        self.assertEqual(self.rows(), [])

    def test_multi_key_config_parsing(self):
        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "k1, k2",
            "GEMINI_API_KEY_2": "k2",
            "GEMINI_API_KEY_3": "k3",
            "GEMINI_API_KEYS": "k0, k1",
        }):
            keys = config._parse_keys("GEMINI_API_KEY", "GEMINI_API_KEYS", "GEMINI_API_KEY")
            self.assertEqual(keys, ("k0", "k1", "k2", "k3"))

    def test_multi_gemini_api_keys_rotation_on_quota(self):
        # 2 組 API 金鑰：第 1 組額度用完時，馬上換第 2 組繼續（維持高品質主要模型，不降級）
        self.set_channel(count=1)
        # 第一段請求用 key1 成功；第二段請求時 key1 收到 429 額度用盡，key2 接手成功
        FakeGemini.post_error = staticmethod(
            lambda call_no, body: QUOTA_EXCEEDED if body.get("_api_key") == "key-1" and call_no > 1 else None
        )
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key-1", "GEMINI_API_KEY_2": "key-2"}), \
                self.assertLogs("transcriber", level="WARNING") as transcriber_logs:
            code, _ = self.run_main("--video-id", TODAY)
        self.assertEqual(code, 0)
        self.assertEqual([r["model"] for r in FakeGemini.requests], [MODEL] * 3)  # 全程都是主要模型
        self.assertEqual(self.rows()[0][9], MODEL)  # 「模型」欄位仍是主要模型
        warnings = " | ".join(transcriber_logs.output)
        self.assertIn("API 金鑰 #1 額度用完", warnings)
        self.assertIn("馬上切換到 API 金鑰 #2 繼續", warnings)
        # 驗證被接受的請求中包含 key-1 與 key-2
        api_keys_used = [r.get("_api_key") for r in FakeGemini.requests]
        self.assertEqual(api_keys_used, ["key-1", "key-2", "key-2"])

    def test_all_keys_exhausted_switches_to_fallback_model(self):
        # 所有金鑰在主要模型都耗盡額度時，才切換到備援模型，且重設金鑰從第 1 組開始嘗試
        self.set_channel(count=1)
        FakeGemini.post_error = staticmethod(
            lambda call_no, body: QUOTA_EXCEEDED if body["model"] == MODEL else None
        )
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key-1", "GEMINI_API_KEY_2": "key-2", "GEMINI_FALLBACK_MODEL": FALLBACK}), \
                self.assertLogs("transcriber", level="WARNING") as transcriber_logs:
            code, _ = self.run_main("--video-id", TODAY)
        self.assertEqual(code, 0)
        self.assertEqual([r["model"] for r in FakeGemini.requests], [FALLBACK] * 3)
        warnings = " | ".join(transcriber_logs.output)
        self.assertIn(f"所有 API 金鑰在 {MODEL} 的額度均已用完", warnings)
        self.assertIn(f"改用備援模型 {FALLBACK}", warnings)

    def test_youtube_client_multi_key_rotation(self):
        # 模擬 YouTubeClient 在第 1 組金鑰遇到 403 quotaExceeded 時自動切換到第 2 組
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(params.get("key"))
            resp = mock.MagicMock()
            if params.get("key") == "yt-1":
                resp.status_code = 403
                resp.text = "quotaExceeded"
            else:
                resp.status_code = 200
                resp.json.return_value = {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "U123"}}}]}
            return resp

        yt = youtube_client.YouTubeClient(["yt-1", "yt-2"])
        with mock.patch.object(yt.session, "get", fake_get):
            data = REAL_YOUTUBE_GET(yt, "channels", id="test")
        self.assertEqual(data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"], "U123")
        self.assertEqual(calls, ["yt-1", "yt-2"])

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
        self.assertIn(f"主要模型 {MODEL}", out.getvalue())
        self.assertIn("未設定備援模型", out.getvalue())
        self.assertEqual(self.log_results(), ["設定檢查通過"])
        self.assertEqual(FakeGemini.requests, [])  # 檢查不會用掉生成額度

    def test_retry_once_429_immediately_switches_key(self):
        # 驗證「重試 1 次仍回傳 429 就換鑰匙」：Key 1 遇到 429 後重試 1 次（共 2 次），仍 429 則立刻換 Key 2，不嘗試第 3 次
        self.set_channel(count=1)
        key1_calls = 0

        def custom_error(call_no, body):
            nonlocal key1_calls
            if body.get("_api_key") == "key-1":
                key1_calls += 1
                return QUOTA_EXCEEDED
            return None

        FakeGemini.post_error = staticmethod(custom_error)
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "key-1", "GEMINI_API_KEY_2": "key-2"}), \
                self.assertLogs("transcriber", level="WARNING") as transcriber_logs:
            code, _ = self.run_main("--video-id", TODAY)
        self.assertEqual(code, 0)
        # Key 1 只能有 2 次嘗試（初次 + 1 次重試），不能有第 3 次
        self.assertEqual(key1_calls, 2)
        warnings = " | ".join(transcriber_logs.output)
        self.assertIn("API 金鑰 #1 額度用完", warnings)
        self.assertIn("重試 1 次仍回傳 429", warnings)
        self.assertIn("馬上切換到 API 金鑰 #2 繼續", warnings)

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

    # -- --watch：排程在同一次執行中每 3 分鐘檢查 ----------------------------------
    def fake_clock(self, hour, minute, on_sleep=None):
        """把 main 的時鐘換成假的：sleep 只推進時間，並可在每次 sleep 時改變影片狀態。"""
        clock = {"now": datetime.combine(self.today, time(hour, minute), tzinfo=TAIPEI), "sleeps": []}

        def fake_sleep(seconds):
            clock["sleeps"].append(seconds)
            clock["now"] += timedelta(seconds=seconds)
            if on_sleep:
                on_sleep(clock["now"])

        for p in (mock.patch.object(main, "now_taipei", lambda: clock["now"]),
                  mock.patch.object(main, "sleep", fake_sleep),
                  mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"})):
            p.start()
            self.addCleanup(p.stop)
        return clock

    def test_watch_waits_until_1120_then_checks_every_3_minutes(self):
        self.set_channel(count=2, live="live")
        self.run_main("--mode", "init")  # 先寫入舊的那集，試算表才不是空的
        before = len(FakeGemini.requests)

        def stream_finishes(now):
            if now >= datetime.combine(self.today, time(11, 26), tzinfo=TAIPEI):  # 11:15 結束，11:20 起可轉錄
                self.videos[TODAY] = make_video(TODAY, self.today, 3995,
                                                ended_at=datetime.combine(self.today, time(11, 15), tzinfo=TAIPEI))

        clock = self.fake_clock(11, 10, stream_finishes)
        code, output = self.run_main("--mode", "daily", "--watch")
        self.assertEqual(code, 0)
        # 11:10 啟動 → 等 10 分鐘到 11:20 → 11:20、11:23 還在直播 → 11:26 可轉錄
        self.assertEqual(clock["sleeps"], [600, 180, 180])
        self.assertIn("等到 11:20 開始檢查", output)
        self.assertIn("第 2 次檢查：今天的直播還沒準備好，11:26 再檢查", output)
        self.assertEqual(len(FakeGemini.requests), before + 3)
        self.assertEqual(self.rows()[-1][3], TODAY)

    def test_watch_started_late_checks_immediately_and_exits_when_done(self):
        self.set_channel(count=2)
        self.run_main("--mode", "init")  # 兩集都已寫入
        clock = self.fake_clock(11, 50)
        code, output = self.run_main("--mode", "daily", "--watch")
        self.assertEqual((code, clock["sleeps"]), (0, []))
        self.assertIn("已經在試算表中", output)

    def test_watch_gives_up_at_poll_end(self):
        self.set_channel(count=2, live="live")
        self.run_main("--mode", "init")
        clock = self.fake_clock(13, 50)
        code, output = self.run_main("--mode", "daily", "--watch")
        self.assertEqual(code, 0)
        self.assertEqual(clock["sleeps"], [180, 180, 180])  # 13:50、13:53、13:56、13:59 共 4 次；再下一次 14:02 超過 14:00
        self.assertIn("停止等待（共檢查 4 次）", output)

    def test_watch_without_stream_today_exits_after_one_check(self):
        self.set_channel(count=2)
        del self.videos[TODAY]
        self.run_main("--mode", "init")
        clock = self.fake_clock(11, 20)
        code, _ = self.run_main("--mode", "daily", "--watch")
        self.assertEqual((code, clock["sleeps"]), (0, []))  # 休市日不空等，交給之後的備援排程

    def test_watch_options_are_validated(self):
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            main.parse_args(["--mode", "init", "--watch"])
        with mock.patch.dict(os.environ, {"POLL_START": "11:70"}), self.assertRaises(ConfigError):
            config.load_settings()
        with mock.patch.dict(os.environ, {"POLL_START": "15:00", "POLL_END": "14:00"}), self.assertRaises(ConfigError):
            config.load_settings()

    # -- 寄信通知 ----------------------------------------------------------------
    def enable_mail(self):
        p = mock.patch.dict(os.environ, {"MAIL_USERNAME": "bot@gmail.com", "MAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
                                         "MAIL_TO": "me@example.com, you@example.com"})
        p.start()
        self.addCleanup(p.stop)

    def run_report(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
            return self.run_main("--mode", "report")

    def test_success_email_is_sent_right_after_transcription(self):
        self.enable_mail()
        self.set_channel(count=1)
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        message = FakeSMTP.sent[0]
        body = message.get_content()
        self.assertTrue(message["Subject"].startswith("✅ 逐字稿完成："))
        self.assertEqual(message["To"], "me@example.com, you@example.com")
        self.assertEqual(FakeSMTP.logins, [("bot@gmail.com", "abcdefghijklmnop")])  # 應用程式密碼去掉空白
        self.assertIn("字數：", body)
        self.assertIn("台積電", body)  # 開頭預覽
        self.assertIn("https://docs.google.com/spreadsheets/d/sheet-id", body)
        self.assertIn("https://github.com/owner/repo/actions/workflows/daily-transcript.yml", body)
        self.assertIn("已寄成功通知", self.log_results())

    def test_no_email_when_mail_is_not_configured(self):
        self.set_channel(count=1)
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)
        self.assertEqual(FakeSMTP.sent, [])

    def test_scheduled_failure_sends_one_report_at_1705(self):
        self.enable_mail()
        self.set_channel(count=2, live="live")
        self.run_main("--mode", "init")  # 寫入舊的那集（會寄一封成功通知）
        FakeSMTP.sent.clear()
        self.videos[TODAY] = make_video(TODAY, self.today, 3995)
        FakeGemini.final_status = staticmethod(lambda index: "failed")
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}), redirect_stdout(io.StringIO()):
            self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(FakeSMTP.sent, [])  # 失敗當下不寄信

        self.assertEqual(self.run_report()[0], 0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        body = FakeSMTP.sent[0].get_content()
        self.assertTrue(FakeSMTP.sent[0]["Subject"].startswith("❌ 逐字稿未完成："))
        self.assertIn("失敗", body)
        self.assertIn("重試 3 次仍失敗", body)
        self.assertIn(f"video_id 填 {TODAY}", body)

        self.run_report()  # 同一天再執行不重複寄
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_report_resends_success_email_that_was_not_delivered(self):
        self.enable_mail()
        self.set_channel(count=1)
        FakeSMTP.fail_login = True
        self.assertEqual(self.run_main("--video-id", TODAY)[0], 0)  # 寄信失敗不影響轉錄結果
        self.assertIn("寄信失敗", self.log_results())
        self.assertEqual(self.rows()[0][3], TODAY)

        FakeSMTP.fail_login = False
        self.run_report()
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertTrue(FakeSMTP.sent[0]["Subject"].startswith("✅ 逐字稿完成："))
        self.assertIn("台積電", FakeSMTP.sent[0].get_content())
        self.run_report()
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_report_sends_nothing_when_done_and_notified_or_no_stream(self):
        self.enable_mail()
        self.set_channel(count=1)
        self.run_main("--video-id", TODAY)
        self.run_report()
        self.assertEqual(len(FakeSMTP.sent), 1)  # 只有轉錄當下那封

        del self.videos[TODAY]  # 休市日
        self.run_report()
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_check_mode_sends_test_email(self):
        self.enable_mail()
        self.set_channel()
        out = io.StringIO()
        with redirect_stdout(out):
            code = main.main(["--mode", "check"])
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(out.getvalue().count("✅"), 5)
        self.assertEqual([m["Subject"] for m in FakeSMTP.sent], ["✅ 設定檢查：寄信測試"])

    def test_mail_settings_must_be_paired(self):
        with mock.patch.dict(os.environ, {"MAIL_USERNAME": "bot@gmail.com"}):
            with self.assertRaises(ConfigError):
                config.load_settings()
            out = io.StringIO()
            with redirect_stdout(out):
                self.set_channel()
                self.assertEqual(main.main(["--mode", "check"]), 1)
            self.assertIn("MAIL_USERNAME 和 MAIL_APP_PASSWORD 要一起設定", out.getvalue())

    def test_model_without_background_support_switches_to_streaming(self):
        # 重現 GitHub 上的錯誤：gemini-3.5-flash-lite 不支援 background=True
        self.set_channel(count=1)
        FakeGemini.no_background_models = {MODEL}
        with self.assertLogs("transcriber", level="INFO") as transcriber_logs:
            code, _ = self.run_main("--video-id", TODAY)
        self.assertEqual(code, 0)
        # 第 1 段：背景模式被拒絕（不算重試、不等待）→ 串流成功；之後的片段直接用串流
        self.assertEqual(FakeGemini.post_calls, 4)
        self.assertEqual([bool(r.get("stream")) for r in FakeGemini.requests], [True, True, True])
        self.assertTrue(all(not r.get("background") for r in FakeGemini.requests))
        logs = " | ".join(transcriber_logs.output)
        self.assertEqual(logs.count("不支援背景模式，改用串流模式"), 1)
        self.assertNotIn("秒後重新送出", logs)
        row = self.rows()[0]
        self.assertEqual(row[3], TODAY)
        self.assertIn("0s-1800s 台積電 2330 漲 3.5%。", row[11])  # 分段收到的文字有完整接起來
        self.assertIn("3600s-3995s", row[11])

    def test_invalid_video_id_is_rejected(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            main.parse_args(["--video-id", "not a valid id; rm -rf /"])


if __name__ == "__main__":
    unittest.main()
