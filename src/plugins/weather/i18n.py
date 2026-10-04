"""Translations for the text the weather plugin generates itself.

Every provider is handled here rather than inside each provider, so adding a language never
requires touching provider code. Strings are keyed by the English text they replace, which keeps
the table small and makes an unknown string fall through unchanged instead of disappearing.
"""
from datetime import date, datetime
from functools import partial

# Languages the weather plugin can render. Reused to build the settings dropdown.
LANGUAGES = {
    "en": "English",
    "fr": "Français",
}

# English source text -> translation. Anything not listed here is rendered as-is.
STRINGS = {
    "en": {},
    "fr": {
        # Metrics
        "Sunrise": "Lever",
        "Sunset": "Coucher",
        "Wind": "Vent",
        "Humidity": "Humidité",
        "Pressure": "Pression",
        "UV Index": "Indice UV",
        "Visibility": "Visibilité",
        "Air Quality": "Qualité de l'air",
        # AQHI risk bands
        "Low": "Faible",
        "Moderate": "Modéré",
        "High": "Élevé",
        "Very High": "Très élevé",
        # Template text
        "Last refresh:": "Dernière mise à jour :",
        "Feels Like": "Ressenti",
    },
}

# Date names, because strftime follows the process locale rather than the chosen language and
# the display is too small to rely on the server's locale being French.
WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "fr": ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"],
}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin",
           "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
}

# Lower case abbreviations for the forecast strip, where the full names do not fit.
WEEKDAY_ABBR = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "fr": ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"],
}

# Languages do not just rename the parts of a date, they also order them differently. English
# leads with the month and pads the day ("Friday, October 02"), while French leads with the day,
# drops the padding and uses no comma ("vendredi 2 octobre"). A single strftime pattern cannot
# produce both, so each language carries its own.
DATE_FORMATS = {
    "en": "{weekday}, {month} {day:02d}",
    "fr": "{weekday} {day} {month}",
}


def t(language, text):
    """Translate a string of English text, falling back to the text itself."""
    return STRINGS.get(language, {}).get(text, text)


def format_date(language, value):
    """Format a date the way the language orders and spells it."""
    months = MONTHS.get(language, MONTHS["en"])
    return DATE_FORMATS.get(language, DATE_FORMATS["en"]).format(
        weekday=weekday(language, value.weekday()),
        month=months[value.month - 1],
        day=value.day,
    )


def format_weekday(language, value, abbreviate=False):
    """Format the weekday part of a date, abbreviated for the forecast strip."""
    return weekday(language, value.weekday(), abbreviate=abbreviate)


def localize(template_params, language):
    """Translate the plugin generated text in template params.

    Applied once per image so every provider is covered without any provider knowing about
    languages. Labels and units are translated in place; values such as "hPa" or "%" have no
    entry and are left alone.

    Providers hand over dates as date and datetime objects rather than preformatted strings, so
    the field order can follow the language here. Anything already a string is left untouched.
    """
    for data_point in template_params.get("data_points") or []:
        data_point["label"] = t(language, data_point.get("label"))
        if data_point.get("unit") in STRINGS.get(language, {}):
            data_point["unit"] = t(language, data_point["unit"])

    if isinstance(template_params.get("current_date"), (datetime, date)):
        template_params["current_date"] = format_date(language, template_params["current_date"])

    for forecast_day in template_params.get("forecast") or []:
        if isinstance(forecast_day.get("day"), (datetime, date)):
            forecast_day["day"] = format_weekday(language, forecast_day["day"], abbreviate=True)

    # Exposed to weather.html for the strings that live in the template itself.
    template_params["t"] = partial(t, language)
    return template_params


def weekday(language, index, abbreviate=False):
    """Name a weekday by index, where Monday is 0."""
    names = WEEKDAY_ABBR if abbreviate else WEEKDAYS
    return names.get(language, names["en"])[index]


def month(language, index):
    """Name a month by index, where January is 0."""
    return MONTHS.get(language, MONTHS["en"])[index]
