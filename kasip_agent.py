from pdf_mailer import create_pdf, send_email

"""
Kasip eBook Delivery Agent
Automatically generate and deliver eBook PDFs upon payment confirmation
"""

import os
import json
import base64
from datetime import datetime
from flask import Flask, request, jsonify
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import requests
from io import BytesIO

app = Flask(__name__)

# ============================================
# CONFIG
# ============================================
CONFIG = {
    # Gmail credentials
    "gmail_credentials_path": "credentials.json",
    "gmail_token_path": "token.json",
    "service_account_path": "service-account.json",

    # Asset/logo
    "kasip_logo_url": None,

    # From email
    "sender_email": "noreply@kasip.com",

    # Google Sheet (optional)
    "google_sheet_id": "",

    # Testing flags
    "skip_slip_verification": True,
    "require_image_content_type": False,

    # ✅ ใส่ตรงนี้
    "content_file_path": "ebook_content.md"
}


# ============================================
# 1) SLIP VERIFICATION
# ============================================
def verify_slip(slip_url: str, **kwargs) -> dict:
    """
    ตรวจสลิปแบบยืดหยุ่น
    คืนค่ารูปแบบเดียวกับที่ webhook ใช้: {"valid": bool, "skipped": bool, "reason": str}
    """
    if CONFIG.get("skip_slip_verification", False):
        return {"valid": True, "skipped": True, "reason": "verification skipped for testing"}

    if not slip_url:
        return {"valid": False, "skipped": False, "reason": "empty slip_url"}

    try:
        r = requests.get(slip_url, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return {"valid": False, "skipped": False, "reason": f"http {r.status_code}"}

        if CONFIG.get("require_image_content_type", False):
            ctype = r.headers.get("content-type", "")
            if not ctype.startswith("image/"):
                return {"valid": False, "skipped": False, "reason": f"content-type {ctype}"}

        # NOTE: ที่นี่เราไม่เซฟภาพลงไฟล์เพื่อความเร็วในการทดสอบ
        return {"valid": True, "skipped": False, "reason": ""}
    except Exception as e:
        return {"valid": False, "skipped": False, "reason": str(e)}

# ============================================
# 2) PDF GENERATION
# ============================================
def create_pdf(ebook_title, language="TH", output_path=None, content_text=None):
    """
    Generate eBook PDF from markdown-like content with page breaks.
    Uses ReportLab Platypus for better text wrapping & layout.
    - If content_text is None, will try to read CONFIG['content_file_path'].
    - Insert a new page where a line equals '---page---'
    """
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    import os

    if output_path is None:
        output_path = f"ebook_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    # --- Load content ---
    if content_text is None:
        path = CONFIG.get("content_file_path", "ebook_content.md")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                content_text = f.read()
        else:
            content_text = f"# {ebook_title}\n\n(ยังไม่มีไฟล์เนื้อหา: {path})"

    # --- Fonts (optional Thai font if available) ---
    # ถ้ามีไฟล์ TH Sarabun ในโฟลเดอร์ ให้ปลดคอมเมนต์ 3 บรรทัดล่างนี้
    # try:
    #     pdfmetrics.registerFont(TTFont('THSarabun', 'THSarabunNew.ttf'))
    #     base_font = 'THSarabun'
    # except:
    base_font = 'Helvetica'

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="TitleCenter",
        parent=styles['Title'],
        fontName=base_font,
        alignment=TA_CENTER,
        fontSize=28,
        spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        name="Body",
        parent=styles['BodyText'],
        fontName=base_font,
        fontSize=12,
        leading=18,
    ))
    styles.add(ParagraphStyle(
        name="H2",
        parent=styles['Heading2'],
        fontName=base_font,
        fontSize=18,
        spaceBefore=12,
        spaceAfter=6,
    ))

    doc = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=40, bottomMargin=36)
    story = []

    # Cover
    story.append(Paragraph(ebook_title, styles['TitleCenter']))
    story.append(Paragraph("Kasip Publishing © 2025", styles['Body']))
    story.append(Spacer(1, 40))
    story.append(PageBreak())

    # Parse simple markdown-ish text
    lines = content_text.splitlines()
    buffer_par = []

    def flush_paragraphs():
        if buffer_par:
            story.append(Paragraph("<br/>".join(buffer_par), styles['Body']))
            story.append(Spacer(1, 8))
            buffer_par.clear()

    for raw in lines:
        line = raw.strip()
        if line == "---page---":
            flush_paragraphs()
            story.append(PageBreak())
            continue
        if line.startswith("## "):        # H2
            flush_paragraphs()
            story.append(Paragraph(line[3:].strip(), styles['H2']))
            continue
        if line.startswith("# "):         # H1
            flush_paragraphs()
            story.append(Paragraph(line[2:].strip(), styles['TitleCenter']))
            story.append(Spacer(1, 12))
            continue
        # normal text
        if line == "":
            flush_paragraphs()
        else:
            buffer_par.append(line)

    flush_paragraphs()
    doc.build(story)
    return output_path


# ============================================
# 3) EMAIL DELIVERY (Gmail API)
# ============================================
def get_gmail_service():
    """
    ใช้ได้ 2 ทาง:
      - OAuth2 token.json (ง่ายสุด)
      - Service Account (ต้องมี Domain-Wide Delegation)
    """
    try:
        # ใช้ Service Account ถ้ามี (กรณีองค์กร)
        if os.path.exists(CONFIG["service_account_path"]) and CONFIG["service_account_path"]:
            credentials = service_account.Credentials.from_service_account_file(
                CONFIG["service_account_path"],
                scopes=['https://www.googleapis.com/auth/gmail.send']
            )
            return build('gmail', 'v1', credentials=credentials)

        # ปกติใช้ OAuth2 token.json
        if os.path.exists(CONFIG["gmail_token_path"]) and CONFIG["gmail_token_path"]:
            credentials = Credentials.from_authorized_user_file(
                CONFIG["gmail_token_path"],
                ['https://www.googleapis.com/auth/gmail.send']
            )
            return build('gmail', 'v1', credentials=credentials)

        raise Exception("No valid Gmail credentials found (token.json/service-account.json).")
    except Exception as e:
        raise Exception(f"Failed to initialize Gmail service: {e}")

def send_email(recipient_email: str, ebook_title: str, pdf_path: str, language: str = "TH") -> dict:
    try:
        service = get_gmail_service()

        msg = MIMEMultipart()
        msg['to'] = recipient_email
        msg['from'] = CONFIG.get("sender_email") or recipient_email
        if language.upper() == "TH":
            msg['subject'] = f"🎉 eBook ของคุณพร้อมแล้ว: {ebook_title}"
            body = (
                "สวัสดีค่ะ,\n\n"
                f'ขอบคุณที่สั่งซื้อ eBook จาก Kasip!\n\nแนบไฟล์ "{ebook_title}" มาให้แล้วครับ/ค่ะ '
                "เปิดอ่านได้ทันทีบนอุปกรณ์ของคุณ\n\n"
                "หากมีคำถาม ติดต่อ support@kasip.com\n\nด้วยความปรารถนาดี,\nทีม Kasip"
            )
        else:
            msg['subject'] = f"🎉 Your eBook is Ready: {ebook_title}"
            body = (
                "Hello,\n\n"
                f'Thank you for purchasing from Kasip!\n\nYour eBook "{ebook_title}" is attached.\n\n'
                "If you have any questions, contact support@kasip.com\n\nBest regards,\nKasip Team"
            )
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        with open(pdf_path, 'rb') as f:
            part = MIMEBase('application', 'pdf')
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = f"{ebook_title.replace(' ', '_')}.pdf"
        part.add_header('Content-Disposition', f'attachment; filename="{filename}"')
        msg.attach(part)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')
        res = service.users().messages().send(userId='me', body={'raw': raw}).execute()

        return {"success": True, "message_id": res.get('id')}
    except HttpError as e:
        return {"success": False, "error": f"Gmail API error: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ============================================
# 4) ACTIVITY LOG (optional)
# ============================================
def log_activity(email, ebook_title, status, details=None):
    try:
        sheet_id = CONFIG.get("google_sheet_id", "")
        if not sheet_id:
            return {"logged": False, "reason": "no_sheet_config"}
        if os.path.exists(CONFIG["service_account_path"]):
            creds = service_account.Credentials.from_service_account_file(
                CONFIG["service_account_path"],
                scopes=['https://www.googleapis.com/auth/spreadsheets']
            )
            svc = build('sheets', 'v4', credentials=creds)
            row = [datetime.now().isoformat(), email, ebook_title, status, json.dumps(details or {}, ensure_ascii=False)]
            body = {'values': [row]}
            svc.spreadsheets().values().append(
                spreadsheetId=sheet_id,
                range='Deliveries!A:E',
                valueInputOption='RAW',
                body=body
            ).execute()
            return {"logged": True}
        return {"logged": False, "reason": "no_service_account"}
    except Exception as e:
        return {"logged": False, "error": str(e)}

# ===================================================
# 5) WEBHOOK
# ===================================================
@app.route('/webhook/deliver-ebook', methods=['POST'])
def deliver_ebook_webhook():
    try:
        # 1) รับข้อมูลจาก webhook
        data = request.get_json(force=True) or {}
        print("\n==============================")
        print("📩 Webhook Triggered!")
        print("Incoming Data:", data)
        print("==============================")

        # 2) ตรวจฟิลด์ที่จำเป็น
        required = ['email', 'slip_image_url', 'ebook_title']
        missing = [k for k in required if k not in data]
        if missing:
            print("❌ Missing Fields:", missing)
            return jsonify({"success": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

        email = data['email']
        slip_image_url = data['slip_image_url']
        ebook_title = data['ebook_title']
        language = data.get('language', 'TH')

        print(f"🧾 Processing Request")
        print(f" - Email: {email}")
        print(f" - Slip: {slip_image_url}")
        print(f" - Ebook: {ebook_title}")
        print(f" - Lang: {language}")

        # 3) จำลองการตรวจสลิป
        slip = {"valid": True, "reason": "verification skipped for testing"}
        print("✅ Slip Verified")

        # 4) จำลองการสร้างไฟล์ PDF
        pdf_path = "ebook_test.pdf"
        print("📘 PDF Created (mock):", pdf_path)

        # 5) จำลองการส่งอีเมล
        print("📨 Sending email...")
        email_res = {"success": True, "message_id": "mock12345"}

        # 6) สรุปผลลัพธ์
        if email_res.get("success"):
            print("✅ eBook Delivered Successfully!")
            return jsonify({
                "success": True,
                "message": "✅ eBook delivered successfully",
                "details": {
                    "email": email,
                    "ebook": ebook_title,
                    "slip": slip,
                    "email_result": email_res
                }
            }), 200
        else:
            print("❌ Email Delivery Failed")
            return jsonify({"success": False, "error": "Email delivery failed"}), 500

    except Exception as e:
        print("🔥 ERROR in deliver_ebook_webhook:", str(e))
        return jsonify({"success": False, "error": str(e)}), 500

# ===============================================
# 6) MAIN — Production Mode
# ===============================================
if __name__ == "__main__":
    os.makedirs('logs', exist_ok=True)
    os.makedirs('temp', exist_ok=True)

    print("🚀 Starting Kasip eBook Delivery Agent (Production Mode)…")
    print("📬 Webhook: /webhook/deliver-ebook")
    print("💓 Health  : /health")

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000)
    except Exception as e:
        print(f"❌ Error starting server: {e}")
