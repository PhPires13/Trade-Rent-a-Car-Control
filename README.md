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
`SECRET_KEY`, `DEBUG=False`, `ALLOWED_HOSTS`, `DATABASE_URL` (Postgres do Neon ou Railway), `CSRF_TRUSTED_ORIGINS`
(o bloco "Produção" do `.env.example` traz os exemplos).

- **Serviço web**: usa o `Procfile` e o `railway.json` (sonda de saúde em `/saude/`, que responde "ok" sem login).
- **Serviço da rotina diária**: 2º serviço do mesmo repositório, com as mesmas variáveis, apontando em
  *Settings > Config-as-code* para `railway.cron.json` (`python manage.py rotina_diaria` às `5 3 * * *` UTC =
  00:05 de Brasília). Sem esse arquivo o serviço herda o `Procfile`, sobe o site e nunca termina — a rotina não roda.
- **Primeiro acesso**: depois do 1º deploy, `railway run python manage.py createsuperuser`.
- **Erros**: com `DEBUG=False` o erro 500 aparece no log do serviço (stderr), com o traceback.
