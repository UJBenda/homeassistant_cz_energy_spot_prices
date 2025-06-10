from __future__ import annotations
import logging
from datetime import datetime, timedelta
from typing import Optional
from decimal import Decimal
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_CURRENCY, CONF_UNIT_OF_MEASUREMENT
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant

from . import SpotRateConfigEntry
from .coordinator import SpotRateCoordinator, SpotRateData, CONSECUTIVE_HOURS
from .spot_rate_mixin import ElectricitySpotRateSensorMixin, GasSpotRateSensorMixin, Trade
from .spot_rate_settings import SpotRateSettings

logger = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SpotRateConfigEntry, async_add_entities):
    logger.info('async_setup_entry %s, data: [%s] options: [%s]', entry.unique_id, entry.data, entry.options)

    coordinator = entry.runtime_data
    currency = entry.data[CONF_CURRENCY]
    unit = entry.data[CONF_UNIT_OF_MEASUREMENT]

    settings = SpotRateSettings(
        currency=currency,
        unit=unit,
        currency_human={
            'EUR': '€',
            'CZK': 'Kč',
            'USD': '$',
        }.get(currency) or '?',
        timezone=hass.config.time_zone,
        zoneinfo=ZoneInfo(hass.config.time_zone),
    )

    has_tomorrow_electricity_data = HasTomorrowElectricityData(
        hass=hass,
        settings=settings,
        coordinator=coordinator,
        trade = Trade.SPOT,
    )

    has_tomorrow_gas_data = HasTomorrowGasData(
        hass=hass,
        settings=settings,
        coordinator=coordinator,
        trade = Trade.SPOT,
    )

    sensors = [
        has_tomorrow_electricity_data,
        has_tomorrow_gas_data
    ]

    for i in CONSECUTIVE_HOURS:
        sensors.append(
            ConsecutiveCheapestElectricitySensor(
                hours=i,
                hass=hass,
                settings=settings,
                coordinator=coordinator,
                trade = Trade.SPOT,
            )
        )

    if coordinator._electricity_buy_rate_template is not None:
        for i in CONSECUTIVE_HOURS:
            sensors.append(
                ConsecutiveCheapestElectricitySensor(
                    hours=i,
                    hass=hass,
                    settings=settings,
                    coordinator=coordinator,
                    trade = Trade.BUY,
                )
            )

    if coordinator._electricity_sell_rate_template is not None:
        for i in CONSECUTIVE_HOURS:
            sensors.append(
                ConsecutiveCheapestElectricitySensor(
                    hours=i,
                    hass=hass,
                    settings=settings,
                    coordinator=coordinator,
                    trade = Trade.SELL,
                )
            )

    CONSECUTIVE_HOURS_EXPENSIVE = (1, 2, 4, 8)

    for i in CONSECUTIVE_HOURS_EXPENSIVE:
        sensors.append(
            ConsecutiveMostExpensiveElectricitySensor(
                hours=i,
                hass=hass,
                settings=settings,
                coordinator=coordinator,
                trade=Trade.SPOT,
            )
        )

    if coordinator._electricity_buy_rate_template is not None:
        for i in CONSECUTIVE_HOURS_EXPENSIVE:
            sensors.append(
                ConsecutiveMostExpensiveElectricitySensor(
                    hours=i,
                    hass=hass,
                    settings=settings,
                    coordinator=coordinator,
                    trade=Trade.BUY,
                )
            )

    if coordinator._electricity_sell_rate_template is not None:
        for i in CONSECUTIVE_HOURS_EXPENSIVE:
            sensors.append(
                ConsecutiveMostExpensiveElectricitySensor(
                    hours=i,
                    hass=hass,
                    settings=settings,
                    coordinator=coordinator,
                    trade=Trade.SELL,
                )
            )

    async_add_entities(sensors)


class ElectricityBinarySpotRateSensorBase(ElectricitySpotRateSensorMixin, BinarySensorEntity):
    pass


class ConsecutiveCheapestElectricitySensor(ElectricityBinarySpotRateSensorBase):
    _attr_icon = 'mdi:cash-clock'

    def __init__(self, hours: int, hass: HomeAssistant, settings: SpotRateSettings, coordinator: SpotRateCoordinator, trade: Trade) -> None:
        self.hours = hours

        if self.hours == 1:
            self._attr_unique_id = f'binary_sensor.{trade.lower()}_electricity_is_cheapest'
            self._attr_translation_key = f'{trade.lower()}_electricity_is_cheapest'
        else:
            self._attr_unique_id = f'binary_sensor.{trade.lower()}_electricity_is_cheapest_{self.hours}_hours_block'
            self._attr_translation_key = f'{trade.lower()}_electricity_is_cheapest_hours_block'
            self._attr_translation_placeholders = {
                'hours': self.hours,
            }

        self.entity_id = self._attr_unique_id

        super().__init__(hass=hass, settings=settings, coordinator=coordinator, trade=trade)

    def _compute_attr(self, rate_data: SpotRateData, start: datetime, end: datetime) -> dict:
        dt = start
        min_price: Optional[Decimal] = None
        max_price: Optional[Decimal] = None
        sum_price: Decimal = Decimal(0)
        count: int = 0

        hourly_rates = self._get_trade_rates(rate_data)
        while dt <= end:
            hour = hourly_rates.hour_for_dt(dt)
            sum_price += hour.price
            count += 1
            if min_price is None or hour.price < min_price:
                min_price = hour.price

            if max_price is None or hour.price > max_price:
                max_price = hour.price

            dt += timedelta(hours=1)
        return {
            'Start': start,
            'Start hour': start.hour,
            'End': end,
            'End hour': end.hour,
            'Min': round(float(min_price or 0), 2),
            'Max': round(float(max_price or 0), 2),
            'Mean': round(float(sum_price / count), 2) if count > 0 else 0,
        }

    def update(self, rate_data: Optional[SpotRateData]):
        self._attr = {}

        if not rate_data:
            self._available = False
            self._attr_is_on = None
            return

        is_on = False
        hourly_rates = self._get_trade_rates(rate_data)
        for hour in hourly_rates.hours_by_dt.values():
            start = hour.dt_local - timedelta(hours=self.hours - 1)
            end = hour.dt_local + timedelta(hours=1, seconds=-1)

            # Ignore start times before now, we only want future blocks
            if end < hourly_rates.now:
                continue

            if hour.cheapest_consecutive_order[self.hours] == 1:
                if not self._attr:
                    # Only put it there once, so to contains closes interval in the future
                    self._attr = self._compute_attr(rate_data, start, end)

                if start <= hourly_rates.now <= end:
                    is_on = True

            self._attr_is_on = is_on
            self._available = True


class ConsecutiveMostExpensiveElectricitySensor(ElectricityBinarySpotRateSensorBase):
    _attr_icon = 'mdi:cash-clock'

    def __init__(self, hours: int, hass: HomeAssistant, settings: SpotRateSettings, coordinator: SpotRateCoordinator, trade: Trade) -> None:
        self.hours = hours

        if self.hours == 1:
            self._attr_unique_id = f'binary_sensor.{trade.lower()}_electricity_is_most_expensive'
            self._attr_translation_key = f'{trade.lower()}_electricity_is_most_expensive'
        else:
            self._attr_unique_id = f'binary_sensor.{trade.lower()}_electricity_is_most_expensive_{self.hours}_hours_block'
            self._attr_translation_key = f'{trade.lower()}_electricity_is_most_expensive_hours_block'
            self._attr_translation_placeholders = {
                'hours': self.hours,
            }

        self.entity_id = self._attr_unique_id

        super().__init__(hass=hass, settings=settings, coordinator=coordinator, trade=trade)

    def _compute_attr(self, rate_data: SpotRateData, start: datetime, end: datetime) -> dict:
        dt = start
        min_price: Optional[Decimal] = None
        max_price: Optional[Decimal] = None
        sum_price: Decimal = Decimal(0)
        count: int = 0

        hourly_rates = self._get_trade_rates(rate_data)
        while dt <= end:
            hour = hourly_rates.hour_for_dt(dt)
            sum_price += hour.price
            count += 1
            if min_price is None or hour.price < min_price:
                min_price = hour.price

            if max_price is None or hour.price > max_price:
                max_price = hour.price

            dt += timedelta(hours=1)
        return {
            'Start': start,
            'Start hour': start.hour,
            'End': end,
            'End hour': end.hour,
            'Min': round(float(min_price or 0), 2),
            'Max': round(float(max_price or 0), 2),
            'Mean': round(float(sum_price / count), 2) if count > 0 else 0,
        }

    def update(self, rate_data: Optional[SpotRateData]):
        self._attr = {}

        if not rate_data:
            self._available = False
            self._attr_is_on = None
            return

        is_on = False
        hourly_rates = self._get_trade_rates(rate_data)
        for hour in hourly_rates.hours_by_dt.values():
            start = hour.dt_local - timedelta(hours=self.hours - 1)
            end = hour.dt_local + timedelta(hours=1, seconds=-1)

            # Ignore start times before now, we only want future blocks
            if end < hourly_rates.now:
                continue

            if hour.most_expensive_consecutive_order[self.hours] == 1:
                if not self._attr:
                    # Only put it there once, so to contains closes interval in the future
                    self._attr = self._compute_attr(rate_data, start, end)

                if start <= hourly_rates.now <= end:
                    is_on = True

            self._attr_is_on = is_on
            self._available = True


class HasTomorrowElectricityData(ElectricityBinarySpotRateSensorBase):
    _attr_icon = 'mdi:cash-clock'

    def __init__(self, hass: HomeAssistant, settings: SpotRateSettings, coordinator: SpotRateCoordinator, trade: Trade) -> None:
        self._attr_unique_id = f'binary_sensor.{trade.lower()}_electricity_has_tomorrow_data'
        self._attr_translation_key = f'{trade.lower()}_electricity_has_tomorrow_data'

        self.entity_id = self._attr_unique_id

        super().__init__(hass=hass, settings=settings, coordinator=coordinator, trade=trade)

    def update(self, rate_data: Optional[SpotRateData]):
        self._attr = {}

        if not rate_data:
            self._attr_is_on = None
            self._available = False
            return

        self._attr_is_on = self._get_trade_rates(rate_data).tomorrow is not None
        self._available = True


class GasBinarySpotRateSensorBase(GasSpotRateSensorMixin, BinarySensorEntity):
    pass


class HasTomorrowGasData(GasBinarySpotRateSensorBase):
    _attr_icon = 'mdi:cash-clock'

    def __init__(self, hass: HomeAssistant, settings: SpotRateSettings, coordinator: SpotRateCoordinator, trade: Trade) -> None:
        self._attr_unique_id = f'binary_sensor.{trade.lower()}_gas_has_tomorrow_data'
        self._attr_translation_key = f'{trade.lower()}_gas_has_tomorrow_data'

        self.entity_id = self._attr_unique_id

        super().__init__(hass=hass, settings=settings, coordinator=coordinator, trade=trade)

    def update(self, rate_data: Optional[SpotRateData]):
        self._attr = {}

        if not rate_data:
            self._attr_is_on = None
            self._available = False
            return

        self._attr_is_on = self._get_trade_rates(rate_data).tomorrow is not None
        self._available = True
