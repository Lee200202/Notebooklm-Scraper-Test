"""把逐字稿存到 Google 試算表（雲端 Excel，可隨時「檔案 → 下載 → Microsoft Excel」）。

工作表「逐字稿」：一集一列，逐字稿太長時自動切到「逐字稿1、逐字稿2…」多個欄位。
工作表「執行紀錄」：每次轉錄成功／失敗的紀錄，也用來限制同一集每天的重試次數。
"""
from datetime import datetime

import gspread
from gspread.exceptions import APIError, SpreadsheetNotFound

from config import CELL_CHUNK_CHARS, SHEET_LOG, SHEET_TRANSCRIPTS, TAIPEI
from youtube_client import Stream

META_HEADER = ["日期", "星期", "標題", "影片ID", "影片網址", "直播開始", "直播結束", "長度(分鐘)", "字數", "模型", "寫入時間"]
TOTAL_COLUMNS = 26  # A–Z，後面 15 欄給逐字稿（最多 45 萬字）
TRANSCRIPT_HEADER = META_HEADER + [f"逐字稿{i}" for i in range(1, TOTAL_COLUMNS - len(META_HEADER) + 1)]
LOG_HEADER = ["時間", "模式", "日期", "影片ID", "結果", "訊息"]
WEEKDAYS = "一二三四五六日"

RESULT_OK = "成功"
RESULT_FAILED = "失敗"
RESULT_QUOTA = "配額不足"
RESULT_CHECK = "設定檢查通過"

# 只要求試算表權限，不要求整個 Google 雲端硬碟的權限
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else ""


class SheetStore:
    def __init__(self, spreadsheet_id: str, service_account_info: dict):
        client = gspread.service_account_from_dict(service_account_info, scopes=SCOPES)
        # 不把服務帳戶 email 印出來：repo 是公開的，Actions log 任何人都看得到
        share_hint = "並已在試算表「共用」給服務帳戶 email（Cloud Shell 步驟最後顯示的那個，權限：編輯者）"
        try:
            self.book = client.open_by_key(spreadsheet_id)
        except SpreadsheetNotFound:
            raise RuntimeError(f"找不到試算表：請確認 SPREADSHEET_ID 正確，{share_hint}") from None
        except PermissionError as exc:
            raise RuntimeError(
                f"沒有權限開啟試算表：請確認 Google Sheets API 已啟用，{share_hint}。原始錯誤：{exc.__cause__}"
            ) from None
        try:
            self.transcripts = self._worksheet(SHEET_TRANSCRIPTS, TRANSCRIPT_HEADER)
            self.log_sheet = self._worksheet(SHEET_LOG, LOG_HEADER)
        except APIError as exc:
            if exc.response.status_code == 403:
                raise RuntimeError(f"服務帳戶無法寫入試算表：請把共用權限從「檢視者」改成「編輯者」。原始錯誤：{exc}") from None
            raise

    def _worksheet(self, title: str, header: list[str]):
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=1000, cols=len(header))
        if not ws.row_values(1):
            ws.update(range_name="A1", values=[header])
            ws.freeze(rows=1)
        return ws

    def existing_video_ids(self) -> set[str]:
        column = META_HEADER.index("影片ID") + 1
        return {v for v in self.transcripts.col_values(column)[1:] if v}

    def append_transcript(self, stream: Stream, text: str, model: str) -> None:
        chunks = [text[i : i + CELL_CHUNK_CHARS] for i in range(0, len(text), CELL_CHUNK_CHARS)]
        max_chunks = TOTAL_COLUMNS - len(META_HEADER)
        if len(chunks) > max_chunks:
            raise RuntimeError(f"逐字稿長度 {len(text)} 字超過試算表可存放的 {max_chunks} 欄")
        row = [
            stream.episode_date.isoformat(),
            WEEKDAYS[stream.episode_date.weekday()],
            stream.title,
            stream.video_id,
            stream.url,
            _fmt(stream.started_at),
            _fmt(stream.ended_at),
            round(stream.duration_sec / 60, 1),
            len("".join(text.split())),
            model,
            _fmt(datetime.now(TAIPEI)),
            *chunks,
        ]
        # RAW：逐字稿內容若以 = 或 + 開頭也不會被當成公式
        self.transcripts.append_row(row, value_input_option="RAW", table_range="A1")

    def log(self, mode: str, stream: Stream | None, result: str, message: str = "") -> None:
        now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
        self.log_sheet.append_row(
            [
                now,
                mode,
                stream.episode_date.isoformat() if stream else "",
                stream.video_id if stream else "",
                result,
                message[:2000],
            ],
            value_input_option="RAW",
            table_range="A1",
        )

    def failed_attempts_today(self, video_id: str) -> int:
        today = datetime.now(TAIPEI).strftime("%Y-%m-%d")
        return sum(
            1
            for row in self.log_sheet.get_all_values()[1:]
            if len(row) >= 5 and row[0].startswith(today) and row[3] == video_id and row[4] == RESULT_FAILED
        )
