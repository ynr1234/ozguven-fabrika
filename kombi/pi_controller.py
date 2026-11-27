"""Öngörülü, yoğuşma dostu ve haftalık programlı kazan kontrol algoritması."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class PIConfig:
    comfort_temp_c: float = 22.0
    away_temp_c: float = 20.0
    kp: float = 2.8
    ki: float = 0.0012
    trend_damping: float = 4.0
    min_active_supply_temp_c: float = 39.0   # Kısa döngü koruması tabanı (yoğuşma bölgesi)
    max_supply_temp_c: float = 53.0          # Yoğuşma verimini koruyan tavan
    idle_supply_temp_c: float = 30.0         # Vitodens 050-W ısıtma kesme tabanı
    failsafe_supply_temp_c: float = 42.0     # Bayat veri durumunda güvenli sabit gidiş suyu
    stale_data_seconds: int = 900            # 15 dakika
    idle_deadband_c: float = 0.3             # Hedefin +0.3°C üstünde bekleme tabanına çek
    max_step_change_c: float = 2.0           # Tek döngüde ani sıcaklık sıçramasını önle
    integral_limit: float = 2500.0


@dataclass
class PIState:
    integral_error: float = 0.0
    last_error: float = 0.0
    last_output_supply_temp_c: float = 42.0
    updated_at: Optional[datetime] = None


@dataclass(frozen=True)
class ControlDecision:
    active_target_room_temp_c: float
    schedule_mode: str
    target_supply_temp_c: float
    fail_safe_active: bool
    heating_active: bool
    error_c: float
    new_integral_error: float
    reason: str


def resolve_schedule_target(
    now_utc: datetime,
    comfort_temp_c: float,
    away_temp_c: float,
    indoor_temp_c: Optional[float],
    learned_heating_rate_c_per_h: float = 0.45,
) -> tuple[float, str]:
    """Hafta içi 08:00-17:30 ekonomi + dinamik ön ısıtma, akşam ve hafta sonu konfor."""
    local_dt = now_utc.astimezone(ISTANBUL_TZ)
    weekday = local_dt.weekday()  # 0=Pzt .. 4=Cum, 5=Cmt, 6=Paz
    if weekday >= 5:
        return comfort_temp_c, "COMFORT_WEEKEND"

    minutes = local_dt.hour * 60 + local_dt.minute
    start_away = 8 * 60          # 08:00
    end_away = 17 * 60 + 30      # 17:30
    preheat_earliest = 15 * 60 + 30

    if start_away <= minutes < end_away:
        if minutes >= preheat_earliest and indoor_temp_c is not None and indoor_temp_c < (comfort_temp_c - 0.1):
            remaining_min = end_away - minutes
            rate = max(0.2, learned_heating_rate_c_per_h)
            needed_min = max(15.0, min(120.0, ((comfort_temp_c - indoor_temp_c) / rate) * 60.0))
            if remaining_min <= needed_min:
                return comfort_temp_c, "PREHEAT"
        return away_temp_c, "AWAY"

    return comfort_temp_c, "COMFORT"


def _weather_base_supply(outside_temp_c: Optional[float], min_next_3h_c: Optional[float]) -> float:
    if outside_temp_c is None:
        return 42.0
    if min_next_3h_c is not None:
        effective_outside = 0.7 * outside_temp_c + 0.3 * min_next_3h_c
    else:
        effective_outside = outside_temp_c
    # 14°C dış havada ~39.5°C, 10°C -> ~42°C, 5°C -> ~45°C, 0°C -> ~48°C
    return 39.5 + max(0.0, (14.0 - effective_outside) * 0.6)


def compute_supply_temperature(
    indoor_temp_c: Optional[float],
    sensor_timestamp: Optional[datetime],
    state: PIState,
    config: PIConfig,
    now: Optional[datetime] = None,
    outside_temp_c: Optional[float] = None,
    min_next_3h_outside_c: Optional[float] = None,
    solar_wm2: Optional[float] = None,
    max_next_3h_solar_wm2: Optional[float] = None,
    indoor_trend_c_per_h: float = 0.0,
    learned_heating_rate_c_per_h: float = 0.45,
) -> ControlDecision:
    """İç sıcaklık, dış hava tahmini, güneş kazancı ve öğrenilen trende göre kazan gidiş suyunu hesaplar."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    active_target, schedule_mode = resolve_schedule_target(
        now_utc=now,
        comfort_temp_c=config.comfort_temp_c,
        away_temp_c=config.away_temp_c,
        indoor_temp_c=indoor_temp_c,
        learned_heating_rate_c_per_h=learned_heating_rate_c_per_h,
    )

    # 1. Bayat Veri Koruması (Fail-Safe)
    if indoor_temp_c is None or sensor_timestamp is None:
        return ControlDecision(
            active_target_room_temp_c=active_target,
            schedule_mode="FAILSAFE",
            target_supply_temp_c=config.failsafe_supply_temp_c,
            fail_safe_active=True,
            heating_active=True,
            error_c=0.0,
            new_integral_error=state.integral_error,
            reason="Sensör verisi yok; güvenli sabit gidiş suyuna (42°C) geçildi.",
        )

    if sensor_timestamp.tzinfo is None:
        sensor_timestamp = sensor_timestamp.replace(tzinfo=timezone.utc)

    age_seconds = (now - sensor_timestamp).total_seconds()
    if age_seconds > config.stale_data_seconds:
        return ControlDecision(
            active_target_room_temp_c=active_target,
            schedule_mode="FAILSAFE",
            target_supply_temp_c=config.failsafe_supply_temp_c,
            fail_safe_active=True,
            heating_active=True,
            error_c=0.0,
            new_integral_error=state.integral_error,
            reason=f"Sensör verisi {int(age_seconds)} sn eski (> {config.stale_data_seconds} sn); fail-safe aktif.",
        )

    error = active_target - indoor_temp_c

    # 2. Güneş / Aşırı Isınma Koruması -> Kombi ısıtma tabanına (30°C) çekilir
    solar_active = (solar_wm2 is not None and solar_wm2 >= 300.0)
    early_solar_or_rising_cutoff = error <= -0.15 and (indoor_trend_c_per_h >= 0.15 or solar_active)
    if error <= -config.idle_deadband_c or early_solar_or_rising_cutoff:
        decayed_integral = max(0.0, state.integral_error * 0.5)
        return ControlDecision(
            active_target_room_temp_c=active_target,
            schedule_mode=schedule_mode,
            target_supply_temp_c=config.idle_supply_temp_c,
            fail_safe_active=False,
            heating_active=False,
            error_c=error,
            new_integral_error=decayed_integral,
            reason=(
                f"[{schedule_mode}] Oda {indoor_temp_c:.2f}°C (hedef {active_target:.1f}°C, "
                f"trend {indoor_trend_c_per_h:+.2f}°C/sa); {config.idle_supply_temp_c:.0f}°C tabana çekildi."
            ),
        )

    # 3. Yumuşak PI + Dış Hava Bazı + Öngörülü Trend/Güneş Sönümleme
    if state.updated_at is not None:
        last_dt = state.updated_at if state.updated_at.tzinfo else state.updated_at.replace(tzinfo=timezone.utc)
        dt = max(1.0, min(1800.0, (now - last_dt).total_seconds()))
    else:
        dt = 300.0

    raw_integral = state.integral_error + (error * dt)
    clamped_integral = max(-config.integral_limit, min(config.integral_limit, raw_integral))

    base_supply = _weather_base_supply(outside_temp_c, min_next_3h_outside_c)
    p_term = config.kp * error
    i_term = config.ki * clamped_integral

    # Oda zaten ısınıyorsa erken yumuşat (overshoot önleme)
    trend_adj = -config.trend_damping * max(0.0, indoor_trend_c_per_h)

    # Önümüzdeki 3 saatte güçlü güneş bekleniyorsa ve fark küçükse 1-1.5°C erken kıs
    solar_adj = 0.0
    if max_next_3h_solar_wm2 is not None and max_next_3h_solar_wm2 >= 350.0 and error < 1.0:
        solar_adj = -1.5

    raw_supply = base_supply + p_term + i_term + trend_adj + solar_adj

    # 4. Kısa Döngü Koruması + Yoğuşma Tavanı
    bounded_supply = max(
        config.min_active_supply_temp_c,
        min(config.max_supply_temp_c, raw_supply),
    )

    # 5. Rampa Hızı Sınırlama (Aktif moddayken döngü başına ±2°C'den hızlı sıçrama yapma)
    if state.last_output_supply_temp_c >= config.min_active_supply_temp_c:
        delta = bounded_supply - state.last_output_supply_temp_c
        if abs(delta) > config.max_step_change_c:
            bounded_supply = state.last_output_supply_temp_c + (
                config.max_step_change_c if delta > 0 else -config.max_step_change_c
            )

    final_supply = float(round(bounded_supply))

    # Anti-windup
    if (raw_supply > config.max_supply_temp_c and error > 0) or (
        raw_supply < config.min_active_supply_temp_c and error < 0
    ):
        clamped_integral = state.integral_error

    return ControlDecision(
        active_target_room_temp_c=active_target,
        schedule_mode=schedule_mode,
        target_supply_temp_c=final_supply,
        fail_safe_active=False,
        heating_active=True,
        error_c=error,
        new_integral_error=clamped_integral,
        reason=(
            f"[{schedule_mode}] Baz={base_supply:.1f}°C, P={p_term:+.2f}, I={i_term:+.2f}, "
            f"Trend={trend_adj:+.2f}, Güneş={solar_adj:+.1f} -> {final_supply:.0f}°C"
        ),
    )
