"""
Re-authenticate Gmail OAuth token.
Run inside Docker container:
  docker exec -it ener-ai-app python scripts/gmail_reauth.py

Requires /tmp/credentials.json (download from Google Cloud Console → OAuth 2.0 Client IDs → Download JSON)
"""
import json
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_PATH = Path("/app/data/gmail_token.json")
CREDS_PATH = Path("/tmp/credentials.json")


def main():
    if not CREDS_PATH.exists():
        print(f"❌ ไม่พบ {CREDS_PATH}")
        print("📋 ขั้นตอน:")
        print("  1. ไป Google Cloud Console → APIs & Services → Credentials")
        print("  2. กด Download JSON ของ OAuth 2.0 Client ID")
        print("  3. copy ไฟล์เข้า container:")
        print("     docker cp credentials.json ener-ai-app:/tmp/credentials.json")
        return

    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
    # run_console() = ให้ URL แล้วรับ code จาก terminal (headless-friendly)
    creds = flow.run_console()

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"✅ บันทึก token ที่ {TOKEN_PATH}")
    print("🎉 Gmail พร้อมใช้งานแล้วครับ")


if __name__ == "__main__":
    main()
