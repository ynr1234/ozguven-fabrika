"""FastAPI /kombi kontrol paneli ve cronjob uç noktaları."""

import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .db import (
    estimate_indoor_trend_c_per_hour,
    get_recent_logs,
    learn_thermal_profile,
    load_control_settings,
    update_user_settings,
)
from .service import run_cycle_if_due

router = APIRouter()
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

COOKIE_NAME = "kombi_auth"
TR_TZ = timezone(timedelta(hours=3))


def _admin_pin() -> str:
    return os.getenv("KOMBI_ADMIN_PIN", "1234").strip()


def _sign_token(pin: str) -> str:
    secret = os.getenv("KOMBI_COOKIE_SECRET", f"kombi-secret-{pin}").encode("utf-8")
    return hmac.new(secret, pin.encode("utf-8"), hashlib.sha256).hexdigest()


def _is_authenticated(request: Request, pin_param: Optional[str] = None) -> bool:
    expected_pin = _admin_pin()
    if pin_param and hmac.compare_digest(pin_param.strip(), expected_pin):
        return True
    header_pin = request.headers.get("X-Kombi-Pin", "").strip()
    if header_pin and hmac.compare_digest(header_pin, expected_pin):
        return True
    cookie_val = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie_val) and hmac.compare_digest(cookie_val, _sign_token(expected_pin))


def _format_tr_time(dt: Optional[datetime]) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TR_TZ).strftime("%d.%m.%Y %H:%M")


def _valid_hhmm(raw: str, fallback: str) -> str:
    val = raw.strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", val):
        return val
    return fallback


def _build_dashboard_context(message: Optional[str] = None) -> Dict[str, Any]:
    settings = load_control_settings()
    logs = get_recent_logs(limit=24)
    boiler = settings.last_boiler_json or {}
    trend = estimate_indoor_trend_c_per_hour(90)
    profile = learn_thermal_profile()

    cur_in = settings.last_sensor_temp_c if settings.last_sensor_temp_c is not None else settings.away_temp_c
    est_2c_min = profile.estimate_minutes_to_raise(
        delta_temp_c=2.0,
        indoor_temp_c=cur_in,
        outside_temp_c=settings.last_outside_temp_c,
        supply_temp_c=46.0,
    )
    est_net_rate = profile.predicted_net_heating_rate(
        supply_temp_c=46.0,
        indoor_temp_c=cur_in,
        outside_temp_c=settings.last_outside_temp_c,
    )
    est_cool_rate = profile.predicted_cooling_rate(
        indoor_temp_c=cur_in,
        outside_temp_c=settings.last_outside_temp_c,
    )

    formatted_logs = []
    for row in logs:
        formatted_logs.append(
            {
                "time": _format_tr_time(row.get("recorded_at")),
                "indoor": row.get("indoor_temp_c"),
                "target_room": row.get("target_room_temp_c"),
                "outside": row.get("outside_temp_c"),
                "supply": row.get("boiler_supply_temp_c"),
                "target_supply": row.get("target_supply_temp_c"),
                "burner": row.get("burner_active"),
                "mod": row.get("modulation_pct"),
                "mode": row.get("schedule_mode") or "-",
            }
        )

    return {
        "authenticated": True,
        "message": message,
        "settings": settings,
        "boiler": boiler,
        "trend": trend,
        "profile": profile,
        "est_2c_min": est_2c_min,
        "est_net_rate": est_net_rate,
        "est_cool_rate": est_cool_rate,
        "last_update_tr": _format_tr_time(settings.pi_state.updated_at),
        "last_sensor_tr": _format_tr_time(settings.last_sensor_at),
        "logs": formatted_logs,
    }


@router.get("/kombi", response_class=HTMLResponse)
def kombi_dashboard(request: Request, msg: Optional[str] = None) -> HTMLResponse:
    if not _is_authenticated(request):
        return templates.TemplateResponse(
            request=request,
            name="kombi.html",
            context={"authenticated": False, "error": msg},
        )
    ctx = _build_dashboard_context(message=msg)
    return templates.TemplateResponse(request=request, name="kombi.html", context=ctx)


@router.post("/kombi/login")
def kombi_login(pin: str = Form(...)) -> RedirectResponse:
    if not hmac.compare_digest(pin.strip(), _admin_pin()):
        return RedirectResponse(url="/kombi?msg=Hatal%C4%B1%20%C5%9Fifre", status_code=303)
    resp = RedirectResponse(url="/kombi", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=_sign_token(_admin_pin()),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return resp


@router.post("/kombi/logout")
def kombi_logout() -> RedirectResponse:
    resp = RedirectResponse(url="/kombi", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.post("/kombi/settings")
def kombi_update_settings(
    request: Request,
    automation_enabled: Optional[str] = Form(None),
    comfort_temp_c: float = Form(...),
    away_temp_c: float = Form(...),
    away_start_time: str = Form("08:00"),
    comfort_ready_time: str = Form("17:30"),
) -> RedirectResponse:
    if not _is_authenticated(request):
        return RedirectResponse(url="/kombi?msg=Oturum%20gerekli", status_code=303)

    comfort = max(16.0, min(26.0, round(comfort_temp_c, 1)))
    away = max(15.0, min(24.0, round(away_temp_c, 1)))
    enabled = automation_enabled == "on"
    start_t = _valid_hhmm(away_start_time, "08:00")
    ready_t = _valid_hhmm(comfort_ready_time, "17:30")

    update_user_settings(
        automation_enabled=enabled,
        comfort_temp_c=comfort,
        away_temp_c=away,
        away_start_time=start_t,
        comfort_ready_time=ready_t,
    )
    return RedirectResponse(url="/kombi?msg=Ayarlar%20kaydedildi", status_code=303)


@router.post("/kombi/run-now")
def kombi_run_now(request: Request, dry_run: Optional[str] = Form(None)) -> RedirectResponse:
    if not _is_authenticated(request):
        return RedirectResponse(url="/kombi?msg=Oturum%20gerekli", status_code=303)
    is_dry = dry_run == "on"
    run_cycle_if_due(force=True, dry_run=is_dry)
    note = "Kuru test tamamlandı" if is_dry else "Kontrol döngüsü çalıştırıldı"
    return RedirectResponse(url=f"/kombi?msg={note}", status_code=303)


@router.get("/api/kombi/tick")
@router.post("/api/kombi/tick")
def kombi_cron_tick(
    request: Request,
    pin: Optional[str] = Query(None),
    force: bool = Query(False),
    dry_run: bool = Query(False),
) -> JSONResponse:
    if not _is_authenticated(request, pin_param=pin):
        return JSONResponse({"error": "Yetkisiz erişim (PIN geçersiz)."}, status_code=401)
    result = run_cycle_if_due(force=force, dry_run=dry_run)
    return JSONResponse(result)


@router.get("/api/kombi/status")
def kombi_status_api(request: Request, pin: Optional[str] = Query(None)) -> JSONResponse:
    if not _is_authenticated(request, pin_param=pin):
        return JSONResponse({"error": "Yetkisiz erişim (PIN geçersiz)."}, status_code=401)
    settings = load_control_settings()
    profile = learn_thermal_profile()
    return JSONResponse(
        {
            "automation_enabled": settings.automation_enabled,
            "comfort_temp_c": settings.comfort_temp_c,
            "away_temp_c": settings.away_temp_c,
            "away_start_time": settings.away_start_time,
            "comfort_ready_time": settings.comfort_ready_time,
            "active_target_room_temp_c": settings.target_room_temp_c,
            "indoor_temp_c": settings.last_sensor_temp_c,
            "outside_temp_c": settings.last_outside_temp_c,
            "solar_wm2": settings.last_solar_wm2,
            "schedule_mode": settings.last_schedule_mode,
            "decision_reason": settings.last_decision_reason,
            "last_output_supply_temp_c": settings.pi_state.last_output_supply_temp_c,
            "k_heat": profile.k_heat,
            "k_loss": profile.k_loss,
            "heat_samples": profile.heat_samples,
            "loss_samples": profile.loss_samples,
            "boiler": settings.last_boiler_json,
        }
    )
