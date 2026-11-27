"""Midea SmartHome Cloud üzerinden klima oda sıcaklığı okuma istemcisi.

midealocal.cloud.SmartHomeCloud (güncel _re_route + Basic auth + iampwd)
ve /v1/device/status/lua/get uç noktasını kullanır.
"""

import argparse
import asyncio
import getpass
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv
from midealocal.cloud import SmartHomeCloud

AC_DEVICE_TYPE = 0xAC


@dataclass(frozen=True)
class MideaACReading:
    appliance_code: int
    name: str
    online: bool
    indoor_temp_c: Optional[float]
    outdoor_temp_c: Optional[float]
    target_temp_c: Optional[float]
    power_on: Optional[bool]
    mode: Optional[str]
    recorded_at: datetime


class MideaCloudReader:
    """SmartHome bulutu üzerinden Midea klima sensörlerini okur."""

    def __init__(
        self,
        account: Optional[str] = None,
        password: Optional[str] = None,
        cloud_name: Optional[str] = None,
    ) -> None:
        self.account = (account or os.getenv("MIDEA_ACCOUNT", "")).strip()
        self.password = password or os.getenv("MIDEA_PASSWORD", "")
        self.cloud_name = (cloud_name or os.getenv("MIDEA_APP_NAME", "SmartHome")).strip() or "SmartHome"

        if not self.account or not self.password:
            raise RuntimeError("MIDEA_ACCOUNT ve MIDEA_PASSWORD tanımlı olmalıdır.")

    async def _login_and_list(self, session: aiohttp.ClientSession) -> tuple[SmartHomeCloud, Dict[int, Dict[str, Any]]]:
        cloud = SmartHomeCloud(
            cloud_name=self.cloud_name,
            session=session,
            account=self.account,
            password=self.password,
        )
        ok = await cloud.login()
        if not ok:
            raise RuntimeError("Midea SmartHome Cloud girişi başarısız oldu.")

        appliances = await cloud.list_appliances(home_id=None) or {}
        return cloud, appliances

    @staticmethod
    def _parse_valid_temp(raw_val: Any) -> Optional[float]:
        if raw_val is None:
            return None
        try:
            temp = float(raw_val)
        except (TypeError, ValueError):
            return None
        # Midea bazı kapalı durumlarda dış sıcaklık için 102 gibi sentinel değer döner
        if temp <= -50.0 or temp >= 80.0:
            return None
        return temp

    async def _query_ac_lua_status(
        self,
        cloud: SmartHomeCloud,
        appliance_code: int,
    ) -> Optional[Dict[str, Any]]:
        payload = cloud._make_general_data()
        payload.update({"applianceCode": str(appliance_code)})
        return await cloud._api_request(
            endpoint="/v1/device/status/lua/get",
            data=payload,
            raise_for_error=False,
        )

    async def read_all_acs(self) -> List[MideaACReading]:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            cloud, appliances = await self._login_and_list(session)
            results: List[MideaACReading] = []
            now = datetime.now(timezone.utc)

            for code, info in appliances.items():
                if info.get("type") != AC_DEVICE_TYPE:
                    continue

                status: Optional[Dict[str, Any]] = None
                if info.get("online"):
                    try:
                        status = await self._query_ac_lua_status(cloud, code)
                    except Exception as exc:
                        print(f"[UYARI] {info.get('name')} ({code}) durum sorgusu başarısız: {exc}")

                power_raw = status.get("power") if status else None
                power_on = (power_raw == "on") if power_raw in ("on", "off") else None

                results.append(
                    MideaACReading(
                        appliance_code=code,
                        name=str(info.get("name") or code),
                        online=bool(info.get("online")),
                        indoor_temp_c=self._parse_valid_temp(status.get("indoor_temperature")) if status else None,
                        outdoor_temp_c=self._parse_valid_temp(status.get("outdoor_temperature")) if status else None,
                        target_temp_c=self._parse_valid_temp(status.get("temperature")) if status else None,
                        power_on=power_on,
                        mode=str(status.get("mode")) if status and status.get("mode") is not None else None,
                        recorded_at=now,
                    )
                )
            return results


def fetch_ac_readings(
    account: Optional[str] = None,
    password: Optional[str] = None,
    cloud_name: Optional[str] = None,
) -> List[MideaACReading]:
    reader = MideaCloudReader(account=account, password=password, cloud_name=cloud_name)
    return asyncio.run(reader.read_all_acs())


def main() -> None:
    import logging

    load_dotenv()
    parser = argparse.ArgumentParser(description="Midea SmartHome Cloud Klima Okuma Testi")
    parser.add_argument("--account", default=os.getenv("MIDEA_ACCOUNT", ""))
    parser.add_argument("--cloud", default=os.getenv("MIDEA_APP_NAME", "SmartHome"))
    parser.add_argument(
        "--visible-password",
        action="store_true",
        help="Şifreyi ekranda görünür şekilde gir",
    )
    parser.add_argument("--debug", action="store_true", help="Detaylı logları göster")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    account = args.account.strip() or input("Midea SmartHome E-posta: ").strip()
    password = os.getenv("MIDEA_PASSWORD", "")
    if not password:
        if args.visible_password:
            password = input("Midea SmartHome Şifre (görünür): ")
        else:
            print("(Güvenlik gereği yazarken ekranda karakter görünmez; şifreyi yazıp Enter'a basın.)")
            password = getpass.getpass("Midea SmartHome Şifre: ")

    readings = fetch_ac_readings(account=account, password=password, cloud_name=args.cloud)
    if not readings:
        print("Hesapta klima (0xAC) cihazı bulunamadı.")
        return

    for item in readings:
        print(
            f"- {item.name} (ID={item.appliance_code}, online={item.online}): "
            f"oda={item.indoor_temp_c}°C, dis={item.outdoor_temp_c}°C, "
            f"hedef={item.target_temp_c}°C, acik={item.power_on}"
        )


if __name__ == "__main__":
    main()
