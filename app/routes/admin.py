import json
import shlex
import subprocess
from datetime import datetime
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, abort)
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from .. import db, limiter
from ..models import (Admin, EmailList, PVMonitor, CompoundRule,
                      ProcessMonitor, NotificationLog, SystemConfig, ActionLog, Category,
                      Recipient, CARRIERS, get_category_colors)

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
@limiter.limit("20 per minute")
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

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

@admin_bp.route('/categories')
@login_required
def categories():
    items = Category.query.order_by(Category.name).all()
    return render_template('admin/categories.html', categories=items)


@admin_bp.route('/categories/add', methods=['GET', 'POST'])
@admin_bp.route('/categories/<int:cat_id>/edit', methods=['GET', 'POST'])
@login_required
def category_form(cat_id=None):
    cat = Category.query.get_or_404(cat_id) if cat_id else Category()
    errors = {}
    if request.method == 'POST':
        cat.name = request.form.get('name', '').strip()
        cat.description = request.form.get('description', '').strip() or None
        color = request.form.get('color', '').strip()
        cat.color = color if color and color.startswith('#') and len(color) == 7 else None
        if not cat.name:
            errors['name'] = 'Category name is required.'
        existing = Category.query.filter_by(name=cat.name).first()
        if existing and existing.id != cat_id:
            errors['name'] = 'A category with this name already exists.'
        if not errors:
            if not cat_id:
                db.session.add(cat)
            db.session.commit()
            flash(f'Category "{cat.name}" {"updated" if cat_id else "created"}.', 'success')
            return redirect(url_for('admin.categories'))
    return render_template('admin/category_form.html', cat=cat, cat_id=cat_id, errors=errors)


@admin_bp.route('/categories/<int:cat_id>/delete', methods=['POST'])
@login_required
def category_delete(cat_id):
    cat = Category.query.get_or_404(cat_id)
    # Unlink PVs and processes before deleting
    for pv in cat.pv_monitors:
        pv.category_id = None
    for pm in cat.process_monitors:
        pm.category_id = None
    db.session.delete(cat)
    db.session.commit()
    flash(f'Category "{cat.name}" deleted. Monitors moved to Uncategorized.', 'warning')
    return redirect(url_for('admin.categories'))


# ---------------------------------------------------------------------------
# PV monitors
# ---------------------------------------------------------------------------

@admin_bp.route('/pvs')
@login_required
def pvs():
    cat_filter = request.args.get('category', '')
    categories = Category.query.order_by(Category.name).all()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    q = PVMonitor.query.order_by(PVMonitor.pv_name)
    if cat_filter == '__none__':
        q = q.filter(PVMonitor.category_id == None)
    elif cat_filter:
        q = q.join(Category).filter(Category.name == cat_filter)
    items = q.all()
    return render_template('admin/pvs.html', pvs=items,
                           categories=categories, cat_filter=cat_filter,
                           email_lists=email_lists,
                           category_colors=get_category_colors())


@admin_bp.route('/pvs/add', methods=['GET', 'POST'])
@admin_bp.route('/pvs/<int:pv_id>/edit', methods=['GET', 'POST'])
@login_required
def pv_form(pv_id=None):
    pv = PVMonitor.query.get_or_404(pv_id) if pv_id else PVMonitor()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    categories = Category.query.order_by(Category.name).all()
    errors = {}

    if request.method == 'POST':
        pv.pv_name = request.form.get('pv_name', '').strip()
        pv.alias = request.form.get('alias', '').strip() or None
        pv.description = request.form.get('description', '').strip() or None
        pv.condition_op = request.form.get('condition_op', '>')
        pv.enabled = 'enabled' in request.form
        pv.auto_enable = 'auto_enable' in request.form
        pv.notify_flag = 'notify_flag' in request.form
        pv.notify_on_disconnect = 'notify_on_disconnect' in request.form
        cat_id = request.form.get('category_id', '')
        pv.category_id = int(cat_id) if cat_id else None

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
                           email_lists=email_lists, categories=categories,
                           condition_ops=CONDITION_OPS, errors=errors)


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
    email_lists = EmailList.query.order_by(EmailList.name).all()
    return render_template('admin/rules.html', rules=items, pv_map=pv_map,
                           email_lists=email_lists)


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
                    'check_interval', 'default_notify_interval', 'site_name', 'site_url',
                    'timezone', 'archiver_url',
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
            try:
                from ..watchdog import restart_watchdog
                from flask import current_app
                restart_watchdog(current_app._get_current_object(), new_interval)
            except Exception:
                logger.warning('Could not reschedule watchdog job; settings were still saved.')

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


# ---------------------------------------------------------------------------
# Process monitors
# ---------------------------------------------------------------------------

MATCH_TYPES = [
    ('name',    'Process name (partial, case-insensitive)'),
    ('cmdline', 'Command-line substring (full command including arguments)'),
    ('command', 'Shell command (exit code 0 = running, non-zero = stopped)'),
]


@admin_bp.route('/processes')
@login_required
def processes():
    cat_filter = request.args.get('category', '')
    categories = Category.query.order_by(Category.name).all()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    q = ProcessMonitor.query.order_by(ProcessMonitor.name)
    if cat_filter == '__none__':
        q = q.filter(ProcessMonitor.category_id == None)
    elif cat_filter:
        q = q.join(Category).filter(Category.name == cat_filter)
    items = q.all()
    recent_actions = (ActionLog.query
                      .order_by(ActionLog.timestamp.desc())
                      .limit(10).all())
    return render_template('admin/processes.html',
                           processes=items, recent_actions=recent_actions,
                           categories=categories, cat_filter=cat_filter,
                           email_lists=email_lists,
                           category_colors=get_category_colors())


@admin_bp.route('/bulk', methods=['POST'])
@login_required
def bulk_update():
    """Bulk-edit email list, category, or enabled state for multiple PVs/processes/rules."""
    item_type = request.form.get('type', '')
    ids = [int(i) for i in request.form.get('ids', '').split(',') if i.strip().isdigit()]

    if not ids:
        flash('No items selected.', 'warning')
        return redirect(request.referrer or url_for('admin.index'))

    def _parse(key):
        """Return (value_or_None, changed). '' = no change, '__none__' = set NULL."""
        v = request.form.get(key, '').strip()
        if v == '':
            return None, False
        if v == '__none__':
            return None, True
        return v, True

    cat_val,   change_cat     = _parse('category_id')
    email_val, change_email   = _parse('email_list_id')
    enabled_val, change_enabled = _parse('enabled')

    MODEL_MAP = {'pv': PVMonitor, 'process': ProcessMonitor, 'rule': CompoundRule}
    REDIRECT_MAP = {'pv': 'admin.pvs', 'process': 'admin.processes', 'rule': 'admin.rules'}

    model = MODEL_MAP.get(item_type)
    if not model:
        flash('Unknown item type.', 'danger')
        return redirect(url_for('admin.index'))

    items = model.query.filter(model.id.in_(ids)).all()
    for item in items:
        if change_cat and hasattr(item, 'category_id'):
            item.category_id = int(cat_val) if cat_val else None
        if change_email and hasattr(item, 'email_list_id'):
            item.email_list_id = int(email_val) if email_val else None
        if change_enabled:
            if enabled_val == 'auto':
                if hasattr(item, 'auto_enable'):
                    item.auto_enable = True
            else:
                item.enabled = (enabled_val == '1')
    db.session.commit()
    flash(f'Updated {len(items)} item(s).', 'success')
    return redirect(url_for(REDIRECT_MAP[item_type]))


@admin_bp.route('/processes/add', methods=['GET', 'POST'])
@admin_bp.route('/processes/<int:proc_id>/edit', methods=['GET', 'POST'])
@login_required
def process_form(proc_id=None):
    pm = ProcessMonitor.query.get_or_404(proc_id) if proc_id else ProcessMonitor()
    email_lists = EmailList.query.order_by(EmailList.name).all()
    categories = Category.query.order_by(Category.name).all()
    errors = {}

    if request.method == 'POST':
        pm.name = request.form.get('name', '').strip()
        pm.description = request.form.get('description', '').strip() or None
        pm.match_type = request.form.get('match_type', 'name')
        pm.match_value = request.form.get('match_value', '').strip()
        pm.start_command = request.form.get('start_command', '').strip() or None
        pm.stop_command = request.form.get('stop_command', '').strip() or None
        pm.working_dir = request.form.get('working_dir', '').strip() or None
        pm.notify_flag = 'notify_flag' in request.form
        pm.enabled = 'enabled' in request.form
        cat_id = request.form.get('category_id', '')
        pm.category_id = int(cat_id) if cat_id else None

        ni = request.form.get('notify_interval', '3600').strip()
        try:
            pm.notify_interval = int(ni)
        except ValueError:
            pm.notify_interval = 3600

        elist_id = request.form.get('email_list_id', '')
        pm.email_list_id = int(elist_id) if elist_id else None

        if not pm.name:
            errors['name'] = 'Display name is required.'
        if not pm.match_value:
            errors['match_value'] = 'Match value is required.'

        if not errors:
            if not proc_id:
                db.session.add(pm)
            db.session.commit()
            flash(f'Process monitor "{pm.name}" {"updated" if proc_id else "added"}.', 'success')
            return redirect(url_for('admin.processes'))

    return render_template('admin/process_form.html', pm=pm, proc_id=proc_id,
                           email_lists=email_lists, categories=categories,
                           match_types=MATCH_TYPES, errors=errors)


@admin_bp.route('/processes/<int:proc_id>/delete', methods=['POST'])
@login_required
def process_delete(proc_id):
    pm = ProcessMonitor.query.get_or_404(proc_id)
    name = pm.name
    db.session.delete(pm)
    db.session.commit()
    flash(f'Process monitor "{name}" deleted.', 'warning')
    return redirect(url_for('admin.processes'))


@admin_bp.route('/processes/<int:proc_id>/toggle', methods=['POST'])
@login_required
def process_toggle(proc_id):
    pm = ProcessMonitor.query.get_or_404(proc_id)
    pm.enabled = not pm.enabled
    db.session.commit()
    flash(f'Process monitor "{pm.name}" {"enabled" if pm.enabled else "disabled"}.', 'info')
    return redirect(url_for('admin.processes'))


# ---------------------------------------------------------------------------
# Process control helpers
# ---------------------------------------------------------------------------

def _proc_start(pm):
    if not pm.start_command:
        return False, "No start command configured."
    try:
        cmd = shlex.split(pm.start_command)
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                      start_new_session=True)
        if pm.working_dir:
            kwargs['cwd'] = pm.working_dir
        proc = subprocess.Popen(cmd, **kwargs)
        return True, f"Started (PID {proc.pid})"
    except FileNotFoundError as exc:
        return False, f"Command not found: {exc}"
    except Exception as exc:
        return False, str(exc)


def _proc_stop(pm):
    if pm.stop_command:
        try:
            cmd = shlex.split(pm.stop_command)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            out = (result.stdout + result.stderr).strip()
            return result.returncode == 0, out or (
                "Stop command succeeded." if result.returncode == 0
                else f"Exit {result.returncode}.")
        except subprocess.TimeoutExpired:
            return False, "Stop command timed out after 15 s."
        except Exception as exc:
            return False, str(exc)
    elif pm.pid:
        try:
            import psutil
            p = psutil.Process(pm.pid)
            p.terminate()
            try:
                p.wait(timeout=5)
                return True, f"PID {pm.pid} terminated (SIGTERM)."
            except psutil.TimeoutExpired:
                p.kill()
                return True, f"PID {pm.pid} killed (SIGKILL after SIGTERM timeout)."
        except psutil.NoSuchProcess:
            return False, f"No process with PID {pm.pid}."
        except Exception as exc:
            return False, str(exc)
    else:
        return False, "No stop command and no known PID."


def _proc_kill(pm):
    if not pm.pid:
        return False, "No known PID."
    try:
        import psutil
        psutil.Process(pm.pid).kill()
        return True, f"PID {pm.pid} killed (SIGKILL)."
    except psutil.NoSuchProcess:
        return False, f"No process with PID {pm.pid}."
    except Exception as exc:
        return False, str(exc)


def _log_action(pm, action, command, success, output):
    db.session.add(ActionLog(
        admin_username=current_user.username,
        action=action,
        process_id=pm.id,
        process_name=pm.name,
        command=command or '',
        success=success,
        output=output,
    ))
    db.session.commit()


# ---------------------------------------------------------------------------
# Process start / stop / kill routes
# ---------------------------------------------------------------------------

@admin_bp.route('/processes/<int:proc_id>/start', methods=['POST'])
@login_required
def process_start(proc_id):
    pm = ProcessMonitor.query.get_or_404(proc_id)
    success, output = _proc_start(pm)
    _log_action(pm, 'START', pm.start_command, success, output)
    flash(f'Start {"succeeded" if success else "failed"}: {output}',
          'success' if success else 'danger')
    next_url = request.form.get('next') or url_for('admin.processes')
    return redirect(next_url)


@admin_bp.route('/processes/<int:proc_id>/stop', methods=['POST'])
@login_required
def process_stop(proc_id):
    pm = ProcessMonitor.query.get_or_404(proc_id)
    success, output = _proc_stop(pm)
    _log_action(pm, 'STOP', pm.stop_command or f'kill {pm.pid}', success, output)
    flash(f'Stop {"succeeded" if success else "failed"}: {output}',
          'success' if success else 'danger')
    next_url = request.form.get('next') or url_for('admin.processes')
    return redirect(next_url)


@admin_bp.route('/processes/<int:proc_id>/kill', methods=['POST'])
@login_required
def process_kill(proc_id):
    pm = ProcessMonitor.query.get_or_404(proc_id)
    success, output = _proc_kill(pm)
    _log_action(pm, 'KILL', f'kill -9 {pm.pid}', success, output)
    flash(f'Kill {"succeeded" if success else "failed"}: {output}',
          'success' if success else 'danger')
    next_url = request.form.get('next') or url_for('admin.processes')
    return redirect(next_url)


# ---------------------------------------------------------------------------
# Action log
# ---------------------------------------------------------------------------

@admin_bp.route('/processes/action-log')
@login_required
def action_logs():
    page = request.args.get('page', 1, type=int)
    pagination = (ActionLog.query
                  .order_by(ActionLog.timestamp.desc())
                  .paginate(page=page, per_page=50, error_out=False))
    return render_template('admin/action_logs.html',
                           logs=pagination.items, pagination=pagination)


# ---------------------------------------------------------------------------
# Recipients (self-registration via invitation)
# ---------------------------------------------------------------------------

@admin_bp.route('/recipients')
@login_required
def recipients():
    email_lists  = EmailList.query.order_by(EmailList.name).all()
    all_recips   = (Recipient.query
                   .order_by(Recipient.email_list_id, Recipient.email).all())
    return render_template('admin/recipients.html',
                           recipients=all_recips, email_lists=email_lists,
                           carriers=CARRIERS)


@admin_bp.route('/recipients/invite', methods=['POST'])
@login_required
def recipients_invite():
    import re, secrets
    from datetime import timedelta

    list_id   = request.form.get('email_list_id', type=int)
    emails_raw = request.form.get('emails', '')

    if not list_id:
        flash('Please select an email list.', 'warning')
        return redirect(url_for('admin.recipients'))

    EmailList.query.get_or_404(list_id)

    addresses = list({e.strip().lower()
                      for e in re.split(r'[,\n\r\s]+', emails_raw) if '@' in e})
    if not addresses:
        flash('No valid email addresses entered.', 'warning')
        return redirect(url_for('admin.recipients'))

    cfg       = {r.key: r.value for r in SystemConfig.query.all()}
    site_url  = (cfg.get('site_url') or '').rstrip('/')
    site_name = cfg.get('site_name') or 'EPICS PV Watchdog'

    sent = skipped = 0
    for addr in addresses:
        existing = Recipient.query.filter_by(email=addr, email_list_id=list_id).first()
        if existing and existing.confirmed:
            skipped += 1
            continue
        token  = secrets.token_urlsafe(32)
        expiry = datetime.utcnow() + timedelta(days=7)
        if existing:
            existing.token        = token
            existing.token_expiry = expiry
        else:
            db.session.add(Recipient(email=addr, email_list_id=list_id,
                                     token=token, token_expiry=expiry))
        reg_url = f'{site_url}/register/{token}' if site_url else f'/register/{token}'
        subject = f'Invitation: Register for {site_name} notifications'
        body    = (
            f'You have been invited to receive alarm notifications from {site_name}.\n\n'
            f'Please register your contact details (name, phone, and carrier) '
            f'by visiting the link below:\n\n'
            f'  {reg_url}\n\n'
            f'This link expires in 7 days. Phone and carrier are optional but '
            f'enable SMS text alerts in addition to email.\n\n'
            f'If you did not expect this invitation, you can safely ignore this email.'
        )
        from ..email_utils import send_notification_email  # noqa: local import avoids circular
        send_notification_email([addr], subject, body, cfg)
        sent += 1

    db.session.commit()
    flash(f'Invitations sent: {sent}. Already registered (skipped): {skipped}.', 'success')
    return redirect(url_for('admin.recipients'))


@admin_bp.route('/recipients/<int:rec_id>/resend', methods=['POST'])
@login_required
def recipient_resend(rec_id):
    import secrets
    from datetime import timedelta
    r = Recipient.query.get_or_404(rec_id)
    if r.confirmed:
        flash(f'{r.email} has already registered.', 'info')
        return redirect(url_for('admin.recipients'))
    r.token        = secrets.token_urlsafe(32)
    r.token_expiry = datetime.utcnow() + timedelta(days=7)
    db.session.commit()
    cfg       = {row.key: row.value for row in SystemConfig.query.all()}
    site_url  = (cfg.get('site_url') or '').rstrip('/')
    site_name = cfg.get('site_name') or 'EPICS PV Watchdog'
    reg_url   = f'{site_url}/register/{r.token}' if site_url else f'/register/{r.token}'
    subject   = f'Invitation: Register for {site_name} notifications'
    body      = (
        f'You have been invited to receive alarm notifications from {site_name}.\n\n'
        f'Please register your contact details by visiting:\n\n'
        f'  {reg_url}\n\n'
        f'This link expires in 7 days.'
    )
    from ..email_utils import send_notification_email
    send_notification_email([r.email], subject, body, cfg)
    flash(f'Invitation resent to {r.email}.', 'success')
    return redirect(url_for('admin.recipients'))


@admin_bp.route('/recipients/<int:rec_id>/delete', methods=['POST'])
@login_required
def recipient_delete(rec_id):
    r = Recipient.query.get_or_404(rec_id)
    db.session.delete(r)
    db.session.commit()
    flash('Recipient removed.', 'success')
    return redirect(url_for('admin.recipients'))
