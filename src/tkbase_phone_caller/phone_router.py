"""Country-based phone number routing with auto-provisioning.

Automatically selects the correct local phone number based on the
destination country. If no number exists for a country, it can
optionally provision one via Twilio and import it into Vapi.

Phone number inventory is stored in:
    ~/.config/tkbase/phone_numbers.json

Format:
    {
        "numbers": {
            "+1": {
                "vapi_id": "a1b18b31-...",
                "number": "+12562128499",
                "provider": "vapi",
                "country": "US",
                "label": "US sales"
            },
            "+81": {
                "vapi_id": "...",
                "number": "+815031968784",
                "provider": "twilio",
                "country": "JP",
                "label": "Japan sales"
            }
        },
        "default_prefix": "+1",
        "twilio_account_sid": "",
        "twilio_auth_token": "",
        "auto_provision": false
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

_DEFAULT_CONFIG_DIR = Path.home() / ".config" / "tkbase"
_INVENTORY_FILE = "phone_numbers.json"
_VAPI_BASE_URL = "https://api.vapi.ai"
_TWILIO_BASE_URL = "https://api.twilio.com/2010-04-01"

# Country code prefix to ISO country code mapping
_PREFIX_TO_COUNTRY: dict[str, str] = {
    "+1": "US",
    "+44": "GB",
    "+81": "JP",
    "+49": "DE",
    "+61": "AU",
    "+33": "FR",
    "+82": "KR",
    "+86": "CN",
    "+91": "IN",
    "+65": "SG",
    "+852": "HK",
    "+971": "AE",
    "+972": "IL",
    "+55": "BR",
    "+52": "MX",
    "+31": "NL",
    "+46": "SE",
    "+47": "NO",
    "+358": "FI",
    "+64": "NZ",
}


@dataclass
class PhoneEntry:
    """A phone number entry in the routing table."""
    vapi_id: str
    number: str
    provider: str
    country: str
    label: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "vapi_id": self.vapi_id,
            "number": self.number,
            "provider": self.provider,
            "country": self.country,
            "label": self.label,
        }


class PhoneRouter:
    """Route outbound calls through country-appropriate phone numbers.

    Args:
        inventory_path: Path to phone_numbers.json.
        vapi_api_key: Vapi API key (for provisioning/importing).
        auto_provision: If True, automatically buy numbers for new countries.
    """

    def __init__(
        self,
        inventory_path: str | Path | None = None,
        vapi_api_key: Optional[str] = None,
        auto_provision: bool = False,
    ):
        self._inventory_path = (
            Path(inventory_path) if inventory_path
            else _DEFAULT_CONFIG_DIR / _INVENTORY_FILE
        )
        self._vapi_api_key = vapi_api_key
        self._auto_provision = auto_provision
        self._inventory: dict[str, Any] = {}
        self._load_inventory()

    def _load_inventory(self) -> None:
        """Load phone number inventory from disk."""
        if not self._inventory_path.exists():
            self._inventory = {
                "numbers": {},
                "default_prefix": "+1",
                "twilio_account_sid": "",
                "twilio_auth_token": "",
                "auto_provision": False,
            }
            return
        self._inventory = json.loads(self._inventory_path.read_text())

    def _save_inventory(self) -> None:
        """Persist phone number inventory to disk."""
        self._inventory_path.parent.mkdir(parents=True, exist_ok=True)
        self._inventory_path.write_text(
            json.dumps(self._inventory, ensure_ascii=False, indent=2)
        )

    def _extract_prefix(self, phone_number: str) -> str:
        """Extract country prefix from E.164 number.

        Tries longer prefixes first (e.g. +852 before +8).
        """
        for length in (4, 3, 2):
            prefix = phone_number[:length]
            if prefix in _PREFIX_TO_COUNTRY:
                return prefix
        # Default: first 2 chars
        return phone_number[:2]

    def get_number_for(self, destination: str) -> PhoneEntry | None:
        """Get the best phone number to call a destination.

        Args:
            destination: E.164 phone number to call.

        Returns:
            PhoneEntry for the matching country, or default, or None.
        """
        prefix = self._extract_prefix(destination)
        numbers = self._inventory.get("numbers", {})

        # Exact country match
        if prefix in numbers:
            return PhoneEntry(**numbers[prefix])

        # Fall back to default
        default_prefix = self._inventory.get("default_prefix", "+1")
        if default_prefix in numbers:
            logger.info(
                "router.fallback",
                destination_prefix=prefix,
                using_prefix=default_prefix,
            )
            return PhoneEntry(**numbers[default_prefix])

        return None

    def get_vapi_phone_id(self, destination: str) -> str | None:
        """Get Vapi phone number ID for a destination country."""
        entry = self.get_number_for(destination)
        if entry is None:
            return None
        return entry.vapi_id

    def add_number(
        self,
        prefix: str,
        vapi_id: str,
        number: str,
        provider: str = "vapi",
        country: str = "",
        label: str = "",
    ) -> PhoneEntry:
        """Add a phone number to the routing table.

        Args:
            prefix: Country prefix (e.g. "+1", "+81").
            vapi_id: Vapi phone number ID.
            number: E.164 phone number.
            provider: "vapi", "twilio", "vonage", "telnyx".
            country: ISO country code (auto-detected from prefix if empty).
            label: Display label.
        """
        if not country:
            country = _PREFIX_TO_COUNTRY.get(prefix, "")

        entry = PhoneEntry(
            vapi_id=vapi_id,
            number=number,
            provider=provider,
            country=country,
            label=label,
        )
        self._inventory.setdefault("numbers", {})[prefix] = entry.to_dict()
        self._save_inventory()

        logger.info(
            "router.number_added",
            prefix=prefix,
            number=number,
            country=country,
        )
        return entry

    def remove_number(self, prefix: str) -> bool:
        """Remove a phone number from the routing table."""
        numbers = self._inventory.get("numbers", {})
        if prefix in numbers:
            del numbers[prefix]
            self._save_inventory()
            return True
        return False

    def list_numbers(self) -> dict[str, PhoneEntry]:
        """List all phone numbers in the routing table."""
        return {
            prefix: PhoneEntry(**data)
            for prefix, data in self._inventory.get("numbers", {}).items()
        }

    def set_default(self, prefix: str) -> None:
        """Set the default country prefix for unknown destinations."""
        self._inventory["default_prefix"] = prefix
        self._save_inventory()

    # --- Auto-provisioning via Twilio ---

    async def provision_number(
        self,
        country_code: str,
        area_code: str = "",
    ) -> PhoneEntry | None:
        """Search and buy a number from Twilio, then import to Vapi.

        Args:
            country_code: ISO country code (e.g. "US", "JP", "GB").
            area_code: Preferred area code (optional).

        Returns:
            PhoneEntry if successful, None if failed.
        """
        twilio_sid = self._inventory.get("twilio_account_sid", "")
        twilio_token = self._inventory.get("twilio_auth_token", "")

        if not twilio_sid or not twilio_token:
            logger.error("provision.no_twilio_credentials")
            return None

        # Step 1: Search available numbers
        number = await self._twilio_search_number(
            twilio_sid, twilio_token, country_code, area_code,
        )
        if not number:
            return None

        # Step 2: Buy the number on Twilio
        purchased = await self._twilio_buy_number(
            twilio_sid, twilio_token, number,
        )
        if not purchased:
            return None

        # Step 3: Import to Vapi
        vapi_id = await self._vapi_import_twilio_number(
            number, twilio_sid, twilio_token,
        )
        if not vapi_id:
            return None

        # Step 4: Add to routing table
        prefix = self._number_to_prefix(number)
        entry = self.add_number(
            prefix=prefix,
            vapi_id=vapi_id,
            number=number,
            provider="twilio",
            country=country_code,
            label=f"{country_code} auto-provisioned",
        )
        return entry

    def _number_to_prefix(self, number: str) -> str:
        """Convert E.164 number to its country prefix."""
        for length in (4, 3, 2):
            prefix = number[:length]
            if prefix in _PREFIX_TO_COUNTRY:
                return prefix
        return number[:2]

    async def _twilio_search_number(
        self,
        sid: str,
        token: str,
        country_code: str,
        area_code: str = "",
    ) -> str | None:
        """Search Twilio for available local numbers."""
        url = (
            f"{_TWILIO_BASE_URL}/Accounts/{sid}"
            f"/AvailablePhoneNumbers/{country_code}/Local.json"
        )
        params: dict[str, Any] = {"PageSize": 1, "VoiceEnabled": True}
        if area_code:
            params["AreaCode"] = area_code

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, auth=(sid, token), params=params)
            if resp.status_code != 200:
                logger.error(
                    "twilio.search_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                    country=country_code,
                )
                return None
            data = resp.json()

        numbers = data.get("available_phone_numbers", [])
        if not numbers:
            logger.warning("twilio.no_numbers_available", country=country_code)
            return None

        found = numbers[0]["phone_number"]
        logger.info("twilio.number_found", number=found, country=country_code)
        return found

    async def _twilio_buy_number(
        self,
        sid: str,
        token: str,
        number: str,
    ) -> bool:
        """Purchase a number on Twilio."""
        url = f"{_TWILIO_BASE_URL}/Accounts/{sid}/IncomingPhoneNumbers.json"

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                auth=(sid, token),
                data={"PhoneNumber": number},
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "twilio.buy_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                    number=number,
                )
                return False

        logger.info("twilio.number_purchased", number=number)
        return True

    async def _vapi_import_twilio_number(
        self,
        number: str,
        twilio_sid: str,
        twilio_token: str,
    ) -> str | None:
        """Import a Twilio number into Vapi."""
        api_key = self._vapi_api_key
        if not api_key:
            # Try loading from vapi credentials
            vapi_creds_path = _DEFAULT_CONFIG_DIR / "vapi_credentials.json"
            if vapi_creds_path.exists():
                vapi_creds = json.loads(vapi_creds_path.read_text())
                api_key = vapi_creds.get("api_key", "")

        if not api_key:
            logger.error("vapi.no_api_key")
            return None

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_VAPI_BASE_URL}/phone-number",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "provider": "twilio",
                    "number": number,
                    "twilioAccountSid": twilio_sid,
                    "twilioAuthToken": twilio_token,
                },
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "vapi.import_failed",
                    status=resp.status_code,
                    body=resp.text[:200],
                    number=number,
                )
                return None
            data = resp.json()

        vapi_id = data.get("id", "")
        logger.info("vapi.number_imported", vapi_id=vapi_id, number=number)
        return vapi_id

    # --- Sync Vapi inventory ---

    async def sync_from_vapi(self) -> int:
        """Fetch all phone numbers from Vapi and update local inventory.

        Returns:
            Number of phone numbers synced.
        """
        api_key = self._vapi_api_key
        if not api_key:
            vapi_creds_path = _DEFAULT_CONFIG_DIR / "vapi_credentials.json"
            if vapi_creds_path.exists():
                vapi_creds = json.loads(vapi_creds_path.read_text())
                api_key = vapi_creds.get("api_key", "")

        if not api_key:
            logger.error("vapi.no_api_key")
            return 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{_VAPI_BASE_URL}/phone-number",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"limit": 100},
            )
            resp.raise_for_status()
            numbers = resp.json()

        count = 0
        for num in numbers:
            phone = num.get("number", "")
            if not phone:
                continue
            prefix = self._number_to_prefix(phone)
            country = _PREFIX_TO_COUNTRY.get(prefix, "")
            provider = num.get("provider", "unknown")

            self._inventory.setdefault("numbers", {})[prefix] = {
                "vapi_id": num.get("id", ""),
                "number": phone,
                "provider": provider,
                "country": country,
                "label": num.get("name", ""),
            }
            count += 1

        self._save_inventory()
        logger.info("router.synced", count=count)
        return count
