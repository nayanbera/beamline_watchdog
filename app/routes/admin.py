import json
from datetime import datetime
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from .. import db
from ..models import (Admin, EmailList, PVMonitor, CompoundRule,
                      NotificationLog, SystemConfig)

admin_bp = Blueprint('admin', __name__)

CONDITION_OPS = [
    ('>', 'value > threshold  (alarm when above)'),
    ('<', 'value < threshold  (alarm when below)'),
    ('>=', 'value ≥ threshold'),
    ('<=', 'value ≤ threshold'),
    ('==', 'value = threshold  (exact match)'),
    ('!=', 'value ≠ threshold'),
    ('in_range', 'in range [v1, v2]'),
    ('out_range', 'outside range [v1, v2]'),
]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin.index'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            login_user(admin)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('admin.index'))
        error = 'Invalid username or password.'
    return render_template('login.html', error=error)


@admin_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('dashboard.index'))


# ---------------------------------------------------------------------------
# Admin home
# ---------------------------------------------------------------------------

@admin_bp.route('/')
@login_required
def index():
    pv_count = PVMonitor.query.count()
    rule_count = CompoundRule.query.count()
    list_count = EmailList.query.count()
    alarm_pvs = PVMonitor.query.filter_by(status='ALARM').count()
    recent_logs = (NotificationLog.query
                   .order_by(NotificationLog.timestamp.desc())
                   .limit(10).all())
    return render_template('admin/index.html',
                           pv_count=pv_count, rule_count=rule_count,
                           list_count=list_count, alarm_pvs=alarm_pvs,
                           recent_logs=recent_logs)


# ---------------------------------------------------------------------------
# PV monitors
# ---------------------------------------------------------------------------

@admin_bp.route('/pvs')
@login_required
def pvs():
    items = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    return render_template('admin/pvs.html', pvs=items)


@admin_bp.route('/pvs/add', methods=['GET', 'POST'])
@admin_bp.route('/pvs/<int:pv_id>/edit', methods=['GET', 'POST'])
@login_required
def pv_form(pv_id=None):
    pv = PVMonitor.query.get_or_404(pv_id) if pv_id else PVMonitor()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    errors = {}

    if request.method == 'POST':
        pv.pv_name = request.form.get('pv_name', '').strip()
        pv.alias = request.form.get('alias', '').strip() or None
        pv.description = request.form.get('description', '').strip() or None
        pv.condition_op = request.form.get('condition_op', '>')
        pv.enabled = 'enabled' in request.form
        pv.notify_flag = 'notify_flag' in request.form

        try:
            pv.condition_value = float(request.form.get('condition_value', ''))
        except ValueError:
            errors['condition_value'] = 'Must be a number.'

        v2 = request.form.get('condition_value2', '').strip()
        if v2:
            try:
                pv.condition_value2 = float(v2)
            except ValueError:
                errors['condition_value2'] = 'Must be a number.'
        else:
            pv.condition_value2 = None

        ni = request.form.get('notify_interval', '3600').strip()
        try:
            pv.notify_interval = int(ni)
        except ValueError:
            pv.notify_interval = 3600

        elist_id = request.form.get('email_list_id', '')
        pv.email_list_id = int(elist_id) if elist_id else None

        if not pv.pv_name:
            errors['pv_name'] = 'PV name is required.'

        # Uniqueness check (excluding self on edit)
        existing = PVMonitor.query.filter_by(pv_name=pv.pv_name).first()
        if existing and existing.id != pv_id:
            errors['pv_name'] = 'A PV with this name already exists.'

        if not errors:
            if not pv_id:
                db.session.add(pv)
            db.session.commit()
            flash(f'PV "{pv.pv_name}" {"updated" if pv_id else "added"} successfully.', 'success')
            return redirect(url_for('admin.pvs'))

    return render_template('admin/pv_form.html', pv=pv, pv_id=pv_id,
                           email_lists=email_lists, condition_ops=CONDITION_OPS,
                           errors=errors)


@admin_bp.route('/pvs/<int:pv_id>/delete', methods=['POST'])
@login_required
def pv_delete(pv_id):
    pv = PVMonitor.query.get_or_404(pv_id)
    name = pv.pv_name
    db.session.delete(pv)
    db.session.commit()
    flash(f'PV "{name}" deleted.', 'warning')
    return redirect(url_for('admin.pvs'))


@admin_bp.route('/pvs/<int:pv_id>/toggle', methods=['POST'])
@login_required
def pv_toggle(pv_id):
    pv = PVMonitor.query.get_or_404(pv_id)
    pv.enabled = not pv.enabled
    db.session.commit()
    state = 'enabled' if pv.enabled else 'disabled'
    flash(f'PV "{pv.display_name}" {state}.', 'info')
    return redirect(url_for('admin.pvs'))


# ---------------------------------------------------------------------------
# Email lists
# ---------------------------------------------------------------------------

@admin_bp.route('/email-lists')
@login_required
def email_lists():
    items = EmailList.query.order_by(EmailList.name).all()
    return render_template('admin/email_lists.html', lists=items)


@admin_bp.route('/email-lists/add', methods=['GET', 'POST'])
@admin_bp.route('/email-lists/<int:list_id>/edit', methods=['GET', 'POST'])
@login_required
def email_list_form(list_id=None):
    elist = EmailList.query.get_or_404(list_id) if list_id else EmailList()
    errors = {}

    if request.method == 'POST':
        elist.name = request.form.get('name', '').strip()
        elist.description = request.form.get('description', '').strip() or None

        raw = request.form.get('emails', '')
        parsed = []
        for line in raw.replace(',', '\n').splitlines():
            addr = line.strip()
            if addr:
                parsed.append(addr)
        elist.emails = parsed

        if not elist.name:
            errors['name'] = 'List name is required.'

        existing = EmailList.query.filter_by(name=elist.name).first()
        if existing and existing.id != list_id:
            errors['name'] = 'A list with this name already exists.'

        if not errors:
            if not list_id:
                db.session.add(elist)
            db.session.commit()
            flash(f'Email list "{elist.name}" {"updated" if list_id else "created"}.', 'success')
            return redirect(url_for('admin.email_lists'))

    emails_text = '\n'.join(elist.emails) if elist.emails else ''
    return render_template('admin/email_list_form.html', elist=elist,
                           list_id=list_id, emails_text=emails_text, errors=errors)


@admin_bp.route('/email-lists/<int:list_id>/delete', methods=['POST'])
@login_required
def email_list_delete(list_id):
    elist = EmailList.query.get_or_404(list_id)
    name = elist.name
    db.session.delete(elist)
    db.session.commit()
    flash(f'Email list "{name}" deleted.', 'warning')
    return redirect(url_for('admin.email_lists'))


# ---------------------------------------------------------------------------
# Compound rules
# ---------------------------------------------------------------------------

@admin_bp.route('/rules')
@login_required
def rules():
    items = CompoundRule.query.order_by(CompoundRule.name).all()
    all_pvs = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    pv_map = {p.id: p.display_name for p in all_pvs}
    return render_template('admin/rules.html', rules=items, pv_map=pv_map)


@admin_bp.route('/rules/add', methods=['GET', 'POST'])
@admin_bp.route('/rules/<int:rule_id>/edit', methods=['GET', 'POST'])
@login_required
def rule_form(rule_id=None):
    rule = CompoundRule.query.get_or_404(rule_id) if rule_id else CompoundRule()
    all_pvs = PVMonitor.query.order_by(PVMonitor.pv_name).all()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    errors = {}

    if request.method == 'POST':
        rule.name = request.form.get('name', '').strip()
        rule.description = request.form.get('description', '').strip() or None
        rule.logic_op = request.form.get('logic_op', 'AND')
        rule.enabled = 'enabled' in request.form
        rule.notify_flag = 'notify_flag' in request.form

        ni = request.form.get('notify_interval', '3600').strip()
        try:
            rule.notify_interval = int(ni)
        except ValueError:
            rule.notify_interval = 3600

        elist_id = request.form.get('email_list_id', '')
        rule.email_list_id = int(elist_id) if elist_id else None

        conditions_json = request.form.get('conditions_json', '[]')
        try:
            conditions = json.loads(conditions_json)
            if not isinstance(conditions, list):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            conditions = []
            errors['conditions'] = 'Invalid conditions data.'

        if not rule.name:
            errors['name'] = 'Rule name is required.'
        if not conditions:
            errors['conditions'] = errors.get('conditions', 'At least one condition is required.')

        existing = CompoundRule.query.filter_by(name=rule.name).first()
        if existing and existing.id != rule_id:
            errors['name'] = 'A rule with this name already exists.'

        if not errors:
            rule.set_expression({'logic': rule.logic_op, 'conditions': conditions})
            if not rule_id:
                db.session.add(rule)
            db.session.commit()
            flash(f'Rule "{rule.name}" {"updated" if rule_id else "created"}.', 'success')
            return redirect(url_for('admin.rules'))

    existing_conditions = rule.get_expression().get('conditions', []) if rule_id else []
    return render_template('admin/rule_form.html', rule=rule, rule_id=rule_id,
                           all_pvs=all_pvs, email_lists=email_lists,
                           existing_conditions=existing_conditions, errors=errors)


@admin_bp.route('/rules/<int:rule_id>/delete', methods=['POST'])
@login_required
def rule_delete(rule_id):
    rule = CompoundRule.query.get_or_404(rule_id)
    name = rule.name
    db.session.delete(rule)
    db.session.commit()
    flash(f'Rule "{name}" deleted.', 'warning')
    return redirect(url_for('admin.rules'))


@admin_bp.route('/rules/<int:rule_id>/toggle', methods=['POST'])
@login_required
def rule_toggle(rule_id):
    rule = CompoundRule.query.get_or_404(rule_id)
    rule.enabled = not rule.enabled
    db.session.commit()
    flash(f'Rule "{rule.name}" {"enabled" if rule.enabled else "disabled"}.', 'info')
    return redirect(url_for('admin.rules'))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    config_rows = SystemConfig.query.all()
    cfg = {r.key: r for r in config_rows}
    errors = {}
    success = False

    if request.method == 'POST':
        action = request.form.get('action', 'settings')

        if action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            confirm_pw = request.form.get('confirm_password', '')

            if not current_user.check_password(current_pw):
                errors['current_password'] = 'Current password is incorrect.'
            elif len(new_pw) < 6:
                errors['new_password'] = 'Password must be at least 6 characters.'
            elif new_pw != confirm_pw:
                errors['confirm_password'] = 'Passwords do not match.'
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash('Password changed successfully.', 'success')
                return redirect(url_for('admin.settings'))

        else:
            keys = ['mail_server', 'mail_port', 'mail_username', 'mail_password',
                    'mail_use_tls', 'mail_use_ssl', 'mail_sender',
                    'check_interval', 'default_notify_interval', 'site_name',
                    'epics_ca_addr_list', 'epics_ca_auto_addr_list']
            for key in keys:
                if key in ('mail_use_tls', 'mail_use_ssl'):
                    val = 'true' if request.form.get(key) else 'false'
                else:
                    val = request.form.get(key, '').strip()
                row = SystemConfig.query.filter_by(key=key).first()
                if row:
                    row.value = val
                else:
                    db.session.add(SystemConfig(key=key, value=val))
            db.session.commit()

            # Apply EPICS env vars immediately so next watchdog tick uses them
            import os
            addr = request.form.get('epics_ca_addr_list', '').strip()
            auto = request.form.get('epics_ca_auto_addr_list', '').strip()
            if addr:
                os.environ['EPICS_CA_ADDR_LIST'] = addr
            if auto:
                os.environ['EPICS_CA_AUTO_ADDR_LIST'] = auto

            # Reschedule watchdog if interval changed
            new_interval = int(request.form.get('check_interval', 10) or 10)
            from ..watchdog import restart_watchdog
            from flask import current_app
            restart_watchdog(current_app._get_current_object(), new_interval)

            flash('Settings saved. EPICS CA settings take effect on the next watchdog tick.', 'success')
            return redirect(url_for('admin.settings'))

    config_rows = SystemConfig.query.all()
    cfg = {r.key: r.value for r in config_rows}
    return render_template('admin/settings.html', cfg=cfg, errors=errors)


# ---------------------------------------------------------------------------
# Notification log
# ---------------------------------------------------------------------------

@admin_bp.route('/logs')
@login_required
def logs():
    page = request.args.get('page', 1, type=int)
    source_filter = request.args.get('source', '')
    status_filter = request.args.get('status', '')

    query = NotificationLog.query.order_by(NotificationLog.timestamp.desc())
    if source_filter:
        query = query.filter_by(source_type=source_filter)
    if status_filter:
        query = query.filter_by(event_status=status_filter)

    pagination = query.paginate(page=page, per_page=50, error_out=False)
    return render_template('admin/logs.html', logs=pagination.items,
                           pagination=pagination,
                           source_filter=source_filter,
                           status_filter=status_filter)


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------

@admin_bp.route('/users')
@login_required
def users():
    all_users = Admin.query.order_by(Admin.username).all()
    return render_template('admin/users.html', users=all_users)


@admin_bp.route('/users/add', methods=['GET', 'POST'])
@admin_bp.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
def user_form(user_id=None):
    user = Admin.query.get_or_404(user_id) if user_id else Admin()
    errors = {}

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        new_pw = request.form.get('password', '')
        confirm_pw = request.form.get('confirm_password', '')

        if not username:
            errors['username'] = 'Username is required.'

        existing = Admin.query.filter_by(username=username).first()
        if existing and existing.id != user_id:
            errors['username'] = 'That username is already taken.'

        if not user_id:
            # New user — password required
            if not new_pw:
                errors['password'] = 'Password is required.'
            elif len(new_pw) < 6:
                errors['password'] = 'Password must be at least 6 characters.'
            elif new_pw != confirm_pw:
                errors['confirm_password'] = 'Passwords do not match.'
        else:
            # Editing — password optional (blank = keep current)
            if new_pw:
                if len(new_pw) < 6:
                    errors['password'] = 'Password must be at least 6 characters.'
                elif new_pw != confirm_pw:
                    errors['confirm_password'] = 'Passwords do not match.'

        if not errors:
            user.username = username
            if new_pw:
                user.set_password(new_pw)
            if not user_id:
                db.session.add(user)
            db.session.commit()
            flash(f'User "{user.username}" {"updated" if user_id else "created"}.', 'success')
            return redirect(url_for('admin.users'))

    return render_template('admin/user_form.html', user=user, user_id=user_id, errors=errors)


@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@login_required
def user_delete(user_id):
    if user_id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin.users'))
    if Admin.query.count() <= 1:
        flash('Cannot delete the last admin account.', 'danger')
        return redirect(url_for('admin.users'))
    user = Admin.query.get_or_404(user_id)
    name = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{name}" deleted.', 'warning')
    return redirect(url_for('admin.users'))
