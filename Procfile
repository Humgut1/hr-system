web: python -c "from migrate_db import run; run()" && gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120
