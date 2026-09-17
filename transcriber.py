"""用 Gemini API（Interactions API）直接讀 YouTube 網址產生逐字稿。

這個頻道的影片沒有 YouTube 字幕，所以 NotebookLM 或抓字幕的套件拿不到逐字稿；
Gemini 會自己「聽」影片的聲音來聽打。
長影片切成數段（start_offset / end_offset）分別送出，避免超過單次輸出上限。
"""
import logging
import re
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
        self.clients = [genai.Client(api_key=k) for k in self.api_keys]
        self.key_index = 0
        # 額度是「每個模型分開計算」：主要模型額度用完時，改用備援模型繼續
        self.models = [model] + ([fallback_model] if fallback_model and fallback_model != model else [])
        self.model_index = 0
        self.models_used: list[str] = []  # 目前這集實際用到的模型（寫入試算表「模型」欄）
        self.keys_used: list[int] = []    # 目前這集實際用到的金鑰編號（1-based）
        self.segment_seconds = segment_minutes * 60
        self.video_fps = video_fps
        self.vocabulary = vocabulary

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
                # 1. 若還有下一組 API Key，馬上切換（維持高品質主要模型）
                if self.key_index + 1 < len(self.api_keys):
                    old_idx = self.key_index
                    self.key_index += 1
                    log.warning("  API 金鑰 #%d 額度用完（%s），馬上切換到 API 金鑰 #%d 繼續（模型 %s）",
                                old_idx + 1, exc, self.key_index + 1, self.model)
                    continue
                # 2. 所有 API Key 在目前模型的額度都用完，改用備援模型，並切換回第 1 組金鑰
                if self.model_index + 1 < len(self.models):
                    old_model = self.model
                    self.model_index += 1
                    self.key_index = 0
                    if len(self.api_keys) > 1:
                        log.warning("  所有 API 金鑰在 %s 的額度均已用完（%s），改用備援模型 %s 並切換回 API 金鑰 #1 繼續",
                                    old_model, exc, self.model)
                    else:
                        log.warning("  %s 額度用完（%s），改用備援模型 %s 繼續", old_model, exc, self.model)
                    continue
                # 3. 所有金鑰與所有模型額度皆耗盡
                raise
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

    def _request(self, video_url: str, start: int, end: int) -> tuple[str, bool]:
        """送出一段影片並等待結果，回傳 (逐字稿, 是否完整)。

        Gemini 讀取 YouTube 影片時可能回傳 HTTP 400「Request contains an invalid argument.」
        （通常在輪詢結果時才出現）。實測同一個片段：
          - 帶 fps=0.2 → 重送幾次都失敗；不帶 fps → 成功
          - 不帶 fps 偶爾也會失敗一次，重送即可
        所以：帶 fps 的請求失敗後，這一段改成不帶 fps 重送；其他錯誤（金鑰、權限、模型不存在除外）
        等待後重送，最多 MAX_SEGMENT_ATTEMPTS 次。
        """
        use_fps = self.video_fps > 0
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
            processing = {"type": "static", "start_offset": f"{start}s", "end_offset": f"{end}s"}
            if use_fps:
                processing["fps"] = self.video_fps
            key_tag = f"，金鑰 #{self.key_index + 1}" if len(self.api_keys) > 1 else ""
            log.info("  Gemini 轉錄 %s–%s（第 %d 次%s%s）", _hms(start), _hms(end), attempt,
                     f"，fps={self.video_fps}" if use_fps else "", key_tag)

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
                is_quota_403 = status == 403 and any(k in message.lower() for k in ("quota", "exhausted", "ratelimit", "rate_limit"))
                if status in NON_RETRYABLE_STATUS and not is_quota_403:
                    raise
                if status == 429 or is_quota_403:
                    # 完整印出是哪一種額度（每日請求數、每分鐘 token 數…），方便判斷
                    detail = _quota_summary(message) or message[:300]
                    if _is_daily_quota(message) or is_quota_403:
                        raise QuotaExhausted(f"每日額度已用完：{detail}") from exc
                else:
                    detail = message[:300]
                last_error, last_status = f"{stage}時發生錯誤（HTTP {status}，模型 {self.model}）：{detail}", status
                log.warning("  %s", last_error)
                if status == 400 and use_fps:
                    use_fps = self._drop_fps(message)
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
                raise QuotaExhausted("已達 Gemini 帳單的支出上限，請到 AI Studio 調整上限")
            last_error = (
                "Gemini 回傳空白逐字稿" if status == "completed"
                else f"Gemini 回傳狀態 {status}：{getattr(interaction, 'errors', None)}"
            )
            last_status = None
            log.warning("  %s", last_error)
            if use_fps:
                use_fps = self._drop_fps(last_error)

        if last_status == 429:
            # 每分鐘額度在等待 2 分鐘後應已恢復；仍然 429 代表是每日（或更長週期）的額度
            raise QuotaExhausted(f"重試 {MAX_SEGMENT_ATTEMPTS} 次仍回傳 429：{last_error}")
        raise RuntimeError(f"Gemini 轉錄 {_hms(start)}–{_hms(end)} 重試 {MAX_SEGMENT_ATTEMPTS} 次仍失敗。最後錯誤：{last_error}")

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
