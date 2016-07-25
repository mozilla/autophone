# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import calendar
import datetime
import re

import pytz

PACIFIC = pytz.timezone('US/Pacific')
UTC = pytz.timezone('UTC')

__all__ = ['TIMESTAMP', 'DIRECTORY_DATE', 'DIRECTORY_DATETIME',
           'BUILDID', 'DATETIME', 'parse_datetime', 'convert_datetime_to_string',
           'set_time_zone',
           'convert_timestamp_to_date']

TIMESTAMP = 'timestamp'
DIRECTORY_DATE = 'directory-date'
DIRECTORY_DATETIME = 'directory-datetime'
BUILDID = 'buildid'
DATETIME = 'datetime'

def parse_datetime(stringval, tz=UTC):
    """Parse various datetime strings.

    arguments:
    stringval - string value containing the date to be parsed.

    returns: format, datevalue

    Supports parsing of the following datetime value formats:
    buildid            - CCYYMMDDHHSS
    date               - CCYY-MM-DD
    datetime           - CCYY-MM-DDTHH:MM:SS
    directory datetime - CCYY-MM-DD-HH-MM-SS
    timestamp          - seconds since epoch
    """
    format, datetimeval = None, None
    try:
        # Distinguish between timestamps and buildids by converting
        # the value to a float. If the value is greater than the
        # current timestamp then it is a buildid and not a timestamp.
        floatval = float(stringval)
        timestamp = calendar.timegm(datetime.datetime.now(tz=pytz.utc).timetuple())
        if floatval > timestamp:
            # 20131201030203 - buildid
            format = BUILDID
            datetimeval = datetime.datetime.strptime(stringval, '%Y%m%d%H%M%S')
        else:
            format = TIMESTAMP
            datetimeval = datetime.datetime.fromtimestamp(floatval)
    except ValueError:
        # 2013-12-01T03:02:03
        datetime_regex = re.compile(r'([\d]{4}-[\d]{2}-[\d]{2}T[\d]{2}:[\d]{2}:[\d]{2})')
        match = datetime_regex.match(stringval)
        if match:
            stringval = match.group(1)
            format = DATETIME
            datetimeval = datetime.datetime.strptime(stringval, '%Y-%m-%dT%H:%M:%S')
        else:
            # 2013-12-01-03-02-03
            directory_datetime_regex = re.compile(r'([\d]{4}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2}-[\d]{2})')
            match = directory_datetime_regex.match(stringval)
            if match:
                stringval = match.group(1)
                format = DIRECTORY_DATETIME
                datetimeval = datetime.datetime.strptime(stringval, '%Y-%m-%d-%H-%M-%S')
            else:
                # 2013-12-01
                directory_date_regex = re.compile(r'([\d]{4}-[\d]{2}-[\d]{2})')
                match = directory_date_regex.match(stringval)
                if match:
                    stringval = match.group(1)
                    format = DIRECTORY_DATE
                    datetimeval = datetime.datetime.strptime(stringval, '%Y-%m-%d')

    if not format:
        raise ValueError('%s is not a recognized datetime format' % stringval)

    datetimeval = tz.localize(datetimeval)

    return format, datetimeval

def convert_datetime_to_string(dateval, format, tz=UTC):
    """Convert a date to a string of the specified format.

    arguments:
    dateval -- a date value
    format  -- a string containing one of the following format names:
               timestamp          - number of seconds since epoch
               directory-date     - CCYY-MM-DD
               directory-datetime - CCYY-MM-DD-HH-MM-SS
               buildid            - CCYYMMDDHHMMSS
               datetime           - CCYY-MM-DDTHH:MM:SS

    returns: date value.
    """

    if not dateval.tzinfo:
        raise Exception('Can not convert naive date')

    dateval = dateval.astimezone(tz)

    if format == TIMESTAMP:
        return str(int(calendar.timegm(dateval.timetuple())))

    if format == DIRECTORY_DATE:
        return dateval.strftime('%Y-%m-%d')
    if format == DIRECTORY_DATETIME:
        return dateval.strftime('%Y-%m-%d-%H-%M-%S')
    if format == BUILDID:
        return dateval.strftime('%Y%m%d%H%M%S')
    if format == DATETIME:
        return dateval.strftime('%Y-%m-%dT%H:%M:%S')

    raise ValueError("%s is not a recognized format name" % format)

def set_time_zone(dateval):
    """ Set a date's timezone to Mozilla Time.

    arguments:
    dateval - a date value

    returns: date value in Mozilla Time Zone.
    """
    if not dateval.tzinfo:
        pacific = pytz.timezone('US/Pacific')
        dateval = pacific.localize(dateval)
    return dateval

def convert_timestamp_to_date(timestamp):
    """Convert a numeric timestamp to a
    date value in UTC.

    arguments:
    timestamp - seconds in epoch

    returns: date value in Mozilla Time Zone.
    """
    try:
        dateval = datetime.datetime.utcfromtimestamp(timestamp)
        UTC.localize(dateval)
        return dateval
    except (TypeError, ValueError):
        return None


def convert_pacific_to_utc_time(dateval):
    """ Convert a date from Mozilla time
    to UTC time.

    arguments:
    dateval - a date value

    returns: date value in UTC Time Zone.
    """
    dateval = set_time_zone(dateval)
    return dateval.astimezone(UTC)
