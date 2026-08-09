from datetime import datetime
from flask import Blueprint, render_template, request
from .. import db
from ..models import Recipient, CARRIERS

public_bp = Blueprint('public', __name__)


@public_bp.route('/register/<token>', methods=['GET', 'POST'])
def register(token):
    r = Recipient.query.filter_by(token=token).first_or_404()

    if r.confirmed:
        return render_template('register.html', already_confirmed=True, recipient=r)

    if r.is_expired:
        return render_template('register.html', expired=True)

    if request.method == 'POST':
        r.name      = request.form.get('name', '').strip() or None
        digits      = ''.join(c for c in request.form.get('phone', '') if c.isdigit())
        r.phone     = digits or None
        r.carrier   = request.form.get('carrier', '').strip() or None
        r.confirmed = True
        r.updated_at = datetime.utcnow()
        db.session.commit()
        return render_template('register.html', success=True, recipient=r, carriers=CARRIERS)

    return render_template('register.html', recipient=r, carriers=CARRIERS, token=token)
