"""Kombi otomasyon kontrol döngüsü servisi."""

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .db import (
    estimate_indoor_trend_c_per_hour,
    learn_thermal_profile,
    load_control_settings,
    log_temperature,
    save_control_state,
)
from .midea_cloud import fetch_ac_readings
from .pi_controller import PIConfig, compute_supply_temperature
from .viessmann_client import ViessmannClient
from .weather import fetch_outdoor_weather

logger = logging.getLogger("kombi-app")
_cycle_lock = threading.Lock()


def load_pi_config(
    comfort_temp_c: float,
    away_temp_c: float,
    away_start_time: str,
    comfort_ready_time: str,
) -> PIConfig:
    return PIConfig(
        comfort_temp_c=comfort_temp_c,
        away_temp_c=away_temp_c,
        away_start_time=away_start_time,
        comfort_ready_time=comfort_ready_time,
        kp=float(os.getenv("PI_KP", "2.6")),
        ki=float(os.getenv("PI_KI", "0.0012")),
        trend_damping=float(os.getenv("PI_TREND_DAMPING", "4.0")),
        min_active_supply_temp_c=float(os.getenv("MIN_ACTIVE_SUPPLY_TEMP_C", "39.0")),
        max_supply_temp_c=float(os.getenv("MAX_SUPPLY_TEMP_C", "53.0")),
        idle_supply_temp_c=float(os.getenv("IDLE_SUPPLY_TEMP_C", "30.0")),
        failsafe_supply_temp_c=float(os.getenv("FAILSAFE_SUPPLY_TEMP_C", "42.0")),
        stale_data_seconds=int(os.getenv("STALE_DATA_SECONDS", "900")),
        max_step_change_c=float(os.getenv("MAX_STEP_CHANGE_C", "2.0")),
    )


def read_room_and_ac_outdoor() -> tuple[Optional[float], Optional[float], Optional[datetime], str]:
    preferred_room = os.getenv("MIDEA_ROOM_FILTER", "").strip().lower()
    raw_codes = os.getenv("MIDEA_APPLIANCE_CODES", "").strip()
    allowed_codes = {int(c.strip()) for c in raw_codes.split(",") if c.strip()} if raw_codes else set()
    try:
        readings = fetch_ac_readings()
        valid = [r for r in readings if r.indoor_temp_c is not None]
        if allowed_codes:
            matched_by_id = [r for r in valid if r.appliance_code in allowed_codes]
            if matched_by_id:
                valid = matched_by_id
        elif preferred_room:
            matched = [r for r in valid if preferred_room in r.name.lower()]
            if matched:
                valid = matched

        if not valid:
            return None, None, None, "midea_cloud_empty"

        avg_indoor = sum(r.indoor_temp_c for r in valid if r.indoor_temp_c is not None) / len(valid)
        outdoor_vals = [
            r.outdoor_temp_c
            for r in valid
            if r.power_on and r.outdoor_temp_c is not None and -30.0 <= r.outdoor_temp_c <= 55.0
        ]
        ac_outdoor = (sum(outdoor_vals) / len(outdoor_vals)) if outdoor_vals else None
        newest_ts = max(r.recorded_at for r in valid)
        room_label = ", ".join(f"{r.name}({r.appliance_code})" for r in valid)
        return round(avg_indoor, 2), (round(ac_outdoor, 1) if ac_outdoor is not None else None), newest_ts, room_label
    except Exception as exc:
        logger.warning("Midea Cloud okuma hatası: %s", exc)
        return None, None, None, "midea_cloud_error"


def run_cycle(dry_run: bool = False) -> Dict[str, Any]:
    settings = load_control_settings()
    config = load_pi_config(
        settings.comfort_temp_c,
        settings.away_temp_c,
        settings.away_start_time,
        settings.comfort_ready_time,
    )

    indoor_temp, ac_outdoor_temp, sensor_ts, room_label = read_room_and_ac_outdoor()
    weather = fetch_outdoor_weather()

    effective_outside = ac_outdoor_temp if ac_outdoor_temp is not None else weather.outside_temp_c
    outside_src = "midea-outdoor" if ac_outdoor_temp is not None else weather.source

    now = datetime.now(timezone.utc)
    indoor_trend = estimate_indoor_trend_c_per_hour(90)
    thermal_profile = learn_thermal_profile(now)

    decision = compute_supply_temperature(
        indoor_temp_c=indoor_temp,
        sensor_timestamp=sensor_ts,
        state=settings.pi_state,
        config=config,
        now=now,
        outside_temp_c=effective_outside,
        min_next_3h_outside_c=weather.min_next_3h_temp_c,
        solar_wm2=weather.current_solar_wm2,
        max_next_3h_solar_wm2=weather.max_next_3h_solar_wm2,
        indoor_trend_c_per_h=indoor_trend,
        thermal_profile=thermal_profile,
    )

    boiler = ViessmannClient()
    snapshot = boiler.read_snapshot()

    if not settings.automation_enabled:
        schedule_mode = "MANUAL_OFF"
        decision_reason = "Otomasyon panelden kapatıldı; kombiye yazma yapılmıyor (yalnızca izleme)."
        target_supply_for_log = float(snapshot.normal_target_temp_c or settings.pi_state.last_output_supply_temp_c)
        written = False
    else:
        schedule_mode = decision.schedule_mode
        decision_reason = decision.reason
        target_supply_for_log = decision.target_supply_temp_c
        should_write = (
            snapshot.normal_target_temp_c is None
            or abs(snapshot.normal_target_temp_c - decision.target_supply_temp_c) >= 1.0
        )
        written = False
        if should_write and not dry_run:
            boiler.set_normal_supply_temperature(decision.target_supply_temp_c)
            written = True

    save_control_state(
        integral_error=decision.new_integral_error if settings.automation_enabled else settings.pi_state.integral_error,
        last_error=decision.error_c,
        last_output_supply_temp_c=target_supply_for_log,
        target_room_temp_c=decision.active_target_room_temp_c,
        fail_safe_active=decision.fail_safe_active if settings.automation_enabled else False,
        sensor_temp_c=indoor_temp,
        sensor_at=sensor_ts,
        outside_temp_c=effective_outside,
        solar_wm2=weather.current_solar_wm2,
        schedule_mode=schedule_mode,
        decision_reason=decision_reason,
        boiler_json=snapshot.to_dict(),
    )

    log_temperature(
        source=f"midea+{outside_src}",
        room_name=room_label,
        indoor_temp_c=indoor_temp,
        target_room_temp_c=decision.active_target_room_temp_c,
        outside_temp_c=effective_outside,
        solar_radiation_wm2=weather.current_solar_wm2,
        boiler_supply_temp_c=snapshot.supply_temp_c,
        target_supply_temp_c=target_supply_for_log,
        burner_active=snapshot.burner_active,
        modulation_pct=snapshot.modulation_pct,
        pressure_bar=snapshot.pressure_bar,
        burner_starts=snapshot.burner_starts,
        burner_hours=snapshot.burner_hours,
        gas_heating_today_m3=snapshot.gas_heating_today_m3,
        schedule_mode=schedule_mode,
        decision_reason=decision_reason,
        fail_safe_active=decision.fail_safe_active if settings.automation_enabled else False,
    )

    return {
        "indoor_temp_c": indoor_temp,
        "outside_temp_c": effective_outside,
        "outside_source": outside_src,
        "solar_wm2": weather.current_solar_wm2,
        "active_target_room_temp_c": decision.active_target_room_temp_c,
        "schedule_mode": schedule_mode,
        "target_supply_temp_c": target_supply_for_log,
        "estimated_preheat_minutes": decision.estimated_preheat_minutes,
        "k_heat": thermal_profile.k_heat,
        "k_loss": thermal_profile.k_loss,
        "written": written,
        "dry_run": dry_run,
        "reason": decision_reason,
        "boiler": snapshot.to_dict(),
    }


def run_cycle_if_due(
    min_interval_seconds: Optional[int] = None,
    force: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    interval = (
        min_interval_seconds
        if min_interval_seconds is not None
        else int(os.getenv("LOOP_INTERVAL_SECONDS", "300"))
    )
    if not _cycle_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "Halihazırda çalışan bir döngü var."}

    try:
        if not force:
            settings = load_control_settings()
            updated_at = settings.pi_state.updated_at
            if updated_at is not None and settings.last_schedule_mode is not None:
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                elapsed = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if elapsed < max(30, interval - 15):
                    return {
                        "status": "cooldown",
                        "elapsed_seconds": int(elapsed),
                        "next_due_in_seconds": int(interval - elapsed),
                    }
        result = run_cycle(dry_run=dry_run)
        result["status"] = "executed"
        return result
    finally:
        _cycle_lock.release()
