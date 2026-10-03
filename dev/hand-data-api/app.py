"""
Hand Data API — сервис для работы с ручными данными (таблицы с префиксом hand_).
Работает напрямую с PostgreSQL DataLens.
"""
import os
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.sql import SQL, Identifier, Literal
from datetime import date, datetime
from cryptography.fernet import Fernet



app = Flask(__name__)
CORS(app)

# DB_* — для создания hand-таблиц (database_cardio):
DB_HOST = os.environ.get("HAND_DB_HOST", "postgres")
DB_PORT = os.environ.get("HAND_DB_PORT", "5432")
DB_NAME = os.environ.get("HAND_DB_NAME", "database_cardio")
DB_USER = os.environ.get("HAND_DB_USER", "pg-user")
DB_PASSWORD = os.environ.get("HAND_DB_PASSWORD", "postgres")

# US_DB_* — для чтения подключений из внутренней базы DataLens (pg-us-db):
US_DB_HOST = os.environ.get("US_DB_HOST", "postgres")
US_DB_PORT = os.environ.get("US_DB_PORT", "5432")
US_DB_NAME = os.environ.get("US_DB_NAME", "pg-us-db")
US_DB_USER = os.environ.get("US_DB_USER", "pg-user")
US_DB_PASSWORD = os.environ.get("US_DB_PASSWORD", "postgres")


CONTROL_API_CRYPTO_KEY = os.environ.get("CONTROL_API_CRYPTO_KEY", "")


TABLE_PREFIX = "hand_"
VALID_FIELD_TYPES = {
    "text": "TEXT",
    "integer": "INTEGER",
    "real": "REAL",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
}


def get_us_db_connection():
    """Подключение к US-базе DataLens (для чтения подключений)."""
    return psycopg2.connect(
        host=US_DB_HOST, port=US_DB_PORT, dbname=US_DB_NAME,
        user=US_DB_USER, password=US_DB_PASSWORD,
    )


_connections_cache = {}


def get_data_db_connection(connection_id=None):
    """Подключение к базе данных для создания hand-таблиц.
    Если connection_id не указан — подключается к US-базе (по умолчанию)."""
    if not connection_id:
        return psycopg2.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
        )

    if connection_id in _connections_cache:
        return psycopg2.connect(**_connections_cache[connection_id])

    # Получить параметры подключения из US-базы
    us_conn = get_us_db_connection()
    try:
        with us_conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.entry_id,
                       e.name                as name,
                       r.data->>'host'       as host,
                       r.data->>'port'       as port,
                       r.data->>'db_name'   as database,
                       r.data->>'username'   as username,
                       e.unversioned_data->'password'->>'cypher_text' as enc_password
                FROM entries e
                JOIN revisions r ON r.rev_id = COALESCE(e.saved_id, e.published_id)
                
                WHERE e.entry_id = %s::bigint
                  AND e.is_deleted = false
            """, (connection_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Connection {connection_id} not found")
    finally:
        us_conn.close()

    # Расшифровать пароль
    password = ""
    if row['enc_password'] and CONTROL_API_CRYPTO_KEY:
        fernet = Fernet(CONTROL_API_CRYPTO_KEY.encode() if isinstance(CONTROL_API_CRYPTO_KEY, str) else CONTROL_API_CRYPTO_KEY)
        password = fernet.decrypt(row['enc_password'].encode()).decode()

    params = {
        'host': row['host'] or 'localhost',
        'port': row['port'] or '5432',
        'dbname': row['database'] or '',
        'user': row['username'] or '',
        'password': password,
    }
    _connections_cache[connection_id] = params
    return psycopg2.connect(**params)


def validate_table_name(name):
    if not name.startswith(TABLE_PREFIX):
        return False, f"Table name must start with '{TABLE_PREFIX}'"
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        return False, "Table name must contain only lowercase letters, digits and underscores"
    return True, None


def validate_field_name(name):
    if not re.match(r"^[a-z_][a-z0-9_]*$", name):
        return False, "Field name must contain only lowercase letters, digits and underscores"
    return True, None


def format_value(v):
    """Конвертирует date/timestamp из БД в строку dd.MM.YYYY (или dd.MM.YYYY HH:MM)."""
    if isinstance(v, datetime):
        return v.strftime('%d.%m.%Y %H:%M')
    if isinstance(v, date):
        return v.strftime('%d.%m.%Y')
    return v


def parse_date_value(v):
    """Конвертирует dd.MM.YYYY (или dd.MM.YYYY HH:MM) в ISO-формат для PostgreSQL."""
    if not isinstance(v, str) or not v:
        return v
    for fmt in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
        try:
            dt = datetime.strptime(v, fmt)
            return dt.isoformat() if '%H:%M' in fmt else dt.date().isoformat()
        except ValueError:
            continue
    return v



@app.route("/api/v1/hand-tables", methods=["GET"])
def list_tables():
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT table_name,
                       (SELECT count(*) FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = t.table_name) as column_count
                FROM information_schema.tables t
                WHERE table_schema = 'public'
                  AND table_type = 'BASE TABLE'
                  AND table_name LIKE %s
                ORDER BY table_name
            """, (TABLE_PREFIX + "%",))
            return jsonify({"tables": [dict(r) for r in cur.fetchall()]})
    finally:
        conn.close()


@app.route("/api/v1/connections", methods=["GET"])
def list_connections():
    conn = get_us_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT e.entry_id::text as entry_id,
                       e.name              as name,
                       r.data->>'host'     as host,
                       r.data->>'port'     as port,
                       r.data->>'db_name' as database
                FROM entries e
                JOIN revisions r ON r.rev_id = COALESCE(e.saved_id, e.published_id)
                WHERE e.type = 'postgres'
                  AND e.is_deleted = false
                ORDER BY e.name
            """)
            return jsonify({"connections": [dict(r) for r in cur.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()




@app.route("/api/v1/hand-tables", methods=["POST"])
def create_table():
    data = request.get_json()
    table_name = data.get("tableName", "").strip()
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    fields = data.get("fields", [])
    if not fields:
        return jsonify({"error": "At least one field is required"}), 400
    column_defs = [SQL("{} SERIAL PRIMARY KEY").format(Identifier("id"))]
    for f in fields:
        fn = f.get("name", "").strip()
        ft = f.get("type", "text")
        ok, e = validate_field_name(fn)
        if not ok:
            return jsonify({"error": f"Field '{fn}': {e}"}), 400
        if ft not in VALID_FIELD_TYPES:
            return jsonify({"error": f"Field '{fn}': unknown type '{ft}'"}), 400
        column_defs.append(SQL("{} {}").format(Identifier(fn), SQL(VALID_FIELD_TYPES[ft])))
    column_defs.append(SQL("{} TIMESTAMP DEFAULT NOW()").format(Identifier("created_at")))
    connection_id = data.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s", (table_name,))
            if cur.fetchone():
                return jsonify({"error": f"Table '{table_name}' already exists"}), 409
            cur.execute(SQL("CREATE TABLE {} ({})").format(Identifier(table_name), SQL(", ").join(column_defs)))
            conn.commit()
            return jsonify({"message": f"Table '{table_name}' created", "tableName": table_name}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>", methods=["DELETE"])
def delete_table(table_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL("DROP TABLE IF EXISTS {}").format(Identifier(table_name)))
            conn.commit()
            return jsonify({"message": f"Table '{table_name}' dropped"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/schema", methods=["GET"])
def get_schema(table_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
                ORDER BY ordinal_position
            """, (table_name,))
            cols = cur.fetchall()
            if not cols:
                return jsonify({"error": f"Table '{table_name}' not found"}), 404
            return jsonify({"tableName": table_name, "columns": [dict(c) for c in cols]})
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/fields", methods=["POST"])
def add_field(table_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    data = request.get_json()
    fn = data.get("name", "").strip()
    ft = data.get("type", "text")
    ok, e = validate_field_name(fn)
    if not ok:
        return jsonify({"error": e}), 400
    if ft not in VALID_FIELD_TYPES:
        return jsonify({"error": f"Unknown type '{ft}'"}), 400
    connection_id = data.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL("ALTER TABLE {} ADD COLUMN {} {}").format(
                Identifier(table_name), Identifier(fn), SQL(VALID_FIELD_TYPES[ft])))
            conn.commit()
            return jsonify({"message": f"Field '{fn}' added"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/fields/<field_name>", methods=["DELETE"])
def delete_field(table_name, field_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL("ALTER TABLE {} DROP COLUMN IF EXISTS {}").format(
                Identifier(table_name), Identifier(field_name)))
            conn.commit()
            return jsonify({"message": f"Field '{field_name}' dropped"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/data", methods=["GET"])
def get_data(table_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("perPage", 50))
    offset = (page - 1) * per_page
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SQL("SELECT count(*) as total FROM {}").format(Identifier(table_name)))
            total = cur.fetchone()["total"]
            cur.execute(SQL("SELECT * FROM {} ORDER BY id LIMIT %s OFFSET %s").format(Identifier(table_name)), (per_page, offset))
            rows = []
            for r in cur.fetchall():
                row = dict(r)
                for k, v in row.items():
                    row[k] = format_value(v)
                rows.append(row)
            return jsonify({"data": rows, "total": total, "page": page, "perPage": per_page})


    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/data", methods=["POST"])
def insert_row(table_name):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    data = request.get_json()
    data = {k: v for k, v in (data or {}).items() if k not in ("id", "created_at")}
    data = {k: parse_date_value(v) for k, v in data.items()}
    if not data:
        return jsonify({"error": "No valid fields provided"}), 400
    cols = [Identifier(k) for k in data]
    vals = [Literal(v) for v in data.values()]
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                Identifier(table_name), SQL(", ").join(cols), SQL(", ").join(vals)))
            row = cur.fetchone()
            conn.commit()
            return jsonify(dict(row)), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/data/<int:row_id>", methods=["PUT"])
def update_row(table_name, row_id):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    data = request.get_json()
    data = {k: v for k, v in (data or {}).items() if k not in ("id", "created_at")}
    data = {k: parse_date_value(v) for k, v in data.items()}
    if not data:
        return jsonify({"error": "No valid fields to update"}), 400
    sets = [SQL("{} = {}").format(Identifier(k), Literal(v)) for k, v in data.items()]
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(SQL("UPDATE {} SET {} WHERE id = {} RETURNING *").format(
                Identifier(table_name), SQL(", ").join(sets), Literal(row_id)))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": f"Row {row_id} not found"}), 404
            conn.commit()
            return jsonify(dict(row))
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/api/v1/hand-tables/<table_name>/data/<int:row_id>", methods=["DELETE"])
def delete_row(table_name, row_id):
    valid, err = validate_table_name(table_name)
    if not valid:
        return jsonify({"error": err}), 400
    connection_id = request.args.get("connectionId")
    conn = get_data_db_connection(connection_id)
    try:
        with conn.cursor() as cur:
            cur.execute(SQL("DELETE FROM {} WHERE id = {}").format(Identifier(table_name), Literal(row_id)))
            if cur.rowcount == 0:
                return jsonify({"error": f"Row {row_id} not found"}), 404
            conn.commit()
            return jsonify({"message": f"Row {row_id} deleted"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
