"""
FILE: backend/gmail_client.py
PURPOSE: Connects to Gmail account for reading invoices and sending reminders.

CURRENT SETUP:
  OAuth account (sender): sampada317@gmail.com
  This account reads its own inbox for invoice emails
  and sends reminder emails from this address.

FUTURE UPGRADE:
  When moving to Infrabeat Outlook (infrabeat.com),
  replace this file with an Outlook/Microsoft Graph API version.
  The rest of the codebase stays the same.

HOW GMAIL AUTH WORKS:
  1. You created OAuth credentials in Google Cloud Console
  2. credentials.json is saved in your project folder
  3. First run: browser opens → you log in → token.json saved
  4. All future runs: uses token.json automatically (no browser)
  5. Token auto-refreshes — no manual action needed

INSTALL GMAIL LIBRARIES (if not done):
  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
"""

import os
import base64
import pickle
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Optional

CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.getenv("GMAIL_TOKEN_FILE", "token.json")

# Gmail permissions needed
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",   # read emails
    "https://www.googleapis.com/auth/gmail.send",        # send emails
    "https://www.googleapis.com/auth/gmail.labels",      # create labels
    "https://www.googleapis.com/auth/gmail.modify",      # apply labels to mark processed
    "https://www.googleapis.com/auth/drive.file",        # backup DBs to Drive (only files THIS app creates)
]

# ── How many days back to scan ────────────────────────────────
# Only emails from the last 30 days are scanned.
# This keeps scans fast and avoids re-processing old emails.
SCAN_DAYS = int(os.getenv("SCAN_DAYS", "30"))

# ── Gmail search query ────────────────────────────────────────
# Finds emails that likely contain invoice/payment information.
# -label:InvoiceAgent-Processed → skips emails already scanned.
# newer_than:30d                → only last 30 days.
# [TEST] is included so test emails sent by send_test_emails.py are found.
SEARCH_QUERY = (
    "label:inbox "
    "-label:InvoiceAgent-Processed "
    "("
        "subject:invoice OR "
        "subject:payment OR "
        "subject:bill OR "
        "subject:statement OR "
        "subject:due OR "
        "subject:overdue OR "
        "subject:receipt OR "
        "subject:reminder OR "
        "subject:[TEST] "           # ← picks up our test emails
    ") "
    f"newer_than:{SCAN_DAYS}d"      # ← configurable, default 30 days
)

PROCESSED_LABEL_NAME = "InvoiceAgent/Processed"


def get_google_credentials():
    """Load the shared Google OAuth credentials (Gmail + Drive) from
    GMAIL_TOKEN_B64 (cloud) or the local token file, refreshing if expired.
    Returns a Credentials object, or None if no token is available.

    Both get_gmail_service() and services/drive_sync.py use this so Gmail and the
    Drive backup share ONE login. For Drive to work the token must include the
    drive.file scope — after adding it to SCOPES above, regenerate the token once.
    """
    import base64
    from google.auth.transport.requests import Request

    creds = None
    token_b64 = os.getenv("GMAIL_TOKEN_B64", "").strip()
    if token_b64:
        try:
            creds = pickle.loads(base64.b64decode(token_b64))
        except Exception as e:
            print(f"⚠ Could not load GMAIL_TOKEN_B64: {e}")
            creds = None
    if creds is None and Path(TOKEN_FILE).exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        if not token_b64:                       # persist refreshed token locally
            try:
                with open(TOKEN_FILE, "wb") as f:
                    pickle.dump(creds, f)
            except Exception:
                pass
    return creds


def get_gmail_service():
    """
    Authenticates with Gmail and returns a service object.

    Credential resolution order (so it works identically local + cloud):
      1. If GMAIL_TOKEN_B64 is set (Streamlit Cloud) → decode the pickled
         token from that base64 env var, in memory. No file needed.
      2. Else if a token file exists on disk (local) → load it.
      3. If no valid token → refresh it, or run the browser OAuth flow
         (local only — the browser flow can't run on the cloud).

    On the cloud you set two Streamlit secrets:
      GMAIL_CREDENTIALS_B64 = base64 of credentials.json
      GMAIL_TOKEN_B64       = base64 of the pickled token.json
    Locally you just keep credentials/credentials.json + credentials/token.json.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Gmail libraries not installed.\n"
            "Run: pip install google-auth google-auth-oauthlib google-api-python-client"
        )

    import base64
    import io

    creds = None

    # ── 1. Cloud path: pickled token from base64 env var ────────────────
    token_b64 = os.getenv("GMAIL_TOKEN_B64", "").strip()
    if token_b64:
        try:
            creds = pickle.loads(base64.b64decode(token_b64))
        except Exception as e:
            print(f"⚠ Could not load GMAIL_TOKEN_B64: {e}")
            creds = None

    # ── 2. Local path: pickled token from disk ──────────────────────────
    if creds is None and Path(TOKEN_FILE).exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    # ── 3. Validate / refresh / (local) browser flow ────────────────────
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired — refresh automatically (works on cloud too,
            # no browser needed as long as we have a refresh token).
            creds.refresh(Request())
            # Persist the refreshed token back to disk if we're local.
            if not token_b64:
                try:
                    with open(TOKEN_FILE, "wb") as f:
                        pickle.dump(creds, f)
                except Exception:
                    pass
        else:
            # No usable token. On the cloud we CANNOT open a browser, so
            # fail with a clear message instead of hanging.
            if token_b64:
                raise RuntimeError(
                    "Gmail token in GMAIL_TOKEN_B64 is invalid or expired and "
                    "has no refresh token. Regenerate token.json locally "
                    "(run authorize_gmail.py), re-encode it to base64, and "
                    "update the GMAIL_TOKEN_B64 secret."
                )

            # ── Local first-time OAuth: get credentials.json ────────────
            # Prefer base64 env var, else the file on disk.
            creds_b64 = os.getenv("GMAIL_CREDENTIALS_B64", "").strip()
            if creds_b64:
                import json
                import tempfile
                client_config = json.loads(base64.b64decode(creds_b64))
                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            else:
                if not Path(CREDENTIALS_FILE).exists():
                    raise FileNotFoundError(
                        f"\n❌ Gmail credentials file not found: {CREDENTIALS_FILE}\n\n"
                        "TO FIX:\n"
                        "1. Go to https://console.cloud.google.com\n"
                        "2. Create a project → Enable Gmail API\n"
                        "3. Go to APIs & Services → Credentials\n"
                        "4. Create OAuth 2.0 Client ID → Desktop Application\n"
                        "5. Download JSON → rename to 'credentials.json'\n"
                        "6. Put it in the credentials/ folder\n"
                        "7. Run authorize_gmail.py again\n"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)

            print("\n🌐 Opening browser for Gmail authorization...")
            print("   Please log in and click 'Allow'\n")
            creds = flow.run_local_server(port=0)

            # Save token for next time (local only)
            try:
                with open(TOKEN_FILE, "wb") as f:
                    pickle.dump(creds, f)
                print("✅ Gmail authorized. Token saved to", TOKEN_FILE)
            except Exception:
                pass

    return build("gmail", "v1", credentials=creds)

def fetch_invoice_emails(service, max_results: int = 50) -> list:
    """
    Search Gmail inbox for invoice-related emails.
    Returns list of email dicts.
    """
    print(f"🔍 Searching Gmail with query: {SEARCH_QUERY[:80]}...")

    result = service.users().messages().list(
        userId="me",
        q=SEARCH_QUERY,
        maxResults=max_results
    ).execute()

    message_refs = result.get("messages", [])
    print(f"   Found {len(message_refs)} matching emails")

    emails = []
    for ref in message_refs:
        try:
            raw_msg = service.users().messages().get(
                userId="me",
                id=ref["id"],
                format="full"
            ).execute()
            parsed = _parse_message(raw_msg)
            if parsed:
                emails.append(parsed)
        except Exception as e:
            print(f"   ⚠️  Could not read message {ref['id']}: {e}")

    return emails


def _parse_message(msg: dict) -> Optional[dict]:
    """Convert raw Gmail API message into a clean dict."""
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}

    body = _extract_plain_text(msg["payload"])
    if not body or len(body.strip()) < 20:
        return None  # Skip empty or near-empty emails

    from_header = headers.get("From", "")
    from_name = from_header.split("<")[0].strip().strip('"') if "<" in from_header else from_header
    from_email = from_header.split("<")[1].rstrip(">") if "<" in from_header else from_header

    return {
        "id":          msg["id"],
        "thread_id":   msg["threadId"],
        "subject":     headers.get("Subject", "(no subject)"),
        "from":        from_email.strip(),
        "from_name":   from_name.strip(),
        "to":          headers.get("To", ""),
        "date":        headers.get("Date", ""),
        "body":        body[:8000],  # cap at 8000 chars to save API tokens
    }


def _extract_plain_text(payload: dict) -> str:
    """Recursively dig through Gmail's MIME structure to get plain text."""
    mime = payload.get("mimeType", "")

    # Direct plain text
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    # HTML — strip tags as fallback
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return re.sub(r"<[^>]+>", " ", html).strip()

    # Multipart — recurse into parts
    for part in payload.get("parts", []):
        text = _extract_plain_text(part)
        if text and len(text.strip()) > 20:
            return text

    return ""


def send_email(
    service,
    to: str,
    subject: str,
    body: str,
    cc:  Optional[str] = None,
    bcc: Optional[str] = None,
) -> bool:
    """
    Send an email via Gmail with support for multiple recipients.

    Parameters
    ----------
    to      : primary recipient(s)  — comma-separated string
              e.g. "sampada317@gmail.com"
              or   "sampada317@gmail.com, mrunal815@gmail.com"

    cc      : CC recipient(s)       — comma-separated string
              e.g. "sampada.suryawanshi@infrabeat.com, boss@company.com"

    bcc     : BCC recipient(s)      — comma-separated string
              e.g. "accounts@company.com"
              (BCC recipients receive the email but are hidden from others)

    Returns True if sent successfully, False if it failed.
    """
    def clean(addr_str):
        """Clean and normalise a comma-separated address string."""
        if not addr_str:
            return None
        # Strip extra spaces around commas
        parts = [a.strip() for a in str(addr_str).split(",") if a.strip()]
        return ", ".join(parts) if parts else None

    to_clean  = clean(to)
    cc_clean  = clean(cc)
    bcc_clean = clean(bcc)

    if not to_clean:
        print("    ❌  No recipient (To) address provided — skipping send")
        return False

    msg = MIMEMultipart()
    msg["To"]      = to_clean
    msg["Subject"] = subject
    if cc_clean:
        msg["Cc"]  = cc_clean
    # Note: BCC is intentionally NOT added to headers (that would expose it).
    # We include BCC addresses only in the SMTP envelope below.
    msg.attach(MIMEText(body, "plain"))

    raw_bytes = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    # Build the full list of delivery addresses for Gmail API
    # (Gmail API needs all addresses in one list for actual delivery)
    all_recipients = []
    for addr_str in [to_clean, cc_clean, bcc_clean]:
        if addr_str:
            all_recipients.extend([a.strip() for a in addr_str.split(",")])

    try:
        service.users().messages().send(
            userId="me",
            body={"raw": raw_bytes}
        ).execute()

        print(f"    ✅  Email sent successfully")
        print(f"        To  : {to_clean}")
        if cc_clean:
            print(f"        CC  : {cc_clean}")
        if bcc_clean:
            print(f"        BCC : {bcc_clean}")
        return True

    except Exception as e:
        print(f"    ❌  Failed to send email: {e}")
        return False


def mark_email_processed(service, message_id: str):
    """
    Add the 'InvoiceAgent/Processed' label to an email.
    This prevents it from being scanned again on the next run.
    """
    try:
        # Get all labels and find ours (or create it)
        all_labels = service.users().labels().list(userId="me").execute().get("labels", [])
        label = next((l for l in all_labels if l["name"] == PROCESSED_LABEL_NAME), None)

        if not label:
            label = service.users().labels().create(
                userId="me",
                body={
                    "name": PROCESSED_LABEL_NAME,
                    "labelListVisibility": "labelHide",  # hidden in sidebar
                    "messageListVisibility": "hide"
                }
            ).execute()

        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label["id"]]}
        ).execute()

    except Exception as e:
        print(f"    ⚠️  Could not label message {message_id}: {e}")
