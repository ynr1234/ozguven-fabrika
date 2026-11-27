"""Adaptif (kendi kendine öğrenen) ısıl model, öngörülü kontrolcü ve haftalık program."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class ThermalProfile:
    """Evin geçmiş verilerden öğrenilmiş ısınma ve soğuma katsayıları."""
    k_heat: float = 0.032       # °C/saat başına radyatör fazlası (T_gidiş - T_oda)
    k_loss: float = 0.015       # °C/saat başına dış hava farkı (T_oda - T_dış)
    heat_samples: int = 0
    loss_samples: int = 0

    def predicted_cooling_rate(self, indoor_temp_c: float, outside_temp_c: Optional[float]) -> float:
        out = 10.0 if outside_temp_c is None else outside_temp_c
        return self.k_loss * max(0.0, indoor_temp_c - out)

    def predicted_net_heating_rate(
        self,
        supply_temp_c: float,
        indoor_temp_c: float,
        outside_temp_c: Optional[float],
    ) -> float:
        gain = self.k_heat * max(0.0, supply_temp_c - indoor_temp_c)
        loss = self.predicted_cooling_rate(indoor_temp_c, outside_temp_c)
        return max(0.15, gain - loss)

    def estimate_minutes_to_raise(
        self,
        delta_temp_c: float,
        indoor_temp_c: float,
        outside_temp_c: Optional[float],
        supply_temp_c: float = 46.0,
    ) -> int:
        if delta_temp_c <= 0.0:
            return 0
        rate = self.predicted_net_heating_rate(supply_temp_c, indoor_temp_c, outside_temp_c)
        minutes = (delta_temp_c / rate) * 60.0
        return int(round(max(15.0, min(240.0, minutes))))


@dataclass(frozen=True)
class PIConfig:
    comfort_temp_c: float = 22.0
    away_temp_c: float = 20.0
    away_start_time: str = "08:00"
    comfort_ready_time: str = "17:30"
    kp: float = 2.6
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
    estimated_preheat_minutes: int
    reason: str


def _parse_hhmm_minutes(raw: str, default_minutes: int) -> int:
    try:
        hh, mm = raw.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return default_minutes


def resolve_schedule_target(
    now_utc: datetime,
    comfort_temp_c: float,
    away_temp_c: float,
    indoor_temp_c: Optional[float],
    away_start_time: str = "08:00",
    comfort_ready_time: str = "17:30",
    outside_temp_c: Optional[float] = None,
    thermal_profile: Optional[ThermalProfile] = None,
) -> tuple[float, str, int]:
    """Hafta içi ekonomi + öğrenilen ısıl profile göre ön ısıtma ve coast-down kararı."""
    profile = thermal_profile or ThermalProfile()
    local_dt = now_utc.astimezone(ISTANBUL_TZ)
    weekday = local_dt.weekday()  # 0=Pzt .. 4=Cum, 5=Cmt, 6=Paz

    cur_temp = indoor_temp_c if indoor_temp_c is not None else away_temp_c
    needed_min = profile.estimate_minutes_to_raise(
        delta_temp_c=max(0.0, comfort_temp_c - cur_temp),
        indoor_temp_c=cur_temp,
        outside_temp_c=outside_temp_c,
        supply_temp_c=46.0,
    )

    if weekday >= 5:
        return comfort_temp_c, "COMFORT_WEEKEND", needed_min

    minutes = local_dt.hour * 60 + local_dt.minute
    start_away = _parse_hhmm_minutes(away_start_time, 8 * 60)
    end_away = _parse_hhmm_minutes(comfort_ready_time, 17 * 60 + 30)

    if start_away < end_away and start_away <= minutes < end_away:
        remaining_min = end_away - minutes
        if indoor_temp_c is not None and indoor_temp_c < (comfort_temp_c - 0.1):
            if remaining_min <= needed_min:
                return comfort_temp_c, "PREHEAT", needed_min
        return away_temp_c, "AWAY", needed_min

    # Evden çıkmaya (away_start_time) 30 dk veya daha az kaldıysa ve ev süzülerek ekonomi hedefinin üstünde kalacaksa coast-down yap
    if start_away < end_away and (start_away - 30) <= minutes < start_away and indoor_temp_c is not None:
        mins_to_leave = start_away - minutes
        expected_drop = profile.predicted_cooling_rate(indoor_temp_c, outside_temp_c) * (mins_to_leave / 60.0)
        if (indoor_temp_c - expected_drop) >= (away_temp_c + 0.2):
            return away_temp_c, "COAST_DOWN", needed_min

    return comfort_temp_c, "COMFORT", needed_min


def _physics_feedforward_supply(
    indoor_temp_c: float,
    error_c: float,
    effective_outside_c: Optional[float],
    profile: ThermalProfile,
) -> float:
    """Öğrenilmiş k_heat ve k_loss ile gereken gidiş suyu sıcaklığını hesaplar."""
    out = 10.0 if effective_outside_c is None else effective_outside_c
    heat_loss = profile.predicted_cooling_rate(indoor_temp_c, out)
    # Hedef farka orantılı yumuşak istenen ısınma hızı: büyük farkta en fazla ~0.65°C/sa, küçük farkta ~0.10°C/sa
    desired_net_rate = max(0.0, min(0.65, error_c * 0.35))
    required_supply = indoor_temp_c + (desired_net_rate + heat_loss) / max(0.012, profile.k_heat)
    # Dış havaya göre hafif emniyet tabanı
    weather_floor = 39.0 + max(0.0, (14.0 - out) * 0.45)
    return 0.6 * required_supply + 0.4 * weather_floor


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
    thermal_profile: Optional[ThermalProfile] = None,
) -> ControlDecision:
    """Adaptif ısıl model, dış hava tahmini ve güneş kazancına göre kazan gidiş suyunu hesaplar."""
    profile = thermal_profile or ThermalProfile()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    effective_outside = (
        0.7 * outside_temp_c + 0.3 * min_next_3h_outside_c
        if (outside_temp_c is not None and min_next_3h_outside_c is not None)
        else outside_temp_c
    )

    active_target, schedule_mode, est_preheat_min = resolve_schedule_target(
        now_utc=now,
        comfort_temp_c=config.comfort_temp_c,
        away_temp_c=config.away_temp_c,
        indoor_temp_c=indoor_temp_c,
        away_start_time=config.away_start_time,
        comfort_ready_time=config.comfort_ready_time,
        outside_temp_c=effective_outside,
        thermal_profile=profile,
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
            estimated_preheat_minutes=est_preheat_min,
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
            estimated_preheat_minutes=est_preheat_min,
            reason=f"Sensör verisi {int(age_seconds)} sn eski (> {config.stale_data_seconds} sn); fail-safe aktif.",
        )

    error = active_target - indoor_temp_c

    # 2. Güneş / Aşırı Isınma Koruması veya Coast-Down -> Kombi 30°C tabana çekilir
    solar_active = (solar_wm2 is not None and solar_wm2 >= 300.0)
    early_solar_or_rising_cutoff = error <= -0.15 and (indoor_trend_c_per_h >= 0.15 or solar_active)
    if schedule_mode == "COAST_DOWN" or error <= -config.idle_deadband_c or early_solar_or_rising_cutoff:
        decayed_integral = max(0.0, state.integral_error * 0.5)
        return ControlDecision(
            active_target_room_temp_c=active_target,
            schedule_mode=schedule_mode,
            target_supply_temp_c=config.idle_supply_temp_c,
            fail_safe_active=False,
            heating_active=False,
            error_c=error,
            new_integral_error=decayed_integral,
            estimated_preheat_minutes=est_preheat_min,
            reason=(
                f"[{schedule_mode}] Oda {indoor_temp_c:.2f}°C (hedef {active_target:.1f}°C, "
                f"trend {indoor_trend_c_per_h:+.2f}°C/sa); {config.idle_supply_temp_c:.0f}°C tabana çekildi."
            ),
        )

    # 3. Adaptif Feedforward + Yumuşak PI + Trend/Güneş Sönümleme
    if state.updated_at is not None:
        last_dt = state.updated_at if state.updated_at.tzinfo else state.updated_at.replace(tzinfo=timezone.utc)
        dt = max(1.0, min(1800.0, (now - last_dt).total_seconds()))
    else:
        dt = 300.0

    raw_integral = state.integral_error + (error * dt)
    clamped_integral = max(-config.integral_limit, min(config.integral_limit, raw_integral))

    base_supply = _physics_feedforward_supply(indoor_temp_c, error, effective_outside, profile)
    p_term = config.kp * error
    i_term = config.ki * clamped_integral
    trend_adj = -config.trend_damping * max(0.0, indoor_trend_c_per_h)

    solar_adj = 0.0
    if max_next_3h_solar_wm2 is not None and max_next_3h_solar_wm2 >= 350.0 and error < 1.0:
        solar_adj = -1.5

    raw_supply = base_supply + p_term + i_term + trend_adj + solar_adj

    # 4. Kısa Döngü Koruması + Yoğuşma Tavanı
    bounded_supply = max(
        config.min_active_supply_temp_c,
        min(config.max_supply_temp_c, raw_supply),
    )

    # 5. Rampa Hızı Sınırlama (Döngü başına ±2°C)
    if state.last_output_supply_temp_c >= config.min_active_supply_temp_c:
        delta = bounded_supply - state.last_output_supply_temp_c
        if abs(delta) > config.max_step_change_c:
            bounded_supply = state.last_output_supply_temp_c + (
                config.max_step_change_c if delta > 0 else -config.max_step_change_c
            )

    final_supply = float(round(bounded_supply))

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
        estimated_preheat_minutes=est_preheat_min,
        reason=(
            f"[{schedule_mode}] ModelBaz={base_supply:.1f}°C (k_heat={profile.k_heat:.3f}, "
            f"k_loss={profile.k_loss:.3f}), P={p_term:+.2f}, I={i_term:+.2f}, "
            f"Trend={trend_adj:+.2f}, Güneş={solar_adj:+.1f} -> {final_supply:.0f}°C"
        ),
    )
