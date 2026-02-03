"""Data update coordinator for the Tarif EDF integration."""
from __future__ import annotations
import asyncio
import csv
from datetime import timedelta, datetime, date
import logging
import re
from typing import Any
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_REFRESH_INTERVAL,
    CONTRACT_TYPE_BASE,
    CONTRACT_TYPE_HPHC,
    CONTRACT_TYPE_TEMPO,
    TARIF_BASE_URL,
    TARIF_HPHC_URL,
    TARIF_TEMPO_URL,
    TEMPO_COLOR_API_URL,
    TEMPO_COLORS_MAPPING,
    TEMPO_DAY_START_AT,
    TEMPO_TOMRROW_AVAILABLE_AT,
    TEMPO_OFFPEAK_HOURS,
)

_LOGGER = logging.getLogger(__name__)

# HTTP request timeout in seconds
HTTP_TIMEOUT = 30

# User agent required by data.gouv.fr (returns 403 without it)
USER_AGENT = "HomeAssistant-TarifEDF/3.0"


def str_to_time(time_str: str) -> datetime.time:
    """Convert HH:MM string to time object."""
    return datetime.strptime(time_str, "%H:%M").time()


def str_to_date(date_str: str) -> date:
    """Convert DD/MM/YYYY string to date object."""
    return datetime.strptime(date_str, "%d/%m/%Y").date()


def time_in_between(
    now: datetime.time, start: datetime.time, end: datetime.time
) -> bool:
    """Check if current time is between start and end, handling midnight crossover."""
    if start <= end:
        return start <= now < end
    else:
        return start <= now or now < end


def get_tempo_color_from_code(code: int) -> str:
    """Get tempo color name from code."""
    return TEMPO_COLORS_MAPPING.get(code, "indéterminé")


class TarifEdfDataUpdateCoordinator(TimestampDataUpdateCoordinator):
    """Data update coordinator for the Tarif EDF integration."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass=hass,
            logger=_LOGGER,
            name=entry.title,
            update_interval=timedelta(minutes=1),
        )
        self.config_entry = entry
        self.tempo_prices: list[dict] = []
        self._session = async_get_clientsession(hass)
        # Store the previous day's "demain" color for fallback
        self._previous_demain_color: str | None = None
        self._previous_demain_date: date | None = None

    async def _async_fetch_url(self, url: str, as_json: bool = False) -> bytes | dict:
        """Fetch URL content using the shared aiohttp session."""
        headers = {"User-Agent": USER_AGENT}
        async with asyncio.timeout(HTTP_TIMEOUT):
            async with self._session.get(
                url, headers=headers, allow_redirects=True
            ) as response:
                response.raise_for_status()
                if as_json:
                    return await response.json()
                return await response.read()

    async def get_tempo_day(self, target_date: date) -> dict:
        """Fetch Tempo color for a specific date."""
        date_str = target_date.strftime("%Y-%m-%d")
        now = dt_util.now().time()
        check_limit = str_to_time(TEMPO_TOMRROW_AVAILABLE_AT)

        # Check cache first
        for price in self.tempo_prices:
            code_jour = price.get("codeJour", 0)
            if price.get("dateJour") == date_str:
                # Return cached if color is known, or if unknown and before availability time
                if code_jour in [1, 2, 3] or (code_jour == 0 and now < check_limit):
                    return price

        # Fetch from API
        url = f"{TEMPO_COLOR_API_URL}/{date_str}"
        try:
            response_json = await self._async_fetch_url(url, as_json=True)

            # Validate response
            if not isinstance(response_json, dict):
                _LOGGER.warning(
                    "Invalid Tempo API response for %s: not a dict", date_str
                )
                response_json = {"dateJour": date_str, "codeJour": 0}
            elif "codeJour" not in response_json:
                _LOGGER.warning(
                    "Invalid Tempo API response for %s: missing codeJour", date_str
                )
                response_json = {"dateJour": date_str, "codeJour": 0}

            # Ensure dateJour is set
            if "dateJour" not in response_json:
                response_json["dateJour"] = date_str

            # Cache the result
            self.tempo_prices.append(response_json)

            return response_json

        except aiohttp.ClientError as err:
            _LOGGER.warning("Error fetching Tempo color for %s: %s", date_str, err)
            # Return a default response
            return {"dateJour": date_str, "codeJour": 0}
        except Exception as err:
            _LOGGER.warning(
                "Unexpected error fetching Tempo color for %s: %s", date_str, err
            )
            return {"dateJour": date_str, "codeJour": 0}

    def _parse_tariff_csv(
        self, content: bytes, contract_power: str, contract_type: str
    ) -> dict | None:
        """Parse tariff CSV and return the applicable rates based on current date."""
        today = dt_util.now().date()
        decoded = content.decode("utf-8").splitlines()
        reader = csv.DictReader(decoded, delimiter=";")

        best_match = None
        best_date = None

        for row in reader:
            try:
                # Skip rows with empty date or wrong power
                date_debut = row.get("DATE_DEBUT", "").strip()
                # Try both column names: P_SOUSCRITE (TEMPO) and PUISSANCE (BASE/HPHC)
                puissance = row.get("P_SOUSCRITE", row.get("PUISSANCE", "")).strip()

                if not date_debut or puissance != contract_power:
                    continue

                # Parse start date
                start_date = str_to_date(date_debut)

                # Skip if tariff not yet effective
                if start_date > today:
                    continue

                # Parse end date if present
                date_fin = row.get("DATE_FIN", "").strip()
                if date_fin:
                    end_date = str_to_date(date_fin)
                    # Skip if tariff has expired
                    if end_date < today:
                        continue

                # Select the most recent applicable tariff
                if best_date is None or start_date > best_date:
                    best_date = start_date
                    best_match = row

            except (ValueError, KeyError) as err:
                _LOGGER.debug("Skipping row due to parse error: %s", err)
                continue

        if best_match is None:
            return None

        # Extract rates based on contract type
        result = {}

        def parse_price(value: str) -> float:
            """Parse price string to float."""
            if not value:
                return 0.0
            return float(value.replace(",", "."))

        if contract_type == CONTRACT_TYPE_BASE:
            result["base_fixe_ttc"] = parse_price(best_match.get("PART_FIXE_TTC", "0"))
            result["base_variable_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_TTC", "0")
            )
            # Subscription is annual, divide by 12 for monthly
            result["base_abonnement_ttc"] = result["base_fixe_ttc"] / 12

        elif contract_type == CONTRACT_TYPE_HPHC:
            result["hphc_fixe_ttc"] = parse_price(best_match.get("PART_FIXE_TTC", "0"))
            result["hphc_variable_hc_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HC_TTC", "0")
            )
            result["hphc_variable_hp_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HP_TTC", "0")
            )
            # Subscription is annual, divide by 12 for monthly
            result["hphc_abonnement_ttc"] = result["hphc_fixe_ttc"] / 12

        elif contract_type == CONTRACT_TYPE_TEMPO:
            result["tempo_fixe_ttc"] = parse_price(best_match.get("PART_FIXE_TTC", "0"))
            result["tempo_variable_hc_bleu_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HCBleu_TTC", "0")
            )
            result["tempo_variable_hp_bleu_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HPBleu_TTC", "0")
            )
            result["tempo_variable_hc_blanc_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HCBlanc_TTC", "0")
            )
            result["tempo_variable_hp_blanc_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HPBlanc_TTC", "0")
            )
            result["tempo_variable_hc_rouge_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HCRouge_TTC", "0")
            )
            result["tempo_variable_hp_rouge_ttc"] = parse_price(
                best_match.get("PART_VARIABLE_HPRouge_TTC", "0")
            )
            # Subscription is annual, divide by 12 for monthly
            result["tempo_abonnement_ttc"] = result["tempo_fixe_ttc"] / 12

        return result

    def _get_color_code_from_name(self, color_name: str) -> int:
        """Get tempo color code from name."""
        for code, name in TEMPO_COLORS_MAPPING.items():
            if name == color_name:
                return code
        return 0

    async def _async_update_data(self) -> dict[str, Any]:
        """Get the latest data from Tarif EDF and updates the state."""
        data = self.config_entry.data
        contract_type = data["contract_type"]
        contract_power = data["contract_power"]

        if self.data is None:
            self.data = {
                "contract_power": contract_power,
                "contract_type": contract_type,
                "last_refresh_at": None,
                "tarif_actuel_ttc": None,
            }

        refresh_interval = self.config_entry.options.get(
            "refresh_interval", DEFAULT_REFRESH_INTERVAL
        )
        fresh_data_limit = dt_util.now() - timedelta(days=refresh_interval)
        tarif_needs_update = (
            self.data.get("last_refresh_at") is None
            or self.data["last_refresh_at"] < fresh_data_limit
        )

        _LOGGER.debug(
            "EDF tarif_needs_update: %s", "yes" if tarif_needs_update else "no"
        )

        if tarif_needs_update:
            # Select URL based on contract type
            if contract_type == CONTRACT_TYPE_BASE:
                url = TARIF_BASE_URL
            elif contract_type == CONTRACT_TYPE_HPHC:
                url = TARIF_HPHC_URL
            elif contract_type == CONTRACT_TYPE_TEMPO:
                url = TARIF_TEMPO_URL
            else:
                raise UpdateFailed(f"Unknown contract type: {contract_type}")

            try:
                content = await self._async_fetch_url(url)
                rates = self._parse_tariff_csv(content, contract_power, contract_type)

                if rates:
                    self.data.update(rates)
                    self.data["last_refresh_at"] = dt_util.now()
                else:
                    _LOGGER.warning(
                        "No matching tariff found for power %s kVA", contract_power
                    )

            except aiohttp.ClientError as err:
                _LOGGER.error("Error fetching tariff data: %s", err)
                raise UpdateFailed(f"Error fetching tariff data: {err}") from err
            except Exception as err:
                _LOGGER.error("Unexpected error fetching tariff data: %s", err)
                raise UpdateFailed(f"Unexpected error: {err}") from err

        # Handle Tempo-specific data
        if contract_type == CONTRACT_TYPE_TEMPO:
            today_date = dt_util.now().date()
            yesterday = today_date - timedelta(days=1)
            tomorrow = today_date + timedelta(days=1)

            try:
                tempo_yesterday = await self.get_tempo_day(yesterday)
                tempo_today = await self.get_tempo_day(today_date)
                tempo_tomorrow = await self.get_tempo_day(tomorrow)

                yesterday_color = get_tempo_color_from_code(
                    tempo_yesterday.get("codeJour", 0)
                )
                today_color = get_tempo_color_from_code(tempo_today.get("codeJour", 0))
                tomorrow_color = get_tempo_color_from_code(
                    tempo_tomorrow.get("codeJour", 0)
                )

                self.data["tempo_couleur_hier"] = yesterday_color
                self.data["tempo_couleur_demain"] = tomorrow_color

                # FIX: Handle "indéterminé" for today's color by using previous day's "demain" value
                # If today's color is unknown (codeJour = 0), check if we have a stored
                # "demain" value from yesterday that corresponds to today
                if today_color == "indéterminé":
                    # Check if we have a valid fallback from the previous day's "demain"
                    if (
                        self._previous_demain_date == today_date
                        and self._previous_demain_color is not None
                        and self._previous_demain_color != "indéterminé"
                    ):
                        _LOGGER.debug(
                            "Using previous day's 'demain' value (%s) for today's color",
                            self._previous_demain_color
                        )
                        today_color = self._previous_demain_color
                    # Also check if we have it stored in self.data from a previous run
                    elif (
                        self.data.get("_fallback_today_color") is not None
                        and self.data.get("_fallback_today_date") == today_date.isoformat()
                    ):
                        _LOGGER.debug(
                            "Using stored fallback value (%s) for today's color",
                            self.data["_fallback_today_color"]
                        )
                        today_color = self.data["_fallback_today_color"]

                self.data["tempo_couleur_aujourdhui"] = today_color

                # Store tomorrow's color for use as fallback the next day
                # Only store if it's a valid color (not "indéterminé")
                if tomorrow_color != "indéterminé":
                    self._previous_demain_color = tomorrow_color
                    self._previous_demain_date = tomorrow
                    # Also persist in data dict for survival across restarts
                    self.data["_fallback_today_color"] = tomorrow_color
                    self.data["_fallback_today_date"] = tomorrow.isoformat()

                # Determine current color based on time of day
                # Before 06:00, use yesterday's color; after 06:00, use today's color
                if dt_util.now().time() >= str_to_time(TEMPO_DAY_START_AT):
                    _LOGGER.debug("Using today's tempo prices")
                    # Use the already-resolved today_color (which includes fallback logic)
                    current_color_code = self._get_color_code_from_name(today_color)
                else:
                    _LOGGER.debug("Using yesterday's tempo prices")
                    current_color_code = tempo_yesterday.get("codeJour", 0)

                # Set current Tempo rates
                if current_color_code in [1, 2, 3]:
                    color = get_tempo_color_from_code(current_color_code)
                    self.data["tempo_couleur"] = color

                    hp_key = f"tempo_variable_hp_{color}_ttc"
                    hc_key = f"tempo_variable_hc_{color}_ttc"

                    if hp_key in self.data and hc_key in self.data:
                        self.data["tempo_variable_hp_ttc"] = self.data[hp_key]
                        self.data["tempo_variable_hc_ttc"] = self.data[hc_key]
                else:
                    # Color not yet determined
                    self.data["tempo_couleur"] = "indéterminé"
                    # Keep previous values if available, otherwise set to None
                    if "tempo_variable_hp_ttc" not in self.data:
                        self.data["tempo_variable_hp_ttc"] = None
                    if "tempo_variable_hc_ttc" not in self.data:
                        self.data["tempo_variable_hc_ttc"] = None

            except Exception as err:
                _LOGGER.error("Error fetching Tempo colors: %s", err)
                # Set defaults to prevent KeyError
                self.data.setdefault("tempo_couleur", "indéterminé")
                self.data.setdefault("tempo_couleur_hier", "indéterminé")
                self.data.setdefault("tempo_couleur_aujourdhui", "indéterminé")
                self.data.setdefault("tempo_couleur_demain", "indéterminé")
                self.data.setdefault("tempo_variable_hp_ttc", None)
                self.data.setdefault("tempo_variable_hc_ttc", None)

        # Calculate current tariff based on time of day
        default_offpeak_hours = None
        if contract_type == CONTRACT_TYPE_TEMPO:
            default_offpeak_hours = TEMPO_OFFPEAK_HOURS

        off_peak_hours_ranges = self.config_entry.options.get(
            "off_peak_hours_ranges", default_offpeak_hours
        )

        if contract_type == CONTRACT_TYPE_BASE:
            self.data["tarif_actuel_ttc"] = self.data.get("base_variable_ttc")
            self.data["is_off_peak"] = False  # BASE has no off-peak

        elif contract_type in [CONTRACT_TYPE_HPHC, CONTRACT_TYPE_TEMPO]:
            contract_type_key = (
                "hphc" if contract_type == CONTRACT_TYPE_HPHC else "tempo"
            )
            hp_key = f"{contract_type_key}_variable_hp_ttc"
            hc_key = f"{contract_type_key}_variable_hc_ttc"

            # Default to peak rate
            tarif_actuel = self.data.get(hp_key)
            is_off_peak = False

            # Check if currently in off-peak hours
            if off_peak_hours_ranges:
                now = dt_util.now().time()

                for time_range in off_peak_hours_ranges.split(","):
                    time_range = time_range.strip()
                    if not re.match(
                        r"([0-1]?[0-9]|2[0-3]):[0-5][0-9]-([0-1]?[0-9]|2[0-3]):[0-5][0-9]",
                        time_range,
                    ):
                        continue

                    hours = time_range.split("-")
                    start_at = str_to_time(hours[0])
                    end_at = str_to_time(hours[1])

                    if time_in_between(now, start_at, end_at):
                        tarif_actuel = self.data.get(hc_key)
                        is_off_peak = True
                        break

            self.data["tarif_actuel_ttc"] = tarif_actuel
            self.data["is_off_peak"] = is_off_peak

        _LOGGER.debug("EDF Tarif data: %s", self.data)

        return self.data
