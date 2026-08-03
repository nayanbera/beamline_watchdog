import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

logger = logging.getLogger(__name__)


def send_notification_email(recipients, subject, body, config):
    """
    Send a plain-text notification email.
    config: dict of SystemConfig key→value strings.
    Returns (success: bool, message: str).
    """
    if not recipients:
        return False, 'No recipients specified'

    sender = config.get('mail_sender', 'watchdog@localhost')
    host = config.get('mail_server', 'localhost')
    port = int(config.get('mail_port', 25))
    username = config.get('mail_username', '')
    password = config.get('mail_password', '')
    use_tls = config.get('mail_use_tls', 'false').lower() == 'true'
    use_ssl = config.get('mail_use_ssl', 'false').lower() == 'true'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    msg.attach(MIMEText(body, 'plain'))

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=10)
        else:
            server = smtplib.SMTP(host, port, timeout=10)
            if use_tls:
                server.starttls()

        if username and password:
            server.login(username, password)

        server.sendmail(sender, recipients, msg.as_string())
        server.quit()
        logger.info('Email sent to %s: %s', recipients, subject)
        return True, 'Email sent successfully'
    except Exception as exc:
        logger.error('Failed to send email: %s', exc)
        return False, str(exc)
