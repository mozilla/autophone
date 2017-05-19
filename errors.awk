# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

# Parse Autophone logs and output formatted error lines.
#
# This file is dependent upon the format of the Autophone log
# output lines. Should the Autophone log format change, this
# file will need to be updated.

BEGIN {
    line_no = 0
    REMAINDER_LEN = 120
    TEST_LEN = 120
}

function get_remainder(start, len,    remainder) {
    # Beginning with field start, catenate the fields
    # together and return the resulting string.
    remainder=""
    for (i=start; i <= NF; i++) {
        if (remainder)
            remainder = remainder " " $i
        else
            remainder = $i
    }
    remainder = substr(remainder,1, len)
    return remainder
}

function dump_variables() {
    if (DEBUG) {
        printf "device=%s\n", device
        printf "repo=%s\n", repo
        printf "buildid=%s\n", buildid
        printf "buildtype=%s\n", buildtype
        printf "sdk=%s\n", $12
        printf "platform=%s\n", $13
        printf "testsuite=%s\n", testsuite
        printf "test=%s\n", test
        printf "result=%s\n", result
        printf "remainder=%s\n", remainder
    }
    # If the result doesn't consist of capital letters, dashes
    # and spaces, skip the output.
    if (result ~ /^[A-Z -]+$/)
        printf "%-45s; %-120s; %-120s; %-20s; %-20s; %14s; %-5s; %-6s; %-22s; %-16s\n", testsuite, test, remainder, result, repo, buildid, buildtype, sdk, platform, device
    else
        if (DEBUG)
            printf "Skipped non-result %s\n", result
}

function dump_fields() {
    printf "========================================\n"
    printf "0=%s\n", $0
    for (i=1; i <= NF; i++)
        printf "%s=%s\n", i, $i
    printf "\n"
}

/^.*$/ {
    line_no++
    if (DEBUG) {
        printf "Line: %s\n", line_no
        dump_fields()
    }
}

/ [0-9]+ ERROR/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "[0-9]+ ERROR: %s\n", $0
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    result = $16
    test = $14
    if ($18 == "|")
        remainder = get_remainder(19, REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(17, REMAINDER_LEN, remainder)
    test = substr(test,1, TEST_LEN)
    dump_variables()
    next
}

/TEST-UNEXPECTED-[^|]+ [|] [^|]+ [|]/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "TEST-UNEXPECTED: %s\n", $0
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14

    if ($16 == "INFO") {
        if (DEBUG) {
            printf "INFO TEST-UNEXPECTED\n"
        }
        result = $17
        test = $19
        remainder = get_remainder(21, REMAINDER_LEN, remainder)
        remainder = substr(remainder,1, REMAINDER_LEN)
        test = substr(test,1, TEST_LEN)
        dump_variables()
    }
    else {
        if ($15 == "REFTEST") {
            if (DEBUG) {
                printf "REFTEST TEST-UNEXPECTED\n"
            }
            result = $16
            test = $18
            test = gensub(/http:\/\/[^\/]+\/tests\//, "", 1, test)
            remainder = get_remainder(20, REMAINDER_LEN, remainder)
            remainder = substr(remainder,1, REMAINDER_LEN)
            test = substr(test,1, TEST_LEN)
            dump_variables()
        }
        else {
            if (DEBUG) {
                printf "TEST-UNEXPECTED\n"
            }
            result = $15
            test = $17
            remainder = get_remainder(19, REMAINDER_LEN, remainder)
            test = substr(test,1, TEST_LEN)
            dump_variables()
        }
    }
    next
}

/PROCESS-CRASH/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "PROCESS-CRASH: %s\n", $0
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14

    if ($15 == "REFTEST") {
        if (DEBUG) {
            printf "----------------------------------------\n"
            printf "REFTEST PROCESS-CRASH: %s\n", $0
        }
        result = $16
        test = $18
        test = gensub(/http:\/\/[^\/]+\/tests\//, "", 1, test)
        remainder = get_remainder(20, REMAINDER_LEN, remainder)
        test = substr(test,1, TEST_LEN)
        dump_variables()
    }
    else {
        if (DEBUG) {
            printf "----------------------------------------\n"
            printf "PROCESS-CRASH: %s\n", $0
        }
        result = $15
        test = $17
        remainder = get_remainder(19, REMAINDER_LEN, remainder)
        test = substr(test,1, TEST_LEN)
        dump_variables()
    }
    next
}

/I.Gecko.*FATAL ERROR:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "I/Gecko.*FATAL ERROR: %s\n", $0
        dump_fields()
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    test = $14
    result = "FATAL ERROR"
    if ($15 == "logcat:"){
        remainder = get_remainder(21, REMAINDER_LEN, remainder)
    }
    else {
        remainder = get_remainder(20, REMAINDER_LEN, remainder)
    }
    dump_variables()
    next
}

/I.Gecko.*ABORT:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "I/Gecko.*ABORT: %s\n", $0
        dump_fields()
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    test = $14
    result = "FATAL ERROR"
    if ($15 == "logcat:")
        remainder = get_remainder(23, REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(22, REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/E.Gecko.*ABORT:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "E/Gecko.*ABORT: %s\n", $0
        dump_fields()
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    test = $14
    result = "FATAL ERROR"
    if ($15 == "logcat:")
        remainder = get_remainder(24, REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(23, REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/F.MOZ_CRASH/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "F/MOZ_CRASH: %s\n", $0
        dump_fields()
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    test = $14
    result = "FATAL ERROR"
    if ($15 == "logcat:")
        remainder = get_remainder(20, REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(19, REMAINDER_LEN, remainder)
    dump_variables()
    next
}

/MOZ_Assert.*Assertion failure:/ {
    if (DEBUG) {
        printf "----------------------------------------\n"
        printf "F/MOZ_Assert.*Assertion failure: %s\n", $0
        dump_fields()
    }
    device = $4
    repo = $9
    buildid = $10
    buildtype = $11
    sdk = $12
    platform = $13
    testsuite = $14
    test = $14
    result = "FATAL ERROR"
    if ($15 == "logcat:")
        remainder = get_remainder(20, REMAINDER_LEN, remainder)
    else
        remainder = get_remainder(19, REMAINDER_LEN, remainder)
    dump_variables()
    next
}
