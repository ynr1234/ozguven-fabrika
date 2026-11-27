"""Viessmann ViCare API istemcisi ve Neon PostgreSQL tabanlı OAuth2 oturum yöneticisi."""

import base64
import json
import os
import pickle
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from authlib.integrations.requests_client import OAuth2Session
from PyViCare.PyViCare import PyViCare
from PyViCare.PyViCareAbstractOAuthManager import (
    TOKEN_URL,
    AbstractViCareOAuthManager,
)
from PyViCare.PyViCareUtils import PyViCareNotSupportedFeatureError

from .db import load_oauth_token, save_oauth_token


@dataclass(frozen=True)
class BoilerSnapshot:
    device_model: str
    supply_temp_c: Optional[float]
    normal_target_temp_c: Optional[float]
    active_program: Optional[str]
    operating_mode: Optional[str]
    min_temp_c: Optional[float]
    max_temp_c: Optional[float]
    burner_active: Optional[bool]
    modulation_pct: Optional[float]
    burner_hours: Optional[int]
    burner_starts: Optional[int]
    pressure_bar: Optional[float]
    pump_status: Optional[str]
    dhw_temp_c: Optional[float]
    gas_heating_today_m3: Optional[float]
    gas_heating_month_m3: Optional[float]
    heat_heating_today_kwh: Optional[float]
    power_heating_today_kwh: Optional[float]
    malfunction_active: Optional[bool]
    wifi_strength_dbm: Optional[int]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class NeonViCareOAuthManager(AbstractViCareOAuthManager):
    """Token yenilemesini doğrudan authlib refresh_token ile yapıp Neon'a kaydeden yönetici."""

    def __init__(
        self,
        client_id: str,
        token: Dict[str, Any],
        on_token_refreshed: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.client_id = client_id
        self._on_token_refreshed = on_token_refreshed

        def _update_token(new_token: Dict[str, Any], **_kwargs: Any) -> None:
            if self._on_token_refreshed:
                self._on_token_refreshed(new_token)

        session = OAuth2Session(
            client_id=client_id,
            token=token,
            update_token=_update_token,
        )
        super().__init__(session)

    def renewToken(self) -> None:  # type: ignore[override]
        refresh_tok = (self.oauth_session.token or {}).get("refresh_token")
        if not refresh_tok:
            raise RuntimeError("Viessmann token içinde refresh_token bulunamadı.")

        new_token = self.oauth_session.refresh_token(
            TOKEN_URL,
            refresh_token=refresh_tok,
        )
        if self._on_token_refreshed:
            self._on_token_refreshed(dict(new_token))


def decode_token_payload(raw_value: str) -> Dict[str, Any]:
    stripped = raw_value.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    raw_bytes = base64.b64decode(stripped)
    try:
        return json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = pickle.loads(raw_bytes)
        if not isinstance(data, dict):
            raise ValueError("Çözülen pickle token dict formatında değil.")
        return data


def _persist_token(token: Dict[str, Any], provider: str = "viessmann") -> None:
    save_oauth_token(token, provider=provider)


def load_token_with_bootstrap(provider: str = "viessmann") -> Dict[str, Any]:
    token = load_oauth_token(provider=provider)
    if token:
        return token

    bootstrap = os.getenv("VIESSMANN_TOKEN_B64", "").strip()
    if bootstrap:
        token = decode_token_payload(bootstrap)
        _persist_token(token, provider=provider)
        return token

    raise RuntimeError(
        "Neon veritabanında Viessmann token bulunamadı. "
        "Önce token kaydedin veya VIESSMANN_TOKEN_B64 tanımlayın."
    )


def _safe_call(fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except (PyViCareNotSupportedFeatureError, IndexError, KeyError, AttributeError, TypeError):
        return default


class ViessmannClient:
    """Vitodens 050-W okuma ve gidiş suyu sıcaklığı yazma sarmalayıcısı."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        circuit_id: Optional[int] = None,
        device_index: Optional[int] = None,
    ) -> None:
        self.client_id = (client_id or os.getenv("VIESSMANN_CLIENT_ID", "")).strip()
        if not self.client_id:
            raise RuntimeError("VIESSMANN_CLIENT_ID tanımlı değil.")

        self.circuit_id = int(
            circuit_id if circuit_id is not None else os.getenv("VIESSMANN_CIRCUIT_ID", "0")
        )
        self.device_index = int(
            device_index if device_index is not None else os.getenv("VIESSMANN_DEVICE_ID", "0")
        )

        token = load_token_with_bootstrap("viessmann")
        self.oauth_manager = NeonViCareOAuthManager(
            client_id=self.client_id,
            token=token,
            on_token_refreshed=lambda t: _persist_token(t, provider="viessmann"),
        )
        self.vicare = PyViCare()
        self.vicare.setCacheDuration(0)
        self.vicare.initWithExternalOAuth(self.oauth_manager)

        heating_devices = [d for d in self.vicare.devices if d.device_type == "heating"]
        if not heating_devices:
            raise RuntimeError("Viessmann hesabında 'heating' tipinde cihaz bulunamadı.")

        self.device_config = heating_devices[self.device_index]
        self.boiler = self.device_config.asGazBoiler()
        self.circuit = self.boiler.circuits[self.circuit_id]

    def _prop_val(self, feature: str, *keys: str) -> Any:
        try:
            node: Any = self.boiler.getProperty(feature)
            for k in keys:
                node = node[k]
            return node
        except Exception:
            return None

    def read_snapshot(self) -> BoilerSnapshot:
        burner = self.boiler.burners[0] if self.boiler.burners else None
        common_supply = _safe_call(self.boiler.getBoilerCommonSupplyTemperature)
        circuit_supply = _safe_call(self.circuit.getSupplyTemperature)

        return BoilerSnapshot(
            device_model=str(self.device_config.device_model),
            supply_temp_c=common_supply if common_supply is not None else circuit_supply,
            normal_target_temp_c=_safe_call(self.circuit.getNormalTemperature),
            active_program=_safe_call(self.circuit.getActiveProgram),
            operating_mode=_safe_call(self.circuit.getActiveMode),
            min_temp_c=_safe_call(lambda: self.circuit.getProgramMinTemperature("normal")),
            max_temp_c=_safe_call(lambda: self.circuit.getProgramMaxTemperature("normal")),
            burner_active=_safe_call(burner.getActive) if burner else None,
            modulation_pct=_safe_call(burner.getModulation) if burner else None,
            burner_hours=_safe_call(burner.getHours) if burner else None,
            burner_starts=_safe_call(burner.getStarts) if burner else None,
            pressure_bar=_safe_call(self.boiler.getSupplyPressure),
            pump_status=self._prop_val("heating.boiler.pumps.internal", "properties", "status", "value"),
            dhw_temp_c=_safe_call(self.boiler.getDomesticHotWaterOutletTemperature),
            gas_heating_today_m3=self._prop_val("heating.gas.consumption.summary.heating", "properties", "currentDay", "value"),
            gas_heating_month_m3=self._prop_val("heating.gas.consumption.summary.heating", "properties", "currentMonth", "value"),
            heat_heating_today_kwh=self._prop_val("heating.heat.production.summary.heating", "properties", "currentDay", "value"),
            power_heating_today_kwh=self._prop_val("heating.power.consumption.summary.heating", "properties", "currentDay", "value"),
            malfunction_active=self._prop_val("device.lock.malfunction", "properties", "active", "value"),
            wifi_strength_dbm=None,
        )

    def set_normal_supply_temperature(self, target_temp_c: float) -> Any:
        return self.circuit.setNormalTemperature(float(round(target_temp_c)))
