# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

from mozprofile import FirefoxProfile

from s1s2test import S1S2Test


class S1S2GeckoViewTest(S1S2Test):
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

        local_extra_args = ['-profile', self.profile_path]
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

    def create_profile(self, custom_addons=[], custom_prefs=None, root=True):
        # Create, install and initialize the profile to be
        # used in the test.
        #
        # Extensions are not currently supported therefore we will
        # need to kill the process and ignore that fact.

        self.dm.pkill(self.build.app_name, root=root)
        if isinstance(custom_prefs, dict):
            prefs = dict(self.preferences.items() + custom_prefs.items())
        else:
            prefs = self.preferences
        profile = FirefoxProfile(preferences=prefs, addons=custom_addons)
        if not self.install_profile(profile):
            return False

        self.loggerdeco.debug('Attempt to Initialize profile')
        self.run_fennec_with_profile(self.build.app_name, self._initialize_url)

        self.wait_for_fennec()

        # minidumps not created?
        self.handle_crashes()
        return True

    def wait_for_fennec(self, max_wait_time=10, wait_time=5,
                        kill_wait_time=20, root=True):
        # Override S1S2Test's version with shorter wait values
        # since geckoview_example doesn't support extensions
        # and by implication doesn't support quitter.
        return S1S2Test.wait_for_fennec(self,
                                        max_wait_time=max_wait_time,
                                        wait_time=wait_time,
                                        kill_wait_time=kill_wait_time,
                                        root=root)
