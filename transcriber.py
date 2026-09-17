"""用 Gemini API（Interactions API）直接讀 YouTube 網址產生逐字稿。

這個頻道的影片沒有 YouTube 字幕，所以 NotebookLM 或抓字幕的套件拿不到逐字稿；
Gemini 會自己「聽」影片的聲音來聽打。
長影片切成數段（start_offset / end_offset）分別送出，避免超過單次輸出上限。
"""
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from google import genai

from config import SYSTEM_INSTRUCTION

log = logging.getLogger(__name__)

POLL_SECONDS = 10
SEGMENT_TIMEOUT_SECONDS = 15 * 60  # 實測每段 1～3 分鐘，15 分鐘沒結果就放棄這次、重新送出
MIN_SPLIT_SECONDS = 5 * 60
MAX_SEGMENT_ATTEMPTS = 3
RETRY_WAIT_SECONDS = (30, 90)  # 第 2、3 次送出前的等待秒數；兩次合計超過 1 分鐘，可避開每分鐘額度限制
NON_RETRYABLE_STATUS = {401, 403, 404}  # 金鑰無效、沒有權限、模型不存在：重試也不會成功


@dataclass
class StreamResult:
    """串流模式的結果，欄位與 Interaction 相同，後續處理可共用。"""
    status: str
    output_text: str
    usage: Any = None
    errors: Any = None


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


def _quota_summary(message: str) -> str:
    """從 429 錯誤訊息擷取「哪一種額度、上限多少、哪個模型」，例如
    generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash"""
    found = re.findall(r"Quota exceeded for metric: (?:[\w.-]+/)?([\w.-]+), limit: (\d+)(?:, model: ([\w.-]+))?", message)
    return "；".join(f"{metric}, limit: {limit}" + (f", model: {model}" if model else "") for metric, limit, model in found)


def _is_daily_quota(message: str) -> bool:
    lowered = message.lower()
    return any(k in lowered for k in ("perday", "per day", "per_day", "daily"))


class Transcriber:
    def __init__(self, api_key: str | tuple[str, ...] | list[str], model: str, segment_minutes: int, video_fps: float,
                 vocabulary: tuple[str, ...], fallback_model: str = "",
                 api_keys: tuple[str, ...] | list[str] | None = None):
        if isinstance(api_key, (list, tuple)):
            raw_keys = list(api_key)
        elif api_keys:
            raw_keys = list(api_keys)
        elif api_key:
            raw_keys = [api_key]
        else:
            raw_keys = []
        self.api_keys = [k for k in raw_keys if k] or [""]
        self.clients = []
        for k in self.api_keys:
            client = genai.Client(api_key=k or "dummy", http_options={"retry_options": {"attempts": 1}})
            if hasattr(client, "interactions") and hasattr(client.interactions, "sdk_configuration"):
                client.interactions.sdk_configuration.retry_config = None
            self.clients.append(client)
        self.key_index = 0
        # 額度是「每個模型分開計算」：主要模型額度用完時，改用備援模型繼續
        self.models = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])
        self.model_index = 0
        self.models_used: list[str] = []  # 目前這集實際用到的模型（寫入試算表「模型」欄）
        self.keys_used: list[int] = []    # 目前這集實際用到的金鑰編號（1-based）
        self.segment_seconds = segment_minutes * 60
        self.video_fps = video_fps
        self.vocabulary = vocabulary
        # 不支援背景模式（background）的模型，例如 gemini-3.5-flash-lite：偵測到後改用串流模式
        self.stream_models: set[str] = set()
        # (金鑰編號, 模型)：這組金鑰在這個模型的額度已用完，這次執行不再使用
        self.exhausted_keys: set[tuple[int, str]] = set()
        self.stage = "送出請求"

    @property
    def client(self) -> genai.Client:
        return self.clients[self.key_index]

    @property
    def model(self) -> str:
        return self.models[self.model_index]

    def check_models(self) -> list[str]:
        """確認金鑰有效、模型名稱存在。只讀取模型資訊，不會用掉生成請求的額度。"""
        names = []
        for name in self.models:
            info = self.client.models.get(model=name)
            names.append(f"{name}（{getattr(info, 'display_name', None) or name}）")
        return names

    def transcribe(self, video_url: str, duration_sec: int) -> str:
        self.models_used = []
        self.keys_used = []
        sections = []
        for start in range(0, duration_sec, self.segment_seconds):
            end = min(start + self.segment_seconds, duration_sec)
            text = self._transcribe_range(video_url, start, end)
            sections.append(f"【{_hms(start)} – {_hms(end)}】\n{text.strip()}")
        return "\n\n".join(sections)

    def _transcribe_range(self, video_url: str, start: int, end: int) -> str:
        while True:
            try:
                text, complete = self._request(video_url, start, end)
                break
            except QuotaExhausted as exc:
                # 所有金鑰在目前模型的額度都用完：有備援模型就改用備援模型繼續
                if self.model_index + 1 >= len(self.models):
                    raise
                old_model = self.model
                self.model_index += 1
                self.key_index = self._first_available_key()
                log.warning("  %s 在所有金鑰的額度都用完（%s），改用備援模型 %s 繼續（金鑰 #%d）",
                            old_model, exc, self.model, self.key_index + 1)
        if self.model not in self.models_used:
            self.models_used.append(self.model)
        key_num = self.key_index + 1
        if key_num not in self.keys_used:
            self.keys_used.append(key_num)
        if complete:
            return text
        if end - start < MIN_SPLIT_SECONDS * 2:
            log.warning("  %s–%s 輸出被截斷且無法再切分，保留已取得的內容", _hms(start), _hms(end))
            return text
        # 輸出超過上限被截斷：切成兩半重做
        mid = (start + end) // 2
        log.info("  %s–%s 輸出被截斷，切成兩段重試", _hms(start), _hms(end))
        return self._transcribe_range(video_url, start, mid) + "\n\n" + self._transcribe_range(video_url, mid, end)

    def _available_keys(self) -> list[int]:
        return [i for i in range(len(self.api_keys)) if (i, self.model) not in self.exhausted_keys]

    def _first_available_key(self) -> int:
        keys = self._available_keys()
        return keys[0] if keys else 0

    def _request(self, video_url: str, start: int, end: int) -> tuple[str, bool]:
        """送出一段影片並等待結果，回傳 (逐字稿, 是否完整)。

        失敗處理（每一段）：
          1. 任何可重試的失敗（負載過高、400、狀態 failed、429、逾時、連線錯誤…）
             → 不等待，馬上換下一組金鑰重送
          2. 這一輪所有金鑰都失敗 → 等 30 秒（之後 90 秒）再從第一組可用金鑰輪一次，最多 MAX_SEGMENT_ATTEMPTS 輪
          3. 連續兩輪所有金鑰都回 429，或錯誤訊息明確是每日額度 → 視為額度用完（QuotaExhausted），
             由 _transcribe_range 改用備援模型（有設定時）
          4. 401／404／非額度的 403（金鑰無效、模型不存在、沒有權限）→ 直接失敗，不重試

        fps：實測帶 fps=0.2 的片段可能一直回傳 400「Request contains an invalid argument.」，
        不帶 fps 就成功，所以帶 fps 的請求失敗後，這一段改成不帶 fps 重送。
        """
        use_fps = self.video_fps > 0
        prompt = (
            f"請聽打這段影片 {_hms(start)} 到 {_hms(end)} 的完整逐字稿。\n"
            f"可能出現的專有名詞：{'、'.join(self.vocabulary)}"
        )
        multi_key = len(self.api_keys) > 1
        attempt = 0
        last_error = ""
        quota_rounds = 0  # 連續「這一輪所有金鑰都回 429」的輪數

        for round_no in range(1, MAX_SEGMENT_ATTEMPTS + 1):
            if round_no > 1:
                wait = RETRY_WAIT_SECONDS[min(round_no - 2, len(RETRY_WAIT_SECONDS) - 1)]
                log.info("  %s%d 秒後重新送出", "所有金鑰這一輪都失敗，" if multi_key else "", wait)
                time.sleep(wait)

            keys = self._available_keys()
            if not keys:
                raise QuotaExhausted(f"所有金鑰在 {self.model} 的額度都已用完：{last_error}")
            # 從目前使用中的金鑰開始輪（上一段成功的金鑰優先）
            first = keys.index(self.key_index) if self.key_index in keys else 0
            order = keys[first:] + keys[:first]

            all_quota = True
            for position, key in enumerate(order):
                if (key, self.model) in self.exhausted_keys:
                    continue
                self.key_index = key
                attempt += 1
                key_tag = f"，金鑰 #{key + 1}" if multi_key else ""
                processing = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
                if use_fps:
                    processing["fps"] = self.video_fps
                log.info("  Gemini 轉錄 %s–%s（第 %d 次%s%s）", _hms(start), _hms(end), attempt,
                         f"，fps={self.video_fps}" if use_fps else "", key_tag)
                request = {
                    "model": self.model,
                    "system_instruction": SYSTEM_INSTRUCTION,
                    "input": [
                        {"type": "video", "uri": video_url, "processing": processing, "resolution": "low"},
                        {"type": "text", "text": prompt},
                    ],
                    "generation_config": {"thinking_level": "low", "max_output_tokens": 65536},
                }

                try:
                    interaction = self._run(request)
                except Exception as exc:
                    stage = self.stage
                    status, message = _error_details(exc)
                    lowered = message.lower()
                    is_quota_403 = status == 403 and any(k in lowered for k in ("quota", "exhausted", "ratelimit", "rate_limit"))
                    if status in NON_RETRYABLE_STATUS and not is_quota_403:
                        raise
                    if status == 429 or is_quota_403:
                        # 完整印出是哪一種額度（每日請求數、每分鐘 token 數…），方便判斷
                        detail = _quota_summary(message) or message[:300]
                        if _is_daily_quota(message) or is_quota_403:
                            self.exhausted_keys.add((key, self.model))
                    else:
                        detail = message[:300]
                        all_quota = False
                    last_error = f"{stage}時發生錯誤（HTTP {status}，模型 {self.model}{key_tag}）：{detail}"
                    log.warning("  %s", last_error)
                    if status == 400 and use_fps:
                        use_fps = self._drop_fps(message)
                else:
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
                        self.exhausted_keys.add((key, self.model))
                        last_error = f"金鑰 #{key + 1} 已達 Gemini 帳單的支出上限"
                    else:
                        all_quota = False
                        last_error = (
                            f"Gemini 回傳空白逐字稿（模型 {self.model}{key_tag}）" if status == "completed"
                            else f"Gemini 回傳狀態 {status}（模型 {self.model}{key_tag}）：{getattr(interaction, 'errors', None)}"
                        )
                    log.warning("  %s", last_error)
                    if use_fps and status != "budget_exceeded":
                        use_fps = self._drop_fps(last_error)

                # 這一輪還有其他金鑰：不等待，馬上換
                remaining = [k for k in order[position + 1:] if (k, self.model) not in self.exhausted_keys]
                if remaining:
                    log.warning("  金鑰 #%d 失敗，馬上改用金鑰 #%d 重送", key + 1, remaining[0] + 1)

            # 這一輪所有金鑰都失敗了
            if not self._available_keys():
                raise QuotaExhausted(f"所有金鑰在 {self.model} 的額度都已用完：{last_error}")
            quota_rounds = quota_rounds + 1 if all_quota else 0
            if quota_rounds >= 2:
                # 等待後重試仍全部 429：不是每分鐘限制，而是每日（或更長週期）的額度
                raise QuotaExhausted(f"重試 1 次仍回傳 429：{last_error}")
            self.key_index = self._first_available_key()

        raise RuntimeError(f"Gemini 轉錄 {_hms(start)}–{_hms(end)} 重試 {attempt} 次仍失敗。最後錯誤：{last_error}")

    def _drop_fps(self, message: str) -> bool:
        """帶 fps 的請求失敗後改成不帶 fps。回傳新的 use_fps（一律 False）。

        錯誤訊息明確提到 fps（API 不接受這個值）→ 之後所有片段都不再帶 fps；
        否則只有這一段改成不帶 fps，下一段仍照設定嘗試。
        """
        if "fps" in message.lower():
            log.warning("  API 不接受 fps=%s，之後所有片段改用預設取樣", self.video_fps)
            self.video_fps = 0
        else:
            log.warning("  這一段改成不指定 fps 重送（fps 會讓部分影片片段處理失敗）")
        return False

    def _run(self, request: dict):
        """送出請求並取得結果。

        長影片處理時間久，預設用背景模式（background）再輪詢結果，避免連線逾時。
        部分模型（例如 gemini-3.5-flash-lite）不支援背景模式，會立即回傳 HTTP 400
        「does not support background interactions」——這種請求沒有被處理、不佔額度，
        所以直接改用串流模式重送，不算一次重試，之後這個模型都用串流。
        """
        if request["model"] not in self.stream_models:
            self.stage = "送出請求"
            try:
                interaction = self.client.interactions.create(**request, background=True)
            except Exception as exc:
                status, message = _error_details(exc)
                if not (status == 400 and "does not support background" in message.lower()):
                    raise
                self.stream_models.add(request["model"])
                log.info("  模型 %s 不支援背景模式，改用串流模式接收結果", request["model"])
            else:
                self.stage = "等待結果"
                return self._wait(interaction)
        return self._stream(request)

    def _stream(self, request: dict) -> StreamResult:
        """串流模式：連線保持開啟，邊接收邊累積文字，直到 interaction.completed。"""
        self.stage = "串流接收結果"
        stream = self.client.interactions.create(**request, stream=True, timeout=SEGMENT_TIMEOUT_SECONDS)
        parts: list[str] = []
        completed = None
        try:
            for event in stream:
                event_type = getattr(event, "event_type", None)
                if event_type == "step.delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text":
                        parts.append(getattr(delta, "text", "") or "")
                elif event_type == "interaction.completed":
                    completed = getattr(event, "interaction", None)
                elif event_type == "error":
                    error = getattr(event, "error", None)
                    return StreamResult(status="failed", output_text="", errors=error)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if completed is None:
            raise RuntimeError("串流在收到完成事件前中斷")
        text = "".join(parts) or _output_text(completed)
        return StreamResult(status=str(completed.status), output_text=text, usage=getattr(completed, "usage", None))

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
