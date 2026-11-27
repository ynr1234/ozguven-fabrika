-- Neon PostgreSQL Şeması: Kombi Otomasyonu

CREATE TABLE IF NOT EXISTS oauth_tokens (
    provider TEXT PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_type TEXT DEFAULT 'Bearer',
    expires_at TIMESTAMPTZ,
    raw_token JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS temperature_logs (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    room_name TEXT NOT NULL DEFAULT 'salon',
    indoor_temp_c DOUBLE PRECISION,
    target_room_temp_c DOUBLE PRECISION,
    outside_temp_c DOUBLE PRECISION,
    solar_radiation_wm2 DOUBLE PRECISION,
    boiler_supply_temp_c DOUBLE PRECISION,
    target_supply_temp_c DOUBLE PRECISION,
    burner_active BOOLEAN,
    modulation_pct DOUBLE PRECISION,
    pressure_bar DOUBLE PRECISION,
    burner_starts INTEGER,
    burner_hours INTEGER,
    gas_heating_today_m3 DOUBLE PRECISION,
    schedule_mode TEXT,
    decision_reason TEXT,
    fail_safe_active BOOLEAN NOT NULL DEFAULT FALSE,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS outside_temp_c DOUBLE PRECISION;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS solar_radiation_wm2 DOUBLE PRECISION;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS pressure_bar DOUBLE PRECISION;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS burner_starts INTEGER;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS burner_hours INTEGER;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS gas_heating_today_m3 DOUBLE PRECISION;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS schedule_mode TEXT;
ALTER TABLE temperature_logs ADD COLUMN IF NOT EXISTS decision_reason TEXT;

CREATE INDEX IF NOT EXISTS idx_temperature_logs_recorded_at
    ON temperature_logs (recorded_at DESC);

CREATE TABLE IF NOT EXISTS control_state (
    id INTEGER PRIMARY KEY DEFAULT 1,
    automation_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    target_room_temp_c DOUBLE PRECISION NOT NULL DEFAULT 22.0,
    comfort_temp_c DOUBLE PRECISION NOT NULL DEFAULT 22.0,
    away_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0,
    integral_error DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_error DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    last_output_supply_temp_c DOUBLE PRECISION NOT NULL DEFAULT 42.0,
    fail_safe_active BOOLEAN NOT NULL DEFAULT FALSE,
    last_sensor_temp_c DOUBLE PRECISION,
    last_sensor_at TIMESTAMPTZ,
    last_outside_temp_c DOUBLE PRECISION,
    last_solar_wm2 DOUBLE PRECISION,
    last_schedule_mode TEXT,
    last_decision_reason TEXT,
    last_boiler_json JSONB,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT single_row_check CHECK (id = 1)
);

ALTER TABLE control_state ADD COLUMN IF NOT EXISTS automation_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS comfort_temp_c DOUBLE PRECISION NOT NULL DEFAULT 22.0;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS away_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS last_outside_temp_c DOUBLE PRECISION;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS last_solar_wm2 DOUBLE PRECISION;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS last_schedule_mode TEXT;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS last_decision_reason TEXT;
ALTER TABLE control_state ADD COLUMN IF NOT EXISTS last_boiler_json JSONB;

INSERT INTO control_state (id, automation_enabled, target_room_temp_c, comfort_temp_c, away_temp_c, last_output_supply_temp_c)
VALUES (1, TRUE, 22.0, 22.0, 20.0, 42.0)
ON CONFLICT (id) DO NOTHING;
