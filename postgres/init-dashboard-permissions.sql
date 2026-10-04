CREATE TABLE IF NOT EXISTS dashboard_permissions (
    id integer NOT NULL,
    user_id bigint NOT NULL,
    entry_id bigint NOT NULL,
    access_level character varying(50) NOT NULL DEFAULT 'view',
    granted_by bigint,
    granted_at timestamp without time zone DEFAULT now()
);

CREATE SEQUENCE IF NOT EXISTS dashboard_permissions_id_seq
    AS integer START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1;

ALTER SEQUENCE dashboard_permissions_id_seq OWNED BY dashboard_permissions.id;

ALTER TABLE dashboard_permissions
    ALTER COLUMN id SET DEFAULT nextval('dashboard_permissions_id_seq'::regclass);

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

CREATE INDEX IF NOT EXISTS idx_dashboard_permissions_entry ON dashboard_permissions USING btree (entry_id);
CREATE INDEX IF NOT EXISTS idx_dashboard_permissions_user ON dashboard_permissions USING btree (user_id);
