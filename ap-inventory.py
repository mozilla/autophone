#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
import ConfigParser
import argparse
import os.path
import re

from glob import glob

from utils import autophone_path


class Devices(dict):
    """Devices() implements a dictionary of device objects keyed on the
    device id. Each device object contains an attribute 'id'
    whose value is the device's id.

    :param str device_manifest_pattern: a string containing a file
        pattern of device manifests to load. Defaults to 'production-*-devices.ini'.

    Usage:
    devices = Devices()

    where:
    devices = { device_id: {'id': device_id, 'host': 'autophone-9', 'serialno': 'xxxxxxx' } }

    """
    def __init__(self,
                 device_manifest_pattern='production-*-devices.ini'):
        super(Devices, self).__init__(self)

        self.device_manifest_pattern = device_manifest_pattern
        self.device_manifests = glob(os.path.join(autophone_path(), device_manifest_pattern))
        production_re = re.compile('production-(autophone-[0-9]+)-devices.ini')

        for device_manifest in self.device_manifests:
            cfg = ConfigParser.RawConfigParser()
            cfg.read(device_manifest)
            match = production_re.search(device_manifest)
            if match:
                host = match.group(1)
            else:
                host = None

            for device_section in cfg.sections():
                if device_section in self:
                    raise ValueError("Duplicate device %s matches %s",
                                     device_section, self[device_section])
                device = self[device_section] = {'id': device_section, 'host': host}
                for option in cfg.options(device_section):
                    device[option] = cfg.get(device_section, option)

    def match(self, match_rules=None):
        """match() returns a list of device objects which match the
        specified patterns.

        :param list match_rules: list of strings containing patterns to
            be matched. Each pattern is of the form attribute=pattern.

        :returns list of device objects.

        """
        match_rules = match_rules or [r'id=.*']
        match_patterns = {}
        for match_rule in match_rules:
            attribute, pattern = match_rule.split('=')
            match_patterns[attribute] = re.compile(pattern)

        matches = []
        for device_id in self:
            device = self[device_id]
            matched = True
            for attribute in match_patterns:
                if not match_patterns[attribute].search(device[attribute]):
                    matched = False
                    break
            if matched:
                matches.append(device)
        return matches


def format_device(device, output_format):
    if output_format == 'raw':
        return repr(device)
    return output_format % device



def main():
    parser = argparse.ArgumentParser(description="Query device manifests.")
    parser.add_argument("--device-manifests",
                        dest="device_manifest_pattern",
                        default="*-devices.ini",
                        help="File glob pattern matching device manifests. "
                        "(default: *-devices.ini)")
    parser.add_argument("--match",
                        dest="match_rules",
                        action="append",
                        default=[],
                        help="Match devices by attribute=regexp. "
                        "(default: match all devices)")
    parser.add_argument("--output-format",
                        dest="output_format",
                        default="raw",
                        help="Output format. raw - output python dict, attribute format string.")
    args = parser.parse_args()
    devices = Devices(device_manifest_pattern=args.device_manifest_pattern)
    output = [format_device(device, args.output_format) for device in devices.match(args.match_rules)]
    output.sort()
    for device in output:
        print device

if __name__ == '__main__':
    main()
