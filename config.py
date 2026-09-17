"""集中管理設定。所有值都從環境變數讀取，由 GitHub Actions 注入：

- 金鑰類放在 repo 的 Secrets（Settings → Secrets and variables → Actions → Secrets）
- 可調參數放在 repo 的 Variables（同一頁的 Variables 分頁），沒設定就用預設值
"""
import base64
import binascii
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta, timezone

# 台灣沒有日光節約時間，固定 UTC+8
TAIPEI = timezone(timedelta(hours=8))

REQUIRED_SECRETS = ("GEMINI_API_KEY", "YOUTUBE_API_KEY", "SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON")

# 試算表的工作表名稱
SHEET_TRANSCRIPTS = "逐字稿"
SHEET_LOG = "執行紀錄"

# Excel 單一儲存格上限 32,767 字、Google 試算表 50,000 字，
# 取 30,000 切段，下載成 .xlsx 時才不會被截斷
CELL_CHUNK_CHARS = 30_000

SYSTEM_INSTRUCTION = """你是專業的中文逐字稿聽打員，負責把台灣股市直播節目「張震 股市盤中家教班」的語音完整轉成文字。

規則：
1. 逐字完整聽打，不可摘要、省略、改寫或自行補充；「嗯」「那個」等無意義口頭禪可略過，但所有觀點、數字、個股、價位都必須保留。
2. 使用繁體中文與台灣用語，標點符號用全形。
3. 股票代號、指數點位、價格、漲跌幅、日期一律用阿拉伯數字（例如：2330 台積電、跌破 22,500 點、漲 3.5%）。
4. 依語意或話題轉換分段，段落之間空一行。
5. 主持人唸出的觀眾留言照實聽打。
6. 片頭等待畫面、背景音樂、無人說話的片段直接略過，不要描述。
7. 聽不清楚的字詞寫成最合理的寫法，不要加任何註記、括號或說明。
8. 只輸出逐字稿本文，不要加標題、前言、摘要或結語。"""

DEFAULT_VOCABULARY = (
    "張震,信誠環球投顧,加權指數,櫃買指數,台積電,聯發科,鴻海,外資,投信,自營商,"
    "三大法人,融資,融券,當沖,月線,季線,半年線,年線,K線,KD,MACD,RSI,布林通道,"
    "多頭,空頭,停損,停利,除權息,法說會,費半,那斯達克,道瓊,聯準會,CPI,ETF"
)


class ConfigError(Exception):
    """設定缺漏或格式錯誤，訊息會直接顯示在 GitHub Actions 的 log。"""


def env(name: str, default: str = "") -> str:
    # 未設定的 GitHub Variables 會以空字串傳入，一律視為「使用預設值」
    value = os.getenv(name, "").strip()
    return value if value else default


def _number(name: str, default, cast, minimum):
    raw = env(name)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} 必須是數字，目前是「{raw}」") from None
    if value < minimum:
        raise ConfigError(f"{name} 不能小於 {minimum}，目前是 {value}")
    return value


def missing_secrets() -> list[str]:
    return [name for name in REQUIRED_SECRETS if not env(name)]


def parse_service_account(raw: str) -> dict:
    """接受服務帳戶金鑰的 JSON 原文，或 base64 編碼後的 JSON（Cloud Shell 用 base64 -w0 產生）。

    錯誤訊息刻意不包含金鑰內容，避免洩漏到公開的 Actions log。
    """
    raw = raw.strip()
    if raw.startswith("{"):
        text = raw
    else:
        try:
            # 非 base64 字元（例如複製時夾帶的換行、空白）會被忽略
            text = base64.b64decode(raw).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON 不是有效的 JSON，也不是有效的 base64，請重新複製貼上") from None
    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON 的 JSON 格式錯誤（第 {exc.lineno} 行第 {exc.colno} 字），"
            "建議改用 base64 -w0 key.json 產生的單行內容"
        ) from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON 不是服務帳戶金鑰（type 應為 service_account）")
    missing = [k for k in ("client_email", "private_key", "token_uri") if not info.get(k)]
    if missing:
        raise ConfigError(f"GOOGLE_SERVICE_ACCOUNT_JSON 缺少欄位：{', '.join(missing)}")
    # GitHub 只會遮蔽 Secret 原文；base64 解開後的值要另外登記遮蔽，萬一出現在錯誤訊息裡也會顯示成 ***
    mask_in_actions(info.get("client_email"), info.get("project_id"), info.get("private_key_id"))
    return info


def mask_in_actions(*values: str | None) -> None:
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    for value in values:
        if value:
            print(f"::add-mask::{value}", flush=True)


@dataclass(frozen=True)
class Settings:
    # 金鑰欄位 repr=False：就算不小心 print(settings) 也不會把金鑰印到公開 log
    youtube_api_key: str = field(repr=False)
    channel_id: str
    title_keyword: str
    init_video_count: int
    ready_delay_minutes: int

    gemini_api_key: str = field(repr=False)
    gemini_model: str
    segment_minutes: int
    video_fps: float
    vocabulary: tuple[str, ...]
    max_attempts_per_day: int

    spreadsheet_id: str = field(repr=False)
    service_account_json: str = field(repr=False)  # 原文，使用時再用 parse_service_account 解析


def load_settings(strict: bool = True) -> Settings:
    """strict=False 時允許 Secrets 缺漏（給 --mode check 逐項檢查用）。"""
    missing = missing_secrets()
    if strict and missing:
        raise ConfigError(f"缺少 GitHub Secrets：{', '.join(missing)}")
    return Settings(
        youtube_api_key=env("YOUTUBE_API_KEY"),
        # @xinchenginsta（張震_股市盤中家教班）的頻道 ID
        channel_id=env("YOUTUBE_CHANNEL_ID", "UCPqyYS3n6yyXL2jygauXpzg"),
        title_keyword=env("TITLE_KEYWORD", "股市盤中家教班"),
        init_video_count=_number("INIT_VIDEO_COUNT", 5, int, 1),
        # 直播結束後至少等幾分鐘才開始轉錄，讓 YouTube 處理完回放
        ready_delay_minutes=_number("READY_DELAY_MINUTES", 5, int, 0),
        gemini_api_key=env("GEMINI_API_KEY"),
        gemini_model=env("GEMINI_MODEL", "gemini-3.8-flash"),
        # 每段送給 Gemini 的影片長度；免費方案每天約 20 次請求，30 分鐘一段最省
        segment_minutes=_number("SEGMENT_MINUTES", 30, int, 5),
        # 預設 0 = 不指定 fps（API 預設每秒 1 張畫面）。實測 fps=0.2 雖然省 token，
        # 但部分影片片段會讓 Gemini 回傳 HTTP 400「Request contains an invalid argument.」
        video_fps=_number("VIDEO_FPS", 0, float, 0),
        vocabulary=tuple(w.strip() for w in env("CUSTOM_VOCABULARY", DEFAULT_VOCABULARY).split(",") if w.strip()),
        max_attempts_per_day=_number("MAX_ATTEMPTS_PER_DAY", 3, int, 1),
        spreadsheet_id=env("SPREADSHEET_ID"),
        service_account_json=env("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
