"""Best-effort alerts for new leads and page reports.

The whole product funnels into "a lead lands and the CS team calls within one
working day" — a promise printed on every page. Without a push, the lead book
is a box someone must remember to open, and the promise depends on memory.
This module is the push.

Design rules, each load-bearing:

  * NEVER blocks and NEVER raises into the request. The lead is already stored
    before an alert is attempted; a broken webhook must not turn a captured
    lead into a customer-facing error. Delivery runs on a daemon thread and
    failures go to the log.

  * Standard library only (urllib + smtplib) — same no-new-dependency rule as
    the rest of the internet-facing process.

  * Channels are additive: a webhook (Google Chat / Slack / anything that takes
    JSON POST), and/or plain SMTP email. Configure either or both; with neither
    configured every call is a silent no-op and main.py warns at boot.

Env:
  PBN_NOTIFY_WEBHOOK    URL that accepts a JSON POST {kind, subject, lines, ...}
  PBN_NOTIFY_EMAIL_TO   comma-separated recipients
  PBN_SMTP_HOST / PBN_SMTP_PORT (587) / PBN_SMTP_USER / PBN_SMTP_PASS
  PBN_SMTP_FROM         defaults to PBN_SMTP_USER

The payload carries the lead's name and mobile — that is the point; the CS
team needs the number to make the call. Point these at internal channels only.
"""
import json
import logging
import os
import smtplib
import threading
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("pbn")

WEBHOOK = os.getenv("PBN_NOTIFY_WEBHOOK", "").strip()
EMAIL_TO = [a.strip() for a in os.getenv("PBN_NOTIFY_EMAIL_TO", "").split(",") if a.strip()]
SMTP_HOST = os.getenv("PBN_SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("PBN_SMTP_PORT", "587"))
SMTP_USER = os.getenv("PBN_SMTP_USER", "")
SMTP_PASS = os.getenv("PBN_SMTP_PASS", "")
SMTP_FROM = os.getenv("PBN_SMTP_FROM", SMTP_USER or "pbn@localhost")
TIMEOUT_SECONDS = 10


def configured() -> bool:
    return bool(WEBHOOK or (EMAIL_TO and SMTP_HOST))


def _send_webhook(payload: dict):
    req = urllib.request.Request(
        WEBHOOK, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "pbn-notify"})
    urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS).close()


def _send_email(subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT_SECONDS) as s:
        s.starttls()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def _deliver(kind: str, subject: str, lines: list, payload: dict):
    if WEBHOOK:
        try:
            _send_webhook(payload)
        except Exception:                                # noqa: BLE001
            log.exception("notify: webhook delivery failed (%s)", kind)
    if EMAIL_TO and SMTP_HOST:
        try:
            _send_email(subject, "\n".join(lines))
        except Exception:                                # noqa: BLE001
            log.exception("notify: email delivery failed (%s)", kind)


def _fire(kind: str, subject: str, lines: list, extra: dict):
    if not configured():
        return
    payload = {"kind": kind, "subject": subject, "text": "\n".join(lines)}
    payload.update(extra)
    threading.Thread(target=_deliver, args=(kind, subject, lines, payload),
                     daemon=True).start()


def lead_alert(lead: dict, page: dict = None):
    """Called AFTER the lead is stored. lead is the row insert_lead returned."""
    business = ((page or {}).get("business_name")
                or lead.get("referrer_business_name") or "—")
    subject = "New loan lead: {} (via {})".format(lead.get("name", "?"), business)
    lines = [
        "Name:     {}".format(lead.get("name", "")),
        "Mobile:   {}".format(lead.get("mobile", "")),
        "Pincode:  {}".format(lead.get("pincode") or "—"),
        "Referred: {}  ({})".format(business, lead.get("source_path") or ""),
        "At (IST): {}".format(lead.get("at", "")),
        "Work it:  /admin/leads",
    ]
    _fire("lead", subject, lines, {
        "lead_id": lead.get("id"), "source_path": lead.get("source_path")})


def report_alert(report: dict):
    """A takedown request is a compliance clock: it starts the moment the
    report lands, not when someone opens the inbox."""
    subject = "Page report ({}) — act on it".format(report.get("request_type", "other"))
    lines = [
        "Type:    {}".format(report.get("request_type", "")),
        "Page:    {}".format(report.get("page_path") or "—"),
        "Details: {}".format((report.get("details") or "")[:300]),
        "Contact: {}".format(report.get("contact") or "—"),
        "At (IST): {}".format(report.get("at", "")),
        "Handle:  /admin/reports",
    ]
    _fire("report", subject, lines, {"report_id": report.get("id")})
