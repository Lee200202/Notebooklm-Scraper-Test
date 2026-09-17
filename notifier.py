"""用 Gmail（SMTP + 應用程式密碼）寄送通知信。

- 轉錄成功：寫入試算表後立即寄出
- 轉錄失敗：不逐次寄信（排程一天內會自動重試多次），由每天 17:05 的報告統一寄一封

未設定 MAIL_USERNAME / MAIL_APP_PASSWORD 時不寄信，其餘功能照常運作。
"""
import logging
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

import config

log = logging.getLogger(__name__)


class Mailer:
    def __init__(self, username: str, app_password: str, recipients: tuple[str, ...], host: str, port: int):
        self.username = username
        # Google 顯示的應用程式密碼是「abcd efgh ijkl mnop」，登入時要去掉空白
        self.app_password = app_password.replace(" ", "")
        self.recipients = recipients
        self.host = host
        self.port = port

    @classmethod
    def from_settings(cls, cfg: "config.Settings") -> "Mailer | None":
        if not (cfg.mail_username and cfg.mail_app_password):
            return None
        return cls(cfg.mail_username, cfg.mail_app_password, cfg.mail_to, cfg.smtp_host, cfg.smtp_port)

    def _connect(self) -> smtplib.SMTP:
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
            server.starttls()
        server.login(self.username, self.app_password)
        return server

    def send(self, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(("張震逐字稿機器人", self.username))
        message["To"] = ", ".join(self.recipients)
        message.set_content(body)
        server = self._connect()
        try:
            server.send_message(message)
        finally:
            server.quit()
        log.info("已寄出通知信：%s", subject)


def sheet_url(spreadsheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"


def actions_url() -> str:
    """GitHub Actions 會提供 GITHUB_SERVER_URL 與 GITHUB_REPOSITORY；本機執行時沒有就不附連結。"""
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        return ""
    return f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/{repo}/actions/workflows/daily-transcript.yml"


def success_email(items: list[dict], failures: list[tuple[str, str]], sheet: str, actions: str) -> tuple[str, str]:
    """items：每集一個 dict（title、url、chars、models、preview）；failures：(標題, 錯誤訊息)。"""
    subject = f"✅ 逐字稿完成：{items[0]['title']}" if len(items) == 1 else f"✅ 逐字稿完成 {len(items)} 集"
    lines = ["以下直播的逐字稿已寫入 Google 試算表：", ""]
    for item in items:
        lines += [
            f"■ {item['title']}",
            f"  影片：{item['url']}",
            f"  字數：{item['chars']:,} 字（模型：{item['models']}）",
            "  開頭預覽：",
            *(f"    {line}" for line in item["preview"].strip().splitlines()[:8]),
            "",
        ]
    if failures:
        lines += ["這次執行也有未完成的集數（之後的排程會再試）：", *(f"  ✗ {title}：{message}" for title, message in failures), ""]
    lines.append(f"試算表：{sheet}")
    if actions:
        lines.append(f"執行紀錄：{actions}")
    return subject, "\n".join(lines)


def failure_email(title: str, url: str, video_id: str, status: str, checked_at: str,
                  log_rows: list[list[str]], sheet: str, actions: str) -> tuple[str, str]:
    """log_rows：「執行紀錄」工作表中今天這集的列（時間、模式、日期、影片ID、結果、訊息）。"""
    results = {row[4] for row in log_rows}
    lines = [
        f"截至 {checked_at}，今天這集的逐字稿還沒有寫入試算表。",
        "",
        f"■ {title}",
        f"  影片：{url}",
        f"  YouTube 狀態：{status}",
        "",
        "今天的執行紀錄：",
    ]
    if log_rows:
        lines += [f"  {row[0][11:]}  {row[4]}  {row[5][:300]}" for row in log_rows[-10:]]
    else:
        lines.append("  （沒有任何紀錄：今天的排程可能沒有執行到轉錄步驟，請到 GitHub Actions 查看）")
    lines += ["", "可能原因與處理方式："]
    if "配額不足" in results:
        lines.append("  • 配額不足：Gemini 額度用完。每日額度在台灣時間 15:00（冬令 16:00）重置，重置後可手動補抓；"
                     "若經常發生，建議替專案綁定帳單。")
    if "失敗" in results:
        lines.append("  • 失敗：請看上面的錯誤訊息。暫時性錯誤通常手動再執行一次就會成功。")
    if not results & {"配額不足", "失敗"}:
        lines.append("  • 沒有轉錄紀錄：可能是 YouTube 回放一直沒處理完成，或 GitHub 排程沒有執行。")
    lines += ["", "手動補抓：到下面的 GitHub Actions 頁面按「Run workflow」，video_id 填 " + video_id]
    if actions:
        lines.append(f"  {actions}")
    lines.append(f"試算表：{sheet}")
    return f"❌ 逐字稿未完成：{title}", "\n".join(lines)
