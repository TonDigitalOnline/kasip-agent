from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from google.oauth2 import service_account
from googleapiclient.discovery import build
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
import base64, os

# ===============================
# 🧾 1) Create PDF from Markdown
# ===============================
def create_pdf(ebook_title, language="TH"):
    md_path = "ebook_content.md"
    pdf_path = f"{ebook_title}.pdf"

    # อ่านเนื้อหาจากไฟล์ Markdown
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    c = canvas.Canvas(pdf_path, pagesize=A4)
    width, height = A4
    y = height - 80
    c.setFont("Helvetica", 12)

    for line in lines:
        if y < 100:
            c.showPage()
            y = height - 80
            c.setFont("Helvetica", 12)
        c.drawString(80, y, line.strip())
        y -= 18

    c.save()
    print(f"✅ PDF Created: {pdf_path}")
    return pdf_path


# ===============================
# 💌 2) Send Email via Gmail API
# ===============================
def send_email(recipient, subject, body_text, attachment_path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders
    import base64, os

    creds = service_account.Credentials.from_service_account_file(
        "service-account.json",
        scopes=["https://www.googleapis.com/auth/gmail.send"]
    )
    service = build("gmail", "v1", credentials=creds)

    message = MIMEMultipart()
    message["to"] = recipient
    message["subject"] = subject

    message.attach(MIMEText(body_text, "plain", "utf-8"))

    # Attach PDF
    with open(attachment_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            f"attachment; filename={os.path.basename(attachment_path)}"
        )
        message.attach(part)

    raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw_message}).execute()

    print(f"✅ Email sent to {recipient}, ID: {sent['id']}")
    return {"success": True, "message_id": sent["id"]}
