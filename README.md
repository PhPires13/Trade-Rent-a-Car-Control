# Trade Rent a Car — Sistema de Gestão de Frota

Sistema interno de gestão da frota de locação (motoristas de aplicativo): alocações, pagamentos semanais, multas, manutenção preventiva, KM mensal e desmobilização.

- **Especificação (PRD):** [docs.md](docs.md)
- **Planejamento técnico:** [docs-tecnico.md](docs-tecnico.md)
- **Progresso do desenvolvimento:** [ETAPAS.md](ETAPAS.md)

## Stack

Python 3.13 · Django 5.2 LTS · PostgreSQL (SQLite em dev) · HTMX + Alpine.js + Tailwind · uv · pytest · ruff

## Desenvolvimento local

```bash
# dependências (instale o uv antes: https://docs.astral.sh/uv/)
uv sync

# configuração
cp .env.example .env

# banco e usuários
uv run python manage.py migrate
uv run python manage.py createsuperuser

# rodar
uv run python manage.py runserver
```

Testes e qualidade:

```bash
uv run pytest
uv run ruff check .
uv run ruff format .
```

## Deploy (Railway)

O `Procfile` roda migrações + collectstatic + gunicorn. Variáveis necessárias no serviço:
`SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `DATABASE_URL` (Postgres do Neon ou Railway), `CSRF_TRUSTED_ORIGINS`.
