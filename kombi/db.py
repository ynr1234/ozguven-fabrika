"""Neon PostgreSQL veritabanı erişim katmanı."""

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row

from .pi_controller import PIState


@dataclass(frozen=True)
class ControlSettings:
    automation_enabled: bool
    comfort_temp_c: float
    away_temp_c: float
    target_room_temp_c: float
    pi_state: PIState
    last_sensor_temp_c: Optional[float]
    last_sensor_at: Optional[datetime]
    last_outside_temp_c: Optional[float]
    last_solar_wm2: Optional[float]
    last_schedule_mode: Optional[str]
    last_decision_reason: Optional[str]
    last_boiler_json: Optional[Dict[str, Any]]


def get_database_url() -> str:
    url = (os.getenv("KOMBI_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError("KOMBI_DATABASE_URL veya DATABASE_URL ortam değişkeni tanımlı değil.")
    return url


@contextmanager
def get_connection() -> Iterator[psycopg.Connection[Dict[str, Any]]]:
    with psycopg.connect(get_database_url(), row_factory=dict_row) as conn:
        yield conn


def init_schema(schema_path: Optional[Path] = None) -> None:
    path = schema_path or (Path(__file__).resolve().parent / "schema.sql")
    sql = path.read_text(encoding="utf-8")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def load_oauth_token(provider: str = "viessmann") -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, access_token, refresh_token, token_type, expires_at, raw_token
                FROM oauth_tokens
                WHERE provider = %s
                """,
                (provider,),
            )
            row = cur.fetchone()
    if not row:
        return None
    if row.get("raw_token"):
        return dict(row["raw_token"])
    expires_at = row.get("expires_at")
    return {
        "access_token": row["access_token"],
        "refresh_token": row["refresh_token"],
        "token_type": row.get("token_type") or "Bearer",
        "expires_at": expires_at.timestamp() if expires_at else None,
    }


def save_oauth_token(token: Dict[str, Any], provider: str = "viessmann") -> None:
    expires_at_ts = token.get("expires_at")
    expires_at = (
        datetime.fromtimestamp(float(expires_at_ts), tz=timezone.utc)
        if expires_at_ts is not None
        else None
    )
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO oauth_tokens (
                    provider, access_token, refresh_token, token_type, expires_at, raw_token, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (provider) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(NULLIF(EXCLUDED.refresh_token, ''), oauth_tokens.refresh_token),
                    token_type = EXCLUDED.token_type,
                    expires_at = EXCLUDED.expires_at,
                    raw_token = EXCLUDED.raw_token,
                    updated_at = NOW()
                """,
                (
                    provider,
                    token["access_token"],
                    token.get("refresh_token", ""),
                    token.get("token_type", "Bearer"),
                    expires_at,
                    json.dumps(token),
                ),
            )
        conn.commit()


def load_control_settings() -> ControlSettings:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM control_state WHERE id = 1")
            row = cur.fetchone()
    if not row:
        return ControlSettings(
            automation_enabled=True,
            comfort_temp_c=22.0,
            away_temp_c=20.0,
            target_room_temp_c=22.0,
            pi_state=PIState(),
            last_sensor_temp_c=None,
            last_sensor_at=None,
            last_outside_temp_c=None,
            last_solar_wm2=None,
            last_schedule_mode=None,
            last_decision_reason=None,
            last_boiler_json=None,
        )
    return ControlSettings(
        automation_enabled=bool(row.get("automation_enabled", True)),
        comfort_temp_c=float(row.get("comfort_temp_c", 22.0)),
        away_temp_c=float(row.get("away_temp_c", 20.0)),
        target_room_temp_c=float(row["target_room_temp_c"]),
        pi_state=PIState(
            integral_error=float(row["integral_error"]),
            last_error=float(row["last_error"]),
            last_output_supply_temp_c=float(row["last_output_supply_temp_c"]),
            updated_at=row["updated_at"],
        ),
        last_sensor_temp_c=row.get("last_sensor_temp_c"),
        last_sensor_at=row.get("last_sensor_at"),
        last_outside_temp_c=row.get("last_outside_temp_c"),
        last_solar_wm2=row.get("last_solar_wm2"),
        last_schedule_mode=row.get("last_schedule_mode"),
        last_decision_reason=row.get("last_decision_reason"),
        last_boiler_json=row.get("last_boiler_json"),
    )


def update_user_settings(
    automation_enabled: Optional[bool] = None,
    comfort_temp_c: Optional[float] = None,
    away_temp_c: Optional[float] = None,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE control_state
                SET automation_enabled = COALESCE(%s, automation_enabled),
                    comfort_temp_c = COALESCE(%s, comfort_temp_c),
                    away_temp_c = COALESCE(%s, away_temp_c)
                WHERE id = 1
                """,
                (automation_enabled, comfort_temp_c, away_temp_c),
            )
        conn.commit()


def save_control_state(
    integral_error: float,
    last_error: float,
    last_output_supply_temp_c: float,
    target_room_temp_c: float,
    fail_safe_active: bool,
    sensor_temp_c: Optional[float],
    sensor_at: Optional[datetime],
    outside_temp_c: Optional[float],
    solar_wm2: Optional[float],
    schedule_mode: str,
    decision_reason: str,
    boiler_json: Optional[Dict[str, Any]] = None,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE control_state
                SET integral_error = %s,
                    last_error = %s,
                    last_output_supply_temp_c = %s,
                    target_room_temp_c = %s,
                    fail_safe_active = %s,
                    last_sensor_temp_c = COALESCE(%s, last_sensor_temp_c),
                    last_sensor_at = COALESCE(%s, last_sensor_at),
                    last_outside_temp_c = COALESCE(%s, last_outside_temp_c),
                    last_solar_wm2 = COALESCE(%s, last_solar_wm2),
                    last_schedule_mode = %s,
                    last_decision_reason = %s,
                    last_boiler_json = COALESCE(%s::jsonb, last_boiler_json),
                    updated_at = NOW()
                WHERE id = 1
                """,
                (
                    integral_error,
                    last_error,
                    last_output_supply_temp_c,
                    target_room_temp_c,
                    fail_safe_active,
                    sensor_temp_c,
                    sensor_at,
                    outside_temp_c,
                    solar_wm2,
                    schedule_mode,
                    decision_reason,
                    json.dumps(boiler_json) if boiler_json is not None else None,
                ),
            )
        conn.commit()


def log_temperature(
    source: str,
    room_name: str,
    indoor_temp_c: Optional[float],
    target_room_temp_c: float,
    outside_temp_c: Optional[float],
    solar_radiation_wm2: Optional[float],
    boiler_supply_temp_c: Optional[float],
    target_supply_temp_c: float,
    burner_active: Optional[bool],
    modulation_pct: Optional[float],
    pressure_bar: Optional[float],
    burner_starts: Optional[int],
    burner_hours: Optional[int],
    gas_heating_today_m3: Optional[float],
    schedule_mode: str,
    decision_reason: str,
    fail_safe_active: bool,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO temperature_logs (
                    source, room_name, indoor_temp_c, target_room_temp_c,
                    outside_temp_c, solar_radiation_wm2,
                    boiler_supply_temp_c, target_supply_temp_c,
                    burner_active, modulation_pct, pressure_bar,
                    burner_starts, burner_hours, gas_heating_today_m3,
                    schedule_mode, decision_reason, fail_safe_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    source,
                    room_name,
                    indoor_temp_c,
                    target_room_temp_c,
                    outside_temp_c,
                    solar_radiation_wm2,
                    boiler_supply_temp_c,
                    target_supply_temp_c,
                    burner_active,
                    modulation_pct,
                    pressure_bar,
                    burner_starts,
                    burner_hours,
                    gas_heating_today_m3,
                    schedule_mode,
                    decision_reason,
                    fail_safe_active,
                ),
            )
        conn.commit()


def get_recent_logs(limit: int = 36) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM temperature_logs
                ORDER BY recorded_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return list(cur.fetchall())


def estimate_indoor_trend_c_per_hour(lookback_minutes: int = 90) -> float:
    """Son kayıtlardan oda sıcaklığı değişim hızını (°C/saat) hesaplar."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indoor_temp_c, recorded_at
                FROM temperature_logs
                WHERE indoor_temp_c IS NOT NULL
                  AND recorded_at >= NOW() - (%s || ' minutes')::INTERVAL
                ORDER BY recorded_at ASC
                """,
                (lookback_minutes,),
            )
            rows = cur.fetchall()

    if len(rows) < 2:
        return 0.0

    first, last = rows[0], rows[-1]
    dt_hours = (last["recorded_at"] - first["recorded_at"]).total_seconds() / 3600.0
    if dt_hours < 0.15:
        return 0.0

    rate = (float(last["indoor_temp_c"]) - float(first["indoor_temp_c"])) / dt_hours
    return max(-2.0, min(2.0, round(rate, 3)))


def estimate_learned_heating_rate_c_per_hour(default_rate: float = 0.45) -> float:
    """Son 7 günde brülör yanarken odanın ortalama ısınma hızını (°C/saat) öğrenir."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT indoor_temp_c, burner_active, recorded_at
                FROM temperature_logs
                WHERE indoor_temp_c IS NOT NULL
                  AND recorded_at >= NOW() - INTERVAL '7 days'
                ORDER BY recorded_at ASC
                """
            )
            rows = cur.fetchall()

    rates: List[float] = []
    for prev, curr in zip(rows, rows[1:]):
        if not (prev.get("burner_active") and curr.get("burner_active")):
            continue
        dt_h = (curr["recorded_at"] - prev["recorded_at"]).total_seconds() / 3600.0
        if not (0.05 <= dt_h <= 0.5):
            continue
        d_temp = float(curr["indoor_temp_c"]) - float(prev["indoor_temp_c"])
        rate = d_temp / dt_h
        if 0.1 <= rate <= 1.5:
            rates.append(rate)

    if len(rates) < 3:
        return default_rate

    avg_rate = sum(rates) / len(rates)
    return max(0.25, min(1.2, round(avg_rate, 3)))
