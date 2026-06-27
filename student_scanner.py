"""Student QR / barcode verification helpers."""
import base64
import io
import os

import qrcode
from flask import current_app, has_request_context, request


def get_site_base_url():
    """Resolve public base URL for QR links (SITE_URL env or current request host)."""
    base = ''
    if current_app:
        base = (current_app.config.get('SITE_URL') or os.environ.get('SITE_URL') or '').strip()
    if not base and has_request_context() and request:
        base = request.host_url.rstrip('/')
    return (base or '').rstrip('/')


def build_student_verify_url(student, base_url=None):
    """Build the public verification URL encoded in the student QR code."""
    if not student or not getattr(student, 'secure_qr_token', None):
        return None
    base = (base_url or get_site_base_url()).rstrip('/')
    return f'{base}/verify-student/{student.secure_qr_token}'


def generate_student_scanner_code(student, base_url=None):
    """
    Return a base64 data-URI PNG QR image for template embedding.
    Encodes the secure verification URL for this student.
    """
    verify_url = build_student_verify_url(student, base_url=base_url)
    if not verify_url:
        return None

    qr = qrcode.QRCode(version=1, box_size=8, border=3)
    qr.add_data(verify_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white')

    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    encoded = base64.b64encode(buffer.getvalue()).decode('utf-8')
    return f'data:image/png;base64,{encoded}'
