#!/bin/bash
set -eo pipefail

PSQL="psql -U pg-user -d pg-us-db -v ON_ERROR_STOP=1"

echo "=== post-init: Начало инициализации ==="

echo "=== Заполняем published_id для connections ==="
$PSQL -c "UPDATE entries SET published_id = saved_id WHERE scope = 'connection' AND published_id IS NULL;"

echo "=== Создаём базу database_cardio ==="
$PSQL -c "SELECT 1 FROM pg_database WHERE datname = 'database_cardio'" | grep -q 1 || \
  psql -U pg-user -d postgres -c "CREATE DATABASE database_cardio;"

echo "=== Создаём таблицы в database_cardio ==="
psql -U pg-user -d database_cardio -v ON_ERROR_STOP=1 << 'SQL'
CREATE TABLE IF NOT EXISTS departments (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS metrics (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT,
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan_metrics (
    id SERIAL PRIMARY KEY,
    department_id INTEGER REFERENCES departments(id),
    metric_id INTEGER REFERENCES metrics(id),
    plan_value NUMERIC,
    fact_value NUMERIC,
    report_date DATE NOT NULL,
    comment TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

GRANT ALL ON ALL TABLES IN SCHEMA public TO pg-user;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO pg-user;
SQL

echo "=== Проверка published_id ==="
$PSQL -c "SELECT entry_id, name, published_id, saved_id FROM entries WHERE scope = 'connection';"

echo "=== post-init: Инициализация завершена ==="
