# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

class ProcessStates(object):
    """ProcessStates enumerates several string constants which are used,
    in combination with phonestatus.PhoneStatus, to direct the action
    of Autophone in response to received commands.

    ProcessStates are used in AutoPhone, PhoneWorker and
    PhoneWorkerSubProcess. When an AutoPhone, PhoneWorker or
    PhoneWorkerSubProcess object is initialized, the object's `state`
    property is set to STARTING.

    When AutoPhone calls PhoneWorker.start() to start the
    PhoneWorkerSubProcess processes, AutoPhone.state and
    PhoneWorker.state are set to RUNNING. When PhoneWorker.start()
    starts the PhoneWorkerSubProcess process,
    PhoneWorkerSubProcess.run() sets PhoneWorkerSubProcess.state to
    RUNNING.

    RESTARTING is used for both AutoPhone and PhoneWorker when they
    will be restarted.

    SHUTTINGDOWN is used for AutoPhone, PhoneWorker and
    PhoneWorkerSubProcess when they are in the processing of shutting
    down cleanly after the currently running tests have completed.

    STOPPING is used for AutoPhone and PhoneWorker when they are in
    the process of stopping immediately with out waiting for the
    currently running tests to complete.
    """

    STARTING = 'STARTING'
    RUNNING = 'RUNNING'
    RESTARTING = 'RESTARTING'
    SHUTTINGDOWN = 'SHUTTINGDOWN'
    STOPPING = 'STOPPING'


