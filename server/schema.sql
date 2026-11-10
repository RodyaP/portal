CREATE TABLE IF NOT EXISTS bookings (
 id uuid PRIMARY KEY, series_id uuid, room integer NOT NULL CHECK (room IN (1,2)),
 starts timestamptz NOT NULL, ends timestamptz NOT NULL CHECK (ends > starts),
 title text NOT NULL, purpose text NOT NULL DEFAULT '',
 organizer jsonb NOT NULL, creator jsonb NOT NULL, participants jsonb NOT NULL DEFAULT '[]',
 kind text NOT NULL DEFAULT 'meeting' CHECK(kind IN ('meeting','block')),
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
 version integer NOT NULL DEFAULT 1, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT no_overlap EXCLUDE USING gist (int4range(room,room,'[]') WITH &&, tstzrange(starts,ends,'[)') WITH &&)
 WHERE (status='active') DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS bookings_dates ON bookings(starts,ends);
CREATE INDEX IF NOT EXISTS bookings_series ON bookings(series_id);
CREATE TABLE IF NOT EXISTS audit (
 id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
 actor jsonb NOT NULL, action text NOT NULL, booking_id uuid, reason text NOT NULL DEFAULT '',
 changes jsonb NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS requests (
 actor text NOT NULL, request_id uuid NOT NULL, fingerprint text NOT NULL,
 response jsonb NOT NULL, at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(actor,request_id)
);
CREATE TABLE IF NOT EXISTS portal_local (
 id integer PRIMARY KEY CHECK(id=1), login text NOT NULL, password_hash text NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_sessions (
 token_hash text PRIMARY KEY, person_id text NOT NULL, local boolean NOT NULL,
 expires timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS portal_sessions_expires ON portal_sessions(expires);
CREATE TABLE IF NOT EXISTS portal_roles (
 person_id text PRIMARY KEY, person jsonb NOT NULL,
 admin boolean NOT NULL DEFAULT false, delegate boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS portal_settings (
 id integer PRIMARY KEY CHECK(id=1), payload text NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_attempts (
 key text PRIMARY KEY, attempts integer NOT NULL, started timestamptz NOT NULL
);
