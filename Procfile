web: python manage.py migrate --no-input && python manage.py collectstatic --no-input && gunicorn config.wsgi --workers ${WEB_CONCURRENCY:-3} --timeout 120 --log-file -
