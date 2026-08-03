from . import login_manager
from .models import Admin


@login_manager.user_loader
def load_user(user_id):
    return Admin.query.get(int(user_id))
