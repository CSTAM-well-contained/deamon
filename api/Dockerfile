# Control plane image: FastAPI app + its tests.
# Responsible for: installing dependencies (from pyproject.toml) and starting uvicorn.
# NOT responsible for: Postgres or gateways (separate containers in docker-compose.yml).
# Serves all criteria.

FROM python:3.12-slim

# haproxy is only used by tests/test_config_render.py to check the template with `haproxy -c`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends haproxy \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
# Install dependencies first (cached layer), read straight from pyproject.toml.
COPY pyproject.toml .
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['test']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

COPY app ./app
COPY tests ./tests

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
