import os
from app import app, init

required = ('SECRET_KEY', 'ADMIN_PASSWORD')
missing = [key for key in required if not os.environ.get(key)]
if missing:
    raise RuntimeError('Missing required environment variables: ' + ', '.join(missing))
if len(os.environ['SECRET_KEY']) < 32:
    raise RuntimeError('SECRET_KEY must be at least 32 characters')
if len(os.environ['ADMIN_PASSWORD']) < 12:
    raise RuntimeError('ADMIN_PASSWORD must be at least 12 characters')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=True)
init()
