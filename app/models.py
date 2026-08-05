import json
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from . import db


class Admin(UserMixin, db.Model):
    __tablename__ = 'admins'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class EmailList(db.Model):
    __tablename__ = 'email_lists'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(500))
    _emails = db.Column('emails', db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def emails(self):
        try:
            return json.loads(self._emails or '[]')
        except (ValueError, TypeError):
            return []

    @emails.setter
    def emails(self, value):
        self._emails = json.dumps(value if value else [])

    def get_emails(self):
        return self.emails

    @property
    def email_count(self):
        return len(self.emails)


class PVMonitor(db.Model):
    __tablename__ = 'pv_monitors'
    id = db.Column(db.Integer, primary_key=True)
    pv_name = db.Column(db.String(255), unique=True, nullable=False)
    alias = db.Column(db.String(200))
    description = db.Column(db.String(500))
    # Alarm condition: value <op> condition_value [and condition_value2]
    # condition is TRUE → alarm state
    condition_op = db.Column(db.String(20), nullable=False)
    condition_value = db.Column(db.Float, nullable=False)
    condition_value2 = db.Column(db.Float)           # used for in_range / out_range
    notify_flag = db.Column(db.Boolean, default=True)
    notify_on_disconnect = db.Column(db.Boolean, default=False)
    email_list_id = db.Column(db.Integer, db.ForeignKey('email_lists.id'), nullable=True)
    email_list = db.relationship('EmailList', backref='pv_monitors', foreign_keys=[email_list_id])
    enabled = db.Column(db.Boolean, default=True)
    notify_interval = db.Column(db.Integer, default=3600)  # secs between repeat alerts
    # Runtime state — updated by watchdog
    current_value = db.Column(db.Float)
    current_value_str = db.Column(db.String(100))
    status = db.Column(db.String(20), default='UNKNOWN')
    last_checked = db.Column(db.DateTime)
    last_alarm = db.Column(db.DateTime)
    last_notified = db.Column(db.DateTime)
    last_notified_disconnect = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def display_name(self):
        return self.alias or self.pv_name

    @property
    def condition_str(self):
        labels = {
            '>': '>', '<': '<', '>=': '≥', '<=': '≤',
            '==': '=', '!=': '≠',
            'in_range': 'in range', 'out_range': 'out of range',
        }
        op = labels.get(self.condition_op, self.condition_op)
        if self.condition_op in ('in_range', 'out_range') and self.condition_value2 is not None:
            return f"value {op} [{self.condition_value}, {self.condition_value2}]"
        return f"value {op} {self.condition_value}"


class CompoundRule(db.Model):
    """
    A boolean combination of individual PV alarm states.
    expression (JSON): {"logic": "AND"|"OR",
                        "conditions": [{"pv_id": N, "expected_status": "ALARM"|"OK"}, ...]}
    The rule is in ALARM when the combined boolean expression evaluates to True.
    """
    __tablename__ = 'compound_rules'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    description = db.Column(db.String(500))
    logic_op = db.Column(db.String(10), default='AND')  # AND | OR
    expression = db.Column(db.Text, default='{}')
    notify_flag = db.Column(db.Boolean, default=True)
    email_list_id = db.Column(db.Integer, db.ForeignKey('email_lists.id'), nullable=True)
    email_list = db.relationship('EmailList', backref='compound_rules', foreign_keys=[email_list_id])
    enabled = db.Column(db.Boolean, default=True)
    notify_interval = db.Column(db.Integer, default=3600)
    # Runtime state
    status = db.Column(db.String(20), default='UNKNOWN')
    last_checked = db.Column(db.DateTime)
    last_alarm = db.Column(db.DateTime)
    last_notified = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_expression(self):
        try:
            return json.loads(self.expression or '{}')
        except (ValueError, TypeError):
            return {'logic': self.logic_op, 'conditions': []}

    def set_expression(self, data):
        self.expression = json.dumps(data)
        self.logic_op = data.get('logic', 'AND')


class NotificationLog(db.Model):
    __tablename__ = 'notification_logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    source_type = db.Column(db.String(10))   # 'PV' or 'RULE'
    source_id = db.Column(db.Integer)
    source_name = db.Column(db.String(255))
    event_status = db.Column(db.String(20))  # 'ALARM' or 'RECOVERED'
    message = db.Column(db.Text)
    _recipients = db.Column('recipients', db.Text, default='[]')
    success = db.Column(db.Boolean)
    error_message = db.Column(db.Text)

    @property
    def recipients(self):
        try:
            return json.loads(self._recipients or '[]')
        except (ValueError, TypeError):
            return []

    @recipients.setter
    def recipients(self, value):
        self._recipients = json.dumps(value if value else [])


class ProcessMonitor(db.Model):
    """
    Monitor a system process by name or command-line substring.
    Alarm = process is NOT running.
    Optional start_command / stop_command allow admins to control the process from the UI.
    """
    __tablename__ = 'process_monitors'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)          # display name
    description = db.Column(db.String(500))
    # match_type: 'name'    — partial, case-insensitive match on process name
    #             'cmdline' — substring match on full command-line string
    match_type = db.Column(db.String(20), default='name')
    match_value = db.Column(db.String(500), nullable=False)   # string to search for
    notify_flag = db.Column(db.Boolean, default=True)
    email_list_id = db.Column(db.Integer, db.ForeignKey('email_lists.id'), nullable=True)
    email_list = db.relationship('EmailList', backref='process_monitors',
                                 foreign_keys=[email_list_id])
    enabled = db.Column(db.Boolean, default=True)
    notify_interval = db.Column(db.Integer, default=3600)
    # Optional control commands (blank = not configurable from UI)
    start_command = db.Column(db.String(1000))  # shell command to start this process
    stop_command = db.Column(db.String(1000))   # shell command for graceful stop; blank = SIGTERM by PID
    working_dir = db.Column(db.String(500))     # working directory for start_command
    # Runtime state — updated by watchdog
    status = db.Column(db.String(20), default='UNKNOWN')  # RUNNING, STOPPED, UNKNOWN
    pid = db.Column(db.Integer)
    last_checked = db.Column(db.DateTime)
    last_stopped = db.Column(db.DateTime)
    last_notified = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ActionLog(db.Model):
    """Audit log for admin-initiated process start/stop/kill actions."""
    __tablename__ = 'action_logs'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    admin_username = db.Column(db.String(80), nullable=False)
    action = db.Column(db.String(10), nullable=False)   # START, STOP, KILL
    process_id = db.Column(db.Integer)
    process_name = db.Column(db.String(200))
    command = db.Column(db.String(1000))
    success = db.Column(db.Boolean)
    output = db.Column(db.Text)


class SystemConfig(db.Model):
    __tablename__ = 'system_config'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text, default='')
    description = db.Column(db.String(500))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
