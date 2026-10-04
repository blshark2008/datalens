#!/bin/bash
set -eo pipefail

echo "  [post-init] Creating dashboard_permissions table..."

export PGPASSWORD="${POSTGRES_PASSWORD}"

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB_US}" <<'EOSQL'
-- Создаём таблицу, если её ещё нет
CREATE TABLE IF NOT EXISTS dashboard_permissions (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    entry_id bigint NOT NULL,
    access_level character varying(50) NOT NULL DEFAULT 'view',
    granted_by bigint,
    granted_at timestamp without time zone DEFAULT now()
);

-- Последовательность для id
CREATE SEQUENCE IF NOT EXISTS dashboard_permissions_id_seq
    AS integer START WITH 1 INCREMENT BY 1 NO MINIMIZE NO MAXVALUE CACHE 1;

ALTER SEQUENCE dashboard_permissions_id_seq OWNED BY dashboard_permissions.id;

ALTER TABLE dashboard_permissions
    ALTER COLUMN id SET DEFAULT nextval('dashboard_permissions_id_seq'::regclass);

-- Первичный ключ
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dashboard_permissions_pkey'
          AND conrelid = 'dashboard_permissions'::regclass
    ) THEN
        ALTER TABLE dashboard_permissions
            ADD CONSTRAINT dashboard_permissions_pkey PRIMARY KEY (id);
    END IF;
END $$;

-- Уникальное ограничение (user_id, entry_id)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'dashboard_permissions_user_id_entry_id_key'
          AND conrelid = 'dashboard_permissions'::regclass
    ) THEN
        ALTER TABLE dashboard_permissions
            ADD CONSTRAINT dashboard_permissions_user_id_entry_id_key UNIQUE (user_id, entry_id);
    END IF;
END $$;

-- Индексы
CREATE INDEX IF NOT EXISTS idx_dashboard_permissions_entry ON dashboard_permissions USING btree (entry_id);
CREATE INDEX IF NOT EXISTS idx_dashboard_permissions_user ON dashboard_permissions USING btree (user_id);

-- Если таблица существовала со старыми типами — приводим к актуальным
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dashboard_permissions'
          AND column_name = 'user_id'
          AND data_type <> 'bigint'
    ) THEN
        ALTER TABLE dashboard_permissions
            ALTER COLUMN user_id TYPE bigint USING user_id::bigint;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dashboard_permissions'
          AND column_name = 'entry_id'
          AND data_type <> 'bigint'
    ) THEN
        ALTER TABLE dashboard_permissions
            ALTER COLUMN entry_id TYPE bigint USING entry_id::bigint;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dashboard_permissions'
          AND column_name = 'granted_by'
          AND data_type <> 'bigint'
    ) THEN
        ALTER TABLE dashboard_permissions
            ALTER COLUMN granted_by TYPE bigint USING granted_by::bigint;
    END IF;
END $$;
EOSQL

echo "  [post-init] dashboard_permissions table ready."
