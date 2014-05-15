# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import os
import posixpath
import re
import subprocess
import tempfile
import time


# Do not make ADBTimeout a child class of ADBError
# so that ADBTimeout exceptions will not be caught
# by ADBError handlers

class ADBBaseError(Exception):
    def __init__(self, message='', result=None):
        self.result = None

        if result:
            self.result = dict(result)
            self.result['message'] = message or ''
            self.result['stdout'] = result['stdout'].read().rstrip()
            self.result['stderr'] = result['stderr'].read().rstrip()

    def __str__(self):
        if self.result:
            return '%(message)s: %(args)s, exitcode: %(exitcode)s, stdout: %(stdout)s, stderr: %(stderr)s' % self.result
        return '%s' % self.message

class ADBError(ADBBaseError):
    pass

class ADBTimeout(ADBBaseError):
    pass

class ADB(object):

    def __init__(self, device_serial,
                 adb='adb',
                 test_root='',
                 logger_name='adb',
                 log_level=logging.INFO,
                 timeout=300,
                 reboot_settling_time=60):
        self._logger = logging.getLogger(logger_name)
        self._adb_path = adb
        self._device_serial = device_serial
        self.test_root = test_root
        self._log_level = log_level
        self._timeout = timeout
        self._polling_interval = 0.1
        self._reboot_settling_time = reboot_settling_time
        self._have_root_shell = False
        self._have_su = False
        self._have_android_su = False

        uid = 'uid=0'
        cmd_id = 'LD_LIBRARY_PATH=/vendor/lib:/system/lib id'
        # Is shell already running as root?
        # Catch exceptions due to the potential for segfaults
        # calling su when using an improperly rooted device.
        try:
            if self.shell_output("id").find(uid) != -1:
                self._have_root_shell = True
        except ADBError:
            self._logger.exception('ADB.__init__: id')
        # Do we have a 'Superuser' sh like su?
        try:
            if self.shell_output("su -c '%s'" % cmd_id).find(uid) != -1:
                self._have_su = True
        except ADBError:
            self._logger.exception('ADB.__init__: id')
        # Do we have Android's su?
        try:
            if self.shell_output("su 0 id").find(uid) != -1:
                self._have_android_su = True
        except ADBError:
            self._logger.exception('ADB.__init__: id')

        self._mkdir_p = None
        # Force the use of /system/bin/ls or /system/xbin/ls in case
        # there is /sbin/ls which embeds ansi escape codes to colorize
        # the output.  Detect if we are using busybox ls. We want each
        # entry on a single line and we don't want . or ..
        if self.shell_bool("/system/bin/ls /"):
            self._ls = "/system/bin/ls"
        elif self.shell_bool("/system/xbin/ls /"):
            self._ls = "/system/xbin/ls"
        else:
            raise ADBError("ADB.__init__: ls not found")
        try:
            self.shell_output("%s -1A /" % self._ls)
            self._ls += " -1A"
        except ADBError:
            self._ls += " -a"

        self._logger.debug("ADB: %s" % self.__dict__)

        self._setup_test_root()

    @staticmethod
    def _escape_command_line(cmd):
        """
        Utility function to return escaped and quoted version of command line.
        """
        quoted_cmd = []

        for arg in cmd:
            arg.replace('&', '\&')

            needs_quoting = False
            for char in [' ', '(', ')', '"', '&']:
                if arg.find(char) >= 0:
                    needs_quoting = True
                    break
            if needs_quoting:
                arg = "'%s'" % arg

            quoted_cmd.append(arg)

        return " ".join(quoted_cmd)

    @staticmethod
    def _get_exitcode(file_obj):
        """
        Get the exitcode from the last line of the file_obj for shell commands.
        """
        file_obj.seek(0, os.SEEK_END)

        line = ''
        length = file_obj.tell()
        offset = 1
        while length - offset >= 0:
            file_obj.seek(-offset, os.SEEK_END)
            char = file_obj.read(1)
            if not char:
                break
            if char != '\r' and char != '\n':
                line = char + line
            elif line:
                # we have collected everything up to the beginning of the line
                break
            offset += 1

        match = re.match(r'rc=([0-9]+)', line)
        if match:
            exitcode = int(match.group(1))
            file_obj.seek(-1, os.SEEK_CUR)
            file_obj.truncate()
        else:
            exitcode = None

        return exitcode

    def _setup_test_root(self, timeout=None, root=False):
        """
        setup the device root and cache its value
        """
        # if self.test_root is already set, create it if necessary, and use it
        if self.test_root:
            if not self.is_dir(self.test_root, timeout=timeout, root=root):
                try:
                    self.mkdir(self.test_root, timeout=timeout, root=root)
                except:
                    self._logger.exception("Unable to create device root %s" %
                                           self.test_root)
                    raise
            return

        paths = [('/mnt/sdcard', 'tests'),
                 ('/data/local', 'tests')]
        for (base_path, sub_path) in paths:
            if self.is_dir(base_path, timeout=timeout, root=root):
                test_root = os.path.join(base_path, sub_path)
                try:
                    if not self.is_dir(test_root):
                        self.mkdir(test_root, timeout=timeout, root=root)
                    self.test_root = test_root
                    return
                except:
                    self._logger.exception('_setup_test_root: '
                                           'Failure setting test_root to %s' %
                                           test_root)

        raise ADBError("Unable to set up device root using paths: [%s]"
                       % ", ".join(["'%s'" % os.path.join(b, s)
                                    for b, s in paths]))

    def command(self, cmds, timeout=None):
        """Executes an adb command on the host.

        :param cmds:    list containing the command and its arguments to be
                        executed.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.

        command() provides a low level interface for executing
        commands on the host via adb.  For executing shell commands on
        the device, use ADB.shell().  The caller provides a list
        containing commands, as well as a timeout period in seconds.

        A subprocess is spawned to execute adb for the device with
        stdout and stderr directed to named temporary files. If the
        process takes longer than the specified timeout, the process
        is terminated.

        The command returns an object containing:
        args - a list containing the command and its arguments.
        stdout - name of file containing stdout.
        stderr - name of file containing stderr.
        timedout - boolean indicating if the command timed out.
        exitcode - exit code of the command.

        It is the caller's responsibilty to clean up by closing
        the stdout and stderr temporary files.

        """
        result = {'args': None,
                  'stdout': None,
                  'stderr': None,
                  'timedout': None,
                  'exitcode': None}

        args = [self._adb_path]
        if self._device_serial:
            args.extend(['-s', self._device_serial])
        if not 'wait-for-device' in cmds:
            args.append("wait-for-device")
        args.extend(cmds)

        result['args'] = args
        result['stdout'] = tempfile.TemporaryFile()
        result['stderr'] = tempfile.TemporaryFile()
        proc = subprocess.Popen(args,
                                stdout=result['stdout'],
                                stderr=result['stderr'])

        if timeout is None:
            timeout = self._timeout

        timeout = int(timeout)
        start_time = time.time()
        result['exitcode'] = proc.poll()
        while ((time.time() - start_time) <= timeout and
               result['exitcode'] == None):
            time.sleep(self._polling_interval)
            result['exitcode'] = proc.poll()
        if result['exitcode'] == None:
            proc.kill()
            result['timedout'] = True
            result['exitcode'] = proc.poll()

        result['stdout'].seek(0, os.SEEK_SET)
        result['stderr'].seek(0, os.SEEK_SET)

        return result

    def command_output(self, cmds, timeout=None):
        """
        Executes an adb command on the host returning stdout.


        :param cmds:    list containing the command and its arguments to be
                        executed.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.

        Exceptions:

        ADBTimeout  - raised if the command takes longer than timeout seconds.
        ADBError    - raised if the command exits with a non-zero exit code.
        """
        result = None
        try:
            result = self.command(cmds, timeout=timeout)
            if result['timedout']:
                raise ADBTimeout(result=result)
            elif result['exitcode']:
                raise ADBError(result=result)
            output = result['stdout'].read().rstrip()
            self._logger.debug('command_output: %s, '
                               'timeout: %s, '
                               'timedout: %s, '
                               'exitcode: %s, output: %s' %
                               (' '.join(result['args']),
                                timeout,
                                result['timedout'],
                                result['exitcode'],
                                output))
            return output
        finally:
            if result:
                result['stdout'].close()
                result['stderr'].close()

    def shell(self, cmd, env=None, cwd=None, timeout=None, root=False):
        """
        Executes a shell command on the device.

        :param cmd:     string containing the command to be executed.
        :param env:     optional dictionary of environment variables and their
                        values.
        :param cwd:     optional string containing the directory from which to
                        execute.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.

        shell() provides a low level interface for executing commands
        on the device via adb shell.  the caller provides a flag
        indicating if the command is to be executed as root, a string
        for any requested working directory, a hash defining the
        environment, a string containing shell commands, as well as a
        timeout period in seconds.

        The command line to be executed is created to set the current
        directory, set the required environment variables, optionally
        execute the command using su and to output the return code of
        the command to stdout. The command list is created as a
        command sequence separated by && which will terminate the
        command sequence on the first command which returns a non-zero
        exit code.

        A subprocess is spawned to execute adb shell for the device
        with stdout and stderr directed to temporary files. If the
        process takes longer than the specified timeout, the process
        is terminated. The return code is extracted from the stdout
        and is then removed from the file.

        The command returns an object containing:

        args - a list containing the command and its arguments.
        stdout - name of file containing stdout.
        stderr - name of file containing stderr.
        timedout - boolean indicating if the command timed out.
        exitcode - exit code of the command.

        It is the caller's responsibilty to clean up by closing
        the stdout and stderr temporary files.
        """
        result = {'args': None,
                  'stdout': None,
                  'stderr': None,
                  'timedout': None,
                  'exitcode': None}

        if root:
            ld_library_path='LD_LIBRARY_PATH=/vendor/lib:/system/lib'
            cmd = '%s %s' % (ld_library_path, cmd)
            if self._have_root_shell:
                pass
            elif self._have_su:
                cmd = "su -c \"%s\"" % cmd
            elif self._have_android_su:
                cmd = "su 0 \"%s\"" % cmd
            else:
                raise ADBError('Can not run command %s as root!' % cmd)

        # prepend cwd and env to command if necessary
        if cwd:
            cmd = "cd %s && %s" % (cwd, cmd)
        if env:
            envstr = '&& '.join(map(lambda x: 'export %s=%s' %
                                    (x[0], x[1]), env.iteritems()))
            cmd = envstr + "&& " + cmd
        cmd += "; echo rc=$?"

        args = [self._adb_path]
        if self._device_serial:
            args.extend(['-s', self._device_serial])
        args.extend(["wait-for-device", "shell", cmd])

        result['args'] = args
        result['stdout'] = tempfile.TemporaryFile()
        result['stderr'] = tempfile.TemporaryFile()
        proc = subprocess.Popen(args, stdout=result['stdout'],
                                stderr=result['stderr'])

        if timeout is None:
            timeout = self._timeout

        timeout = int(timeout)
        start_time = time.time()
        ret_code = proc.poll()
        while ((time.time() - start_time) <= timeout) and ret_code == None:
            time.sleep(self._polling_interval)
            ret_code = proc.poll()
        if ret_code == None:
            proc.kill()
            result['timedout'] = True
            result['exitcode'] = proc.poll()
        else:
            result['exitcode'] = self._get_exitcode(result['stdout'])

        result['stdout'].seek(0, os.SEEK_SET)
        result['stderr'].seek(0, os.SEEK_SET)

        return result

    def shell_output(self, cmd, env=None, cwd=None, timeout=None, root=False):
        """
        Executes an adb shell on the device returning stdout.

        :param cmd:     string containing the command to be executed.
        :param env:     optional dictionary of environment variables and their
                        values.
        :param cwd:     optional string containing the directory from which to
                        execute.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.

        Exceptions:

        ADBTimeout  - raised if the command takes longer than timeout seconds.
        ADBError    - raised if the command exits with a non-zero exit code.
        """
        result = None
        try:
            result = self.shell(cmd, env=env, cwd=cwd,
                                timeout=timeout, root=root)
            if result['timedout']:
                raise ADBTimeout(result=result)
            elif result['exitcode']:
                raise ADBError(result=result)
            output = result['stdout'].read().rstrip()
            self._logger.debug('shell_output: %s, '
                               'timeout: %s, '
                               'root: %s, '
                               'timedout: %s, '
                               'exitcode: %s, '
                               'output: %s' %
                               (' '.join(result['args']),
                                timeout,
                                root,
                                result['timedout'],
                                result['exitcode'],
                                output))
            return output
        finally:
            if result:
                result['stdout'].close()
                result['stderr'].close()

    def shell_bool(self, cmd, env=None, cwd=None, timeout=None, root=False):
        """
        Executes a shell command on the device returning True on success
        and False on failure.

        :param cmd:     string containing the command to be executed.
        :param env:     optional dictionary of environment variables and their
                        values.
        :param cwd:     optional string containing the directory from which to
                        execute.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.

        Exceptions:

        ADBTimeout - raised if the command takes longer than timeout seconds.
        """
        result = None
        try:
            result = self.shell(cmd, env=env, cwd=cwd,
                                timeout=timeout, root=root)
            if result['timedout']:
                raise ADBTimeout(result=result)
            return result['exitcode'] == 0
        finally:
            if result:
                result['stdout'].close()
                result['stderr'].close()

    def chmod(self, path, recursive=False, mask="777", timeout=None, root=False):
        """
        Recursively changes the permissions of a directory on the device.

        :param path: string containing the directory name on the device.
        :param recursive: boolean specifying if the command should be executed
                          recursively.
        :param mask:      optional string containing the octal permissions.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path.strip())
        self._logger.debug('chmod: path=%s, recursive=%s, mask=%s, root=%s' %
                           (path, recursive, mask, root))
        self.shell_output("chmod %s %s" % (mask, path),
                          timeout=timeout, root=root)
        if recursive and self.is_dir(path, timeout=timeout, root=root):
            files = self.list_files(path, timeout=timeout, root=root)
            for f in files:
                entry = path + "/" + f
                self._logger.debug('chmod: entry=%s' % entry)
                if self.is_dir(entry, timeout=timeout, root=root):
                    self._logger.debug('chmod: recursion entry=%s' % entry)
                    self.chmod(entry, recursive=recursive, mask=mask,
                               timeout=timeout, root=root)
                elif self.is_file(entry, timeout=timeout, root=root):
                    try:
                        self.shell_output("chmod %s %s" % (mask, entry),
                                          timeout=timeout, root=root)
                        self._logger.debug('chmod: file entry=%s' % entry)
                    except ADBError, e:
                        if e.message.find('No such file or directory'):
                            # some kind of race condition is causing files
                            # to disappear. Catch and report the error here.
                            self._logger.warning('chmod: File %s vanished!: %s' %
                                                 (entry, e))
                else:
                    self._logger.warning('chmod: entry %s does not exist' %
                                         entry)

    def exists(self, path, timeout=None, root=False):
        """
        Returns True if the path exists on the device.

        :param path: string containing the directory name on the device.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path)
        return self.shell_bool('ls -a %s' % path, timeout=timeout, root=root)

    def is_dir(self, path, timeout=None, root=False):
        """
        Returns True if path is an existing directory on the device.

        :param path: string containing the path on the device.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path)
        return self.shell_bool('ls -a %s/' % path, timeout=timeout, root=root)

    def is_file(self, path, timeout=None, root=False):
        """
        Returns True if path is an existing file on the device.

        :param path: string containing the file name on the device.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path)
        return (
            self.exists(path, timeout=timeout, root=root) and
            not self.is_dir(path, timeout=timeout, root=root))

    def get_logcat(self,
                  filter_specs=[
                      "dalvikvm:I",
                      "ConnectivityService:S",
                      "WifiMonitor:S",
                      "WifiStateTracker:S",
                      "wpa_supplicant:S",
                      "NetworkStateTracker:S"],
                  format="time",
                  filter_out_regexps=[],
                  timeout=None):
        """
        Returns the contents of the logcat file as a list of strings.

        :param filter_specs: optional list containing logcat messages to be
                            included.
        :param format: optional logcat format.
        :param filterOutRexps: optional list of logcat messages to be excluded.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        cmds = ["logcat", "-v", format, "-d"] + filter_specs
        lines = self.command_output(cmds, timeout=timeout).split('\r')

        for regex in filter_out_regexps:
            lines = [line for line in lines if not re.search(regex, line)]

        return lines

    def kill(self, pids, sig=None, timeout=None, root=False):
        """
        Kills processes on the device given a list of process ids.

        :param pids: list of process ids to be killed.
        :param sig:     optional signal to be sent to the process.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        args = ["kill"]
        if sig:
            args.append("-%d" % sig)
        args.extend([str(pid) for pid in pids])
        self.shell_output(' '.join(args), timeout=timeout, root=root)

    def pkill(self, appname, sig=None, timeout=None, root=False):
        """
        Kills a processes on the device matching a name.

        :param appname: string containing the app name of the process to be
                        killed.
        :param sig:     optional signal to be sent to the process.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        procs = self.get_process_list(timeout=timeout)
        pids = [proc[0] for proc in procs if proc[1] == appname]
        if not pids:
            return

        try:
            self.kill(pids, sig, timeout=timeout, root=root)
        except ADBError, e:
            if self.process_exist(appname, timeout=timeout):
                raise e

    def launch_application(self, app_name, activity_name, intent, url=None,
                          extras=None, wait=True, fail_if_running=True,
                          timeout=None):
        """
        Launches an Android application

        :param app_name: Name of application (e.g. `com.android.chrome`)
        :param activity_name: Name of activity to launch (e.g. `.Main`)
        :param intent: Intent to launch application with
        :param url: URL to open
        :param extras: Dictionary of extra arguments to launch application with
        :param wait: If True, wait for application to start before returning
        :param fail_if_running: Raise an exception if instance of application
                                is already running
        """
        # If fail_if_running is True, we throw an exception here. Only one
        # instance of an application can be running at once on Android,
        # starting a new instance may not be what we want depending on what
        # we want to do
        if fail_if_running and self.process_exist(app_name, timeout=timeout):
            raise ADBError("Only one instance of an application may be running "
                           "at once")

        acmd = [ "am", "start" ] + \
            ["-W" if wait else '', "-n", "%s/%s" % (app_name, activity_name)]

        if intent:
            acmd.extend(["-a", intent])

        if extras:
            for (key, val) in extras.iteritems():
                if type(val) is int:
                    extra_type_param = "--ei"
                elif type(val) is bool:
                    extra_type_param = "--ez"
                else:
                    extra_type_param = "--es"
                acmd.extend([extra_type_param, str(key), str(val)])

        if url:
            acmd.extend(["-d", url])

        cmd = self._escape_command_line(acmd)
        self.shell_output(cmd, timeout=timeout)

    def launch_fennec(self, app_name, intent="android.intent.action.VIEW",
                     moz_env=None, extra_args=None, url=None, wait=True,
                     fail_if_running=True, timeout=None):
        """
        Convenience method to launch Fennec on Android with various debugging
        arguments

        :param app_name: Name of fennec application (e.g. `org.mozilla.fennec`)
        :param intent: Intent to launch application with
        :param moz_env: Mozilla specific environment to pass into application
        :param extra_args: Extra arguments to be parsed by fennec
        :param url: URL to open
        :param wait: If True, wait for application to start before returning
        :param fail_if_running: Raise an exception if instance of application
                                is already running
        """
        extras = {}

        if moz_env:
            # moz_env is expected to be a dictionary of environment variables:
            # Fennec itself will set them when launched
            for (env_count, (env_key, env_val)) in enumerate(moz_env.iteritems()):
                extras["env" + str(env_count)] = env_key + "=" + env_val

        # Additional command line arguments that fennec will read and use (e.g.
        # with a custom profile)
        if extra_args:
            extras['args'] = " ".join(extra_args)

        self.launch_application(app_name, ".App", intent, url=url, extras=extras,
                               wait=wait, fail_if_running=fail_if_running,
                               timeout=timeout)


    def list_files(self, path, timeout=None, root=False):
        """
        Return a list of files/directories contained in a directory on the
        device.

        :param path: string containing the directory name on the device.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path.strip())
        data = []
        if self.is_dir(path, timeout=timeout, root=root):
            try:
                data = self.shell_output("%s %s" % (self._ls, path),
                                         timeout=timeout,
                                         root=root).split('\r\n')
                self._logger.debug('list_files: data: %s' % data)
            except ADBError:
                self._logger.exception('Ignoring exception in ADB.list_files')
                pass
        data[:] = [item for item in data if item]
        self._logger.debug('list_files: %s' % data)
        return data

    def mkdir(self, path, parents=False, timeout=None, root=False):
        """
        Create a directory on the device.

        :param path: string containing the directory name on the device to
                     be created.
        :param parents: boolean indicating if the parent directories are also
                        to be created. Think mkdir -p path.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.
        """
        path = posixpath.normpath(path)
        if parents:
            if self._mkdir_p is None or self._mkdir_p:
                # Use shell_bool to catch the possible
                # non-zero exitcode if -p is not supported.
                if self.shell_bool('mkdir -p %s' % path, timeout=timeout):
                    self._mkdir_p = True
                    return
            # mkdir -p is not supported. create the parent
            # directories individually.
            if not self.is_dir(posixpath.dirname(path)):
                parts = path.split('/')
                name = "/"
                for part in parts[:-1]:
                    if part != "":
                        name = posixpath.join(name, part)
                        if not self.is_dir(name):
                            # Use shell_output to allow any non-zero
                            # exitcode to raise an ADBError.
                            self.shell_output('mkdir %s' % name,
                                              timeout=timeout, root=root)
        self.shell_output('mkdir %s' % path, timeout=timeout, root=root)
        if not self.is_dir(path, timeout=timeout, root=root):
            raise ADBError('mkdir %s Failed' % path)

    def process_exist(self, process_name, timeout=None):
        """
        Returns True if process with name process_name is running on device.

        :param process_name: string containing the name of the process to check.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        if not isinstance(process_name, basestring):
            raise TypeError("Process name %s is not a string" % process_name)

        pid = None

        # Filter out extra spaces.
        parts = [x for x in process_name.split(' ') if x != '']
        process_name = ' '.join(parts)

        # Filter out the quoted env string if it exists
        # ex: '"name=value;name2=value2;etc=..." process args' -> 'process args'
        parts = process_name.split('"')
        if len(parts) > 2:
            process_name = ' '.join(parts[2:]).strip()

        pieces = process_name.split(' ')
        parts = pieces[0].split('/')
        app = parts[-1]

        proc_list = self.get_process_list(timeout=timeout)
        if not proc_list:
            return None

        for proc in proc_list:
            proc_name = proc[1].split('/')[-1]
            if proc_name == app:
                pid = proc[0]
                break
        return pid

    def get_process_list(self, timeout=None):
        """
        Returns list of tuples (pid, name, user) for running
        processes on device.

        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        result = None
        try:
            result = self.shell("ps", timeout=timeout)
            if result['timedout']:
                raise ADBTimeout(result=result)
            elif result['exitcode']:
                raise ADBError(result=result)
            # first line is the headers
            header = result['stdout'].readline()
            pid_i = -1
            user_i = -1
            els = header.split()
            for i in range(len(els)):
                item = els[i].lower()
                if item == 'user':
                    user_i = i
                elif item == 'pid':
                    pid_i = i
            if user_i == -1 or pid_i == -1:
                raise ADBError('get_process_list: Unknown format: %s' % header,
                               result=result)
            ret = []
            line = result['stdout'].readline()
            while line:
                els = line.split()
                ret.append([int(els[pid_i]), els[-1], els[user_i]])
                line = result['stdout'].readline()
            self._logger.debug('get_process_list: %s' % ret)
            return ret
        except ValueError:
            self._logger.exception('get_process_list: %s %s' % (header, line),
                                   result=result)
            raise
        finally:
            if result:
                result['stdout'].close()
                result['stderr'].close()

    def push(self, local, remote, timeout=None):
        """
        Pushes a file or directory to the device.

        :param local: string containing the name of the local file or
                      directory name.
        :param remote: string containing the name of the remote file or
                        directory name.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        self.command_output(["push", os.path.realpath(local), remote],
                            timeout=timeout)

    def reboot(self, timeout=None):
        """
        Reboots the device.

        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.


        reboot() reboots the device, waits for it to reboot and the waits
        for an additional ADB._reboot_settling_time before returning True if
        get_state() returns device.
        """
        self.command_output(["reboot"], timeout)
        self.command_output(["wait-for-device"], timeout=timeout)
        time.sleep(self._reboot_settling_time)
        return self.get_state() == 'device'

    def clear_logcat(self, timeout=None):
        """
        Clears logcat.

        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        self.command_output(["logcat", "-c"], timeout=timeout)

    def rmdir(self, path, timeout=None, root=False):
        """
        Delete empty directory on the device.

        :param path: string containing the directory name on the device.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.

        Exceptions:

        ADBError - raised if the directory could not be removed.
        """
        self.shell_output("rmdir %s" % path, timeout=timeout, root=root)
        if self.is_dir(path, timeout=timeout, root=root):
            raise ADBError('rmdir("%s") failed to remove directory.' % path)

    def rm(self, path, recursive=False, force=False, timeout=None, root=False):
        """
        rm - delete files or directories the device.

        :param path: string containing the file name on the device.
        :param recursive: optional boolean specifying if the command is to be
                          applied recursively to the target. Default is False.
        :param force: optional boolean which if True will not raise an error
                      when attempting to delete a non-existent file. Default
                      is False.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        :param root:    optional boolean specifying if the command should be
                        executed as root.

        Exceptions:

        ADBError - raised if the file could not be removed.
        """
        cmd = "rm"
        if recursive:
            cmd += " -r"
        try:
            self.shell_output("%s %s" % (cmd, path), timeout=timeout, root=root)
            if self.is_file(path, timeout=timeout, root=root):
                raise ADBError('rm("%s") failed to remove file.' % path)
        except ADBError, e:
            if not force and 'No such file or directory' in e.result['stdout']:
                raise

    def is_app_installed(self, app_name, timeout=None):
        """
        Returns True if an app is installed on the device.

        :param app_name: string containing the name of the app to be checked.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        data = self.shell_output("pm list package %s" % app_name, timeout=timeout)
        if data.find(app_name) == -1:
            return False
        return True

    def install_app(self, apk_path, timeout=None):
        """
        Installs an app on the device.

        :param apk_path: string containing the apk file name to be
                              installed.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.

        Exceptions:

        ADBError - raised if the app could not be installed.
        """
        data = self.command_output(["install", apk_path], timeout=timeout)
        if data.find('Success') == -1:
            raise ADBError("install failed for %s. Got: %s" %
                           (apk_path, data))

    def uninstall_app(self, app_name, timeout=None):
        """
        Uninstalls an app on the device.

        :param app_name: string containing the name of the app to be uninstalled.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.

        Exceptions:

        ADBError - raised if the app could not be uninstalled. No error is
                   raised if the app is not already installed on the device.
        """
        if self.is_app_installed(app_name, timeout=timeout):
            data = self.command_output(["uninstall", app_name], timeout=timeout)
            if data.find('Success') == -1:
                raise ADBError("uninstall failed for %s. Got: %s" % (app_name, data))

    def update_app(self, app_bundle_path, timeout=None):
        """
        Updates an app on the device.

        :param app_bundle_path: string containing the apk file name to be updated.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        return self.command_output(["install", "-r", app_bundle_path],
                                   timeout=timeout)

    def get_prop(self, prop, timeout=None):
        """
        Gets value of a property from the device.

        :param prop: string containing the propery name.
        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        output = self.shell_output('getprop %s' % prop, timeout=timeout)
        return output

    def power_on(self, timeout=None):
        """
        Sets the device's power stayon value.

        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.

        Exceptions:

        ADBError - raises ADBError exception if the command returns a
                   non-zero exitcode not equal to 137.
        """
        try:
            self.shell_output('svc power stayon true', timeout=timeout)
        except ADBError, e:
            # executing this via adb shell errors, but not interactively.
            if e.result['exitcode'] != 137:
                raise
            self._logger.warning('Unable to set power stayon true: %s' % e)

    def get_state(self, timeout=None):
        """
        Returns the device's state.

        :param timeout: optional integer specifying the maximum time in seconds
                        for any spawned adb process to complete before throwing
                        an ADBTimeout.
                        This timeout is per adb call. The total time spent
                        may exceed this value. If it is not specified, the value
                        set in the ADB constructor is used.
        """
        output = self.command_output(["get-state"], timeout=timeout).strip()
        return output
