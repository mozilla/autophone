AutoPhone, a mobile-device manager for automated-test frameworks
================================================================

AutoPhone controls one or more mobile devices via Mozilla's SUT agent and
adb. Its primary goals are to

* execute arbitrary tests on devices
* provide basic device status
* between tests, verify that devices are still connected and responsive, and,
  if not, attempt to recover them

AutoPhone does not provide a test framework. Rather, it executes arbitrary
Python code, which can also launch and control subprocesses to execute tests
of any format and design.

The [project page](https:// wiki.mozilla.org/Auto-tools/Projects/AutoPhone)
contains more background, goals, implementation notes, and other such
information.

The main code repo is currently https://github.com/markrcote/autophone/

See also [phonedash](https://github.com/markrcote/phonedash) for a basic
results server. At some point, [DataZilla](https://github.com/mozilla/datazilla)
will obsolete this.

See the files INSTALL and USAGE for more detailed information.

