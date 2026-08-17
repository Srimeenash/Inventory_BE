import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


logger = logging.getLogger(__name__)


def _send_multipart_email(
    *,
    recipient_email,
    subject,
    text_template,
    html_template,
    context,
):
    if not recipient_email:
        logger.warning(
            "IPMS email skipped: recipient email missing."
        )
        return False

    try:
        text_content = render_to_string(
            text_template,
            context,
        )

        html_content = render_to_string(
            html_template,
            context,
        )

        email = EmailMultiAlternatives(
            subject=subject,
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient_email],
        )

        email.attach_alternative(
            html_content,
            "text/html",
        )

        email.send(
            fail_silently=False
        )

        return True

    except Exception:
        logger.exception(
            "Unable to send IPMS email to %s",
            recipient_email,
        )
        return False


def send_ipms_email(
    *,
    recipient_email,
    subject,
    context,
):
    return _send_multipart_email(
        recipient_email=recipient_email,
        subject=subject,
        text_template=(
            "emails/ipms_notification.txt"
        ),
        html_template=(
            "emails/ipms_notification.html"
        ),
        context=context,
    )


def send_login_otp_email(
    *,
    recipient_email,
    recipient_name,
    code,
    expiry_minutes=5,
):
    return _send_multipart_email(
        recipient_email=recipient_email,
        subject=(
            "IPMS Login Verification Code"
        ),
        text_template=(
            "emails/login_otp.txt"
        ),
        html_template=(
            "emails/login_otp.html"
        ),
        context={
            "recipient_name": (
                recipient_name
                or recipient_email
            ),
            "code": code,
            "expiry_minutes": (
                expiry_minutes
            ),
        },
    )