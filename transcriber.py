"""用 Gemini API（Interactions API）直接讀 YouTube 網址產生逐字稿。

這個頻道的影片沒有 YouTube 字幕，所以 NotebookLM 或抓字幕的套件拿不到逐字稿；
Gemini 會自己「聽」影片的聲音來聽打。
長影片切成數段（start_offset / end_offset）分別送出，避免超過單次輸出上限。
"""
import logging
import time

from google import genai

from config import SYSTEM_INSTRUCTION

log = logging.getLogger(__name__)

POLL_SECONDS = 10
SEGMENT_TIMEOUT_SECONDS = 30 * 60
MIN_SPLIT_SECONDS = 5 * 60


class QuotaExhausted(RuntimeError):
    """Gemini 當日配額用完，今天不必再試。"""


def _hms(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _output_text(interaction) -> str:
    text = getattr(interaction, "output_text", None)
    if text:
        return text
    parts = []
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) != "model_output":
            continue
        for content in getattr(step, "content", None) or []:
            if getattr(content, "type", None) == "text":
                parts.append(getattr(content, "text", "") or "")
    return "".join(parts)


class Transcriber:
    def __init__(self, api_key: str, model: str, segment_minutes: int, video_fps: float, vocabulary: tuple[str, ...]):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.segment_seconds = segment_minutes * 60
        self.video_fps = video_fps
        self.vocabulary = vocabulary

    def check_model(self) -> str:
        """確認金鑰有效、模型名稱存在。只讀取模型資訊，不會用掉生成請求的額度。"""
        model = self.client.models.get(model=self.model)
        return getattr(model, "display_name", None) or self.model

    def transcribe(self, video_url: str, duration_sec: int) -> str:
        sections = []
        for start in range(0, duration_sec, self.segment_seconds):
            end = min(start + self.segment_seconds, duration_sec)
            text = self._transcribe_range(video_url, start, end)
            sections.append(f"【{_hms(start)} – {_hms(end)}】\n{text.strip()}")
        return "\n\n".join(sections)

    def _transcribe_range(self, video_url: str, start: int, end: int) -> str:
        text, complete = self._request(video_url, start, end)
        if complete:
            return text
        if end - start < MIN_SPLIT_SECONDS * 2:
            log.warning("  %s–%s 輸出被截斷且無法再切分，保留已取得的內容", _hms(start), _hms(end))
            return text
        # 輸出超過上限被截斷：切成兩半重做
        mid = (start + end) // 2
        log.info("  %s–%s 輸出被截斷，切成兩段重試", _hms(start), _hms(end))
        return self._transcribe_range(video_url, start, mid) + "\n\n" + self._transcribe_range(video_url, mid, end)

    def _request(self, video_url: str, start: int, end: int) -> tuple[str, bool]:
        processing = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
        if self.video_fps > 0:
            processing["fps"] = self.video_fps
        prompt = (
            f"請聽打這段影片 {_hms(start)} 到 {_hms(end)} 的完整逐字稿。\n"
            f"可能出現的專有名詞：{'、'.join(self.vocabulary)}"
        )

        for attempt in range(1, 4):
            log.info("  Gemini 轉錄 %s–%s（第 %d 次）", _hms(start), _hms(end), attempt)
            try:
                interaction = self.client.interactions.create(
                    model=self.model,
                    system_instruction=SYSTEM_INSTRUCTION,
                    input=[
                        {"type": "video", "uri": video_url, "processing": processing, "resolution": "low"},
                        {"type": "text", "text": prompt},
                    ],
                    generation_config={"thinking_level": "low", "max_output_tokens": 65536},
                    # 長影片處理時間久，用背景模式再輪詢結果，避免連線逾時
                    background=True,
                )
                interaction = self._wait(interaction)
            except Exception as exc:  # SDK 的 HTTP 錯誤帶有 status_code / body
                status = getattr(exc, "status_code", None)
                body = str(getattr(exc, "body", "") or exc)
                if status == 429 and ("PerDay" in body or "per day" in body.lower()):
                    raise QuotaExhausted(f"Gemini 今日配額已用完：{body[:300]}") from exc
                if status == 400 and "fps" in processing:
                    log.warning("  API 不接受 fps=%s，之後改用預設取樣：%s", processing["fps"], body[:200])
                    processing.pop("fps")
                    self.video_fps = 0
                    continue
                if (status == 429 or (status or 0) >= 500 or isinstance(exc, TimeoutError)) and attempt < 3:
                    wait = 60 * attempt
                    log.warning("  暫時性錯誤（%s），%d 秒後重試：%s", status, wait, body[:200])
                    time.sleep(wait)
                    continue
                raise

            usage = getattr(interaction, "usage", None)
            if usage:
                log.info(
                    "  token 用量：輸入 %s／輸出 %s／思考 %s",
                    usage.total_input_tokens, usage.total_output_tokens, usage.total_thought_tokens,
                )
            status = str(interaction.status)
            text = _output_text(interaction)
            if status == "completed":
                if not text.strip():
                    raise RuntimeError(f"Gemini 回傳空白逐字稿（{_hms(start)}–{_hms(end)}）")
                return text, True
            if status == "incomplete":
                return text, False
            raise RuntimeError(f"Gemini 轉錄失敗，狀態 {status}：{getattr(interaction, 'errors', None)}")

        raise RuntimeError(f"Gemini 轉錄 {_hms(start)}–{_hms(end)} 重試多次仍失敗")

    def _wait(self, interaction):
        deadline = time.monotonic() + SEGMENT_TIMEOUT_SECONDS
        while str(interaction.status) in ("queued", "in_progress"):
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待 Gemini 超過 {SEGMENT_TIMEOUT_SECONDS // 60} 分鐘")
            time.sleep(POLL_SECONDS)
            interaction = self.client.interactions.get(id=interaction.id)
        return interaction
