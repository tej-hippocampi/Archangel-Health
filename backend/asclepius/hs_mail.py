"""Durable health-system lifecycle letters; enqueue with the business transaction."""
import hashlib
import json
import os
from datetime import datetime
from onboarding_emails import (build_hs_dla_request_email,
    build_hs_agreement_receipt_email, build_hs_uploads_open_email)


def portal_url():
    base = (os.getenv('PUBLIC_BASE_URL') or os.getenv('ASCLEPIUS_PORTAL_URL') or os.getenv('BASE_URL') or 'http://localhost:8000').rstrip('/')
    return base if base.endswith('/provider') else base + '/provider'


def message(hs_id, kind, event, to, subject, body, attachment=None):
    return dict(idempotency_key=f"hs:{hs_id}:{kind}:{event}:" + hashlib.sha256(to.strip().lower().encode()).hexdigest(),
                kind=kind, recipient_email=to.strip(), subject=subject, body_html=body,
                attachment_json=json.dumps(attachment) if attachment else None)


def request_messages(hs, accounts, event='approval'):
    body = build_hs_dla_request_email(organization=hs['name'], portal_url=portal_url())
    return [message(hs['hs_id'], 'hs_dla_request', event, a['email'],
                    'One signature away: your data licensing agreement', body)
            for a in accounts if a.get('active') and a.get('email')]


def signed_messages(hs, accounts, signature, pdf_sha, founders=()):
    from asclepius import dla
    receipt = build_hs_agreement_receipt_email(
        organization=hs['name'], doc_version=signature['doc_version'],
        signer_name=signature['typed_name'], signer_title=signature['typed_title'],
        signed_at=signature['signed_at'], doc_sha256=signature['doc_sha256'])
    event = signature['doc_sha256']
    messages = [message(hs['hs_id'], 'hs_agreement_signed', event, addr,
        f"[Health system] Agreement signed: {hs['name']}", receipt) for addr in founders]
    if signature.get('signer_email'):
        messages.append(message(hs['hs_id'], 'hs_agreement_receipt', event,
            signature['signer_email'], f"Signed: your data licensing agreement, {hs['name']}", receipt,
            {'sha256': pdf_sha, 'filename': dla.pdf_filename(organization=hs['name'], version=signature['doc_version'])}))
    opened = build_hs_uploads_open_email(organization=hs['name'], portal_url=portal_url(),
        signer_name=signature['typed_name'], signed_at=signature['signed_at'])
    messages.extend(message(hs['hs_id'], 'hs_uploads_open', event, a['email'],
        f"Uploads are open for {hs['name']}", opened)
        for a in accounts if a.get('active') and a.get('email'))
    return messages


def enqueue(conn, messages):
    for row in messages or []:
        conn.execute("INSERT OR IGNORE INTO admin_notify_outbox "
            "(idempotency_key,kind,subject,body_html,recipient_email,attachment_json,status,created_at) "
            "VALUES (?,?,?,?,?,?,'pending',?)", tuple(row[k] for k in (
                'idempotency_key','kind','subject','body_html','recipient_email','attachment_json'))
                + (datetime.utcnow().isoformat(),))


def attachments(row):
    if not row.get('attachment_json'):
        return []
    from asclepius import assets
    item = json.loads(row['attachment_json'])
    data, _ = assets.load_asset(item['sha256'], verify=True)
    return [(item['filename'], 'application/pdf', data)]


def status(store, hs_id):
    with store._conn() as conn:
        rows = conn.execute("SELECT id,kind,recipient_email,status,send_attempts,last_error,sent_at "
                            "FROM admin_notify_outbox WHERE idempotency_key LIKE ? ORDER BY id",
                            (f'hs:{hs_id}:%',)).fetchall()
    return [dict(r) for r in rows]
