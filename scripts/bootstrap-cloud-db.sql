-- Run this once in Neon's SQL editor, as the project's owner role, before the
-- first deploy.
--
-- It creates the vector extension and the low-privilege runtime role. Neon
-- gives you one owner role; pointing the API at that role would work, and
-- would also mean an application bug could ALTER or DROP the tables it
-- queries. The API asserts at startup that it is not connected as a superuser
-- or a table owner, and refuses to serve traffic if it is -- so this script is
-- not optional.
--
-- Replace the password before running. Then set, on the API service:
--
--   DATABASE_URL        = postgresql+psycopg://footnote_app:<password>@<host>/<db>?sslmode=require
--   DATABASE_ADMIN_URL  = <the owner connection string Neon gave you>

CREATE EXTENSION IF NOT EXISTS vector;

CREATE ROLE footnote_app LOGIN PASSWORD 'replace-me'
    NOSUPERUSER NOCREATEDB NOCREATEROLE;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE neondb TO footnote_app;   -- rename if your db differs
GRANT USAGE ON SCHEMA public TO footnote_app;

-- Tables do not exist yet: the first migration creates them and grants on them,
-- and sets default privileges so later migrations do the same automatically.
-- Nothing else is needed here.
