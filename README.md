# Vendor Ops API

Django + Django REST Framework + PostgreSQL backend for The Good Flea Vendor Ops.
Full setup, architecture and deployment notes live in the frontend repository's README
(`vendor-ops-frontend/README.md`). Quick start:

```sh
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # then edit DATABASE_URL and DJANGO_SECRET_KEY
python manage.py migrate
python manage.py seed_dev         # demo users + vendors, DEBUG only
python manage.py runserver 8000
python manage.py test
```

Apps: `accounts` (users and roles), `vendors` (vendors, applications, intake events, notes,
activity timeline), `tasks` (tasks, business-hours scheduling, call logging), `imports` (CSV).
Business rules live in each app's `services.py`; views stay thin.
