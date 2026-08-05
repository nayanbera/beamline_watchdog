import os
import logging
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

_INSECURE_DEFAULT = 'dev-key-change-in-production'


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or _INSECURE_DEFAULT
    SQLALCHEMY_DATABASE_URI = (
        os.environ.get('DATABASE_URL') or
        'sqlite:///' + os.path.join(basedir, 'instance', 'watchdog.db')
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600  # token valid for 1 hour

    @classmethod
    def warn_if_insecure(cls):
        if cls.SECRET_KEY == _INSECURE_DEFAULT:
            logging.getLogger('config').critical(
                '\n' + '=' * 60 +
                '\nINSECURE SECRET_KEY — sessions can be forged!' +
                '\nFix: generate a key and add it to .env' +
                '\n  python -c "import secrets; print(secrets.token_hex(32))"' +
                '\n  echo SECRET_KEY=<value> >> .env' +
                '\n' + '=' * 60
            )
