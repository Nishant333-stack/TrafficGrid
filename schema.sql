-- PostGIS is optional; skip if not available
-- CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region TEXT,
    environment TEXT NOT NULL DEFAULT 'local',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO tenants (id, name, region, environment)
VALUES ('bengaluru-traffic', 'Bengaluru Traffic Command', 'Bengaluru', 'local')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS app_users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    police_station TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    event_type TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    address TEXT,
    end_address TEXT,
    event_cause TEXT,
    requires_road_closure BOOLEAN,
    start_datetime TIMESTAMPTZ,
    end_datetime TIMESTAMPTZ,
    status TEXT,
    description TEXT,
    veh_type TEXT,
    veh_no TEXT,
    corridor TEXT,
    priority TEXT,
    route_path TEXT,
    police_station TEXT,
    closed_datetime TIMESTAMPTZ,
    resolved_datetime TIMESTAMPTZ,
    zone TEXT,
    junction TEXT,
    duration_minutes INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_zone ON events (zone);
CREATE INDEX IF NOT EXISTS idx_events_police_station ON events (police_station);
CREATE INDEX IF NOT EXISTS idx_events_start_datetime ON events (start_datetime);

CREATE TABLE IF NOT EXISTS police_stations (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    zone TEXT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    available_personnel INTEGER NOT NULL DEFAULT 0,
    available_barricades INTEGER NOT NULL DEFAULT 0
);

INSERT INTO police_stations
    (name, zone, latitude, longitude, available_personnel, available_barricades)
VALUES
    ('Yelahanka', 'North Zone 2', 13.101419, 77.596026, 31, 48),
    ('HAL Old Airport', 'East Zone 1', 12.953229, 77.697134, 24, 35),
    ('Sadashivanagar', 'Central Zone 1', 13.010332, 77.579722, 19, 27),
    ('Byatarayanapura', 'West Zone 2', 12.949359, 77.534226, 22, 31),
    ('Halasuru Gate', 'Central Zone 2', 12.967149, 77.587305, 28, 41),
    ('Yeshwanthpura', 'North Zone 1', 13.026197, 77.544762, 26, 44),
    ('Hennuru', 'East Zone 2', 13.044663, 77.633338, 20, 29),
    ('Kodigehalli', 'North Zone 2', 13.047052, 77.585742, 18, 24),
    ('Banaswadi', 'East Zone 1', 13.000874, 77.656685, 27, 39),
    ('K.R. Pura', 'East Zone 2', 13.016153, 77.705730, 32, 46),
    ('Kamakshipalya', 'West Zone 1', 12.987790, 77.507889, 23, 33),
    ('Cubbon Park', 'Central Zone 1', 12.978084, 77.595608, 21, 30),
    ('Jalahalli', 'North Zone 1', 13.043600, 77.548758, 17, 25),
    ('Chamarajpet', 'Central Zone 2', 12.965532, 77.563788, 20, 28),
    ('High ground', 'Central Zone 1', 12.988736, 77.585475, 18, 26)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    predicted_severity TEXT,
    predicted_duration_minutes INTEGER,
    actual_duration_minutes INTEGER,
    officer_rating INTEGER CHECK (officer_rating BETWEEN 1 AND 5),
    plan_accepted BOOLEAN,
    adjusted_personnel INTEGER,
    plan_total_personnel INTEGER,
    plan_json JSONB,
    seed_source TEXT,
    event_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS plan_workflows (
    id BIGSERIAL PRIMARY KEY,
    plan_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'bengaluru-traffic',
    actor TEXT,
    approval_chain JSONB,
    plan_json JSONB,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plan_workflows_plan_id ON plan_workflows (plan_id);
CREATE INDEX IF NOT EXISTS idx_plan_workflows_event_id ON plan_workflows (event_id);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    audit_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id TEXT,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created ON audit_log (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS field_status_updates (
    id BIGSERIAL PRIMARY KEY,
    status_id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    actor TEXT,
    station TEXT,
    event_id TEXT NOT NULL,
    control_point_node_id TEXT,
    status TEXT NOT NULL,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    note TEXT,
    photo_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_field_status_event ON field_status_updates (event_id, created_at DESC);
