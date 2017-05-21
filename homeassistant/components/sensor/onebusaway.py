"""
Support for OneBusAway bus arrival times

Request api key here: http://pugetsound.onebusaway.org/p/ContactUs.action
Find your stop id here: http://pugetsound.onebusaway.org/where/standard

minutes_after - include vehicles arriving or departing in the next n minutes

Example configs:

sensor:
    # for http://pugetsound.onebusaway.org/where/standard/stop.action?id=1_700&route=40_100236
    - platform: onebusaway
      name: Westlake Center
      api_key: !secret onebusaway_api_key
      id: "1_700"
      routes:
        - id: "40_100236"
          name: 545E
      minutes_after: 60

or 
    - platform: onebusaway
      api_key: !secret onebusaway_api_key
      id: "1_701"
        
"""
import asyncio
import logging
import datetime as dt
import requests
from functools import partial

import voluptuous as vol

from homeassistant.const import STATE_UNKNOWN
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (CONF_ID, CONF_NAME, CONF_API_KEY)
from homeassistant.helpers.entity import Entity
from homeassistant.util import Throttle
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util

_LOGGER = logging.getLogger(__name__)

# api info
# http://developer.onebusaway.org/modules/onebusaway-application-modules/current/api/where/index.html
API_URL = "https://api.pugetsound.onebusaway.org/api/where"
API_STOP_INFO_URL = API_URL + \
    "/stop/{}.json?key={}"
API_ARRIVAL_INFO_URL = API_URL + \
    "/arrivals-and-departures-for-stop/{}.json?key={}&minutesAfter={}"

DEFAULT_ICON = 'mdi:bus'

DEFAULT_MINUTES_AFTER = 90

MIN_TIME_BETWEEN_UPDATES = dt.timedelta(seconds=60)

CONF_ROUTES = 'routes'
CONF_UPDATE_INTERVAL = 'update_interval'
CONF_MINUTES_AFTER = 'minutes_after'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME): cv.string,
    vol.Required(CONF_API_KEY): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Optional(CONF_ROUTES): vol.All(
        cv.ensure_list,
        [vol.Schema({
            vol.Required(CONF_ID): cv.string,
            vol.Optional(CONF_NAME): cv.string
        })]),
    vol.Optional(CONF_MINUTES_AFTER, default=DEFAULT_MINUTES_AFTER): cv.positive_int,
})

TIME_STR_FORMAT = '%I:%M %p'

ATTR_ROUTE_SHORT_NAME = 'Route Short Name'
ATTR_ROUTE_LONG_NAME = 'Route Long Name'
ATTR_TRIP_HEAD_SIGN = 'Trip Head Sign'
ATTR_TOTAL_STOPS_IN_TRIP = 'Total Stops in Trip'
ATTR_STOP_NUMBER_IN_TRIP = 'Stop Number in Trip'
ATTR_NUMBER_OF_STOPS_AWAY = 'Number of Stops Away'
ATTR_SCHEDULED_ARRIVAL_TIME = 'Scheduled Arrival Time'
ATTR_PREDICTED_ARRIVAL_TIME = 'Predicted Arrival Time'


@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the sensor."""
    _LOGGER.debug('OneBusAwaySensor: async_setup_platform')

    api_key = config.get(CONF_API_KEY)
    stop_id = config.get(CONF_ID)
    stop_name = config.get(CONF_NAME)
    routes = config.get(CONF_ROUTES)
    minutes_after = config.get(CONF_MINUTES_AFTER)

    devices = []

    stop_info_data = None

    if stop_name is None:
        # get the official stop name
        stop_info_url = API_STOP_INFO_URL.format(stop_id, api_key)
        stop_info_data = yield from OneBusAway.async_call_api(hass, stop_info_url, 'async_setup_platform')
        if stop_info_data == None:
            return

        stop_name = stop_info_data['entry']['name']

    if routes:
        for route in routes:
            route_id = route[CONF_ID]
            route_name = route[CONF_NAME]

            device = OneBusAway(hass, api_key, stop_id,
                                stop_name, route_id, route_name, minutes_after)
            yield from device.async_update()

            devices.append(device)

    else:
        # add a device for each route that uses the stop
        if stop_info_data == None:
            stop_info_url = API_STOP_INFO_URL.format(stop_id, api_key)
            stop_info_data = yield from OneBusAway.async_call_api(hass, stop_info_url, 'async_setup_platform')
            if stop_info_data == None:
                return

        for route in stop_info_data['references']['routes']:
            route_id = route['id']
            route_name = route['shortName']

            device = OneBusAway(hass, api_key, stop_id,
                                stop_name, route_id, route_name, minutes_after)
            yield from device.async_update()

            devices.append(device)

    async_add_devices(devices)


class OneBusAway(Entity):
    """Representation of the sensor."""

    def __init__(self, hass, api_key, stop_id, stop_name, route_id, route_name, minutes_after):
        """Initialize the sensor."""

        self._hass = hass
        self._api_key = api_key
        self._stop_id = stop_id
        self._stop_name = stop_name
        self._route_id = route_id
        self._route_name = route_name
        self._minutes_after = minutes_after

        self._state = STATE_UNKNOWN
        self._state_attributes = None

    @property
    def name(self):
        """Return the name of the sensor."""
        return '{} {}'.format(self._stop_name, self._route_name)

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return DEFAULT_ICON

    @property
    def state(self):
        """Return the next bus time."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return additional information about the bus and bus stop."""
        return self._state_attributes

    @asyncio.coroutine
    @Throttle(MIN_TIME_BETWEEN_UPDATES)
    def async_update(self):
        """Fetch the latest data for the bus stop."""

        self._state = STATE_UNKNOWN
        self._state_attributes = {}

        url = self.get_arrival_info_url()

        response_object_data = yield from OneBusAway.async_call_api(self._hass, url, self.name)
        if response_object_data is None:
            return

        next_arrival = None

        for arrival in response_object_data['entry']['arrivalsAndDepartures']:
            if arrival['routeId'] == self._route_id:
                next_arrival = OneBusAwayBusArrival(arrival)

                self._state = next_arrival.get_next_bus_time()
                self._state_attributes = next_arrival.get_state_attributes()

                break

    def get_arrival_info_url(self):
        """Get the url for retrieving arrival info."""
        return API_ARRIVAL_INFO_URL.format(self._stop_id, self._api_key, self._minutes_after)

    @staticmethod
    def get_time_string(epoch_milliseconds):
        """Get the time string from epoch milliseconds."""
        return dt.datetime.fromtimestamp(epoch_milliseconds / 1000).strftime(TIME_STR_FORMAT)

    @staticmethod
    @asyncio.coroutine
    def async_call_api(hass, url, name):
        """Calls the given api and returns the response data"""
        try:
            response = yield from hass.loop.run_in_executor(None, partial(requests.get, url, timeout=10))
        except (requests.exceptions.RequestException, ValueError):
            _LOGGER.warning(
                'Could not update status for OneBusAway (%s). Url: %s',
                name, url)
            return None

        if response.status_code != 200:
            _LOGGER.warning(
                'Invalid status_code from OneBusAway: %s (%s). Url: %s',
                response.status_code, name, url)
            return None

        response_object = response.json()

        if response_object['code'] != 200:
            _LOGGER.warning(
                'Invalid code from OneBusAway: %s (%s). Url: %s',
                response_object['code'], name, url)
            return None

        return response_object['data']


class OneBusAwayBusArrival(object):
    """Represents the arrival of a bus."""

    def __init__(self, data):
        self._data = data

    @property
    def route_short_name(self):
        """Get the short name of the route."""
        return self._data.get('routeShortName')

    @property
    def route_long_name(self):
        """Get the long name of the route."""
        return self._data.get('routeLongName')

    @property
    def trip_head_sign(self):
        """Get the total amount of stops in the trip."""
        return self._data.get('tripHeadsign')

    @property
    def total_stops_in_trip(self):
        """Get the total amount of stops in the trip."""
        return self._data.get('totalStopsInTrip')

    @property
    def stop_sequence(self):
        """Get the stop number in the trip."""
        return self._data.get('stopSequence')

    @property
    def number_of_stops_away(self):
        """Get the number of stops away the next bus is."""
        return self._data.get('numberOfStopsAway')

    @property
    def scheduled_arrival_time(self):
        """Get the scheduled arrival time of the next bus."""
        return self._data.get('scheduledArrivalTime')

    @property
    def predicted_arrival_time(self):
        """Get the predicted arrival time of the next bus."""
        return self._data.get('predictedArrivalTime')

    def get_next_bus_time(self):
        """Get the next bus time."""
        if self.predicted_arrival_time > 0:
            return OneBusAway.get_time_string(self.predicted_arrival_time)

        return OneBusAway.get_time_string(self.scheduled_arrival_time)

    def get_state_attributes(self):
        """Get the state attributes."""
        state_attributes = {
            ATTR_ROUTE_SHORT_NAME: self.route_short_name,
            ATTR_TRIP_HEAD_SIGN: self.trip_head_sign,
            ATTR_TOTAL_STOPS_IN_TRIP: self.total_stops_in_trip,
            ATTR_STOP_NUMBER_IN_TRIP: self.stop_sequence,
            ATTR_NUMBER_OF_STOPS_AWAY: self.number_of_stops_away,
            ATTR_SCHEDULED_ARRIVAL_TIME: OneBusAway.get_time_string(self.scheduled_arrival_time)
        }

        # some bus routes don't have long names
        if self.route_long_name:
            state_attributes[ATTR_ROUTE_LONG_NAME] = self.route_long_name

        # set the predicted arrival time to the scheduled arrival time if
        # predicted arrival time is 0
        if self.predicted_arrival_time > 0:
            state_attributes[ATTR_PREDICTED_ARRIVAL_TIME] = OneBusAway.get_time_string(
                self.predicted_arrival_time)
        else:
            state_attributes[ATTR_PREDICTED_ARRIVAL_TIME] = state_attributes[ATTR_SCHEDULED_ARRIVAL_TIME]

        return state_attributes
