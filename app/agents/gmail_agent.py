import asyncio
import base64
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.core.ai import chat
from app.core.agents import log_agent_run
from app.core.event_log import log_event
from app.core.policy import build_system_prompt

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_TOKEN_PATH = Path("/app/data/gmail_token.json")
_CREDENTIALS_PATH = Path("/tmp/credentials.json")
_BANGKOK = ZoneInfo("Asia/Bangkok")
_SUMMARY_SYSTEM = build_system_prompt(
    """งานของพี่ตอนนี้: สรุป email เป็นภาษาไทย แยกตามความสำคัญ high/medium/low
บอกสั้น กระชับ ว่าฉบับไหนควรรีบตอบก่อน"""
)
_DRAFT_SYSTEM = build_system_prompt(
    """งานของพี่ตอนนี้: ช่วยกบร่าง reply email แบบสุภาพ กระชับ ใช้งานได้จริง
ถ้า email เป็นภาษาอังกฤษ ให้ร่าง reply ภาษาอังกฤษที่สุภาพ"""
)


def _build_sync_service():
    if not _TOKEN_PATH.exists():
        raise FileNotFoundError(f"ไม่พบ gmail token ที่ {_TOKEN_PATH}")

    creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    if not creds.valid:
        if _CREDENTIALS_PATH.exists():
            raise RuntimeError("gmail token ใช้งานไม่ได้ กรุณา refresh token ใหม่จาก credentials")
        raise RuntimeError("gmail token ใช้งานไม่ได้ และไม่พบ credentials สำหรับ refresh")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


async def get_gmail_service():
    return await asyncio.to_thread(_build_sync_service)


async def _execute_gmail(callable_obj):
    return await asyncio.to_thread(callable_obj)


async def _get_message_metadata(service, email_id: str, headers: list[str] | None = None) -> dict:
    request_headers = headers or ["From", "Subject", "Date", "Message-ID"]
    return await _execute_gmail(
        lambda: service.users()
        .messages()
        .get(
            userId="me",
            id=email_id,
            format="metadata",
            metadataHeaders=request_headers,
        )
        .execute()
    )


def _headers_map(payload: dict) -> dict[str, str]:
    return {
        str(header.get("name") or ""): str(header.get("value") or "")
        for header in payload.get("headers", [])
    }


def _parse_email_date(date_str: str) -> str:
    try:
        dt = parsedate_to_datetime(date_str)
        bangkok = dt.astimezone(_BANGKOK)
        return bangkok.strftime("%d/%m %H:%M")
    except Exception:
        return str(date_str or "")[:16]


@log_agent_run("GmailAgent")
async def fetch_unread_emails(max_results: int = 10) -> list[dict]:
    service = await get_gmail_service()
    results = await _execute_gmail(
        lambda: service.users()
        .messages()
        .list(userId="me", q="is:unread", maxResults=max_results)
        .execute()
    )

    emails: list[dict] = []
    for message in results.get("messages", []):
        detail = await _get_message_metadata(service, str(message.get("id") or ""))
        headers = _headers_map(detail.get("payload", {}) or {})
        snippet = str(detail.get("snippet") or "").strip()
        emails.append(
            {
                "id": str(message.get("id") or ""),
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "date_bangkok": _parse_email_date(headers.get("Date", "")),
                "snippet": snippet[:200],
                "thread_id": str(detail.get("threadId") or ""),
                "message_id": headers.get("Message-ID", ""),
            }
        )
    return emails


@log_agent_run("GmailAgent")
async def summarize_emails() -> str:
    emails = await fetch_unread_emails()
    if not emails:
        return "📧 ไม่มี email ใหม่ครับ"

    email_text = "\n\n".join(
        [
            f"ID: {email['id']}\nจาก: {email['from']}\nเรื่อง: {email['subject']}\nเนื้อหา: {email['snippet']}"
            for email in emails
        ]
    )

    summary = await chat(
        f"สรุป emails เหล่านี้เป็นภาษาไทย บอกว่าอันไหนสำคัญ:\n\n{email_text}",
        system=_SUMMARY_SYSTEM,
        agent="gmail",
        preferred_model="groq",
        strict_model=True,
    )

    lines = [f"📧 Email ใหม่ {len(emails)} ฉบับ", "", summary, ""]
    for index, email in enumerate(emails[:5], start=1):
        lines.append(
            f"{index}. {email['subject'][:50]}\n"
            f"   จาก: {email['from'][:30]}\n"
            f"   🕐 {email.get('date_bangkok', email.get('date', '')[:16])}\n"
            f"   ID: {email['id']}"
        )

    result_text = "\n".join(lines)
    try:
        await log_event(
            agent_name="GmailAgent",
            event_type="insight",
            summary=f"สรุป email ใหม่ {len(emails)} ฉบับ",
            tags=["gmail", "email", "summary"],
            context=result_text[:400],
            result="success",
        )
    except Exception:
        pass
    return result_text


async def _fetch_email_for_reply(service, email_id: str) -> dict:
    detail = await _get_message_metadata(service, email_id, ["From", "Subject", "Date", "Message-ID"])
    headers = _headers_map(detail.get("payload", {}) or {})
    return {
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "message_id": headers.get("Message-ID", ""),
        "thread_id": str(detail.get("threadId") or ""),
        "snippet": str(detail.get("snippet") or "").strip(),
    }


@log_agent_run("GmailAgent")
async def draft_reply(email_id: str) -> str:
    service = await get_gmail_service()
    original = await _fetch_email_for_reply(service, email_id)
    if not original.get("subject") and not original.get("from"):
        return f"❌ ไม่พบ email id {email_id}"

    prompt = (
        f"ช่วยร่าง reply email ฉบับนี้\n"
        f"จาก: {original['from']}\n"
        f"เรื่อง: {original['subject']}\n"
        f"เนื้อหาโดยย่อ: {original['snippet']}\n\n"
        "ให้ตอบพร้อมใช้งานจริง และไม่ยาวเกินไป"
    )
    draft = await chat(
        prompt,
        system=_DRAFT_SYSTEM,
        agent="gmail",
        preferred_model="groq",
        strict_model=True,
    )
    return (
        f"✉️ Draft reply สำหรับ email {email_id}\n"
        f"ถึง: {original['from']}\n"
        f"เรื่อง: {original['subject']}\n\n"
        f"{draft.strip()}"
    )


@log_agent_run("GmailAgent")
async def reply_email(email_id: str, reply_text: str) -> str:
    service = await get_gmail_service()
    original = await _fetch_email_for_reply(service, email_id)
    if not original.get("subject") and not original.get("from"):
        return f"❌ ไม่พบ email id {email_id}"

    def _send():
        msg = MIMEText(reply_text)
        msg["To"] = original.get("from", "")
        msg["Subject"] = "Re: " + original.get("subject", "")
        if original.get("message_id"):
            msg["In-Reply-To"] = original["message_id"]
            msg["References"] = original["message_id"]

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return (
            service.users()
            .messages()
            .send(
                userId="me",
                body={"raw": raw, "threadId": original.get("thread_id", "")},
            )
            .execute()
        )

    await _execute_gmail(_send)
    try:
        await log_event(
            agent_name="GmailAgent",
            event_type="action",
            summary=f"ตอบ email {email_id}",
            tags=["gmail", "email", "reply"],
            result="success",
        )
    except Exception:
        pass
    return f"✅ ตอบ email แล้วครับ\nถึง: {original.get('from', '')}"
