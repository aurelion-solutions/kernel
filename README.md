Launch application
```
uv run uvicorn main:app --reload \
  --log-level debug \
  --access-log
```

Create migrations
```
# model should be mentioned in src/models/__init__.py
uv run alembic revision --autogenerate -m "add <some model>"

uv run alembic upgrade head
```
