# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import ConfigParser

from s1s2test import S1S2Test


class S1S2GeckoViewTest(S1S2Test):
    def __init__(self, dm=None, phone=None, options=None,
                 config_file=None, chunk=1, repos=[]):

        S1S2Test.__init__(self, dm=dm, phone=phone, options=options,
                          config_file=config_file, chunk=chunk, repos=repos)

        # [builds]
        try:
            self.e10s = self.cfg.get('builds', 'e10s') == 'true'
        except (ConfigParser.NoSectionError, ConfigParser.NoOptionError):
            self.e10s = False

    @property
    def name(self):
        return 'autophone-s1s2geckoview%s' % self.name_suffix

    def run_fennec_with_profile(self, appname, url, extra_args=[]):
        self.loggerdeco.debug('run_fennec_with_profile: %s %s %s' %
                              (appname, url, extra_args))

        extras = {}

        self.environment["R_LOG_VERBOSE"] = 1
        self.environment["R_LOG_LEVEL"] = 6
        self.environment["R_LOG_DESTINATION"] = "stderr"

        if self.environment:
            for (env_count, (env_key, env_val)) in enumerate(self.environment.iteritems()):
                extras["env" + str(env_count)] = env_key + "=" + str(env_val)

        local_extra_args = ['-profile', self.profile_path,
                            '--ez', 'use_multiprocess %s' % (
                                'true' if self.e10s else 'false')]
        local_extra_args.extend(extra_args)
        extras['args'] = " ".join(local_extra_args)

        try:
            self.dm.pkill(appname, root=True)
            self.dm.launch_application(
                appname,
                "%s.GeckoViewActivity" % appname,
                "android.intent.action.Main",
                url=url,
                extras=extras,
                wait=False,
                fail_if_running=False)
        except:
            self.loggerdeco.exception('run_fennec_with_profile: Exception:')
            raise
