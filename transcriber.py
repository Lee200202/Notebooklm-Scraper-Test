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
SEGMENT_TIMEOUT_SECONDS = 15 * 60  # 實測每段 1～3 分鐘，15 分鐘沒結果就放棄這次、重新送出
MIN_SPLIT_SECONDS = 5 * 60
MAX_SEGMENT_ATTEMPTS = 3
RETRY_WAIT_SECONDS = (30, 90)  # 第 2、3 次送出前的等待秒數；兩次合計超過 1 分鐘，可避開每分鐘額度限制
NON_RETRYABLE_STATUS = {401, 403, 404}  # 金鑰無效、沒有權限、模型不存在：重試也不會成功


class QuotaExhausted(RuntimeError):
    """Gemini 額度用完（每日額度或持續的 429），這次執行不必再試後面的影片。"""


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


def _error_details(exc: Exception) -> tuple[int | None, str]:
    """SDK 的 HTTP 錯誤帶有 status_code 與 body（API 回傳的 JSON）；連線錯誤則沒有 status_code。"""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    return status, str(body) if body else f"{type(exc).__name__}: {exc}"


def _is_daily_quota(message: str) -> bool:
    lowered = message.lower()
    return any(k in lowered for k in ("perday", "per day", "per_day", "daily"))


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
        """送出一段影片並等待結果，回傳 (逐字稿, 是否完整)。

        Gemini 的 YouTube 影片處理偶爾會暫時失敗：實測在輪詢結果時回傳
        HTTP 400「Request contains an invalid argument.」，重新送出同樣的請求就會成功。
        所以除了金鑰／權限／模型錯誤之外，一律等待後重新送出，最多 MAX_SEGMENT_ATTEMPTS 次。
        """
        processing = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
        if self.video_fps > 0:
            processing["fps"] = self.video_fps
        prompt = (
            f"請聽打這段影片 {_hms(start)} 到 {_hms(end)} 的完整逐字稿。\n"
            f"可能出現的專有名詞：{'、'.join(self.vocabulary)}"
        )

        last_error, last_status = "", None
        for attempt in range(1, MAX_SEGMENT_ATTEMPTS + 1):
            if attempt > 1:
                wait = RETRY_WAIT_SECONDS[min(attempt - 2, len(RETRY_WAIT_SECONDS) - 1)]
                log.info("  %d 秒後重新送出", wait)
                time.sleep(wait)
            log.info("  Gemini 轉錄 %s–%s（第 %d 次）", _hms(start), _hms(end), attempt)

            stage = "送出請求"
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
                stage = "等待結果"
                interaction = self._wait(interaction)
            except Exception as exc:
                status, message = _error_details(exc)
                if status in NON_RETRYABLE_STATUS:
                    raise
                if status == 429 and _is_daily_quota(message):
                    raise QuotaExhausted(f"Gemini 每日額度已用完：{message[:300]}") from exc
                # 只有錯誤訊息明確提到 fps 才停用；不能把所有 400 都當成 fps 問題
                if status == 400 and "fps" in processing and "fps" in message.lower():
                    log.warning("  API 不接受 fps=%s，改用預設取樣", processing.pop("fps"))
                    self.video_fps = 0
                last_error, last_status = f"{stage}時發生錯誤（HTTP {status}）：{message[:300]}", status
                log.warning("  %s", last_error)
                continue

            usage = getattr(interaction, "usage", None)
            if usage:
                log.info(
                    "  token 用量：輸入 %s／輸出 %s／思考 %s",
                    usage.total_input_tokens, usage.total_output_tokens, usage.total_thought_tokens,
                )
            status = str(interaction.status)
            text = _output_text(interaction)
            if status == "completed" and text.strip():
                return text, True
            if status == "incomplete":
                return text, False
            if status == "budget_exceeded":
                raise RuntimeError("已達 Gemini 帳單的支出上限，請到 AI Studio 調整上限")
            last_error = (
                "Gemini 回傳空白逐字稿" if status == "completed"
                else f"Gemini 回傳狀態 {status}：{getattr(interaction, 'errors', None)}"
            )
            last_status = None
            log.warning("  %s", last_error)

        if last_status == 429:
            # 每分鐘額度在等待 2 分鐘後應已恢復；仍然 429 代表是每日（或更長週期）的額度
            raise QuotaExhausted(f"Gemini 額度不足，重試 {MAX_SEGMENT_ATTEMPTS} 次仍回傳 429：{last_error}")
        raise RuntimeError(f"Gemini 轉錄 {_hms(start)}–{_hms(end)} 重試 {MAX_SEGMENT_ATTEMPTS} 次仍失敗。最後錯誤：{last_error}")

    def _wait(self, interaction):
        deadline = time.monotonic() + SEGMENT_TIMEOUT_SECONDS
        while str(interaction.status) in ("queued", "in_progress"):
            if time.monotonic() > deadline:
                try:  # 取消背景工作，避免放棄後仍在計費
                    self.client.interactions.cancel(id=interaction.id)
                except Exception:
                    pass
                raise TimeoutError(f"等待 Gemini 超過 {SEGMENT_TIMEOUT_SECONDS // 60} 分鐘")
            time.sleep(POLL_SECONDS)
            interaction = self.client.interactions.get(id=interaction.id)
        return interaction
