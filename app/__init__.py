import os
import logging
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'admin.login'
login_manager.login_message = 'Please log in to access the admin panel.'
login_manager.login_message_category = 'warning'


def create_app():
    app = Flask(__name__)

    from config import Config
    app.config.from_object(Config)

    os.makedirs(os.path.dirname(
        app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
    ), exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        from . import models  # noqa: F401
        from . import auth    # noqa: F401  registers user_loader
        db.create_all()
        _run_migrations()
        _initialize_defaults()

    from .routes.dashboard import dashboard_bp
    from .routes.admin import admin_bp
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp, url_prefix='/admin')

    apply_epics_env(app)

    from .watchdog import start_watchdog
    start_watchdog(app)

    _register_template_globals(app)

    return app


def _register_template_globals(app):
    _STATUS_BADGE = {
        'OK':           'bg-success',
        'ALARM':        'bg-danger',
        'DISCONNECTED': 'bg-warning text-dark',
        'ERROR':        'bg-warning text-dark',
        'UNKNOWN':      'bg-secondary',
    }

    @app.template_global()
    def status_badge(status):
        return _STATUS_BADGE.get(status, 'bg-secondary')


def _run_migrations():
    """Add new columns to existing tables so upgrades don't require dropping the database."""
    from sqlalchemy import text
    with db.engine.connect() as conn:
        result = conn.execute(text("PRAGMA table_info(process_monitors)"))
        existing = {row[1] for row in result}
        for col, typedef in [
            ('start_command', 'TEXT'),
            ('stop_command',  'TEXT'),
            ('working_dir',   'TEXT'),
        ]:
            if col not in existing:
                conn.execute(text(f'ALTER TABLE process_monitors ADD COLUMN {col} TEXT'))
        conn.commit()


def _initialize_defaults():
    from .models import Admin, SystemConfig

    if Admin.query.count() == 0:
        admin = Admin(username='admin')
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()
        logging.getLogger(__name__).warning(
            '\n' + '='*60 +
            '\nDEFAULT ADMIN CREATED  username=admin  password=admin' +
            '\nChange this password immediately via Admin > Settings!' +
            '\n' + '='*60
        )

    defaults = [
        ('mail_server',            'localhost',          'SMTP server hostname'),
        ('mail_port',              '25',                 'SMTP server port'),
        ('mail_username',          '',                   'SMTP username (blank if not required)'),
        ('mail_password',          '',                   'SMTP password'),
        ('mail_use_tls',           'false',              'Use STARTTLS for SMTP (true/false)'),
        ('mail_use_ssl',           'false',              'Use SSL/SMTPS for SMTP (true/false)'),
        ('mail_sender',            'watchdog@localhost', 'From address for notification emails'),
        ('check_interval',         '10',                 'How often to check PVs (seconds)'),
        ('default_notify_interval','3600',               'Min seconds between repeated notifications'),
        ('site_name',              'EPICS PV Watchdog',  'Site name shown in page header'),
        ('epics_ca_addr_list',     '',                   'EPICS_CA_ADDR_LIST — space-separated CA broadcast/unicast addresses'),
        ('epics_ca_auto_addr_list','YES',                'EPICS_CA_AUTO_ADDR_LIST — YES or NO'),
    ]
    for key, value, desc in defaults:
        if not SystemConfig.query.filter_by(key=key).first():
            db.session.add(SystemConfig(key=key, value=value, description=desc))
    db.session.commit()


def apply_epics_env(app):
    """Read EPICS CA settings from DB and push to os.environ before first caget."""
    with app.app_context():
        from .models import SystemConfig
        addr = SystemConfig.query.filter_by(key='epics_ca_addr_list').first()
        auto = SystemConfig.query.filter_by(key='epics_ca_auto_addr_list').first()
        if addr and addr.value.strip():
            os.environ['EPICS_CA_ADDR_LIST'] = addr.value.strip()
        if auto and auto.value.strip():
            os.environ['EPICS_CA_AUTO_ADDR_LIST'] = auto.value.strip()
