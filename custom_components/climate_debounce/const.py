"""Constants for Climate Debounce."""

DOMAIN = "climate_debounce"

CONF_DELAY = "delay"
CONF_PER_MINUTE = "commands_per_minute"
CONF_ONE_DECIMAL = "one_decimal"

DEFAULT_DELAY = 5.0  # seconds of quiet before the latest values are sent
DEFAULT_PER_MINUTE = 5  # Google Nest SDM: 5 commands per minute per thermostat
DEFAULT_ONE_DECIMAL = False
MIN_MAX_WAIT = 15.0  # never hold a change longer than this while writes keep coming
